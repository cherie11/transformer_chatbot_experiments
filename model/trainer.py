#  transformer_chatbot
#  Copyright (C) 2018 Golovanov, Tselousov
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
import math
import random

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, DistributedSampler, RandomSampler
from tensorboardX import SummaryWriter
from tqdm import tqdm

from .loss import LabelSmoothingLoss
from .optim import Adam, NoamOpt
from .utils import pad_sequence, repeat_along_dim1

logger = logging.getLogger(__file__)

class Trainer:
    def __init__(self, model, train_dataset, writer=SummaryWriter(), test_dataset=None, train_batch_size=8, test_batch_size=8,
                 batch_split=1, s2s_weight=1, lm_weight=0.5, risk_weight=0, hits_weight=0, lr=6.25e-5, lr_warmup=2000, 
                 n_jobs=0, clip_grad=None, label_smoothing=0, device=torch.device('cuda'), weight_decay=0.1,
                 ignore_idxs=[], local_rank=-1, fp16=False, loss_scale=0,
                 linear_schedule=False, n_epochs=0, single_input=False, evaluate_full_sequences=False):
        if local_rank != -1:
            torch.cuda.set_device(local_rank)
            device = torch.device("cuda", local_rank)
            torch.distributed.init_process_group(backend='nccl',
                                                init_method='env://')
        n_gpu = torch.cuda.device_count()
        logger.info("device: {}, distributed training: {}, 16-bits training: {}, n_gpu: {}".format(
            device, bool(local_rank != -1), fp16, n_gpu))

        self.model = model.to(device)
        if fp16:
            self.model.half()

        if local_rank != -1:
            try:
                from apex.parallel import DistributedDataParallel
                self.model = DistributedDataParallel(self.model)
            except ImportError:
                raise ImportError("Please install apex for distributed training.")
            # self.model = nn.parallel.DistributedDataParallel(self.model, device_ids=[device], output_device=device)
        elif n_gpu > 1 and batch_split % n_gpu == 0 and batch_split > n_gpu:
            self.model.transformer_module = nn.DataParallel(self.model.transformer_module)
            batch_split = batch_split // n_gpu

        self.lm_criterion = nn.CrossEntropyLoss(ignore_index=self.model.padding_idx).to(device)
        self.hits_criterion = nn.CrossEntropyLoss().to(device)
        self.criterion = LabelSmoothingLoss(n_labels=self.model.n_embeddings, smoothing=label_smoothing, ignore_index=self.model.padding_idx).to(device)

        param_optimizer = list(self.model.named_parameters())  # Here we should remove parameters which are not used during to avoid breaking apex with None grads
        no_decay = ['bias']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': weight_decay},
            {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
            ]

        try:
            # TODO (truskovskiyk 20.02.2019 FusedAdam raises nan errors for me)
            # from apex.optimizers import FusedAdam
            # base_optimizer = FusedAdam(optimizer_grouped_parameters, lr=lr, bias_correction=False, max_grad_norm=1.0)
            base_optimizer = Adam(optimizer_grouped_parameters, lr=lr)
        except ImportError:
            logger.info("Apex not found, not using FusedAdam.")
            base_optimizer = Adam(optimizer_grouped_parameters, lr=lr)
        if fp16:
            try:
                from apex.optimizers import FP16_Optimizer
                from apex.optimizers import FusedAdam
            except ImportError:
                raise ImportError("Please install apex for fp16.")
            base_optimizer = FusedAdam(optimizer_grouped_parameters, lr=lr, bias_correction=False, max_grad_norm=1.0)
            if loss_scale == 0:
                base_optimizer = FP16_Optimizer(base_optimizer, dynamic_loss_scale=True)
            else:
                base_optimizer = FP16_Optimizer(base_optimizer, static_loss_scale=loss_scale)
        if not linear_schedule:
            self.optimizer = NoamOpt(self.model.embeddings_size, 1, lr_warmup, base_optimizer, linear_schedule=False, fp16=fp16)
        else:
            total_steps = len(train_dataset) * n_epochs // train_batch_size
            if local_rank != -1:
                total_steps = total_steps // torch.distributed.get_world_size()
            self.optimizer = NoamOpt(self.model.embeddings_size, 1, lr_warmup, base_optimizer, linear_schedule=True,
            lr=lr, total_steps=total_steps, fp16=fp16)

        if local_rank == -1:
            train_sampler = RandomSampler(train_dataset)
        else:
            train_sampler = DistributedSampler(train_dataset)
        self.train_dataloader = DataLoader(train_dataset, batch_size=train_batch_size//batch_split, sampler=train_sampler, 
                                           num_workers=n_jobs, collate_fn=self.collate_func)
        self.train_dataset = train_dataset  # used to sample negative examples
        if test_dataset is not None and local_rank in [-1, 0]:  # only do evaluation on main process
            self.test_dataloader = DataLoader(test_dataset, batch_size=test_batch_size, shuffle=False, 
                                              num_workers=n_jobs, collate_fn=self.collate_func)
        self.vocab = train_dataset.vocab
        self.writer = writer

        self.batch_split = batch_split
        self.s2s_weight = s2s_weight
        self.lm_weight = lm_weight
        self.risk_weight = risk_weight
        self.hits_weight = hits_weight
        self.clip_grad = clip_grad
        self.device = device
        self.ignore_idxs = ignore_idxs
        self.fp16 = fp16
        self.n_epochs = n_epochs
        self.single_input = single_input
        self.evaluate_full_sequences = evaluate_full_sequences

    def state_dict(self):
        return {'model': self.model.state_dict(),
                'optimizer': self.optimizer.state_dict()}

    def load_state_dict(self, state_dict):
        self.model.load_state_dict(state_dict['model'], strict=False)
        self.optimizer.load_state_dict(state_dict['optimizer'])

    def collate_func(self, data):
        persona_info, h, y, distractors_batch = zip(*data)

        contexts = []

        if max(map(len, persona_info)) > 0:
            persona_info = [torch.tensor(d, dtype=torch.long) for d in persona_info]
            contexts.append(persona_info)

        if max(map(len, h)) > 0:
            h = [torch.tensor(d, dtype=torch.long) for d in h]
            contexts.append(h)

        y_out = [torch.tensor(d, dtype=torch.long) for d in y]

        distractors = [torch.tensor(d, dtype=torch.long) for distractors in distractors_batch for d in distractors]

        if self.single_input:
            # we concatenate all the contexts in y (idem for distractors)
            y_out = [torch.cat(pieces, dim=0) for pieces in zip(*(contexts + [y_out]))]
            extended_contexts = [[t for t in c for _ in range(len(distractors)//len(y))] for c in contexts]
            distractors = [torch.cat(pieces, dim=0) for pieces in zip(*(extended_contexts + [distractors]))]
            contexts = [torch.cat(pieces, dim=0) for pieces in zip(*(contexts))]

        # Pad now so we pad correctly when we have only a single input (context concatenated with y)
        y_out = pad_sequence(y_out, batch_first=True, padding_value=self.model.padding_idx)
        distractors = pad_sequence(distractors, batch_first=True, padding_value=self.model.padding_idx)
        if not self.single_input:
            contexts = [pad_sequence(c, batch_first=True, padding_value=self.model.padding_idx) for c in contexts]
        else:
            contexts = [t.unsqueeze(0) for t in contexts]

        return contexts, y_out, distractors

    def _eval_train(self, epoch, risk_func=None): # add ppl and hits@1 evaluations
        self.model.train()

        tqdm_data = tqdm(self.train_dataloader, desc='Train (epoch #{})'.format(epoch))
        s2s_loss = 0
        lm_loss = 0
        risk_loss = 0
        hits_loss = 0
        for i, (contexts, targets, distractors) in enumerate(tqdm_data):
            contexts, targets, distractors = [c.to(self.device) for c in contexts], targets.to(self.device), distractors.to(self.device)

            negative_samples = len(distractors)//len(targets)
            enc_contexts = []

            # lm loss on contexts
            batch_lm_loss = torch.tensor(0, dtype=torch.float, device=self.device)
            if not self.single_input:
                for context in contexts:
                    enc_context = self.model.encode(context.clone())
                    enc_contexts.append(enc_context)

                    if self.lm_weight > 0:
                        context_outputs = self.model.generate(enc_context[0])
                        ignore_mask = torch.stack([context == idx for idx in self.ignore_idxs], dim=-1).any(dim=-1)
                        context.masked_fill_(ignore_mask, self.model.padding_idx)
                        prevs = context_outputs[:, :-1, :].contiguous()
                        nexts = context[:, 1:].contiguous() if context.dim() == 2 else context[:, 1:, 0].contiguous()
                        batch_lm_loss += (self.lm_criterion(prevs.view(-1, prevs.shape[-1]).float(), nexts.view(-1)) / len(contexts))

            # s2s loss on targets
            nexts = targets[:, 1:].contiguous() if targets.dim() == 2 else targets[:, 1:, 0].contiguous()
            if self.hits_weight > 0 and negative_samples > 0:
                # Keep the hidden states for hits@1 loss
                hidden_state, padding_mask, _ = self.model.transformer_module(targets, enc_contexts)
                outputs = self.model.generate(hidden_state[:, :-1].contiguous())
            else:
                outputs = self.model.decode(targets[:, :-1].contiguous(), enc_contexts)
            outputs = F.log_softmax(outputs.float(), dim=-1)
            batch_s2s_loss = self.criterion(outputs.view(-1, outputs.shape[-1]), nexts.view(-1))

            # hits@1 loss on distractors and targets
            if self.hits_weight > 0 and negative_samples > 0:
                extended_contexts = repeat_along_dim1(enc_contexts, negative_samples)
                neg_logits = self.model.decode_classify(distractors, extended_contexts)
                true_logits = self.model.classify(hidden_state, padding_mask)
                clf_logits = torch.cat((true_logits.view(-1, 1), neg_logits.view(-1, negative_samples)), dim=1)
                clf_labels = torch.tensor([0] * len(true_logits), dtype=torch.long, device=self.device)
                batch_hits_loss = self.hits_criterion(clf_logits.float(), clf_labels)

            # risk loss
            batch_risk_loss = torch.tensor(0, dtype=torch.float, device=self.device)
            if risk_func is not None and self.risk_weight > 0:
                self.model.eval()  # desactivate dropout
                if self.single_input:
                    beams, beam_lens = []
                    for b in range(targets.shape[0]):
                        beam_outputs = self.model.beam_search(beam_starts=contexts[b], return_beams=True)
                        beams.append(beam_outputs[0])
                        beam_lens.append(beam_outputs[1])
                    beams = torch.cat(beams, dim=0)
                    beam_lens = torch.cat(beam_lens, dim=0)
                else:
                    beams, beam_lens = self.model.beam_search(enc_contexts=enc_contexts, beam_starts=None, return_beams=True)
                self.model.train()  # re-activate dropout

                labels = targets if targets.dim() == 2 else targets[:, :, 0]
                labels_lens = labels.ne(self.model.padding_idx).sum(dim=-1)
                labels_start = [context.shape[1] + 1 for context in contexts] if self.single_input else [1] * len(contexts)
                labels = [t[s:l-1].tolist() for t, s, l in zip(labels, labels_start, labels_lens)]
                batch_risks = []
                for b in range(beams.shape[1]):
                    predictions = [b[1:l-1].tolist() for b, l in zip(beams[:, b, :], beam_lens[:, b])]
                    risks = torch.tensor(risk_func(predictions, labels), dtype=torch.float, device=self.device)
                    batch_risks.append(risks)
                batch_risks = torch.stack(batch_risks, dim=-1)

                batch_probas = []
                for b in range(beams.shape[1]):
                    logits = self.model.decode(beams[:, b, :-1], enc_contexts)
                    probas = F.log_softmax(logits.float(), dim=-1)
                    probas = torch.gather(probas, -1, beams[:, b, 1:].unsqueeze(-1)).squeeze(-1)
                    probas = probas.sum(dim=-1) / beam_lens[:, b].float()
                    batch_probas.append(probas)
                batch_probas = torch.stack(batch_probas, dim=-1)
                batch_probas = F.softmax(batch_probas, dim=-1)

                batch_risk_loss = torch.mean((batch_risks * batch_probas).sum(dim=-1))

            # optimization
            full_loss = (self.lm_weight * batch_lm_loss
                         + self.risk_weight * batch_risk_loss
                         + self.hits_weight * batch_hits_loss
                         + self.s2s_weight * batch_s2s_loss) / self.batch_split
            self.optimizer.backward(full_loss)

            lm_loss = (i * lm_loss + batch_lm_loss.item()) / (i + 1)
            s2s_loss = (i * s2s_loss + batch_s2s_loss.item()) / (i + 1)
            risk_loss = (i * risk_loss + batch_risk_loss.item()) / (i + 1)
            hits_loss = (i * hits_loss + batch_hits_loss.item()) / (i + 1)
            tqdm_data.set_postfix({'lm_loss': lm_loss, 's2s_loss': s2s_loss, 'risk_loss': risk_loss, 'hits_loss': hits_loss})

            if (i + 1) % self.batch_split == 0:
                if self.clip_grad is not None:
                    for group in self.optimizer.param_groups:
                        nn.utils.clip_grad_norm_(group['params'], self.clip_grad)

                self.optimizer.step()
                self.optimizer.zero_grad()

                # logging
                global_step = (epoch * len(self.train_dataloader) + (i + 1)) // self.batch_split
                self.writer.add_scalar("losses/batch_lm_loss", batch_lm_loss.item(), global_step=global_step)
                self.writer.add_scalar("losses/batch_risk_loss", batch_risk_loss.item(), global_step=global_step)
                self.writer.add_scalar("losses/batch_hits_loss", batch_hits_loss.item(), global_step=global_step)
                self.writer.add_scalar("losses/batch_s2s_loss", batch_s2s_loss.item(), global_step=global_step)
                self.writer.add_scalar("losses/full_loss", full_loss.item(), global_step=global_step)
                self.writer.add_scalar("training/lr", self.optimizer.get_lr(), global_step=global_step)


    def _eval_test(self, metric_funcs={}, external_metrics_func=None, epoch=-1):
        with torch.no_grad():
            self.model.eval()

            tqdm_data = tqdm(self.test_dataloader, desc='Test')
            metrics = {name: 0 for name in ('s2s_loss', 'lm_loss', 'hits_acc') + tuple(metric_funcs.keys())}
            full_references, full_predictions = [], []
            for i, (contexts, targets, distractors) in enumerate(tqdm_data):
                contexts, targets, distractors = [c.to(self.device) for c in contexts], targets.to(self.device), distractors.to(self.device)

                negative_samples = len(distractors)//len(targets)
                enc_contexts = []

                # lm loss
                batch_lm_loss = torch.tensor(0, dtype=torch.float, device=self.device)
                if not self.single_input:
                    for context in contexts:
                        enc_context = self.model.encode(context.clone())
                        enc_contexts.append(enc_context)

                        context_outputs = self.model.generate(enc_context[0])
                        ignore_mask = torch.stack([context == idx for idx in self.ignore_idxs], dim=-1).any(dim=-1)
                        context.masked_fill_(ignore_mask, self.model.padding_idx)
                        prevs = context_outputs[:, :-1, :].contiguous()
                        nexts = context[:, 1:].contiguous() if context.dim() == 2 else context[:, 1:, 0].contiguous()
                        batch_lm_loss += (self.lm_criterion(prevs.view(-1, prevs.shape[-1]).float(), nexts.view(-1)) / len(contexts))
                metrics['lm_loss'] = (metrics['lm_loss'] * i + batch_lm_loss.item()) / (i + 1)

                # s2s loss on targets
                nexts = targets[:, 1:].contiguous() if targets.dim() == 2 else targets[:, 1:, 0].contiguous()
                if negative_samples > 0:
                    # Keep the hidden states for hits@1 loss
                    hidden_state, padding_mask, _ = self.model.transformer_module(targets, enc_contexts)
                    outputs = self.model.generate(hidden_state[:, :-1].contiguous())
                else:
                    outputs = self.model.decode(targets[:, :-1].contiguous(), enc_contexts)
                batch_s2s_loss = self.lm_criterion(outputs.view(-1, outputs.shape[-1]).float(), nexts.view(-1))  # We evaluate with CrossEntropy
                metrics['s2s_loss'] = (metrics['s2s_loss'] * i + batch_s2s_loss.item()) / (i + 1)

                # hits@1 loss on distractors and targets
                batch_hits_acc = torch.tensor(0, dtype=torch.float, device=self.device)
                if negative_samples > 0:
                    extended_contexts = repeat_along_dim1(enc_contexts, negative_samples)
                    neg_logits = self.model.decode_classify(distractors, extended_contexts)
                    true_logits = self.model.classify(hidden_state, padding_mask)
                    clf_logits = torch.cat((true_logits.view(-1, 1), neg_logits.view(-1, negative_samples)), dim=1)
                    clf_labels = torch.tensor([0] * len(true_logits), dtype=torch.long, device=self.device)
                    batch_hits_acc = torch.sum(torch.max(clf_logits, dim=1)[1] == clf_labels).float() / clf_labels.shape[0]
                metrics['hits_acc'] = (metrics['hits_acc'] * i + batch_hits_acc.item()) / (i + 1)

                # full sequence loss
                if self.evaluate_full_sequences:
                    if self.single_input:
                        predictions = []
                        for b in range(targets.shape[0]):
                            beam_outputs = self.model.beam_search(beam_starts=contexts[b])
                            predictions.append(beam_outputs)
                        predictions = sum(predictions, [])
                    else:
                        predictions = self.model.beam_search(enc_contexts=enc_contexts, beam_starts=torch.cat(contexts, dim=1) if self.single_input else None)
                    labels = targets if targets.dim() == 2 else targets[:, :, 0]
                    labels_lens = labels.ne(self.model.padding_idx).sum(dim=-1)
                    labels_start = [context.shape[1] + 1 for context in contexts] if self.single_input else [1] * len(contexts)
                    labels = [t[s:l-1].tolist() for t, s, l in zip(labels, labels_start, labels_lens)]

                    for name, func in metric_funcs.items():
                        score = func(predictions, labels)
                        metrics[name] = (metrics[name] * i + score) / (i + 1)

                    if external_metrics_func:
                        # Store text strings for external metrics
                        string_targets = list(self.vocab.ids2string(t) for t in labels)
                        string_predictions = list(self.vocab.ids2string(t) for t in predictions)
                        full_references.extend(string_targets)
                        full_predictions.extend(string_predictions)

                tqdm_data.set_postfix(dict((n, math.exp(v) if 'loss' in n else v) for n, v in metrics.items()))

            if external_metrics_func and self.evaluate_full_sequences:
                external_metrics = external_metrics_func(full_references, full_predictions)
                metrics.update(external_metrics)

            # logging
            global_step = epoch * len(self.train_dataloader) // self.batch_split
            for key, value in metrics.items():
                self.writer.add_scalar("eval/{}".format(key), value, global_step=global_step)
            logger.info(metrics)

    def test(self, metric_funcs={}, external_metrics_func=None, epoch=-1):
        if hasattr(self, 'test_dataloader'):
            self._eval_test(metric_funcs, external_metrics_func, epoch)

    def train(self, after_epoch_funcs=[], risk_func=None):
        for epoch in range(self.n_epochs):
            self._eval_train(epoch, risk_func)

            for func in after_epoch_funcs:
                func(epoch)

        if self.n_epochs == 0:
            for func in after_epoch_funcs:
                func(-1)

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
import random
import copy

import torch
import torch.nn as nn
import torch.nn.functional as F

from .transformer_module import TransformerModule
from .utils import repeat_along_dim1

logger = logging.getLogger(__file__)


def apex_model(model, *, apex_level=None, optimizer=None, apex_loss_scale=None, num_losses=4):
    if apex_level is not None:
        assert apex_level == 'O0' or model.sparse_embeddings == False, 'Apex doesn\'t support sparse tensors'

        try:
            from apex.amp import initialize
        except ImportError:
            raise ImportError("Please install apex.")

        return initialize(model, optimizer, opt_level=apex_level, loss_scale=apex_loss_scale, num_losses=num_losses)

    return model if optimizer is None else (model, optimizer)


class MultipleChoiceHead(nn.Module):
    """ Classifier Head for the transformer """

    def __init__(self, in_features, dropout):
        super(MultipleChoiceHead, self).__init__()
        self.dropout = nn.Dropout(dropout)
        self.linear = nn.Linear(in_features, 1)

        self._init_weights()

    def _init_weights(self):
        nn.init.normal_(self.linear.weight, std=0.02)
        nn.init.normal_(self.linear.bias, 0)

    def forward(self, hidden_state, padding_mask):
        # Get classification logits as the last logit and apply a Linear layer on them
        # hidden_state (bsz, seq_length, hidden_size)
        # padding_mask (bsz, seq_length)
        last_token_idx = torch.sum(~padding_mask, dim=-1) - 1  # (bsz)
        last_token_idx = last_token_idx.unsqueeze(-1).unsqueeze(-1).expand(-1, -1, hidden_state.size(-1))  # (bsz, 1, hidden_size)
        multiple_choice_h = hidden_state.gather(dim=-2, index=last_token_idx).squeeze(-2)  # (bsz, hidden_size)
        multiple_choice_logits = self.linear(multiple_choice_h).squeeze(-1)  # (bsz)
        return multiple_choice_logits


class TransformerModel(nn.Module):
    def __init__(self, n_layers, n_embeddings, n_pos_embeddings, embeddings_size, 
                 padding_idx, n_heads, dropout, embed_dropout, attn_dropout, ff_dropout,
                 bos_id, eos_id, sent_dialog_id, max_seq_len=256, beam_size=5, sample_best_beam=False,
                 length_penalty=0.8, annealing_topk=None, annealing_proba=0,
                 diversity_coef=0, diversity_groups=1, multiple_choice_head=False,
                 single_input=False, dialog_embeddings=False, constant_pos_embedding=False,
                 shared_enc_dec=True, successive_attention=False, sparse_embeddings=False,
                 shared_attention=True, context_size=2):

        super(TransformerModel, self).__init__()

        self.n_embeddings = n_embeddings
        self.n_pos_embeddings = n_pos_embeddings
        self.embeddings_size = embeddings_size
        self.sparse_embeddings = sparse_embeddings

        self.bos_id = bos_id
        self.padding_idx = padding_idx
        self.eos_id = eos_id
        self.sent_dialog_id = sent_dialog_id

        self.max_seq_len = max_seq_len
        self.beam_size = beam_size
        self.sample_best_beam = sample_best_beam
        self.length_penalty_coef = length_penalty
        self.annealing_proba = annealing_proba
        self.annealing_topk = annealing_topk
        self.diversity_coef = diversity_coef
        self.diversity_groups = diversity_groups

        self.single_input = single_input
        self.dialog_embeddings = dialog_embeddings

        self.decoder = TransformerModule(n_layers, n_embeddings, n_pos_embeddings, embeddings_size, padding_idx,
                                         n_heads, dropout, embed_dropout, attn_dropout, ff_dropout,
                                         constant_pos_embedding=constant_pos_embedding,
                                         successive_attention=successive_attention,
                                         sparse_embedding=sparse_embeddings,
                                         shared_attention=shared_attention,
                                         context_size=context_size)
        self.encoder = self.decoder if shared_enc_dec else copy.deepcopy(self.decoder)
        self.pre_softmax = nn.Linear(embeddings_size, n_embeddings, bias=False)
        self.pre_softmax.weight = self.decoder.embedding.tok_embedding.weight
        self.multiple_choice_head = MultipleChoiceHead(self.embeddings_size, dropout) if multiple_choice_head else None

    def forward(self, x, contexts=[]):
        enc_contexts = [self.encode(c) for c in contexts]
        return self.decode(x, enc_contexts)

    def encode(self, x):
        " Returns a tuple(x, padding_mask)"
        x, padding_mask, _ = self.encoder(x)
        return x, padding_mask

    def generate(self, enc_x):
        return self.pre_softmax(enc_x)

    def classify(self, x, padding_mask):
        return self.multiple_choice_head(x, padding_mask)

    def decode_classify(self, x, enc_contexts=[]):
        x, padding_mask, _ = self.decoder(x, enc_contexts)
        return self.classify(x, padding_mask)

    def decode(self, x, enc_contexts=[]):
        x, _, _ = self.decoder(x, enc_contexts)
        return self.generate(x)

    def predict(self, contexts=[]):
        if self.single_input:
            assert isinstance(contexts, torch.Tensor)
            enc_contexts = []
            beam_starts = contexts
        else:
            enc_contexts = [self.encode(c) for c in contexts]
            beam_starts = None
        prediction = self.beam_search(enc_contexts=enc_contexts, beam_starts=beam_starts)

        return prediction

    def _length_penalty(self, sequence_lengths):
        """https://arxiv.org/abs/1609.08144"""
        return (5 + sequence_lengths) ** self.length_penalty_coef / (5 + 1) ** self.length_penalty_coef

    def _sample(self, beam_scores, num_samples, sample_prob=1.):
        if random.random() < sample_prob:
            beam_probas = F.softmax(beam_scores, dim=-1)
            if self.annealing_topk is not None:
                beam_probas, sample_idxs = beam_probas.topk(self.annealing_topk, dim=-1)
                idxs = torch.multinomial(beam_probas, num_samples)
                idxs = torch.gather(sample_idxs, 1, idxs)
            else:
                idxs = torch.multinomial(beam_probas, num_samples)

            scores = torch.gather(beam_scores, 1, idxs)
        else:
            scores, idxs = beam_scores.topk(num_samples, dim=-1)

        return scores, idxs

    def _fix_past(self, past, beam_idxs):
        for layer_output in past:
            for context in layer_output:
                for v in context:
                    size_ = v.size()
                    tile_size = size_[-2] * size_[-1]
                    new_v = v.contiguous().view(-1, self.beam_size, tile_size)
                    new_v = new_v.gather(1, beam_idxs.unsqueeze(-1).repeat([1, 1, tile_size]))
                    v[...] = new_v.view(*size_)
        return past

    def beam_search(self, enc_contexts=[], return_beams=False, beam_starts=None):
        with torch.no_grad():
            if len(enc_contexts) == 0 and beam_starts is None:
                return []

            batch_size = enc_contexts[0][0].shape[0] if beam_starts is None else beam_starts.shape[0]
            device = next(self.parameters()).device

            prevs = torch.full((batch_size * self.beam_size, 1), fill_value=self.bos_id, dtype=torch.long, device=device)

            beam_scores = torch.zeros(batch_size, self.beam_size, device=device)
            beam_lens = torch.ones(batch_size, self.beam_size, dtype=torch.long, device=device)
            is_end = torch.zeros(batch_size, self.beam_size, dtype=torch.uint8, device=device)

            if beam_starts is not None:
                beam_starts = repeat_along_dim1(beam_starts, self.beam_size)
            beam_enc_contexts = repeat_along_dim1(enc_contexts, self.beam_size)

            current_sample_prob = 1
            group_size = self.beam_size // self.diversity_groups
            diversity_penalty = torch.zeros((batch_size, self.n_embeddings), device=device)
            past = None

            max_seq_len = min(self.n_pos_embeddings - prevs.shape[1] - (beam_starts.shape[1] if beam_starts is not None else 0),
                              self.max_seq_len)

            for i in range(max_seq_len):
                inputs = prevs[:, -1:, ...]  # only use the last token (rest is in past)
                if self.dialog_embeddings and inputs.dim() < 3:
                    inputs = torch.stack((inputs, torch.full_like(inputs, self.sent_dialog_id)), dim=inputs.dim())
                if i == 0 and beam_starts is not None:
                    inputs = torch.cat((beam_starts, inputs), dim=1)

                outputs, _, past = self.decoder(inputs, beam_enc_contexts, past=past)

                logits = self.generate(outputs[:, -1, :])
                log_probs = F.log_softmax(logits.float(), dim=-1)
                log_probs = log_probs.view(batch_size, self.beam_size, -1)
                beam_scores = beam_scores.unsqueeze(-1) + log_probs * (1 - is_end.float().unsqueeze(-1))
                penalty = self._length_penalty(beam_lens.float() + 1 - is_end.float()).unsqueeze(-1)
                beam_scores = beam_scores / penalty

                if i == 0:
                    penalty = penalty[:, 0, :]
                    beam_scores = beam_scores[:, 0, :]

                    beam_scores, idxs = beam_scores.topk(self.beam_size, dim=-1)
                    beam_idxs = torch.zeros((batch_size, self.beam_size), dtype=torch.long, device=device)
                else:
                    penalty = penalty.view(batch_size, self.diversity_groups, group_size, -1)
                    beam_scores = beam_scores.view(batch_size, self.diversity_groups, group_size, -1)

                    all_scores, all_idxs = [], []
                    for g in range(self.diversity_groups):
                        g_beam_scores = beam_scores[:, g, :, :]
                        g_penalty = penalty[:, g, :, :]
                        g_beam_scores -= self.diversity_coef * diversity_penalty.unsqueeze(1) / g_penalty
                        g_beam_scores = g_beam_scores.view(batch_size, -1)

                        g_scores, g_idxs = self._sample(g_beam_scores, group_size, sample_prob=current_sample_prob)
                        g_idxs += g * group_size * self.n_embeddings

                        all_scores.append(g_scores)
                        all_idxs.append(g_idxs)

                        diversity_penalty.scatter_add_(1,
                                                       torch.fmod(g_idxs, self.n_embeddings),
                                                       torch.ones((batch_size, group_size), device=device))

                    diversity_penalty.fill_(0)
                    penalty = penalty.view(batch_size, -1)
                    beam_scores = torch.cat(all_scores, dim=-1)
                    idxs = torch.cat(all_idxs, dim=-1) 

                    beam_idxs = (idxs.float() / self.n_embeddings).long()

                sym_idxs = torch.fmod(idxs, log_probs.shape[-1])
                is_end = torch.gather(is_end, 1, beam_idxs)
                beam_lens = torch.gather(beam_lens, 1, beam_idxs)

                sym_idxs[is_end] = self.padding_idx
                beam_lens[~is_end] += 1
                is_end[sym_idxs == self.eos_id] = 1

                sym_idxs = sym_idxs.view(batch_size * self.beam_size, 1)
                prevs = prevs.view(batch_size, self.beam_size, -1)
                prevs = torch.gather(prevs, 1, beam_idxs.unsqueeze(-1).repeat(1, 1, prevs.shape[-1]))
                prevs = prevs.view(batch_size * self.beam_size, -1)
                prevs = torch.cat([prevs, sym_idxs], dim=1)

                past = self._fix_past(past, beam_idxs)

                if all(is_end.view(-1)):
                    break

                beam_scores *= penalty
                current_sample_prob *= self.annealing_proba

            predicts = []
            result = prevs.view(batch_size, self.beam_size, -1)

            if return_beams:
                return result, beam_lens

            if self.sample_best_beam:
                probs = F.softmax(beam_scores, dim=-1)
                bests = torch.multinomial(probs, 1).view(-1)
            else:
                bests = beam_scores.argmax(dim=-1)
            
            for i in range(batch_size):
                best_len = beam_lens[i, bests[i]]
                best_seq = result[i, bests[i], 1:best_len-1]
                predicts.append(best_seq.tolist())
                
        return predicts

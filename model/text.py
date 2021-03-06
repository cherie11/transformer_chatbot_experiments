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

from collections import Counter, defaultdict

import ftfy
import spacy
from tqdm import trange


class SpacyLowerTokenizer:
    def __init__(self):
        self.tokenizer = spacy.load('en', disable=['parser', 'tagger', 'ner', 'textcat'])

    def __call__(self, string):
        string = ftfy.fix_text(string)
        words = [t.text.strip() for t in self.tokenizer(string)]
        words = [w.lower() for w in words if w]

        return words

class BPEVocab:
    we = '</w>'

    pad_token = '<pad>'
    bos_token = '<s>'
    eos_token = '</s>'
    info_bos = '<i>'
    info_eos = '</i>'
    talker1_bos = '<t1>'
    talker1_eos = '</t1>'
    talker2_bos = '<t2>'
    talker2_eos = '</t2>'
    sent_dialog_token = '<s>'
    info_dialog_token = '<i>'
    talker1_dialog_token = '<t1>'
    talker2_dialog_token = '<t2>'

    @staticmethod
    def from_files(vocab_path, codes_path, *args, **kwargs):
        with open(vocab_path, 'r', encoding='utf-8') as vocab_file:
            vocab = [t.strip() for t in vocab_file.readlines()]

        with open(codes_path, 'r', encoding='utf-8') as codes_file:
            codes = [c.strip() for c in codes_file.readlines()]

            if codes[0].startswith('#version'):
                codes = codes[1:]

            codes = [tuple(c.split()) for c in codes if c]

        return BPEVocab(vocab, codes, *args, **kwargs)

    @staticmethod
    def get_pairs(string):
        if len(string) < 2:
            return set()

        return set(zip(string[:-1], string[1:]))

    def __init__(self, vocab, codes, tokenizer=SpacyLowerTokenizer(), zero_shot=False):
        if zero_shot: # only one additional token: BPEVocab.pad_token = <pad>
            self.spec_tokens = [BPEVocab.pad_token]
            self.bos_token = '"</w>'
            self.eos_token = '"</w>'
            self.info_bos = '.</w>'
            self.info_eos = '.</w>'
            self.talker1_bos = '"</w>'
            self.talker1_eos = '"</w>'
            self.talker2_bos = '"</w>'
            self.talker2_eos = '"</w>'
            self.sent_dialog_token = '"</w>'
            self.info_dialog_token = '.</w>'
            self.talker1_dialog_token = '"</w>'
            self.talker2_dialog_token = '"</w>'
        else:
            #TODO: add check for special tokens
            self.spec_tokens = [BPEVocab.pad_token, BPEVocab.bos_token, BPEVocab.eos_token,
                                BPEVocab.info_bos, BPEVocab.info_eos, BPEVocab.talker1_bos,
                                BPEVocab.talker1_eos, BPEVocab.talker2_bos, BPEVocab.talker2_eos]

        vocab = self.spec_tokens + vocab

        self.token2id = {t: i for i, t in enumerate(vocab)}
        self.id2token = {i: t for i, t in enumerate(vocab)}
        self.bpe_ranks = dict(zip(codes, range(len(codes))))
        self.tokenizer = tokenizer
        self.cache = {}

    def __len__(self):
        return len(self.token2id)

    @property
    def n_special_tokens(self):
        return len(self.spec_tokens)

    @property
    def special_tokens_ids(self):
        return [self.token2id[t] for t in self.spec_tokens]

    @property
    def pad_id(self):
        return self.token2id[self.pad_token]

    @property
    def bos_id(self):
        return self.token2id[self.bos_token]

    @property
    def eos_id(self):
        return self.token2id[self.eos_token]

    @property
    def info_bos_id(self):
        return self.token2id[self.info_bos]

    @property
    def info_eos_id(self):
        return self.token2id[self.info_eos]

    @property
    def talker1_bos_id(self):
        return self.token2id[self.talker1_bos]

    @property
    def talker1_eos_id(self):
        return self.token2id[self.talker1_eos]

    @property
    def talker2_bos_id(self):
        return self.token2id[self.talker2_bos]

    @property
    def talker2_eos_id(self):
        return self.token2id[self.talker2_eos]

    @property
    def sent_dialog_id(self):
        return self.token2id[self.sent_dialog_token]

    @property
    def info_dialog_id(self):
        return self.token2id[self.info_dialog_token]

    @property
    def talker1_dialog_id(self):
        return self.token2id[self.talker1_dialog_token]

    @property
    def talker2_dialog_id(self):
        return self.token2id[self.talker2_dialog_token]

    def get_prefix2words(self, convai_dict, smoothing_freq=5):
        # map BPE-prefix => dict(full_words beginning with BPE-prefix, associated words_counts)
        prefix2words = defaultdict(dict)
        for i in trange(len(convai_dict)):
            word = convai_dict[i]
            freq = convai_dict.freq[word] + smoothing_freq
            prefix = self._bpe(word)[0]
            prefix2words[prefix].update(dict([(word, freq)]))

        # translate in map of frequency ratios
        for prefix, words in prefix2words.items():
            total_counts = sum(words.values())
            prefix2words[prefix] = dict((word, count/total_counts) for word, count in words.items())

        return prefix2words

    def _bpe(self, token):
        if token in self.cache:
            return self.cache[token]

        word = tuple(token[:-1]) + (token[-1] + BPEVocab.we,)
        pairs = BPEVocab.get_pairs(word)

        if not pairs:
            return (token + BPEVocab.we,)

        while True:
            bigram = min(pairs, key=lambda pair: self.bpe_ranks.get(pair, float('inf')))
            if bigram not in self.bpe_ranks:
                break

            first, second = bigram
            new_word = []
            i = 0
            while i < len(word):
                try:
                    j = word.index(first, i)
                    new_word.extend(word[i:j])
                    i = j
                except:
                    new_word.extend(word[i:])
                    break

                if word[i] == first and i < len(word)-1 and word[i+1] == second:
                    new_word.append(first + second)
                    i += 2
                else:
                    new_word.append(word[i])
                    i += 1

            word = tuple(new_word)

            if len(word) == 1:
                break
            else:
                pairs = BPEVocab.get_pairs(word)

        self.cache[token] = word

        return word

    def string2ids(self, string):
        tokens = self.tokenizer(string)
        bpe_tokens = sum([self._bpe(t) for t in tokens], tuple())
        ids = [self.token2id[t] for t in bpe_tokens if t in self.token2id]

        return ids

    @staticmethod
    def to_ids_list(list_obj):
        # Take care of inputs with dialog embeddings (list of pairs, we keep only the first item in the pairs) and single int inputs
        if len(list_obj) == 0:
            return []
        if isinstance(list_obj, int):
            return [list_obj]
        if isinstance(list_obj[0], int):
            return list_obj
        assert isinstance(list_obj[0][0], int)
        return list(item[0] for item in list_obj)

    def ids2string(self, ids):
        ids = self.to_ids_list(ids)
        bpe_tokens = [self.id2token[id] for id in ids]

        return ''.join(bpe_tokens).replace(BPEVocab.we, ' ')

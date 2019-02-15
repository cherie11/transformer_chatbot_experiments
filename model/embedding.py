import math

import torch
from torch import nn


class BaseEmbedding(nn.Module):
    def __init__(self, *, embedding_dim=None, padding_idx=None, **kwargs):
        super(BaseEmbedding, self).__init__()

        assert embedding_dim is not None and padding_idx is not None

        self._embedding_dim = embedding_dim
        self._padding_idx = padding_idx
        self._init_model(**kwargs)

    def _init_model(self, **kwargs):
        raise NotImplementedError

    def forward(self, *args):
        raise NotImplementedError


class PositionalEmbedding(BaseEmbedding):
    def _get_positions(self, x):
        _, seq_len = x.size()

        content_mask = x.ne(self._padding_idx).long()
        positions = content_mask * torch.arange(seq_len).unsqueeze(0)

        return positions


class ConstantPositionalEmbedding(PositionalEmbedding):
    def _init_model(self, **kwargs):
        self.register_buffer('_position_embedding', None)

    @classmethod
    def get_embedding(cls, seq_len, embedding_dim):

        half_dim = embedding_dim // 2

        emb = math.log(10000) / (half_dim - 1)
        emb = torch.exp(torch.arange(half_dim, dtype=torch.float32) * -emb)
        emb = torch.arange(seq_len, dtype=torch.float32).unsqueeze(1) * emb.unsqueeze(0)
        emb = torch.cat([torch.sin(emb), torch.cos(emb)], dim=1).view(seq_len, -1)

        if embedding_dim % 2:
            emb = torch.cat([emb, torch.zeros(seq_len, 1)], dim=1)

        return emb

    def forward(self, x):
        batch_size, seq_len = x.size()

        if self._position_embedding is None or seq_len > self._position_embedding.size(0):
            self._position_embedding = ConstantPositionalEmbedding.get_embedding(seq_len, self._embedding_dim)

        positions = self._get_positions(x)

        return self._position_embedding.index_select(0, positions.view(-1)).view(batch_size, seq_len, -1).to(x.device)


class LearnablePositionalEmbedding(PositionalEmbedding):
    def _init_model(self, *, n_positions=512, **kwargs):
        assert n_positions is not None
        self._position_embedding = nn.Embedding(n_positions,
                                                self._embedding_dim,
                                                self._padding_idx)

    def forward(self, x):
        positions = self._get_positions(x)

        return self._position_embedding(positions)


class DefaultEmbedding(BaseEmbedding):
    def _init_model(self, *, vocab_size=None, **kwargs):
        assert vocab_size is not None
        self._embedding = nn.Embedding(vocab_size,
                                       self._embedding_dim,
                                       self._padding_idx)

    def forward(self, x):
        return self._embedding(x) * math.sqrt(self._embedding.embedding_dim)


class EmbeddingList(nn.Module):
    def __init__(self, *modules, **kwargs):
        super(EmbeddingList, self).__init__()

        self._embeddings = nn.ModuleList([eval(module)(**kwargs) for module in modules])

    def forward(self, x):
        out = 0
        for embedding in self._embeddings:
            out += embedding(x)

        return out


if __name__ == '__main__':
    modules = ['DefaultEmbedding', 'ConstantPositionalEmbedding', 'LearnablePositionalEmbedding']
    parameters = dict(vocab_size=512, embedding_dim=768, padding_idx=0, n_positions=512)

    embedding = EmbeddingList(*modules, **parameters)

    x = torch.randint(0, 128, (8, 256))

    assert embedding(x).size() == (8, 256, 768)
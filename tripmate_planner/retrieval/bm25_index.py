"""Per-(city, kind) BM25 indexes over the POI corpus; scores for all docs so pre-filtering is a mask."""

from functools import lru_cache

import bm25s
import numpy as np

from tripmate_planner.retrieval.corpus import KINDS, PoiDoc, build_corpus
from tripmate_planner.retrieval.tokenize import tokenize


class BM25Index:
    def __init__(self, docs: tuple[PoiDoc, ...], k1: float = 1.5, b: float = 0.75) -> None:
        self.groups: dict[tuple[str, str], list[int]] = {}
        for i, d in enumerate(docs):
            self.groups.setdefault((d.city, d.kind), []).append(i)
        self.models: dict[tuple[str, str], bm25s.BM25] = {}
        self.vocab: dict[tuple[str, str], dict[str, int]] = {}
        for key, idx in self.groups.items():
            corpus_tokens = [tokenize(docs[i].text) for i in idx]
            vocab: dict[str, int] = {}
            ids = [[vocab.setdefault(t, len(vocab)) for t in toks] for toks in corpus_tokens]
            model = bm25s.BM25(k1=k1, b=b)
            model.index(bm25s.tokenization.Tokenized(ids=ids, vocab=vocab), show_progress=False)
            self.models[key], self.vocab[key] = model, vocab

    def scores(self, city: str, kind: str, query: str) -> tuple[list[int], np.ndarray]:
        """Return (global doc indexes, scores) for every doc of (city, kind)."""
        key = (city, kind)
        vocab = self.vocab[key]
        q = [vocab[t] for t in tokenize(query) if t in vocab]
        if not q:
            return self.groups[key], np.zeros(len(self.groups[key]), dtype=np.float32)
        return self.groups[key], np.asarray(self.models[key].get_scores(q), dtype=np.float32)


@lru_cache(maxsize=2)
def get_bm25(k1: float = 1.5, b: float = 0.75) -> BM25Index:
    _ = KINDS
    return BM25Index(build_corpus(), k1=k1, b=b)

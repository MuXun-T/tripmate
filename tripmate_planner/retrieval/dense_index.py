"""Exact dense retrieval: bge-small-zh-v1.5 embeddings in a float32 matrix (no ANN index)."""

import json
from functools import lru_cache

import numpy as np
import numpy.typing as npt

from tripmate_planner.paths import CACHE, CONFIGS
from tripmate_planner.retrieval.corpus import build_corpus, corpus_hash


def _model_path_and_rev() -> tuple[str, str]:
    v = json.loads((CONFIGS / "versions.json").read_text(encoding="utf-8"))
    return v["model_paths"]["dense_model"], v["dense_model"]["revision"]


class DenseIndex:
    def __init__(self, query_instruction: str) -> None:
        from sentence_transformers import SentenceTransformer

        path, rev = _model_path_and_rev()
        self.model = SentenceTransformer(path, device="cpu")
        self.instruction = query_instruction
        docs = build_corpus()
        cache_file = CACHE / "index" / "dense" / f"{corpus_hash(docs)[:16]}_{rev[:8]}.npy"
        if cache_file.exists():
            self.matrix = np.load(cache_file)
        else:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.matrix = self.model.encode([d.text for d in docs], batch_size=64, normalize_embeddings=True,
                                            convert_to_numpy=True, show_progress_bar=False).astype(np.float32)
            np.save(cache_file, self.matrix)
            (cache_file.with_suffix(".ids.json")).write_text(json.dumps([d.doc_id for d in docs]), encoding="utf-8")

    def encode_query(self, text: str) -> npt.NDArray[np.float32]:
        vec = self.model.encode([self.instruction + text], normalize_embeddings=True, convert_to_numpy=True,
                                show_progress_bar=False)
        out: npt.NDArray[np.float32] = vec[0].astype(np.float32)
        return out

    def scores(self, idx: list[int], query: str) -> npt.NDArray[np.float32]:
        out: npt.NDArray[np.float32] = self.matrix[idx] @ self.encode_query(query)
        return out


@lru_cache(maxsize=1)
def get_dense(query_instruction: str) -> DenseIndex:
    return DenseIndex(query_instruction)

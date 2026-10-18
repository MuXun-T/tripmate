"""bge-reranker-base cross-encoder reranking on CPU."""

import json
from functools import lru_cache

from tripmate_planner.paths import CONFIGS


class Reranker:
    def __init__(self, max_length: int = 128, batch_size: int = 32) -> None:
        from sentence_transformers import CrossEncoder

        path = json.loads((CONFIGS / "versions.json").read_text(encoding="utf-8"))["model_paths"]["rerank_model"]
        self.model = CrossEncoder(path, max_length=max_length, device="cpu")
        self.batch_size = batch_size

    def scores(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        out = self.model.predict([(query, t) for t in texts], batch_size=self.batch_size, show_progress_bar=False)
        return [float(x) for x in out]


@lru_cache(maxsize=1)
def get_reranker(max_length: int = 128, batch_size: int = 32) -> Reranker:
    return Reranker(max_length, batch_size)

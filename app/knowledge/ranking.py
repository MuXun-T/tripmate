"""Deterministic hybrid ranking shared by online retrieval and offline evaluation."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from app.knowledge.embedding import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    cosine_similarity,
    embed_text,
)


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate: Mapping[str, Any]
    candidate_id: str
    score: float
    semantic_score: float
    lexical_score: float


def _lexical_overlap(query: str, text: str) -> float:
    def terms(value: str) -> set[str]:
        result: set[str] = set()
        for token in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", value.casefold()):
            result.add(token)
        return result

    query_terms = terms(query)
    if not query_terms:
        return 0.0
    return len(query_terms & terms(text)) / len(query_terms)


def rank_candidates(
    query: str,
    candidates: Sequence[Mapping[str, Any]],
    *,
    id_key: str = "candidate_id",
    text_key: str = "text",
    embedding_key: str = "embedding",
    lexical_key: str = "lexical_score",
) -> list[RankedCandidate]:
    normalized = " ".join(query.split())
    query_vector = embed_text(normalized)
    usable_embeddings = any(
        row.get(embedding_key) is not None
        and row.get("embedding_model", EMBEDDING_MODEL) == EMBEDDING_MODEL
        and int(row.get("vector_dimensions", EMBEDDING_DIMENSIONS)) == EMBEDDING_DIMENSIONS
        for row in candidates
    )
    ranked: list[RankedCandidate] = []
    for index, row in enumerate(candidates):
        vector = row.get(embedding_key)
        valid_embedding = (
            usable_embeddings
            and vector is not None
            and row.get("embedding_model", EMBEDDING_MODEL) == EMBEDDING_MODEL
            and int(row.get("vector_dimensions", EMBEDDING_DIMENSIONS)) == EMBEDDING_DIMENSIONS
        )
        semantic = (
            cosine_similarity(query_vector, cast(Sequence[float], vector))
            if valid_embedding
            else 0.0
        )
        raw_lexical = row.get(lexical_key)
        lexical = (
            _lexical_overlap(normalized, str(row.get(text_key, "")))
            if raw_lexical is None
            else min(max(float(raw_lexical), 0.0), 1.0)
        )
        score = 0.8 * semantic + 0.2 * lexical if valid_embedding else lexical
        ranked.append(
            RankedCandidate(
                candidate=row,
                candidate_id=str(row.get(id_key, index)),
                score=round(score, 8),
                semantic_score=round(semantic, 8),
                lexical_score=round(lexical, 8),
            )
        )
    ranked.sort(key=lambda item: (-item.score, -item.semantic_score, -item.lexical_score, item.candidate_id))
    return ranked

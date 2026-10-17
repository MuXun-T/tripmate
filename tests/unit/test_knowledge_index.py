from pathlib import Path

import pytest

from app.knowledge.embedding import EMBEDDING_DIMENSIONS, cosine_similarity, embed_text
from app.knowledge.index import (
    DEFAULT_SOURCE,
    EvaluationDataset,
    RetrievalCase,
    SourceBundle,
    evaluate_bundle,
    evaluate_source,
    load_evaluation_dataset,
)
from app.knowledge.redis_index import _pack_vector, _parse_search_results

EXTERNAL_DATASET = Path(__file__).parents[2] / "knowledge" / "evaluation" / "travelplanner-validation.json"


def test_local_source_passes_fixed_offline_retrieval_cases() -> None:
    source = SourceBundle.model_validate_json(DEFAULT_SOURCE.read_bytes())

    metrics = evaluate_source(source)

    assert len(source.documents) == 4
    assert metrics["case_count"] == 6
    assert metrics["recall_at_5"] == metrics["mrr"] == 1.0
    assert metrics["citation_correctness"] == metrics["citation_locatability"] == 1.0
    assert metrics["unknown_precision"] == metrics["unknown_recall"] == 1.0
    assert all(item.source_url.startswith("repo://") for item in source.documents)


def test_external_real_dataset_is_pinned_and_evaluated_offline() -> None:
    dataset = load_evaluation_dataset(EXTERNAL_DATASET)

    assert isinstance(dataset, EvaluationDataset)
    assert dataset.dataset_id == "osunlp/TravelPlanner"
    assert dataset.license == "CC-BY-4.0"
    assert dataset.dataset_revision == "8736504ecfc31b7f8b7e40122873c337e83fff7c"
    assert len(dataset.documents) == 36
    assert len(dataset.cases) == 12
    metrics = evaluate_bundle(dataset.bundle(), dataset.cases)
    assert metrics["recall_at_5"] >= 0.9
    assert metrics["mrr"] >= 0.85
    assert metrics["citation_correctness"] >= 0.8
    assert metrics["citation_locatability"] == 1.0
    assert metrics["unknown_case_count"] == 0
    assert metrics["retrieval_p50_ms"] > 0
    assert metrics["retrieval_p95_ms"] >= metrics["retrieval_p50_ms"]


def test_retrieval_case_rejects_mismatched_answerability_labels() -> None:
    with pytest.raises(ValueError, match="relevant chunks"):
        RetrievalCase(case_id="bad", query="q", answerable=True)
    with pytest.raises(ValueError, match="cannot have relevant chunks"):
        RetrievalCase(
            case_id="bad-unknown",
            query="q",
            answerable=False,
            relevant_chunk_ids=["chunk-1"],
        )


def test_hash_ngram_embeddings_are_stable_normalized_and_synonym_aware() -> None:
    rainy_query = embed_text("雨天怎么调整活动")
    indoor_policy = embed_text("降雨天气变化时可以优先安排室内活动")
    reservation_policy = embed_text("已确认预约按价格快照保留")

    assert len(rainy_query) == EMBEDDING_DIMENSIONS
    assert rainy_query == embed_text("雨天怎么调整活动")
    assert abs(sum(value * value for value in rainy_query) - 1.0) < 1e-8
    assert cosine_similarity(rainy_query, indoor_policy) > cosine_similarity(
        rainy_query, reservation_policy
    )


def test_empty_query_embeds_to_zero_without_invalid_scores() -> None:
    empty = embed_text(" \n ")

    assert not any(empty)
    assert cosine_similarity(empty, embed_text("行程计划")) == 0.0


def test_redis_vector_results_preserve_chunk_identity_and_cosine_score() -> None:
    raw = [
        1,
        "doc-key",
        [
            "chunk_id",
            "chunk-1",
            "document_id",
            "document-1",
            "vector_score",
            "0.25",
            "content_sha256",
            "a" * 64,
        ],
    ]

    parsed = _parse_search_results(raw)

    assert parsed[0]["chunk_id"] == "chunk-1"
    assert parsed[0]["document_id"] == "document-1"
    assert parsed[0]["score"] == 0.75
    assert len(_pack_vector([0.0] * EMBEDDING_DIMENSIONS)) == EMBEDDING_DIMENSIONS * 4

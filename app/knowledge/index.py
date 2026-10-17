import argparse
import asyncio
import hashlib
import json
from collections.abc import Iterable, Sequence
from math import log2
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.knowledge.embedding import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    embed_text,
)
from app.knowledge.ranking import rank_candidates
from app.knowledge.redis_index import RedisKnowledgeIndex
from app.persistence.postgres.database import create_engine, session_factory
from redis.asyncio import Redis

DEFAULT_SOURCE = Path(__file__).resolve().parents[2] / "knowledge" / "sources" / "tripmate-core.json"
EVALUATION_CASES = (
    ("local-weather", "下雨天气变化时优先安排什么活动", "tripmate-weather-and-indoor-planning#chunk-0"),
    ("local-authority", "门票价格和余票库存依据什么判断", "tripmate-authoritative-trip-data#chunk-0"),
    ("local-reservation", "修改行程后已确认预约的原票价会变吗", "tripmate-confirmed-reservation-snapshots#chunk-0"),
    ("local-preference", "饮食过敏偏好什么时候可以保存", "tripmate-preference-consent#chunk-0"),
)
UNKNOWN_EVALUATION_CASES = (
    ("local-unknown-cancellation", "What is the cancellation policy for tickets?"),
    ("local-unknown-weather", "What is the weather forecast on Mars?"),
)

UNKNOWN_SCORE_THRESHOLD = 0.05


class SourceChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    heading: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=20000)
    keywords: list[str] = Field(default_factory=list, max_length=100)


class SourceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    external_document_id: str = Field(min_length=1, max_length=200)
    document_type: str = Field(min_length=1, max_length=80)
    source: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=200)
    source_url: str = Field(min_length=1, max_length=1000)
    title: str = Field(min_length=1, max_length=500)
    chunks: list[SourceChunk] = Field(min_length=1, max_length=1000)


class SourceBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_version: str = Field(min_length=1, max_length=100)
    documents: list[SourceDocument] = Field(min_length=1, max_length=1000)


class RetrievalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=2000)
    relevant_chunk_ids: list[str] = Field(default_factory=list, max_length=100)
    required_citation_ids: list[str] = Field(default_factory=list, max_length=100)
    answerable: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_answerability(self) -> "RetrievalCase":
        if self.answerable and not self.relevant_chunk_ids:
            raise ValueError("answerable retrieval cases require relevant chunks")
        if not self.answerable and self.relevant_chunk_ids:
            raise ValueError("unknown retrieval cases cannot have relevant chunks")
        if not set(self.required_citation_ids) <= set(self.relevant_chunk_ids):
            raise ValueError("required citations must be relevant chunks")
        return self


class EvaluationDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str = Field(min_length=1, max_length=200)
    dataset_url: str = Field(min_length=1, max_length=1000)
    dataset_revision: str = Field(min_length=1, max_length=100)
    license: str = Field(min_length=1, max_length=100)
    split: str = Field(min_length=1, max_length=100)
    source_files: dict[str, str] = Field(min_length=1, max_length=20)
    source_version: str = Field(min_length=1, max_length=100)
    selection: dict[str, Any] = Field(default_factory=dict)
    documents: list[SourceDocument] = Field(min_length=1, max_length=5000)
    cases: list[RetrievalCase] = Field(min_length=1, max_length=5000)

    def bundle(self) -> SourceBundle:
        return SourceBundle(source_version=self.source_version, documents=self.documents)


def _chunk_text(document: SourceDocument, chunk: SourceChunk) -> str:
    return "\n".join((document.title, chunk.heading, chunk.content, " ".join(chunk.keywords)))


def _default_evaluation_cases() -> tuple[RetrievalCase, ...]:
    answerable = tuple(
        RetrievalCase(
            case_id=case_id,
            query=query,
            relevant_chunk_ids=[chunk_id],
            required_citation_ids=[chunk_id],
        )
        for case_id, query, chunk_id in EVALUATION_CASES
    )
    unknown = tuple(
        RetrievalCase(case_id=case_id, query=query, answerable=False)
        for case_id, query in UNKNOWN_EVALUATION_CASES
    )
    return answerable + unknown


def _candidate_id(document: SourceDocument, ordinal: int) -> str:
    return f"{document.external_document_id}#chunk-{ordinal}"


def _candidates(bundle: SourceBundle) -> list[dict[str, Any]]:
    return [
        {
            "candidate_id": _candidate_id(document, ordinal),
            "document_id": document.external_document_id,
            "text": _chunk_text(document, chunk),
            "content": chunk.content,
            "content_sha256": hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
            "embedding": embed_text(_chunk_text(document, chunk)),
            "embedding_model": EMBEDDING_MODEL,
            "vector_dimensions": EMBEDDING_DIMENSIONS,
        }
        for document in bundle.documents
        for ordinal, chunk in enumerate(document.chunks)
    ]


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile)))
    return round(ordered[index], 3)


def _ndcg_at_k(ranked_ids: Sequence[str], relevant: set[str], k: int) -> float:
    gains = [1.0 if item in relevant else 0.0 for item in ranked_ids[:k]]
    dcg = sum(gain / log2(index + 2) for index, gain in enumerate(gains))
    ideal = sum(1.0 / log2(index + 2) for index in range(min(k, len(relevant))))
    return dcg / ideal if ideal else 0.0


def evaluate_bundle(
    bundle: SourceBundle,
    cases: Iterable[RetrievalCase],
    *,
    top_k: int = 5,
    unknown_score_threshold: float = UNKNOWN_SCORE_THRESHOLD,
) -> dict[str, Any]:
    if top_k < 1:
        raise ValueError("top_k must be positive")
    case_list = tuple(cases)
    candidates = _candidates(bundle)
    candidate_map = {row["candidate_id"]: row for row in candidates}
    for case in case_list:
        if case.answerable and not set(case.relevant_chunk_ids) <= candidate_map.keys():
            raise ValueError(f"Case {case.case_id} references missing relevant chunks")
        if not set(case.required_citation_ids) <= candidate_map.keys():
            raise ValueError(f"Case {case.case_id} references missing required citations")
    reciprocal_ranks: list[float] = []
    recalls_at_3: list[float] = []
    recalls_at_5: list[float] = []
    ndcgs_at_k: list[float] = []
    citation_locatable: list[float] = []
    citation_correct: list[float] = []
    unknown_predictions: list[bool] = []
    unknown_truth: list[bool] = []
    durations: list[float] = []
    for case in case_list:
        started = perf_counter()
        ranked = rank_candidates(case.query, candidates)
        durations.append((perf_counter() - started) * 1000)
        ranked_ids = [item.candidate_id for item in ranked]
        relevant = set(case.relevant_chunk_ids)
        first_rank = next(
            (index for index, candidate_id in enumerate(ranked_ids, start=1) if candidate_id in relevant),
            None,
        )
        reciprocal_ranks.append(1.0 / first_rank if first_rank is not None else 0.0)
        recalls_at_3.append(
            len(set(ranked_ids[:3]) & relevant) / len(relevant) if relevant else 0.0
        )
        recalls_at_5.append(
            len(set(ranked_ids[:top_k]) & relevant) / len(relevant) if relevant else 0.0
        )
        ndcgs_at_k.append(_ndcg_at_k(ranked_ids, relevant, top_k))
        emitted = ranked[:top_k] if ranked and ranked[0].score >= unknown_score_threshold else []
        unknown_prediction = not emitted
        unknown_predictions.append(unknown_prediction)
        unknown_truth.append(not case.answerable)
        if emitted:
            locatable = [
                item.candidate_id in candidate_map
                and candidate_map[item.candidate_id]["content_sha256"]
                == hashlib.sha256(str(item.candidate["content"]).encode("utf-8")).hexdigest()
                for item in emitted
            ]
            citation_locatable.append(sum(locatable) / len(locatable))
            emitted_ids = {item.candidate_id for item in emitted}
            required = set(case.required_citation_ids)
            required_score = (
                len(required & emitted_ids) / len(required) if required else 1.0
            )
            citation_correct.append(
                required_score
                if case.answerable and emitted[0].candidate_id in relevant
                else 0.0
            )
        else:
            citation_locatable.append(1.0)
            citation_correct.append(1.0 if not case.answerable else 0.0)

    predicted_unknown_count = sum(unknown_predictions)
    true_unknown_count = sum(
        prediction and truth for prediction, truth in zip(unknown_predictions, unknown_truth, strict=True)
    )
    unknown_count = sum(unknown_truth)
    answerable_count = len(case_list) - unknown_count
    unknown_precision: float | str = (
        round(true_unknown_count / predicted_unknown_count, 4)
        if predicted_unknown_count
        else ("N/A" if not unknown_count else 0.0)
    )
    unknown_recall: float | str = (
        round(true_unknown_count / unknown_count, 4) if unknown_count else "N/A"
    )
    metrics: dict[str, Any] = {
        "case_count": len(case_list),
        "answerable_case_count": answerable_count,
        "unknown_case_count": unknown_count,
        "recall_at_3": round(sum(recalls_at_3) / answerable_count, 4) if answerable_count else 0.0,
        "mrr": round(sum(reciprocal_ranks) / answerable_count, 4) if answerable_count else 0.0,
        f"recall_at_{top_k}": round(sum(recalls_at_5) / answerable_count, 4) if answerable_count else 0.0,
        f"ndcg_at_{top_k}": round(sum(ndcgs_at_k) / answerable_count, 4) if answerable_count else 0.0,
        "citation_locatability": round(sum(citation_locatable) / len(citation_locatable), 4),
        "citation_locatability_scope": "offline_memory_bundle",
        "citation_correctness": round(sum(citation_correct) / len(citation_correct), 4),
        "unknown_precision": unknown_precision,
        "unknown_recall": unknown_recall,
        "unknown_score_threshold": unknown_score_threshold,
        "retrieval_p50_ms": _percentile(durations, 0.50),
        "retrieval_p95_ms": _percentile(durations, 0.95),
        "retrieval_latency_scope": "offline_memory_ranking",
    }
    if not unknown_count:
        metrics["unknown_metrics_status"] = "N/A: evaluation set contains no unknown cases"
    return metrics


def evaluate_source(
    bundle: SourceBundle, cases: Iterable[RetrievalCase] | None = None
) -> dict[str, Any]:
    return evaluate_bundle(bundle, cases or _default_evaluation_cases())


def _persisted_citation_locatability(
    bundle: SourceBundle,
    cases: Sequence[RetrievalCase],
    persisted_hashes: dict[str, str],
    *,
    top_k: int = 5,
) -> float:
    """Check citation IDs against hashes read back from the persisted manifest."""
    candidates = _candidates(bundle)
    values: list[float] = []
    for case in cases:
        ranked = rank_candidates(case.query, candidates)
        emitted = ranked[:top_k] if ranked and ranked[0].score >= UNKNOWN_SCORE_THRESHOLD else []
        if not emitted:
            values.append(1.0 if not case.answerable else 0.0)
            continue
        locatable = [
            persisted_hashes.get(item.candidate_id) == item.candidate["content_sha256"]
            for item in emitted
        ]
        values.append(sum(locatable) / len(locatable))
    return round(sum(values) / len(values), 4) if values else 0.0


def load_evaluation_dataset(path: Path) -> EvaluationDataset:
    return EvaluationDataset.model_validate_json(path.read_bytes())


class KnowledgeIndex:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        source_path: Path = DEFAULT_SOURCE,
        redis: Redis | None = None,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.source_path = source_path
        self.vector_index = RedisKnowledgeIndex(redis, settings) if redis is not None else None

    def _load_source(self) -> tuple[SourceBundle, str, dict[str, Any]]:
        content = self.source_path.read_bytes()
        bundle = SourceBundle.model_validate_json(content)
        source_hash = hashlib.sha256(content).hexdigest()
        metrics = evaluate_source(bundle)
        if (
            metrics["recall_at_5"] < 1.0
            or metrics["mrr"] < 0.5
            or metrics["citation_correctness"] < 1.0
            or metrics["citation_locatability"] < 1.0
        ):
            raise ValueError(f"Knowledge source failed fixed retrieval evaluation: {metrics}")
        metrics.update(
            {
                "source_version": bundle.source_version,
                "source_sha256": source_hash,
                "embedding_model": EMBEDDING_MODEL,
            }
        )
        return bundle, f"local-{source_hash[:16]}", metrics

    async def build(self, tenant_id: UUID) -> dict[str, Any]:
        try:
            return await self._build(tenant_id)
        except Exception as exc:
            await self._mark_failed(tenant_id, exc)
            raise

    async def _build(self, tenant_id: UUID) -> dict[str, Any]:
        bundle, version, metrics = self._load_source()
        metrics = {
            **metrics,
            "index_version": version,
            "active_index_version": version,
        }
        manifest_id = uuid5(NAMESPACE_URL, f"tripmate:{tenant_id}:manifest:{version}")
        async with self.sessions() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 74192026))"),
                {"key": f"knowledge:{tenant_id}:{self.settings.rag_index_alias}"},
            )
            existing = await session.execute(
                text("""SELECT manifest_id,status FROM tripmate.knowledge_manifest
                    WHERE tenant_id=:tenant AND index_version=:version"""),
                {"tenant": tenant_id, "version": version},
            )
            prior = existing.mappings().one_or_none()
            if prior is None:
                await session.execute(
                    text("""INSERT INTO tripmate.knowledge_manifest
                        (manifest_id,tenant_id,index_version,index_alias,embedding_model,
                         vector_dimensions,status,document_count,chunk_count)
                        VALUES(:manifest,:tenant,:version,:alias,:model,:dimensions,'BUILDING',:documents,:chunks)"""),
                    {
                        "manifest": manifest_id,
                        "tenant": tenant_id,
                        "version": version,
                        "alias": self.settings.rag_index_alias,
                        "model": EMBEDDING_MODEL,
                        "dimensions": EMBEDDING_DIMENSIONS,
                        "documents": len(bundle.documents),
                        "chunks": sum(len(item.chunks) for item in bundle.documents),
                    },
                )
                for document in bundle.documents:
                    content_hash = hashlib.sha256(
                        document.model_dump_json().encode("utf-8")
                    ).hexdigest()
                    old = await session.execute(
                        text("""SELECT document_id,content_sha256,status
                            FROM tripmate.knowledge_document WHERE tenant_id=:tenant
                            AND external_document_id=:external AND source_version=:source_version"""),
                        {
                            "tenant": tenant_id,
                            "external": document.external_document_id,
                            "source_version": bundle.source_version,
                        },
                    )
                    old_doc = old.mappings().one_or_none()
                    if old_doc is not None and (
                        old_doc["content_sha256"].strip() != content_hash
                        or old_doc["status"] == "DELETED"
                    ):
                        raise ValueError(
                            "A source version changed or was deleted; publish a new source_version"
                        )
                    document_id = (
                        old_doc["document_id"]
                        if old_doc is not None
                        else uuid5(
                            NAMESPACE_URL,
                            f"tripmate:{tenant_id}:document:{document.external_document_id}:{bundle.source_version}",
                        )
                    )
                    if old_doc is None:
                        await session.execute(
                            text("""INSERT INTO tripmate.knowledge_document
                                (document_id,tenant_id,external_document_id,document_type,source,source_id,
                                 source_url,source_version,title,content_sha256,retrieved_at,status)
                                VALUES(:id,:tenant,:external,:type,:source,:source_id,:url,:source_version,
                                 :title,:hash,now(),'ACTIVE')"""),
                            {
                                "id": document_id,
                                "tenant": tenant_id,
                                "external": document.external_document_id,
                                "type": document.document_type,
                                "source": document.source,
                                "source_id": document.source_id,
                                "url": document.source_url,
                                "source_version": bundle.source_version,
                                "title": document.title,
                                "hash": content_hash,
                            },
                        )
                    for ordinal, chunk in enumerate(document.chunks):
                        chunk_id = uuid5(
                            NAMESPACE_URL,
                            f"tripmate:{tenant_id}:chunk:{version}:{document.external_document_id}:{ordinal}",
                        )
                        vector = embed_text(_chunk_text(document, chunk))
                        await session.execute(
                            text("""INSERT INTO tripmate.knowledge_chunk
                                (chunk_id,tenant_id,document_id,manifest_id,ordinal,heading,content,
                                 content_sha256,metadata,embedding)
                                VALUES(:id,:tenant,:document,:manifest,:ordinal,:heading,:content,:hash,
                                 CAST(:metadata AS jsonb),CAST(:embedding AS double precision[]))"""),
                            {
                                "id": chunk_id,
                                "tenant": tenant_id,
                                "document": document_id,
                                "manifest": manifest_id,
                                "ordinal": ordinal,
                                "heading": chunk.heading,
                                "content": chunk.content,
                                "hash": hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
                                "metadata": json.dumps({"keywords": " ".join(chunk.keywords)}, ensure_ascii=False),
                                "embedding": vector,
                            },
                        )
                await session.execute(
                    text("""UPDATE tripmate.knowledge_manifest SET status='VALIDATED',validated_at=now()
                        WHERE tenant_id=:tenant AND manifest_id=:manifest"""),
                    {"tenant": tenant_id, "manifest": manifest_id},
                )
                evaluation_id = uuid5(
                    NAMESPACE_URL, f"tripmate:{tenant_id}:evaluation:{version}"
                )
                await session.execute(
                    text("""INSERT INTO tripmate.knowledge_evaluation_run
                        (evaluation_id,tenant_id,index_version,embedding_model,case_count,metrics)
                        VALUES(:id,:tenant,:version,:model,:count,CAST(:metrics AS jsonb))"""),
                    {
                        "id": evaluation_id,
                        "tenant": tenant_id,
                        "version": version,
                        "model": EMBEDDING_MODEL,
                        "count": len(EVALUATION_CASES),
                        "metrics": json.dumps(metrics),
                    },
                )
            else:
                manifest_id = prior["manifest_id"]
                if prior["status"] == "FAILED":
                    raise ValueError("A failed knowledge manifest cannot be activated")
                deleted = await session.scalar(
                    text("""SELECT EXISTS(SELECT 1 FROM tripmate.knowledge_chunk c
                        JOIN tripmate.knowledge_document d ON d.tenant_id=c.tenant_id
                          AND d.document_id=c.document_id WHERE c.tenant_id=:tenant
                          AND c.manifest_id=:manifest AND d.status='DELETED')"""),
                    {"tenant": tenant_id, "manifest": manifest_id},
                )
                if deleted:
                    raise ValueError("Cannot activate an index containing deleted sources")
            if self.vector_index is not None:
                vector_result = await session.execute(
                    text("""SELECT c.chunk_id,c.document_id,c.content,c.content_sha256,c.embedding,
                            c.heading,d.title,d.source,d.source_version,d.source_url,d.retrieved_at,
                            m.index_version
                        FROM tripmate.knowledge_chunk c
                        JOIN tripmate.knowledge_document d
                          ON d.tenant_id=c.tenant_id AND d.document_id=c.document_id
                        JOIN tripmate.knowledge_manifest m
                          ON m.tenant_id=c.tenant_id AND m.manifest_id=c.manifest_id
                        WHERE c.tenant_id=:tenant AND c.manifest_id=:manifest
                          AND d.status<>'DELETED' ORDER BY c.chunk_id"""),
                    {"tenant": tenant_id, "manifest": manifest_id},
                )
                await self.vector_index.replace_manifest(
                    tenant_id, manifest_id, [dict(row) for row in vector_result.mappings()]
                )
            generation = await self._activate(session, tenant_id, manifest_id)
        return {
            "index_version": version,
            "manifest_id": str(manifest_id),
            "generation": generation,
            "metrics": metrics,
        }

    async def _mark_failed(self, tenant_id: UUID, exc: Exception) -> None:
        try:
            content = self.source_path.read_bytes()
            bundle = SourceBundle.model_validate_json(content)
            version = f"local-{hashlib.sha256(content).hexdigest()[:16]}"
            document_count = len(bundle.documents)
            chunk_count = sum(len(item.chunks) for item in bundle.documents)
        except Exception:
            content = self.source_path.as_posix().encode("utf-8")
            version = f"failed-{hashlib.sha256(content).hexdigest()[:16]}"
            document_count = chunk_count = 0
        manifest_id = uuid5(NAMESPACE_URL, f"tripmate:{tenant_id}:manifest:{version}")
        async with self.sessions() as session, session.begin():
            await session.execute(
                text("""INSERT INTO tripmate.knowledge_manifest
                    (manifest_id,tenant_id,index_version,index_alias,embedding_model,vector_dimensions,
                     status,document_count,chunk_count,failure_reason)
                    VALUES(:manifest,:tenant,:version,:alias,:model,:dimensions,'FAILED',:documents,:chunks,:reason)
                    ON CONFLICT (tenant_id,index_version) DO UPDATE SET
                      status=CASE WHEN tripmate.knowledge_manifest.status='ACTIVE'
                                  THEN tripmate.knowledge_manifest.status ELSE 'FAILED' END,
                      failure_reason=EXCLUDED.failure_reason"""),
                {"manifest": manifest_id, "tenant": tenant_id, "version": version,
                 "alias": self.settings.rag_index_alias, "model": EMBEDDING_MODEL,
                 "dimensions": EMBEDDING_DIMENSIONS, "documents": document_count,
                 "chunks": chunk_count, "reason": str(exc)[:1000]},
            )

    async def rollback(self, tenant_id: UUID, index_version: str) -> dict[str, Any]:
        async with self.sessions() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 74192026))"),
                {"key": f"knowledge:{tenant_id}:{self.settings.rag_index_alias}"},
            )
            result = await session.execute(
                text("""SELECT manifest_id,status FROM tripmate.knowledge_manifest
                    WHERE tenant_id=:tenant AND index_version=:version FOR UPDATE"""),
                {"tenant": tenant_id, "version": index_version},
            )
            target = result.mappings().one_or_none()
            if target is None or target["status"] in {"BUILDING", "FAILED"}:
                raise ValueError("Knowledge index version is unavailable")
            deleted = await session.scalar(
                text("""SELECT EXISTS(SELECT 1 FROM tripmate.knowledge_chunk c
                    JOIN tripmate.knowledge_document d ON d.tenant_id=c.tenant_id
                      AND d.document_id=c.document_id WHERE c.tenant_id=:tenant
                      AND c.manifest_id=:manifest AND d.status='DELETED')"""),
                {"tenant": tenant_id, "manifest": target["manifest_id"]},
            )
            if deleted:
                raise ValueError("Cannot roll back to an index containing deleted sources")
            generation = await self._activate(session, tenant_id, target["manifest_id"])
        if self.vector_index is not None:
            async with self.sessions() as session:
                vector_result = await session.execute(
                    text("""SELECT c.chunk_id,c.document_id,c.content,c.content_sha256,c.embedding,
                            c.heading,d.title,d.source,d.source_version,d.source_url,d.retrieved_at,
                            m.index_version
                        FROM tripmate.knowledge_chunk c
                        JOIN tripmate.knowledge_document d
                          ON d.tenant_id=c.tenant_id AND d.document_id=c.document_id
                        JOIN tripmate.knowledge_manifest m
                          ON m.tenant_id=c.tenant_id AND m.manifest_id=c.manifest_id
                        WHERE c.tenant_id=:tenant AND c.manifest_id=:manifest
                          AND d.status<>'DELETED' ORDER BY c.chunk_id"""),
                    {"tenant": tenant_id, "manifest": target["manifest_id"]},
                )
                rows = [dict(row) for row in vector_result.mappings()]
            await self.vector_index.replace_manifest(tenant_id, target["manifest_id"], rows)
        return {"index_version": index_version, "generation": generation}

    async def evaluate_manifest(
        self,
        tenant_id: UUID,
        index_version: str,
        cases: Iterable[RetrievalCase] | None = None,
    ) -> dict[str, Any]:
        """Replay the same fixed cases against a candidate or retired manifest."""
        case_list = tuple(cases) if cases is not None else _default_evaluation_cases()
        async with self.sessions() as session:
            result = await session.execute(
                text("""SELECT m.manifest_id,m.index_version,m.embedding_model,m.vector_dimensions,
                    d.external_document_id,d.document_type,d.source,d.source_id,d.source_url,
                    d.source_version,d.title,c.ordinal,c.heading,c.content,c.metadata
                    FROM tripmate.knowledge_manifest m
                    JOIN tripmate.knowledge_chunk c ON c.tenant_id=m.tenant_id
                      AND c.manifest_id=m.manifest_id
                    JOIN tripmate.knowledge_document d ON d.tenant_id=c.tenant_id
                      AND d.document_id=c.document_id
                    WHERE m.tenant_id=:tenant AND m.index_version=:version
                    ORDER BY d.external_document_id,c.ordinal"""),
                {"tenant": tenant_id, "version": index_version},
            )
            rows = list(result.mappings())
            persisted_result = await session.execute(
                text("""SELECT d.external_document_id,c.ordinal,c.content_sha256
                    FROM tripmate.knowledge_manifest m
                    JOIN tripmate.knowledge_chunk c ON c.tenant_id=m.tenant_id
                      AND c.manifest_id=m.manifest_id
                    JOIN tripmate.knowledge_document d ON d.tenant_id=c.tenant_id
                      AND d.document_id=c.document_id
                    WHERE m.tenant_id=:tenant AND m.index_version=:version"""),
                {"tenant": tenant_id, "version": index_version},
            )
            persisted_hashes = {
                f"{row['external_document_id']}#chunk-{row['ordinal']}": str(row["content_sha256"]).strip()
                for row in persisted_result.mappings()
            }
        if not rows:
            raise ValueError("Knowledge manifest has no chunks to evaluate")
        documents: dict[str, dict[str, Any]] = {}
        for row in rows:
            document = documents.setdefault(
                str(row["external_document_id"]),
                {
                    "external_document_id": str(row["external_document_id"]),
                    "document_type": str(row["document_type"]),
                    "source": str(row["source"]),
                    "source_id": str(row["source_id"]),
                    "source_url": str(row["source_url"]),
                    "source_version": str(row["source_version"]),
                    "title": str(row["title"]),
                    "chunks": [],
                },
            )
            metadata = row["metadata"] if isinstance(row["metadata"], dict) else {}
            keywords = str(metadata.get("keywords", "")).split()
            document["chunks"].append(
                {
                    "heading": str(row["heading"] or f"chunk-{row['ordinal']}"),
                    "content": str(row["content"]),
                    "keywords": keywords,
                }
            )
        bundle = SourceBundle(
            source_version=str(rows[0]["source_version"]),
            documents=[SourceDocument.model_validate(value) for value in documents.values()],
        )
        metrics = evaluate_source(bundle, case_list)
        metrics["citation_locatability"] = _persisted_citation_locatability(
            bundle, case_list, persisted_hashes
        )
        metrics["citation_locatability_scope"] = "postgres_manifest_roundtrip"
        metrics.update(
            {
                "index_version": index_version,
                "manifest_id": str(rows[0]["manifest_id"]),
                "embedding_model": str(rows[0]["embedding_model"]),
            }
        )
        return metrics

    async def delete_document(self, tenant_id: UUID, external_document_id: str) -> int:
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                text("""UPDATE tripmate.knowledge_document SET status='DELETED',updated_at=now()
                    WHERE tenant_id=:tenant AND external_document_id=:external AND status<>'DELETED'
                    RETURNING document_id"""),
                {"tenant": tenant_id, "external": external_document_id},
            )
            document_ids = [row["document_id"] for row in result.mappings()]
        if self.vector_index is not None and document_ids:
            await self.vector_index.tombstone_documents(tenant_id, document_ids)
        return len(document_ids)

    async def _activate(
        self, session: AsyncSession, tenant_id: UUID, manifest_id: UUID
    ) -> int:
        alias_result = await session.execute(
            text("""SELECT manifest_id,generation FROM tripmate.knowledge_active_alias
                WHERE tenant_id=:tenant AND index_alias=:alias FOR UPDATE"""),
            {"tenant": tenant_id, "alias": self.settings.rag_index_alias},
        )
        alias = alias_result.mappings().one_or_none()
        if alias is not None and alias["manifest_id"] == manifest_id:
            return int(alias["generation"])
        generation = int(alias["generation"]) + 1 if alias is not None else 1
        if alias is not None:
            await session.execute(
                text("""UPDATE tripmate.knowledge_manifest SET status='RETIRED'
                    WHERE tenant_id=:tenant AND manifest_id=:manifest"""),
                {"tenant": tenant_id, "manifest": alias["manifest_id"]},
            )
        await session.execute(
            text("""UPDATE tripmate.knowledge_manifest SET status='ACTIVE'
                WHERE tenant_id=:tenant AND manifest_id=:manifest"""),
            {"tenant": tenant_id, "manifest": manifest_id},
        )
        await session.execute(
            text("""INSERT INTO tripmate.knowledge_active_alias
                (tenant_id,index_alias,manifest_id,generation)
                VALUES(:tenant,:alias,:manifest,:generation)
                ON CONFLICT (tenant_id,index_alias) DO UPDATE SET
                    manifest_id=EXCLUDED.manifest_id,generation=EXCLUDED.generation,updated_at=now()"""),
            {
                "tenant": tenant_id,
                "alias": self.settings.rag_index_alias,
                "manifest": manifest_id,
                "generation": generation,
            },
        )
        return generation


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Build or roll back the local TripMate knowledge index")
    parser.add_argument("--tenant-id", required=True, type=UUID)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--rollback")
    parser.add_argument("--delete-document")
    args = parser.parse_args()
    settings = get_settings()
    if settings.app_env not in {"development", "test"}:
        raise SystemExit("Local knowledge indexing is restricted to development/test settings")
    engine = create_engine(settings)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        index = KnowledgeIndex(
            session_factory(engine), settings, source_path=args.source, redis=redis
        )
        if args.rollback:
            result = await index.rollback(args.tenant_id, args.rollback)
        elif args.delete_document:
            result = {"deleted_documents": await index.delete_document(args.tenant_id, args.delete_document)}
        else:
            result = await index.build(args.tenant_id)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())

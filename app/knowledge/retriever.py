from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.context import RequestContext
from app.knowledge.embedding import (
    cosine_similarity,
    embed_text,
)
from app.knowledge.ranking import rank_candidates
from app.knowledge.redis_index import RedisKnowledgeIndex
from redis.asyncio import Redis


class Citation(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: str
    chunk_id: str
    title: str
    source: str
    source_version: str
    source_url: str
    heading: str | None
    excerpt: str
    content_sha256: str
    index_version: str
    score: float | None
    retrieved_at: datetime


class KnowledgeRetriever:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        redis: Redis | None = None,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.vector_index = RedisKnowledgeIndex(redis, settings) if redis is not None else None

    async def search(
        self, ctx: RequestContext, query: str, *, top_k: int | None = None
    ) -> list[Citation]:
        citations, _diagnostics = await self.search_with_diagnostics(ctx, query, top_k=top_k)
        return citations

    async def search_with_diagnostics(
        self, ctx: RequestContext, query: str, *, top_k: int | None = None
    ) -> tuple[list[Citation], dict[str, Any]]:
        started = perf_counter()
        normalized = " ".join(query.split())[:1000]
        if not normalized:
            return [], {
                "active_index_version": None,
                "filters": {"tenant_id": str(ctx.tenant_id), "query": "empty"},
                "elapsed_ms": 0.0,
                "unknown_reason": "EMPTY_QUERY",
                "retrieval_latency_scope": "postgres_query_and_local_ranking",
            }
        limit = min(max(top_k or self.settings.rag_top_k, 1), 20)
        vector_backend = "postgres_fallback"
        active_index_version: str | None = None
        async with self.sessions() as session:
            alias = await session.execute(
                text("""SELECT m.manifest_id,m.index_version FROM tripmate.knowledge_active_alias a
                    JOIN tripmate.knowledge_manifest m
                      ON m.tenant_id=a.tenant_id AND m.manifest_id=a.manifest_id
                    WHERE a.tenant_id=:tenant AND a.index_alias=:alias AND m.status='ACTIVE'"""),
                {"tenant": ctx.tenant_id, "alias": self.settings.rag_index_alias},
            )
            active = alias.mappings().one_or_none()
            if active is not None:
                active_index_version = str(active["index_version"])
            if active is not None and self.vector_index is not None:
                try:
                    vector_rows = await self.vector_index.search(
                        ctx.tenant_id,
                        active["manifest_id"],
                        normalized,
                        limit=limit,
                    )
                except Exception:
                    vector_rows = []
                if vector_rows:
                    ids = [str(item["chunk_id"]) for item in vector_rows]
                    placeholders = ",".join(f":chunk_{index}" for index in range(len(ids)))
                    valid_result = await session.execute(
                        text(f"""SELECT c.chunk_id FROM tripmate.knowledge_chunk c
                            JOIN tripmate.knowledge_document d
                              ON d.tenant_id=c.tenant_id AND d.document_id=c.document_id
                            JOIN tripmate.knowledge_manifest m
                              ON m.tenant_id=c.tenant_id AND m.manifest_id=c.manifest_id
                            WHERE c.tenant_id=:tenant AND c.manifest_id=:manifest
                              AND m.status='ACTIVE' AND d.status<>'DELETED'
                              AND c.chunk_id IN ({placeholders})"""),
                        {
                            "tenant": ctx.tenant_id,
                            "manifest": active["manifest_id"],
                            **{f"chunk_{index}": value for index, value in enumerate(ids)},
                        },
                    )
                    valid_ids = {str(row["chunk_id"]) for row in valid_result.mappings()}
                    citations = [
                        self._citation(item, float(item["score"]))
                        for item in vector_rows
                        if str(item["chunk_id"]) in valid_ids
                        and float(item["score"]) >= self.settings.rag_min_score
                    ]
                    vector_backend = "redis_hnsw"
                    return citations, {
                        "active_index_version": active_index_version,
                        "filters": {
                            "tenant_id": str(ctx.tenant_id),
                            "index_alias": self.settings.rag_index_alias,
                            "deleted_documents": "excluded",
                            "manifest_status": "ACTIVE",
                            "top_k": limit,
                        },
                        "elapsed_ms": round((perf_counter() - started) * 1000, 3),
                        "unknown_reason": None if citations else "BELOW_MIN_SCORE",
                        "retrieval_latency_scope": "redis_hnsw_and_postgres_citation_filter",
                        "vector_backend": vector_backend,
                    }
            rows = await session.execute(
                text("""SELECT d.document_id,c.chunk_id,d.title,d.source,d.source_version,d.source_url,
                    c.heading,c.content,c.content_sha256,c.embedding,c.ordinal,m.index_version,
                    m.embedding_model,m.vector_dimensions,d.retrieved_at,
                    ts_rank_cd(to_tsvector('simple',c.content),plainto_tsquery('simple',:query)) AS lexical_score
                    FROM tripmate.knowledge_chunk c
                    JOIN tripmate.knowledge_document d ON d.tenant_id=c.tenant_id AND d.document_id=c.document_id
                    JOIN tripmate.knowledge_manifest m ON m.tenant_id=c.tenant_id AND m.manifest_id=c.manifest_id
                    JOIN tripmate.knowledge_active_alias a ON a.tenant_id=m.tenant_id
                      AND a.manifest_id=m.manifest_id AND a.index_alias=:alias
                    WHERE c.tenant_id=:tenant AND d.status<>'DELETED' AND m.status='ACTIVE'
                    ORDER BY d.retrieved_at DESC,c.ordinal"""),
                {
                    "query": normalized,
                    "alias": self.settings.rag_index_alias,
                    "tenant": ctx.tenant_id,
                },
            )
            mapped = [dict(row) for row in rows.mappings()]
        ranked = rank_candidates(
            normalized,
            mapped,
            id_key="chunk_id",
            text_key="content",
            embedding_key="embedding",
            lexical_key="lexical_score",
        )
        citations = [
            self._citation(item.candidate, item.score)
            for item in ranked[:limit]
            if item.score >= self.settings.rag_min_score
        ]
        unknown_reason = None
        if not citations:
            unknown_reason = "NO_ACTIVE_ALIAS_OR_MATCH" if not mapped else "BELOW_MIN_SCORE"
        active_versions = sorted({str(item["index_version"]) for item in mapped})
        return citations, {
            "active_index_version": active_index_version
            or (active_versions[0] if len(active_versions) == 1 else active_versions),
            "filters": {
                "tenant_id": str(ctx.tenant_id),
                "index_alias": self.settings.rag_index_alias,
                "deleted_documents": "excluded",
                "manifest_status": "ACTIVE",
                "top_k": limit,
            },
            "elapsed_ms": round((perf_counter() - started) * 1000, 3),
            "unknown_reason": unknown_reason,
            "retrieval_latency_scope": "postgres_query_and_local_ranking",
            "vector_backend": vector_backend,
        }

    @staticmethod
    def _citation(row: Any, score: float) -> Citation:
        retrieved_at = row["retrieved_at"]
        if isinstance(retrieved_at, str):
            retrieved_at = datetime.fromisoformat(retrieved_at)
        return Citation(
            document_id=str(row["document_id"]),
            chunk_id=str(row["chunk_id"]),
            title=row["title"],
            source=row["source"],
            source_version=row["source_version"],
            source_url=row["source_url"],
            heading=row["heading"],
            excerpt=" ".join(row["content"].split())[:600],
            content_sha256=row["content_sha256"],
            index_version=row["index_version"],
            score=score,
            retrieved_at=retrieved_at.astimezone(UTC),
        )

    async def search_private_cases(
        self, ctx: RequestContext, query: str, *, current_generation: int, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Private examples are filtered by both principal and generation; tombstoned rows never return."""
        if ctx.user_id is None:
            return []
        normalized = " ".join(query.split())[:500]
        if not normalized:
            return []
        async with self.sessions() as session:
            rows = await session.execute(
                text("""SELECT vector_id,content_redacted,content_hash,generation,embedding,created_at
                    FROM tripmate.private_case_vector
                    WHERE tenant_id=:tenant AND user_id=:user AND tombstoned_at IS NULL
                      AND generation=:generation
                      AND (source_preference_id IS NULL OR EXISTS (
                        SELECT 1 FROM tripmate.user_preference p
                        WHERE p.tenant_id=tripmate.private_case_vector.tenant_id
                          AND p.preference_id=tripmate.private_case_vector.source_preference_id
                          AND p.user_id=tripmate.private_case_vector.user_id AND p.deleted_at IS NULL))
                    ORDER BY created_at DESC LIMIT 1000"""),
                {
                    "tenant": ctx.tenant_id,
                    "user": ctx.user_id,
                    "generation": current_generation,
                },
            )
            cases = [dict(row) for row in rows.mappings()]
        query_vector = embed_text(normalized)
        ranked = [
            (
                cosine_similarity(query_vector, row["embedding"])
                if row["embedding"] is not None
                else float(normalized.casefold() in row["content_redacted"].casefold()),
                row,
            )
            for row in cases
        ]
        ranked.sort(key=lambda item: (-item[0], item[1]["created_at"], str(item[1]["vector_id"])))
        return [
            {
                "vector_id": str(row["vector_id"]),
                "content_redacted": row["content_redacted"],
                "content_hash": row["content_hash"],
                "generation": int(row["generation"]),
                "score": score,
            }
            for score, row in ranked[: min(max(limit, 1), 20)]
            if score > 0
        ]

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.context import RequestContext


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
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.sessions = sessions
        self.settings = settings

    async def search(
        self, ctx: RequestContext, query: str, *, top_k: int | None = None
    ) -> list[Citation]:
        normalized = " ".join(query.split())[:1000]
        if not normalized:
            return []
        limit = min(max(top_k or self.settings.rag_top_k, 1), 20)
        async with self.sessions() as session:
            rows = await session.execute(
                text("""SELECT d.document_id,c.chunk_id,d.title,d.source,d.source_version,d.source_url,
                    c.heading,c.content,c.content_sha256,m.index_version,d.retrieved_at,
                    ts_rank_cd(to_tsvector('simple',c.content),plainto_tsquery('simple',:query)) AS lexical_score
                    FROM tripmate.knowledge_chunk c
                    JOIN tripmate.knowledge_document d ON d.tenant_id=c.tenant_id AND d.document_id=c.document_id
                    JOIN tripmate.knowledge_manifest m ON m.tenant_id=c.tenant_id AND m.manifest_id=c.manifest_id
                    JOIN tripmate.knowledge_active_alias a ON a.tenant_id=m.tenant_id
                      AND a.manifest_id=m.manifest_id AND a.index_alias=:alias
                    WHERE c.tenant_id=:tenant AND d.status='ACTIVE' AND m.status='ACTIVE'
                      AND (c.content ILIKE :pattern OR d.title ILIKE :pattern
                           OR c.metadata->>'keywords' ILIKE :pattern)
                    ORDER BY CASE WHEN c.content ILIKE :pattern THEN 0 ELSE 1 END,
                      lexical_score DESC,d.retrieved_at DESC,c.ordinal LIMIT :limit"""),
                {
                    "query": normalized,
                    "pattern": f"%{normalized}%",
                    "alias": self.settings.rag_index_alias,
                    "tenant": ctx.tenant_id,
                    "limit": limit,
                },
            )
            return [
                Citation(
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
                    score=float(row["lexical_score"]) if row["lexical_score"] is not None else None,
                    retrieved_at=row["retrieved_at"].astimezone(UTC),
                )
                for row in rows.mappings()
            ]

    async def search_private_cases(
        self, ctx: RequestContext, query: str, *, current_generation: int, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Private examples are filtered by both principal and generation; tombstoned rows never return."""
        if ctx.user_id is None:
            return []
        async with self.sessions() as session:
            rows = await session.execute(
                text("""SELECT vector_id,content_redacted,content_hash,generation FROM tripmate.private_case_vector
                    WHERE tenant_id=:tenant AND user_id=:user AND tombstoned_at IS NULL
                      AND generation=:generation AND content_redacted ILIKE :pattern
                    ORDER BY created_at DESC LIMIT :limit"""),
                {
                    "tenant": ctx.tenant_id,
                    "user": ctx.user_id,
                    "generation": current_generation,
                    "pattern": f"%{' '.join(query.split())[:500]}%",
                    "limit": min(max(limit, 1), 20),
                },
            )
            return [dict(row) for row in rows.mappings()]

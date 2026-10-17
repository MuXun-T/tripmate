"""Rebuildable Redis Stack HNSW index for curated knowledge chunks."""

from __future__ import annotations

from collections.abc import Awaitable, Iterable, Mapping, Sequence
from datetime import datetime
from struct import pack
from typing import Any, cast
from uuid import UUID

from app.config import Settings
from app.knowledge.embedding import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, embed_text
from redis.asyncio import Redis
from redis.exceptions import ResponseError


class RedisKnowledgeIndex:
    """Keep vector search rebuildable; PostgreSQL remains the citation authority."""

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings
        root = f"{settings.redis_prefix}:knowledge:{settings.app_env}"
        self.index_name = f"{root}:hnsw"
        self.key_prefix = f"{root}:vector:"
        self._ready = False

    def _key(self, tenant_id: UUID, manifest_id: UUID, chunk_id: UUID) -> str:
        return f"{self.key_prefix}{tenant_id}:{manifest_id}:{chunk_id}"

    def _ready_key(self, tenant_id: UUID, manifest_id: UUID) -> str:
        return f"{self.key_prefix}{tenant_id}:{manifest_id}:_ready"

    async def setup(self) -> None:
        if self._ready:
            return
        try:
            await self.redis.execute_command("FT.INFO", self.index_name)  # type: ignore[no-untyped-call]
        except ResponseError as exc:
            message = str(exc).casefold()
            if "unknown index" not in message and "no such index" not in message:
                raise
            await self.redis.execute_command(  # type: ignore[no-untyped-call]
                "FT.CREATE",
                self.index_name,
                "ON",
                "HASH",
                "PREFIX",
                1,
                self.key_prefix,
                "SCHEMA",
                "tenant",
                "TAG",
                "manifest",
                "TAG",
                "deleted",
                "TAG",
                "chunk_id",
                "TAG",
                "document_id",
                "TAG",
                "index_version",
                "TAG",
                "title",
                "TEXT",
                "source",
                "TAG",
                "source_version",
                "TAG",
                "source_url",
                "TEXT",
                "heading",
                "TEXT",
                "content",
                "TEXT",
                "content_sha256",
                "TAG",
                "retrieved_at",
                "TAG",
                "embedding_model",
                "TAG",
                "embedding",
                "VECTOR",
                "HNSW",
                6,
                "TYPE",
                "FLOAT32",
                "DIM",
                EMBEDDING_DIMENSIONS,
                "DISTANCE_METRIC",
                "COSINE",
                "M",
                16,
                "EF_CONSTRUCTION",
                200,
            )
        self._ready = True

    async def check_ready(self) -> bool:
        try:
            await self.setup()
        except Exception:
            return False
        return True

    async def replace_manifest(
        self,
        tenant_id: UUID,
        manifest_id: UUID,
        rows: Iterable[Mapping[str, Any]],
    ) -> int:
        await self.setup()
        await self.redis.delete(self._ready_key(tenant_id, manifest_id))
        await self.drop_manifest(tenant_id, manifest_id)
        pipe = self.redis.pipeline(transaction=False)
        count = 0
        for row in rows:
            vector = row.get("embedding")
            if vector is None:
                vector = embed_text(str(row["content"]))
            values = {
                "tenant": str(tenant_id),
                "manifest": str(manifest_id),
                "deleted": "0",
                "chunk_id": str(row["chunk_id"]),
                "document_id": str(row["document_id"]),
                "index_version": str(row["index_version"]),
                "title": str(row["title"]),
                "source": str(row["source"]),
                "source_version": str(row["source_version"]),
                "source_url": str(row["source_url"]),
                "heading": str(row.get("heading") or ""),
                "content": str(row["content"]),
                "content_sha256": str(row["content_sha256"]).strip(),
                "retrieved_at": _iso_datetime(row["retrieved_at"]),
                "embedding_model": EMBEDDING_MODEL,
                "vector_dimensions": str(EMBEDDING_DIMENSIONS),
                "embedding": _pack_vector(vector),
            }
            pipe.hset(self._key(tenant_id, manifest_id, UUID(str(row["chunk_id"]))), mapping=values)
            count += 1
        if count:
            await pipe.execute()
            await self.redis.set(self._ready_key(tenant_id, manifest_id), "1")
        return count

    async def drop_manifest(self, tenant_id: UUID, manifest_id: UUID) -> int:
        keys = [
            key
            async for key in self.redis.scan_iter(
                match=f"{self.key_prefix}{tenant_id}:{manifest_id}:*"
            )
        ]
        if keys:
            await self.redis.delete(*keys)
        await self.redis.delete(self._ready_key(tenant_id, manifest_id))
        return len(keys)

    async def tombstone_documents(
        self, tenant_id: UUID, document_ids: Sequence[UUID]
    ) -> int:
        if not document_ids:
            return 0
        wanted = {str(item) for item in document_ids}
        keys = [key async for key in self.redis.scan_iter(match=f"{self.key_prefix}{tenant_id}:*")]
        pipe = self.redis.pipeline(transaction=False)
        count = 0
        for key in keys:
            key_text = key.decode() if isinstance(key, bytes) else str(key)
            if key_text.endswith(":_ready"):
                continue
            document_id = await cast(Awaitable[Any], self.redis.hget(key, "document_id"))
            if str(document_id) in wanted:
                pipe.hset(key, "deleted", "1")
                count += 1
        if count:
            await pipe.execute()
        return count

    async def search(
        self,
        tenant_id: UUID,
        manifest_id: UUID,
        query: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        await self.setup()
        if not await self.redis.exists(self._ready_key(tenant_id, manifest_id)):
            return []
        query_vector = _pack_vector(embed_text(query))
        query_text = (
            f"(@tenant:{{{tenant_id}}} @manifest:{{{manifest_id}}} @deleted:{{0}})"
            "=>[KNN $k @embedding $query_vector AS vector_score]"
        )
        raw = await self.redis.execute_command(  # type: ignore[no-untyped-call]
            "FT.SEARCH",
            self.index_name,
            query_text,
            "PARAMS",
            4,
            "k",
            str(limit),
            "query_vector",
            query_vector,
            "SORTBY",
            "vector_score",
            "ASC",
            "RETURN",
            13,
            "chunk_id",
            "document_id",
            "index_version",
            "title",
            "source",
            "source_version",
            "source_url",
            "heading",
            "content",
            "content_sha256",
            "retrieved_at",
            "vector_score",
            "embedding_model",
            "DIALECT",
            2,
        )
        return _parse_search_results(raw)


def _pack_vector(values: Sequence[float]) -> bytes:
    if len(values) != EMBEDDING_DIMENSIONS:
        raise ValueError("knowledge vector dimensions do not match Redis HNSW schema")
    return pack(f"<{EMBEDDING_DIMENSIONS}f", *[float(value) for value in values])


def _iso_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _parse_search_results(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    count = int(raw[0])
    values = raw[1:]
    results: list[dict[str, Any]] = []
    for offset in range(0, min(count, len(values) // 2)):
        fields = values[offset * 2 + 1]
        if isinstance(fields, dict):
            mapped = fields
        else:
            mapped = dict(zip(fields[::2], fields[1::2], strict=True))
        normalized = {
            (key.decode() if isinstance(key, bytes) else str(key)): (
                value.decode() if isinstance(value, bytes) else value
            )
            for key, value in mapped.items()
        }
        distance = float(normalized.pop("vector_score", 1.0))
        normalized["score"] = max(0.0, min(1.0, 1.0 - distance))
        normalized["embedding_model"] = str(normalized.get("embedding_model", EMBEDDING_MODEL))
        results.append(normalized)
    return results

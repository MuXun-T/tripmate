import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.context import RequestContext
from app.domain.errors import DomainError
from app.domain.idempotency import IdempotencyScope, canonical_json, request_hash, sha256_hex
from app.domain.ports import Clock, IdGenerator
from app.events.schema import EventEnvelope
from app.inventory.keys import KeyCodec
from app.knowledge.embedding import embed_text
from app.persistence.postgres.database import affected_rows
from redis.asyncio import Redis


class PreferenceService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        clock: Clock,
        ids: IdGenerator,
        redis: Redis | None = None,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.clock = clock
        self.ids = ids
        self.redis = redis
        self.keys = KeyCodec(settings.redis_prefix, settings.app_env)

    async def list_for_user(self, ctx: RequestContext) -> list[dict[str, Any]]:
        self._require_user(ctx)
        async with self.sessions() as session:
            rows = await session.execute(
                text("""SELECT preference_id,scope,preference_key,value_json,source,effective_at,
                    generation,version FROM tripmate.user_preference WHERE tenant_id=:tenant
                    AND user_id=:user AND deleted_at IS NULL ORDER BY scope,preference_key"""),
                {"tenant": ctx.tenant_id, "user": ctx.user_id},
            )
            return [self._json_value(row) for row in rows.mappings()]

    async def save(
        self,
        ctx: RequestContext,
        *,
        scope: str,
        key: str,
        value: Any,
        expected_version: int | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_user(ctx)
        assert ctx.user_id is not None
        canonical_value = self._validate_value(key, value)
        route = "/v1/preferences"
        operation = "preference.save"
        scope_key = IdempotencyScope(
            ctx.tenant_id, ctx.user_id, route, operation, idempotency_key
        ).key_hash()
        req_hash = request_hash(
            {
                "scope": scope,
                "key": key,
                "value": canonical_value,
                "expected_version": expected_version,
            }
        )
        now = self.clock.now()
        unknown_commit = False
        async with self.sessions() as session:
            tx = await session.begin()
            try:
                await self._lock_idempotency(session, scope_key)
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 74192027))"),
                    {"key": canonical_json([str(ctx.tenant_id), str(ctx.user_id), scope, key])},
                )
                prior = await self._get_idempotency(session, ctx, route, operation, scope_key)
                if prior is not None:
                    result = self._replay(prior, req_hash)
                    await tx.rollback()
                    return result
                row_result = await session.execute(
                    text("""SELECT * FROM tripmate.user_preference WHERE tenant_id=:tenant AND user_id=:user
                        AND scope=:scope AND preference_key=:key AND deleted_at IS NULL FOR UPDATE"""),
                    {"tenant": ctx.tenant_id, "user": ctx.user_id, "scope": scope, "key": key},
                )
                row = row_result.mappings().one_or_none()
                if (
                    row is not None
                    and expected_version is not None
                    and int(row["version"]) != expected_version
                ):
                    raise DomainError("VERSION_CONFLICT", "Preference version is stale", 409)
                pref_id = row["preference_id"] if row is not None else self.ids.new()
                generation = int(row["generation"]) if row is not None else 1
                version = int(row["version"]) + 1 if row is not None else 1
                if row is None:
                    await session.execute(
                        text("""INSERT INTO tripmate.user_preference
                            (preference_id,tenant_id,user_id,scope,preference_key,value_json,source,effective_at,
                             generation,version) VALUES(:id,:tenant,:user,:scope,:key,CAST(:value AS jsonb),
                             'USER_EXPLICIT',:now,:generation,:version)"""),
                        {
                            "id": pref_id,
                            "tenant": ctx.tenant_id,
                            "user": ctx.user_id,
                            "scope": scope,
                            "key": key,
                            "value": json.dumps(canonical_value),
                            "now": now,
                            "generation": generation,
                            "version": version,
                        },
                    )
                else:
                    updated = await session.execute(
                        text("""UPDATE tripmate.user_preference SET value_json=CAST(:value AS jsonb),
                            source='USER_EXPLICIT',effective_at=:now,version=:version,updated_at=:now
                            WHERE tenant_id=:tenant AND preference_id=:id AND version=:old_version"""),
                        {
                            "value": json.dumps(canonical_value),
                            "now": now,
                            "version": version,
                            "tenant": ctx.tenant_id,
                            "id": pref_id,
                            "old_version": row["version"],
                        },
                    )
                    if affected_rows(updated) != 1:
                        raise DomainError("VERSION_CONFLICT", "Preference changed concurrently", 409)
                if key in {"walking_limit.max_minutes", "indoor_ratio.min", "travel_style.tags"}:
                    content = f"{key}: {json.dumps(canonical_value, ensure_ascii=False, sort_keys=True)}"
                    await session.execute(
                        text("""INSERT INTO tripmate.private_case_vector
                            (vector_id,tenant_id,user_id,source_preference_id,generation,content_redacted,
                             content_hash,embedding)
                            VALUES(:id,:tenant,:user,:preference,:generation,:content,:hash,:embedding)
                            ON CONFLICT (tenant_id,source_preference_id)
                                WHERE source_preference_id IS NOT NULL AND tombstoned_at IS NULL
                            DO UPDATE SET generation=EXCLUDED.generation,content_redacted=EXCLUDED.content_redacted,
                                content_hash=EXCLUDED.content_hash,embedding=EXCLUDED.embedding,created_at=now()"""),
                        {
                            "id": self.ids.new(),
                            "tenant": ctx.tenant_id,
                            "user": ctx.user_id,
                            "preference": pref_id,
                            "generation": generation,
                            "content": content,
                            "hash": sha256_hex(content),
                            "embedding": embed_text(content),
                        },
                    )
                await self._write_event(
                    session,
                    ctx,
                    event_type="preference.updated.v1",
                    aggregate_id=pref_id,
                    version=version,
                    payload={"preference_id": str(pref_id), "generation": generation},
                )
                await self._audit(
                    session,
                    ctx,
                    "preference.updated",
                    "UPDATED",
                    sha256_hex(canonical_json(canonical_value)),
                )
                result = {
                    "preference_id": pref_id,
                    "scope": scope,
                    "key": key,
                    "value": canonical_value,
                    "source": "USER_EXPLICIT",
                    "effective_at": now,
                    "generation": generation,
                    "version": version,
                }
                await self._store_idempotency(
                    session, ctx, route, operation, scope_key, req_hash, result, pref_id, now
                )
                try:
                    await tx.commit()
                except DBAPIError:
                    unknown_commit = True
            except Exception:
                if tx.is_active:
                    await tx.rollback()
                raise
        if unknown_commit:
            async with self.sessions() as session:
                prior = await self._get_idempotency(session, ctx, route, operation, scope_key)
            if prior is None:
                raise DomainError(
                    "RECONCILIATION_REQUIRED", "Preference save outcome is unknown", 503, True
                )
            result = self._replay(prior, req_hash)
        return result

    async def delete(
        self,
        ctx: RequestContext,
        preference_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_user(ctx)
        assert ctx.user_id is not None
        route = f"/v1/preferences/{preference_id}"
        operation = "preference.delete"
        scope_key = IdempotencyScope(
            ctx.tenant_id, ctx.user_id, route, operation, idempotency_key
        ).key_hash()
        req_hash = request_hash(
            {"preference_id": preference_id, "expected_version": expected_version}
        )
        now = self.clock.now()
        unknown_commit = False
        async with self.sessions() as session:
            tx = await session.begin()
            try:
                await self._lock_idempotency(session, scope_key)
                prior = await self._get_idempotency(session, ctx, route, operation, scope_key)
                if prior is not None:
                    result = self._replay(prior, req_hash)
                    await tx.rollback()
                    return result
                row_result = await session.execute(
                    text("""SELECT preference_id,generation,version FROM tripmate.user_preference
                        WHERE tenant_id=:tenant AND user_id=:user AND preference_id=:id
                          AND deleted_at IS NULL FOR UPDATE"""),
                    {"tenant": ctx.tenant_id, "user": ctx.user_id, "id": preference_id},
                )
                row = row_result.mappings().one_or_none()
                if row is None:
                    raise DomainError("NOT_FOUND", "Preference was not found", 404)
                if int(row["version"]) != expected_version:
                    raise DomainError("VERSION_CONFLICT", "Preference version is stale", 409)
                generation, version = int(row["generation"]) + 1, int(row["version"]) + 1
                updated = await session.execute(
                    text("""UPDATE tripmate.user_preference SET deleted_at=:now,generation=:generation,
                        version=:version,updated_at=:now WHERE tenant_id=:tenant AND preference_id=:id
                        AND user_id=:user AND version=:old_version AND deleted_at IS NULL"""),
                    {
                        "now": now,
                        "generation": generation,
                        "version": version,
                        "tenant": ctx.tenant_id,
                        "id": preference_id,
                        "user": ctx.user_id,
                        "old_version": expected_version,
                    },
                )
                if affected_rows(updated) != 1:
                    raise DomainError("VERSION_CONFLICT", "Preference changed concurrently", 409)
                await session.execute(
                    text("""UPDATE tripmate.private_case_vector SET tombstoned_at=:now,
                        generation=GREATEST(generation+1,:generation)
                        WHERE tenant_id=:tenant AND user_id=:user AND tombstoned_at IS NULL"""),
                    {
                        "now": now,
                        "generation": generation,
                        "tenant": ctx.tenant_id,
                        "user": ctx.user_id,
                    },
                )
                await self._write_event(
                    session,
                    ctx,
                    event_type="preference.deleted.v1",
                    aggregate_id=preference_id,
                    version=version,
                    payload={"preference_id": str(preference_id), "generation": generation},
                )
                await self._audit(session, ctx, "preference.deleted", "TOMBSTONED", None)
                result = {
                    "preference_id": preference_id,
                    "deleted": True,
                    "generation": generation,
                    "version": version,
                    "deleted_at": now,
                }
                await self._store_idempotency(
                    session, ctx, route, operation, scope_key, req_hash, result, preference_id, now
                )
                try:
                    await tx.commit()
                except DBAPIError:
                    unknown_commit = True
            except Exception:
                if tx.is_active:
                    await tx.rollback()
                raise
        if unknown_commit:
            async with self.sessions() as session:
                prior = await self._get_idempotency(session, ctx, route, operation, scope_key)
            if prior is None:
                raise DomainError(
                    "RECONCILIATION_REQUIRED", "Preference delete outcome is unknown", 503, True
                )
            result = self._replay(prior, req_hash)
        if self.redis is not None:
            key = f"{self.keys._root(ctx.tenant_id)}:user:{ctx.user_id}:memory"
            try:
                await self.redis.delete(key)
            except Exception:
                # The committed tombstone filters retrieval until asynchronous cache cleanup succeeds.
                pass
        return result

    @staticmethod
    async def _lock_idempotency(session: AsyncSession, key_hash: str) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key_hash, 74192026))"),
            {"key_hash": key_hash},
        )

    async def _get_idempotency(
        self,
        session: AsyncSession,
        ctx: RequestContext,
        route: str,
        operation: str,
        key_hash: str,
    ) -> Any:
        result = await session.execute(
            text("""SELECT request_hash,result_json FROM tripmate.idempotency_record
                WHERE tenant_id=:tenant AND user_id=:user AND route=:route AND operation=:operation
                  AND key_hash=:key_hash AND expires_at>:now"""),
            {
                "tenant": ctx.tenant_id,
                "user": ctx.user_id,
                "route": route,
                "operation": operation,
                "key_hash": key_hash,
                "now": self.clock.now(),
            },
        )
        return result.mappings().one_or_none()

    @staticmethod
    def _replay(prior: Any, expected_hash: str) -> dict[str, Any]:
        if prior["request_hash"] != expected_hash:
            raise DomainError("IDEMPOTENCY_KEY_REUSE", "Idempotency key was reused", 409)
        result = prior["result_json"]
        replayed = result if isinstance(result, dict) else json.loads(result)
        if isinstance(replayed.get("preference_id"), str):
            replayed["preference_id"] = UUID(replayed["preference_id"])
        for key in ("effective_at", "deleted_at"):
            if isinstance(replayed.get(key), str):
                replayed[key] = datetime.fromisoformat(replayed[key])
        return replayed

    async def _store_idempotency(
        self,
        session: AsyncSession,
        ctx: RequestContext,
        route: str,
        operation: str,
        key_hash: str,
        req_hash: str,
        result: dict[str, Any],
        resource_id: UUID,
        now: datetime,
    ) -> None:
        await session.execute(
            text("""DELETE FROM tripmate.idempotency_record
                WHERE tenant_id=:tenant AND user_id=:user AND route=:route AND operation=:operation
                  AND key_hash=:key_hash AND expires_at<=:now"""),
            {
                "tenant": ctx.tenant_id,
                "user": ctx.user_id,
                "route": route,
                "operation": operation,
                "key_hash": key_hash,
                "now": now,
            },
        )
        await session.execute(
            text("""INSERT INTO tripmate.idempotency_record
                (tenant_id,user_id,route,operation,key_hash,request_hash,http_status,result_json,
                 resource_type,resource_id,expires_at)
                VALUES(:tenant,:user,:route,:operation,:key_hash,:request_hash,200,
                 CAST(:result AS jsonb),'user_preference',:resource,:expires)"""),
            {
                "tenant": ctx.tenant_id,
                "user": ctx.user_id,
                "route": route,
                "operation": operation,
                "key_hash": key_hash,
                "request_hash": req_hash,
                "result": json.dumps(result, default=str),
                "resource": resource_id,
                "expires": now + timedelta(seconds=self.settings.idempotency_ttl_seconds),
            },
        )

    async def _write_event(
        self,
        session: AsyncSession,
        ctx: RequestContext,
        *,
        event_type: str,
        aggregate_id: UUID,
        version: int,
        payload: dict[str, Any],
    ) -> None:
        envelope = EventEnvelope(
            event_id=self.ids.new(),
            event_type=event_type,
            event_version=1,
            occurred_at=self.clock.now(),
            tenant_id=ctx.tenant_id,
            aggregate_type="user_preference",
            aggregate_id=aggregate_id,
            aggregate_version=version,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            producer="tripmate-api",
            payload=payload,
        )
        await session.execute(
            text("""INSERT INTO tripmate.outbox_event
                (event_id,tenant_id,aggregate_type,aggregate_id,aggregate_version,event_type,event_version,payload,status)
                VALUES(:event,:tenant,'user_preference',:aggregate,:version,:type,1,CAST(:payload AS jsonb),'PENDING')"""),
            {
                "event": envelope.event_id,
                "tenant": ctx.tenant_id,
                "aggregate": aggregate_id,
                "version": version,
                "type": event_type,
                "payload": json.dumps(envelope.model_dump(mode="json")),
            },
        )

    async def _audit(
        self,
        session: AsyncSession,
        ctx: RequestContext,
        action: str,
        result_code: str,
        params_hash: str | None,
    ) -> None:
        await session.execute(
            text("""INSERT INTO tripmate.audit_log
                (tenant_id,actor_user_id,request_id,trace_id,action,parameters_hash,result_code)
                VALUES(:tenant,:user,:request,:trace,:action,:hash,:result)"""),
            {
                "tenant": ctx.tenant_id,
                "user": ctx.user_id,
                "request": ctx.request_id,
                "trace": ctx.trace_id,
                "action": action,
                "hash": params_hash,
                "result": result_code,
            },
        )

    @staticmethod
    def _validate_value(key: str, value: Any) -> Any:
        if key == "walking_limit.max_minutes":
            if not isinstance(value, int) or not 1 <= value <= 1440:
                raise DomainError(
                    "INVALID_ARGUMENT", "Walking limit must be 1 to 1440 minutes", 400
                )
            return value
        if key == "indoor_ratio.min":
            if not isinstance(value, int | float) or not 0 <= float(value) <= 1:
                raise DomainError(
                    "INVALID_ARGUMENT", "Indoor ratio must be between zero and one", 400
                )
            return float(value)
        if key == "dietary_restriction.tags":
            if (
                not isinstance(value, list)
                or len(value) > 30
                or any(
                    not isinstance(item, str) or not item.strip() or len(item) > 40
                    for item in value
                )
            ):
                raise DomainError("INVALID_ARGUMENT", "Dietary tags must be short strings", 400)
            return sorted({item.strip().casefold() for item in value})
        if key == "travel_style.tags":
            if (
                not isinstance(value, list)
                or len(value) > 30
                or any(not isinstance(item, str) or len(item) > 40 for item in value)
            ):
                raise DomainError("INVALID_ARGUMENT", "Travel style tags are invalid", 400)
            return sorted({item.strip().casefold() for item in value if item.strip()})
        raise DomainError("INVALID_ARGUMENT", "Preference key is not registered", 400)

    @staticmethod
    def _json_value(row: Any) -> dict[str, Any]:
        data = dict(row)
        if isinstance(data["value_json"], str):
            data["value_json"] = json.loads(data["value_json"])
        return data

    @staticmethod
    def _require_user(ctx: RequestContext) -> None:
        if ctx.user_id is None:
            raise DomainError("UNAUTHORIZED", "Authenticated user is required", 401)

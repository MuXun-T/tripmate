import json
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.context import RequestContext
from app.domain.errors import DomainError
from app.domain.idempotency import IdempotencyScope, request_hash
from app.domain.ports import Clock, IdGenerator
from app.events.schema import EventEnvelope


class TripService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.clock = clock
        self.ids = ids

    async def create(
        self,
        ctx: RequestContext,
        *,
        destination: str,
        start_date: Any,
        end_date: Any,
        timezone: str,
        party_size: int,
        budget_limit: Decimal | None,
        currency: str | None,
        companion_type: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if ctx.user_id is None:
            raise DomainError("UNAUTHORIZED", "Authenticated user is required", 401)
        if end_date < start_date or not 1 <= party_size <= 100:
            raise DomainError("INVALID_ARGUMENT", "Trip date range or party size is invalid", 400)
        route, operation = "/v1/trips", "trip.create"
        scope = IdempotencyScope(ctx.tenant_id, ctx.user_id, route, operation, idempotency_key)
        key_hash = scope.key_hash()
        req_hash = request_hash(
            {
                "destination": destination,
                "start_date": start_date,
                "end_date": end_date,
                "timezone": timezone,
                "party_size": party_size,
                "budget_limit": budget_limit,
                "currency": currency,
                "companion_type": companion_type,
            }
        )
        trip_id, event_id, now = self.ids.new(), self.ids.new(), self.clock.now()
        stored: dict[str, Any]
        async with self.sessions() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key_hash, 74192026))"),
                {"key_hash": key_hash},
            )
            prior = await session.execute(
                text("""SELECT request_hash,result_json FROM tripmate.idempotency_record
                    WHERE tenant_id=:tenant AND user_id=:user AND route=:route AND operation=:operation
                    AND key_hash=:key_hash AND expires_at>:now"""),
                {
                    "tenant": ctx.tenant_id,
                    "user": ctx.user_id,
                    "route": route,
                    "operation": operation,
                    "key_hash": key_hash,
                    "now": now,
                },
            )
            old = prior.mappings().one_or_none()
            if old is not None:
                if old["request_hash"] != req_hash:
                    raise DomainError(
                        "IDEMPOTENCY_KEY_REUSE", "Idempotency key has a different request", 409
                    )
                value = (
                    old["result_json"]
                    if isinstance(old["result_json"], dict)
                    else json.loads(old["result_json"])
                )
                return value
            await session.execute(
                text("""INSERT INTO tripmate.trip
                    (trip_id,tenant_id,owner_user_id,destination,start_date,end_date,timezone,party_size,
                     budget_limit,currency,companion_type,status)
                    VALUES(:trip,:tenant,:user,:destination,:start,:end,:timezone,:party,:budget,:currency,
                     :companion,'DRAFT')"""),
                {
                    "trip": trip_id,
                    "tenant": ctx.tenant_id,
                    "user": ctx.user_id,
                    "destination": destination,
                    "start": start_date,
                    "end": end_date,
                    "timezone": timezone,
                    "party": party_size,
                    "budget": budget_limit,
                    "currency": currency,
                    "companion": companion_type,
                },
            )
            await session.execute(
                text("""INSERT INTO tripmate.trip_member(tenant_id,trip_id,user_id,role,status)
                    VALUES(:tenant,:trip,:user,'OWNER','ACTIVE')"""),
                {"tenant": ctx.tenant_id, "trip": trip_id, "user": ctx.user_id},
            )
            envelope = EventEnvelope(
                event_id=event_id,
                event_type="trip.created.v1",
                event_version=1,
                occurred_at=now,
                tenant_id=ctx.tenant_id,
                aggregate_type="trip",
                aggregate_id=trip_id,
                aggregate_version=1,
                request_id=ctx.request_id,
                trace_id=ctx.trace_id,
                producer="tripmate-api",
                payload={
                    "trip_id": str(trip_id),
                    "destination": destination,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                },
            )
            await session.execute(
                text("""INSERT INTO tripmate.outbox_event
                    (event_id,tenant_id,aggregate_type,aggregate_id,aggregate_version,event_type,event_version,
                     payload,status) VALUES(:event,:tenant,'trip',:trip,1,:type,1,CAST(:payload AS jsonb),'PENDING')"""),
                {
                    "event": event_id,
                    "tenant": ctx.tenant_id,
                    "trip": trip_id,
                    "type": envelope.event_type,
                    "payload": json.dumps(envelope.model_dump(mode="json")),
                },
            )
            stored = {
                "trip_id": str(trip_id),
                "tenant_id": str(ctx.tenant_id),
                "owner_user_id": str(ctx.user_id),
                "destination": destination,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "timezone": timezone,
                "party_size": party_size,
                "budget_limit": str(budget_limit) if budget_limit is not None else None,
                "currency": currency,
                "companion_type": companion_type,
                "status": "DRAFT",
                "version": 1,
            }
            await session.execute(
                text("""INSERT INTO tripmate.idempotency_record
                    (tenant_id,user_id,route,operation,key_hash,request_hash,http_status,result_json,resource_type,
                     resource_id,expires_at) VALUES(:tenant,:user,:route,:operation,:key_hash,:request_hash,201,
                     CAST(:result AS jsonb),'trip',:resource,:expires)"""),
                {
                    "tenant": ctx.tenant_id,
                    "user": ctx.user_id,
                    "route": route,
                    "operation": operation,
                    "key_hash": key_hash,
                    "request_hash": req_hash,
                    "result": json.dumps(stored),
                    "resource": trip_id,
                    "expires": now + timedelta(seconds=self.settings.idempotency_ttl_seconds),
                },
            )
            await session.execute(
                text("""INSERT INTO tripmate.audit_log
                    (tenant_id,actor_user_id,request_id,trace_id,action,result_code,details_redacted)
                    VALUES(:tenant,:user,:request,:trace,'trip.created','CREATED',
                    jsonb_build_object('trip_id',:trip))"""),
                {
                    "tenant": ctx.tenant_id,
                    "user": ctx.user_id,
                    "request": ctx.request_id,
                    "trace": ctx.trace_id,
                    "trip": str(trip_id),
                },
            )
        return stored

    async def get(self, ctx: RequestContext, trip_id: UUID) -> dict[str, Any]:
        if ctx.user_id is None:
            raise DomainError("UNAUTHORIZED", "Authenticated user is required", 401)
        async with self.sessions() as session:
            result = await session.execute(
                text("""SELECT t.* FROM tripmate.trip t WHERE t.tenant_id=:tenant AND t.trip_id=:trip
                    AND (t.owner_user_id=:user OR EXISTS(SELECT 1 FROM tripmate.trip_member m
                      WHERE m.tenant_id=t.tenant_id AND m.trip_id=t.trip_id AND m.user_id=:user
                      AND m.status='ACTIVE'))"""),
                {"tenant": ctx.tenant_id, "trip": trip_id, "user": ctx.user_id},
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise DomainError("NOT_FOUND", "Trip was not found", 404)
            data = dict(row)
            data["budget_limit"] = (
                str(data["budget_limit"]) if data["budget_limit"] is not None else None
            )
            data["currency"] = data["currency"].strip() if data["currency"] else None
            return data

    async def set_ready(
        self, ctx: RequestContext, trip_id: UUID, expected_version: int
    ) -> dict[str, Any]:
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                text("""UPDATE tripmate.trip SET status='READY',version=version+1,updated_at=:now
                    WHERE tenant_id=:tenant AND trip_id=:trip AND owner_user_id=:user AND status='PLANNING'
                    AND version=:version RETURNING trip_id,status,version"""),
                {
                    "now": self.clock.now(),
                    "tenant": ctx.tenant_id,
                    "trip": trip_id,
                    "user": ctx.user_id,
                    "version": expected_version,
                },
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise DomainError(
                    "VERSION_CONFLICT", "Trip is not planning at the expected version", 409
                )
            return dict(row)

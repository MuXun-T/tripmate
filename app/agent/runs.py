import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.context import RequestContext
from app.domain.errors import DomainError
from app.domain.idempotency import IdempotencyScope, request_hash
from app.domain.ports import Clock, IdGenerator
from app.events.schema import EventEnvelope
from app.persistence.postgres.database import affected_rows
from app.security.redaction import content_hash, redact_message


@dataclass(frozen=True, slots=True)
class RunReceipt:
    run_id: UUID
    thread_id: str
    status: str
    version: int
    replayed: bool = False


class AgentRunService:
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

    async def create_run(
        self,
        ctx: RequestContext,
        *,
        conversation_id: UUID,
        message_id: UUID,
        message: str,
        trip_id: UUID | None,
        idempotency_key: str,
    ) -> RunReceipt:
        if ctx.user_id is None:
            raise DomainError("UNAUTHORIZED", "Authenticated user is required", 401)
        route = f"/v1/conversations/{conversation_id}/messages"
        operation = "run.create"
        scope = IdempotencyScope(ctx.tenant_id, ctx.user_id, route, operation, idempotency_key)
        key_hash = scope.key_hash()
        raw_hash = content_hash(message)
        redacted = redact_message(message)
        req_hash = request_hash({"message_hash": raw_hash, "trip_id": trip_id})
        thread_id = f"tenant:{ctx.tenant_id}:user:{ctx.user_id}:conversation:{conversation_id}"
        run_id = self.ids.new()
        event_id = self.ids.new()
        now = self.clock.now()
        result: RunReceipt
        unknown_commit = False

        async with self.sessions() as session:
            tx = await session.begin()
            try:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key_hash, 74192026))"),
                    {"key_hash": key_hash},
                )
                old = await session.execute(
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
                prior = old.mappings().one_or_none()
                if prior is not None:
                    if prior["request_hash"] != req_hash:
                        raise DomainError(
                            "IDEMPOTENCY_KEY_REUSE", "Idempotency key has a different message", 409
                        )
                    result_data = (
                        prior["result_json"]
                        if isinstance(prior["result_json"], dict)
                        else json.loads(prior["result_json"])
                    )
                    await tx.rollback()
                    return RunReceipt(
                        UUID(result_data["run_id"]),
                        result_data["thread_id"],
                        result_data["status"],
                        int(result_data["version"]),
                        True,
                    )
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:thread_id, 0))"),
                    {"thread_id": thread_id},
                )
                # Serialize messages for this thread before allocating a sequence or run.
                active = await session.scalar(
                    text("""SELECT run_id FROM tripmate.agent_run WHERE tenant_id=:tenant
                        AND thread_id=:thread AND status IN ('RUNNING','WAITING_CONFIRMATION')
                        FOR UPDATE"""),
                    {"tenant": ctx.tenant_id, "thread": thread_id},
                )
                if active is not None:
                    raise DomainError(
                        "THREAD_BUSY", "A run is already active in this conversation", 409, True
                    )
                if trip_id is not None:
                    allowed = await session.scalar(
                        text("""SELECT EXISTS(SELECT 1 FROM tripmate.trip t WHERE t.tenant_id=:tenant
                            AND t.trip_id=:trip AND (t.owner_user_id=:user OR EXISTS(
                              SELECT 1 FROM tripmate.trip_member m WHERE m.tenant_id=t.tenant_id
                              AND m.trip_id=t.trip_id AND m.user_id=:user AND m.status='ACTIVE'
                              AND m.role IN ('OWNER','EDITOR','VIEWER'))))"""),
                        {"tenant": ctx.tenant_id, "trip": trip_id, "user": ctx.user_id},
                    )
                    if not allowed:
                        raise DomainError("NOT_FOUND", "Trip was not found", 404)
                sequence = await session.scalar(
                    text("""SELECT COALESCE(MAX(sequence_no),0)+1 FROM tripmate.conversation_message
                        WHERE tenant_id=:tenant AND conversation_id=:conversation"""),
                    {"tenant": ctx.tenant_id, "conversation": conversation_id},
                )
                envelope = EventEnvelope(
                    event_id=event_id,
                    event_type="agent.run.requested.v1",
                    event_version=1,
                    occurred_at=now,
                    tenant_id=ctx.tenant_id,
                    aggregate_type="agent_run",
                    aggregate_id=run_id,
                    aggregate_version=1,
                    request_id=ctx.request_id,
                    trace_id=ctx.trace_id,
                    producer="tripmate-api",
                    payload={
                        "run_id": str(run_id),
                        "conversation_id": str(conversation_id),
                        "message_id": str(message_id),
                        "message_hash": raw_hash,
                    },
                )
                await session.execute(
                    text("""INSERT INTO tripmate.agent_run
                        (run_id,tenant_id,user_id,trip_id,thread_id,intent,status,requested_event_id)
                        VALUES(:run,:tenant,:user,:trip,:thread,'UNCLASSIFIED','RUNNING',:event)"""),
                    {
                        "run": run_id,
                        "tenant": ctx.tenant_id,
                        "user": ctx.user_id,
                        "trip": trip_id,
                        "thread": thread_id,
                        "event": event_id,
                    },
                )
                await session.execute(
                    text("""INSERT INTO tripmate.conversation_message
                        (tenant_id,conversation_id,message_id,user_id,sequence_no,content_redacted,content_hash,run_id)
                        VALUES(:tenant,:conversation,:message,:user,:sequence,:content,:hash,:run)"""),
                    {
                        "tenant": ctx.tenant_id,
                        "conversation": conversation_id,
                        "message": message_id,
                        "user": ctx.user_id,
                        "sequence": sequence,
                        "content": redacted,
                        "hash": raw_hash,
                        "run": run_id,
                    },
                )
                await session.execute(
                    text("""INSERT INTO tripmate.outbox_event
                        (event_id,tenant_id,aggregate_type,aggregate_id,aggregate_version,event_type,event_version,
                         payload,status) VALUES(:event,:tenant,'agent_run',:run,1,:event_type,1,
                         CAST(:payload AS jsonb),'PENDING')"""),
                    {
                        "event": event_id,
                        "tenant": ctx.tenant_id,
                        "run": run_id,
                        "event_type": envelope.event_type,
                        "payload": json.dumps(envelope.model_dump(mode="json")),
                    },
                )
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
                        (tenant_id,user_id,route,operation,key_hash,request_hash,http_status,result_json,expires_at)
                        VALUES(:tenant,:user,:route,:operation,:key_hash,:request_hash,202,CAST(:result AS jsonb),:expires)"""),
                    {
                        "tenant": ctx.tenant_id,
                        "user": ctx.user_id,
                        "route": route,
                        "operation": operation,
                        "key_hash": key_hash,
                        "request_hash": req_hash,
                        "result": json.dumps(
                            {
                                "run_id": str(run_id),
                                "thread_id": thread_id,
                                "status": "RUNNING",
                                "version": 1,
                            }
                        ),
                        "expires": now + timedelta(seconds=self.settings.idempotency_ttl_seconds),
                    },
                )
                await session.execute(
                    text("""INSERT INTO tripmate.audit_log
                        (tenant_id,actor_user_id,run_id,request_id,trace_id,action,result_code,details_redacted)
                        VALUES(:tenant,:user,:run,:request,:trace,'agent.run.requested','ACCEPTED',
                        jsonb_build_object('message_hash',:message_hash))"""),
                    {
                        "tenant": ctx.tenant_id,
                        "user": ctx.user_id,
                        "run": run_id,
                        "request": ctx.request_id,
                        "trace": ctx.trace_id,
                        "message_hash": raw_hash,
                    },
                )
                try:
                    await tx.commit()
                except DBAPIError:
                    unknown_commit = True
                result = RunReceipt(run_id, thread_id, "RUNNING", 1)
            except Exception:
                if tx.is_active:
                    await tx.rollback()
                raise
        if unknown_commit:
            async with self.sessions() as session:
                row = await session.execute(
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
                prior = row.mappings().one_or_none()
                if prior is None:
                    raise DomainError(
                        "RECONCILIATION_REQUIRED",
                        "Run commit outcome is unknown; retry with the same key",
                        503,
                        True,
                    )
                if prior["request_hash"] != req_hash:
                    raise DomainError(
                        "IDEMPOTENCY_KEY_REUSE", "Idempotency key has a different message", 409
                    )
                payload = (
                    prior["result_json"]
                    if isinstance(prior["result_json"], dict)
                    else json.loads(prior["result_json"])
                )
                result = RunReceipt(
                    UUID(payload["run_id"]),
                    payload["thread_id"],
                    payload["status"],
                    int(payload["version"]),
                    True,
                )
        return result

    async def claim_run(
        self, tenant_id: UUID, run_id: UUID, worker_id: str, *, lease_seconds: int = 30
    ) -> dict[str, Any] | None:
        now = self.clock.now()
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                text("""UPDATE tripmate.agent_run SET lease_owner=:worker,lease_until=:until,
                    fencing_version=fencing_version+1,version=version+1,updated_at=:now
                    WHERE tenant_id=:tenant AND run_id=:run AND status='RUNNING'
                      AND (lease_until IS NULL OR lease_until<=:now OR lease_owner=:worker)
                    RETURNING run_id,user_id,trip_id,thread_id,status,version,fencing_version,
                        checkpoint_ref,resume_command"""),
                {
                    "worker": worker_id,
                    "until": now + timedelta(seconds=lease_seconds),
                    "now": now,
                    "tenant": tenant_id,
                    "run": run_id,
                },
            )
            row = result.mappings().one_or_none()
            return dict(row) if row is not None else None

    async def list_claimable(self, *, limit: int = 20) -> list[dict[str, Any]]:
        now = self.clock.now()
        async with self.sessions() as session:
            result = await session.execute(
                text("""SELECT tenant_id,run_id,thread_id FROM tripmate.agent_run
                    WHERE status='RUNNING' AND (lease_until IS NULL OR lease_until<=:now)
                    ORDER BY created_at,run_id LIMIT :limit"""),
                {"now": now, "limit": limit},
            )
            return [dict(row) for row in result.mappings()]

    async def renew_run_lease(
        self,
        tenant_id: UUID,
        run_id: UUID,
        worker_id: str,
        fence: int,
        *,
        lease_seconds: int = 60,
    ) -> bool:
        now = self.clock.now()
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                text("""UPDATE tripmate.agent_run SET lease_until=:until,updated_at=:now
                    WHERE tenant_id=:tenant AND run_id=:run AND status='RUNNING'
                      AND lease_owner=:worker AND fencing_version=:fence AND lease_until>:now"""),
                {
                    "until": now + timedelta(seconds=lease_seconds),
                    "now": now,
                    "tenant": tenant_id,
                    "run": run_id,
                    "worker": worker_id,
                    "fence": fence,
                },
            )
            return affected_rows(result) == 1

    async def claim_next(self, worker_id: str, *, lease_seconds: int = 60) -> dict[str, Any] | None:
        now = self.clock.now()
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                text("""WITH candidate AS (
                    SELECT tenant_id,run_id FROM tripmate.agent_run
                    WHERE status='RUNNING' AND (lease_until IS NULL OR lease_until<=:now)
                    ORDER BY created_at,run_id FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE tripmate.agent_run r SET lease_owner=:worker,lease_until=:until,
                    fencing_version=r.fencing_version+1,version=r.version+1,updated_at=:now
                FROM candidate c WHERE r.tenant_id=c.tenant_id AND r.run_id=c.run_id
                RETURNING r.run_id,r.tenant_id,r.user_id,r.trip_id,r.thread_id,r.status,r.version,
                    r.fencing_version,r.checkpoint_ref,r.resume_command"""),
                {"now": now, "worker": worker_id, "until": now + timedelta(seconds=lease_seconds)},
            )
            row = result.mappings().one_or_none()
            return dict(row) if row is not None else None

    async def load_run_input(self, tenant_id: UUID, run_id: UUID) -> dict[str, Any]:
        async with self.sessions() as session:
            result = await session.execute(
                text("""SELECT r.run_id,r.tenant_id,r.user_id,r.trip_id,r.thread_id,r.status,
                    r.version,r.fencing_version,cm.conversation_id,cm.message_id,cm.content_redacted,
                    cm.content_hash FROM tripmate.agent_run r JOIN tripmate.conversation_message cm
                      ON cm.tenant_id=r.tenant_id AND cm.run_id=r.run_id
                    WHERE r.tenant_id=:tenant AND r.run_id=:run ORDER BY cm.sequence_no DESC LIMIT 1"""),
                {"tenant": tenant_id, "run": run_id},
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise DomainError("NOT_FOUND", "Run input was not found", 404)
            return dict(row)

    async def finish_run(
        self, tenant_id: UUID, run_id: UUID, worker_id: str, fence: int, result_json: dict[str, Any]
    ) -> bool:
        async with self.sessions() as session, session.begin():
            updated = await session.execute(
                text("""UPDATE tripmate.agent_run SET status='COMPLETED',result_json=CAST(:result AS jsonb),
                    checkpoint_ref=:checkpoint,resume_command=NULL,lease_owner=NULL,lease_until=NULL,
                    version=version+1,updated_at=:now WHERE tenant_id=:tenant AND run_id=:run
                    AND status='RUNNING' AND lease_owner=:worker AND fencing_version=:fence"""),
                {
                    "result": json.dumps(result_json),
                    "checkpoint": str(run_id),
                    "now": self.clock.now(),
                    "tenant": tenant_id,
                    "run": run_id,
                    "worker": worker_id,
                    "fence": fence,
                },
            )
            return affected_rows(updated) == 1

    async def release_waiting_run(
        self, tenant_id: UUID, run_id: UUID, worker_id: str, fence: int, result_json: dict[str, Any]
    ) -> bool:
        async with self.sessions() as session, session.begin():
            updated = await session.execute(
                text("""UPDATE tripmate.agent_run SET result_json=CAST(:result AS jsonb),
                    checkpoint_ref=:checkpoint,resume_command=NULL,lease_owner=NULL,lease_until=NULL,
                    version=version+1,updated_at=:now WHERE tenant_id=:tenant AND run_id=:run
                    AND status='WAITING_CONFIRMATION' AND lease_owner=:worker AND fencing_version=:fence"""),
                {
                    "result": json.dumps(result_json),
                    "checkpoint": str(run_id),
                    "now": self.clock.now(),
                    "tenant": tenant_id,
                    "run": run_id,
                    "worker": worker_id,
                    "fence": fence,
                },
            )
            return affected_rows(updated) == 1

    async def fail_run(
        self, tenant_id: UUID, run_id: UUID, worker_id: str, fence: int, code: str
    ) -> bool:
        async with self.sessions() as session, session.begin():
            updated = await session.execute(
                text("""UPDATE tripmate.agent_run SET status='FAILED',last_error_code=:code,
                    resume_command=NULL,lease_owner=NULL,lease_until=NULL,version=version+1,updated_at=:now
                    WHERE tenant_id=:tenant AND run_id=:run AND status='RUNNING'
                      AND lease_owner=:worker AND fencing_version=:fence"""),
                {
                    "code": code[:80],
                    "now": self.clock.now(),
                    "tenant": tenant_id,
                    "run": run_id,
                    "worker": worker_id,
                    "fence": fence,
                },
            )
            return affected_rows(updated) == 1

    async def get_run(self, ctx: RequestContext, run_id: UUID) -> dict[str, Any]:
        if ctx.user_id is None:
            raise DomainError("UNAUTHORIZED", "Authenticated user is required", 401)
        async with self.sessions() as session:
            result = await session.execute(
                text("""SELECT run_id,thread_id,trip_id,status,checkpoint_ref,awaiting_action_id,
                    result_json,last_error_code,version,created_at,updated_at
                    FROM tripmate.agent_run WHERE tenant_id=:tenant AND user_id=:user AND run_id=:run"""),
                {"tenant": ctx.tenant_id, "user": ctx.user_id, "run": run_id},
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise DomainError("NOT_FOUND", "Run was not found", 404)
            return dict(row)

    async def release_run(
        self, tenant_id: UUID, run_id: UUID, worker_id: str, fencing_version: int
    ) -> bool:
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                text("""UPDATE tripmate.agent_run SET lease_owner=NULL,lease_until=NULL,
                    version=version+1,updated_at=:now WHERE tenant_id=:tenant AND run_id=:run
                    AND lease_owner=:worker AND fencing_version=:fence"""),
                {
                    "now": self.clock.now(),
                    "tenant": tenant_id,
                    "run": run_id,
                    "worker": worker_id,
                    "fence": fencing_version,
                },
            )
            return affected_rows(result) == 1

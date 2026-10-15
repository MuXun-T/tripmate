import asyncio
import hashlib
import json
import logging
import random
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.errors import DomainError
from app.domain.idempotency import canonical_json
from app.events.schema import EventEnvelope
from app.inventory.keys import KeyCodec
from app.persistence.postgres.database import affected_rows
from redis.asyncio import Redis

logger = logging.getLogger(__name__)
EventHandler = Callable[[AsyncSession, EventEnvelope], Awaitable[None]]


def event_domain(event_type: str) -> str:
    if event_type.startswith("reservation."):
        return "reservations"
    if event_type.startswith("inventory."):
        return "inventory"
    if event_type.startswith("knowledge."):
        return "knowledge"
    if event_type.startswith("agent."):
        return "runs"
    if event_type.startswith("reminder."):
        return "reminders"
    if event_type.startswith("trip."):
        return "trips"
    if event_type.startswith("preference."):
        return "preferences"
    raise ValueError(f"No stream is registered for event type {event_type}")


class OutboxPublisher:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis: Redis,
        settings: Settings,
        *,
        replay_window: timedelta = timedelta(days=7),
    ) -> None:
        self.sessions = sessions
        self.redis = redis
        self.settings = settings
        self.keys = KeyCodec(settings.redis_prefix, settings.app_env)
        self.replay_window = replay_window
        if settings.stream_auto_trim:
            raise ValueError(
                "Stream trimming is disabled until all consumer safe watermarks are verified"
            )

    async def publish_batch(self, limit: int | None = None) -> int:
        published = 0
        batch_size = limit or self.settings.stream_batch_size
        async with self.sessions() as session:
            async with session.begin():
                rows = await session.execute(
                    text("""SELECT * FROM tripmate.outbox_event
                        WHERE status='PENDING' AND next_attempt_at<=now()
                        ORDER BY created_at,event_id FOR UPDATE SKIP LOCKED LIMIT :limit"""),
                    {"limit": batch_size},
                )
                for row in rows.mappings():
                    envelope_data = row["payload"]
                    if isinstance(envelope_data, str):
                        envelope_data = json.loads(envelope_data)
                    try:
                        envelope = EventEnvelope.model_validate(envelope_data)
                        stream = self.keys.stream(
                            envelope.tenant_id, event_domain(envelope.event_type)
                        )
                        stream_id = await self.redis.xadd(
                            stream, {"event": canonical_json(envelope.model_dump(mode="json"))}
                        )
                    except Exception as exc:
                        attempts = int(row["attempts"]) + 1
                        status = (
                            "FAILED" if attempts >= self.settings.max_event_attempts else "PENDING"
                        )
                        delay = min(300, 2 ** min(attempts, 8)) + random.random()
                        await session.execute(
                            text("""UPDATE tripmate.outbox_event SET status=:status,attempts=:attempts,
                                next_attempt_at=now()+(:delay * interval '1 second')
                                WHERE event_id=:event"""),
                            {
                                "status": status,
                                "attempts": attempts,
                                "delay": delay,
                                "event": row["event_id"],
                            },
                        )
                        logger.warning(
                            "outbox_publish_failed",
                            extra={
                                "event_id": str(row["event_id"]),
                                "error_type": type(exc).__name__,
                                "attempt": attempts,
                                "terminal": status == "FAILED",
                            },
                        )
                        continue
                    await session.execute(
                        text("""UPDATE tripmate.outbox_event SET status='PUBLISHED',attempts=attempts+1,
                            replay_until=:replay_until,published_stream=:stream,published_stream_id=:stream_id,
                            published_at=now() WHERE event_id=:event"""),
                        {
                            "replay_until": datetime.now(UTC) + self.replay_window,
                            "stream": stream,
                            "stream_id": stream_id.decode()
                            if isinstance(stream_id, bytes)
                            else str(stream_id),
                            "event": row["event_id"],
                        },
                    )
                    published += 1
        return published

    async def replay_stream(
        self, tenant_id: UUID, domain: str, *, since: datetime | None = None
    ) -> int:
        """Rebuild a lost Stream from the retained outbox window; inbox deduplicates repeated event IDs."""
        start = since or datetime.now(UTC) - self.replay_window
        count = 0
        async with self.sessions() as session:
            rows = await session.execute(
                text("""SELECT payload FROM tripmate.outbox_event WHERE tenant_id=:tenant
                    AND status='PUBLISHED' AND published_stream=:stream AND created_at>=:since
                    AND (replay_until IS NULL OR replay_until>=now())
                    ORDER BY created_at,event_id"""),
                {
                    "tenant": tenant_id,
                    "stream": self.keys.stream(tenant_id, domain),
                    "since": start,
                },
            )
            for row in rows.mappings():
                payload = (
                    row["payload"]
                    if isinstance(row["payload"], dict)
                    else json.loads(row["payload"])
                )
                envelope = EventEnvelope.model_validate(payload)
                await self.redis.xadd(
                    self.keys.stream(tenant_id, domain),
                    {"event": canonical_json(envelope.model_dump(mode="json"))},
                )
                count += 1
        return count


class InboxProcessor:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis: Redis,
        settings: Settings,
        *,
        consumer: str,
        group: str,
        handler: EventHandler,
    ) -> None:
        self.sessions = sessions
        self.redis = redis
        self.settings = settings
        self.consumer = consumer
        self.group = group
        self.handler = handler
        self.keys = KeyCodec(settings.redis_prefix, settings.app_env)

    async def ensure_group(self, tenant_id: UUID, domain: str) -> None:
        stream = self.keys.stream(tenant_id, domain)
        try:
            await self.redis.xgroup_create(stream, self.group, id="0-0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def process(self, stream: str, message_id: str | bytes, fields: dict[Any, Any]) -> bool:
        event_raw = fields.get("event", fields.get(b"event"))
        try:
            raw = event_raw.decode() if isinstance(event_raw, bytes) else str(event_raw)
            envelope = EventEnvelope.model_validate_json(raw)
        except Exception:
            logger.error(
                "invalid_stream_envelope", extra={"stream": stream, "message_id": str(message_id)}
            )
            return False
        if envelope.tenant_id not in self._tenant_from_stream(stream):
            return False
        claim = await self._claim(envelope, raw)
        if claim is None:
            async with self.sessions() as session:
                status = await session.scalar(
                    text(
                        "SELECT status FROM tripmate.inbox_event WHERE event_id=:event AND consumer=:consumer"
                    ),
                    {"event": envelope.event_id, "consumer": self.consumer},
                )
            if status == "PROCESSED" or status == "DLQ":
                await self.redis.xack(stream, self.group, message_id)
                return True
            return False
        try:
            async with self.sessions() as session:
                async with session.begin():
                    inbox_result = await session.execute(
                        text("""SELECT status,lease_owner,lease_until,claim_token,fencing_version
                            FROM tripmate.inbox_event WHERE event_id=:event AND consumer=:consumer FOR UPDATE"""),
                        {"event": envelope.event_id, "consumer": self.consumer},
                    )
                    row = inbox_result.mappings().one_or_none()
                    if (
                        row is None
                        or row["status"] != "PROCESSING"
                        or row["lease_owner"] != self.consumer
                        or row["claim_token"] != claim["claim_token"]
                        or int(row["fencing_version"]) != claim["fencing_version"]
                        or row["lease_until"] <= datetime.now(UTC)
                    ):
                        raise DomainError(
                            "STALE_LEASE", "Inbox lease was fenced by a newer consumer", 409
                        )
                    await self.handler(session, envelope)
                    completed = await session.execute(
                        text("""UPDATE tripmate.inbox_event SET status='PROCESSED',processed_at=now(),
                            lease_owner=NULL,lease_until=NULL,claim_token=NULL,updated_at=now()
                            WHERE event_id=:event AND consumer=:consumer AND status='PROCESSING'
                              AND lease_owner=:owner AND claim_token=:claim AND fencing_version=:fence"""),
                        {
                            "event": envelope.event_id,
                            "consumer": self.consumer,
                            "owner": self.consumer,
                            "claim": claim["claim_token"],
                            "fence": claim["fencing_version"],
                        },
                    )
                    if affected_rows(completed) != 1:
                        raise DomainError("STALE_LEASE", "Inbox completion was fenced", 409)
            await self.redis.xack(stream, self.group, message_id)
            return True
        except Exception as exc:
            terminal = await self._record_failure(
                envelope, stream, str(message_id), raw, claim, exc
            )
            if terminal:
                await self.redis.xack(stream, self.group, message_id)
            return False

    async def _claim(self, envelope: EventEnvelope, raw: str) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        claim_token = uuid4()
        payload_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                text("""INSERT INTO tripmate.inbox_event
                    (event_id,consumer,tenant_id,status,attempt_count,lease_owner,lease_until,claim_token,
                     fencing_version,payload_hash)
                    VALUES(:event,:consumer,:tenant,'PROCESSING',1,:owner,:until,:claim,1,:hash)
                    ON CONFLICT(event_id,consumer) DO UPDATE SET status='PROCESSING',
                      attempt_count=tripmate.inbox_event.attempt_count+1,lease_owner=EXCLUDED.lease_owner,
                      lease_until=EXCLUDED.lease_until,claim_token=EXCLUDED.claim_token,
                      fencing_version=tripmate.inbox_event.fencing_version+1,
                      updated_at=now(),error_code=NULL
                    WHERE tripmate.inbox_event.status NOT IN ('PROCESSED','DLQ')
                      AND (tripmate.inbox_event.status<>'PROCESSING' OR tripmate.inbox_event.lease_until<=:now)
                    RETURNING fencing_version,attempt_count"""),
                {
                    "event": envelope.event_id,
                    "consumer": self.consumer,
                    "tenant": envelope.tenant_id,
                    "owner": self.consumer,
                    "until": now + timedelta(seconds=30),
                    "claim": claim_token,
                    "hash": payload_hash,
                    "now": now,
                },
            )
            row = result.mappings().one_or_none()
            if row is None:
                return None
            return {
                "claim_token": claim_token,
                "fencing_version": int(row["fencing_version"]),
                "attempt_count": int(row["attempt_count"]),
            }

    async def _record_failure(
        self,
        envelope: EventEnvelope,
        stream: str,
        message_id: str,
        raw: str,
        claim: dict[str, Any],
        exc: Exception,
    ) -> bool:
        terminal = False
        error_code = exc.code if isinstance(exc, DomainError) else "CONSUMER_ERROR"
        stack_hash = hashlib.sha256(
            (type(exc).__name__ + "\n" + traceback.format_exc()).encode("utf-8")
        ).hexdigest()
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                text("""SELECT attempt_count,status FROM tripmate.inbox_event WHERE event_id=:event
                    AND consumer=:consumer AND status='PROCESSING' AND lease_owner=:owner
                    AND claim_token=:claim AND fencing_version=:fence FOR UPDATE"""),
                {
                    "event": envelope.event_id,
                    "consumer": self.consumer,
                    "owner": self.consumer,
                    "claim": claim["claim_token"],
                    "fence": claim["fencing_version"],
                },
            )
            row = result.mappings().one_or_none()
            if row is None:
                return False
            attempts = int(row["attempt_count"])
            terminal = attempts >= self.settings.max_event_attempts
            if terminal:
                await session.execute(
                    text("""UPDATE tripmate.inbox_event SET status='DLQ',lease_owner=NULL,lease_until=NULL,
                        claim_token=NULL,error_code=:error,updated_at=now() WHERE event_id=:event AND consumer=:consumer"""),
                    {"error": error_code, "event": envelope.event_id, "consumer": self.consumer},
                )
                await session.execute(
                    text("""INSERT INTO tripmate.dead_letter_event
                        (tenant_id,event_id,consumer,stream_name,group_name,stream_message_id,event_payload,
                         payload_hash,error_code,stack_hash,attempt_count,status)
                        VALUES(:tenant,:event,:consumer,:stream,:group_name,:message_id,CAST(:payload AS jsonb),
                         :payload_hash,:error,:stack_hash,:attempts,'OPEN')
                        ON CONFLICT (tenant_id,event_id,consumer) DO NOTHING"""),
                    {
                        "tenant": envelope.tenant_id,
                        "event": envelope.event_id,
                        "consumer": self.consumer,
                        "stream": stream,
                        "group_name": self.group,
                        "message_id": message_id,
                        "payload": raw,
                        "payload_hash": hashlib.sha256(raw.encode()).hexdigest(),
                        "error": error_code,
                        "stack_hash": stack_hash,
                        "attempts": attempts,
                    },
                )
            else:
                await session.execute(
                    text("""UPDATE tripmate.inbox_event SET status='RECEIVED',lease_owner=NULL,lease_until=NULL,
                        claim_token=NULL,error_code=:error,updated_at=now() WHERE event_id=:event AND consumer=:consumer"""),
                    {"error": error_code, "event": envelope.event_id, "consumer": self.consumer},
                )
        logger.warning(
            "stream_event_failed",
            extra={
                "event_id": str(envelope.event_id),
                "consumer": self.consumer,
                "attempt": claim["attempt_count"],
                "error_code": error_code,
                "terminal": terminal,
            },
        )
        return terminal

    async def consume_once(self, tenant_id: UUID, domain: str, *, block_ms: int = 1000) -> int:
        await self.ensure_group(tenant_id, domain)
        stream = self.keys.stream(tenant_id, domain)
        messages = await self.redis.xreadgroup(
            self.group,
            self.consumer,
            {stream: ">"},
            count=self.settings.stream_batch_size,
            block=block_ms,
        )
        processed = 0
        for _stream, entries in messages:
            for message_id, fields in entries:
                processed += bool(await self.process(stream, message_id, fields))
        return processed

    async def claim_pending(self, tenant_id: UUID, domain: str, *, limit: int = 100) -> int:
        await self.ensure_group(tenant_id, domain)
        stream = self.keys.stream(tenant_id, domain)
        cursor: str | bytes = "0-0"
        claimed_count = 0
        while True:
            claimed = await self.redis.xautoclaim(
                stream,
                self.group,
                self.consumer,
                min_idle_time=self.settings.pending_claim_idle_ms,
                start_id=cursor,
                count=limit,
            )
            cursor, entries = claimed[0], claimed[1]
            for message_id, fields in entries:
                claimed_count += bool(await self.process(stream, message_id, fields))
            cursor_text = cursor.decode() if isinstance(cursor, bytes) else cursor
            if cursor_text == "0-0" or not entries:
                break
        return claimed_count

    @staticmethod
    def _tenant_from_stream(stream: str) -> set[UUID]:
        start = stream.find("{")
        end = stream.find("}", start + 1)
        if start < 0 or end < 0:
            return set()
        try:
            return {UUID(stream[start + 1 : end])}
        except ValueError:
            return set()


async def outbox_loop(publisher: OutboxPublisher, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            count = await publisher.publish_batch()
        except Exception:
            logger.exception("outbox_loop_failed")
            count = 0
        if count == 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.25)
            except TimeoutError:
                pass

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.context import RequestContext
from app.domain.errors import DomainError
from app.domain.ports import Clock, IdGenerator
from app.events.schema import EventEnvelope
from app.inventory.keys import KeyCodec
from app.persistence.postgres.database import affected_rows
from redis.asyncio import Redis


class ReminderService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis: Redis,
        settings: Settings,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self.sessions = sessions
        self.redis = redis
        self.settings = settings
        self.clock = clock
        self.ids = ids
        self.keys = KeyCodec(settings.redis_prefix, settings.app_env)

    async def enqueue_event(self, event: EventEnvelope) -> None:
        reminder_id = UUID(str(event.payload["reminder_id"]))
        scheduled_at = datetime.fromisoformat(str(event.payload["scheduled_at"]))
        await self.redis.zadd(
            self.keys.reminders(event.tenant_id),
            {str(reminder_id): scheduled_at.timestamp() * 1000},
        )

    async def rebuild_due(self, tenant_id: UUID) -> int:
        async with self.sessions() as session:
            rows = await session.execute(
                text("""SELECT reminder_id,scheduled_at FROM tripmate.reminder
                    WHERE tenant_id=:tenant AND scheduled_at<=now()
                      AND (status IN ('SCHEDULED','RETRYING')
                           OR (status='SENDING' AND lease_until<=now()))"""),
                {"tenant": tenant_id},
            )
            due = {
                str(row["reminder_id"]): row["scheduled_at"].timestamp() * 1000
                for row in rows.mappings()
            }
        if due:
            await self.redis.zadd(self.keys.reminders(tenant_id), due)
        return len(due)

    async def dispatch_due(self, worker_id: str, *, limit: int = 100) -> int:
        now = self.clock.now()
        async with self.sessions() as session:
            rows = await session.execute(
                text("""SELECT DISTINCT tenant_id FROM tripmate.reminder
                    WHERE (status IN ('SCHEDULED','RETRYING') OR
                           (status='SENDING' AND lease_until<=:now)) AND scheduled_at<=:now
                    ORDER BY tenant_id LIMIT :limit"""),
                {"now": now, "limit": limit},
            )
            tenants = [row["tenant_id"] for row in rows.mappings()]
        delivered = 0
        for tenant_id in tenants:
            key = self.keys.reminders(tenant_id)
            if not await self.redis.exists(key):
                await self.rebuild_due(tenant_id)
            members = await self.redis.zrangebyscore(
                key, "-inf", now.timestamp() * 1000, start=0, num=limit
            )
            if not members:
                await self.rebuild_due(tenant_id)
                members = await self.redis.zrangebyscore(
                    key, "-inf", now.timestamp() * 1000, start=0, num=limit
                )
            for member in members:
                try:
                    reminder_id = UUID(str(member))
                except ValueError:
                    await self.redis.zrem(key, member)
                    continue
                try:
                    if await self._deliver(tenant_id, reminder_id, worker_id):
                        delivered += 1
                except Exception as exc:
                    await self._mark_failure(tenant_id, reminder_id, worker_id, exc)
                await self.redis.zrem(key, member)
                retry_at = await self._retry_at(tenant_id, reminder_id)
                if retry_at is not None:
                    await self.redis.zadd(key, {member: retry_at.timestamp() * 1000})
        return delivered

    async def list_notifications(self, ctx: RequestContext, *, limit: int = 50) -> list[dict[str, Any]]:
        if ctx.user_id is None:
            raise DomainError("UNAUTHORIZED", "Authenticated user is required", 401)
        async with self.sessions() as session:
            rows = await session.execute(
                text("""SELECT notification_id,reminder_id,promotion_id,title,body,status,created_at,read_at
                    FROM tripmate.notification_inbox WHERE tenant_id=:tenant AND user_id=:user
                    ORDER BY created_at DESC,notification_id LIMIT :limit"""),
                {"tenant": ctx.tenant_id, "user": ctx.user_id, "limit": min(max(limit, 1), 100)},
            )
            return [dict(row) for row in rows.mappings()]

    async def mark_read(self, ctx: RequestContext, notification_id: UUID) -> dict[str, Any]:
        if ctx.user_id is None:
            raise DomainError("UNAUTHORIZED", "Authenticated user is required", 401)
        now = self.clock.now()
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                text("""UPDATE tripmate.notification_inbox SET status='READ',read_at=COALESCE(read_at,:now)
                    WHERE tenant_id=:tenant AND user_id=:user AND notification_id=:notification
                    RETURNING notification_id,status,read_at"""),
                {"now": now, "tenant": ctx.tenant_id, "user": ctx.user_id, "notification": notification_id},
            )
            row = result.mappings().one_or_none()
        if row is None:
            raise DomainError("NOT_FOUND", "Notification was not found", 404)
        return dict(row)

    async def _deliver(self, tenant_id: UUID, reminder_id: UUID, worker_id: str) -> bool:
        now = self.clock.now()
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                text("""SELECT * FROM tripmate.reminder
                    WHERE tenant_id=:tenant AND reminder_id=:reminder FOR UPDATE"""),
                {"tenant": tenant_id, "reminder": reminder_id},
            )
            reminder = result.mappings().one_or_none()
            if reminder is None or reminder["status"] == "SENT":
                return False
            if reminder["status"] not in {"SCHEDULED", "RETRYING", "SENDING"} or reminder["scheduled_at"] > now:
                return False
            if reminder["status"] == "SENDING" and reminder["lease_until"] is not None and reminder["lease_until"] > now:
                return False
            fence = int(reminder["fencing_version"]) + 1
            await session.execute(
                text("""UPDATE tripmate.reminder SET status='SENDING',lease_owner=:owner,
                    lease_until=:until,fencing_version=:fence,attempt_count=attempt_count+1,
                    version=version+1 WHERE tenant_id=:tenant AND reminder_id=:reminder"""),
                {"owner": worker_id, "until": now + timedelta(seconds=30), "fence": fence,
                 "tenant": tenant_id, "reminder": reminder_id},
            )
            title, body = _notification_text(reminder["reminder_type"], reminder["scheduled_at"])
            await session.execute(
                text("""INSERT INTO tripmate.notification_inbox
                    (notification_id,tenant_id,user_id,reminder_id,title,body)
                    VALUES(:notification,:tenant,:user,:reminder,:title,:body)
                    ON CONFLICT (notification_id) DO NOTHING"""),
                {"notification": reminder["notification_id"], "tenant": tenant_id,
                 "user": reminder["user_id"], "reminder": reminder_id, "title": title, "body": body},
            )
            owned = await session.scalar(
                text("""SELECT EXISTS(SELECT 1 FROM tripmate.notification_inbox
                    WHERE notification_id=:notification AND tenant_id=:tenant AND user_id=:user
                      AND reminder_id=:reminder)"""),
                {"notification": reminder["notification_id"], "tenant": tenant_id,
                 "user": reminder["user_id"], "reminder": reminder_id},
            )
            if not owned:
                raise DomainError("RECONCILIATION_REQUIRED", "Notification id belongs to another reminder", 503)
            updated = await session.execute(
                text("""UPDATE tripmate.reminder SET status='SENT',lease_owner=NULL,lease_until=NULL,
                    last_error_code=NULL,version=version+1,updated_at=:now
                    WHERE tenant_id=:tenant AND reminder_id=:reminder AND status='SENDING'
                      AND lease_owner=:owner AND fencing_version=:fence"""),
                {"now": now, "tenant": tenant_id, "reminder": reminder_id,
                 "owner": worker_id, "fence": fence},
            )
            if affected_rows(updated) != 1:
                raise DomainError("STALE_LEASE", "Reminder delivery was fenced", 409, True)
            envelope = EventEnvelope(
                event_id=self.ids.new(), event_type="reminder.sent.v1", event_version=1,
                occurred_at=now, tenant_id=tenant_id, aggregate_type="reminder",
                aggregate_id=reminder_id, aggregate_version=int(reminder["version"]) + 2,
                request_id=f"reminder:{reminder_id}", trace_id=f"reminder:{reminder_id}",
                producer="tripmate-reminder-worker",
                payload={"reminder_id": str(reminder_id), "notification_id": str(reminder["notification_id"])},
            )
            await session.execute(
                text("""INSERT INTO tripmate.outbox_event
                    (event_id,tenant_id,aggregate_type,aggregate_id,aggregate_version,event_type,event_version,payload,status)
                    VALUES(:event,:tenant,'reminder',:reminder,:version,:type,1,CAST(:payload AS jsonb),'PENDING')"""),
                {"event": envelope.event_id, "tenant": tenant_id, "reminder": reminder_id,
                 "version": envelope.aggregate_version, "type": envelope.event_type,
                 "payload": json.dumps(envelope.model_dump(mode="json"))},
            )
        return True

    async def _mark_failure(
        self, tenant_id: UUID, reminder_id: UUID, worker_id: str, error: Exception
    ) -> None:
        now = self.clock.now()
        async with self.sessions() as session, session.begin():
            row = (await session.execute(
                text("""SELECT attempt_count,status,lease_owner FROM tripmate.reminder
                    WHERE tenant_id=:tenant AND reminder_id=:reminder FOR UPDATE"""),
                {"tenant": tenant_id, "reminder": reminder_id},
            )).mappings().one_or_none()
            if row is None or row["status"] == "SENT":
                return
            if row["status"] == "SENDING" and row["lease_owner"] != worker_id:
                return
            attempts = int(row["attempt_count"]) + 1
            terminal = attempts >= self.settings.max_event_attempts
            next_at = now + timedelta(seconds=min(300, 2 ** min(attempts, 8)))
            await session.execute(
                text("""UPDATE tripmate.reminder SET status=:status,attempt_count=:attempts,
                    scheduled_at=:scheduled,lease_owner=NULL,lease_until=NULL,last_error_code=:error,
                    version=version+1,updated_at=:now
                    WHERE tenant_id=:tenant AND reminder_id=:reminder"""),
                {"status": "FAILED" if terminal else "RETRYING", "attempts": attempts,
                 "scheduled": next_at, "error": type(error).__name__[:80], "now": now,
                 "tenant": tenant_id, "reminder": reminder_id},
            )

    async def _retry_at(self, tenant_id: UUID, reminder_id: UUID) -> datetime | None:
        async with self.sessions() as session:
            row = (await session.execute(
                text("""SELECT status,scheduled_at FROM tripmate.reminder
                    WHERE tenant_id=:tenant AND reminder_id=:reminder"""),
                {"tenant": tenant_id, "reminder": reminder_id},
            )).mappings().one_or_none()
        if row is None or row["status"] != "RETRYING":
            return None
        return cast(datetime, row["scheduled_at"])


def _notification_text(reminder_type: str, scheduled_at: datetime) -> tuple[str, str]:
    label = {
        "departure": "出行提醒",
        "reservation": "预约提醒",
        "weather_check": "天气提醒",
    }[reminder_type]
    return label, f"提醒时间：{scheduled_at.astimezone(UTC).isoformat()}"

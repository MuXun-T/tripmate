import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text

from app.config import Settings
from app.domain.context import RequestContext
from app.domain.ports import SystemClock
from app.persistence.postgres.migrate import apply_migrations
from app.runtime import Runtime


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def runtime() -> AsyncIterator[Runtime]:
    database_url = os.getenv("TRIPMATE_TEST_DATABASE_URL")
    redis_url = os.getenv("TRIPMATE_TEST_REDIS_URL")
    if not database_url or not redis_url:
        pytest.skip("TRIPMATE_TEST_DATABASE_URL and TRIPMATE_TEST_REDIS_URL are required")
    settings = Settings(
        app_env="test",
        database_url=database_url,
        redis_url=redis_url,
        auth_signing_secret=SecretStr("tripmate-integration-secret-0123456789"),
        db_statement_timeout_ms=30000,
        pending_claim_idle_ms=1000,
        stream_auto_trim=False,
    )
    await apply_migrations(settings)
    runtime = await Runtime.create(settings)
    try:
        yield runtime
    finally:
        await runtime.close()


@dataclass(frozen=True, slots=True)
class IntegrationScope:
    tenant_id: UUID
    user_id: UUID
    attraction_id: UUID
    trip_id: UUID
    slot_id: UUID
    run_id: UUID
    thread_id: str
    start_at: datetime
    runtime: Runtime

    def user_context(self, *, request_id: str = "integration-user") -> RequestContext:
        now = SystemClock().now()
        return RequestContext(
            request_id=request_id,
            trace_id=f"trace-{request_id}",
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            roles=("user",),
            deadline=now + timedelta(minutes=2),
            source="http",
            trip_id=self.trip_id,
        )

    def agent_context(self) -> RequestContext:
        user = self.user_context(request_id="integration-agent")
        return replace(
            user,
            source="agent",
            run_id=self.run_id,
            thread_id=self.thread_id,
            lease_owner="integration-fixture",
            fencing_version=1,
        )


@pytest_asyncio.fixture(loop_scope="module")
async def tenant_scope(runtime: Runtime) -> AsyncIterator[IntegrationScope]:
    tenant_id, user_id, attraction_id, trip_id, slot_id, run_id = (uuid4() for _ in range(6))
    thread_id = f"tenant:{tenant_id}:user:{user_id}:conversation:{uuid4()}"
    now = SystemClock().now()
    start_at = now + timedelta(days=10)
    local_day = start_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    policy = {
        "policy_id": "integration-fixed-policy",
        "policy_version": "test-v1",
        "timezone": "Asia/Shanghai",
        "source": "test",
        "rules": [
            {
                "cutoff_at": (start_at - timedelta(days=2)).isoformat(),
                "refund_percent": "100.00",
                "reason_code": "INTEGRATION_CUTOFF",
            }
        ],
    }
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("INSERT INTO tripmate.tenant(tenant_id,name,status) VALUES(:id,:name,'ACTIVE')"),
            {"id": tenant_id, "name": f"integration-{tenant_id}"},
        )
        await session.execute(
            text("""INSERT INTO tripmate.user_profile(user_id,tenant_id,display_name,status)
                VALUES(:user,:tenant,'Integration User','ACTIVE')"""),
            {"user": user_id, "tenant": tenant_id},
        )
        await session.execute(
            text("""INSERT INTO tripmate.attraction
                (attraction_id,tenant_id,external_source,external_id,city,name,category,source_version,status)
                VALUES(:id,:tenant,'integration',:external,'test-city','Test Attraction','scenic','test-v1','ACTIVE')"""),
            {"id": attraction_id, "tenant": tenant_id, "external": str(attraction_id)},
        )
        await session.execute(
            text("""INSERT INTO tripmate.trip
                (trip_id,tenant_id,owner_user_id,destination,start_date,end_date,timezone,party_size,
                 budget_limit,currency,status)
                VALUES(:trip,:tenant,:user,'Test City',:start,:end,'Asia/Shanghai',1,100.00,'CNY','PLANNING')"""),
            {
                "trip": trip_id,
                "tenant": tenant_id,
                "user": user_id,
                "start": local_day,
                "end": local_day,
            },
        )
        await session.execute(
            text("""INSERT INTO tripmate.activity_slot
                (slot_id,tenant_id,attraction_id,external_id,start_at,end_at,capacity,price,currency,
                 cancellation_policy,policy_version,activity_version,status)
                VALUES(:slot,:tenant,:attraction,:external,:start,:end,2,25.00,'CNY',
                 CAST(:policy AS jsonb),'test-v1',1,'ACTIVE')"""),
            {
                "slot": slot_id,
                "tenant": tenant_id,
                "attraction": attraction_id,
                "external": str(slot_id),
                "start": start_at,
                "end": start_at + timedelta(hours=2),
                "policy": json.dumps(policy),
            },
        )
        await session.execute(
            text("""INSERT INTO tripmate.inventory
                (tenant_id,slot_id,total_quantity,available_quantity,status)
                VALUES(:tenant,:slot,2,2,'ACTIVE')"""),
            {"tenant": tenant_id, "slot": slot_id},
        )
        await session.execute(
            text("""INSERT INTO tripmate.agent_run
                (run_id,tenant_id,user_id,trip_id,thread_id,intent,status,lease_owner,lease_until,
                 fencing_version)
                VALUES(:run,:tenant,:user,:trip,:thread,'UNCLASSIFIED','RUNNING',:owner,:until,1)"""),
            {
                "run": run_id,
                "tenant": tenant_id,
                "user": user_id,
                "trip": trip_id,
                "thread": thread_id,
                "owner": "integration-fixture",
                "until": now + timedelta(minutes=5),
            },
        )
    scope = IntegrationScope(
        tenant_id,
        user_id,
        attraction_id,
        trip_id,
        slot_id,
        run_id,
        thread_id,
        start_at,
        runtime,
    )
    try:
        yield scope
    finally:
        root = runtime.projection.keys._root(tenant_id)
        keys = [key async for key in runtime.redis.scan_iter(match=f"{root}:*")]
        if keys:
            await runtime.redis.delete(*keys)
        async with runtime.sessions() as session, session.begin():
            await session.execute(
                text("UPDATE tripmate.agent_run SET awaiting_action_id=NULL WHERE tenant_id=:tenant"),
                {"tenant": tenant_id},
            )
            for table in (
                "notification_inbox",
                "waitlist_promotion",
                "reminder",
                "evaluation_result",
                "knowledge_evaluation_run",
                "knowledge_active_alias",
                "knowledge_chunk",
                "knowledge_document",
                "knowledge_manifest",
                "audit_log",
                "agent_tool_call",
                "agent_run_event",
                "dead_letter_event",
                "inbox_event",
                "stream_group_watermark",
                "outbox_replay_cursor",
                "idempotency_record",
                "reservation_confirmation",
                "trip_item",
                "reservation",
                "action_confirmation",
                "agent_action",
                "conversation_message",
                "agent_run",
                "outbox_event",
                "private_case_vector",
                "user_preference",
                "evaluation_case",
                "trip_member",
                "inventory",
                "activity_slot",
                "trip",
                "attraction",
                "user_profile",
                "tenant",
            ):
                await session.execute(
                    text(f"DELETE FROM tripmate.{table} WHERE tenant_id=:tenant"),
                    {"tenant": tenant_id},
                )

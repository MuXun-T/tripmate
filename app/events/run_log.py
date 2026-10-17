import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.context import RequestContext
from app.domain.errors import DomainError
from app.domain.ports import IdGenerator
from app.events.schema import RunEvent


async def persist_run_event(
    session: AsyncSession,
    ctx: RequestContext,
    ids: IdGenerator,
    run_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    *,
    occurred_at: datetime,
    event_key: str | None = None,
) -> dict[str, Any]:
    if ctx.user_id is None:
        raise DomainError("UNAUTHORIZED", "Authenticated user is required", 401)
    event = RunEvent(event_type=event_type, payload=payload)
    result = await session.execute(
        text("""INSERT INTO tripmate.agent_run_event
            (event_id,tenant_id,run_id,user_id,event_type,event_key,occurred_at,payload)
            SELECT :event,:tenant,r.run_id,:user,:type,:event_key,:occurred,CAST(:payload AS jsonb)
            FROM tripmate.agent_run r WHERE r.tenant_id=:tenant AND r.user_id=:user AND r.run_id=:run
            ON CONFLICT (tenant_id,run_id,event_key) DO NOTHING
            RETURNING event_sequence,event_id,event_type,occurred_at,payload"""),
        {
            "event": ids.new(),
            "tenant": ctx.tenant_id,
            "run": run_id,
            "user": ctx.user_id,
            "type": event.event_type,
            "event_key": event_key,
            "occurred": occurred_at,
            "payload": json.dumps(event.payload),
        },
    )
    row = result.mappings().one_or_none()
    if row is None and event_key is not None:
        result = await session.execute(
            text("""SELECT event_sequence,event_id,event_type,occurred_at,payload
                FROM tripmate.agent_run_event WHERE tenant_id=:tenant AND user_id=:user
                  AND run_id=:run AND event_key=:event_key"""),
            {
                "tenant": ctx.tenant_id,
                "user": ctx.user_id,
                "run": run_id,
                "event_key": event_key,
            },
        )
        row = result.mappings().one_or_none()
    if row is None:
        raise DomainError("NOT_FOUND", "Run was not found", 404)
    return dict(row)

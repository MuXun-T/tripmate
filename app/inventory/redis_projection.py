from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.inventory.keys import KeyCodec
from app.persistence.postgres.database import affected_rows
from redis.asyncio import Redis

SCRIPT_DIR = Path(__file__).resolve().parents[2] / "redis" / "scripts"


class RedisInventoryProjection:
    """Rebuildable Redis views; all quantities supplied here come from committed PostgreSQL rows."""

    def __init__(
        self,
        client: Redis,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.sessions = sessions
        self.keys = KeyCodec(settings.redis_prefix, settings.app_env)
        self.project_hold_script: Any = None
        self.project_release_script: Any = None
        self.project_snapshot_script: Any = None

    async def setup(self) -> None:
        self.project_hold_script = self.client.register_script(
            (SCRIPT_DIR / "project_hold.lua").read_text(encoding="utf-8")
        )
        self.project_release_script = self.client.register_script(
            (SCRIPT_DIR / "project_release.lua").read_text(encoding="utf-8")
        )
        self.project_snapshot_script = self.client.register_script(
            (SCRIPT_DIR / "project_snapshot.lua").read_text(encoding="utf-8")
        )

    async def project(
        self,
        *,
        tenant_id: UUID,
        slot_id: UUID,
        reservation_id: UUID,
        snapshot: dict[str, Any],
        hold: dict[str, Any] | None,
    ) -> str:
        if self.project_hold_script is None:
            await self.setup()
        stock_key = self.keys.inventory(tenant_id, slot_id)
        hold_key = self.keys.hold(tenant_id, reservation_id)
        expiry_key = self.keys.expiry(tenant_id)
        if not self.keys.same_hash_tag(stock_key, hold_key, expiry_key):
            raise ValueError("inventory Lua keys do not share a Redis hash slot")
        common = [
            int(snapshot["generation"]),
            int(snapshot["version"]),
            int(snapshot["total"]),
            int(snapshot["available"]),
            int(snapshot["held"]),
            int(snapshot["confirmed"]),
            str(snapshot["status"]),
            str(snapshot.get("updated_at", "")),
        ]
        if hold is None:
            result = await self.project_release_script(
                keys=[stock_key, hold_key, expiry_key],
                args=[*common, str(reservation_id)],
            )
        else:
            result = await self.project_hold_script(
                keys=[stock_key, hold_key, expiry_key],
                args=[
                    *common,
                    str(reservation_id),
                    str(hold.get("token_hash", "")),
                    float(hold["expires_at"]),
                    max(1, int(hold.get("ttl_seconds", self.settings.hold_ttl_seconds))),
                ],
            )
        return result.decode() if isinstance(result, bytes) else str(result)

    async def read(self, tenant_id: UUID, slot_id: UUID) -> dict[str, str] | None:
        command = cast(
            Awaitable[dict[Any, Any]], self.client.hgetall(self.keys.inventory(tenant_id, slot_id))
        )
        value = await command
        return {
            key.decode() if isinstance(key, bytes) else str(key): (
                item.decode() if isinstance(item, bytes) else str(item)
            )
            for key, item in value.items()
        } if value else None

    async def rebuild_tenant(self, tenant_id: UUID) -> int:
        if self.sessions is None:
            raise RuntimeError("PostgreSQL sessions are required for projection rebuild")
        count = 0
        async with self.sessions() as session:
            stocks = await session.execute(
                text("""SELECT tenant_id,slot_id,total_quantity,available_quantity,held_quantity,
                    confirmed_quantity,status,reconciliation_generation,version,updated_at
                    FROM tripmate.inventory WHERE tenant_id=:tenant ORDER BY slot_id"""),
                {"tenant": tenant_id},
            )
            for row in stocks.mappings():
                snapshot = {
                    "total": row["total_quantity"],
                    "available": row["available_quantity"],
                    "held": row["held_quantity"],
                    "confirmed": row["confirmed_quantity"],
                    "status": row["status"],
                    "generation": row["reconciliation_generation"],
                    "version": row["version"],
                    "updated_at": row["updated_at"].isoformat(),
                }
                stock_key = self.keys.inventory(tenant_id, row["slot_id"])
                await self.project_snapshot_script(
                    keys=[stock_key],
                    args=[
                        snapshot["generation"],
                        snapshot["version"],
                        snapshot["total"],
                        snapshot["available"],
                        snapshot["held"],
                        snapshot["confirmed"],
                        snapshot["status"],
                        snapshot["updated_at"],
                    ],
                )
                holds = await session.execute(
                    text("""SELECT reservation_id,hold_token_hash,hold_expires_at
                        FROM tripmate.reservation WHERE tenant_id=:tenant AND slot_id=:slot AND status='HELD'
                        ORDER BY reservation_id"""),
                    {"tenant": tenant_id, "slot": row["slot_id"]},
                )
                for held in holds.mappings():
                    expiry = held["hold_expires_at"].timestamp()
                    await self.project_hold_script(
                        keys=[
                            stock_key,
                            self.keys.hold(tenant_id, held["reservation_id"]),
                            self.keys.expiry(tenant_id),
                        ],
                        args=[
                            snapshot["generation"],
                            snapshot["version"],
                            snapshot["total"],
                            snapshot["available"],
                            snapshot["held"],
                            snapshot["confirmed"],
                            snapshot["status"],
                            snapshot["updated_at"],
                            str(held["reservation_id"]),
                            str(held["hold_token_hash"]),
                            expiry,
                            max(1, int(expiry - row["updated_at"].timestamp())),
                        ],
                    )
                count += 1
        return count

    async def rebuild_all(self) -> int:
        if self.sessions is None:
            raise RuntimeError("PostgreSQL sessions are required for projection rebuild")
        async with self.sessions() as session:
            rows = await session.execute(
                text("SELECT DISTINCT tenant_id FROM tripmate.inventory ORDER BY tenant_id")
            )
            tenants = [row[0] for row in rows]
        rebuilt = 0
        for tenant_id in tenants:
            rebuilt += await self.rebuild_tenant(tenant_id)
        return rebuilt

    async def reconcile_pending(
        self, worker_id: str, *, tenant_id: UUID | None = None, limit: int = 100
    ) -> int:
        """Rebuild marked slots from PostgreSQL and clear each marker after Redis succeeds."""
        if self.sessions is None:
            raise RuntimeError("PostgreSQL sessions are required for reconciliation")
        now = datetime.now(UTC)
        async with self.sessions() as session, session.begin():
            rows = await session.execute(
                text("""SELECT reconciliation_id,tenant_id,slot_id FROM tripmate.inventory_reconciliation
                    WHERE ((status IN ('PENDING','FAILED') AND next_attempt_at<=:now)
                       OR (status='PROCESSING' AND lease_until<=:now))
                      AND (:tenant IS NULL OR tenant_id=:tenant)
                    ORDER BY next_attempt_at,reconciliation_id FOR UPDATE SKIP LOCKED LIMIT :limit"""),
                {"now": now, "tenant": tenant_id, "limit": min(max(limit, 1), 500)},
            )
            tasks = [dict(row) for row in rows.mappings()]
            for task in tasks:
                await session.execute(
                    text("""UPDATE tripmate.inventory_reconciliation SET status='PROCESSING',
                        lease_owner=:worker,lease_until=:until,attempt_count=attempt_count+1,updated_at=:now
                        WHERE tenant_id=:tenant AND reconciliation_id=:id"""),
                    {"worker": worker_id, "until": now + timedelta(seconds=30), "now": now,
                     "tenant": task["tenant_id"], "id": task["reconciliation_id"]},
                )
        resolved = 0
        for task in tasks:
            try:
                async with self.sessions() as session, session.begin():
                    updated = await session.execute(
                        text("""UPDATE tripmate.inventory SET
                            reconciliation_generation=reconciliation_generation+1,version=version+1,updated_at=:now
                            WHERE tenant_id=:tenant AND slot_id=:slot AND status='RECONCILING'"""),
                        {"now": datetime.now(UTC), "tenant": task["tenant_id"], "slot": task["slot_id"]},
                    )
                    if affected_rows(updated) != 1:
                        raise ValueError("inventory slot is not pending reconciliation")
                await self.rebuild_tenant(task["tenant_id"])
                async with self.sessions() as session, session.begin():
                    activated = await session.execute(
                        text("""UPDATE tripmate.inventory SET status='ACTIVE',updated_at=:now
                            WHERE tenant_id=:tenant AND slot_id=:slot AND status='RECONCILING'"""),
                        {"now": datetime.now(UTC), "tenant": task["tenant_id"], "slot": task["slot_id"]},
                    )
                    if affected_rows(activated) != 1:
                        raise ValueError("inventory slot changed during reconciliation")
                    await session.execute(
                        text("""UPDATE tripmate.inventory_reconciliation SET status='RESOLVED',
                            lease_owner=NULL,lease_until=NULL,last_error_code=NULL,last_error_message=NULL,updated_at=:now
                            WHERE tenant_id=:tenant AND reconciliation_id=:id AND lease_owner=:worker"""),
                        {"now": datetime.now(UTC), "tenant": task["tenant_id"],
                         "id": task["reconciliation_id"], "worker": worker_id},
                    )
                resolved += 1
            except Exception as exc:
                async with self.sessions() as session, session.begin():
                    await session.execute(
                        text("""UPDATE tripmate.inventory SET status='RECONCILING',updated_at=:now
                            WHERE tenant_id=:tenant AND slot_id=:slot"""),
                        {"now": datetime.now(UTC), "tenant": task["tenant_id"], "slot": task["slot_id"]},
                    )
                    await session.execute(
                text("""UPDATE tripmate.inventory_reconciliation SET status='FAILED',
                            lease_owner=NULL,lease_until=NULL,next_attempt_at=:next,last_error_code='REBUILD_FAILED',
                            last_error_message=:message,updated_at=:now
                    WHERE tenant_id=:tenant AND reconciliation_id=:id AND lease_owner=:worker"""),
                        {"next": datetime.now(UTC) + timedelta(seconds=30), "message": str(exc)[:500],
                         "now": datetime.now(UTC), "tenant": task["tenant_id"],
                         "id": task["reconciliation_id"], "worker": worker_id},
                    )
        return resolved

    async def ping(self) -> bool:
        return bool(await self.client.ping())

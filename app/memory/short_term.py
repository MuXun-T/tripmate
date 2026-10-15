import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.context import RequestContext
from app.domain.errors import DomainError
from app.domain.ports import Clock
from app.inventory.keys import KeyCodec
from redis.asyncio import Redis
from redis.exceptions import RedisError

SCRIPT_DIR = Path(__file__).resolve().parents[2] / "redis" / "scripts"


class MemoryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trip_id: UUID | None = None
    structured_constraints: dict[str, str | int | float | bool | None] | None = None
    selected_slot_ids: list[str] | None = Field(default=None, max_length=100)
    confirmed_slot_ids: list[str] | None = Field(default=None, max_length=100)
    unresolved_slots: list[str] | None = Field(default=None, max_length=50)
    citation_ids: list[str] | None = Field(default=None, max_length=100)
    recent_run_ids: list[str] | None = Field(default=None, max_length=20)
    recent_action_ids: list[str] | None = Field(default=None, max_length=20)
    message_id: UUID | None = None
    message_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ConversationMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    tenant_id: UUID
    user_id: UUID
    conversation_id: UUID
    version: int = Field(ge=1)
    trip_id: UUID | None = None
    structured_constraints: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict
    )
    selected_slot_ids: list[str] = Field(default_factory=list, max_length=100)
    confirmed_slot_ids: list[str] = Field(default_factory=list, max_length=100)
    unresolved_slots: list[str] = Field(default_factory=list, max_length=50)
    citation_ids: list[str] = Field(default_factory=list, max_length=100)
    recent_run_ids: list[str] = Field(default_factory=list, max_length=20)
    recent_action_ids: list[str] = Field(default_factory=list, max_length=20)
    message_sequence: int = Field(default=0, ge=0)
    message_ids: list[str] = Field(default_factory=list, max_length=100)
    last_message_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    updated_at: datetime
    expires_at: datetime


class ShortTermMemoryService:
    def __init__(
        self,
        client: Redis,
        settings: Settings,
        clock: Clock,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        self.client = client
        self.settings = settings
        self.clock = clock
        self.sessions = sessions
        self.keys = KeyCodec(settings.redis_prefix, settings.app_env)
        self._cas_script: Any = None

    def _json(self) -> Any:
        return cast(Any, self.client).json()

    async def setup(self) -> None:
        self._cas_script = self.client.register_script(
            (SCRIPT_DIR / "short_memory_cas.lua").read_text(encoding="utf-8")
        )

    async def get(self, ctx: RequestContext, conversation_id: UUID) -> dict[str, Any] | None:
        user_id = self._require_user(ctx)
        key = self.keys.conversation(ctx.tenant_id, user_id, conversation_id)
        try:
            raw = await self._json().get(key)
        except RedisError:
            return await self._rebuild_from_database(ctx, conversation_id, key)
        if raw is None:
            return await self._rebuild_from_database(ctx, conversation_id, key)
        memory = ConversationMemory.model_validate(raw)
        if (
            memory.tenant_id != ctx.tenant_id
            or memory.user_id != user_id
            or memory.conversation_id != conversation_id
        ):
            raise DomainError("TENANT_SCOPE_DENIED", "Conversation memory scope mismatch", 403)
        if memory.expires_at <= self.clock.now():
            await self.client.delete(key)
            return None
        return memory.model_dump(mode="json")

    async def merge(
        self, ctx: RequestContext, conversation_id: UUID, patch: MemoryPatch
    ) -> dict[str, Any]:
        user_id = self._require_user(ctx)
        if self._cas_script is None:
            await self.setup()
        key = self.keys.conversation(ctx.tenant_id, user_id, conversation_id)
        for _ in range(8):
            current_data = await self.get(ctx, conversation_id)
            current = ConversationMemory.model_validate(current_data) if current_data else None
            now = self.clock.now()
            message_ids = list(current.message_ids if current else [])
            message_sequence = current.message_sequence if current else 0
            if patch.message_id is not None and str(patch.message_id) not in message_ids:
                message_ids = _merge_unique(message_ids, [str(patch.message_id)], 100)
                message_sequence += 1
            values: dict[str, Any] = {
                "schema_version": 1,
                "tenant_id": ctx.tenant_id,
                "user_id": user_id,
                "conversation_id": conversation_id,
                "version": (current.version if current else 0) + 1,
                "trip_id": patch.trip_id or (current.trip_id if current else None),
                "structured_constraints": {
                    **(current.structured_constraints if current else {}),
                    **(patch.structured_constraints or {}),
                },
                "selected_slot_ids": _merge_patch_list(
                    current.selected_slot_ids if current else [], patch.selected_slot_ids, 100
                ),
                "confirmed_slot_ids": _merge_patch_list(
                    current.confirmed_slot_ids if current else [], patch.confirmed_slot_ids, 100
                ),
                "unresolved_slots": (
                    patch.unresolved_slots
                    if patch.unresolved_slots is not None
                    else (current.unresolved_slots if current else [])
                ),
                "citation_ids": _merge_patch_list(
                    current.citation_ids if current else [], patch.citation_ids, 100
                ),
                "recent_run_ids": _merge_unique(
                    current.recent_run_ids if current else [],
                    patch.recent_run_ids or [],
                    20,
                ),
                "recent_action_ids": _merge_unique(
                    current.recent_action_ids if current else [],
                    patch.recent_action_ids or [],
                    20,
                ),
                "message_sequence": message_sequence,
                "message_ids": message_ids,
                "last_message_hash": patch.message_hash
                or (current.last_message_hash if current else None),
                "updated_at": now,
                "expires_at": now + timedelta(seconds=self.settings.short_term_memory_ttl_seconds),
            }
            memory = ConversationMemory.model_validate(values)
            try:
                saved = await self._cas_script(
                    keys=[key],
                    args=[
                        current.version if current else 0,
                        memory.model_dump_json(),
                        self.settings.short_term_memory_ttl_seconds,
                    ],
                )
            except RedisError as exc:
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Short-term memory is unavailable", 503, True
                ) from exc
            if int(saved) == 1:
                return memory.model_dump(mode="json")
        raise DomainError("VERSION_CONFLICT", "Conversation memory changed repeatedly", 409, True)

    async def clear(self, ctx: RequestContext, conversation_id: UUID) -> bool:
        user_id = self._require_user(ctx)
        key = self.keys.conversation(ctx.tenant_id, user_id, conversation_id)
        return bool(await self.client.delete(key))

    @staticmethod
    def _require_user(ctx: RequestContext) -> UUID:
        if ctx.user_id is None:
            raise DomainError("UNAUTHORIZED", "Authenticated user is required", 401)
        return ctx.user_id

    async def _rebuild_from_database(
        self, ctx: RequestContext, conversation_id: UUID, key: str
    ) -> dict[str, Any] | None:
        user_id = self._require_user(ctx)
        async with self.sessions() as session:
            result = await session.execute(
                text("""SELECT cm.message_id,cm.sequence_no,cm.content_hash,cm.run_id,
                    cm.created_at,r.trip_id,r.result_json
                    FROM tripmate.conversation_message cm
                    LEFT JOIN tripmate.agent_run r ON r.tenant_id=cm.tenant_id AND r.run_id=cm.run_id
                    WHERE cm.tenant_id=:tenant AND cm.user_id=:user AND cm.conversation_id=:conversation
                      AND cm.created_at>:cutoff
                    ORDER BY cm.sequence_no DESC LIMIT 20"""),
                {
                    "tenant": ctx.tenant_id,
                    "user": user_id,
                    "conversation": conversation_id,
                    "cutoff": self.clock.now()
                    - timedelta(seconds=self.settings.short_term_memory_ttl_seconds),
                },
            )
            rows = list(result.mappings())
            if not rows:
                return None
            rows.reverse()
            last = rows[-1]
            stored = _as_mapping(last["result_json"])
            report = _as_mapping(stored.get("result"))
            plan = _as_mapping(stored.get("trip_plan") or report.get("trip_plan"))
            trip_id = last["trip_id"]
            confirmed: list[str] = []
            if trip_id is not None:
                confirmed_rows = await session.execute(
                    text("""SELECT DISTINCT slot_id FROM tripmate.reservation
                        WHERE tenant_id=:tenant AND user_id=:user AND trip_id=:trip
                          AND status='CONFIRMED' ORDER BY slot_id"""),
                    {"tenant": ctx.tenant_id, "user": user_id, "trip": trip_id},
                )
                confirmed = [str(row[0]) for row in confirmed_rows]

        items_value = plan.get("items")
        items = items_value if isinstance(items_value, list) else []
        citations_value = stored.get("citations") or report.get("citations") or []
        citations = citations_value if isinstance(citations_value, list) else []
        actions_value = stored.get("pending_actions") or report.get("pending_actions") or []
        actions = actions_value if isinstance(actions_value, list) else []
        run_ids = [str(row["run_id"]) for row in rows if row["run_id"] is not None]
        memory = ConversationMemory(
            tenant_id=ctx.tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            version=1,
            trip_id=trip_id,
            structured_constraints={
                key: plan[key]
                for key in ("destination", "start_date", "end_date", "party_size", "timezone")
                if key in plan and isinstance(plan[key], str | int | float | bool)
            },
            selected_slot_ids=[
                str(item["slot_id"])
                for item in items
                if isinstance(item, dict) and item.get("slot_id")
            ],
            confirmed_slot_ids=confirmed,
            citation_ids=[
                str(item["chunk_id"])
                for item in citations
                if isinstance(item, dict) and item.get("chunk_id")
            ],
            recent_run_ids=_merge_unique([], run_ids, 20),
            recent_action_ids=[
                str(item["action_id"])
                for item in actions
                if isinstance(item, dict) and item.get("action_id")
            ][-20:],
            message_sequence=int(last["sequence_no"]),
            message_ids=[str(row["message_id"]) for row in rows][-100:],
            last_message_hash=str(last["content_hash"]),
            updated_at=self.clock.now(),
            expires_at=self.clock.now()
            + timedelta(seconds=self.settings.short_term_memory_ttl_seconds),
        )
        if self._cas_script is None:
            await self.setup()
        try:
            saved = await self._cas_script(
                keys=[key],
                args=[0, memory.model_dump_json(), self.settings.short_term_memory_ttl_seconds],
            )
        except RedisError:
            return memory.model_dump(mode="json")
        if int(saved) != 1:
            try:
                latest = await self._json().get(key)
            except RedisError:
                return memory.model_dump(mode="json")
            if latest is not None:
                return ConversationMemory.model_validate(latest).model_dump(mode="json")
        return memory.model_dump(mode="json")


def _merge_patch_list(current: list[str], incoming: list[str] | None, limit: int) -> list[str]:
    if incoming is None:
        return current
    return _merge_unique(current, incoming, limit)


def _merge_unique(current: list[str], incoming: list[str], limit: int) -> list[str]:
    return list(dict.fromkeys([*current, *incoming]))[-limit:]


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}

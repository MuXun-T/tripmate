import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.context import RequestContext
from app.domain.errors import DomainError
from app.domain.idempotency import canonical_json, sha256_hex
from app.events.schema import EventEnvelope
from app.knowledge.retriever import KnowledgeRetriever
from app.memory.preferences import PreferenceService
from app.reservation.service import ReservationService
from app.tools.realtime import RealtimeAdapters
from redis.asyncio import Redis

logger = logging.getLogger(__name__)
_TOOL_CONTEXT: ContextVar[RequestContext] = ContextVar("tripmate_tool_context")
READ_ONLY_TOOL_IDS = frozenset(
    {
        "search_attractions",
        "get_weather",
        "get_opening_hours",
        "estimate_route_time",
        "check_ticket_inventory",
        "search_travel_knowledge",
        "get_trip_context",
        "get_reservation_history",
        "calculate_trip_budget",
    }
)


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SearchAttractionsInput(StrictInput):
    query: str = Field(min_length=1, max_length=150)
    city: str | None = Field(default=None, max_length=80)


class WeatherInput(StrictInput):
    city: str = Field(min_length=1, max_length=80)
    visit_date: date


class OpeningHoursInput(StrictInput):
    attraction_id: UUID
    visit_date: date
    timezone: str = Field(default="UTC", min_length=1, max_length=80)


class RouteInput(StrictInput):
    origin_attraction_id: UUID
    destination_attraction_id: UUID


class InventoryInput(StrictInput):
    trip_id: UUID
    slot_id: UUID
    quantity: int = Field(default=1, ge=1, le=100)


class KnowledgeInput(StrictInput):
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=20)


class TripInput(StrictInput):
    trip_id: UUID


class BudgetInput(StrictInput):
    trip_id: UUID


class ProposeReservationInput(StrictInput):
    trip_id: UUID
    slot_id: UUID
    quantity: int = Field(ge=1, le=100)
    reason: str = Field(default="", max_length=200)


class ProposeCancellationInput(StrictInput):
    reservation_id: UUID
    reason: str = Field(min_length=1, max_length=200)


class PreferenceInput(StrictInput):
    scope: str = Field(default="personal", min_length=1, max_length=40)
    key: str = Field(min_length=1, max_length=80)
    value: Any
    reason: str = Field(default="", max_length=200)


ToolHandler = Callable[[RequestContext, BaseModel], Awaitable[Any]]


class ToolGateway:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        realtime: RealtimeAdapters,
        knowledge: KnowledgeRetriever,
        reservations: ReservationService,
        preferences: PreferenceService,
        redis: Redis | None = None,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.realtime = realtime
        self.knowledge = knowledge
        self.reservations = reservations
        self.preferences = preferences
        self.redis = redis
        self.registry: dict[str, tuple[type[BaseModel], ToolHandler, StructuredTool]] = {}
        self._local_windows: dict[tuple[str, str, str], tuple[int, int]] = {}
        self._register_tools()

    def _register(
        self, name: str, description: str, schema: type[BaseModel], handler: ToolHandler
    ) -> None:
        async def invoke(**kwargs: Any) -> Any:
            return await handler(_TOOL_CONTEXT.get(), schema.model_validate(kwargs))

        tool = StructuredTool.from_function(
            coroutine=invoke, name=name, description=description, args_schema=schema
        )
        self.registry[name] = (schema, handler, tool)

    def _register_tools(self) -> None:
        self._register(
            "search_attractions",
            "Find tenant-scoped attractions by name or category.",
            SearchAttractionsInput,
            self._search_attractions,
        )
        self._register(
            "get_weather",
            "Fetch a dated forecast from Open-Meteo; unavailable data is UNKNOWN.",
            WeatherInput,
            self._get_weather,
        )
        self._register(
            "get_opening_hours",
            "Read dated activity windows from the local catalog.",
            OpeningHoursInput,
            self._get_opening_hours,
        )
        self._register(
            "estimate_route_time",
            "Estimate a route from tenant-scoped attraction coordinates.",
            RouteInput,
            self._estimate_route,
        )
        self._register(
            "check_ticket_inventory",
            "Read authoritative PostgreSQL inventory and price snapshot.",
            InventoryInput,
            self._check_inventory,
        )
        self._register(
            "search_travel_knowledge",
            "Retrieve static travel knowledge with traceable citations.",
            KnowledgeInput,
            self._search_knowledge,
        )
        self._register(
            "get_trip_context",
            "Read a Trip and its itinerary within the current user's scope.",
            TripInput,
            self._get_trip_context,
        )
        self._register(
            "get_reservation_history",
            "Read reservations owned by the current user for a Trip.",
            TripInput,
            self._get_reservation_history,
        )
        self._register(
            "calculate_trip_budget",
            "Sum deterministic unit prices for a party size.",
            BudgetInput,
            self._calculate_budget,
        )
        self._register(
            "propose_reservation",
            "Persist a reservation candidate; this tool does not hold stock.",
            ProposeReservationInput,
            self._propose_reservation,
        )
        self._register(
            "propose_cancellation",
            "Persist a cancellation candidate; this tool does not cancel an order.",
            ProposeCancellationInput,
            self._propose_cancellation,
        )
        self._register(
            "save_user_preference",
            "Validate and persist a preference candidate for explicit approval.",
            PreferenceInput,
            self._propose_preference,
        )

    def readonly_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "tool_id": tool_id,
                "description": self.registry[tool_id][2].description,
                "parameters": self.registry[tool_id][0].model_json_schema(),
            }
            for tool_id in sorted(READ_ONLY_TOOL_IDS)
            if tool_id in self.registry
        ]

    async def invoke_readonly(
        self, tool_id: str, arguments: dict[str, Any], ctx: RequestContext
    ) -> Any:
        if tool_id not in READ_ONLY_TOOL_IDS or tool_id not in self.registry:
            raise DomainError("TOOL_NOT_ALLOWED", "Tool is not registered as read-only", 403)
        schema = self.registry[tool_id][0]
        parsed = schema.model_validate(arguments)
        return await self.invoke(tool_id, parsed.model_dump(mode="json"), ctx)

    async def invoke(self, name: str, arguments: dict[str, Any], ctx: RequestContext) -> Any:
        spec = self.registry.get(name)
        if spec is None:
            raise DomainError("TOOL_NOT_ALLOWED", "Tool is not registered", 403)
        if ctx.user_id is None or ctx.source not in {"http", "agent"}:
            raise DomainError(
                "TOOL_NOT_ALLOWED", "Tool invocation requires an authenticated user", 403
            )
        schema, _handler, tool = spec
        parsed = schema.model_validate(arguments)
        await self._check_rate_limit(ctx, name)
        started = time.perf_counter()
        result_code = "OK"
        result_hash = None
        try:
            token = _TOOL_CONTEXT.set(ctx)
            try:
                result = await asyncio.wait_for(
                    tool.ainvoke(parsed.model_dump(mode="json")),
                    timeout=self.settings.external_http_timeout_seconds * 2,
                )
            finally:
                _TOOL_CONTEXT.reset(token)
            result_hash = sha256_hex(canonical_json(result))
            return result
        except TimeoutError as exc:
            result_code = "DEPENDENCY_TIMEOUT"
            raise DomainError(
                result_code, "Tool exceeded its execution deadline", 503, True
            ) from exc
        except DomainError as exc:
            result_code = exc.code
            raise
        except Exception as exc:
            result_code = "TOOL_PROVIDER_ERROR"
            raise DomainError(
                result_code, "Tool provider failed", 503, True,
                details={"provider": type(exc).__name__},
            ) from exc
        finally:
            await self._audit_tool_call(
                ctx,
                name,
                parsed,
                result_hash,
                result_code,
                int((time.perf_counter() - started) * 1000),
            )

    async def _check_rate_limit(self, ctx: RequestContext, tool_name: str) -> None:
        key = f"{self.settings.redis_prefix}:v1:{self.settings.app_env}:{{{ctx.tenant_id}}}:limit:{ctx.user_id}:{tool_name}"
        try:
            if self.redis is not None:
                count = await self.redis.incr(key)
                if count == 1:
                    await self.redis.expire(key, 60)
                if count > 60:
                    raise DomainError("RATE_LIMITED", "Tool rate limit exceeded", 429, True)
                return
        except DomainError:
            raise
        except Exception:
            pass
        window = int(time.time() // 60)
        local_key = (str(ctx.tenant_id), str(ctx.user_id), tool_name)
        old_window, count = self._local_windows.get(local_key, (window, 0))
        count = count + 1 if old_window == window else 1
        self._local_windows[local_key] = (window, count)
        if count > 60:
            raise DomainError("RATE_LIMITED", "Tool rate limit exceeded", 429, True)

    async def _audit_tool_call(
        self,
        ctx: RequestContext,
        name: str,
        args: BaseModel,
        result_hash: str | None,
        result_code: str,
        duration_ms: int,
    ) -> None:
        if ctx.user_id is None:
            return
        async with self.sessions() as session, session.begin():
            await session.execute(
                text("""INSERT INTO tripmate.agent_tool_call
                    (tenant_id,run_id,user_id,tool_name,arguments_hash,result_hash,result_code,duration_ms)
                    VALUES(:tenant,:run,:user,:tool,:args_hash,:result_hash,:code,:duration)"""),
                {
                    "tenant": ctx.tenant_id,
                    "run": ctx.run_id,
                    "user": ctx.user_id,
                    "tool": name,
                    "args_hash": sha256_hex(canonical_json(args.model_dump(mode="json"))),
                    "result_hash": result_hash,
                    "code": result_code,
                    "duration": duration_ms,
                },
            )

    async def _search_attractions(self, ctx: RequestContext, data: BaseModel) -> Any:
        args = SearchAttractionsInput.model_validate(data)
        return await self.realtime.search_attractions(ctx, args.query, args.city)

    async def _get_weather(self, ctx: RequestContext, data: BaseModel) -> Any:
        args = WeatherInput.model_validate(data)
        return await self.realtime.get_weather(args.city, args.visit_date)

    async def _get_opening_hours(self, ctx: RequestContext, data: BaseModel) -> Any:
        args = OpeningHoursInput.model_validate(data)
        return await self.realtime.get_opening_hours(
            ctx, args.attraction_id, args.visit_date, args.timezone
        )

    async def _estimate_route(self, ctx: RequestContext, data: BaseModel) -> Any:
        args = RouteInput.model_validate(data)
        return await self.realtime.estimate_route_time(
            ctx, args.origin_attraction_id, args.destination_attraction_id
        )

    async def _check_inventory(self, ctx: RequestContext, data: BaseModel) -> Any:
        args = InventoryInput.model_validate(data)
        async with self.sessions() as session:
            result = await session.execute(
                text("""SELECT i.available_quantity,i.total_quantity,i.held_quantity,i.confirmed_quantity,
                    i.version AS inventory_version,i.status AS inventory_status,
                    s.activity_version,s.price,s.currency,s.start_at,s.end_at,s.status AS slot_status
                    FROM tripmate.inventory i JOIN tripmate.activity_slot s
                      ON s.tenant_id=i.tenant_id AND s.slot_id=i.slot_id
                    JOIN tripmate.trip t ON t.tenant_id=i.tenant_id AND t.trip_id=:trip
                    WHERE i.tenant_id=:tenant AND i.slot_id=:slot
                    AND (t.owner_user_id=:user OR EXISTS(SELECT 1 FROM tripmate.trip_member m
                      WHERE m.tenant_id=t.tenant_id AND m.trip_id=t.trip_id AND m.user_id=:user AND m.status='ACTIVE'))"""),
                {
                    "tenant": ctx.tenant_id,
                    "trip": args.trip_id,
                    "slot": args.slot_id,
                    "user": ctx.user_id,
                },
            )
            row = result.mappings().one_or_none()
        if row is None:
            raise DomainError("NOT_FOUND", "Trip inventory was not found", 404)
        invariant = (
            int(row["available_quantity"])
            + int(row["held_quantity"])
            + int(row["confirmed_quantity"])
        )
        if invariant != int(row["total_quantity"]):
            raise DomainError(
                "RECONCILIATION_REQUIRED", "Inventory invariant is violated", 503, True
            )
        return {
            "available_quantity": int(row["available_quantity"]),
            "requested_quantity": args.quantity,
            "available": int(row["available_quantity"]) >= args.quantity,
            "price": str(Decimal(row["price"])),
            "currency": row["currency"].strip(),
            "activity_version": int(row["activity_version"]),
            "inventory_version": int(row["inventory_version"]),
            "slot_window": {
                "start_at": row["start_at"].isoformat(),
                "end_at": row["end_at"].isoformat(),
            },
            "slot_status": row["slot_status"],
            "inventory_status": row["inventory_status"],
            "queried_at": datetime.now(UTC).isoformat(),
            "source": "postgres",
        }

    async def _search_knowledge(self, ctx: RequestContext, data: BaseModel) -> Any:
        args = KnowledgeInput.model_validate(data)
        citations = await self.knowledge.search(ctx, args.query, top_k=args.top_k)
        return [citation.model_dump(mode="json") for citation in citations]

    async def _get_trip_context(self, ctx: RequestContext, data: BaseModel) -> Any:
        args = TripInput.model_validate(data)
        async with self.sessions() as session:
            trip = await session.execute(
                text("""SELECT t.* FROM tripmate.trip t WHERE t.tenant_id=:tenant AND t.trip_id=:trip
                    AND (t.owner_user_id=:user OR EXISTS(SELECT 1 FROM tripmate.trip_member m
                      WHERE m.tenant_id=t.tenant_id AND m.trip_id=t.trip_id AND m.user_id=:user AND m.status='ACTIVE'))"""),
                {"tenant": ctx.tenant_id, "trip": args.trip_id, "user": ctx.user_id},
            )
            row = trip.mappings().one_or_none()
            if row is None:
                raise DomainError("NOT_FOUND", "Trip was not found", 404)
            items = await session.execute(
                text("""SELECT ti.sequence_no,ti.attraction_id,ti.slot_id,ti.start_at,ti.end_at,
                    ti.cost,ti.currency,ti.constraint_status FROM tripmate.trip_item ti
                    WHERE ti.tenant_id=:tenant AND ti.trip_id=:trip ORDER BY ti.sequence_no"""),
                {"tenant": ctx.tenant_id, "trip": args.trip_id},
            )
            return {
                "trip": {
                    **dict(row),
                    "budget_limit": str(row["budget_limit"])
                    if row["budget_limit"] is not None
                    else None,
                },
                "items": [dict(item) for item in items.mappings()],
            }

    async def _get_reservation_history(self, ctx: RequestContext, data: BaseModel) -> Any:
        args = TripInput.model_validate(data)
        async with self.sessions() as session:
            rows = await session.execute(
                text("""SELECT reservation_id,slot_id,status,quantity,price_snapshot,currency,version,
                    hold_expires_at,created_at FROM tripmate.reservation WHERE tenant_id=:tenant
                    AND user_id=:user AND trip_id=:trip ORDER BY created_at DESC LIMIT 100"""),
                {"tenant": ctx.tenant_id, "user": ctx.user_id, "trip": args.trip_id},
            )
            return [dict(row) for row in rows.mappings()]

    async def _calculate_budget(self, ctx: RequestContext, data: BaseModel) -> Any:
        args = BudgetInput.model_validate(data)
        async with self.sessions() as session:
            result = await session.execute(
                text("""SELECT t.currency,t.party_size,
                    COALESCE(SUM(CASE WHEN r.status='CONFIRMED'
                      THEN r.price_snapshot*r.quantity ELSE ti.cost*t.party_size END),0) AS total
                    FROM tripmate.trip t
                    LEFT JOIN tripmate.trip_item ti ON ti.tenant_id=t.tenant_id AND ti.trip_id=t.trip_id
                    LEFT JOIN tripmate.reservation r ON r.tenant_id=ti.tenant_id AND r.reservation_id=ti.reservation_id
                    WHERE t.tenant_id=:tenant AND t.trip_id=:trip
                      AND (t.owner_user_id=:user OR EXISTS(SELECT 1 FROM tripmate.trip_member m
                        WHERE m.tenant_id=t.tenant_id AND m.trip_id=t.trip_id AND m.user_id=:user AND m.status='ACTIVE'))
                    GROUP BY t.currency,t.party_size"""),
                {"tenant": ctx.tenant_id, "trip": args.trip_id, "user": ctx.user_id},
            )
            row = result.mappings().one_or_none()
        if row is None:
            raise DomainError("NOT_FOUND", "Trip was not found", 404)
        total = Decimal(row["total"])
        return {
            "total": str(total.quantize(Decimal("0.01"))),
            "currency": (row["currency"] or "CNY").strip(),
            "party_size": int(row["party_size"]),
            "source": "postgres trip and reservation price snapshots",
        }

    async def _propose_reservation(self, ctx: RequestContext, data: BaseModel) -> Any:
        if ctx.run_id is None:
            raise DomainError("INVALID_ARGUMENT", "Reservation proposal requires an AgentRun", 400)
        args = ProposeReservationInput.model_validate(data)
        action_id, parameters_hash = await self.reservations.create_candidate_action(
            ctx,
            run_id=ctx.run_id,
            action_type="propose_reservation",
            parameters={
                "trip_id": str(args.trip_id),
                "slot_id": str(args.slot_id),
                "quantity": args.quantity,
                "reason": args.reason,
            },
        )
        return {
            "action_id": str(action_id),
            "action_type": "propose_reservation",
            "parameters_hash": parameters_hash,
            "status": "PROPOSED",
            "requires_confirmation": True,
        }

    async def _propose_cancellation(self, ctx: RequestContext, data: BaseModel) -> Any:
        if ctx.run_id is None:
            raise DomainError("INVALID_ARGUMENT", "Cancellation proposal requires an AgentRun", 400)
        args = ProposeCancellationInput.model_validate(data)
        action_id, parameters_hash = await self.reservations.create_candidate_action(
            ctx,
            run_id=ctx.run_id,
            action_type="propose_cancellation",
            parameters={"reservation_id": str(args.reservation_id), "reason": args.reason},
        )
        return {
            "action_id": str(action_id),
            "action_type": "propose_cancellation",
            "parameters_hash": parameters_hash,
            "status": "PROPOSED",
            "requires_confirmation": True,
        }

    async def _propose_preference(self, ctx: RequestContext, data: BaseModel) -> Any:
        if ctx.run_id is None:
            raise DomainError("INVALID_ARGUMENT", "Preference proposal requires an AgentRun", 400)
        args = PreferenceInput.model_validate(data)
        validated = self.preferences._validate_value(args.key, args.value)
        current = next(
            (
                item
                for item in await self.preferences.list_for_user(ctx)
                if item["scope"] == args.scope and item["preference_key"] == args.key
            ),
            None,
        )
        expected_version = int(current["version"]) if current is not None else None
        parameters_hash = sha256_hex(
            canonical_json(
                {
                    "tenant_id": str(ctx.tenant_id),
                    "user_id": str(ctx.user_id),
                    "run_id": str(ctx.run_id),
                    "key": args.key,
                    "scope": args.scope,
                    "value": validated,
                    "reason": args.reason,
                    "expected_version": expected_version,
                }
            )
        )
        fingerprint = sha256_hex(
            canonical_json(
                {
                    "run_id": str(ctx.run_id),
                    "type": "save_preference",
                    "parameters_hash": parameters_hash,
                }
            )
        )
        async with self.sessions() as session, session.begin():
            row = await session.execute(
                text("""INSERT INTO tripmate.agent_action
                    (tenant_id,run_id,action_type,parameters,parameters_hash,status,action_fingerprint)
                    VALUES(:tenant,:run,'save_preference',CAST(:parameters AS jsonb),:hash,'PROPOSED',:fingerprint)
                    ON CONFLICT (tenant_id,run_id,action_fingerprint) DO UPDATE SET action_fingerprint=EXCLUDED.action_fingerprint
                    RETURNING action_id"""),
                {
                    "tenant": ctx.tenant_id,
                    "run": ctx.run_id,
                    "parameters": json.dumps(
                        {
                            "scope": args.scope,
                            "key": args.key,
                            "value": validated,
                            "reason": args.reason,
                            "expected_version": expected_version,
                        }
                    ),
                    "hash": parameters_hash,
                    "fingerprint": fingerprint,
                },
            )
            action_id = row.scalar_one()
            run_update = await session.execute(
                text("""UPDATE tripmate.agent_run SET status='WAITING_CONFIRMATION',awaiting_action_id=:action,
                    version=version+1,updated_at=now() WHERE tenant_id=:tenant AND run_id=:run
                    AND user_id=:user AND status='RUNNING' AND lease_owner=:owner
                    AND fencing_version=:fence AND lease_until>now() RETURNING version"""),
                {
                    "action": action_id,
                    "tenant": ctx.tenant_id,
                    "run": ctx.run_id,
                    "user": ctx.user_id,
                    "owner": ctx.lease_owner,
                    "fence": ctx.fencing_version,
                },
            )
            run_version = run_update.scalar_one_or_none()
            if run_version is None:
                existing = await session.scalar(
                    text(
                        "SELECT status FROM tripmate.agent_run WHERE tenant_id=:tenant AND run_id=:run"
                    ),
                    {"tenant": ctx.tenant_id, "run": ctx.run_id},
                )
                current_action = await session.scalar(
                    text("""SELECT awaiting_action_id FROM tripmate.agent_run
                        WHERE tenant_id=:tenant AND run_id=:run"""),
                    {"tenant": ctx.tenant_id, "run": ctx.run_id},
                )
                if existing != "WAITING_CONFIRMATION" or current_action != action_id:
                    raise DomainError("VERSION_CONFLICT", "Run is not available for approval", 409)
            else:
                envelope = EventEnvelope(
                    event_id=uuid4(),
                    event_type="agent.approval_required.v1",
                    event_version=1,
                    occurred_at=datetime.now(UTC),
                    tenant_id=ctx.tenant_id,
                    aggregate_type="agent_run",
                    aggregate_id=ctx.run_id,
                    aggregate_version=int(run_version),
                    request_id=ctx.request_id,
                    trace_id=ctx.trace_id,
                    run_id=ctx.run_id,
                    producer="tripmate-api",
                    payload={
                        "run_id": str(ctx.run_id),
                        "action_id": str(action_id),
                        "action_type": "save_preference",
                        "parameters_hash": parameters_hash,
                    },
                )
                await session.execute(
                    text("""INSERT INTO tripmate.outbox_event
                        (event_id,tenant_id,aggregate_type,aggregate_id,aggregate_version,event_type,event_version,payload,status)
                        VALUES(:event,:tenant,'agent_run',:run,:version,:type,1,CAST(:payload AS jsonb),'PENDING')"""),
                    {
                        "event": envelope.event_id,
                        "tenant": ctx.tenant_id,
                        "run": ctx.run_id,
                        "version": run_version,
                        "type": envelope.event_type,
                        "payload": json.dumps(envelope.model_dump(mode="json")),
                    },
                )
        return {
            "action_id": str(action_id),
            "action_type": "save_preference",
            "parameters_hash": parameters_hash,
            "status": "PROPOSED",
            "requires_confirmation": True,
        }

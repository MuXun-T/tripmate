import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.runs import AgentRunService
from app.agent.state import AgentState
from app.domain.constraints import InventoryFact, validate_trip_plan
from app.domain.context import RequestContext
from app.domain.errors import DomainError
from app.domain.idempotency import canonical_json, sha256_hex
from app.domain.models import ActivityPlanItem, ConstraintState, Money, TimeWindow, TripPlan
from app.domain.ports import Clock, IdGenerator
from app.events.schema import EventEnvelope
from app.knowledge.retriever import KnowledgeRetriever
from app.memory.preferences import PreferenceService
from app.memory.short_term import MemoryPatch, ShortTermMemoryService
from app.reservation.service import ReservationService
from app.tools.gateway import ToolGateway

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GraphDependencies:
    sessions: async_sessionmaker[AsyncSession]
    settings: Any
    clock: Clock
    ids: IdGenerator
    runs: AgentRunService
    reservations: ReservationService
    preferences: PreferenceService
    short_term_memory: ShortTermMemoryService
    knowledge: KnowledgeRetriever
    tools: ToolGateway


def build_graph(deps: GraphDependencies, checkpointer: Any) -> Any:
    async def load_context(state: AgentState) -> dict[str, Any]:
        run = await deps.runs.load_run_input(state["tenant_id"], state["run_id"])
        if run["user_id"] != state["user_id"] or run["thread_id"] != state["thread_id"]:
            raise DomainError(
                "TENANT_SCOPE_DENIED", "Run state does not match its persisted scope", 403
            )
        return {
            "message": run["content_redacted"],
            "message_hash": run["content_hash"],
            "message_id": run["message_id"],
            "conversation_id": run["conversation_id"],
            "node": "intent",
        }

    async def intent(state: AgentState) -> dict[str, Any]:
        message = state.get("message", "").casefold()
        if any(token in message for token in ("取消", "退订", "cancel")):
            classified = "cancellation"
        elif any(token in message for token in ("预约", "预订", "订票", "reserve", "book")):
            classified = "reservation"
        elif any(
            token in message for token in ("政策", "规则", "退改", "儿童", "开放时间", "几点")
        ):
            classified = "knowledge"
        else:
            classified = "planning"
        missing = [] if state.get("trip_id") else ["trip_id"]
        return {"intent": classified, "missing_slots": missing, "node": "memory_load"}

    async def memory_load(state: AgentState) -> dict[str, Any]:
        ctx = _context(state, deps.clock)
        preferences = await deps.preferences.list_for_user(ctx)
        try:
            memory = await deps.short_term_memory.get(ctx, state["conversation_id"])
        except Exception:
            logger.warning("short_term_memory_read_failed", extra={"run_id": str(state["run_id"])})
            memory = None
        refs = [
            {
                "preference_id": str(item["preference_id"]),
                "key": item["preference_key"],
                "version": item["version"],
                "generation": item["generation"],
            }
            for item in preferences
        ]
        remembered_trip_id = memory.get("trip_id") if memory else None
        trip_id = state.get("trip_id") or (UUID(remembered_trip_id) if remembered_trip_id else None)
        return {
            "preference_refs": refs,
            "conversation_memory": memory or {},
            "trip_id": trip_id,
            "missing_slots": [] if trip_id else state.get("missing_slots", ["trip_id"]),
            "node": "rag_retrieve",
        }

    async def rag_retrieve(state: AgentState) -> dict[str, Any]:
        ctx = _context(state, deps.clock)
        query = state.get("message", "")
        citations = await deps.knowledge.search(ctx, query, top_k=deps.settings.rag_top_k)
        return {
            "citations": [citation.model_dump(mode="json") for citation in citations],
            "node": "realtime_query",
        }

    async def realtime_query(state: AgentState) -> dict[str, Any]:
        ctx = _context(state, deps.clock)
        facts: dict[str, Any] = {}
        trip_id = state.get("trip_id")
        if trip_id is not None:
            facts["trip_context"] = await deps.tools.invoke(
                "get_trip_context", {"trip_id": str(trip_id)}, ctx
            )
        message = state.get("message", "").casefold()
        if trip_id is not None and any(
            word in message for word in ("天气", "下雨", "weather", "rain")
        ):
            trip = facts.get("trip_context", {}).get("trip", {})
            facts["weather"] = await deps.tools.invoke(
                "get_weather",
                {
                    "city": trip.get("destination", ""),
                    "visit_date": str(trip.get("start_date", date.today().isoformat())),
                },
                ctx,
            )
        return {
            "realtime_facts": facts,
            "trip_context": facts.get("trip_context", {}),
            "node": "plan",
        }

    async def plan(state: AgentState) -> dict[str, Any]:
        if state.get("trip_id") is None:
            return {"trip_plan": {}, "node": "constraints"}
        trip_id = state["trip_id"]
        tenant_id, user_id = state["tenant_id"], state["user_id"]
        async with deps.sessions() as session:
            trip_result = await session.execute(
                text("""SELECT * FROM tripmate.trip WHERE tenant_id=:tenant AND trip_id=:trip
                    AND (owner_user_id=:user OR EXISTS(SELECT 1 FROM tripmate.trip_member m
                      WHERE m.tenant_id=:tenant AND m.trip_id=:trip AND m.user_id=:user AND m.status='ACTIVE'))"""),
                {"tenant": tenant_id, "trip": trip_id, "user": user_id},
            )
            trip = trip_result.mappings().one_or_none()
            if trip is None:
                raise DomainError("NOT_FOUND", "Trip was not found", 404)
            zone = ZoneInfo(trip["timezone"])
            start = datetime.combine(trip["start_date"], time.min, zone).astimezone(UTC)
            end = datetime.combine(trip["end_date"] + timedelta(days=1), time.min, zone).astimezone(
                UTC
            )
            slot_result = await session.execute(
                text("""SELECT s.slot_id,s.start_at,s.end_at,s.price,s.currency,s.activity_version,
                    s.status AS slot_status,a.attraction_id,a.name,a.category,a.latitude,a.longitude,
                    i.available_quantity,i.version AS inventory_version,i.status AS inventory_status
                    FROM tripmate.activity_slot s JOIN tripmate.attraction a
                      ON a.tenant_id=s.tenant_id AND a.attraction_id=s.attraction_id
                    JOIN tripmate.inventory i ON i.tenant_id=s.tenant_id AND i.slot_id=s.slot_id
                    WHERE s.tenant_id=:tenant AND s.start_at>=:start_at AND s.start_at<:end_at
                      AND s.status='ACTIVE' AND i.status='ACTIVE' AND a.status='ACTIVE'
                    ORDER BY s.start_at,s.slot_id LIMIT 100"""),
                {"tenant": tenant_id, "start_at": start, "end_at": end},
            )
            slots = list(slot_result.mappings())
            confirmed = await session.execute(
                text("""SELECT slot_id FROM tripmate.reservation WHERE tenant_id=:tenant
                    AND trip_id=:trip AND status='CONFIRMED'"""),
                {"tenant": tenant_id, "trip": trip_id},
            )
            confirmed_slot_ids = {str(row["slot_id"]) for row in confirmed.mappings()}
        citations = state.get("citations", [])
        selected = next(
            (
                slot
                for slot in slots
                if int(slot["available_quantity"]) >= int(trip["party_size"])
                or str(slot["slot_id"]) in confirmed_slot_ids
            ),
            None,
        )
        items: list[ActivityPlanItem] = []
        inventory_facts: dict[str, InventoryFact] = {}
        if selected is not None:
            qty = int(trip["party_size"])
            item_cost = Decimal(selected["price"]) * qty
            item = ActivityPlanItem(
                sequence_no=1,
                attraction_id=selected["attraction_id"],
                slot_id=selected["slot_id"],
                title=selected["name"],
                window=TimeWindow(
                    start_at=selected["start_at"],
                    end_at=selected["end_at"],
                    timezone=trip["timezone"],
                ),
                cost=Money(
                    amount=item_cost.quantize(Decimal("0.01")),
                    currency=selected["currency"].strip(),
                ),
                reservation_required=True,
                inventory_version=int(selected["inventory_version"]),
                activity_version=int(selected["activity_version"]),
                constraint_status=ConstraintState.NOT_EVALUATED,
                citation_ids=tuple(str(item["chunk_id"]) for item in citations),
            )
            items.append(item)
            inventory_facts[str(selected["slot_id"])] = InventoryFact(
                slot_id=str(selected["slot_id"]),
                available_quantity=int(selected["available_quantity"]),
                inventory_version=int(selected["inventory_version"]),
            )
        total = sum((item.cost.amount for item in items), Decimal("0"))
        currency = (
            trip["currency"].strip()
            if trip["currency"]
            else (selected["currency"].strip() if selected else "CNY")
        )
        plan_model = TripPlan(
            trip_id=trip_id,
            destination=trip["destination"],
            start_date=trip["start_date"],
            end_date=trip["end_date"],
            timezone=trip["timezone"],
            party_size=int(trip["party_size"]),
            items=tuple(items),
            total_cost=Money(amount=total.quantize(Decimal("0.01")), currency=currency),
            feasible=bool(items),
            generated_at=deps.clock.now(),
            planner_version="tripmate-deterministic-v1",
            hard_constraints=(),
            soft_preferences=(),
        )
        inventory_by_slot = inventory_facts
        result = validate_trip_plan(
            plan_model,
            inventory=inventory_by_slot,
            confirmed_slot_ids=frozenset(confirmed_slot_ids),
            open_slot_ids=frozenset(str(slot["slot_id"]) for slot in slots),
            budget_limit=Decimal(trip["budget_limit"])
            if trip["budget_limit"] is not None
            else None,
        )
        plan_data = plan_model.model_dump(mode="json")
        plan_data["feasible"] = result.feasible
        plan_data["hard_constraints"] = list(result.checks)
        return {
            "trip_plan": plan_data,
            "inventory_facts": {
                key: {
                    "available_quantity": value.available_quantity,
                    "inventory_version": value.inventory_version,
                }
                for key, value in inventory_by_slot.items()
            },
            "node": "constraints",
        }

    async def constraints(state: AgentState) -> dict[str, Any]:
        plan_data = state.get("trip_plan", {})
        if not plan_data:
            constraint_data = {
                "feasible": False,
                "state": "UNKNOWN",
                "checks": [{"code": "TRIP_REQUIRED", "state": "UNKNOWN"}],
            }
            return {"constraint_result": constraint_data, "node": "report"}
        plan_model = TripPlan.model_validate(plan_data)
        inventory = {
            slot: InventoryFact(
                slot_id=slot,
                available_quantity=int(fact["available_quantity"]),
                inventory_version=int(fact["inventory_version"]),
            )
            for slot, fact in state.get("inventory_facts", {}).items()
        }
        confirmed_slots: set[str] = set()
        context = state.get("trip_context", {})
        if not isinstance(context, dict):
            context = {}
        for item in context.get("items", []):
            if item.get("reservation_id"):
                confirmed_slots.add(str(item.get("slot_id")))
        result = validate_trip_plan(
            plan_model, inventory=inventory, confirmed_slot_ids=frozenset(confirmed_slots)
        )
        constraint_data = {
            "feasible": result.feasible,
            "state": result.state,
            "checks": list(result.checks),
            "total_cost": str(result.total_cost),
        }
        plan_data = {
            **plan_data,
            "feasible": result.feasible,
            "hard_constraints": list(result.checks),
        }
        return {
            "trip_plan": plan_data,
            "constraint_result": constraint_data,
            "node": "persist_plan",
        }

    async def persist_plan(state: AgentState) -> dict[str, Any]:
        plan_data = state.get("trip_plan", {})
        if not plan_data or state.get("trip_id") is None:
            return {"node": "route_after_plan"}
        await _persist_plan(deps, state, plan_data)
        return {"node": "route_after_plan"}

    async def approval_persist(state: AgentState) -> dict[str, Any]:
        intent_name = state.get("intent")
        trip_id = state.get("trip_id")
        if trip_id is None:
            return {"missing_slots": ["trip_id"], "node": "report"}
        ctx = _context(state, deps.clock)
        if intent_name == "reservation":
            plan = state.get("trip_plan", {})
            items = plan.get("items", [])
            if not items or not plan.get("feasible"):
                return {
                    "errors": [{"id": "no-slot", "code": "INVENTORY_NOT_ENOUGH"}],
                    "node": "report",
                }
            item = items[0]
            parameters = {
                "trip_id": str(trip_id),
                "slot_id": item["slot_id"],
                "quantity": int(plan["party_size"]),
                "reason": "用户请求预约",
            }
            action_type = "propose_reservation"
        elif intent_name == "cancellation":
            ids = re.findall(r"[0-9a-fA-F-]{36}", state.get("message", ""))
            reservation_id = UUID(ids[0]) if ids else None
            if reservation_id is None:
                ctx_reservations = await deps.tools.invoke(
                    "get_reservation_history", {"trip_id": str(trip_id)}, ctx
                )
                if not ctx_reservations:
                    return {
                        "errors": [{"id": "no-reservation", "code": "NOT_FOUND"}],
                        "node": "report",
                    }
                reservation_id = UUID(str(ctx_reservations[0]["reservation_id"]))
            parameters = {"reservation_id": str(reservation_id), "reason": "用户请求取消"}
            action_type = "propose_cancellation"
        else:
            return {"node": "report"}
        action_id, parameters_hash = await deps.reservations.create_candidate_action(
            ctx, run_id=state["run_id"], action_type=action_type, parameters=parameters
        )
        await _remember_conversation(state, deps, action_ids=[str(action_id)])
        action = {
            "action_id": str(action_id),
            "action_type": action_type,
            "parameters_hash": parameters_hash,
            "status": "PROPOSED",
            "requires_confirmation": True,
        }
        return {
            "pending_actions": [action],
            "awaiting_action_id": action_id,
            "node": "interrupt_wait",
        }

    async def interrupt_wait(state: AgentState) -> dict[str, Any]:
        action_id = state.get("awaiting_action_id")
        if action_id is None:
            raise DomainError("CHECKPOINT_SCHEMA_MISMATCH", "Approval action ID is missing", 409)
        async with deps.sessions() as session:
            result = await session.execute(
                text("""SELECT a.action_id,a.action_type,a.parameters_hash,a.status,
                    c.status AS confirmation_status,c.expires_at FROM tripmate.agent_action a
                    LEFT JOIN tripmate.action_confirmation c ON c.tenant_id=a.tenant_id AND c.action_id=a.action_id
                    WHERE a.tenant_id=:tenant AND a.run_id=:run AND a.action_id=:action"""),
                {"tenant": state["tenant_id"], "run": state["run_id"], "action": action_id},
            )
            action = result.mappings().one_or_none()
        if action is None:
            raise DomainError(
                "CHECKPOINT_SCHEMA_MISMATCH", "Persisted approval action is missing", 409
            )
        decision = interrupt(
            {
                "action_id": str(action["action_id"]),
                "action_type": action["action_type"],
                "parameters_hash": action["parameters_hash"],
                "status": action["status"],
                "confirmation_status": action["confirmation_status"],
                "expires_at": action["expires_at"].isoformat() if action["expires_at"] else None,
            }
        )
        return {"approval_response": decision, "node": "verify_action"}

    async def verify_action(state: AgentState) -> dict[str, Any]:
        action_id = state.get("awaiting_action_id")
        async with deps.sessions() as session:
            result = await session.execute(
                text("""SELECT a.status,a.result_json,r.status AS reservation_status,r.reservation_id
                    FROM tripmate.agent_action a LEFT JOIN tripmate.reservation r
                      ON r.tenant_id=a.tenant_id AND r.approved_action_id=a.action_id
                    WHERE a.tenant_id=:tenant AND a.run_id=:run AND a.action_id=:action"""),
                {"tenant": state["tenant_id"], "run": state["run_id"], "action": action_id},
            )
            row = result.mappings().one_or_none()
        if row is None:
            raise DomainError("NOT_FOUND", "Approved action disappeared", 404)
        return {
            "approval_response": {
                **(state.get("approval_response") or {}),
                "persisted_action_status": row["status"],
                "reservation_status": row["reservation_status"],
                "reservation_id": str(row["reservation_id"]) if row["reservation_id"] else None,
            },
            "node": "report",
        }

    async def report(state: AgentState) -> dict[str, Any]:
        pending = state.get("pending_actions", [])
        if state.get("errors"):
            answer = "无法确认当前安排，请检查行程和实时数据后重试。"
        elif state.get("missing_slots"):
            answer = "请先提供要使用的行程编号。"
        elif pending:
            answer = "已生成待确认操作；当前尚未创建最终预约。"
        elif state.get("approval_response", {}).get("persisted_action_status") == "REJECTED":
            answer = "已记录拒绝，原操作不会再次提交。"
        elif state.get("approval_response", {}).get("reservation_status") == "HELD":
            answer = "库存已短期预占，仍需提交预约确认；预占会在有效期后自动释放。"
        elif state.get("trip_plan", {}).get("feasible"):
            answer = "行程方案已生成并通过硬约束校验。"
        elif state.get("intent") == "knowledge" and state.get("citations"):
            answer = "检索到以下静态政策资料；实时天气、价格、库存和订单状态请以实时查询为准。"
        else:
            answer = "没有足够的实时数据生成可执行方案。"
        result = {
            "run_id": str(state["run_id"]),
            "intent": state.get("intent"),
            "answer": answer,
            "trip_plan": state.get("trip_plan"),
            "constraint_result": state.get("constraint_result"),
            "citations": state.get("citations", []),
            "pending_actions": pending,
            "approval_response": state.get("approval_response"),
            "errors": state.get("errors", []),
        }
        await _remember_conversation(
            state,
            deps,
            action_ids=[
                str(item["action_id"])
                for item in pending
                if isinstance(item, dict) and item.get("action_id")
            ],
        )
        return {"result": result, "node": "done"}

    builder = StateGraph(AgentState)
    builder.add_node("load_context", load_context)
    builder.add_node("intent", intent)
    builder.add_node("memory_load", memory_load)
    builder.add_node("rag_retrieve", rag_retrieve)
    builder.add_node("realtime_query", realtime_query)
    builder.add_node("plan", plan)
    builder.add_node("constraints", constraints)
    builder.add_node("persist_plan", persist_plan)
    builder.add_node("approval_persist", approval_persist)
    builder.add_node("interrupt_wait", interrupt_wait)
    builder.add_node("verify_action", verify_action)
    builder.add_node("report", report)
    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "intent")
    builder.add_edge("intent", "memory_load")
    builder.add_edge("memory_load", "rag_retrieve")
    builder.add_edge("rag_retrieve", "realtime_query")
    builder.add_edge("realtime_query", "plan")
    builder.add_edge("plan", "constraints")
    builder.add_conditional_edges(
        "constraints", _after_constraints, {"persist_plan": "persist_plan", "report": "report"}
    )
    builder.add_conditional_edges(
        "persist_plan", _after_plan, {"approval_persist": "approval_persist", "report": "report"}
    )
    builder.add_edge("approval_persist", "interrupt_wait")
    builder.add_edge("interrupt_wait", "verify_action")
    builder.add_edge("verify_action", "report")
    builder.add_edge("report", END)
    return builder.compile(checkpointer=checkpointer)


def _after_constraints(state: AgentState) -> str:
    if state.get("constraint_result", {}).get("feasible"):
        return "persist_plan"
    return "report"


async def _remember_conversation(
    state: AgentState, deps: GraphDependencies, *, action_ids: list[str] | None = None
) -> None:
    conversation_id = state.get("conversation_id")
    if conversation_id is None:
        return
    plan = state.get("trip_plan") or {}
    trip = (state.get("trip_context") or {}).get("trip", {})
    structured_constraints = {
        key: value
        for source in (trip, plan)
        for key, value in source.items()
        if key in {"destination", "start_date", "end_date", "party_size", "timezone"}
        and isinstance(value, str | int | float | bool)
    }
    items = plan.get("items", []) if isinstance(plan, dict) else []
    selected_slots = [
        str(item["slot_id"])
        for item in items
        if isinstance(item, dict) and item.get("slot_id")
    ]
    confirmed_slots = []
    approval = state.get("approval_response") or {}
    if approval.get("reservation_status") == "CONFIRMED":
        confirmed_slots = selected_slots
    citations = state.get("citations", [])
    try:
        await deps.short_term_memory.merge(
            _context(state, deps.clock),
            conversation_id,
            MemoryPatch(
                trip_id=state.get("trip_id"),
                structured_constraints=structured_constraints,
                selected_slot_ids=selected_slots,
                confirmed_slot_ids=confirmed_slots,
                unresolved_slots=state.get("missing_slots", []),
                citation_ids=[
                    str(item["chunk_id"])
                    for item in citations
                    if isinstance(item, dict) and item.get("chunk_id")
                ],
                recent_run_ids=[str(state["run_id"])],
                recent_action_ids=action_ids or [],
                message_id=state.get("message_id"),
                message_hash=state.get("message_hash"),
            ),
        )
    except Exception:
        logger.warning("short_term_memory_write_failed", extra={"run_id": str(state["run_id"])})


def _after_plan(state: AgentState) -> str:
    if state.get("intent") in {"reservation", "cancellation"} and state.get(
        "constraint_result", {}
    ).get("feasible"):
        return "approval_persist"
    return "report"


def _context(state: AgentState, clock: Clock) -> RequestContext:
    now = clock.now()
    return RequestContext(
        request_id=f"run:{state['run_id']}",
        trace_id=f"run:{state['run_id']}",
        tenant_id=state["tenant_id"],
        user_id=state["user_id"],
        roles=("user",),
        run_id=state["run_id"],
        thread_id=state["thread_id"],
        lease_owner=state.get("lease_owner"),
        fencing_version=state.get("fencing_version"),
        trip_id=state.get("trip_id"),
        deadline=now + timedelta(minutes=2),
        source="agent",
    )


async def _persist_plan(deps: GraphDependencies, state: AgentState, plan: dict[str, Any]) -> None:
    trip_id = state["trip_id"]
    now = deps.clock.now()
    status = "READY" if plan.get("feasible") else "INFEASIBLE"
    plan_hash = sha256_hex(canonical_json(plan))
    event_id = uuid5(state["run_id"], f"trip-plan:{plan_hash}")
    async with deps.sessions() as session, session.begin():
        lease = await session.scalar(
            text("""SELECT 1 FROM tripmate.agent_run WHERE tenant_id=:tenant AND run_id=:run
                AND user_id=:user AND status='RUNNING' AND lease_owner=:owner
                AND fencing_version=:fence AND lease_until>:now FOR UPDATE"""),
            {
                "tenant": state["tenant_id"],
                "run": state["run_id"],
                "user": state["user_id"],
                "owner": state.get("lease_owner"),
                "fence": state.get("fencing_version"),
                "now": now,
            },
        )
        if lease is None:
            raise DomainError("STALE_LEASE", "Agent run lease is no longer current", 409, True)
        published_plan = await session.scalar(
            text("SELECT 1 FROM tripmate.outbox_event WHERE tenant_id=:tenant AND event_id=:event"),
            {"tenant": state["tenant_id"], "event": event_id},
        )
        if published_plan is not None:
            return
        trip_result = await session.execute(
            text("""SELECT status,version FROM tripmate.trip WHERE tenant_id=:tenant AND trip_id=:trip
                AND owner_user_id=:user FOR UPDATE"""),
            {"tenant": state["tenant_id"], "trip": trip_id, "user": state["user_id"]},
        )
        trip = trip_result.mappings().one_or_none()
        if trip is None:
            raise DomainError("NOT_FOUND", "Trip was not found", 404)
        if trip["status"] == "DRAFT":
            await session.execute(
                text("""UPDATE tripmate.trip SET status='PLANNING',version=version+1,updated_at=:now
                    WHERE tenant_id=:tenant AND trip_id=:trip AND status='DRAFT'"""),
                {"now": now, "tenant": state["tenant_id"], "trip": trip_id},
            )
        await session.execute(
            text("""DELETE FROM tripmate.trip_item WHERE tenant_id=:tenant AND trip_id=:trip
                AND reservation_id IS NULL"""),
            {"tenant": state["tenant_id"], "trip": trip_id},
        )
        existing_max = await session.scalar(
            text(
                "SELECT COALESCE(MAX(sequence_no),0) FROM tripmate.trip_item WHERE tenant_id=:tenant AND trip_id=:trip"
            ),
            {"tenant": state["tenant_id"], "trip": trip_id},
        )
        for index, item in enumerate(plan.get("items", []), start=int(existing_max or 0) + 1):
            window = item["window"]
            cost = item["cost"]
            await session.execute(
                text("""INSERT INTO tripmate.trip_item
                    (trip_item_id,tenant_id,trip_id,sequence_no,attraction_id,slot_id,start_at,end_at,
                     cost,currency,constraint_status)
                    VALUES(:id,:tenant,:trip,:seq,:attraction,:slot,:start,:end,:cost,:currency,:constraint)"""),
                {
                    "id": deps.ids.new(),
                    "tenant": state["tenant_id"],
                    "trip": trip_id,
                    "seq": index,
                    "attraction": item.get("attraction_id"),
                    "slot": item.get("slot_id"),
                    "start": window["start_at"],
                    "end": window["end_at"],
                    "cost": Decimal(cost["amount"]),
                    "currency": cost["currency"],
                    "constraint": "PASS" if plan.get("feasible") else "FAIL",
                },
            )
        update = await session.execute(
            text("""UPDATE tripmate.trip SET status=:status,version=version+1,updated_at=:now
                WHERE tenant_id=:tenant AND trip_id=:trip AND status IN ('PLANNING','READY','INFEASIBLE')
                RETURNING version"""),
            {"status": status, "now": now, "tenant": state["tenant_id"], "trip": trip_id},
        )
        version = update.scalar_one_or_none()
        if version is None:
            raise DomainError("VERSION_CONFLICT", "Trip changed while saving its plan", 409)
        envelope = EventEnvelope(
            event_id=event_id,
            event_type="trip.updated.v1",
            event_version=1,
            occurred_at=now,
            tenant_id=state["tenant_id"],
            aggregate_type="trip",
            aggregate_id=trip_id,
            aggregate_version=int(version),
            request_id=f"run:{state['run_id']}",
            trace_id=f"run:{state['run_id']}",
            run_id=state["run_id"],
            producer="tripmate-agent",
            payload={"trip_id": str(trip_id), "status": status, "plan_hash": plan_hash},
        )
        await session.execute(
            text("""INSERT INTO tripmate.outbox_event
                (event_id,tenant_id,aggregate_type,aggregate_id,aggregate_version,event_type,event_version,payload,status)
                VALUES(:event,:tenant,'trip',:trip,:version,:type,1,CAST(:payload AS jsonb),'PENDING')
                ON CONFLICT(event_id) DO NOTHING"""),
            {
                "event": event_id,
                "tenant": state["tenant_id"],
                "trip": trip_id,
                "version": version,
                "type": envelope.event_type,
                "payload": json.dumps(envelope.model_dump(mode="json")),
            },
        )

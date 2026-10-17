import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field, StrictStr
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
# shortcut: cap plans at six items; raise it when itinerary size is budgeted and evaluated.
MAX_PLAN_ITEMS = 6
MAX_TOOL_SELECTION_STEPS = 3
SHAREABLE_PREFERENCE_KEYS = frozenset(
    {"walking_limit.max_minutes", "indoor_ratio.min", "travel_style.tags"}
)
SENSITIVE_PREFERENCE_PATTERN = re.compile(
    r"过敏|不吃|忌口|饮食限制|饮食偏好|不能吃|避免食用|素食|allerg|dietary|vegetarian|vegan|halal|kosher",
    re.IGNORECASE,
)


class IntentClassification(BaseModel):
    intent: Literal[
        "cancellation", "reservation", "waitlist", "reminder", "knowledge", "planning", "preference"
    ]


class SlotRanking(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_ids: list[StrictStr] = Field(min_length=1, max_length=100)


class ReadOnlyToolSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_id: StrictStr | None
    arguments: dict[str, Any]


def _model_context_message(message: str) -> str:
    parts = re.split(r"([，,；;。.!?\n])", message)
    for index in range(0, len(parts), 2):
        if SENSITIVE_PREFERENCE_PATTERN.search(parts[index]):
            parts[index] = ""
    return "".join(parts).strip()


async def _run_readonly_tool_loop(
    selector: Any | None,
    gateway: ToolGateway,
    message: str,
    ctx: RequestContext,
    *,
    timeout_seconds: float,
    invoke: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None,
) -> list[str]:
    if selector is None:
        return []
    catalog = gateway.readonly_tool_schemas()
    allowed = {item["tool_id"] for item in catalog}
    observations: list[dict[str, Any]] = []
    called: list[str] = []
    try:
        for _ in range(MAX_TOOL_SELECTION_STEPS):
            raw = await asyncio.wait_for(
                selector.ainvoke(
                    {
                        "message": _model_context_message(message),
                        "tools": json.dumps(catalog, ensure_ascii=False),
                        "observations": json.dumps(observations, ensure_ascii=False, default=str),
                    }
                ),
                timeout=timeout_seconds,
            )
            choice = ReadOnlyToolSelection.model_validate(raw)
            if choice.tool_id is None:
                if choice.arguments:
                    raise ValueError("no-tool response must have empty arguments")
                break
            if choice.tool_id not in allowed:
                raise ValueError("model selected a tool outside the read-only registry")
            result = (
                await invoke(choice.tool_id, choice.arguments)
                if invoke is not None
                else await gateway.invoke_readonly(choice.tool_id, choice.arguments, ctx)
            )
            observations.append({"tool_id": choice.tool_id, "result": result})
            called.append(choice.tool_id)
        return called
    except Exception as exc:
        logger.warning(
            "readonly_tool_selection_failed",
            extra={"run_id": str(ctx.run_id), "error_type": type(exc).__name__},
        )
        return []


def _rule_intent(message: str) -> str:
    message = message.casefold()
    if any(token in message for token in ("取消", "退订", "cancel")):
        return "cancellation"
    if any(token in message for token in ("候补", "等位", "waitlist")):
        return "waitlist"
    if any(token in message for token in ("提醒", "reminder")):
        return "reminder"
    if any(token in message for token in ("预约", "预订", "订票", "reserve", "book")):
        return "reservation"
    if _is_preference_intent_request(message):
        return "preference"
    if any(token in message for token in ("政策", "规则", "退改", "儿童", "开放时间", "几点")):
        return "knowledge"
    return "planning"


async def _classify_intent(message: str, classifier: Any | None, run_id: Any) -> str:
    if _is_preference_intent_request(message):
        return "preference"
    if classifier is None:
        return _rule_intent(message)
    try:
        result = await classifier.ainvoke({"message": _model_context_message(message)})
        return IntentClassification.model_validate(result).intent
    except Exception as exc:
        logger.warning(
            "llm_intent_classification_failed",
            extra={"run_id": str(run_id), "error_type": type(exc).__name__},
        )
        return _rule_intent(message)


def _is_explicit_preference_request(message: str) -> bool:
    text = message.casefold()
    asks_to_remember = any(token in text for token in ("记住", "保存偏好", "以后记得", "remember"))
    names_preference = any(
        token in text for token in ("步行", "走路", "室内", "户外", "博物馆", "风格", "喜欢")
    )
    return asks_to_remember and names_preference


def _is_preference_intent_request(message: str) -> bool:
    text = message.casefold()
    return _is_explicit_preference_request(text) or any(
        token in text for token in ("过敏", "不吃", "素食", "饮食限制", "allergy", "dietary")
    )


def _explicit_preference_candidate(message: str) -> dict[str, Any] | None:
    text = message.casefold()
    if not _is_explicit_preference_request(text):
        return None
    if any(token in text for token in ("步行", "走路")):
        minutes = re.search(r"(\d{1,4})\s*(?:分钟|分|minutes?)", text)
        if minutes:
            value = int(minutes.group(1))
            if 1 <= value <= 1440:
                return {"key": "walking_limit.max_minutes", "value": value}
            return None
    if "室内" in text or "户外" in text:
        tags = [tag for token, tag in (("室内", "indoor"), ("户外", "outdoor")) if token in text]
        return {"key": "travel_style.tags", "value": tags}
    tags = [
        tag
        for token, tag in (
            ("博物馆", "museum"),
            ("自然", "nature"),
            ("文化", "culture"),
            ("历史", "history"),
            ("美食", "food"),
            ("休闲", "relaxing"),
        )
        if token in text
    ]
    return {"key": "travel_style.tags", "value": tags} if tags else None


def _select_cancellation_reservation(
    reservations: Sequence[dict[str, Any]], requested_id: UUID | None
) -> tuple[str | None, list[dict[str, Any]]]:
    confirmed = [item for item in reservations if item.get("status") == "CONFIRMED"]
    if requested_id is not None:
        selected = next(
            (item for item in confirmed if str(item["reservation_id"]) == str(requested_id)),
            None,
        )
        return (str(requested_id), []) if selected is not None else (None, [])
    if len(confirmed) == 1:
        return str(confirmed[0]["reservation_id"]), []
    if len(confirmed) > 1:
        options = [
            {
                "reservation_id": str(item["reservation_id"]),
                "slot_id": str(item["slot_id"]),
                "status": str(item["status"]),
            }
            for item in confirmed
        ]
        return None, options
    return None, []


async def _rank_candidate_slots(
    slots: Sequence[Any],
    message: str,
    ranker: Any | None,
    run_id: Any,
    *,
    preferences: dict[str, Any] | None = None,
    weather: dict[str, Any] | None = None,
    cases: list[str] | None = None,
) -> tuple[list[Any], bool]:
    if ranker is None or len(slots) < 2:
        return list(slots), False
    by_id = {str(slot["slot_id"]): slot for slot in slots}
    try:
        candidates = [
            {
                "slot_id": slot_id,
                "name": slot["name"],
                "category": slot["category"],
                "start_at": slot["start_at"].isoformat(),
                "end_at": slot["end_at"].isoformat(),
            }
            for slot_id, slot in by_id.items()
        ]
        result = await ranker.ainvoke(
            {
                "message": _model_context_message(message),
                "candidates": json.dumps(candidates, ensure_ascii=False),
                "preferences": json.dumps(preferences or {}, ensure_ascii=False, default=str),
                "weather": json.dumps(weather or {}, ensure_ascii=False, default=str),
                "cases": json.dumps(cases or [], ensure_ascii=False),
            }
        )
        ranking = SlotRanking.model_validate(result)
        if (
            len(ranking.slot_ids) != len(by_id)
            or len(set(ranking.slot_ids)) != len(ranking.slot_ids)
            or set(ranking.slot_ids) != set(by_id)
        ):
            raise ValueError("slot ranking must contain every candidate ID exactly once")
        return [by_id[slot_id] for slot_id in ranking.slot_ids], True
    except Exception as exc:
        logger.warning(
            "llm_slot_ranking_failed",
            extra={"run_id": str(run_id), "error_type": type(exc).__name__},
        )
        return list(slots), False


def _shareable_preferences(preferences: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        str(item["preference_key"]): item["value_json"]
        for item in preferences
        if item.get("preference_key") in SHAREABLE_PREFERENCE_KEYS
    }


def _safe_private_case_context(cases: Sequence[dict[str, Any]]) -> list[str]:
    safe: list[str] = []
    for case in cases:
        content = str(case.get("content_redacted", ""))[:300]
        key, separator, _value = content.partition(":")
        if (
            separator
            and key.strip() in SHAREABLE_PREFERENCE_KEYS
            and not SENSITIVE_PREFERENCE_PATTERN.search(content)
        ):
            safe.append(content)
    return safe[:5]


def _planning_request_overrides(message: str) -> dict[str, Any]:
    text = message.casefold()
    values: dict[str, Any] = {}
    if any(token in text for token in ("优先室内", "尽量室内", "多安排室内", "prefer indoor")):
        values["indoor_ratio.min"] = 1.0
    if any(token in text for token in ("少走路", "少步行", "短步行", "less walking", "short walk")):
        values["walking_limit.max_minutes"] = 30
    return values


def _exclude_explicitly_avoided(
    candidates: Sequence[Any], message: str, confirmed_slot_ids: frozenset[str]
) -> list[Any]:
    text = message.casefold()
    if not any(
        token in text
        for token in (
            "不要",
            "不去",
            "不想去",
            "不想安排",
            "换掉",
            "移除",
            "别安排",
            "avoid",
            "skip",
        )
    ):
        return list(candidates)
    return [
        slot
        for slot in candidates
        if str(slot["slot_id"]) in confirmed_slot_ids or str(slot["name"]).casefold() not in text
    ]


def _plan_candidate_orders(candidates: Sequence[Any]) -> list[tuple[str, list[Any]]]:
    ranked = list(candidates)
    rank = {str(slot["slot_id"]): index for index, slot in enumerate(ranked)}
    return [
        ("推荐方案", ranked),
        (
            "低价格",
            sorted(ranked, key=lambda slot: (Decimal(slot["price"]), rank[str(slot["slot_id"])])),
        ),
        (
            "偏室内",
            sorted(
                ranked,
                key=lambda slot: (
                    0 if slot.get("indoor") is True else 1 if slot.get("indoor") is False else 2,
                    rank[str(slot["slot_id"])],
                ),
            ),
        ),
    ]


def _weather_risk(weather: dict[str, Any]) -> bool | None:
    known = False
    for day in weather.values():
        if not isinstance(day, dict) or day.get("status") != "OK":
            continue
        daily = day.get("daily", {})
        probability = daily.get("precipitation_probability_max", [])
        if probability:
            known = True
            if float(probability[0]) >= 50:
                return True
        codes = daily.get("weather_code", [])
        if codes:
            known = True
            if int(codes[0]) >= 51:
                return True
    return False if known else None


def _score_plan_preferences(
    items: Sequence[dict[str, Any]],
    preferences: dict[str, Any],
    weather: dict[str, Any],
) -> tuple[dict[str, Any], float]:
    walk_minutes = sum(
        int(item["route_from_previous_minutes"])
        for item in items
        if item.get("route_from_previous_minutes") is not None
    )
    walking_limit = preferences.get("walking_limit.max_minutes")
    walking_score = None
    if isinstance(walking_limit, int | float) and walking_limit > 0:
        walking_score = round(max(0.0, 100.0 - max(0, walk_minutes - walking_limit) * 100 / walking_limit), 2)

    known_indoor = [item["indoor"] for item in items if item.get("indoor") is not None]
    indoor_ratio = (
        sum(bool(value) for value in known_indoor) / len(known_indoor) if known_indoor else None
    )
    indoor_target = preferences.get("indoor_ratio.min")
    indoor_score = None
    if indoor_ratio is not None and isinstance(indoor_target, int | float):
        indoor_score = (
            100.0
            if indoor_target <= 0 or indoor_ratio >= indoor_target
            else round(indoor_ratio * 100 / indoor_target, 2)
        )

    rainy = _weather_risk(weather)
    outdoor_ratio = (
        sum(value is False for value in known_indoor) / len(known_indoor) if known_indoor else None
    )
    weather_score = (
        round((1.0 - outdoor_ratio) * 100, 2)
        if rainy is True and outdoor_ratio is not None
        else None
    )
    scores = [score for score in (walking_score, indoor_score, weather_score) if score is not None]
    overall = round(sum(scores) / len(scores), 2) if scores else 100.0
    return (
        {
            "walking_minutes": walk_minutes,
            "walking_limit_minutes": walking_limit,
            "walking_score": walking_score,
            "indoor_ratio": round(indoor_ratio, 3) if indoor_ratio is not None else None,
            "indoor_ratio_min": indoor_target,
            "indoor_score": indoor_score,
            "rain_risk": rainy,
            "weather_score": weather_score,
            "overall_score": overall,
        },
        overall,
    )


def _select_plan_slots(
    ranked: Sequence[Any],
    *,
    party_size: int,
    budget_limit: Decimal | None,
    confirmed_slot_ids: frozenset[str],
    confirmed_costs: dict[str, Decimal],
) -> list[Any]:
    selected = [
        slot for slot in ranked if str(slot["slot_id"]) in confirmed_slot_ids
    ]
    selected_ids = {str(slot["slot_id"]) for slot in selected}
    total = sum(
        (
            confirmed_costs.get(str(slot["slot_id"]), Decimal(slot["price"]) * party_size)
            for slot in selected
        ),
        Decimal("0"),
    )
    for slot in ranked:
        slot_id = str(slot["slot_id"])
        if slot_id in selected_ids:
            continue
        if len(selected) >= MAX_PLAN_ITEMS:
            break
        start, end = slot["start_at"], slot["end_at"]
        if any(start < item["end_at"] and item["start_at"] < end for item in selected):
            continue
        item_cost = Decimal(slot["price"]) * party_size
        if budget_limit is not None and total + item_cost > budget_limit:
            continue
        selected.append(slot)
        selected_ids.add(slot_id)
        total += item_cost
    return sorted(selected, key=lambda slot: (slot["start_at"], str(slot["slot_id"])))


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
    intent_classifier: Any | None = None
    slot_ranker: Any | None = None
    tool_selector: Any | None = None


def build_graph(deps: GraphDependencies, checkpointer: Any) -> Any:
    async def invoke_tool(
        state: AgentState,
        tool_id: str,
        arguments: dict[str, Any],
        *,
        read_only: bool = False,
    ) -> Any:
        ctx = _context(state, deps.clock)
        await deps.runs.append_run_event(
            ctx, state["run_id"], "tool_start", {"tool_id": tool_id}
        )
        try:
            result = (
                await deps.tools.invoke_readonly(tool_id, arguments, ctx)
                if read_only
                else await deps.tools.invoke(tool_id, arguments, ctx)
            )
        except Exception as exc:
            code = exc.code if isinstance(exc, DomainError) else "DEPENDENCY_UNAVAILABLE"
            await deps.runs.append_run_event(
                ctx,
                state["run_id"],
                "tool_end",
                {"tool_id": tool_id, "status": "error", "error_code": code},
            )
            raise
        await deps.runs.append_run_event(
            ctx, state["run_id"], "tool_end", {"tool_id": tool_id, "status": "ok"}
        )
        return result

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
        classified = await _classify_intent(
            state.get("message", ""), deps.intent_classifier, state.get("run_id")
        )
        missing = [] if state.get("trip_id") or classified == "preference" else ["trip_id"]
        return {"intent": classified, "missing_slots": missing, "node": "memory_load"}

    async def memory_load(state: AgentState) -> dict[str, Any]:
        ctx = _context(state, deps.clock)
        preferences = await deps.preferences.list_for_user(ctx)
        case_context: list[str] = []
        if hasattr(deps.knowledge, "search_private_cases"):
            try:
                cases = await deps.knowledge.search_private_cases(
                    ctx,
                    state.get("message", ""),
                    current_generation=max(
                        (int(item["generation"]) for item in preferences), default=1
                    ),
                    limit=5,
                )
                case_context = _safe_private_case_context(cases)
            except Exception:
                logger.warning("private_case_recall_failed", extra={"run_id": str(state["run_id"])})
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
            "planning_preferences": {
                **_shareable_preferences(preferences),
                **_planning_request_overrides(state.get("message", "")),
            },
            "private_case_context": case_context,
            "conversation_memory": memory or {},
            "trip_id": trip_id,
            "missing_slots": [] if trip_id else state.get("missing_slots", ["trip_id"]),
            "node": "rag_retrieve",
        }

    async def rag_retrieve(state: AgentState) -> dict[str, Any]:
        ctx = _context(state, deps.clock)
        query = state.get("message", "")
        citations = await deps.knowledge.search(ctx, query, top_k=deps.settings.rag_top_k)
        await deps.runs.append_run_event(
            ctx,
            state["run_id"],
            "citation",
            {
                "citations": [
                    {
                        "chunk_id": citation.chunk_id,
                        "document_id": citation.document_id,
                        "content_sha256": citation.content_sha256,
                        "index_version": citation.index_version,
                    }
                    for citation in citations
                ]
            },
        )
        return {
            "citations": [citation.model_dump(mode="json") for citation in citations],
            "node": "realtime_query",
        }

    async def realtime_query(state: AgentState) -> dict[str, Any]:
        facts: dict[str, Any] = {}
        trip_id = state.get("trip_id")
        if trip_id is not None:
            facts["trip_context"] = await invoke_tool(
                state, "get_trip_context", {"trip_id": str(trip_id)}
            )
        message = state.get("message", "").casefold()
        if trip_id is not None and any(
            word in message for word in ("天气", "下雨", "雨天", "weather", "rain")
        ):
            trip = facts.get("trip_context", {}).get("trip", {})
            weather: dict[str, Any] = {}
            day = trip.get("start_date")
            last_day = trip.get("end_date", day)
            if isinstance(day, str):
                day = date.fromisoformat(day)
            if isinstance(last_day, str):
                last_day = date.fromisoformat(last_day)
            if isinstance(day, date) and isinstance(last_day, date):
                while day <= last_day:
                    key = day.isoformat()
                    weather[key] = await invoke_tool(
                        state,
                        "get_weather",
                        {"city": trip.get("destination", ""), "visit_date": key},
                    )
                    day += timedelta(days=1)
            facts["weather"] = weather
        return {
            "realtime_facts": facts,
            "trip_context": facts.get("trip_context", {}),
            "node": "tool_selection",
        }

    async def tool_selection(state: AgentState) -> dict[str, Any]:
        ctx = _context(state, deps.clock)

        async def invoke_selected(tool_id: str, arguments: dict[str, Any]) -> Any:
            return await invoke_tool(state, tool_id, arguments, read_only=True)

        selected = await _run_readonly_tool_loop(
            deps.tool_selector,
            deps.tools,
            state.get("message", ""),
            ctx,
            timeout_seconds=deps.settings.llm_timeout_seconds,
            invoke=invoke_selected,
        )
        return {"selected_tool_ids": selected, "node": "plan"}

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
                    s.status AS slot_status,a.attraction_id,a.name,a.category,a.latitude,a.longitude,a.indoor,
                    i.available_quantity,i.version AS inventory_version,i.status AS inventory_status
                    FROM tripmate.activity_slot s JOIN tripmate.attraction a
                      ON a.tenant_id=s.tenant_id AND a.attraction_id=s.attraction_id
                    JOIN tripmate.inventory i ON i.tenant_id=s.tenant_id AND i.slot_id=s.slot_id
                    WHERE s.tenant_id=:tenant AND (
                      (s.start_at>=:start_at AND s.start_at<:end_at
                        AND s.status='ACTIVE' AND i.status='ACTIVE' AND a.status='ACTIVE')
                      OR EXISTS (SELECT 1 FROM tripmate.reservation r
                        WHERE r.tenant_id=s.tenant_id AND r.slot_id=s.slot_id
                          AND r.trip_id=:trip AND r.status='CONFIRMED'))
                    ORDER BY CASE WHEN EXISTS (SELECT 1 FROM tripmate.reservation r
                      WHERE r.tenant_id=s.tenant_id AND r.slot_id=s.slot_id
                        AND r.trip_id=:trip AND r.status='CONFIRMED') THEN 0 ELSE 1 END,
                      s.start_at,s.slot_id LIMIT 100"""),
                {"tenant": tenant_id, "trip": trip_id, "start_at": start, "end_at": end},
            )
            slots = list(slot_result.mappings())
            confirmed = await session.execute(
                text("""SELECT slot_id,SUM(quantity) AS quantity,
                    SUM(price_snapshot*quantity) AS total_cost,MIN(currency) AS currency
                    FROM tripmate.reservation WHERE tenant_id=:tenant
                    AND trip_id=:trip AND status='CONFIRMED' GROUP BY slot_id"""),
                {"tenant": tenant_id, "trip": trip_id},
            )
            confirmed_rows = list(confirmed.mappings())
            confirmed_by_slot = {str(row["slot_id"]): row for row in confirmed_rows}
            confirmed_slot_ids = frozenset(confirmed_by_slot)
        if state.get("intent") == "waitlist":
            waitlist_candidates = [
                {
                    "slot_id": str(slot["slot_id"]),
                    "name": slot["name"],
                    "category": slot["category"],
                    "start_at": slot["start_at"].isoformat(),
                    "end_at": slot["end_at"].isoformat(),
                    "price": str(slot["price"]),
                    "currency": str(slot["currency"]).strip(),
                    "available_quantity": int(slot["available_quantity"]),
                }
                for slot in slots
                if str(slot["slot_id"]) not in confirmed_slot_ids
                and int(slot["available_quantity"]) < int(trip["party_size"])
                and slot["slot_status"] == "ACTIVE"
                and slot["inventory_status"] == "ACTIVE"
            ]
            return {
                "trip_plan": {},
                "waitlist_candidates": waitlist_candidates,
                "node": "constraints",
            }
        if state.get("intent") == "reminder":
            return {"trip_plan": {}, "node": "constraints"}
        citations = state.get("citations", [])
        candidates = [
            slot
            for slot in slots
            if int(slot["available_quantity"]) >= int(trip["party_size"])
            or str(slot["slot_id"]) in confirmed_slot_ids
        ]
        candidates = _exclude_explicitly_avoided(
            candidates, state.get("message", ""), confirmed_slot_ids
        )
        budget_limit = Decimal(trip["budget_limit"]) if trip["budget_limit"] is not None else None
        ranked_candidates, ranking_used = await _rank_candidate_slots(
            candidates,
            state.get("message", ""),
            deps.slot_ranker,
            state.get("run_id"),
            preferences=state.get("planning_preferences", {}),
            weather=state.get("realtime_facts", {}).get("weather", {}),
            cases=state.get("private_case_context", []),
        )
        confirmed_costs = {
            slot_id: Decimal(row["total_cost"])
            for slot_id, row in confirmed_by_slot.items()
        }
        opening_facts: dict[str, Any] = {}
        route_facts: dict[str, Any] = {}
        weather = state.get("realtime_facts", {}).get("weather", {})
        preferences = state.get("planning_preferences", {})
        options: list[dict[str, Any]] = []
        seen_slot_sets: set[tuple[str, ...]] = set()
        for name, order in _plan_candidate_orders(ranked_candidates):
            selected = _select_plan_slots(
                order,
                party_size=int(trip["party_size"]),
                budget_limit=budget_limit,
                confirmed_slot_ids=confirmed_slot_ids,
                confirmed_costs=confirmed_costs,
            )
            slot_set = tuple(sorted(str(slot["slot_id"]) for slot in selected))
            if slot_set in seen_slot_sets:
                continue
            seen_slot_sets.add(slot_set)
            items: list[ActivityPlanItem] = []
            inventory_facts: dict[str, InventoryFact] = {}
            open_slot_ids: set[str] = set()
            previous: Any | None = None
            for slot in selected:
                slot_id = str(slot["slot_id"])
                attraction_id = str(slot["attraction_id"])
                visit_date = slot["start_at"].astimezone(zone).date().isoformat()
                opening_key = f"{attraction_id}:{visit_date}"
                if opening_key not in opening_facts:
                    opening_facts[opening_key] = await invoke_tool(
                        state,
                        "get_opening_hours",
                        {
                            "attraction_id": attraction_id,
                            "visit_date": visit_date,
                            "timezone": trip["timezone"],
                        },
                    )
                opening = opening_facts[opening_key]
                available_windows = opening.get("available_windows", [])
                window_slot_ids = {
                    str(window["slot_id"])
                    for window in available_windows
                    if window.get("status") == "ACTIVE"
                }
                if opening.get("status") != "OK" or slot_id not in window_slot_ids:
                    if slot_id not in confirmed_slot_ids:
                        continue
                open_slot_ids.update(window_slot_ids)

                route_minutes = None
                if previous is not None:
                    route_key = f"{previous['attraction_id']}:{attraction_id}"
                    if route_key not in route_facts:
                        route_facts[route_key] = await invoke_tool(
                            state,
                            "estimate_route_time",
                            {
                                "origin_attraction_id": str(previous["attraction_id"]),
                                "destination_attraction_id": attraction_id,
                            },
                        )
                    route = route_facts[route_key]
                    if route.get("status") == "OK":
                        route_minutes = int(route["estimated_minutes"])
                        if previous["end_at"] + timedelta(minutes=route_minutes) > slot["start_at"]:
                            if slot_id not in confirmed_slot_ids:
                                continue
                    elif slot_id not in confirmed_slot_ids:
                        continue

                confirmed_row = confirmed_by_slot.get(slot_id)
                if confirmed_row is not None:
                    item_cost = Decimal(confirmed_row["total_cost"])
                    item_currency = str(confirmed_row["currency"]).strip()
                else:
                    item_cost = Decimal(slot["price"]) * int(trip["party_size"])
                    item_currency = slot["currency"].strip()
                items.append(
                    ActivityPlanItem(
                        sequence_no=len(items) + 1,
                        attraction_id=slot["attraction_id"],
                        slot_id=slot["slot_id"],
                        title=slot["name"],
                        window=TimeWindow(
                            start_at=slot["start_at"],
                            end_at=slot["end_at"],
                            timezone=trip["timezone"],
                        ),
                        cost=Money(
                            amount=item_cost.quantize(Decimal("0.01")),
                            currency=item_currency,
                        ),
                        route_from_previous_minutes=route_minutes,
                        indoor=slot["indoor"],
                        reservation_required=True,
                        inventory_version=int(slot["inventory_version"]),
                        activity_version=int(slot["activity_version"]),
                        constraint_status=ConstraintState.NOT_EVALUATED,
                        citation_ids=tuple(str(item["chunk_id"]) for item in citations),
                    )
                )
                inventory_facts[slot_id] = InventoryFact(
                    slot_id=slot_id,
                    available_quantity=int(slot["available_quantity"]),
                    inventory_version=int(slot["inventory_version"]),
                )
                previous = slot

            item_data = [item.model_dump(mode="json") for item in items]
            soft_scores, overall_score = _score_plan_preferences(item_data, preferences, weather)
            soft_summary = tuple(
                {"key": key, "value": value}
                for key, value in soft_scores.items()
                if value is not None
            )
            total = sum((item.cost.amount for item in items), Decimal("0"))
            currency = (
                trip["currency"].strip()
                if trip["currency"]
                else (items[0].cost.currency if items else "CNY")
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
                planner_version="tripmate-hybrid-comparison-v3",
                hard_constraints=(),
                soft_preferences=soft_summary,
            )
            result = validate_trip_plan(
                plan_model,
                inventory=inventory_facts,
                confirmed_slot_ids=confirmed_slot_ids,
                open_slot_ids=frozenset(open_slot_ids | set(confirmed_slot_ids)),
                budget_limit=budget_limit,
            )
            plan_data = plan_model.model_dump(mode="json")
            plan_data["feasible"] = result.feasible
            plan_data["hard_constraints"] = list(result.checks)
            options.append(
                {
                    "name": name,
                    "score": overall_score,
                    "tradeoffs": soft_scores,
                    "trip_plan": plan_data,
                    "inventory_facts": inventory_facts,
                    "open_slot_ids": open_slot_ids,
                }
            )
        if not options:
            raise DomainError("TRIP_INFEASIBLE", "No candidate plan could be generated", 422)
        chosen_index = max(
            range(len(options)),
            key=lambda index: (
                bool(options[index]["trip_plan"]["feasible"]),
                float(options[index]["score"]),
                -index,
            ),
        )
        chosen = options[chosen_index]
        plan_options = [
            {
                "name": option["name"],
                "score": option["score"],
                "tradeoffs": option["tradeoffs"],
                "trip_plan": option["trip_plan"],
            }
            for option in options
        ]
        plan_options.insert(0, plan_options.pop(chosen_index))
        return {
            "trip_plan": chosen["trip_plan"],
            "plan_options": plan_options,
            "inventory_facts": {
                key: {
                    "available_quantity": value.available_quantity,
                    "inventory_version": value.inventory_version,
                }
                for key, value in chosen["inventory_facts"].items()
            },
            "confirmed_slot_ids": sorted(confirmed_slot_ids),
            "open_slot_ids": sorted(chosen["open_slot_ids"]),
            "realtime_facts": {"opening_hours": opening_facts, "routes": route_facts},
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
        confirmed_slots = set(state.get("confirmed_slot_ids", []))
        context = state.get("trip_context", {})
        if not isinstance(context, dict):
            context = {}
        for item in context.get("items", []):
            if item.get("reservation_id"):
                confirmed_slots.add(str(item.get("slot_id")))
        result = validate_trip_plan(
            plan_model,
            inventory=inventory,
            confirmed_slot_ids=frozenset(confirmed_slots),
            open_slot_ids=frozenset(state.get("open_slot_ids", [])) | frozenset(confirmed_slots),
            budget_limit=(
                Decimal(context["budget_limit"])
                if context.get("budget_limit") is not None
                else None
            ),
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
        await _persist_plan(
            deps,
            state,
            plan_data,
            confirmed_slot_ids=frozenset(state.get("confirmed_slot_ids", [])),
        )
        return {"node": "route_after_plan"}

    async def approval_persist(state: AgentState) -> dict[str, Any]:
        intent_name = state.get("intent")
        trip_id = state.get("trip_id")
        if intent_name == "preference":
            candidate = _explicit_preference_candidate(state.get("message", ""))
            if candidate is None:
                return {"missing_slots": ["preference_value"], "node": "report"}
            ctx = _context(state, deps.clock)
            action = await invoke_tool(
                state,
                "save_user_preference",
                {
                    "scope": "personal",
                    **candidate,
                    "reason": "用户明确请求保存该偏好",
                },
            )
            await deps.runs.append_run_event(
                ctx,
                state["run_id"],
                "approval_required",
                {"action_id": action["action_id"], "action_type": action["action_type"]},
                event_key=f"action:{action['action_id']}:approval_required",
            )
            await _remember_conversation(state, deps, action_ids=[action["action_id"]])
            return {
                "pending_actions": [action],
                "awaiting_action_id": UUID(str(action["action_id"])),
                "missing_slots": [],
                "node": "interrupt_wait",
            }
        if trip_id is None:
            return {"missing_slots": ["trip_id"], "node": "report"}
        ctx = _context(state, deps.clock)
        if intent_name == "waitlist":
            options = state.get("waitlist_candidates", [])
            if not options:
                return {"errors": [{"id": "waitlist-unavailable", "code": "INVENTORY_NOT_ENOUGH"}], "node": "report"}
            message = state.get("message", "").casefold()
            requested_ids = set(re.findall(r"[0-9a-fA-F-]{36}", message))
            matching = [item for item in options if str(item["slot_id"]) in requested_ids]
            if not matching:
                matching = [item for item in options if str(item["name"]).casefold() in message]
            if len(matching) == 1:
                selected = matching[0]
            elif len(options) == 1:
                selected = options[0]
            else:
                return {"missing_slots": ["slot_id"], "clarification_options": options, "node": "report"}
            trip = (state.get("trip_context") or {}).get("trip", {})
            parameters = {
                "trip_id": str(trip_id),
                "slot_id": selected["slot_id"],
                "quantity": int(trip.get("party_size", 1)),
                "reason": "用户明确选择候补",
            }
            action_type = "propose_waitlist"
        elif intent_name == "reminder":
            trip = (state.get("trip_context") or {}).get("trip", {})
            start_date = trip.get("start_date")
            if isinstance(start_date, str):
                start_date = date.fromisoformat(start_date)
            if not isinstance(start_date, date):
                return {"errors": [{"id": "reminder-trip-date", "code": "INVALID_ARGUMENT"}], "node": "report"}
            timezone = str(trip.get("timezone", "UTC"))
            scheduled_at = datetime.combine(start_date - timedelta(days=1), time(9), ZoneInfo(timezone))
            if scheduled_at <= deps.clock.now():
                return {"errors": [{"id": "reminder-time-passed", "code": "INVALID_ARGUMENT"}], "node": "report"}
            message = state.get("message", "")
            reminder_type = "weather_check" if "天气" in message else "reservation" if "预约" in message else "departure"
            parameters = {
                "trip_id": str(trip_id),
                "reminder_type": reminder_type,
                "scheduled_at": scheduled_at.isoformat(),
                "timezone": timezone,
                "reason": "用户请求创建行程提醒",
            }
            action_type = "create_reminder"
        elif intent_name == "reservation":
            plan = state.get("trip_plan", {})
            items = plan.get("items", [])
            if not items or not plan.get("feasible"):
                return {
                    "errors": [{"id": "no-slot", "code": "INVENTORY_NOT_ENOUGH"}],
                    "node": "report",
                }
            confirmed = set(state.get("confirmed_slot_ids", []))
            item = next(
                (candidate for candidate in items if str(candidate.get("slot_id")) not in confirmed),
                None,
            )
            if item is None:
                return {"node": "report"}
            parameters = {
                "trip_id": str(trip_id),
                "slot_id": item["slot_id"],
                "quantity": int(plan["party_size"]),
                "reason": "用户请求预约",
            }
            action_type = "propose_reservation"
        elif intent_name == "cancellation":
            ids = re.findall(r"[0-9a-fA-F-]{36}", state.get("message", ""))
            requested_id = UUID(ids[0]) if ids else None
            history = await invoke_tool(
                state, "get_reservation_history", {"trip_id": str(trip_id)}
            )
            reservation_id, options = _select_cancellation_reservation(history, requested_id)
            if options:
                return {
                    "missing_slots": ["reservation_id"],
                    "clarification_options": options,
                    "node": "report",
                }
            if reservation_id is None:
                return {
                    "errors": [{"id": "reservation-not-found", "code": "NOT_FOUND"}],
                    "node": "report",
                }
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
        await deps.runs.append_run_event(
            ctx,
            state["run_id"],
            "approval_required",
            {"action_id": str(action_id), "action_type": action_type},
            event_key=f"action:{action_id}:approval_required",
        )
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
                text("""SELECT a.action_type,a.status,a.result_json,
                    r.status AS reservation_status,r.reservation_id
                    FROM tripmate.agent_action a LEFT JOIN tripmate.reservation r
                      ON r.tenant_id=a.tenant_id AND r.approved_action_id=a.action_id
                    WHERE a.tenant_id=:tenant AND a.run_id=:run AND a.action_id=:action"""),
                {"tenant": state["tenant_id"], "run": state["run_id"], "action": action_id},
            )
            row = result.mappings().one_or_none()
        if row is None:
            raise DomainError("NOT_FOUND", "Approved action disappeared", 404)
        pending = [
            {**item, "status": row["status"]}
            if isinstance(item, dict) and str(item.get("action_id")) == str(action_id)
            else item
            for item in state.get("pending_actions", [])
        ]
        return {
            "approval_response": {
                **(state.get("approval_response") or {}),
                "persisted_action_status": row["status"],
                "persisted_action_type": row["action_type"],
                "action_result": row["result_json"],
                "reservation_status": row["reservation_status"],
                "reservation_id": str(row["reservation_id"]) if row["reservation_id"] else None,
            },
            "pending_actions": pending,
            "node": "report",
        }

    async def report(state: AgentState) -> dict[str, Any]:
        pending = state.get("pending_actions", [])
        if state.get("errors"):
            answer = "无法确认当前安排，请检查行程和实时数据后重试。"
        elif "preference_value" in state.get("missing_slots", []):
            answer = "请明确说明要保存的步行时长、室内/户外或游玩风格偏好。"
        elif "slot_id" in state.get("missing_slots", []):
            answer = "请从候补候选中选择一个时段。"
        elif state.get("missing_slots"):
            answer = (
                "请提供要取消的预约编号。"
                if "reservation_id" in state.get("missing_slots", [])
                else "请先提供要使用的行程编号。"
            )
        elif (
            state.get("approval_response", {}).get("persisted_action_status") == "EXECUTED"
            and state.get("approval_response", {}).get("persisted_action_type") == "save_preference"
        ):
            answer = "已按确认结果保存偏好。"
        elif (
            state.get("approval_response", {}).get("persisted_action_status") == "EXECUTED"
            and state.get("approval_response", {}).get("persisted_action_type") == "propose_waitlist"
        ):
            answer = "已加入候补队列；候补不占用库存，库存有变化后仍需再次确认预约。"
        elif (
            state.get("approval_response", {}).get("persisted_action_status") == "EXECUTED"
            and state.get("approval_response", {}).get("persisted_action_type") == "create_reminder"
        ):
            answer = "提醒已创建，发送后会出现在站内通知收件箱。"
        elif state.get("approval_response", {}).get("persisted_action_status") == "REJECTED":
            answer = "已记录拒绝，原操作不会再次提交。"
        elif state.get("approval_response", {}).get("reservation_status") == "HELD":
            answer = "库存已短期预占，仍需提交预约确认；预占会在有效期后自动释放。"
        elif pending:
            answer = "已生成待确认操作；当前尚未创建最终预约。"
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
            "plan_options": state.get("plan_options", []),
            "constraint_result": state.get("constraint_result"),
            "citations": state.get("citations", []),
            "selected_tool_ids": state.get("selected_tool_ids", []),
            "pending_actions": pending,
            "clarification_options": state.get("clarification_options", []),
            "waitlist_candidates": state.get("waitlist_candidates", []),
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

    def with_start_event(name: str, node: Any) -> Any:
        async def invoke(state: AgentState) -> dict[str, Any]:
            await deps.runs.append_run_event(
                _context(state, deps.clock),
                state["run_id"],
                "node_started",
                {"node": name},
            )
            return cast(dict[str, Any], await node(state))

        return invoke

    builder = StateGraph(AgentState)
    builder.add_node("load_context", with_start_event("load_context", load_context))
    builder.add_node("intent", with_start_event("intent", intent))
    builder.add_node("memory_load", with_start_event("memory_load", memory_load))
    builder.add_node("rag_retrieve", with_start_event("rag_retrieve", rag_retrieve))
    builder.add_node("realtime_query", with_start_event("realtime_query", realtime_query))
    builder.add_node("tool_selection", with_start_event("tool_selection", tool_selection))
    builder.add_node("plan", with_start_event("plan", plan))
    builder.add_node("constraints", with_start_event("constraints", constraints))
    builder.add_node("persist_plan", with_start_event("persist_plan", persist_plan))
    builder.add_node("approval_persist", with_start_event("approval_persist", approval_persist))
    builder.add_node("interrupt_wait", with_start_event("interrupt_wait", interrupt_wait))
    builder.add_node("verify_action", with_start_event("verify_action", verify_action))
    builder.add_node("report", with_start_event("report", report))
    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "intent")
    builder.add_edge("intent", "memory_load")
    builder.add_edge("memory_load", "rag_retrieve")
    builder.add_edge("rag_retrieve", "realtime_query")
    builder.add_edge("realtime_query", "tool_selection")
    builder.add_edge("tool_selection", "plan")
    builder.add_edge("plan", "constraints")
    builder.add_conditional_edges(
        "constraints",
        _after_constraints,
        {
            "approval_persist": "approval_persist",
            "persist_plan": "persist_plan",
            "report": "report",
        },
    )
    builder.add_conditional_edges(
        "persist_plan", _after_plan, {"approval_persist": "approval_persist", "report": "report"}
    )
    builder.add_conditional_edges(
        "approval_persist",
        _after_approval,
        {"interrupt_wait": "interrupt_wait", "report": "report"},
    )
    builder.add_edge("interrupt_wait", "verify_action")
    builder.add_edge("verify_action", "report")
    builder.add_edge("report", END)
    return builder.compile(checkpointer=checkpointer)


def _after_constraints(state: AgentState) -> str:
    if state.get("intent") in {"preference", "waitlist", "reminder"}:
        return "approval_persist"
    if state.get("constraint_result", {}).get("feasible"):
        return "persist_plan"
    return "report"


def _after_approval(state: AgentState) -> str:
    return "interrupt_wait" if state.get("awaiting_action_id") is not None else "report"


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
    if state.get("intent") in {"reservation", "cancellation", "waitlist", "reminder"} and state.get(
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


async def _persist_plan(
    deps: GraphDependencies,
    state: AgentState,
    plan: dict[str, Any],
    *,
    confirmed_slot_ids: frozenset[str],
) -> None:
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
        reserved = await session.execute(
            text("""SELECT slot_id FROM tripmate.trip_item WHERE tenant_id=:tenant
                AND trip_id=:trip AND reservation_id IS NOT NULL"""),
            {"tenant": state["tenant_id"], "trip": trip_id},
        )
        reserved_slot_ids = confirmed_slot_ids | {
            str(row["slot_id"]) for row in reserved.mappings() if row["slot_id"] is not None
        }
        next_sequence = int(existing_max or 0) + 1
        for item in plan.get("items", []):
            if item.get("slot_id") is not None and str(item["slot_id"]) in reserved_slot_ids:
                continue
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
                    "seq": next_sequence,
                    "attraction": item.get("attraction_id"),
                    "slot": item.get("slot_id"),
                    "start": window["start_at"],
                    "end": window["end_at"],
                    "cost": Decimal(cost["amount"]),
                    "currency": cost["currency"],
                    "constraint": "PASS" if plan.get("feasible") else "FAIL",
                },
            )
            next_sequence += 1
        update = await session.execute(
            text("""UPDATE tripmate.trip SET status=CASE WHEN status='CONFIRMED' THEN 'CONFIRMED' ELSE :status END,
                    version=version+1,updated_at=:now
                WHERE tenant_id=:tenant AND trip_id=:trip
                  AND status IN ('PLANNING','READY','INFEASIBLE','CONFIRMED')
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

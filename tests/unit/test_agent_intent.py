import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from app.agent.graph import (
    GraphDependencies,
    _classify_intent,
    _exclude_explicitly_avoided,
    _explicit_preference_candidate,
    _model_context_message,
    _plan_candidate_orders,
    _rank_candidate_slots,
    _run_readonly_tool_loop,
    _safe_private_case_context,
    _score_plan_preferences,
    _select_cancellation_reservation,
    _select_plan_slots,
    _shareable_preferences,
    build_graph,
)
from app.agent.worker import _intent_prompt, _slot_ranking_prompt, _tool_selection_prompt
from app.config import Settings
from app.domain.context import RequestContext
from app.domain.ports import SystemClock
from app.tools.gateway import PreferenceInput, ProposeReservationInput, ToolGateway, TripInput


class FakeClassifier:
    def __init__(self, result: object) -> None:
        self.result = result
        self.input: object = None

    async def ainvoke(self, value: object) -> object:
        self.input = value
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeRanker:
    def __init__(self, result: object) -> None:
        self.result = result
        self.input: object = None

    async def ainvoke(self, value: object) -> object:
        self.input = value
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeSelector:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.inputs: list[object] = []

    async def ainvoke(self, value: object) -> object:
        self.inputs.append(value)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _readonly_gateway() -> ToolGateway:
    gateway = object.__new__(ToolGateway)
    gateway.registry = {
        "get_trip_context": (TripInput, None, SimpleNamespace(description="Read a Trip.")),
        "save_user_preference": (
            PreferenceInput,
            None,
            SimpleNamespace(description="Propose a preference."),
        ),
        "propose_reservation": (
            ProposeReservationInput,
            None,
            SimpleNamespace(description="Propose a reservation."),
        ),
    }
    gateway.invoke = AsyncMock(return_value={"trip": {"status": "PLANNING"}})
    return gateway


def _agent_context() -> RequestContext:
    return RequestContext(
        request_id="unit",
        trace_id="unit",
        tenant_id=uuid4(),
        user_id=uuid4(),
        roles=("user",),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        source="agent",
        run_id=uuid4(),
    )


def _slot(
    slot_id: str, *, start_hour: int = 9, duration_hours: int = 2, price: str = "999.00"
) -> dict[str, object]:
    start_at = datetime(2026, 10, 15, start_hour, tzinfo=UTC)
    return {
        "slot_id": slot_id,
        "name": f"Attraction {slot_id}",
        "category": "museum",
        "start_at": start_at,
        "end_at": start_at + timedelta(hours=duration_hours),
        "price": price,
        "available_quantity": 5,
    }


async def test_configured_classifier_result_is_validated() -> None:
    classifier = FakeClassifier({"intent": "knowledge"})

    result = await _classify_intent("今天天气如何", classifier, uuid4())

    assert result == "knowledge"
    assert classifier.input == {"message": "今天天气如何"}


async def test_classifier_failure_falls_back_to_rules() -> None:
    result = await _classify_intent("我要取消预约", FakeClassifier(RuntimeError()), uuid4())

    assert result == "cancellation"


async def test_explicit_preference_intent_uses_no_llm_and_builds_a_limited_candidate() -> None:
    classifier = FakeClassifier({"intent": "planning"})

    intent = await _classify_intent("记住我最多步行30分钟", classifier, uuid4())

    assert intent == "preference"
    assert classifier.input is None
    assert _explicit_preference_candidate("记住我最多步行30分钟") == {
        "key": "walking_limit.max_minutes",
        "value": 30,
    }
    assert _explicit_preference_candidate("我对花生过敏") is None
    assert await _classify_intent("我对花生过敏", classifier, uuid4()) == "preference"
    assert classifier.input is None


async def test_slot_ranker_selects_only_database_candidate_ids() -> None:
    slots = [_slot("slot-a"), _slot("slot-b")]
    ranker = FakeRanker({"slot_ids": ["slot-b", "slot-a"]})

    result, used_model = await _rank_candidate_slots(slots, "偏好室内活动", ranker, uuid4())

    assert [slot["slot_id"] for slot in result] == ["slot-b", "slot-a"]
    assert used_model
    assert '"slot_id": "slot-a"' in ranker.input["candidates"]
    assert "price" not in ranker.input["candidates"]
    assert "available_quantity" not in ranker.input["candidates"]


async def test_slot_ranker_receives_only_shareable_preferences() -> None:
    preferences = _shareable_preferences(
        [
            {"preference_key": "travel_style.tags", "value_json": ["museum"]},
            {"preference_key": "dietary_restriction.tags", "value_json": ["peanut"]},
        ]
    )
    ranker = FakeRanker({"slot_ids": ["slot-a", "slot-b"]})

    _ranked, used_model = await _rank_candidate_slots(
        [_slot("slot-a"), _slot("slot-b")],
        "我对花生过敏，请优先室内活动",
        ranker,
        uuid4(),
        preferences=preferences,
        weather={"status": "UNKNOWN"},
    )

    sent_preferences = json.loads(ranker.input["preferences"])
    assert sent_preferences == {"travel_style.tags": ["museum"]}
    assert used_model
    assert "peanut" not in ranker.input["preferences"]
    assert "花生" not in ranker.input["message"]
    assert "优先室内" in ranker.input["message"]
    assert json.loads(ranker.input["weather"]) == {"status": "UNKNOWN"}


def test_sensitive_preference_clause_is_removed_from_model_context() -> None:
    message = "我对花生过敏，请找杭州的室内景点"

    safe_message = _model_context_message(message)

    assert "花生" not in safe_message
    assert "过敏" not in safe_message
    assert "杭州的室内景点" in safe_message


def test_private_case_context_allows_only_redacted_shareable_preferences() -> None:
    cases = _safe_private_case_context(
        [
            {"content_redacted": 'travel_style.tags: ["museum"]'},
            {"content_redacted": 'dietary_restriction.tags: ["peanut"]'},
            {"content_redacted": 'walking_limit.max_minutes: "peanut allergy"'},
            {"content_redacted": 'unknown.preference: ["museum"]'},
        ]
    )

    assert cases == ['travel_style.tags: ["museum"]']


def test_plan_options_compare_price_and_indoor_orders_deterministically() -> None:
    outdoor = {**_slot("outdoor", price="20"), "indoor": False}
    indoor_expensive = {**_slot("indoor", price="80"), "indoor": True}
    unknown = {**_slot("unknown", price="30"), "indoor": None}

    options = _plan_candidate_orders([outdoor, indoor_expensive, unknown])

    assert [name for name, _ in options] == ["推荐方案", "低价格", "偏室内"]
    assert [slot["slot_id"] for slot in options[1][1]] == ["outdoor", "unknown", "indoor"]
    assert [slot["slot_id"] for slot in options[2][1]] == ["indoor", "outdoor", "unknown"]


def test_soft_scores_use_route_indoor_and_rain_facts_without_changing_hard_facts() -> None:
    items = [
        {"route_from_previous_minutes": 60, "indoor": True},
        {"route_from_previous_minutes": None, "indoor": False},
    ]
    weather = {"2026-10-15": {"status": "OK", "daily": {"weather_code": [61]}}}

    scores, overall = _score_plan_preferences(
        items,
        {"walking_limit.max_minutes": 30, "indoor_ratio.min": 0.75},
        weather,
    )

    assert scores == {
        "walking_minutes": 60,
        "walking_limit_minutes": 30,
        "walking_score": 0.0,
        "indoor_ratio": 0.5,
        "indoor_ratio_min": 0.75,
        "indoor_score": 66.67,
        "rain_risk": True,
        "weather_score": 50.0,
        "overall_score": 38.89,
    }
    assert overall == 38.89


def test_replanning_excludes_only_unconfirmed_explicitly_avoided_slots() -> None:
    west_lake = {**_slot("confirmed"), "name": "西湖"}
    museum = {**_slot("unconfirmed"), "name": "博物馆"}

    candidates = _exclude_explicitly_avoided(
        [west_lake, museum], "我不想去西湖", frozenset({"confirmed"})
    )

    assert [slot["slot_id"] for slot in candidates] == ["confirmed", "unconfirmed"]
    candidates = _exclude_explicitly_avoided(
        [west_lake, museum], "我不想去博物馆", frozenset({"confirmed"})
    )
    assert [slot["slot_id"] for slot in candidates] == ["confirmed"]


def test_plan_slot_selection_keeps_confirmed_items_and_respects_budget_and_overlap() -> None:
    confirmed = _slot("confirmed", start_hour=9, duration_hours=1, price="20")
    overlap = _slot("overlap", start_hour=9, duration_hours=2, price="1")
    fits = _slot("fits", start_hour=10, duration_hours=1, price="20")
    over_budget = _slot("over-budget", start_hour=12, duration_hours=1, price="100")

    selected = _select_plan_slots(
        [overlap, fits, over_budget, confirmed],
        party_size=1,
        budget_limit=Decimal("50"),
        confirmed_slot_ids=frozenset({"confirmed"}),
        confirmed_costs={"confirmed": Decimal("30")},
    )

    assert [slot["slot_id"] for slot in selected] == ["confirmed", "fits"]


def test_cancellation_resolution_requires_a_choice_when_multiple_reservations_exist() -> None:
    first, second = str(uuid4()), str(uuid4())
    reservations = [
        {"reservation_id": first, "slot_id": "slot-a", "status": "CONFIRMED"},
        {"reservation_id": second, "slot_id": "slot-b", "status": "CONFIRMED"},
        {"reservation_id": str(uuid4()), "slot_id": "slot-c", "status": "CANCELLED"},
    ]

    selected, options = _select_cancellation_reservation(reservations, None)

    assert selected is None
    assert [item["reservation_id"] for item in options] == [first, second]
    assert _select_cancellation_reservation(reservations, UUID(second)) == (second, [])
    assert _select_cancellation_reservation(reservations, uuid4()) == (None, [])


async def test_slot_ranker_rejects_unknown_id_and_falls_back() -> None:
    slots = [_slot("slot-a"), _slot("slot-b")]

    result, used_model = await _rank_candidate_slots(
        slots, "优先室内", FakeRanker({"slot_ids": ["foreign", "slot-a"]}), uuid4()
    )

    assert result == slots
    assert not used_model


async def test_slot_ranker_rejects_invalid_structured_output_and_falls_back() -> None:
    slots = [_slot("slot-a"), _slot("slot-b")]

    result, used_model = await _rank_candidate_slots(
        slots, "优先室内", FakeRanker({"slot_ids": "slot-a"}), uuid4()
    )

    assert result == slots
    assert not used_model


async def test_slot_ranker_failure_falls_back() -> None:
    slots = [_slot("slot-a"), _slot("slot-b")]

    result, used_model = await _rank_candidate_slots(
        slots, "优先室内", FakeRanker(TimeoutError()), uuid4()
    )

    assert result == slots
    assert not used_model


async def test_readonly_tool_loop_runs_registered_tool_then_stops() -> None:
    gateway = _readonly_gateway()
    selector = FakeSelector(
        {"tool_id": "get_trip_context", "arguments": {"trip_id": str(uuid4())}},
        {"tool_id": None, "arguments": {}},
    )

    called = await _run_readonly_tool_loop(
        selector,
        gateway,
        "我对花生过敏，请查看行程",
        _agent_context(),
        timeout_seconds=1,
    )

    assert called == ["get_trip_context"]
    gateway.invoke.assert_awaited_once()
    visible = json.loads(selector.inputs[0]["tools"])
    assert {item["tool_id"] for item in visible} == {"get_trip_context"}
    assert "花生" not in selector.inputs[0]["message"]
    assert "查看行程" in selector.inputs[0]["message"]
    assert json.loads(selector.inputs[1]["observations"])[0]["result"]["trip"]["status"] == "PLANNING"


async def test_readonly_tool_loop_rejects_write_tool_ids_without_invoking() -> None:
    gateway = _readonly_gateway()
    selector = FakeSelector(
        {"tool_id": "save_user_preference", "arguments": {"key": "diet", "value": []}}
    )

    called = await _run_readonly_tool_loop(
        selector, gateway, "记住我的偏好", _agent_context(), timeout_seconds=1
    )

    assert called == []
    gateway.invoke.assert_not_awaited()


async def test_readonly_tool_loop_schema_errors_and_invalid_output_fall_back() -> None:
    gateway = _readonly_gateway()
    context = _agent_context()
    bad_arguments = FakeSelector(
        {"tool_id": "get_trip_context", "arguments": {"trip_id": "not-a-uuid"}}
    )
    invalid_output = FakeSelector({"tool_id": "get_trip_context"})

    assert await _run_readonly_tool_loop(
        bad_arguments, gateway, "查看行程", context, timeout_seconds=1
    ) == []
    assert await _run_readonly_tool_loop(
        invalid_output, gateway, "查看行程", context, timeout_seconds=1
    ) == []
    gateway.invoke.assert_not_awaited()


async def test_readonly_tool_loop_timeout_and_call_failure_fall_back() -> None:
    class SlowSelector:
        async def ainvoke(self, _value: object) -> object:
            await asyncio.sleep(0.02)
            return {"tool_id": None, "arguments": {}}

    gateway = _readonly_gateway()
    context = _agent_context()
    assert await _run_readonly_tool_loop(
        SlowSelector(), gateway, "查询", context, timeout_seconds=0.001
    ) == []

    gateway.invoke.side_effect = RuntimeError("gateway failure")
    selector = FakeSelector(
        {"tool_id": "get_trip_context", "arguments": {"trip_id": str(uuid4())}}
    )
    assert await _run_readonly_tool_loop(
        selector, gateway, "查询", context, timeout_seconds=1
    ) == []


def test_intent_prompt_keeps_json_example_as_literal() -> None:
    prompt = _intent_prompt().invoke({"message": "请问景点开放到几点？"})

    assert len(prompt.messages) == 2


def test_slot_ranking_prompt_has_only_the_structured_result_contract() -> None:
    prompt = _slot_ranking_prompt().invoke(
        {
            "message": "偏好室内活动",
            "preferences": "{}",
            "weather": "{}",
            "cases": "[]",
            "candidates": '[{"slot_id":"slot-a"}]',
        }
    )

    assert len(prompt.messages) == 2


async def test_graph_records_node_start_before_running_each_node() -> None:
    tenant_id, user_id, run_id, conversation_id, message_id = (uuid4() for _ in range(5))
    thread_id = f"tenant:{tenant_id}:user:{user_id}:conversation:{conversation_id}"
    runs = SimpleNamespace(
        load_run_input=AsyncMock(
            return_value={
                "user_id": user_id,
                "thread_id": thread_id,
                "content_redacted": "规划一次周末出游",
                "content_hash": "a" * 64,
                "message_id": message_id,
                "conversation_id": conversation_id,
            }
        ),
        append_run_event=AsyncMock(),
    )
    dependencies = GraphDependencies(
        sessions=None,
        settings=SimpleNamespace(rag_top_k=3, llm_timeout_seconds=1),
        clock=SystemClock(),
        ids=None,
        runs=runs,
        reservations=None,
        preferences=SimpleNamespace(list_for_user=AsyncMock(return_value=[])),
        short_term_memory=SimpleNamespace(
            get=AsyncMock(return_value=None), merge=AsyncMock(return_value=None)
        ),
        knowledge=SimpleNamespace(search=AsyncMock(return_value=[])),
        tools=None,
    )
    graph = build_graph(dependencies, checkpointer=None)

    await graph.ainvoke(
        {
            "schema_version": 1,
            "run_id": run_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "thread_id": thread_id,
        }
    )

    node_events = [
        call for call in runs.append_run_event.await_args_list if call.args[2] == "node_started"
    ]
    assert [call.args[3]["node"] for call in node_events] == [
        "load_context",
        "intent",
        "memory_load",
        "rag_retrieve",
        "realtime_query",
        "tool_selection",
        "plan",
        "constraints",
        "report",
    ]


def test_tool_selection_prompt_does_not_bind_model_tools() -> None:
    prompt = _tool_selection_prompt().invoke(
        {"message": "查看行程", "tools": "[]", "observations": "[]"}
    )

    assert len(prompt.messages) == 2


def test_optional_model_defaults_have_no_app_specific_key_setting() -> None:
    settings = Settings()

    assert "llm_api_key" not in Settings.model_fields
    assert settings.llm_base_url == "https://api.deepseek.com"
    assert settings.llm_model == "deepseek-flash"


def test_client_init_failure_keeps_worker_on_rule_classifier(monkeypatch) -> None:
    from app.agent import worker

    def fail_client(*args: object, **kwargs: object) -> None:
        raise ValueError("invalid proxy configuration")

    monkeypatch.setattr(worker, "ChatOpenAI", fail_client)
    runtime = SimpleNamespace(settings=Settings())

    assert worker._build_intent_classifier(runtime) is None

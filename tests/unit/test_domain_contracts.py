from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain.cancellation import CancellationPolicyEvaluator
from app.domain.constraints import InventoryFact, validate_trip_plan
from app.domain.idempotency import IdempotencyScope, request_hash
from app.domain.models import (
    ActivityPlanItem,
    CancellationPolicySnapshot,
    CancellationRule,
    Money,
    TimeWindow,
    TripPlan,
)
from app.events.runtime import event_domain
from app.events.schema import EventEnvelope, RunEvent
from app.inventory.keys import KeyCodec
from app.memory.short_term import MemoryPatch
from app.tools.gateway import SearchAttractionsInput


def test_idempotency_hash_covers_entire_scope_and_canonical_request() -> None:
    tenant_id, other_tenant_id, user_id = uuid4(), uuid4(), uuid4()
    scope = IdempotencyScope(tenant_id, user_id, "/v1/reservations", "hold", "same-key")

    assert scope.key_hash() != IdempotencyScope(
        other_tenant_id, user_id, "/v1/reservations", "hold", "same-key"
    ).key_hash()
    assert scope.key_hash() != IdempotencyScope(
        tenant_id, user_id, "/v1/trips", "hold", "same-key"
    ).key_hash()
    assert request_hash({"slot": "a", "quantity": 1}) == request_hash(
        {"quantity": 1, "slot": "a"}
    )
    assert request_hash({"slot": "a", "quantity": 1}) != request_hash(
        {"slot": "a", "quantity": 2}
    )


def test_event_envelope_rejects_tokens_and_raw_message_content() -> None:
    event = {
        "event_id": str(uuid4()),
        "event_type": "reservation.held.v1",
        "event_version": 1,
        "occurred_at": datetime.now(UTC).isoformat(),
        "tenant_id": str(uuid4()),
        "aggregate_type": "reservation",
        "aggregate_id": str(uuid4()),
        "aggregate_version": 1,
        "request_id": "request-1",
        "trace_id": "trace-1",
        "producer": "test",
        "payload": {"reservation_id": str(uuid4()), "quantity": 2},
    }
    assert EventEnvelope.model_validate(event).event_version == 1

    event["payload"] = {"nested": {"confirmation_token": "secret"}}
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(event)

    event["payload"] = {"raw_message": "private user text"}
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(event)


def test_run_events_reject_secrets_sensitive_preferences_and_raw_user_text() -> None:
    with pytest.raises(ValidationError):
        RunEvent.model_validate(
            {"event_type": "confirmation", "payload": {"confirmation_token": "secret"}}
        )
    with pytest.raises(ValidationError):
        RunEvent.model_validate(
            {"event_type": "citation", "payload": {"dietary_restriction_tags": ["peanut"]}}
        )

    event = RunEvent.model_validate(
        {"event_type": "citation", "payload": {"chunk_id": str(uuid4()), "index_version": "v1"}}
    )
    assert event.event_type == "citation"


def test_stream_domains_include_every_p0_outbox_producer() -> None:
    assert event_domain("trip.created.v1") == "trips"
    assert event_domain("preference.deleted.v1") == "preferences"
    assert event_domain("agent.run.requested.v1") == "runs"
    assert event_domain("reservation.expired.v1") == "reservations"

    tenant_id, other_tenant_id, user_id = uuid4(), uuid4(), uuid4()
    codec = KeyCodec("tripmate", "test")
    assert codec.same_hash_tag(
        codec.inventory(tenant_id, uuid4()), codec.hold(tenant_id, uuid4()), codec.expiry(tenant_id)
    )
    conversation_id = uuid4()
    memory_key = codec.conversation(tenant_id, user_id, conversation_id)
    assert codec.same_hash_tag(memory_key, codec.inventory(tenant_id, uuid4()))
    assert memory_key != codec.conversation(tenant_id, other_tenant_id, conversation_id)


def test_tool_schema_rejects_service_authority_fields() -> None:
    with pytest.raises(ValidationError):
        SearchAttractionsInput.model_validate(
            {"query": "museum", "tenant_id": str(uuid4()), "price": "0.00"}
        )


def test_short_term_memory_patch_rejects_raw_message_fields() -> None:
    with pytest.raises(ValidationError):
        MemoryPatch.model_validate({"message": "private unredacted text"})


def test_trip_constraints_fail_closed_on_unknown_inventory_and_overlap() -> None:
    start = datetime(2026, 10, 24, 1, tzinfo=UTC)
    item = ActivityPlanItem(
        sequence_no=1,
        slot_id=uuid4(),
        title="Ticketed visit",
        window=TimeWindow(
            start_at=start,
            end_at=start + timedelta(hours=2),
            timezone="Asia/Shanghai",
        ),
        cost=Money(amount=Decimal("35.00"), currency="CNY"),
        reservation_required=True,
    )
    plan = TripPlan(
        destination="Hangzhou",
        start_date=date(2026, 10, 24),
        end_date=date(2026, 10, 24),
        timezone="Asia/Shanghai",
        party_size=2,
        items=(item,),
        total_cost=Money(amount=Decimal("35.00"), currency="CNY"),
        feasible=True,
        generated_at=datetime.now(UTC),
        planner_version="test",
    )

    unknown = validate_trip_plan(plan, inventory={})
    assert unknown.state == "UNKNOWN"
    assert not unknown.feasible

    available = validate_trip_plan(
        plan,
        inventory={
            str(item.slot_id): InventoryFact(str(item.slot_id), 1, 1),
        },
    )
    assert available.state == "FAIL"
    assert {check["code"] for check in available.checks} == {"INVENTORY_NOT_ENOUGH"}

    overlapping_item = ActivityPlanItem(
        sequence_no=2,
        title="Overlapping stop",
        window=TimeWindow(
            start_at=start + timedelta(hours=1),
            end_at=start + timedelta(hours=3),
            timezone="Asia/Shanghai",
        ),
        cost=Money(amount=Decimal("35.00"), currency="CNY"),
    )
    overlapping_plan = plan.model_copy(
        update={
            "items": (item, overlapping_item),
            "total_cost": Money(amount=Decimal("70.00"), currency="CNY"),
        }
    )
    overlap = validate_trip_plan(
        overlapping_plan,
        inventory={str(item.slot_id): InventoryFact(str(item.slot_id), 2, 1)},
    )
    assert overlap.state == "FAIL"
    assert "TIME_OVERLAP" in {check["code"] for check in overlap.checks}

    confirmed = validate_trip_plan(
        plan,
        inventory={str(item.slot_id): InventoryFact(str(item.slot_id), 0, 1)},
        confirmed_slot_ids=frozenset({str(item.slot_id)}),
        open_slot_ids=frozenset({str(item.slot_id)}),
    )
    assert confirmed.state == "PASS"

    routed_item = ActivityPlanItem(
        sequence_no=2,
        title="Next stop",
        window=TimeWindow(
            start_at=start + timedelta(hours=2, minutes=30),
            end_at=start + timedelta(hours=3, minutes=30),
            timezone="Asia/Shanghai",
        ),
        cost=Money(amount=Decimal("35.00"), currency="CNY"),
        route_from_previous_minutes=30,
    )
    routed_plan = plan.model_copy(
        update={
            "items": (item, routed_item),
            "total_cost": Money(amount=Decimal("70.00"), currency="CNY"),
        }
    )
    route_fits = validate_trip_plan(
        routed_plan,
        inventory={str(item.slot_id): InventoryFact(str(item.slot_id), 2, 1)},
    )
    assert route_fits.state == "PASS"

    route_conflict = routed_plan.model_copy(
        update={
            "items": (item, routed_item.model_copy(update={"route_from_previous_minutes": 31}))
        }
    )
    route_fails = validate_trip_plan(
        route_conflict,
        inventory={str(item.slot_id): InventoryFact(str(item.slot_id), 2, 1)},
    )
    assert "ROUTE_TIME_CONFLICT" in {check["code"] for check in route_fails.checks}


def test_cancellation_uses_fixed_snapshot_and_cutoff_boundary() -> None:
    cutoff = datetime(2026, 10, 20, tzinfo=UTC)
    policy = CancellationPolicySnapshot(
        policy_id="policy-1",
        policy_version="v1",
        timezone="UTC",
        rules=(
            CancellationRule(
                cutoff_at=cutoff,
                refund_percent=Decimal("50.00"),
                reason_code="BEFORE_CUTOFF",
            ),
        ),
    )
    evaluator = CancellationPolicyEvaluator()

    assert evaluator.evaluate(policy, cutoff - timedelta(microseconds=1)).refund_percent == Decimal(
        "50.00"
    )
    rejected = evaluator.evaluate(policy, cutoff)
    assert not rejected.allowed
    assert rejected.reason_code == "CANCELLATION_CUTOFF_PASSED"

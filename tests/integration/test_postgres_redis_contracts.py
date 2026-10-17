import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from math import ceil
from time import perf_counter
from types import SimpleNamespace
from typing import Any, TypedDict
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio.session import AsyncSessionTransaction

from app.agent.graph import _persist_plan
from app.agent.worker import RunWorker
from app.api import create_app
from app.domain.errors import DomainError
from app.domain.ports import SystemClock
from app.evaluation.replay import EvaluationReplayer
from app.events.runtime import InboxProcessor, OutboxPublisher
from app.events.schema import EventEnvelope
from app.knowledge.index import KnowledgeIndex
from app.memory.short_term import MemoryPatch
from app.persistence.postgres.database import affected_rows
from app.reservation.service import ReservationService
from app.runtime import Runtime
from app.security.auth import issue_bearer_token
from tests.integration.conftest import IntegrationScope

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.redis_stack,
    pytest.mark.asyncio(loop_scope="module"),
]


async def _approved_candidate(scope: IntegrationScope) -> tuple[Any, Any]:
    runtime = scope.runtime
    action_id, _parameters_hash = await runtime.reservations.create_candidate_action(
        scope.agent_context(),
        run_id=scope.run_id,
        action_type="propose_reservation",
        parameters={
            "trip_id": str(scope.trip_id),
            "slot_id": str(scope.slot_id),
            "quantity": 1,
            "reason": "integration contract",
        },
    )
    action_confirmation = await runtime.reservations.issue_confirmation(
        scope.user_context(), action_id, idempotency_key=f"issue-{uuid4()}"
    )
    return action_id, action_confirmation


async def _approved_hold(
    scope: IntegrationScope,
    *,
    idempotency_key: str | None = None,
    slot_id: UUID | None = None,
) -> tuple[Any, Any, Any]:
    runtime = scope.runtime
    slot_id = slot_id or scope.slot_id
    action_id, _parameters_hash = await runtime.reservations.create_candidate_action(
        scope.agent_context(),
        run_id=scope.run_id,
        action_type="propose_reservation",
        parameters={
            "trip_id": str(scope.trip_id),
            "slot_id": str(slot_id),
            "quantity": 1,
            "reason": "integration contract",
        },
    )
    action_confirmation = await runtime.reservations.issue_confirmation(
        scope.user_context(), action_id, idempotency_key=f"issue-{uuid4()}"
    )
    key = idempotency_key or f"hold-{uuid4()}"
    hold = await runtime.reservations.hold(
        scope.user_context(),
        trip_id=scope.trip_id,
        slot_id=slot_id,
        quantity=1,
        action_id=action_id,
        action_confirmation_token=action_confirmation["confirmation_token"],
        expected_inventory_version=None,
        expected_activity_version=1,
        idempotency_key=key,
    )
    return action_id, action_confirmation, hold


async def test_private_preference_vectors_are_scoped_safe_and_deleted(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    ctx = tenant_scope.user_context()
    saved = await runtime.preferences.save(
        ctx,
        scope="personal",
        key="travel_style.tags",
        value=["museum"],
        idempotency_key=f"vector-preference-{uuid4()}",
    )
    await runtime.preferences.save(
        ctx,
        scope="personal",
        key="dietary_restriction.tags",
        value=["peanut"],
        idempotency_key=f"sensitive-preference-{uuid4()}",
    )
    other_user = uuid4()
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("""INSERT INTO tripmate.user_profile(user_id,tenant_id,display_name,status)
                VALUES(:user,:tenant,'Private Case Peer','ACTIVE')"""),
            {"user": other_user, "tenant": tenant_scope.tenant_id},
        )
        vectors = await session.execute(
            text("""SELECT source_preference_id,content_redacted,embedding
                FROM tripmate.private_case_vector WHERE tenant_id=:tenant AND user_id=:user
                  AND tombstoned_at IS NULL"""),
            {"tenant": tenant_scope.tenant_id, "user": tenant_scope.user_id},
        )
        vector_rows = list(vectors.mappings())

    assert len(vector_rows) == 1
    assert vector_rows[0]["source_preference_id"] == saved["preference_id"]
    assert "peanut" not in vector_rows[0]["content_redacted"]
    assert len(vector_rows[0]["embedding"]) == 384
    cases = await runtime.knowledge.search_private_cases(ctx, "museum", current_generation=1)
    assert len(cases) == 1
    assert await runtime.knowledge.search_private_cases(
        replace(ctx, user_id=other_user), "museum", current_generation=1
    ) == []
    assert await runtime.knowledge.search_private_cases(
        replace(ctx, tenant_id=uuid4()), "museum", current_generation=1
    ) == []

    deleted = await runtime.preferences.delete(
        ctx,
        saved["preference_id"],
        expected_version=saved["version"],
        idempotency_key=f"delete-vector-preference-{uuid4()}",
    )
    assert deleted["deleted"] is True
    assert await runtime.knowledge.search_private_cases(ctx, "museum", current_generation=1) == []
    async with runtime.sessions() as session:
        tombstone = await session.scalar(
            text("""SELECT tombstoned_at FROM tripmate.private_case_vector
                WHERE tenant_id=:tenant AND source_preference_id=:preference"""),
            {"tenant": tenant_scope.tenant_id, "preference": saved["preference_id"]},
        )
    assert tombstone is not None


@pytest.mark.postgres
async def test_waitlist_action_writes_only_after_confirmation_and_is_idempotent(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("""UPDATE tripmate.inventory SET total_quantity=0,available_quantity=0,
                version=version+1 WHERE tenant_id=:tenant AND slot_id=:slot"""),
            {"tenant": tenant_scope.tenant_id, "slot": tenant_scope.slot_id},
        )
    await runtime.projection.rebuild_all()
    action_id, _parameters_hash = await runtime.reservations.create_candidate_action(
        tenant_scope.agent_context(),
        run_id=tenant_scope.run_id,
        action_type="propose_waitlist",
        parameters={
            "trip_id": str(tenant_scope.trip_id),
            "slot_id": str(tenant_scope.slot_id),
            "quantity": 1,
            "reason": "user requested a waitlist",
        },
    )
    confirmation = await runtime.reservations.issue_confirmation(
        tenant_scope.user_context(), action_id, idempotency_key=f"waitlist-confirm-{uuid4()}"
    )
    async with runtime.sessions() as session:
        assert await session.scalar(
            text("SELECT count(*) FROM tripmate.reservation WHERE tenant_id=:tenant"),
            {"tenant": tenant_scope.tenant_id},
        ) == 0

    idempotency_key = f"waitlist-execute-{uuid4()}"
    result = await runtime.reservations.execute_p1_action(
        tenant_scope.user_context(),
        action_id,
        action_type="propose_waitlist",
        confirmation_token=confirmation["confirmation_token"],
        idempotency_key=idempotency_key,
    )
    replay = await runtime.reservations.execute_p1_action(
        tenant_scope.user_context(),
        action_id,
        action_type="propose_waitlist",
        confirmation_token=confirmation["confirmation_token"],
        idempotency_key=idempotency_key,
    )
    assert result == replay
    assert result["status"] == "WAITLISTED"
    with pytest.raises(DomainError):
        await runtime.reservations.execute_p1_action(
            tenant_scope.user_context(),
            action_id,
            action_type="propose_waitlist",
            confirmation_token=confirmation["confirmation_token"],
            idempotency_key=f"waitlist-replay-with-new-key-{uuid4()}",
        )
    async with runtime.sessions() as session:
        reservation = (
            await session.execute(
                text("""SELECT status,price_snapshot FROM tripmate.reservation
                    WHERE tenant_id=:tenant AND reservation_id=:reservation"""),
                {"tenant": tenant_scope.tenant_id, "reservation": result["reservation_id"]},
            )
        ).mappings().one()
        inventory = (
            await session.execute(
                text("""SELECT total_quantity,available_quantity,held_quantity,confirmed_quantity
                    FROM tripmate.inventory WHERE tenant_id=:tenant AND slot_id=:slot"""),
                {"tenant": tenant_scope.tenant_id, "slot": tenant_scope.slot_id},
            )
        ).mappings().one()
    assert reservation["status"] == "WAITLISTED"
    assert reservation["price_snapshot"] == Decimal("25.00")
    assert inventory["total_quantity"] == inventory["available_quantity"] == 0
    assert inventory["held_quantity"] == inventory["confirmed_quantity"] == 0


@pytest.mark.postgres
async def test_waitlist_promotion_requires_fresh_facts_and_is_idempotent(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("""UPDATE tripmate.inventory SET total_quantity=0,available_quantity=0,
                version=version+1 WHERE tenant_id=:tenant AND slot_id=:slot"""),
            {"tenant": tenant_scope.tenant_id, "slot": tenant_scope.slot_id},
        )
    action_id, _ = await runtime.reservations.create_candidate_action(
        tenant_scope.agent_context(),
        run_id=tenant_scope.run_id,
        action_type="propose_waitlist",
        parameters={
            "trip_id": str(tenant_scope.trip_id),
            "slot_id": str(tenant_scope.slot_id),
            "quantity": 1,
            "reason": "promotion contract",
        },
    )
    action_confirmation = await runtime.reservations.issue_confirmation(
        tenant_scope.user_context(), action_id, idempotency_key=f"promotion-waitlist-{uuid4()}"
    )
    waitlisted = await runtime.reservations.execute_p1_action(
        tenant_scope.user_context(),
        action_id,
        action_type="propose_waitlist",
        confirmation_token=action_confirmation["confirmation_token"],
        idempotency_key=f"promotion-create-{uuid4()}",
    )
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("""UPDATE tripmate.inventory SET total_quantity=1,available_quantity=1,
                version=version+1 WHERE tenant_id=:tenant AND slot_id=:slot"""),
            {"tenant": tenant_scope.tenant_id, "slot": tenant_scope.slot_id},
        )
    proposals = await runtime.reservations.propose_waitlist_promotions(tenant_scope.user_context())
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["reservation_id"] == waitlisted["reservation_id"]

    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("""UPDATE tripmate.activity_slot SET price=30.00,activity_version=activity_version+1
                WHERE tenant_id=:tenant AND slot_id=:slot"""),
            {"tenant": tenant_scope.tenant_id, "slot": tenant_scope.slot_id},
        )
    with pytest.raises(DomainError, match="Waitlist facts changed"):
        await runtime.reservations.confirm_waitlist_promotion(
            tenant_scope.user_context(),
            UUID(proposal["promotion_id"]),
            confirmation_token=proposal["confirmation_token"],
            idempotency_key=f"promotion-stale-{uuid4()}",
        )

    refreshed = await runtime.reservations.propose_waitlist_promotions(tenant_scope.user_context())
    assert len(refreshed) == 1
    key = f"promotion-confirm-{uuid4()}"
    confirmed = await runtime.reservations.confirm_waitlist_promotion(
        tenant_scope.user_context(),
        UUID(refreshed[0]["promotion_id"]),
        confirmation_token=refreshed[0]["confirmation_token"],
        idempotency_key=key,
    )
    assert confirmed == await runtime.reservations.confirm_waitlist_promotion(
        tenant_scope.user_context(),
        UUID(refreshed[0]["promotion_id"]),
        confirmation_token=refreshed[0]["confirmation_token"],
        idempotency_key=key,
    )
    async with runtime.sessions() as session:
        reservation = (
            await session.execute(
                text("""SELECT status,price_snapshot,version FROM tripmate.reservation
                    WHERE tenant_id=:tenant AND reservation_id=:reservation"""),
                {"tenant": tenant_scope.tenant_id, "reservation": waitlisted["reservation_id"]},
            )
        ).mappings().one()
        inventory = (
            await session.execute(
                text("""SELECT total_quantity,available_quantity,held_quantity,confirmed_quantity
                    FROM tripmate.inventory WHERE tenant_id=:tenant AND slot_id=:slot"""),
                {"tenant": tenant_scope.tenant_id, "slot": tenant_scope.slot_id},
            )
        ).mappings().one()
    assert reservation["status"] == "CONFIRMED"
    assert reservation["price_snapshot"] == Decimal("30.00")
    assert inventory["total_quantity"] == (
        inventory["available_quantity"] + inventory["held_quantity"] + inventory["confirmed_quantity"]
    )


@pytest.mark.postgres
@pytest.mark.redis_stack
async def test_confirmed_reminder_is_delivered_once_to_its_user_inbox(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    scheduled_at = SystemClock().now() + timedelta(minutes=10)
    action_id, _parameters_hash = await runtime.reservations.create_candidate_action(
        tenant_scope.agent_context(),
        run_id=tenant_scope.run_id,
        action_type="create_reminder",
        parameters={
            "trip_id": str(tenant_scope.trip_id),
            "reminder_type": "weather_check",
            "scheduled_at": scheduled_at.isoformat(),
            "timezone": "Asia/Shanghai",
        },
    )
    confirmation = await runtime.reservations.issue_confirmation(
        tenant_scope.user_context(), action_id, idempotency_key=f"reminder-confirm-{uuid4()}"
    )
    idempotency_key = f"reminder-execute-{uuid4()}"
    result = await runtime.reservations.execute_p1_action(
        tenant_scope.user_context(),
        action_id,
        action_type="create_reminder",
        confirmation_token=confirmation["confirmation_token"],
        idempotency_key=idempotency_key,
    )
    assert result == await runtime.reservations.execute_p1_action(
        tenant_scope.user_context(),
        action_id,
        action_type="create_reminder",
        confirmation_token=confirmation["confirmation_token"],
        idempotency_key=idempotency_key,
    )

    other_user = uuid4()
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("""INSERT INTO tripmate.user_profile(user_id,tenant_id,display_name,status)
                VALUES(:user,:tenant,'Notification Peer','ACTIVE')"""),
            {"user": other_user, "tenant": tenant_scope.tenant_id},
        )
        await session.execute(
            text("UPDATE tripmate.reminder SET scheduled_at=:due WHERE tenant_id=:tenant AND reminder_id=:id"),
            {
                "due": SystemClock().now() - timedelta(seconds=1),
                "tenant": tenant_scope.tenant_id,
                "id": result["reminder_id"],
            },
        )
    assert await runtime.reminders.rebuild_due(tenant_scope.tenant_id) == 1
    assert await runtime.reminders.dispatch_due(f"reminder-worker-{uuid4()}") == 1
    assert await runtime.reminders.dispatch_due(f"reminder-worker-{uuid4()}") == 0
    notifications = await runtime.reminders.list_notifications(tenant_scope.user_context())
    assert len(notifications) == 1
    assert str(notifications[0]["notification_id"]) == str(result["notification_id"])
    assert notifications[0]["status"] == "UNREAD"
    assert await runtime.reminders.list_notifications(
        replace(tenant_scope.user_context(), user_id=other_user)
    ) == []
    read = await runtime.reminders.mark_read(
        tenant_scope.user_context(), UUID(str(result["notification_id"]))
    )
    assert read["status"] == "READ"
    async with runtime.sessions() as session:
        reminder = (
            await session.execute(
                text("SELECT status,attempt_count FROM tripmate.reminder WHERE tenant_id=:tenant AND reminder_id=:id"),
                {"tenant": tenant_scope.tenant_id, "id": result["reminder_id"]},
            )
        ).mappings().one()
        assert await session.scalar(
            text("SELECT count(*) FROM tripmate.notification_inbox WHERE tenant_id=:tenant AND reminder_id=:id"),
            {"tenant": tenant_scope.tenant_id, "id": result["reminder_id"]},
        ) == 1
    assert reminder["status"] == "SENT"
    assert reminder["attempt_count"] == 1


@pytest.mark.postgres
@pytest.mark.redis_stack
async def test_reminder_failure_retries_then_reaches_terminal_failed_state(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    scheduled_at = SystemClock().now() + timedelta(minutes=10)
    action_id, _parameters_hash = await runtime.reservations.create_candidate_action(
        tenant_scope.agent_context(),
        run_id=tenant_scope.run_id,
        action_type="create_reminder",
        parameters={
            "trip_id": str(tenant_scope.trip_id),
            "reminder_type": "departure",
            "scheduled_at": scheduled_at.isoformat(),
            "timezone": "Asia/Shanghai",
        },
    )
    confirmation = await runtime.reservations.issue_confirmation(
        tenant_scope.user_context(), action_id, idempotency_key=f"failure-reminder-confirm-{uuid4()}"
    )
    created = await runtime.reservations.execute_p1_action(
        tenant_scope.user_context(),
        action_id,
        action_type="create_reminder",
        confirmation_token=confirmation["confirmation_token"],
        idempotency_key=f"failure-reminder-create-{uuid4()}",
    )
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("""UPDATE tripmate.reminder SET scheduled_at=:due
                WHERE tenant_id=:tenant AND reminder_id=:reminder"""),
            {
                "due": SystemClock().now() - timedelta(seconds=1),
                "tenant": tenant_scope.tenant_id,
                "reminder": created["reminder_id"],
            },
        )
    await runtime.reminders.rebuild_due(tenant_scope.tenant_id)
    original_deliver = runtime.reminders._deliver

    async def fail_delivery(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("simulated notification provider failure")

    runtime.reminders._deliver = fail_delivery  # type: ignore[method-assign]
    try:
        assert await runtime.reminders.dispatch_due(f"failure-worker-{uuid4()}") == 0
    finally:
        runtime.reminders._deliver = original_deliver  # type: ignore[method-assign]

    async with runtime.sessions() as session:
        first = (
            await session.execute(
                text("""SELECT status,attempt_count,last_error_code FROM tripmate.reminder
                    WHERE tenant_id=:tenant AND reminder_id=:reminder"""),
                {"tenant": tenant_scope.tenant_id, "reminder": created["reminder_id"]},
            )
        ).mappings().one()
    assert first["status"] == "RETRYING"
    assert first["attempt_count"] == 1
    assert first["last_error_code"] == "RuntimeError"
    for _ in range(runtime.settings.max_event_attempts - 1):
        await runtime.reminders._mark_failure(
            tenant_scope.tenant_id,
            UUID(str(created["reminder_id"])),
            "failure-worker",
            RuntimeError("simulated notification provider failure"),
        )
    async with runtime.sessions() as session:
        terminal = await session.scalar(
            text("SELECT status FROM tripmate.reminder WHERE tenant_id=:tenant AND reminder_id=:reminder"),
            {"tenant": tenant_scope.tenant_id, "reminder": created["reminder_id"]},
        )
    assert terminal == "FAILED"


@pytest.mark.postgres
async def test_alternatives_obey_inventory_budget_and_confirmed_time_conflicts(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    source_start = tenant_scope.start_at
    blocker_id, valid_id, conflict_id, sold_out_id, over_budget_id = (uuid4() for _ in range(5))
    source_end = source_start + timedelta(hours=2)
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("""INSERT INTO tripmate.activity_slot
                (slot_id,tenant_id,attraction_id,external_id,start_at,end_at,capacity,price,currency,
                 cancellation_policy,policy_version,activity_version,status)
                VALUES(:slot,:tenant,:attraction,:external,:start,:end,1,:price,'CNY',
                 CAST(:policy AS jsonb),'test-v1',1,'ACTIVE')"""),
            [
                {
                    "slot": blocker_id,
                    "tenant": tenant_scope.tenant_id,
                    "attraction": tenant_scope.attraction_id,
                    "external": str(blocker_id),
                    "start": source_start + timedelta(hours=1, minutes=30),
                    "end": source_end + timedelta(hours=1),
                    "price": Decimal("25.00"),
                    "policy": json.dumps({"policy_id": "test", "policy_version": "test-v1", "rules": []}),
                },
                {
                    "slot": valid_id,
                    "tenant": tenant_scope.tenant_id,
                    "attraction": tenant_scope.attraction_id,
                    "external": str(valid_id),
                    "start": source_start + timedelta(minutes=5),
                    "end": source_start + timedelta(minutes=45),
                    "price": Decimal("30.00"),
                    "policy": json.dumps({"policy_id": "test", "policy_version": "test-v1", "rules": []}),
                },
                {
                    "slot": conflict_id,
                    "tenant": tenant_scope.tenant_id,
                    "attraction": tenant_scope.attraction_id,
                    "external": str(conflict_id),
                    "start": source_start + timedelta(minutes=60),
                    "end": source_start + timedelta(minutes=110),
                    "price": Decimal("20.00"),
                    "policy": json.dumps({"policy_id": "test", "policy_version": "test-v1", "rules": []}),
                },
                {
                    "slot": sold_out_id,
                    "tenant": tenant_scope.tenant_id,
                    "attraction": tenant_scope.attraction_id,
                    "external": str(sold_out_id),
                    "start": source_start + timedelta(minutes=5),
                    "end": source_start + timedelta(minutes=45),
                    "price": Decimal("5.00"),
                    "policy": json.dumps({"policy_id": "test", "policy_version": "test-v1", "rules": []}),
                },
                {
                    "slot": over_budget_id,
                    "tenant": tenant_scope.tenant_id,
                    "attraction": tenant_scope.attraction_id,
                    "external": str(over_budget_id),
                    "start": source_start + timedelta(minutes=5),
                    "end": source_start + timedelta(minutes=45),
                    "price": Decimal("101.00"),
                    "policy": json.dumps({"policy_id": "test", "policy_version": "test-v1", "rules": []}),
                },
            ],
        )
        await session.execute(
            text("""INSERT INTO tripmate.inventory
                (tenant_id,slot_id,total_quantity,available_quantity,status)
                VALUES(:tenant,:slot,:total,:available,'ACTIVE')"""),
            [
                {"tenant": tenant_scope.tenant_id, "slot": blocker_id, "total": 1, "available": 1},
                {"tenant": tenant_scope.tenant_id, "slot": valid_id, "total": 1, "available": 1},
                {"tenant": tenant_scope.tenant_id, "slot": conflict_id, "total": 1, "available": 1},
                {"tenant": tenant_scope.tenant_id, "slot": sold_out_id, "total": 0, "available": 0},
                {"tenant": tenant_scope.tenant_id, "slot": over_budget_id, "total": 1, "available": 1},
            ],
        )
    await runtime.projection.rebuild_all()
    action_id, _action_confirmation, held = await _approved_hold(
        tenant_scope, slot_id=blocker_id
    )
    confirmed = await runtime.reservations.confirm(
        tenant_scope.user_context(),
        reservation_id=held.reservation_id,
        action_id=action_id,
        confirmation_token=held.confirmation_token or "",
        expected_version=1,
        idempotency_key=f"confirm-blocker-{uuid4()}",
    )
    assert confirmed.status == "CONFIRMED"

    alternatives = await runtime.trips.alternatives(
        tenant_scope.user_context(), tenant_scope.trip_id, tenant_scope.slot_id
    )
    assert [str(item["slot_id"]) for item in alternatives] == [str(valid_id)]


@pytest.mark.postgres
@pytest.mark.redis_stack
async def test_operations_views_are_tenant_scoped(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    foreign_tenant, foreign_user, foreign_run = uuid4(), uuid4(), uuid4()
    foreign_thread = f"tenant:{foreign_tenant}:user:{foreign_user}:conversation:{uuid4()}"
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("INSERT INTO tripmate.tenant(tenant_id,name,status) VALUES(:id,:name,'ACTIVE')"),
            {"id": foreign_tenant, "name": f"ops-scope-{foreign_tenant}"},
        )
        await session.execute(
            text("""INSERT INTO tripmate.user_profile(user_id,tenant_id,display_name,status)
                VALUES(:user,:tenant,'Ops Peer','ACTIVE')"""),
            {"user": foreign_user, "tenant": foreign_tenant},
        )
        await session.execute(
            text("""INSERT INTO tripmate.agent_run
                (run_id,tenant_id,user_id,thread_id,intent,status,last_error_code)
                VALUES(:run,:tenant,:user,:thread,'UNCLASSIFIED','FAILED','FOREIGN_SCOPE_TEST')"""),
            {"run": foreign_run, "tenant": foreign_tenant, "user": foreign_user, "thread": foreign_thread},
        )
    local_token = issue_bearer_token(
        tenant_scope.tenant_id,
        tenant_scope.user_id,
        runtime.settings.auth_signing_secret.get_secret_value(),
    )
    foreign_token = issue_bearer_token(
        foreign_tenant,
        foreign_user,
        runtime.settings.auth_signing_secret.get_secret_value(),
    )
    try:
        with TestClient(create_app(runtime.settings)) as client:
            local_faults = client.get(
                "/v1/operations/faults", headers={"Authorization": f"Bearer {local_token}"}
            )
            local_metrics = client.get(
                "/v1/operations/metrics", headers={"Authorization": f"Bearer {local_token}"}
            )
            foreign_faults = client.get(
                "/v1/operations/faults", headers={"Authorization": f"Bearer {foreign_token}"}
            )
            foreign_metrics = client.get(
                "/v1/operations/metrics", headers={"Authorization": f"Bearer {foreign_token}"}
            )
        assert local_faults.status_code == local_metrics.status_code == 200
        assert foreign_faults.status_code == foreign_metrics.status_code == 200
        assert "FOREIGN_SCOPE_TEST" not in local_faults.text
        assert "FOREIGN_SCOPE_TEST" not in local_metrics.text
        assert "FOREIGN_SCOPE_TEST" in foreign_faults.text
        assert any(
            row["status"] == "FAILED"
            for row in foreign_metrics.json()["data"]["runs"]
        )
    finally:
        async with runtime.sessions() as session, session.begin():
            await session.execute(
                text("DELETE FROM tripmate.agent_run WHERE tenant_id=:tenant"),
                {"tenant": foreign_tenant},
            )
            await session.execute(
                text("DELETE FROM tripmate.user_profile WHERE tenant_id=:tenant"),
                {"tenant": foreign_tenant},
            )
            await session.execute(
                text("DELETE FROM tripmate.tenant WHERE tenant_id=:tenant"),
                {"tenant": foreign_tenant},
            )


@pytest.mark.postgres
@pytest.mark.redis_stack
async def test_500_parallel_reservations_preserve_inventory_and_report_latency(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    count = 500
    now = SystemClock().now()
    owner = f"load-{uuid4()}"
    trip_ids, slot_ids, run_ids = ([uuid4() for _ in range(count)] for _ in range(3))
    trip_rows = [
        {
            "trip": trip_ids[index],
            "tenant": tenant_scope.tenant_id,
            "user": tenant_scope.user_id,
            "start": tenant_scope.start_at.date(),
            "end": tenant_scope.start_at.date(),
        }
        for index in range(count)
    ]
    slot_rows = [
        {
            "slot": slot_ids[index],
            "tenant": tenant_scope.tenant_id,
            "attraction": tenant_scope.attraction_id,
            "external": str(slot_ids[index]),
            "start": tenant_scope.start_at,
            "end": tenant_scope.start_at + timedelta(hours=2),
        }
        for index in range(count)
    ]
    run_rows = [
        {
            "run": run_ids[index],
            "tenant": tenant_scope.tenant_id,
            "user": tenant_scope.user_id,
            "trip": trip_ids[index],
            "thread": f"load:{run_ids[index]}",
            "owner": owner,
            "until": now + timedelta(minutes=10),
        }
        for index in range(count)
    ]
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("""INSERT INTO tripmate.trip
                (trip_id,tenant_id,owner_user_id,destination,start_date,end_date,timezone,party_size,
                 budget_limit,currency,status)
                VALUES(:trip,:tenant,:user,'Load Test',:start,:end,'Asia/Shanghai',1,NULL,'CNY','PLANNING')"""),
            trip_rows,
        )
        await session.execute(
            text("""INSERT INTO tripmate.activity_slot
                (slot_id,tenant_id,attraction_id,external_id,start_at,end_at,capacity,price,currency,
                 cancellation_policy,policy_version,activity_version,status)
                VALUES(:slot,:tenant,:attraction,:external,:start,:end,1,25.00,'CNY',
                 CAST('{"policy_id":"load","policy_version":"load-v1","rules":[]}' AS jsonb),
                 'load-v1',1,'ACTIVE')"""),
            slot_rows,
        )
        await session.execute(
            text("""INSERT INTO tripmate.inventory
                (tenant_id,slot_id,total_quantity,available_quantity,status)
                VALUES(:tenant,:slot,1,1,'ACTIVE')"""),
            [{"tenant": tenant_scope.tenant_id, "slot": slot_id} for slot_id in slot_ids],
        )
        await session.execute(
            text("""INSERT INTO tripmate.agent_run
                (run_id,tenant_id,user_id,trip_id,thread_id,intent,status,lease_owner,lease_until,fencing_version)
                VALUES(:run,:tenant,:user,:trip,:thread,'UNCLASSIFIED','RUNNING',:owner,:until,1)"""),
            run_rows,
        )
    await runtime.projection.rebuild_all()

    async def prepare(index: int) -> tuple[UUID, UUID, UUID, str]:
        agent_ctx = replace(
            tenant_scope.agent_context(),
            run_id=run_ids[index],
            trip_id=trip_ids[index],
            thread_id=f"load:{run_ids[index]}",
            lease_owner=owner,
        )
        action_id, _parameters_hash = await runtime.reservations.create_candidate_action(
            agent_ctx,
            run_id=run_ids[index],
            action_type="propose_reservation",
            parameters={
                "trip_id": str(trip_ids[index]),
                "slot_id": str(slot_ids[index]),
                "quantity": 1,
                "reason": "isolated 500-request load test",
            },
        )
        confirmation = await runtime.reservations.issue_confirmation(
            tenant_scope.user_context(),
            action_id,
            idempotency_key=f"load-confirm-{index}-{uuid4()}",
        )
        return trip_ids[index], slot_ids[index], action_id, confirmation["confirmation_token"]

    prepared = await asyncio.gather(*(prepare(index) for index in range(count)))

    async def reserve(
        item: tuple[UUID, UUID, UUID, str]
    ) -> tuple[str, float]:
        trip_id, slot_id, action_id, token = item
        started = perf_counter()
        try:
            await runtime.reservations.hold(
                tenant_scope.user_context(),
                trip_id=trip_id,
                slot_id=slot_id,
                quantity=1,
                action_id=action_id,
                action_confirmation_token=token,
                expected_inventory_version=None,
                expected_activity_version=1,
                idempotency_key=f"load-hold-{action_id}",
            )
            status = "HELD"
        except DomainError as exc:
            status = exc.code
        return status, perf_counter() - started

    started = perf_counter()
    outcomes = await asyncio.gather(*(reserve(item) for item in prepared))
    elapsed = perf_counter() - started
    statuses = [status for status, _duration in outcomes]
    durations = sorted(duration for _status, duration in outcomes)
    p95_ms = round(durations[ceil(0.95 * count) - 1] * 1000, 2)
    async with runtime.sessions() as session:
        summary = (
            await session.execute(
                text("""SELECT count(*) AS slots,
                    COALESCE(sum(total_quantity),0) AS total,
                    COALESCE(sum(available_quantity),0) AS available,
                    COALESCE(sum(held_quantity),0) AS held,
                    COALESCE(sum(confirmed_quantity),0) AS confirmed,
                    count(*) FILTER (WHERE available_quantity+held_quantity+confirmed_quantity<>total_quantity)
                      AS violations
                    FROM tripmate.inventory WHERE tenant_id=:tenant AND total_quantity=1"""),
                {"tenant": tenant_scope.tenant_id},
            )
        ).mappings().one()
    report = {
        "requests": count,
        "succeeded": statuses.count("HELD"),
        "failed": count - statuses.count("HELD"),
        "elapsed_seconds": round(elapsed, 3),
        "throughput_per_second": round(count / elapsed, 2),
        "p95_request_ms": p95_ms,
        "inventory": {key: int(value) for key, value in summary.items()},
    }
    print(json.dumps(report, sort_keys=True))
    assert report["succeeded"] == count
    assert report["inventory"]["slots"] == count
    assert report["inventory"]["total"] == report["inventory"]["held"] == count
    assert report["inventory"]["available"] == report["inventory"]["confirmed"] == 0
    assert report["inventory"]["violations"] == 0


@pytest.mark.postgres
@pytest.mark.redis_stack
async def test_500_hot_slot_retries_with_one_key_create_one_reservation(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    action_id, confirmation = await _approved_candidate(tenant_scope)
    idempotency_key = f"hot-slot-retry-{uuid4()}"

    async def retry() -> tuple[UUID, bool, float]:
        started = perf_counter()
        result = await runtime.reservations.hold(
            tenant_scope.user_context(),
            trip_id=tenant_scope.trip_id,
            slot_id=tenant_scope.slot_id,
            quantity=1,
            action_id=action_id,
            action_confirmation_token=confirmation["confirmation_token"],
            expected_inventory_version=None,
            expected_activity_version=1,
            idempotency_key=idempotency_key,
        )
        return result.reservation_id, result.replayed, perf_counter() - started

    started = perf_counter()
    results = await asyncio.gather(*(retry() for _ in range(500)))
    elapsed = perf_counter() - started
    reservation_ids = {reservation_id for reservation_id, _replayed, _time in results}
    durations = sorted(duration for _reservation_id, _replayed, duration in results)
    async with runtime.sessions() as session:
        inventory = (
            await session.execute(
                text("""SELECT total_quantity,available_quantity,held_quantity,confirmed_quantity
                    FROM tripmate.inventory WHERE tenant_id=:tenant AND slot_id=:slot"""),
                {"tenant": tenant_scope.tenant_id, "slot": tenant_scope.slot_id},
            )
        ).mappings().one()
        reservation_count = await session.scalar(
            text("SELECT count(*) FROM tripmate.reservation WHERE tenant_id=:tenant"),
            {"tenant": tenant_scope.tenant_id},
        )
    report = {
        "requests": len(results),
        "unique_reservations": len(reservation_ids),
        "first_executions": sum(not replayed for _id, replayed, _duration in results),
        "elapsed_seconds": round(elapsed, 3),
        "throughput_per_second": round(len(results) / elapsed, 2),
        "p95_request_ms": round(durations[ceil(0.95 * len(results)) - 1] * 1000, 2),
        "inventory": {key: int(value) for key, value in inventory.items()},
        "reservation_rows": int(reservation_count or 0),
    }
    print(json.dumps(report, sort_keys=True))
    assert len(reservation_ids) == report["unique_reservations"] == 1
    assert report["first_executions"] == report["reservation_rows"] == 1
    assert inventory["available_quantity"] == 1
    assert inventory["held_quantity"] == 1
    assert inventory["available_quantity"] + inventory["held_quantity"] + inventory[
        "confirmed_quantity"
    ] == inventory["total_quantity"]


@pytest.mark.postgres
async def test_postgres_inventory_cas_is_concurrent_and_conserves_quantity(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    async def take_one() -> int:
        async with runtime.sessions() as session, session.begin():
            result = await session.execute(
                text("""UPDATE tripmate.inventory SET available_quantity=available_quantity-1,
                    held_quantity=held_quantity+1,version=version+1
                    WHERE tenant_id=:tenant AND slot_id=:slot AND version=1 AND available_quantity>=1"""),
                {"tenant": tenant_scope.tenant_id, "slot": tenant_scope.slot_id},
            )
            await asyncio.sleep(0.05)
            return affected_rows(result)

    results = await asyncio.gather(take_one(), take_one())
    assert sorted(results) == [0, 1]
    async with runtime.sessions() as session:
        row = (
            await session.execute(
                text("""SELECT total_quantity,available_quantity,held_quantity,confirmed_quantity
                    FROM tripmate.inventory WHERE tenant_id=:tenant AND slot_id=:slot"""),
                {"tenant": tenant_scope.tenant_id, "slot": tenant_scope.slot_id},
            )
        ).mappings().one()
    assert row["available_quantity"] + row["held_quantity"] + row["confirmed_quantity"] == row[
        "total_quantity"
    ]
    assert row["available_quantity"] == 1
    assert row["held_quantity"] == 1


@pytest.mark.postgres
async def test_postgres_composite_foreign_key_rejects_cross_tenant_activity(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    foreign_tenant, foreign_attraction = uuid4(), uuid4()
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("INSERT INTO tripmate.tenant(tenant_id,name,status) VALUES(:id,:name,'ACTIVE')"),
            {"id": foreign_tenant, "name": f"foreign-{foreign_tenant}"},
        )
        await session.execute(
            text("""INSERT INTO tripmate.attraction
                (attraction_id,tenant_id,external_source,external_id,city,name,category,source_version,status)
                VALUES(:id,:tenant,'integration',:external,'foreign','Foreign','scenic','v1','ACTIVE')"""),
            {
                "id": foreign_attraction,
                "tenant": foreign_tenant,
                "external": str(foreign_attraction),
            },
        )
    try:
        with pytest.raises(IntegrityError):
            async with runtime.sessions() as session, session.begin():
                await session.execute(
                    text("""INSERT INTO tripmate.activity_slot
                        (tenant_id,attraction_id,start_at,end_at,capacity,price,currency,
                         cancellation_policy,policy_version,status)
                        VALUES(:tenant,:attraction,:start,:end,1,1.00,'CNY','{}'::jsonb,'v1','ACTIVE')"""),
                    {
                        "tenant": tenant_scope.tenant_id,
                        "attraction": foreign_attraction,
                        "start": tenant_scope.start_at,
                        "end": tenant_scope.start_at + timedelta(hours=1),
                    },
                )
    finally:
        async with runtime.sessions() as session, session.begin():
            await session.execute(
                text("DELETE FROM tripmate.attraction WHERE tenant_id=:tenant"),
                {"tenant": foreign_tenant},
            )
            await session.execute(
                text("DELETE FROM tripmate.tenant WHERE tenant_id=:tenant"),
                {"tenant": foreign_tenant},
            )


@pytest.mark.postgres
async def test_committed_hold_unknown_ack_and_idempotent_retry_restore_credentials(
    runtime: Runtime,
    tenant_scope: IntegrationScope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action_id, action_confirmation = await _approved_candidate(tenant_scope)
    key = f"unknown-commit-{uuid4()}"
    original_commit = AsyncSessionTransaction.commit
    raised = False

    async def commit_then_lose_ack(transaction: AsyncSessionTransaction) -> None:
        nonlocal raised
        await original_commit(transaction)
        if not raised:
            raised = True
            raise DBAPIError(
                None,
                None,
                RuntimeError("simulated lost commit acknowledgement"),
                connection_invalidated=True,
            )

    monkeypatch.setattr(AsyncSessionTransaction, "commit", commit_then_lose_ack)
    first = await runtime.reservations.hold(
        tenant_scope.user_context(),
        trip_id=tenant_scope.trip_id,
        slot_id=tenant_scope.slot_id,
        quantity=1,
        action_id=action_id,
        action_confirmation_token=action_confirmation["confirmation_token"],
        expected_inventory_version=None,
        expected_activity_version=1,
        idempotency_key=key,
    )
    monkeypatch.setattr(AsyncSessionTransaction, "commit", original_commit)
    second = await runtime.reservations.hold(
        tenant_scope.user_context(),
        trip_id=tenant_scope.trip_id,
        slot_id=tenant_scope.slot_id,
        quantity=1,
        action_id=action_id,
        action_confirmation_token=action_confirmation["confirmation_token"],
        expected_inventory_version=None,
        expected_activity_version=1,
        idempotency_key=key,
    )

    assert raised
    assert first.reservation_id == second.reservation_id
    assert first.hold_token == second.hold_token
    assert first.confirmation_token == second.confirmation_token
    assert second.replayed
    async with runtime.sessions() as session:
        count = await session.scalar(
            text("""SELECT count(*) FROM tripmate.reservation
                WHERE tenant_id=:tenant AND slot_id=:slot"""),
            {"tenant": tenant_scope.tenant_id, "slot": tenant_scope.slot_id},
        )
    assert count == 1


@pytest.mark.postgres
async def test_confirm_and_expire_compete_for_one_inventory_transition(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    action_id, _action_confirmation, held = await _approved_hold(tenant_scope)
    expiry = SystemClock().now() + timedelta(seconds=0.1)
    confirm_now = expiry - timedelta(seconds=1)
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("UPDATE tripmate.reservation SET hold_expires_at=:expires WHERE reservation_id=:id"),
            {"expires": expiry, "id": held.reservation_id},
        )
        await session.execute(
            text("""UPDATE tripmate.reservation_confirmation SET expires_at=:expires
                WHERE reservation_id=:id"""),
            {"expires": expiry + timedelta(minutes=1), "id": held.reservation_id},
        )
    await asyncio.sleep(0.12)
    confirm_service = ReservationService(
        runtime.sessions,
        runtime.settings,
        _FixedClock(confirm_now),
        runtime.reservations.ids,
        runtime.projection,
    )
    confirm_result, expire_result = await asyncio.gather(
        confirm_service.confirm(
            tenant_scope.user_context(),
            reservation_id=held.reservation_id,
            action_id=action_id,
            confirmation_token=held.confirmation_token or "",
            expected_version=1,
            idempotency_key=f"confirm-race-{uuid4()}",
        ),
        runtime.reservations.expire(tenant_scope.tenant_id, held.reservation_id),
        return_exceptions=True,
    )
    confirmed = not isinstance(confirm_result, Exception)
    expired = expire_result is True
    assert confirmed ^ expired
    async with runtime.sessions() as session:
        row = (
            await session.execute(
                text("""SELECT r.status,i.total_quantity,i.available_quantity,i.held_quantity,
                    i.confirmed_quantity FROM tripmate.reservation r JOIN tripmate.inventory i
                      ON i.tenant_id=r.tenant_id AND i.slot_id=r.slot_id
                    WHERE r.tenant_id=:tenant AND r.reservation_id=:reservation"""),
                {"tenant": tenant_scope.tenant_id, "reservation": held.reservation_id},
            )
        ).mappings().one()
    assert row["status"] == ("CONFIRMED" if confirmed else "EXPIRED")
    assert row["available_quantity"] + row["held_quantity"] + row["confirmed_quantity"] == row[
        "total_quantity"
    ]
    assert row["held_quantity"] == 0


@pytest.mark.postgres
@pytest.mark.redis_stack
async def test_inventory_projection_rebuild_uses_committed_postgres_rows(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    _action_id, _confirmation, held = await _approved_hold(tenant_scope)
    stock_key = runtime.projection.keys.inventory(tenant_scope.tenant_id, tenant_scope.slot_id)
    hold_key = runtime.projection.keys.hold(tenant_scope.tenant_id, held.reservation_id)
    expiry_key = runtime.projection.keys.expiry(tenant_scope.tenant_id)
    await runtime.redis.delete(stock_key, hold_key, expiry_key)

    assert await runtime.projection.rebuild_tenant(tenant_scope.tenant_id) == 1
    stock = await runtime.projection.read(tenant_scope.tenant_id, tenant_scope.slot_id)
    assert stock is not None
    assert (int(stock["total"]), int(stock["available"]), int(stock["held"])) == (2, 1, 1)
    assert await runtime.redis.exists(hold_key) == 1
    assert await runtime.redis.zscore(expiry_key, str(held.reservation_id)) is not None


@pytest.mark.postgres
@pytest.mark.redis_stack
async def test_stream_replay_and_pending_recovery_use_inbox_fencing(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    event = EventEnvelope(
        event_id=uuid4(),
        event_type="trip.updated.v1",
        event_version=1,
        occurred_at=SystemClock().now(),
        tenant_id=tenant_scope.tenant_id,
        aggregate_type="trip",
        aggregate_id=tenant_scope.trip_id,
        aggregate_version=2,
        request_id="integration-stream",
        trace_id="integration-stream-trace",
        producer="integration-test",
        payload={"trip_id": str(tenant_scope.trip_id), "version": 2},
    )
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("""INSERT INTO tripmate.outbox_event
                (event_id,tenant_id,aggregate_type,aggregate_id,aggregate_version,event_type,
                 event_version,payload,status)
                VALUES(:event,:tenant,'trip',:trip,2,:type,1,CAST(:payload AS jsonb),'PENDING')"""),
            {
                "event": event.event_id,
                "tenant": event.tenant_id,
                "trip": event.aggregate_id,
                "type": event.event_type,
                "payload": event.model_dump_json(),
            },
        )
    publisher = OutboxPublisher(runtime.sessions, runtime.redis, runtime.settings)
    assert await publisher.publish_batch() >= 1
    stream = runtime.projection.keys.stream(tenant_scope.tenant_id, "trips")
    await runtime.redis.delete(stream)
    assert await publisher.replay_stream(tenant_scope.tenant_id, "trips") == 1
    async with runtime.sessions() as session:
        cursor = await session.scalar(
            text("""SELECT cursor_event_id FROM tripmate.outbox_replay_cursor
                WHERE tenant_id=:tenant AND stream_name=:stream"""),
            {"tenant": tenant_scope.tenant_id, "stream": stream},
        )
    assert cursor == event.event_id

    attempts = 0

    async def fail_once(_session: Any, received: EventEnvelope) -> None:
        nonlocal attempts
        if received.event_id == event.event_id and attempts == 0:
            attempts += 1
            raise RuntimeError("transient handler failure")

    processor = InboxProcessor(
        runtime.sessions,
        runtime.redis,
        runtime.settings,
        consumer=f"integration-{uuid4()}",
        group="tripmate-integration-v1",
        handler=fail_once,
    )
    assert await processor.consume_once(tenant_scope.tenant_id, "trips", block_ms=1) == 0
    await asyncio.sleep(1.1)
    assert await processor.claim_pending(tenant_scope.tenant_id, "trips") == 1
    async with runtime.sessions() as session:
        inbox = (
            await session.execute(
                text("""SELECT status,attempt_count,fencing_version FROM tripmate.inbox_event
                    WHERE event_id=:event AND consumer=:consumer"""),
                {"event": event.event_id, "consumer": processor.consumer},
            )
        ).mappings().one()
        watermark = await session.scalar(
            text("""SELECT safe_message_id FROM tripmate.stream_group_watermark
                WHERE tenant_id=:tenant AND stream_name=:stream AND group_name=:group_name"""),
            {
                "tenant": tenant_scope.tenant_id,
                "stream": stream,
                "group_name": processor.group,
            },
        )
    assert inbox["status"] == "PROCESSED"
    assert inbox["attempt_count"] == 2
    assert inbox["fencing_version"] == 2
    assert watermark is not None


@pytest.mark.postgres
async def test_expired_run_lease_is_reclaimed_and_stale_fence_cannot_finish(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("""UPDATE tripmate.agent_run SET lease_owner='old-worker',lease_until=:expired,
                fencing_version=8,status='RUNNING' WHERE tenant_id=:tenant AND run_id=:run"""),
            {
                "expired": SystemClock().now() - timedelta(seconds=1),
                "tenant": tenant_scope.tenant_id,
                "run": tenant_scope.run_id,
            },
        )
    claim = await runtime.runs.claim_run(
        tenant_scope.tenant_id, tenant_scope.run_id, "new-worker", lease_seconds=30
    )
    assert claim is not None
    assert int(claim["fencing_version"]) == 9
    assert not await runtime.runs.finish_run(
        tenant_scope.tenant_id, tenant_scope.run_id, "old-worker", 8, {"result": {}}
    )
    assert await runtime.runs.finish_run(
        tenant_scope.tenant_id,
        tenant_scope.run_id,
        "new-worker",
        int(claim["fencing_version"]),
        {"result": {"status": "recovered"}},
    )


@pytest.mark.postgres
async def test_preference_idempotency_tombstone_and_async_physical_delete(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    ctx = tenant_scope.user_context()
    save_key = f"preference-save-{uuid4()}"
    saved = await runtime.preferences.save(
        ctx,
        scope="personal",
        key="dietary_restriction.tags",
        value=["Vegetarian"],
        idempotency_key=save_key,
    )
    replay = await runtime.preferences.save(
        ctx,
        scope="personal",
        key="dietary_restriction.tags",
        value=["Vegetarian"],
        idempotency_key=save_key,
    )
    assert saved["preference_id"] == replay["preference_id"]
    assert saved["version"] == replay["version"]
    with pytest.raises(DomainError, match="Idempotency key was reused"):
        await runtime.preferences.save(
            ctx,
            scope="personal",
            key="dietary_restriction.tags",
            value=["Vegan"],
            idempotency_key=save_key,
        )

    vector_id = uuid4()
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("""INSERT INTO tripmate.private_case_vector
                (vector_id,tenant_id,user_id,generation,content_redacted,content_hash)
                VALUES(:id,:tenant,:user,1,'redacted private example',:hash)"""),
            {
                "id": vector_id,
                "tenant": tenant_scope.tenant_id,
                "user": tenant_scope.user_id,
                "hash": "a" * 64,
            },
        )
    deleted = await runtime.preferences.delete(
        ctx,
        saved["preference_id"],
        expected_version=saved["version"],
        idempotency_key=f"preference-delete-{uuid4()}",
    )
    assert deleted["deleted"] is True
    assert await runtime.preferences.list_for_user(ctx) == []
    assert await runtime.knowledge.search_private_cases(
        ctx, "private example", current_generation=deleted["generation"]
    ) == []
    assert await RunWorker(runtime)._purge_tombstoned_private_cases() == 1
    async with runtime.sessions() as session:
        assert await session.scalar(
            text("SELECT count(*) FROM tripmate.private_case_vector WHERE vector_id=:id"),
            {"id": vector_id},
        ) == 0


@pytest.mark.postgres
async def test_agent_preference_action_requires_confirmation_and_resumes_idempotently(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    candidate = await runtime.tools.invoke(
        "save_user_preference",
        {
            "scope": "personal",
            "key": "travel_style.tags",
            "value": ["indoor"],
            "reason": "explicit user request",
        },
        tenant_scope.agent_context(),
    )
    action_id = UUID(candidate["action_id"])
    assert not await runtime.preferences.list_for_user(tenant_scope.user_context())

    issued = await runtime.reservations.issue_confirmation(
        tenant_scope.user_context(), action_id, idempotency_key=f"issue-{uuid4()}"
    )
    with pytest.raises(DomainError):
        await runtime.reservations.confirm_preference_action(
            tenant_scope.user_context(),
            action_id,
            confirmation_token="invalid-confirmation-token",
            idempotency_key=f"reject-{uuid4()}",
        )
    assert not await runtime.preferences.list_for_user(tenant_scope.user_context())

    key = f"confirm-preference-{uuid4()}"
    confirmed = await runtime.reservations.confirm_preference_action(
        tenant_scope.user_context(),
        action_id,
        confirmation_token=issued["confirmation_token"],
        idempotency_key=key,
    )
    replay = await runtime.reservations.confirm_preference_action(
        tenant_scope.user_context(),
        action_id,
        confirmation_token=issued["confirmation_token"],
        idempotency_key=key,
    )
    preferences = await runtime.preferences.list_for_user(tenant_scope.user_context())

    assert confirmed["status"] == "EXECUTED"
    assert replay == confirmed
    assert [item["preference_key"] for item in preferences] == ["travel_style.tags"]
    assert preferences[0]["value_json"] == ["indoor"]
    async with runtime.sessions() as session:
        run = (
            await session.execute(
                text("""SELECT status,resume_command FROM tripmate.agent_run
                    WHERE tenant_id=:tenant AND run_id=:run"""),
                {"tenant": tenant_scope.tenant_id, "run": tenant_scope.run_id},
            )
        ).mappings().one()
    assert run["status"] == "RUNNING"
    assert run["resume_command"]["decision"] == "approved"
    event_batch = await runtime.runs.get_run_event_batch(
        tenant_scope.user_context(), tenant_scope.run_id, after_sequence=0
    )
    assert [item["event_type"] for item in event_batch["events"]].count("confirmation") == 2
    assert {item["payload"]["status"] for item in event_batch["events"] if item["event_type"] == "confirmation"} == {
        "TOKEN_ISSUED",
        "CONFIRMED",
    }


@pytest.mark.postgres
async def test_preference_rejection_is_idempotent_and_does_not_write_the_value(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    candidate = await runtime.tools.invoke(
        "save_user_preference",
        {
            "scope": "personal",
            "key": "travel_style.tags",
            "value": ["museum"],
            "reason": "explicit user request",
        },
        tenant_scope.agent_context(),
    )
    action_id = UUID(candidate["action_id"])
    issued = await runtime.reservations.issue_confirmation(
        tenant_scope.user_context(), action_id, idempotency_key=f"issue-reject-{uuid4()}"
    )
    reject_key = f"reject-preference-{uuid4()}"

    rejected = await runtime.reservations.reject_action(
        tenant_scope.user_context(), action_id, expected_version=1, idempotency_key=reject_key
    )
    replay = await runtime.reservations.reject_action(
        tenant_scope.user_context(), action_id, expected_version=1, idempotency_key=reject_key
    )

    assert rejected == replay
    assert rejected["status"] == "REJECTED"
    assert str(issued["action_id"]) == str(action_id)
    assert await runtime.preferences.list_for_user(tenant_scope.user_context()) == []
    async with runtime.sessions() as session:
        run = (
            await session.execute(
                text("SELECT status,resume_command FROM tripmate.agent_run WHERE tenant_id=:tenant AND run_id=:run"),
                {"tenant": tenant_scope.tenant_id, "run": tenant_scope.run_id},
            )
        ).mappings().one()
    assert run["status"] == "RUNNING"
    assert run["resume_command"]["decision"] == "rejected"
    event_batch = await runtime.runs.get_run_event_batch(
        tenant_scope.user_context(), tenant_scope.run_id, after_sequence=0
    )
    assert [item["payload"]["status"] for item in event_batch["events"] if item["event_type"] == "confirmation"] == [
        "TOKEN_ISSUED",
        "REJECTED",
    ]


@pytest.mark.postgres
async def test_replanning_keeps_confirmed_trip_item_and_reservation_price_snapshot(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    action_id, _action_confirmation, held = await _approved_hold(tenant_scope)
    confirmed = await runtime.reservations.confirm(
        tenant_scope.user_context(),
        reservation_id=held.reservation_id,
        action_id=action_id,
        confirmation_token=held.confirmation_token or "",
        expected_version=1,
        idempotency_key=f"confirm-for-replan-{uuid4()}",
    )
    assert confirmed.status == "CONFIRMED"
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("UPDATE tripmate.activity_slot SET price=999.00 WHERE tenant_id=:tenant AND slot_id=:slot"),
            {"tenant": tenant_scope.tenant_id, "slot": tenant_scope.slot_id},
        )
    worker_id = f"replan-{uuid4()}"
    claim = await runtime.runs.claim_run(
        tenant_scope.tenant_id, tenant_scope.run_id, worker_id, lease_seconds=60
    )
    assert claim is not None
    proposed_plan = {
        "feasible": True,
        "items": [
            {
                "slot_id": str(tenant_scope.slot_id),
                "window": {
                    "start_at": tenant_scope.start_at,
                    "end_at": tenant_scope.start_at + timedelta(hours=2),
                },
                "cost": {"amount": "999.00", "currency": "CNY"},
            }
        ],
    }
    await _persist_plan(
        SimpleNamespace(
            sessions=runtime.sessions,
            clock=runtime.reservations.clock,
            ids=runtime.reservations.ids,
        ),
        {
            "tenant_id": tenant_scope.tenant_id,
            "user_id": tenant_scope.user_id,
            "run_id": tenant_scope.run_id,
            "trip_id": tenant_scope.trip_id,
            "lease_owner": worker_id,
            "fencing_version": int(claim["fencing_version"]),
        },
        proposed_plan,
        confirmed_slot_ids=frozenset({str(tenant_scope.slot_id)}),
    )

    async with runtime.sessions() as session:
        row = (
            await session.execute(
                text("""SELECT ti.reservation_id,ti.cost,r.price_snapshot
                    FROM tripmate.trip_item ti JOIN tripmate.reservation r
                      ON r.tenant_id=ti.tenant_id AND r.reservation_id=ti.reservation_id
                    WHERE ti.tenant_id=:tenant AND ti.trip_id=:trip"""),
                {"tenant": tenant_scope.tenant_id, "trip": tenant_scope.trip_id},
            )
        ).mappings().one()
    assert row["reservation_id"] == held.reservation_id
    assert row["price_snapshot"] == Decimal("25.00")
    assert row["cost"] == row["price_snapshot"]


@pytest.mark.postgres
async def test_knowledge_index_evaluates_switches_rolls_back_and_filters_deletions(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    index = KnowledgeIndex(runtime.sessions, runtime.settings)
    legacy_manifest = uuid4()
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("""INSERT INTO tripmate.knowledge_manifest
                (manifest_id,tenant_id,index_version,index_alias,embedding_model,vector_dimensions,
                 status,document_count,chunk_count,validated_at)
                VALUES(:manifest,:tenant,'integration-v0',:alias,'legacy-lexical-v1',1,
                 'ACTIVE',0,0,now())"""),
            {
                "manifest": legacy_manifest,
                "tenant": tenant_scope.tenant_id,
                "alias": runtime.settings.rag_index_alias,
            },
        )
        await session.execute(
            text("""INSERT INTO tripmate.knowledge_active_alias
                (tenant_id,index_alias,manifest_id,generation)
                VALUES(:tenant,:alias,:manifest,1)"""),
            {
                "tenant": tenant_scope.tenant_id,
                "alias": runtime.settings.rag_index_alias,
                "manifest": legacy_manifest,
            },
        )

    built = await index.build(tenant_scope.tenant_id)
    assert built["metrics"]["recall_at_5"] == 1.0
    assert built["metrics"]["mrr"] == 1.0
    assert built["metrics"]["citation_correctness"] == 1.0
    assert built["metrics"]["index_version"] == built["index_version"]
    assert built["generation"] == 2
    citation = await runtime.knowledge.search(
        tenant_scope.user_context(), "雨天如何调整活动安排", top_k=4
    )
    assert citation[0].title == "天气变化与室内偏好"
    assert citation[0].source_url.startswith("repo://")
    other_tenant, other_user = uuid4(), uuid4()
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("INSERT INTO tripmate.tenant(tenant_id,name,status) VALUES(:tenant,:name,'ACTIVE')"),
            {"tenant": other_tenant, "name": f"Other KB tenant {other_tenant}"},
        )
        await session.execute(
            text("""INSERT INTO tripmate.user_profile(user_id,tenant_id,display_name,status)
                VALUES(:user,:tenant,'Other KB user','ACTIVE')"""),
            {"tenant": other_tenant, "user": other_user},
        )
    try:
        assert await runtime.knowledge.search(
            replace(tenant_scope.user_context(), tenant_id=other_tenant, user_id=other_user),
            "雨天室内活动",
            top_k=4,
        ) == []
    finally:
        async with runtime.sessions() as session, session.begin():
            await session.execute(
                text("DELETE FROM tripmate.user_profile WHERE tenant_id=:tenant"),
                {"tenant": other_tenant},
            )
            await session.execute(
                text("DELETE FROM tripmate.tenant WHERE tenant_id=:tenant"),
                {"tenant": other_tenant},
            )
    async with runtime.sessions() as session:
        evaluation = (
            await session.execute(
                text("""SELECT case_count,metrics FROM tripmate.knowledge_evaluation_run
                    WHERE tenant_id=:tenant AND index_version=:version"""),
                {"tenant": tenant_scope.tenant_id, "version": built["index_version"]},
            )
        ).mappings().one()
    assert evaluation["case_count"] == 4
    assert evaluation["metrics"] == built["metrics"]

    replay = await index.build(tenant_scope.tenant_id)
    assert replay["generation"] == built["generation"]
    rolled_back = await index.rollback(tenant_scope.tenant_id, "integration-v0")
    assert rolled_back["generation"] == 3
    assert await runtime.knowledge.search(
        tenant_scope.user_context(), "雨天如何调整活动安排", top_k=4
    ) == []
    await index.rollback(tenant_scope.tenant_id, built["index_version"])

    deleted = await index.delete_document(
        tenant_scope.tenant_id, "tripmate-weather-and-indoor-planning"
    )
    assert deleted == 1
    after_delete = await runtime.knowledge.search(
        tenant_scope.user_context(), "雨天如何调整活动安排", top_k=4
    )
    assert all(item.title != "天气变化与室内偏好" for item in after_delete)
    with pytest.raises(ValueError, match="deleted sources"):
        await index.rollback(tenant_scope.tenant_id, built["index_version"])


@pytest.mark.postgres
@pytest.mark.redis_stack
async def test_worker_recovers_checkpoint_and_writes_one_terminal_event(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    class RecoveryState(TypedDict, total=False):
        decision: dict[str, bool]
        result: dict[str, Any]

    async def approval(_state: RecoveryState) -> dict[str, Any]:
        decision = interrupt({"action_id": "recovery-action"})
        return {"decision": decision}

    async def finish(state: RecoveryState) -> dict[str, Any]:
        return {"result": {"decision": state["decision"]}}

    graph_builder = StateGraph(RecoveryState)
    graph_builder.add_node("approval", approval)
    graph_builder.add_node("finish", finish)
    graph_builder.add_edge(START, "approval")
    graph_builder.add_edge("approval", "finish")
    graph_builder.add_edge("finish", END)
    graph = graph_builder.compile(checkpointer=runtime.checkpointer)
    config = {"configurable": {"thread_id": tenant_scope.thread_id}}
    try:
        paused = await graph.ainvoke({"decision": {"approved": False}}, config)
        assert "__interrupt__" in paused
        async with runtime.sessions() as session, session.begin():
            await session.execute(
                text("""UPDATE tripmate.agent_run SET status='RUNNING',awaiting_action_id=NULL,
                    resume_command=CAST(:resume AS jsonb),lease_owner='crashed-worker',
                    lease_until=:expired,fencing_version=1
                    WHERE tenant_id=:tenant AND run_id=:run"""),
                {
                    "resume": json.dumps({"approved": True}),
                    "expired": SystemClock().now() - timedelta(seconds=1),
                    "tenant": tenant_scope.tenant_id,
                    "run": tenant_scope.run_id,
                },
            )
        worker = RunWorker(runtime, worker_id=f"recovery-{uuid4()}")
        worker.graph = graph

        assert await worker.run_once()
        assert not await worker.run_once()
        run = await runtime.runs.get_run(tenant_scope.user_context(), tenant_scope.run_id)
        assert run["status"] == "COMPLETED"
        assert run["result_json"]["result"]["decision"] == {"approved": True}
        events = await runtime.runs.get_run_event_batch(
            tenant_scope.user_context(), tenant_scope.run_id, after_sequence=0
        )
        assert [item["event_type"] for item in events["events"]].count("done") == 1
    finally:
        await runtime.checkpointer.adelete_thread(tenant_scope.thread_id)


@pytest.mark.postgres
async def test_run_failure_event_is_committed_with_failed_status(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    assert await runtime.runs.fail_run(
        tenant_scope.tenant_id,
        tenant_scope.run_id,
        "integration-fixture",
        1,
        "AGENT_EXECUTION_FAILED",
    )

    run = await runtime.runs.get_run(tenant_scope.user_context(), tenant_scope.run_id)
    events = await runtime.runs.get_run_event_batch(
        tenant_scope.user_context(), tenant_scope.run_id, after_sequence=0
    )

    assert run["status"] == "FAILED"
    assert run["last_error_code"] == "AGENT_EXECUTION_FAILED"
    assert [item["payload"]["error_code"] for item in events["events"] if item["event_type"] == "error"] == [
        "AGENT_EXECUTION_FAILED"
    ]


@pytest.mark.postgres
@pytest.mark.redis_stack
async def test_run_sse_resumes_after_cursor_and_enforces_run_ownership(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    ctx = tenant_scope.user_context()
    citation = await runtime.runs.append_run_event(
        ctx,
        tenant_scope.run_id,
        "citation",
        {"citations": [{"chunk_id": "chunk-a", "index_version": "v1"}]},
        event_key="citation:cursor-test",
    )
    await runtime.runs.append_run_event(
        ctx,
        tenant_scope.run_id,
        "node_started",
        {"node": "tool_selection"},
        event_key="node:cursor-test",
    )
    await runtime.runs.append_run_event(
        ctx,
        tenant_scope.run_id,
        "tool_start",
        {"tool_id": "get_trip_context"},
        event_key="tool:cursor-test",
    )
    assert await runtime.runs.finish_run(
        tenant_scope.tenant_id,
        tenant_scope.run_id,
        "integration-fixture",
        1,
        {"result": {"status": "done"}},
    )
    async with runtime.sessions() as session, session.begin():
        other_user = uuid4()
        await session.execute(
            text("""INSERT INTO tripmate.user_profile(user_id,tenant_id,display_name,status)
                VALUES(:user,:tenant,'Other integration user','ACTIVE')"""),
            {"user": other_user, "tenant": tenant_scope.tenant_id},
        )
    token = issue_bearer_token(
        tenant_scope.tenant_id,
        tenant_scope.user_id,
        runtime.settings.auth_signing_secret.get_secret_value(),
    )
    other_token = issue_bearer_token(
        tenant_scope.tenant_id,
        other_user,
        runtime.settings.auth_signing_secret.get_secret_value(),
    )
    with TestClient(create_app(runtime.settings)) as client:
        response = client.get(
            f"/v1/runs/{tenant_scope.run_id}/events",
            headers={"Authorization": f"Bearer {token}", "Last-Event-ID": str(citation["event_sequence"])},
        )
        denied = client.get(
            f"/v1/runs/{tenant_scope.run_id}/events",
            headers={"Authorization": f"Bearer {other_token}"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: citation" not in response.text
    assert "event: tool_start" in response.text
    assert "event: node_started" in response.text
    assert "event: done" in response.text
    assert denied.status_code == 404


@pytest.mark.redis_stack
async def test_short_term_memory_is_versioned_scoped_and_expires(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    conversation_id = uuid4()
    ctx = tenant_scope.user_context()
    message_id = uuid4()
    initial = await runtime.short_term_memory.merge(
        ctx,
        conversation_id,
        MemoryPatch(
            trip_id=tenant_scope.trip_id,
            selected_slot_ids=[str(tenant_scope.slot_id)],
            unresolved_slots=["budget"],
            message_id=message_id,
            message_hash="a" * 64,
        ),
    )
    assert initial["version"] == 1
    assert initial["message_sequence"] == 1

    await asyncio.gather(
        runtime.short_term_memory.merge(
            ctx,
            conversation_id,
            MemoryPatch(confirmed_slot_ids=[str(tenant_scope.slot_id)]),
        ),
        runtime.short_term_memory.merge(
            ctx,
            conversation_id,
            MemoryPatch(citation_ids=[str(uuid4())], message_id=message_id),
        ),
    )
    saved = await runtime.short_term_memory.get(ctx, conversation_id)
    assert saved is not None
    assert saved["version"] == 3
    assert saved["message_sequence"] == 1
    assert saved["selected_slot_ids"] == [str(tenant_scope.slot_id)]
    assert saved["confirmed_slot_ids"] == [str(tenant_scope.slot_id)]
    assert await runtime.redis.ttl(
        runtime.short_term_memory.keys.conversation(
            tenant_scope.tenant_id, tenant_scope.user_id, conversation_id
        )
    ) > 0

    other_user = replace(ctx, user_id=uuid4())
    assert await runtime.short_term_memory.get(other_user, conversation_id) is None

    key = runtime.short_term_memory.keys.conversation(
        tenant_scope.tenant_id, tenant_scope.user_id, conversation_id
    )
    await runtime.redis.expire(key, 1)
    await asyncio.sleep(1.1)
    assert await runtime.short_term_memory.get(ctx, conversation_id) is None


@pytest.mark.postgres
async def test_expired_idempotency_key_can_be_reused(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    action_id, _ = await _approved_candidate(tenant_scope)
    key = f"confirmation-reused-{uuid4()}"
    route = f"/v1/actions/{action_id}/confirmation"
    first = await runtime.reservations.issue_confirmation(
        tenant_scope.user_context(), action_id, idempotency_key=key
    )
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("""UPDATE tripmate.idempotency_record SET expires_at=now()-interval '1 second'
                WHERE tenant_id=:tenant AND user_id=:user AND route=:route
                  AND operation='action.confirmation.issue'"""),
            {
                "tenant": tenant_scope.tenant_id,
                "user": tenant_scope.user_id,
                "route": route,
            },
        )

    second = await runtime.reservations.issue_confirmation(
        tenant_scope.user_context(), action_id, idempotency_key=key
    )
    assert second["version"] == first["version"] + 1
    assert second["confirmation_token"] != first["confirmation_token"]


@pytest.mark.postgres
async def test_evaluation_replay_records_tools_constraints_citations_and_is_idempotent(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    case_id = uuid4()
    citations = [{"chunk_id": "citation-contract"}]
    async with runtime.sessions() as session, session.begin():
        await session.execute(
            text("""UPDATE tripmate.agent_run SET status='COMPLETED',
                result_json=CAST(:result AS jsonb) WHERE tenant_id=:tenant AND run_id=:run"""),
            {
                "result": json.dumps(
                    {
                        "result": {"intent": "planning"},
                        "constraint_result": {"state": "PASS"},
                        "citations": citations,
                    }
                ),
                "tenant": tenant_scope.tenant_id,
                "run": tenant_scope.run_id,
            },
        )
        await session.execute(
            text("""INSERT INTO tripmate.agent_tool_call
                (tenant_id,run_id,user_id,tool_name,arguments_hash,result_code)
                VALUES(:tenant,:run,:user,'get_trip_context',:hash,'OK')"""),
            {
                "tenant": tenant_scope.tenant_id,
                "run": tenant_scope.run_id,
                "user": tenant_scope.user_id,
                "hash": "a" * 64,
            },
        )
        await session.execute(
            text("""INSERT INTO tripmate.evaluation_case
                (tenant_id,case_id,case_version,user_id,conversation_id,trip_id,name,input_json,
                 expected_json,active)
                VALUES(:tenant,:case,1,:user,:conversation,:trip,'contract',CAST('{}' AS jsonb),
                  CAST(:expected AS jsonb),true)"""),
            {
                "tenant": tenant_scope.tenant_id,
                "case": case_id,
                "user": tenant_scope.user_id,
                "conversation": uuid4(),
                "trip": tenant_scope.trip_id,
                "expected": json.dumps(
                    {
                        "intent": "planning",
                        "expected_tools": ["get_trip_context"],
                        "allowed_tools": ["get_trip_context"],
                        "constraint_state": "PASS",
                        "required_citation_ids": ["citation-contract"],
                        "reservation_status": None,
                    }
                ),
            },
        )

    evaluator = EvaluationReplayer(
        runtime.sessions,
        runtime.runs,
        runtime.reservations.clock,
        runtime.reservations.ids,
    )
    ctx = tenant_scope.user_context()
    report = await evaluator.evaluate_run(ctx, case_id, tenant_scope.run_id)
    repeated = await evaluator.evaluate_run(ctx, case_id, tenant_scope.run_id)
    assert report["status"] == "PASSED"
    assert repeated["result_hash"] == report["result_hash"]
    async with runtime.sessions() as session:
        assert await session.scalar(
            text("""SELECT count(*) FROM tripmate.evaluation_result
                WHERE tenant_id=:tenant AND case_id=:case AND run_id=:run"""),
            {"tenant": tenant_scope.tenant_id, "case": case_id, "run": tenant_scope.run_id},
        ) == 1


@pytest.mark.postgres
async def test_evaluation_case_rejects_empty_expectations(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    with pytest.raises(IntegrityError):
        async with runtime.sessions() as session, session.begin():
            await session.execute(
                text("""INSERT INTO tripmate.evaluation_case
                    (tenant_id,case_id,case_version,user_id,conversation_id,trip_id,name,input_json,
                     expected_json,active)
                    VALUES(:tenant,:case,1,:user,:conversation,:trip,'empty-contract',
                      CAST('{}' AS jsonb),CAST('{}' AS jsonb),true)"""),
                {
                    "tenant": tenant_scope.tenant_id,
                    "case": uuid4(),
                    "user": tenant_scope.user_id,
                    "conversation": uuid4(),
                    "trip": tenant_scope.trip_id,
                },
            )


class _ApprovalState(TypedDict, total=False):
    action_id: str
    decision: dict[str, bool]
    completed: bool


class _FixedClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


@pytest.mark.redis_stack
async def test_real_redis_saver_preserves_interrupt_pending_writes_and_resume(
    runtime: Runtime, tenant_scope: IntegrationScope
) -> None:
    approval_calls = 0
    completions: list[str] = []

    async def approval(state: _ApprovalState) -> dict[str, Any]:
        nonlocal approval_calls
        approval_calls += 1
        decision = interrupt({"action_id": state["action_id"]})
        return {"decision": decision}

    async def complete(state: _ApprovalState) -> dict[str, Any]:
        completions.append(state["action_id"])
        return {"completed": True}

    builder = StateGraph(_ApprovalState)
    builder.add_node("approval", approval)
    builder.add_node("complete", complete)
    builder.add_edge(START, "approval")
    builder.add_edge("approval", "complete")
    builder.add_edge("complete", END)
    graph = builder.compile(checkpointer=runtime.checkpointer)
    thread_id = f"tenant:{tenant_scope.tenant_id}:integration:{uuid4()}"
    config = {"configurable": {"thread_id": thread_id}}
    try:
        first = await graph.ainvoke({"action_id": "persisted-action"}, config)
        assert "__interrupt__" in first
        checkpoint = await runtime.checkpointer.aget_tuple(config)
        assert checkpoint is not None
        assert checkpoint.pending_writes is not None
        assert any(channel == "__interrupt__" for _task_id, channel, _value in checkpoint.pending_writes)
        snapshot = await graph.aget_state(config)
        assert snapshot.next == ("approval",)
        assert snapshot.tasks and snapshot.tasks[0].interrupts

        resumed = await graph.ainvoke(Command(resume={"approved": True}), config)
        assert resumed["decision"] == {"approved": True}
        assert resumed["completed"] is True
        assert approval_calls == 2
        assert completions == ["persisted-action"]
    finally:
        await runtime.checkpointer.adelete_thread(thread_id)

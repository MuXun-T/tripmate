import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, TypedDict
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio.session import AsyncSessionTransaction

from app.agent.worker import RunWorker
from app.domain.errors import DomainError
from app.domain.ports import SystemClock
from app.evaluation.replay import EvaluationReplayer
from app.events.runtime import InboxProcessor, OutboxPublisher
from app.events.schema import EventEnvelope
from app.memory.short_term import MemoryPatch
from app.persistence.postgres.database import affected_rows
from app.reservation.service import ReservationService
from app.runtime import Runtime
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
    scope: IntegrationScope, *, idempotency_key: str | None = None
) -> tuple[Any, Any, Any]:
    runtime = scope.runtime
    action_id, action_confirmation = await _approved_candidate(scope)
    key = idempotency_key or f"hold-{uuid4()}"
    hold = await runtime.reservations.hold(
        scope.user_context(),
        trip_id=scope.trip_id,
        slot_id=scope.slot_id,
        quantity=1,
        action_id=action_id,
        action_confirmation_token=action_confirmation["confirmation_token"],
        expected_inventory_version=None,
        expected_activity_version=1,
        idempotency_key=key,
    )
    return action_id, action_confirmation, hold


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
    assert inbox["status"] == "PROCESSED"
    assert inbox["attempt_count"] == 2
    assert inbox["fencing_version"] == 2


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

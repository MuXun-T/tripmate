import base64
import hmac
import json
import logging
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.context import RequestContext
from app.domain.errors import DomainError
from app.domain.idempotency import IdempotencyScope, canonical_json, request_hash, sha256_hex
from app.domain.models import CancellationPolicySnapshot, ReservationStatus
from app.domain.ports import Clock, IdGenerator
from app.events.schema import EventEnvelope
from app.persistence.postgres.database import affected_rows
from app.reservation.policy import CancellationPolicyEvaluator

logger = logging.getLogger(__name__)


class ProjectionPort(Protocol):
    async def project(
        self,
        *,
        tenant_id: UUID,
        slot_id: UUID,
        reservation_id: UUID,
        snapshot: dict[str, Any],
        hold: dict[str, Any] | None,
    ) -> str | None: ...


@dataclass(frozen=True, slots=True)
class ReservationResult:
    reservation_id: UUID
    tenant_id: UUID
    trip_id: UUID
    user_id: UUID
    slot_id: UUID
    status: str
    quantity: int
    price_snapshot: Decimal
    currency: str
    version: int
    inventory_version: int
    hold_expires_at: datetime | None = None
    hold_token: str | None = None
    confirmation_token: str | None = None
    projection_pending: bool = False
    replayed: bool = False

    def stable_payload(self) -> dict[str, Any]:
        return {
            "reservation_id": str(self.reservation_id),
            "tenant_id": str(self.tenant_id),
            "trip_id": str(self.trip_id),
            "user_id": str(self.user_id),
            "slot_id": str(self.slot_id),
            "status": self.status,
            "quantity": self.quantity,
            "price_snapshot": str(self.price_snapshot),
            "currency": self.currency.strip(),
            "version": self.version,
            "inventory_version": self.inventory_version,
            "hold_expires_at": self.hold_expires_at.isoformat() if self.hold_expires_at else None,
        }

    @classmethod
    def from_payload(cls, value: dict[str, Any], *, replayed: bool = False) -> "ReservationResult":
        expiry = value.get("hold_expires_at")
        return cls(
            reservation_id=UUID(value["reservation_id"]),
            tenant_id=UUID(value["tenant_id"]),
            trip_id=UUID(value["trip_id"]),
            user_id=UUID(value["user_id"]),
            slot_id=UUID(value["slot_id"]),
            status=value["status"],
            quantity=int(value["quantity"]),
            price_snapshot=Decimal(value["price_snapshot"]),
            currency=value["currency"].strip(),
            version=int(value["version"]),
            inventory_version=int(value["inventory_version"]),
            hold_expires_at=datetime.fromisoformat(expiry) if expiry else None,
            replayed=replayed,
        )


class ReservationService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        clock: Clock,
        ids: IdGenerator,
        projection: ProjectionPort | None = None,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.clock = clock
        self.ids = ids
        self.projection = projection
        self.cancellation = CancellationPolicyEvaluator()

    async def hold(
        self,
        ctx: RequestContext,
        *,
        trip_id: UUID,
        slot_id: UUID,
        quantity: int,
        action_id: UUID,
        action_confirmation_token: str,
        expected_inventory_version: int | None,
        expected_activity_version: int | None,
        idempotency_key: str,
    ) -> ReservationResult:
        self._require_user(ctx)
        assert ctx.user_id is not None
        if not 1 <= quantity <= 100:
            raise DomainError("INVALID_ARGUMENT", "quantity must be between 1 and 100", 400)
        route = "/v1/reservations"
        operation = "hold"
        scope = IdempotencyScope(ctx.tenant_id, ctx.user_id, route, operation, idempotency_key)
        key_hash = scope.key_hash()
        req_hash = request_hash(
            {
                "trip_id": trip_id,
                "slot_id": slot_id,
                "quantity": quantity,
                "action_id": action_id,
                "expected_inventory_version": expected_inventory_version,
                "expected_activity_version": expected_activity_version,
            }
        )
        result: ReservationResult
        snapshot: dict[str, Any] | None = None
        reservation_id = self.ids.new()
        hold_token = secrets.token_urlsafe(32)
        confirmation_token = ""
        hold_expires_at = self.clock.now() + timedelta(seconds=self.settings.hold_ttl_seconds)
        now = self.clock.now()
        unknown_commit = False

        async with self.sessions() as session:
            tx = await session.begin()
            try:
                await self._lock_idempotency_scope(session, key_hash)
                old = await self._get_idempotency(session, ctx, route, operation, key_hash)
                if old is not None:
                    result = self._replay(old, req_hash)
                    await tx.rollback()
                    return result
                stock = await self._lock_inventory(session, ctx.tenant_id, slot_id)
                old = await self._get_idempotency(session, ctx, route, operation, key_hash)
                if old is not None:
                    result = self._replay(old, req_hash)
                    await tx.rollback()
                    return result
                self._check_expected_versions(
                    stock, expected_inventory_version, expected_activity_version
                )
                if stock["slot_status"] != "ACTIVE" or stock["inventory_status"] != "ACTIVE":
                    raise DomainError(
                        "INVENTORY_NOT_ENOUGH", "Activity is not currently sellable", 409
                    )
                if int(stock["available_quantity"]) < quantity:
                    raise DomainError(
                        "INVENTORY_NOT_ENOUGH",
                        "Requested quantity is not available",
                        409,
                        details={"available_quantity": int(stock["available_quantity"])},
                    )
                action = await self._load_approved_action(
                    session,
                    ctx,
                    action_id,
                    action_confirmation_token,
                    now,
                    expected_kind="propose_reservation",
                )
                parameters = self._canonical_action_parameters(
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    trip_id=trip_id,
                    slot_id=slot_id,
                    quantity=quantity,
                    price=Decimal(stock["price"]),
                    currency=str(stock["currency"]).strip(),
                    activity_version=int(stock["activity_version"]),
                    inventory_version=int(stock["inventory_version"]),
                    policy_version=str(stock["policy_version"]),
                    reason=str(action["parameters"].get("reason", "")),
                )
                if action["parameters_hash"] != sha256_hex(canonical_json(parameters)):
                    raise DomainError(
                        "DUPLICATE_ACTION", "Action parameters no longer match current facts", 409
                    )
                if not self._action_matches(action["parameters"], trip_id, slot_id, quantity):
                    raise DomainError(
                        "INVALID_ARGUMENT", "Action does not authorize this reservation", 400
                    )
                if int(stock["available_quantity"]) < quantity:
                    raise DomainError(
                        "INVENTORY_NOT_ENOUGH", "Requested quantity is not available", 409
                    )
                new_available = int(stock["available_quantity"]) - quantity
                new_held = int(stock["held_quantity"]) + quantity
                inv_version = int(stock["inventory_version"]) + 1
                updated = await session.execute(
                    text("""UPDATE tripmate.inventory
                        SET available_quantity=:available, held_quantity=:held, version=:new_version,
                            updated_at=:now
                        WHERE tenant_id=:tenant AND slot_id=:slot AND version=:old_version
                          AND status='ACTIVE'"""),
                    {
                        "available": new_available,
                        "held": new_held,
                        "new_version": inv_version,
                        "now": now,
                        "tenant": ctx.tenant_id,
                        "slot": slot_id,
                        "old_version": stock["inventory_version"],
                    },
                )
                if affected_rows(updated) != 1:
                    raise DomainError("VERSION_CONFLICT", "Inventory changed while holding", 409)
                policy = stock["cancellation_policy"]
                if isinstance(policy, str):
                    policy = json.loads(policy)
                canonical_hash = sha256_hex(canonical_json(parameters))
                hold_claims = {
                    "purpose": "tripmate-hold-token",
                    "tenant_id": str(ctx.tenant_id),
                    "user_id": str(ctx.user_id),
                    "reservation_id": str(reservation_id),
                    "action_id": str(action_id),
                    "parameters_hash": canonical_hash,
                    "expires_at": hold_expires_at.isoformat(),
                }
                hold_token = self._sign_confirmation_claims(hold_claims)
                confirmation_id = self.ids.new()
                confirmation_expires_at = min(
                    hold_expires_at,
                    now + timedelta(seconds=self.settings.action_confirmation_ttl_seconds),
                )
                confirmation_hash = sha256_hex(
                    canonical_json(
                        {
                            "tenant_id": str(ctx.tenant_id),
                            "user_id": str(ctx.user_id),
                            "action_id": str(action_id),
                            "reservation_id": str(reservation_id),
                            "parameters_hash": canonical_hash,
                            "reservation_version": 1,
                        }
                    )
                )
                confirmation_claims = self._confirmation_claims(
                    ctx,
                    action_id,
                    confirmation_id,
                    confirmation_hash,
                    1,
                    confirmation_expires_at,
                    reservation_id=reservation_id,
                    kind="reservation",
                )
                confirmation_token = self._sign_confirmation_claims(confirmation_claims)
                confirmation_changed = await session.execute(
                    text("""UPDATE tripmate.action_confirmation
                        SET status='CONSUMED', consumed_at=:now, version=version+1
                        WHERE tenant_id=:tenant AND action_id=:action AND user_id=:user
                          AND status='ISSUED' AND token_hash=:token_hash
                          AND parameters_hash=:parameters_hash AND expires_at>:now"""),
                    {
                        "now": now,
                        "tenant": ctx.tenant_id,
                        "action": action_id,
                        "user": ctx.user_id,
                        "token_hash": sha256_hex(action_confirmation_token),
                        "parameters_hash": canonical_hash,
                    },
                )
                if affected_rows(confirmation_changed) != 1:
                    raise DomainError(
                        "DUPLICATE_ACTION", "Action confirmation is no longer valid", 409
                    )
                action_changed = await session.execute(
                    text("""UPDATE tripmate.agent_action SET status='CONFIRMED', version=version+1,
                        updated_at=:now WHERE tenant_id=:tenant AND action_id=:action AND status='PROPOSED'"""),
                    {"now": now, "tenant": ctx.tenant_id, "action": action_id},
                )
                if affected_rows(action_changed) != 1:
                    raise DomainError("DUPLICATE_ACTION", "Action was already approved", 409)
                run_changed = await session.execute(
                    text("""UPDATE tripmate.agent_run SET status='RUNNING', awaiting_action_id=NULL,
                        resume_command=CAST(:resume AS jsonb),lease_owner=NULL,lease_until=NULL,
                        fencing_version=fencing_version+1,version=version+1,updated_at=:now
                        WHERE tenant_id=:tenant AND run_id=:run
                        AND user_id=:user AND status='WAITING_CONFIRMATION' RETURNING version"""),
                    {
                        "resume": json.dumps(
                            {
                                "decision": "hold_created",
                                "action_id": str(action_id),
                                "reservation_id": str(reservation_id),
                            }
                        ),
                        "now": now,
                        "tenant": ctx.tenant_id,
                        "run": action["run_id"],
                        "user": ctx.user_id,
                    },
                )
                run_version = run_changed.scalar_one_or_none()
                if run_version is None:
                    raise DomainError("VERSION_CONFLICT", "Run approval state changed", 409)
                await self._insert_outbox(
                    session,
                    replace(ctx, run_id=action["run_id"], action_id=action_id),
                    aggregate_type="agent_run",
                    aggregate_id=action["run_id"],
                    aggregate_version=int(run_version),
                    event_type="agent.run.resume_requested.v1",
                    payload={
                        "run_id": str(action["run_id"]),
                        "action_id": str(action_id),
                        "decision": "hold_created",
                    },
                )
                await session.execute(
                    text("""INSERT INTO tripmate.reservation
                        (reservation_id,tenant_id,trip_id,user_id,slot_id,quantity,price_snapshot,currency,
                         status,hold_token_hash,hold_expires_at,route,operation,idempotency_key_hash,
                         request_hash,parameters_hash,approved_action_id,activity_version,inventory_version,
                         policy_version,policy_snapshot)
                        VALUES(:id,:tenant,:trip,:user,:slot,:quantity,:price,:currency,'HELD',:hold_hash,
                         :expires,:route,:operation,:key_hash,:request_hash,:parameters_hash,:action_id,
                         :activity_version,:inventory_version,:policy_version,CAST(:policy AS jsonb))"""),
                    {
                        "id": reservation_id,
                        "tenant": ctx.tenant_id,
                        "trip": trip_id,
                        "user": ctx.user_id,
                        "slot": slot_id,
                        "quantity": quantity,
                        "price": stock["price"],
                        "currency": str(stock["currency"]).strip(),
                        "hold_hash": sha256_hex(hold_token),
                        "expires": hold_expires_at,
                        "route": route,
                        "operation": operation,
                        "key_hash": key_hash,
                        "request_hash": req_hash,
                        "parameters_hash": canonical_hash,
                        "action_id": action_id,
                        "activity_version": stock["activity_version"],
                        "inventory_version": inv_version,
                        "policy_version": stock["policy_version"],
                        "policy": json.dumps(policy),
                    },
                )
                await session.execute(
                    text("""INSERT INTO tripmate.reservation_confirmation
                        (confirmation_id,tenant_id,reservation_id,action_id,user_id,token_hash,parameters_hash,
                         issued_at,expires_at,status)
                        VALUES(:confirmation,:tenant,:reservation,:action,:user,:token_hash,:parameters_hash,
                         :issued,:expires,'ISSUED')"""),
                    {
                        "confirmation": confirmation_id,
                        "tenant": ctx.tenant_id,
                        "reservation": reservation_id,
                        "action": action_id,
                        "user": ctx.user_id,
                        "token_hash": sha256_hex(confirmation_token),
                        "parameters_hash": confirmation_hash,
                        "issued": now,
                        "expires": confirmation_expires_at,
                    },
                )
                await self._link_trip_item(
                    session, ctx.tenant_id, trip_id, slot_id, stock, reservation_id, now
                )
                result = ReservationResult(
                    reservation_id=reservation_id,
                    tenant_id=ctx.tenant_id,
                    trip_id=trip_id,
                    user_id=ctx.user_id,
                    slot_id=slot_id,
                    status=ReservationStatus.HELD.value,
                    quantity=quantity,
                    price_snapshot=Decimal(stock["price"]),
                    currency=str(stock["currency"]).strip(),
                    version=1,
                    inventory_version=inv_version,
                    hold_expires_at=hold_expires_at,
                    hold_token=hold_token,
                    confirmation_token=confirmation_token,
                )
                await self._insert_outbox(
                    session,
                    ctx,
                    aggregate_type="reservation",
                    aggregate_id=reservation_id,
                    aggregate_version=1,
                    event_type="reservation.held.v1",
                    payload={
                        "reservation_id": str(reservation_id),
                        "slot_id": str(slot_id),
                        "inventory_version": inv_version,
                        "quantity": quantity,
                        "hold_expires_at": hold_expires_at.isoformat(),
                    },
                )
                await self._insert_audit(
                    session,
                    ctx,
                    action="reservation.held",
                    result_code="HELD",
                    action_id=action_id,
                    reservation_id=reservation_id,
                    parameters_hash=canonical_hash,
                )
                await self._insert_idempotency(
                    session,
                    ctx,
                    route,
                    operation,
                    key_hash,
                    req_hash,
                    201,
                    {
                        **result.stable_payload(),
                        "replay_tokens": {
                            "hold": hold_claims,
                            "confirmation": confirmation_claims,
                        },
                    },
                    "reservation",
                    reservation_id,
                )
                snapshot = self._inventory_snapshot(
                    stock, new_available, new_held, int(stock["confirmed_quantity"]), inv_version
                )
                try:
                    await tx.commit()
                except DBAPIError:
                    unknown_commit = True
            except Exception:
                if tx.is_active:
                    await tx.rollback()
                raise
        if unknown_commit:
            recovered = await self._recover_idempotency(ctx, route, operation, key_hash, req_hash)
            if recovered is None:
                raise DomainError(
                    "RECONCILIATION_REQUIRED",
                    "Database commit outcome is unknown; retry with the same key",
                    503,
                    True,
                    {"operation": operation, "key_hash": key_hash},
                )
            result = recovered
            result = replace(result, replayed=True)
        await self._project_after_commit(
            ctx.tenant_id,
            slot_id,
            result.reservation_id,
            snapshot,
            {
                "reservation_id": str(result.reservation_id),
                "token_hash": sha256_hex(result.hold_token) if result.hold_token else "",
                "expires_at": result.hold_expires_at.timestamp() if result.hold_expires_at else 0,
            }
            if result.status == ReservationStatus.HELD.value
            else None,
        )
        return result

    async def confirm(
        self,
        ctx: RequestContext,
        *,
        reservation_id: UUID,
        action_id: UUID,
        confirmation_token: str,
        expected_version: int,
        idempotency_key: str,
    ) -> ReservationResult:
        self._require_user(ctx)
        assert ctx.user_id is not None
        route = f"/v1/reservations/{reservation_id}/confirm"
        operation = "confirm"
        scope = IdempotencyScope(ctx.tenant_id, ctx.user_id, route, operation, idempotency_key)
        key_hash = scope.key_hash()
        req_hash = request_hash(
            {
                "reservation_id": reservation_id,
                "action_id": action_id,
                "expected_version": expected_version,
            }
        )
        result: ReservationResult
        snapshot: dict[str, Any] | None = None
        now = self.clock.now()
        unknown_commit = False
        async with self.sessions() as session:
            tx = await session.begin()
            try:
                await self._lock_idempotency_scope(session, key_hash)
                old = await self._get_idempotency(session, ctx, route, operation, key_hash)
                if old is not None:
                    result = self._replay(old, req_hash)
                    await tx.rollback()
                    return result
                ref = await session.execute(
                    text(
                        "SELECT slot_id FROM tripmate.reservation WHERE tenant_id=:tenant AND reservation_id=:id AND user_id=:user"
                    ),
                    {"tenant": ctx.tenant_id, "id": reservation_id, "user": ctx.user_id},
                )
                slot_id = ref.scalar_one_or_none()
                if slot_id is None:
                    raise DomainError("NOT_FOUND", "Reservation was not found", 404)
                stock = await self._lock_inventory(session, ctx.tenant_id, slot_id)
                old = await self._get_idempotency(session, ctx, route, operation, key_hash)
                if old is not None:
                    result = self._replay(old, req_hash)
                    await tx.rollback()
                    return result
                reservation = await self._lock_reservation(session, ctx, reservation_id)
                if int(reservation["version"]) != expected_version:
                    raise DomainError("VERSION_CONFLICT", "Reservation version is stale", 409)
                if reservation["status"] != "HELD":
                    raise DomainError("RESERVATION_STATE_CONFLICT", "Reservation is not held", 409)
                if reservation["hold_expires_at"] <= now:
                    raise DomainError("HOLD_EXPIRED", "Reservation hold has expired", 409)
                confirmation = await self._consume_reservation_confirmation(
                    session, ctx, reservation, action_id, confirmation_token, now
                )
                self._assert_inventory_consistent(stock, reservation)
                new_held = int(stock["held_quantity"]) - int(reservation["quantity"])
                new_confirmed = int(stock["confirmed_quantity"]) + int(reservation["quantity"])
                inv_version = int(stock["inventory_version"]) + 1
                updated = await session.execute(
                    text("""UPDATE tripmate.inventory SET held_quantity=:held, confirmed_quantity=:confirmed,
                        version=:new_version, updated_at=:now WHERE tenant_id=:tenant AND slot_id=:slot
                        AND version=:old_version AND status='ACTIVE'"""),
                    {
                        "held": new_held,
                        "confirmed": new_confirmed,
                        "new_version": inv_version,
                        "now": now,
                        "tenant": ctx.tenant_id,
                        "slot": slot_id,
                        "old_version": stock["inventory_version"],
                    },
                )
                if affected_rows(updated) != 1:
                    raise DomainError("VERSION_CONFLICT", "Inventory changed while confirming", 409)
                res_update = await session.execute(
                    text("""UPDATE tripmate.reservation SET status='CONFIRMED', version=version+1,
                        inventory_version=:inventory_version, updated_at=:now
                        WHERE tenant_id=:tenant AND reservation_id=:id AND user_id=:user
                          AND status='HELD' AND version=:expected AND hold_expires_at>:now"""),
                    {
                        "inventory_version": inv_version,
                        "now": now,
                        "tenant": ctx.tenant_id,
                        "id": reservation_id,
                        "user": ctx.user_id,
                        "expected": expected_version,
                    },
                )
                if affected_rows(res_update) != 1:
                    raise DomainError(
                        "RESERVATION_STATE_CONFLICT", "Confirm lost the reservation CAS", 409
                    )
                await session.execute(
                    text("""UPDATE tripmate.reservation_confirmation SET status='CONSUMED',consumed_at=:now,
                        version=version+1 WHERE tenant_id=:tenant AND confirmation_id=:id AND status='ISSUED'"""),
                    {"now": now, "tenant": ctx.tenant_id, "id": confirmation["confirmation_id"]},
                )
                action_update = await session.execute(
                    text("""UPDATE tripmate.agent_action SET status='EXECUTED',
                        result_json=CAST(:result AS jsonb),version=version+1,updated_at=:now
                        WHERE tenant_id=:tenant AND action_id=:action AND status='CONFIRMED'"""),
                    {
                        "result": json.dumps(
                            {"reservation_id": str(reservation_id), "status": "CONFIRMED"}
                        ),
                        "now": now,
                        "tenant": ctx.tenant_id,
                        "action": action_id,
                    },
                )
                if affected_rows(action_update) != 1:
                    raise DomainError(
                        "DUPLICATE_ACTION", "Reservation action was already executed", 409
                    )
                await session.execute(
                    text("""UPDATE tripmate.trip_item SET reservation_id=:reservation,
                        constraint_status='PASS',version=version+1,updated_at=:now
                        WHERE tenant_id=:tenant AND trip_id=:trip AND slot_id=:slot"""),
                    {
                        "reservation": reservation_id,
                        "now": now,
                        "tenant": ctx.tenant_id,
                        "trip": reservation["trip_id"],
                        "slot": slot_id,
                    },
                )
                result = ReservationResult(
                    reservation_id=reservation_id,
                    tenant_id=ctx.tenant_id,
                    trip_id=reservation["trip_id"],
                    user_id=ctx.user_id,
                    slot_id=slot_id,
                    status="CONFIRMED",
                    quantity=int(reservation["quantity"]),
                    price_snapshot=Decimal(reservation["price_snapshot"]),
                    currency=str(reservation["currency"]).strip(),
                    version=expected_version + 1,
                    inventory_version=inv_version,
                )
                await self._insert_outbox(
                    session,
                    ctx,
                    aggregate_type="reservation",
                    aggregate_id=reservation_id,
                    aggregate_version=result.version,
                    event_type="reservation.confirmed.v1",
                    payload={
                        "reservation_id": str(reservation_id),
                        "slot_id": str(slot_id),
                        "inventory_version": inv_version,
                        "quantity": result.quantity,
                    },
                )
                await self._insert_audit(
                    session,
                    ctx,
                    action="reservation.confirmed",
                    result_code="CONFIRMED",
                    action_id=action_id,
                    reservation_id=reservation_id,
                    parameters_hash=reservation["parameters_hash"],
                )
                await self._insert_idempotency(
                    session,
                    ctx,
                    route,
                    operation,
                    key_hash,
                    req_hash,
                    200,
                    result.stable_payload(),
                    "reservation",
                    reservation_id,
                )
                snapshot = self._inventory_snapshot(
                    stock, int(stock["available_quantity"]), new_held, new_confirmed, inv_version
                )
                try:
                    await tx.commit()
                except DBAPIError:
                    unknown_commit = True
            except Exception:
                if tx.is_active:
                    await tx.rollback()
                raise
        if unknown_commit:
            recovered = await self._recover_idempotency(ctx, route, operation, key_hash, req_hash)
            if recovered is None:
                raise DomainError(
                    "RECONCILIATION_REQUIRED", "Confirm commit outcome is unknown", 503, True
                )
            result = recovered
        await self._project_after_commit(
            ctx.tenant_id, result.slot_id, reservation_id, snapshot, None
        )
        return result

    async def cancel(
        self,
        ctx: RequestContext,
        *,
        reservation_id: UUID,
        action_id: UUID,
        action_confirmation_token: str,
        expected_version: int,
        reason: str,
        idempotency_key: str,
    ) -> tuple[ReservationResult, Decimal]:
        self._require_user(ctx)
        assert ctx.user_id is not None
        route = f"/v1/reservations/{reservation_id}/cancel"
        operation = "cancel"
        scope = IdempotencyScope(ctx.tenant_id, ctx.user_id, route, operation, idempotency_key)
        key_hash = scope.key_hash()
        req_hash = request_hash(
            {
                "reservation_id": reservation_id,
                "action_id": action_id,
                "expected_version": expected_version,
                "reason": reason,
            }
        )
        result: ReservationResult
        refund = Decimal("0")
        snapshot: dict[str, Any] | None = None
        now = self.clock.now()
        unknown_commit = False
        async with self.sessions() as session:
            tx = await session.begin()
            try:
                await self._lock_idempotency_scope(session, key_hash)
                old = await self._get_idempotency(session, ctx, route, operation, key_hash)
                if old is not None:
                    result = self._replay(old, req_hash)
                    refund = Decimal(old["result_json"].get("refund_amount", "0"))
                    await tx.rollback()
                    return result, refund
                ref = await session.execute(
                    text(
                        "SELECT slot_id FROM tripmate.reservation WHERE tenant_id=:tenant AND reservation_id=:id AND user_id=:user"
                    ),
                    {"tenant": ctx.tenant_id, "id": reservation_id, "user": ctx.user_id},
                )
                slot_id = ref.scalar_one_or_none()
                if slot_id is None:
                    raise DomainError("NOT_FOUND", "Reservation was not found", 404)
                stock = await self._lock_inventory(session, ctx.tenant_id, slot_id)
                old = await self._get_idempotency(session, ctx, route, operation, key_hash)
                if old is not None:
                    result = self._replay(old, req_hash)
                    refund = Decimal(old["result_json"].get("refund_amount", "0"))
                    await tx.rollback()
                    return result, refund
                reservation = await self._lock_reservation(session, ctx, reservation_id)
                if int(reservation["version"]) != expected_version:
                    raise DomainError("VERSION_CONFLICT", "Reservation version is stale", 409)
                if reservation["status"] not in {"HELD", "CONFIRMED"}:
                    raise DomainError(
                        "RESERVATION_STATE_CONFLICT", "Reservation cannot be cancelled", 409
                    )
                action = await self._load_approved_action(
                    session,
                    ctx,
                    action_id,
                    action_confirmation_token,
                    now,
                    expected_kind="propose_cancellation",
                )
                if not self._action_matches(action["parameters"], reservation_id, None, None):
                    raise DomainError(
                        "INVALID_ARGUMENT", "Action does not authorize this cancellation", 400
                    )
                cancel_parameters_hash = sha256_hex(
                    canonical_json(
                        {
                            "tenant_id": str(ctx.tenant_id),
                            "user_id": str(ctx.user_id),
                            "reservation_id": str(reservation_id),
                            "reason": str(action["parameters"].get("reason", "")),
                        }
                    )
                )
                if action["parameters_hash"] != cancel_parameters_hash:
                    raise DomainError(
                        "DUPLICATE_ACTION",
                        "Cancellation parameters do not match the approved action",
                        409,
                    )
                policy_data = reservation["policy_snapshot"]
                if isinstance(policy_data, str):
                    policy_data = json.loads(policy_data)
                decision = self.cancellation.evaluate(
                    CancellationPolicySnapshot.model_validate(policy_data), now
                )
                if reservation["status"] == "CONFIRMED" and not decision.allowed:
                    raise DomainError(
                        "CANCELLATION_NOT_ALLOWED",
                        "Cancellation policy does not allow a refund",
                        409,
                        details={
                            "reason_code": decision.reason_code,
                            "policy_version": decision.policy_version,
                        },
                    )
                self._assert_inventory_consistent(stock, reservation)
                qty = int(reservation["quantity"])
                if reservation["status"] == "HELD":
                    available = int(stock["available_quantity"]) + qty
                    held = int(stock["held_quantity"]) - qty
                    confirmed = int(stock["confirmed_quantity"])
                else:
                    available = int(stock["available_quantity"]) + qty
                    held = int(stock["held_quantity"])
                    confirmed = int(stock["confirmed_quantity"]) - qty
                    refund = (
                        Decimal(reservation["price_snapshot"])
                        * qty
                        * decision.refund_percent
                        / Decimal(100)
                    ).quantize(Decimal("0.01"))
                inv_version = int(stock["inventory_version"]) + 1
                updated = await session.execute(
                    text("""UPDATE tripmate.inventory SET available_quantity=:available,held_quantity=:held,
                        confirmed_quantity=:confirmed,version=:version,updated_at=:now
                        WHERE tenant_id=:tenant AND slot_id=:slot AND version=:old_version"""),
                    {
                        "available": available,
                        "held": held,
                        "confirmed": confirmed,
                        "version": inv_version,
                        "now": now,
                        "tenant": ctx.tenant_id,
                        "slot": slot_id,
                        "old_version": stock["inventory_version"],
                    },
                )
                if affected_rows(updated) != 1:
                    raise DomainError("VERSION_CONFLICT", "Inventory changed while cancelling", 409)
                res_update = await session.execute(
                    text("""UPDATE tripmate.reservation SET status='CANCELLED',version=version+1,
                        inventory_version=:inventory_version,updated_at=:now
                        WHERE tenant_id=:tenant AND reservation_id=:id AND user_id=:user
                        AND status=:old_status AND version=:expected"""),
                    {
                        "inventory_version": inv_version,
                        "now": now,
                        "tenant": ctx.tenant_id,
                        "id": reservation_id,
                        "user": ctx.user_id,
                        "old_status": reservation["status"],
                        "expected": expected_version,
                    },
                )
                if affected_rows(res_update) != 1:
                    raise DomainError(
                        "RESERVATION_STATE_CONFLICT", "Cancel lost the reservation CAS", 409
                    )
                await self._consume_action_confirmation(
                    session, ctx, action_id, action_confirmation_token, now
                )
                action_update = await session.execute(
                    text("""UPDATE tripmate.agent_action SET status='EXECUTED',
                        result_json=CAST(:result AS jsonb),version=version+1,updated_at=:now
                        WHERE tenant_id=:tenant AND action_id=:action AND status='PROPOSED'"""),
                    {
                        "result": json.dumps(
                            {"reservation_id": str(reservation_id), "status": "CANCELLED"}
                        ),
                        "now": now,
                        "tenant": ctx.tenant_id,
                        "action": action_id,
                    },
                )
                if affected_rows(action_update) != 1:
                    raise DomainError(
                        "DUPLICATE_ACTION", "Cancellation action was already executed", 409
                    )
                run_changed = await session.execute(
                    text("""UPDATE tripmate.agent_run SET status='RUNNING',awaiting_action_id=NULL,
                        resume_command=CAST(:resume AS jsonb),lease_owner=NULL,lease_until=NULL,
                        fencing_version=fencing_version+1,version=version+1,updated_at=:now
                        WHERE tenant_id=:tenant AND run_id=:run
                        AND user_id=:user AND status='WAITING_CONFIRMATION' RETURNING version"""),
                    {
                        "resume": json.dumps(
                            {
                                "decision": "action_executed",
                                "action_id": str(action_id),
                                "reservation_id": str(reservation_id),
                            }
                        ),
                        "now": now,
                        "tenant": ctx.tenant_id,
                        "run": action["run_id"],
                        "user": ctx.user_id,
                    },
                )
                run_version = run_changed.scalar_one_or_none()
                if run_version is None:
                    raise DomainError("VERSION_CONFLICT", "Run approval state changed", 409)
                await self._insert_outbox(
                    session,
                    replace(ctx, run_id=action["run_id"], action_id=action_id),
                    aggregate_type="agent_run",
                    aggregate_id=action["run_id"],
                    aggregate_version=int(run_version),
                    event_type="agent.run.resume_requested.v1",
                    payload={
                        "run_id": str(action["run_id"]),
                        "action_id": str(action_id),
                        "decision": "action_executed",
                    },
                )
                result = ReservationResult(
                    reservation_id=reservation_id,
                    tenant_id=ctx.tenant_id,
                    trip_id=reservation["trip_id"],
                    user_id=ctx.user_id,
                    slot_id=slot_id,
                    status="CANCELLED",
                    quantity=qty,
                    price_snapshot=Decimal(reservation["price_snapshot"]),
                    currency=str(reservation["currency"]).strip(),
                    version=expected_version + 1,
                    inventory_version=inv_version,
                )
                stored = result.stable_payload()
                stored["refund_amount"] = str(refund)
                await self._insert_outbox(
                    session,
                    ctx,
                    aggregate_type="reservation",
                    aggregate_id=reservation_id,
                    aggregate_version=result.version,
                    event_type="reservation.cancelled.v1",
                    payload={
                        "reservation_id": str(reservation_id),
                        "slot_id": str(slot_id),
                        "inventory_version": inv_version,
                        "quantity": qty,
                        "reason_code": decision.reason_code,
                        "refund_amount": str(refund),
                    },
                )
                await self._insert_audit(
                    session,
                    ctx,
                    action="reservation.cancelled",
                    result_code="CANCELLED",
                    action_id=action_id,
                    reservation_id=reservation_id,
                    parameters_hash=reservation["parameters_hash"],
                )
                await self._insert_idempotency(
                    session,
                    ctx,
                    route,
                    operation,
                    key_hash,
                    req_hash,
                    200,
                    stored,
                    "reservation",
                    reservation_id,
                )
                snapshot = self._inventory_snapshot(stock, available, held, confirmed, inv_version)
                try:
                    await tx.commit()
                except DBAPIError:
                    unknown_commit = True
            except Exception:
                if tx.is_active:
                    await tx.rollback()
                raise
        if unknown_commit:
            recovered = await self._recover_idempotency(ctx, route, operation, key_hash, req_hash)
            if recovered is None:
                raise DomainError(
                    "RECONCILIATION_REQUIRED", "Cancel commit outcome is unknown", 503, True
                )
            result = recovered
        await self._project_after_commit(
            ctx.tenant_id, result.slot_id, reservation_id, snapshot, None
        )
        return result, refund

    async def expire(self, tenant_id: UUID, reservation_id: UUID) -> bool:
        """Expire one held reservation; only the database CAS winner projects a release."""
        now = self.clock.now()
        snapshot: dict[str, Any] | None = None
        slot_id: UUID | None = None
        won = False
        async with self.sessions() as session:
            tx = await session.begin()
            try:
                ref = await session.execute(
                    text(
                        "SELECT slot_id FROM tripmate.reservation WHERE tenant_id=:tenant AND reservation_id=:id"
                    ),
                    {"tenant": tenant_id, "id": reservation_id},
                )
                slot_id = ref.scalar_one_or_none()
                if slot_id is None:
                    await tx.rollback()
                    return False
                stock = await self._lock_inventory(session, tenant_id, slot_id)
                reservation = await session.execute(
                    text("""SELECT * FROM tripmate.reservation WHERE tenant_id=:tenant
                        AND reservation_id=:id FOR UPDATE"""),
                    {"tenant": tenant_id, "id": reservation_id},
                )
                row = reservation.mappings().one_or_none()
                if row is None or row["status"] != "HELD" or row["hold_expires_at"] > now:
                    await tx.rollback()
                    return False
                self._assert_inventory_consistent(stock, row)
                qty = int(row["quantity"])
                available = int(stock["available_quantity"]) + qty
                held = int(stock["held_quantity"]) - qty
                version = int(stock["inventory_version"]) + 1
                changed = await session.execute(
                    text("""UPDATE tripmate.inventory SET available_quantity=:available,held_quantity=:held,
                        version=:version,updated_at=:now WHERE tenant_id=:tenant AND slot_id=:slot
                        AND version=:old_version"""),
                    {
                        "available": available,
                        "held": held,
                        "version": version,
                        "now": now,
                        "tenant": tenant_id,
                        "slot": slot_id,
                        "old_version": stock["inventory_version"],
                    },
                )
                if affected_rows(changed) != 1:
                    raise DomainError("VERSION_CONFLICT", "Inventory changed during expiry", 409)
                changed = await session.execute(
                    text("""UPDATE tripmate.reservation SET status='EXPIRED',version=version+1,
                        inventory_version=:inventory_version,updated_at=:now
                        WHERE tenant_id=:tenant AND reservation_id=:id AND status='HELD'
                        AND version=:version AND hold_expires_at<=:now"""),
                    {
                        "inventory_version": version,
                        "now": now,
                        "tenant": tenant_id,
                        "id": reservation_id,
                        "version": row["version"],
                    },
                )
                if affected_rows(changed) != 1:
                    raise DomainError(
                        "RESERVATION_STATE_CONFLICT", "Expiry lost the reservation CAS", 409
                    )
                worker_ctx = RequestContext(
                    request_id=f"expire:{reservation_id}",
                    trace_id=f"expire:{reservation_id}",
                    tenant_id=tenant_id,
                    user_id=row["user_id"],
                    roles=("worker",),
                    deadline=now + timedelta(seconds=30),
                    source="worker",
                    reservation_id=reservation_id,
                )
                await self._insert_outbox(
                    session,
                    worker_ctx,
                    aggregate_type="reservation",
                    aggregate_id=reservation_id,
                    aggregate_version=int(row["version"]) + 1,
                    event_type="reservation.expired.v1",
                    payload={
                        "reservation_id": str(reservation_id),
                        "slot_id": str(slot_id),
                        "inventory_version": version,
                        "quantity": qty,
                    },
                )
                await self._insert_audit(
                    session,
                    worker_ctx,
                    action="reservation.expired",
                    result_code="EXPIRED",
                    action_id=row["approved_action_id"],
                    reservation_id=reservation_id,
                    parameters_hash=row["parameters_hash"],
                )
                snapshot = self._inventory_snapshot(
                    stock, available, held, int(stock["confirmed_quantity"]), version
                )
                try:
                    await tx.commit()
                    won = True
                except DBAPIError:
                    won = False
            except Exception:
                if tx.is_active:
                    await tx.rollback()
                raise
        if not won:
            async with self.sessions() as check:
                status = await check.scalar(
                    text(
                        "SELECT status FROM tripmate.reservation WHERE tenant_id=:tenant AND reservation_id=:id"
                    ),
                    {"tenant": tenant_id, "id": reservation_id},
                )
            if status != "EXPIRED":
                return False
            won = True
        if self.projection is not None and slot_id is not None and snapshot is not None:
            await self._project_after_commit(tenant_id, slot_id, reservation_id, snapshot, None)
        return won

    async def create_candidate_action(
        self,
        ctx: RequestContext,
        *,
        run_id: UUID,
        action_type: str,
        parameters: dict[str, Any],
    ) -> tuple[UUID, str]:
        """Persist/reuse a candidate and return its server-computed parameter hash."""
        self._require_user(ctx)
        assert ctx.user_id is not None
        now = self.clock.now()
        allowed_fields = {
            "trip_id",
            "slot_id",
            "quantity",
            "reason",
            "reservation_id",
            "reference_ids",
        }
        unexpected_fields = set(parameters) - allowed_fields
        if unexpected_fields:
            raise DomainError(
                "INVALID_ARGUMENT",
                "Action contains server-controlled fields",
                400,
                details={"fields": sorted(unexpected_fields)},
            )
        if action_type == "propose_reservation":
            required = {"trip_id", "slot_id", "quantity"}
            if not required.issubset(parameters):
                raise DomainError(
                    "INVALID_ARGUMENT", "Reservation action is missing required fields", 400
                )
            slot = UUID(str(parameters["slot_id"]))
            trip = UUID(str(parameters["trip_id"]))
            qty = int(parameters["quantity"])
            stock = await self._read_inventory(ctx.tenant_id, trip, slot)
            canonical = self._canonical_action_parameters(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                trip_id=trip,
                slot_id=slot,
                quantity=qty,
                price=Decimal(stock["price"]),
                currency=str(stock["currency"]).strip(),
                activity_version=int(stock["activity_version"]),
                inventory_version=int(stock["inventory_version"]),
                policy_version=str(stock["policy_version"]),
                reason=str(parameters.get("reason", "")),
            )
        elif action_type == "propose_cancellation":
            if "reservation_id" not in parameters:
                raise DomainError(
                    "INVALID_ARGUMENT", "Cancellation action requires reservation_id", 400
                )
            canonical = {
                "tenant_id": str(ctx.tenant_id),
                "user_id": str(ctx.user_id),
                "reservation_id": str(parameters["reservation_id"]),
                "reason": str(parameters.get("reason", "")),
            }
        else:
            raise DomainError("TOOL_NOT_ALLOWED", "Action type is not registered", 403)
        parameters_hash = sha256_hex(canonical_json(canonical))
        fingerprint = sha256_hex(
            canonical_json(
                {"run_id": str(run_id), "type": action_type, "parameters_hash": parameters_hash}
            )
        )
        action_id = self.ids.new()
        async with self.sessions() as session, session.begin():
            await self._assert_agent_lease(session, ctx, run_id, now)
            existing = await session.execute(
                text(
                    "SELECT action_id, parameters_hash FROM tripmate.agent_action WHERE tenant_id=:tenant "
                    "AND run_id=:run AND action_fingerprint=:fingerprint"
                ),
                {"tenant": ctx.tenant_id, "run": run_id, "fingerprint": fingerprint},
            )
            row = existing.mappings().one_or_none()
            if row is not None:
                if row["parameters_hash"] != parameters_hash:
                    raise DomainError(
                        "DUPLICATE_ACTION", "Action fingerprint has different parameters", 409
                    )
                return row["action_id"], row["parameters_hash"]
            await session.execute(
                text("""INSERT INTO tripmate.agent_action
                    (action_id,tenant_id,run_id,action_type,parameters,parameters_hash,status,action_fingerprint)
                    VALUES(:id,:tenant,:run,:type,CAST(:parameters AS jsonb),:hash,'PROPOSED',:fingerprint)"""),
                {
                    "id": action_id,
                    "tenant": ctx.tenant_id,
                    "run": run_id,
                    "type": action_type,
                    "parameters": json.dumps(parameters),
                    "hash": parameters_hash,
                    "fingerprint": fingerprint,
                },
            )
            run_updated = await session.execute(
                text("""UPDATE tripmate.agent_run SET status='WAITING_CONFIRMATION',awaiting_action_id=:action,
                    version=version+1,updated_at=:now WHERE tenant_id=:tenant AND run_id=:run AND user_id=:user
                    AND status='RUNNING' AND lease_owner=:owner AND fencing_version=:fence
                    AND lease_until>:now RETURNING version"""),
                {
                    "action": action_id,
                    "now": now,
                    "tenant": ctx.tenant_id,
                    "run": run_id,
                    "user": ctx.user_id,
                    "owner": ctx.lease_owner,
                    "fence": ctx.fencing_version,
                },
            )
            run_version = run_updated.scalar_one_or_none()
            if run_version is None:
                raise DomainError("VERSION_CONFLICT", "Run is not available for approval", 409)
            await self._insert_outbox(
                session,
                replace(ctx, run_id=run_id, action_id=action_id),
                aggregate_type="agent_run",
                aggregate_id=run_id,
                aggregate_version=int(run_version),
                event_type="agent.approval_required.v1",
                payload={
                    "run_id": str(run_id),
                    "action_id": str(action_id),
                    "action_type": action_type,
                    "parameters_hash": parameters_hash,
                },
            )
            await self._insert_audit(
                session,
                ctx,
                action="agent.action.proposed",
                result_code="PROPOSED",
                action_id=action_id,
                parameters_hash=parameters_hash,
            )
        return action_id, parameters_hash

    async def issue_confirmation(
        self,
        ctx: RequestContext,
        action_id: UUID,
        *,
        expected_version: int | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_user(ctx)
        assert ctx.user_id is not None
        route = f"/v1/actions/{action_id}/confirmation"
        operation = "action.confirmation.issue"
        scope = IdempotencyScope(ctx.tenant_id, ctx.user_id, route, operation, idempotency_key)
        key_hash = scope.key_hash()
        req_hash = request_hash(
            {"action_id": action_id, "expected_confirmation_version": expected_version}
        )
        now = self.clock.now()
        default_expiry = now + timedelta(seconds=self.settings.action_confirmation_ttl_seconds)
        response: dict[str, Any] | None = None
        unknown_commit = False

        async with self.sessions() as session:
            tx = await session.begin()
            try:
                await self._lock_idempotency_scope(session, key_hash)
                prior = await self._get_idempotency(session, ctx, route, operation, key_hash)
                if prior is not None:
                    if prior["request_hash"] != req_hash:
                        raise DomainError(
                            "IDEMPOTENCY_KEY_REUSE", "Idempotency key was reused", 409
                        )
                    await tx.rollback()
                    return self._restore_confirmation_response(prior["result_json"])

                action_result = await session.execute(
                    text("""SELECT a.*,r.status AS run_status FROM tripmate.agent_action a
                        JOIN tripmate.agent_run r ON r.tenant_id=a.tenant_id AND r.run_id=a.run_id
                        WHERE a.tenant_id=:tenant AND a.action_id=:action AND r.user_id=:user
                        FOR UPDATE OF a,r"""),
                    {"tenant": ctx.tenant_id, "action": action_id, "user": ctx.user_id},
                )
                action = action_result.mappings().one_or_none()
                if action is None:
                    raise DomainError("NOT_FOUND", "Action was not found", 404)

                reservation_id: UUID | None = None
                if action["status"] == "CONFIRMED":
                    reservation_result = await session.execute(
                        text("""SELECT * FROM tripmate.reservation WHERE tenant_id=:tenant
                            AND approved_action_id=:action AND user_id=:user AND status='HELD'
                            FOR UPDATE"""),
                        {"tenant": ctx.tenant_id, "action": action_id, "user": ctx.user_id},
                    )
                    reservation = reservation_result.mappings().one_or_none()
                    if reservation is None:
                        raise DomainError(
                            "DUPLICATE_ACTION", "Action has no active reservation confirmation", 409
                        )
                    reservation_id = reservation["reservation_id"]
                    current = await session.execute(
                        text("""SELECT * FROM tripmate.reservation_confirmation
                            WHERE tenant_id=:tenant AND reservation_id=:reservation FOR UPDATE"""),
                        {"tenant": ctx.tenant_id, "reservation": reservation_id},
                    )
                    record = current.mappings().one_or_none()
                    if record is None:
                        raise DomainError(
                            "RECONCILIATION_REQUIRED",
                            "Reservation confirmation record is missing",
                            503,
                        )
                    if expected_version is not None and int(record["version"]) != expected_version:
                        raise DomainError("VERSION_CONFLICT", "Confirmation version is stale", 409)
                    if reservation["hold_expires_at"] <= now:
                        raise DomainError("HOLD_EXPIRED", "Reservation hold has expired", 409)
                    expires = min(reservation["hold_expires_at"], default_expiry)
                    parameters_hash = sha256_hex(
                        canonical_json(
                            {
                                "tenant_id": str(ctx.tenant_id),
                                "user_id": str(ctx.user_id),
                                "action_id": str(action_id),
                                "reservation_id": str(reservation_id),
                                "parameters_hash": str(reservation["parameters_hash"]),
                                "reservation_version": int(reservation["version"]),
                            }
                        )
                    )
                    version = int(record["version"]) + 1
                    confirmation_id = record["confirmation_id"]
                    claims = self._confirmation_claims(
                        ctx,
                        action_id,
                        confirmation_id,
                        parameters_hash,
                        version,
                        expires,
                        reservation_id=reservation_id,
                        kind="reservation",
                    )
                    token = self._sign_confirmation_claims(claims)
                    updated = await session.execute(
                        text("""UPDATE tripmate.reservation_confirmation SET token_hash=:hash,
                            parameters_hash=:parameters_hash,issued_at=:issued,expires_at=:expires,
                            status='ISSUED',consumed_at=NULL,version=:version
                            WHERE tenant_id=:tenant AND reservation_id=:reservation
                              AND version=:old_version"""),
                        {
                            "hash": sha256_hex(token),
                            "parameters_hash": parameters_hash,
                            "issued": now,
                            "expires": expires,
                            "version": version,
                            "tenant": ctx.tenant_id,
                            "reservation": reservation_id,
                            "old_version": record["version"],
                        },
                    )
                else:
                    if (
                        action["status"] != "PROPOSED"
                        or action["run_status"] != "WAITING_CONFIRMATION"
                    ):
                        raise DomainError(
                            "DUPLICATE_ACTION", "Action is no longer awaiting approval", 409
                        )
                    current = await session.execute(
                        text("""SELECT * FROM tripmate.action_confirmation
                            WHERE tenant_id=:tenant AND action_id=:action FOR UPDATE"""),
                        {"tenant": ctx.tenant_id, "action": action_id},
                    )
                    record = current.mappings().one_or_none()
                    if record is None:
                        if expected_version not in (None, 1):
                            raise DomainError(
                                "VERSION_CONFLICT", "Confirmation version is stale", 409
                            )
                        confirmation_id = self.ids.new()
                        version = 1
                    else:
                        if (
                            expected_version is not None
                            and int(record["version"]) != expected_version
                        ):
                            raise DomainError(
                                "VERSION_CONFLICT", "Confirmation version is stale", 409
                            )
                        confirmation_id = record["confirmation_id"]
                        version = int(record["version"]) + 1
                    claims = self._confirmation_claims(
                        ctx,
                        action_id,
                        confirmation_id,
                        str(action["parameters_hash"]),
                        version,
                        default_expiry,
                        kind="action",
                    )
                    token = self._sign_confirmation_claims(claims)
                    if record is None:
                        await session.execute(
                            text("""INSERT INTO tripmate.action_confirmation
                                (confirmation_id,tenant_id,action_id,user_id,token_hash,parameters_hash,
                                 issued_at,expires_at,status)
                                VALUES(:id,:tenant,:action,:user,:hash,:parameters_hash,
                                 :issued,:expires,'ISSUED')"""),
                            {
                                "id": confirmation_id,
                                "tenant": ctx.tenant_id,
                                "action": action_id,
                                "user": ctx.user_id,
                                "hash": sha256_hex(token),
                                "parameters_hash": action["parameters_hash"],
                                "issued": now,
                                "expires": default_expiry,
                            },
                        )
                    else:
                        updated = await session.execute(
                            text("""UPDATE tripmate.action_confirmation SET token_hash=:hash,
                                issued_at=:issued,expires_at=:expires,status='ISSUED',consumed_at=NULL,
                                version=:version WHERE tenant_id=:tenant AND action_id=:action
                                  AND version=:old_version"""),
                            {
                                "hash": sha256_hex(token),
                                "issued": now,
                                "expires": default_expiry,
                                "version": version,
                                "tenant": ctx.tenant_id,
                                "action": action_id,
                                "old_version": record["version"],
                            },
                        )
                if record is not None and affected_rows(updated) != 1:
                    raise DomainError("VERSION_CONFLICT", "Confirmation issue lost its CAS", 409)

                expires_at = claims["expires_at"]
                stored = {
                    "action_id": str(action_id),
                    "reservation_id": str(reservation_id) if reservation_id else None,
                    "expires_at": expires_at,
                    "version": version,
                    "token_claims": claims,
                }
                await self._insert_idempotency(
                    session,
                    ctx,
                    route,
                    operation,
                    key_hash,
                    req_hash,
                    200,
                    stored,
                    "agent_action",
                    action_id,
                )
                await self._insert_audit(
                    session,
                    ctx,
                    action="agent.action.confirmation_issued",
                    result_code="ISSUED",
                    action_id=action_id,
                    parameters_hash=str(action["parameters_hash"]),
                )
                response = self._restore_confirmation_response(stored)
                try:
                    await tx.commit()
                except DBAPIError:
                    unknown_commit = True
            except Exception:
                if tx.is_active:
                    await tx.rollback()
                raise
        if unknown_commit:
            async with self.sessions() as session:
                prior = await self._get_idempotency(session, ctx, route, operation, key_hash)
            if prior is None:
                raise DomainError(
                    "RECONCILIATION_REQUIRED",
                    "Confirmation issue outcome is unknown; retry with the same key",
                    503,
                    True,
                )
            if prior["request_hash"] != req_hash:
                raise DomainError("IDEMPOTENCY_KEY_REUSE", "Idempotency key was reused", 409)
            response = self._restore_confirmation_response(prior["result_json"])
        assert response is not None
        return response

    async def _issue_confirmation_legacy(
        self, ctx: RequestContext, action_id: UUID, *, expected_version: int | None = None
    ) -> dict[str, Any]:
        self._require_user(ctx)
        now = self.clock.now()
        token = secrets.token_urlsafe(32)
        expires = now + timedelta(seconds=self.settings.action_confirmation_ttl_seconds)
        async with self.sessions() as session, session.begin():
            action_result = await session.execute(
                text("""SELECT a.*,r.status AS run_status,r.version AS run_version
                    FROM tripmate.agent_action a JOIN tripmate.agent_run r
                      ON r.tenant_id=a.tenant_id AND r.run_id=a.run_id
                    WHERE a.tenant_id=:tenant AND a.action_id=:action AND r.user_id=:user FOR UPDATE OF a,r"""),
                {"tenant": ctx.tenant_id, "action": action_id, "user": ctx.user_id},
            )
            action = action_result.mappings().one_or_none()
            if action is None:
                raise DomainError("NOT_FOUND", "Action was not found", 404)
            if action["status"] == "CONFIRMED":
                reservation_result = await session.execute(
                    text("""SELECT * FROM tripmate.reservation WHERE tenant_id=:tenant
                        AND approved_action_id=:action AND user_id=:user AND status='HELD' FOR UPDATE"""),
                    {"tenant": ctx.tenant_id, "action": action_id, "user": ctx.user_id},
                )
                reservation = reservation_result.mappings().one_or_none()
                if reservation is None:
                    raise DomainError(
                        "DUPLICATE_ACTION", "Action has no active reservation confirmation", 409
                    )
                current_reservation_confirmation = await session.execute(
                    text("""SELECT * FROM tripmate.reservation_confirmation WHERE tenant_id=:tenant
                        AND reservation_id=:reservation FOR UPDATE"""),
                    {"tenant": ctx.tenant_id, "reservation": reservation["reservation_id"]},
                )
                confirmation = current_reservation_confirmation.mappings().one_or_none()
                if confirmation is None:
                    raise DomainError(
                        "RECONCILIATION_REQUIRED", "Reservation confirmation record is missing", 503
                    )
                if (
                    expected_version is not None
                    and int(confirmation["version"]) != expected_version
                ):
                    raise DomainError("VERSION_CONFLICT", "Confirmation version is stale", 409)
                if reservation["hold_expires_at"] <= now:
                    raise DomainError("HOLD_EXPIRED", "Reservation hold has expired", 409)
                parameters_hash = sha256_hex(
                    canonical_json(
                        {
                            "tenant_id": str(ctx.tenant_id),
                            "user_id": str(ctx.user_id),
                            "action_id": str(action_id),
                            "reservation_id": str(reservation["reservation_id"]),
                            "parameters_hash": str(reservation["parameters_hash"]),
                            "reservation_version": int(reservation["version"]),
                        }
                    )
                )
                expires = min(reservation["hold_expires_at"], expires)
                await session.execute(
                    text("""UPDATE tripmate.reservation_confirmation SET token_hash=:hash,
                        parameters_hash=:parameters_hash,issued_at=:issued,expires_at=:expires,
                        status='ISSUED',consumed_at=NULL,version=version+1
                        WHERE tenant_id=:tenant AND reservation_id=:reservation"""),
                    {
                        "hash": sha256_hex(token),
                        "parameters_hash": parameters_hash,
                        "issued": now,
                        "expires": expires,
                        "tenant": ctx.tenant_id,
                        "reservation": reservation["reservation_id"],
                    },
                )
                return {
                    "action_id": action_id,
                    "reservation_id": reservation["reservation_id"],
                    "confirmation_token": token,
                    "expires_at": expires,
                    "version": int(confirmation["version"]) + 1,
                }
            if action["status"] != "PROPOSED" or action["run_status"] != "WAITING_CONFIRMATION":
                raise DomainError("DUPLICATE_ACTION", "Action is no longer awaiting approval", 409)
            current = await session.execute(
                text(
                    "SELECT * FROM tripmate.action_confirmation WHERE tenant_id=:tenant AND action_id=:action FOR UPDATE"
                ),
                {"tenant": ctx.tenant_id, "action": action_id},
            )
            record = current.mappings().one_or_none()
            expected = (
                expected_version
                if expected_version is not None
                else (int(record["version"]) if record else None)
            )
            if record is None:
                confirmation_id = self.ids.new()
                await session.execute(
                    text("""INSERT INTO tripmate.action_confirmation
                        (confirmation_id,tenant_id,action_id,user_id,token_hash,parameters_hash,issued_at,expires_at,status)
                        VALUES(:id,:tenant,:action,:user,:hash,:parameters_hash,:issued,:expires,'ISSUED')"""),
                    {
                        "id": confirmation_id,
                        "tenant": ctx.tenant_id,
                        "action": action_id,
                        "user": ctx.user_id,
                        "hash": sha256_hex(token),
                        "parameters_hash": action["parameters_hash"],
                        "issued": now,
                        "expires": expires,
                    },
                )
                version = 1
            else:
                if expected is not None and int(record["version"]) != expected:
                    raise DomainError("VERSION_CONFLICT", "Confirmation version is stale", 409)
                await session.execute(
                    text("""UPDATE tripmate.action_confirmation SET token_hash=:hash,issued_at=:issued,
                        expires_at=:expires,status='ISSUED',consumed_at=NULL,version=version+1
                        WHERE tenant_id=:tenant AND action_id=:action"""),
                    {
                        "hash": sha256_hex(token),
                        "issued": now,
                        "expires": expires,
                        "tenant": ctx.tenant_id,
                        "action": action_id,
                    },
                )
                version = int(record["version"]) + 1
        return {
            "action_id": action_id,
            "confirmation_token": token,
            "expires_at": expires,
            "version": version,
        }

    async def reject_action(
        self, ctx: RequestContext, action_id: UUID, *, expected_version: int, idempotency_key: str
    ) -> dict[str, Any]:
        self._require_user(ctx)
        assert ctx.user_id is not None
        route = f"/v1/actions/{action_id}/reject"
        operation = "action.reject"
        scope = IdempotencyScope(ctx.tenant_id, ctx.user_id, route, operation, idempotency_key)
        key_hash = scope.key_hash()
        req_hash = request_hash(
            {"action_id": action_id, "expected_version": expected_version, "decision": "reject"}
        )
        now = self.clock.now()
        result_payload = {
            "action_id": str(action_id),
            "status": "REJECTED",
            "decision": "reject",
            "version": expected_version + 1,
        }
        unknown_commit = False
        async with self.sessions() as session:
            tx = await session.begin()
            try:
                await self._lock_idempotency_scope(session, key_hash)
                prior = await self._get_idempotency(session, ctx, route, operation, key_hash)
                if prior is not None:
                    payload = (
                        prior["result_json"]
                        if isinstance(prior["result_json"], dict)
                        else json.loads(prior["result_json"])
                    )
                    if prior["request_hash"] != req_hash:
                        raise DomainError(
                            "IDEMPOTENCY_KEY_REUSE", "Idempotency key was reused", 409
                        )
                    await tx.rollback()
                    return payload
                action_result = await session.execute(
                    text("""SELECT a.*,r.status AS run_status FROM tripmate.agent_action a
                        JOIN tripmate.agent_run r ON r.tenant_id=a.tenant_id AND r.run_id=a.run_id
                        WHERE a.tenant_id=:tenant AND a.action_id=:action AND r.user_id=:user
                        FOR UPDATE OF a,r"""),
                    {"tenant": ctx.tenant_id, "action": action_id, "user": ctx.user_id},
                )
                action = action_result.mappings().one_or_none()
                if action is None:
                    raise DomainError("NOT_FOUND", "Action was not found", 404)
                if int(action["version"]) != expected_version:
                    raise DomainError("VERSION_CONFLICT", "Action version is stale", 409)
                if action["status"] != "PROPOSED" or action["run_status"] != "WAITING_CONFIRMATION":
                    raise DomainError(
                        "DUPLICATE_ACTION", "Action is no longer awaiting a decision", 409
                    )
                await session.execute(
                    text("""UPDATE tripmate.action_confirmation SET status='REJECTED',consumed_at=:now,
                        version=version+1 WHERE tenant_id=:tenant AND action_id=:action AND status='ISSUED'"""),
                    {"now": now, "tenant": ctx.tenant_id, "action": action_id},
                )
                action_update = await session.execute(
                    text("""UPDATE tripmate.agent_action SET status='REJECTED',version=version+1,updated_at=:now
                        WHERE tenant_id=:tenant AND action_id=:action AND status='PROPOSED' AND version=:version"""),
                    {
                        "now": now,
                        "tenant": ctx.tenant_id,
                        "action": action_id,
                        "version": expected_version,
                    },
                )
                if affected_rows(action_update) != 1:
                    raise DomainError("DUPLICATE_ACTION", "Action rejection lost its CAS", 409)
                resume = {"decision": "rejected", "action_id": str(action_id)}
                run_update = await session.execute(
                    text("""UPDATE tripmate.agent_run SET status='RUNNING',awaiting_action_id=NULL,
                        resume_command=CAST(:resume AS jsonb),lease_owner=NULL,lease_until=NULL,
                        fencing_version=fencing_version+1,version=version+1,updated_at=:now
                        WHERE tenant_id=:tenant AND run_id=:run AND user_id=:user
                          AND status='WAITING_CONFIRMATION' RETURNING version"""),
                    {
                        "resume": json.dumps(resume),
                        "now": now,
                        "tenant": ctx.tenant_id,
                        "run": action["run_id"],
                        "user": ctx.user_id,
                    },
                )
                run_version = run_update.scalar_one_or_none()
                if run_version is None:
                    raise DomainError(
                        "VERSION_CONFLICT", "Run state changed while rejecting action", 409
                    )
                await self._insert_outbox(
                    session,
                    replace(ctx, run_id=action["run_id"], action_id=action_id),
                    aggregate_type="agent_run",
                    aggregate_id=action["run_id"],
                    aggregate_version=int(run_version),
                    event_type="agent.run.resume_requested.v1",
                    payload={"run_id": str(action["run_id"]), **resume},
                )
                await self._insert_audit(
                    session,
                    ctx,
                    action="agent.action.rejected",
                    result_code="REJECTED",
                    action_id=action_id,
                    parameters_hash=action["parameters_hash"],
                )
                await self._insert_idempotency(
                    session,
                    ctx,
                    route,
                    operation,
                    key_hash,
                    req_hash,
                    200,
                    result_payload,
                    "agent_action",
                    action_id,
                )
                try:
                    await tx.commit()
                except DBAPIError:
                    unknown_commit = True
            except Exception:
                if tx.is_active:
                    await tx.rollback()
                raise
        if unknown_commit:
            async with self.sessions() as session:
                prior = await self._get_idempotency(session, ctx, route, operation, key_hash)
                if prior is None:
                    raise DomainError(
                        "RECONCILIATION_REQUIRED",
                        "Action rejection commit outcome is unknown",
                        503,
                        True,
                    )
                if prior["request_hash"] != req_hash:
                    raise DomainError("IDEMPOTENCY_KEY_REUSE", "Idempotency key was reused", 409)
                return (
                    prior["result_json"]
                    if isinstance(prior["result_json"], dict)
                    else json.loads(prior["result_json"])
                )
        return result_payload

    async def _consume_reservation_confirmation(
        self,
        session: AsyncSession,
        ctx: RequestContext,
        reservation: Any,
        action_id: UUID,
        token: str,
        now: datetime,
    ) -> Any:
        row_result = await session.execute(
            text("""SELECT * FROM tripmate.reservation_confirmation WHERE tenant_id=:tenant
                AND reservation_id=:reservation AND action_id=:action AND user_id=:user FOR UPDATE"""),
            {
                "tenant": ctx.tenant_id,
                "reservation": reservation["reservation_id"],
                "action": action_id,
                "user": ctx.user_id,
            },
        )
        row = row_result.mappings().one_or_none()
        if row is None or row["status"] != "ISSUED" or row["expires_at"] <= now:
            raise DomainError(
                "DUPLICATE_ACTION", "Reservation confirmation is expired or already consumed", 409
            )
        expected_parameters_hash = sha256_hex(
            canonical_json(
                {
                    "tenant_id": str(ctx.tenant_id),
                    "user_id": str(ctx.user_id),
                    "action_id": str(action_id),
                    "reservation_id": str(reservation["reservation_id"]),
                    "parameters_hash": str(reservation["parameters_hash"]),
                    "reservation_version": int(reservation["version"]),
                }
            )
        )
        if (
            row["token_hash"] != sha256_hex(token)
            or row["parameters_hash"] != expected_parameters_hash
        ):
            raise DomainError(
                "UNAUTHORIZED", "Reservation confirmation does not match this action", 401
            )
        return row

    async def _load_approved_action(
        self,
        session: AsyncSession,
        ctx: RequestContext,
        action_id: UUID,
        token: str,
        now: datetime,
        *,
        expected_kind: str,
    ) -> Any:
        result = await session.execute(
            text("""SELECT a.*,r.user_id,r.status AS run_status FROM tripmate.agent_action a
                JOIN tripmate.agent_run r ON r.tenant_id=a.tenant_id AND r.run_id=a.run_id
                WHERE a.tenant_id=:tenant AND a.action_id=:action AND r.user_id=:user FOR UPDATE OF a,r"""),
            {"tenant": ctx.tenant_id, "action": action_id, "user": ctx.user_id},
        )
        action = result.mappings().one_or_none()
        if action is None:
            raise DomainError("NOT_FOUND", "Action was not found", 404)
        if action["action_type"] != expected_kind or action["status"] != "PROPOSED":
            raise DomainError("DUPLICATE_ACTION", "Action is not a matching candidate", 409)
        if action["run_status"] != "WAITING_CONFIRMATION":
            raise DomainError("DUPLICATE_ACTION", "Run is not waiting for this action", 409)
        confirmation_result = await session.execute(
            text("""SELECT * FROM tripmate.action_confirmation WHERE tenant_id=:tenant AND action_id=:action
                AND user_id=:user FOR UPDATE"""),
            {"tenant": ctx.tenant_id, "action": action_id, "user": ctx.user_id},
        )
        confirmation = confirmation_result.mappings().one_or_none()
        if (
            confirmation is None
            or confirmation["status"] != "ISSUED"
            or confirmation["expires_at"] <= now
            or confirmation["token_hash"] != sha256_hex(token)
            or confirmation["parameters_hash"] != action["parameters_hash"]
        ):
            raise DomainError("UNAUTHORIZED", "Action confirmation is invalid or expired", 401)
        action_data = dict(action)
        action_data["parameters"] = (
            action_data["parameters"]
            if isinstance(action_data["parameters"], dict)
            else json.loads(action_data["parameters"])
        )
        return action_data

    async def _consume_action_confirmation(
        self, session: AsyncSession, ctx: RequestContext, action_id: UUID, token: str, now: datetime
    ) -> None:
        changed = await session.execute(
            text("""UPDATE tripmate.action_confirmation SET status='CONSUMED',consumed_at=:now,version=version+1
                WHERE tenant_id=:tenant AND action_id=:action AND user_id=:user AND status='ISSUED'
                AND token_hash=:hash AND expires_at>:now"""),
            {
                "now": now,
                "tenant": ctx.tenant_id,
                "action": action_id,
                "user": ctx.user_id,
                "hash": sha256_hex(token),
            },
        )
        if affected_rows(changed) != 1:
            raise DomainError("DUPLICATE_ACTION", "Action confirmation is no longer valid", 409)

    async def _lock_inventory(self, session: AsyncSession, tenant_id: UUID, slot_id: UUID) -> Any:
        result = await session.execute(
            text("""SELECT i.tenant_id,i.slot_id,i.total_quantity,i.available_quantity,i.held_quantity,
                i.confirmed_quantity,i.status AS inventory_status,i.reconciliation_generation,
                i.version AS inventory_version,s.price,s.currency,s.activity_version,s.status AS slot_status,
                s.start_at,s.end_at,s.cancellation_policy,s.policy_version,a.attraction_id,a.name AS attraction_name
                FROM tripmate.inventory i JOIN tripmate.activity_slot s
                  ON s.tenant_id=i.tenant_id AND s.slot_id=i.slot_id
                JOIN tripmate.attraction a ON a.tenant_id=s.tenant_id AND a.attraction_id=s.attraction_id
                WHERE i.tenant_id=:tenant AND i.slot_id=:slot FOR UPDATE OF i"""),
            {"tenant": tenant_id, "slot": slot_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise DomainError("NOT_FOUND", "Inventory slot was not found", 404)
        if int(row["available_quantity"]) + int(row["held_quantity"]) + int(
            row["confirmed_quantity"]
        ) != int(row["total_quantity"]):
            raise DomainError(
                "RECONCILIATION_REQUIRED", "Inventory invariant is violated", 503, True
            )
        return row

    async def _read_inventory(self, tenant_id: UUID, trip_id: UUID, slot_id: UUID) -> Any:
        async with self.sessions() as session:
            result = await session.execute(
                text("""SELECT i.*,s.price,s.currency,s.activity_version,s.status AS slot_status,
                    s.policy_version,s.cancellation_policy FROM tripmate.inventory i
                    JOIN tripmate.activity_slot s ON s.tenant_id=i.tenant_id AND s.slot_id=i.slot_id
                    JOIN tripmate.trip t ON t.tenant_id=s.tenant_id AND t.tenant_id=i.tenant_id
                    WHERE i.tenant_id=:tenant AND i.slot_id=:slot AND t.trip_id=:trip"""),
                {"tenant": tenant_id, "slot": slot_id, "trip": trip_id},
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise DomainError("NOT_FOUND", "Trip inventory was not found", 404)
            return row

    async def _lock_reservation(
        self, session: AsyncSession, ctx: RequestContext, reservation_id: UUID
    ) -> Any:
        result = await session.execute(
            text("""SELECT * FROM tripmate.reservation WHERE tenant_id=:tenant AND reservation_id=:id
                AND user_id=:user FOR UPDATE"""),
            {"tenant": ctx.tenant_id, "id": reservation_id, "user": ctx.user_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise DomainError("NOT_FOUND", "Reservation was not found", 404)
        return row

    async def _get_idempotency(
        self, session: AsyncSession, ctx: RequestContext, route: str, operation: str, key_hash: str
    ) -> Any:
        result = await session.execute(
            text("""SELECT request_hash,http_status,result_json FROM tripmate.idempotency_record
                WHERE tenant_id=:tenant AND user_id=:user AND route=:route AND operation=:operation
                  AND key_hash=:key_hash AND expires_at>now()"""),
            {
                "tenant": ctx.tenant_id,
                "user": ctx.user_id,
                "route": route,
                "operation": operation,
                "key_hash": key_hash,
            },
        )
        return result.mappings().one_or_none()

    @staticmethod
    async def _lock_idempotency_scope(session: AsyncSession, key_hash: str) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key_hash, 74192026))"),
            {"key_hash": key_hash},
        )

    async def _recover_idempotency(
        self, ctx: RequestContext, route: str, operation: str, key_hash: str, req_hash: str
    ) -> ReservationResult | None:
        try:
            async with self.sessions() as session:
                row = await self._get_idempotency(session, ctx, route, operation, key_hash)
                if row is None:
                    return None
                return self._replay(row, req_hash)
        except DomainError:
            raise
        except Exception:
            return None

    def _replay(self, row: Any, expected_hash: str) -> ReservationResult:
        if row["request_hash"] != expected_hash:
            raise DomainError(
                "IDEMPOTENCY_KEY_REUSE", "Idempotency key was used with a different request", 409
            )
        payload = (
            row["result_json"]
            if isinstance(row["result_json"], dict)
            else json.loads(row["result_json"])
        )
        result = ReservationResult.from_payload(payload, replayed=True)
        replay_tokens = payload.get("replay_tokens") or {}
        hold_claims = replay_tokens.get("hold")
        confirmation_claims = replay_tokens.get("confirmation")
        return replace(
            result,
            hold_token=self._sign_confirmation_claims(hold_claims) if hold_claims else None,
            confirmation_token=(
                self._sign_confirmation_claims(confirmation_claims)
                if confirmation_claims
                else None
            ),
        )

    async def _insert_idempotency(
        self,
        session: AsyncSession,
        ctx: RequestContext,
        route: str,
        operation: str,
        key_hash: str,
        req_hash: str,
        status: int,
        payload: dict[str, Any],
        resource_type: str,
        resource_id: UUID,
    ) -> None:
        now = self.clock.now()
        await session.execute(
            text("""DELETE FROM tripmate.idempotency_record
                WHERE tenant_id=:tenant AND user_id=:user AND route=:route AND operation=:operation
                  AND key_hash=:key_hash AND expires_at<=:now"""),
            {
                "tenant": ctx.tenant_id,
                "user": ctx.user_id,
                "route": route,
                "operation": operation,
                "key_hash": key_hash,
                "now": now,
            },
        )
        await session.execute(
            text("""INSERT INTO tripmate.idempotency_record
                (tenant_id,user_id,route,operation,key_hash,request_hash,http_status,result_json,
                 resource_type,resource_id,expires_at)
                VALUES(:tenant,:user,:route,:operation,:key_hash,:request_hash,:status,
                 CAST(:result AS jsonb),:resource_type,:resource_id,:expires)"""),
            {
                "tenant": ctx.tenant_id,
                "user": ctx.user_id,
                "route": route,
                "operation": operation,
                "key_hash": key_hash,
                "request_hash": req_hash,
                "status": status,
                "result": json.dumps(payload),
                "resource_type": resource_type,
                "resource_id": resource_id,
                "expires": now + timedelta(seconds=self.settings.idempotency_ttl_seconds),
            },
        )

    async def _insert_outbox(
        self,
        session: AsyncSession,
        ctx: RequestContext,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        aggregate_version: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        event_id = self.ids.new()
        envelope = EventEnvelope(
            event_id=event_id,
            event_type=event_type,
            event_version=1,
            occurred_at=self.clock.now(),
            tenant_id=ctx.tenant_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            aggregate_version=aggregate_version,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            run_id=ctx.run_id,
            operation=ctx.operation,
            idempotency_key_hash=None,
            producer="tripmate-api",
            payload=payload,
        )
        await session.execute(
            text("""INSERT INTO tripmate.outbox_event
                (event_id,tenant_id,aggregate_type,aggregate_id,aggregate_version,event_type,event_version,
                 payload,status)
                VALUES(:event_id,:tenant,:aggregate_type,:aggregate_id,:aggregate_version,:event_type,
                 :event_version,CAST(:payload AS jsonb),'PENDING')"""),
            {
                "event_id": event_id,
                "tenant": ctx.tenant_id,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "aggregate_version": aggregate_version,
                "event_type": event_type,
                "event_version": 1,
                "payload": json.dumps(envelope.model_dump(mode="json")),
            },
        )

    async def _insert_audit(
        self,
        session: AsyncSession,
        ctx: RequestContext,
        *,
        action: str,
        result_code: str,
        action_id: UUID | None = None,
        reservation_id: UUID | None = None,
        parameters_hash: str | None = None,
    ) -> None:
        await session.execute(
            text("""INSERT INTO tripmate.audit_log
                (audit_id,tenant_id,actor_user_id,run_id,action_id,reservation_id,request_id,trace_id,
                 action,parameters_hash,result_code,details_redacted)
                VALUES(:id,:tenant,:user,:run,:action_id,:reservation,:request,:trace,:action,
                 :parameters_hash,:result,'{}'::jsonb)"""),
            {
                "id": self.ids.new(),
                "tenant": ctx.tenant_id,
                "user": ctx.user_id,
                "run": ctx.run_id,
                "action_id": action_id,
                "reservation": reservation_id,
                "request": ctx.request_id,
                "trace": ctx.trace_id,
                "action": action,
                "parameters_hash": parameters_hash,
                "result": result_code,
            },
        )

    async def _link_trip_item(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        trip_id: UUID,
        slot_id: UUID,
        stock: Any,
        reservation_id: UUID,
        now: datetime,
    ) -> None:
        await session.execute(
            text("""INSERT INTO tripmate.trip_item
                (trip_item_id,tenant_id,trip_id,sequence_no,attraction_id,slot_id,start_at,end_at,
                 cost,currency,constraint_status)
                SELECT :id,:tenant,:trip,COALESCE(MAX(sequence_no),0)+1,:attraction,:slot,:start,:end,
                    :price,:currency,'PASS' FROM tripmate.trip_item
                WHERE tenant_id=:tenant AND trip_id=:trip
                ON CONFLICT (tenant_id,trip_id,sequence_no) DO NOTHING"""),
            {
                "id": self.ids.new(),
                "tenant": tenant_id,
                "trip": trip_id,
                "attraction": stock["attraction_id"],
                "slot": slot_id,
                "start": stock["start_at"],
                "end": stock["end_at"],
                "price": stock["price"],
                "currency": stock["currency"],
            },
        )

    @staticmethod
    def _canonical_action_parameters(
        *,
        tenant_id: UUID,
        user_id: UUID,
        trip_id: UUID,
        slot_id: UUID,
        quantity: int,
        price: Decimal,
        currency: str,
        activity_version: int,
        inventory_version: int,
        policy_version: str,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "trip_id": str(trip_id),
            "slot_id": str(slot_id),
            "quantity": quantity,
            "reason": reason,
            "price": str(price.quantize(Decimal("0.01"))),
            "currency": currency,
            "activity_version": activity_version,
            "inventory_version": inventory_version,
            "policy_version": policy_version,
        }

    @staticmethod
    def _action_matches(
        parameters: dict[str, Any], trip_id: UUID, slot_id: UUID | None, quantity: int | None
    ) -> bool:
        if not isinstance(parameters, dict):
            parameters = json.loads(parameters)
        if "reservation_id" in parameters:
            return str(parameters["reservation_id"]) == str(trip_id)
        return (
            str(parameters.get("trip_id")) == str(trip_id)
            and str(parameters.get("slot_id")) == str(slot_id)
            and int(parameters.get("quantity", -1)) == quantity
        )

    @staticmethod
    def _check_expected_versions(
        stock: Any, inventory_version: int | None, activity_version: int | None
    ) -> None:
        if inventory_version is not None and inventory_version != int(stock["inventory_version"]):
            raise DomainError("VERSION_CONFLICT", "Inventory snapshot is stale", 409)
        if activity_version is not None and activity_version != int(stock["activity_version"]):
            raise DomainError("VERSION_CONFLICT", "Activity snapshot is stale", 409)

    @staticmethod
    def _assert_inventory_consistent(stock: Any, reservation: Any) -> None:
        quantity = int(reservation["quantity"])
        bucket = "held_quantity" if reservation["status"] == "HELD" else "confirmed_quantity"
        if int(stock[bucket]) < quantity:
            raise DomainError(
                "RECONCILIATION_REQUIRED",
                "Reservation quantity is missing from inventory",
                503,
                True,
            )
        if int(stock["available_quantity"]) + int(stock["held_quantity"]) + int(
            stock["confirmed_quantity"]
        ) != int(stock["total_quantity"]):
            raise DomainError(
                "RECONCILIATION_REQUIRED", "Inventory invariant is violated", 503, True
            )

    @staticmethod
    def _inventory_snapshot(
        stock: Any, available: int, held: int, confirmed: int, version: int
    ) -> dict[str, Any]:
        return {
            "total": int(stock["total_quantity"]),
            "available": available,
            "held": held,
            "confirmed": confirmed,
            "status": stock["inventory_status"],
            "version": version,
            "generation": int(stock["reconciliation_generation"]),
        }

    async def _project_after_commit(
        self,
        tenant_id: UUID,
        slot_id: UUID,
        reservation_id: UUID,
        snapshot: dict[str, Any] | None,
        hold: dict[str, Any] | None,
    ) -> None:
        if self.projection is None or snapshot is None:
            return
        try:
            await self.projection.project(
                tenant_id=tenant_id,
                slot_id=slot_id,
                reservation_id=reservation_id,
                snapshot=snapshot,
                hold=hold,
            )
        except Exception:
            logger.warning(
                "redis_projection_pending",
                extra={
                    "tenant_id": str(tenant_id),
                    "reservation_id": str(reservation_id),
                    "slot_id": str(slot_id),
                },
            )

    @staticmethod
    async def _assert_agent_lease(
        session: AsyncSession, ctx: RequestContext, run_id: UUID, now: datetime
    ) -> None:
        if ctx.source != "agent" or ctx.lease_owner is None or ctx.fencing_version is None:
            raise DomainError("STALE_LEASE", "Agent run lease metadata is missing", 409)
        result = await session.scalar(
            text("""SELECT 1 FROM tripmate.agent_run WHERE tenant_id=:tenant AND run_id=:run
                AND user_id=:user AND status='RUNNING' AND lease_owner=:owner
                AND fencing_version=:fence AND lease_until>:now FOR UPDATE"""),
            {
                "tenant": ctx.tenant_id,
                "run": run_id,
                "user": ctx.user_id,
                "owner": ctx.lease_owner,
                "fence": ctx.fencing_version,
                "now": now,
            },
        )
        if result is None:
            raise DomainError("STALE_LEASE", "Agent run lease is no longer current", 409, True)

    @staticmethod
    def _confirmation_claims(
        ctx: RequestContext,
        action_id: UUID,
        confirmation_id: UUID,
        parameters_hash: str,
        version: int,
        expires_at: datetime,
        *,
        reservation_id: UUID | None = None,
        kind: str,
    ) -> dict[str, Any]:
        claims: dict[str, Any] = {
            "purpose": "tripmate-action-confirmation",
            "kind": kind,
            "tenant_id": str(ctx.tenant_id),
            "user_id": str(ctx.user_id),
            "action_id": str(action_id),
            "confirmation_id": str(confirmation_id),
            "parameters_hash": parameters_hash,
            "version": version,
            "expires_at": expires_at.isoformat(),
        }
        if reservation_id is not None:
            claims["reservation_id"] = str(reservation_id)
        return claims

    def _sign_confirmation_claims(self, claims: dict[str, Any]) -> str:
        payload = canonical_json(claims).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
        secret = self.settings.auth_signing_secret.get_secret_value().encode("utf-8")
        signature = hmac.new(secret, encoded.encode("ascii"), digestmod="sha256").digest()
        encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        return f"{encoded}.{encoded_signature}"

    def _restore_confirmation_response(self, stored: Any) -> dict[str, Any]:
        value = stored if isinstance(stored, dict) else json.loads(stored)
        claims = value["token_claims"]
        response: dict[str, Any] = {
            "action_id": UUID(value["action_id"]),
            "confirmation_token": self._sign_confirmation_claims(claims),
            "expires_at": datetime.fromisoformat(value["expires_at"]),
            "version": int(value["version"]),
        }
        if value.get("reservation_id"):
            response["reservation_id"] = UUID(value["reservation_id"])
        return response

    @staticmethod
    def _require_user(ctx: RequestContext) -> None:
        if ctx.user_id is None:
            raise DomainError("UNAUTHORIZED", "Authenticated user is required", 401)

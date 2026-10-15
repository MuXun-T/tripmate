from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.domain.errors import DomainError
from app.domain.models import TripPlan


@dataclass(frozen=True, slots=True)
class InventoryFact:
    slot_id: str
    available_quantity: int
    inventory_version: int
    queried: bool = True


@dataclass(frozen=True, slots=True)
class ConstraintResult:
    feasible: bool
    state: str
    checks: tuple[dict[str, str], ...]
    total_cost: Decimal


def validate_trip_plan(
    plan: TripPlan,
    *,
    inventory: dict[str, InventoryFact],
    confirmed_slot_ids: frozenset[str] = frozenset(),
    open_slot_ids: frozenset[str] | None = None,
    budget_limit: Decimal | None = None,
) -> ConstraintResult:
    checks: list[dict[str, str]] = []
    hard_failed = False
    unknown = False
    if plan.end_date < plan.start_date:
        raise DomainError("TRIP_INFEASIBLE", "Trip end date precedes its start date", 422)
    ordered = sorted(plan.items, key=lambda item: (item.window.start_at, item.sequence_no))
    previous = None
    total = Decimal("0")
    for item in ordered:
        start_day = item.window.start_at.astimezone(ZoneInfo(plan.timezone)).date()
        if not (plan.start_date <= start_day <= plan.end_date):
            hard_failed = True
            checks.append({"code": "ITEM_OUTSIDE_TRIP", "state": "FAIL"})
        if previous is not None and item.window.start_at < previous.window.end_at:
            hard_failed = True
            checks.append({"code": "TIME_OVERLAP", "state": "FAIL"})
        if item.slot_id is not None and open_slot_ids is not None:
            if str(item.slot_id) not in open_slot_ids:
                hard_failed = True
                checks.append({"code": "SLOT_CLOSED", "state": "FAIL"})
        if item.reservation_required and item.slot_id is not None:
            fact = inventory.get(str(item.slot_id))
            if fact is None or not fact.queried:
                unknown = True
                checks.append({"code": "INVENTORY_UNKNOWN", "state": "UNKNOWN"})
            elif fact.available_quantity < plan.party_size:
                hard_failed = True
                checks.append({"code": "INVENTORY_NOT_ENOUGH", "state": "FAIL"})
        total += item.cost.amount
        previous = item
    plan_slots = {str(item.slot_id) for item in plan.items if item.slot_id is not None}
    if not confirmed_slot_ids.issubset(plan_slots):
        hard_failed = True
        checks.append({"code": "CONFIRMED_RESERVATION_REMOVED", "state": "FAIL"})
    if plan.total_cost.amount != total:
        hard_failed = True
        checks.append({"code": "COST_SNAPSHOT_MISMATCH", "state": "FAIL"})
    if budget_limit is not None and total > budget_limit:
        hard_failed = True
        checks.append({"code": "BUDGET_EXCEEDED", "state": "FAIL"})
    if total < 0:
        hard_failed = True
        checks.append({"code": "INVALID_COST", "state": "FAIL"})
    state = "FAIL" if hard_failed else "UNKNOWN" if unknown else "PASS"
    return ConstraintResult(not hard_failed and not unknown, state, tuple(checks), total)


def require_feasible(result: ConstraintResult) -> None:
    if not result.feasible:
        raise DomainError(
            "TRIP_INFEASIBLE",
            "The itinerary has hard constraints that are not satisfied",
            422,
            details={"state": result.state, "checks": list(result.checks)},
        )

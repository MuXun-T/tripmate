from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ReservationStatus(StrEnum):
    HELD = "HELD"
    CONFIRMED = "CONFIRMED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    WAITLISTED = "WAITLISTED"


class ActionStatus(StrEnum):
    PROPOSED = "PROPOSED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class ConstraintState(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_EVALUATED = "NOT_EVALUATED"


class Money(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount: Decimal = Field(ge=Decimal("0"), max_digits=12, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class TimeWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    start_at: datetime
    end_at: datetime
    timezone: str

    @model_validator(mode="after")
    def validate_window(self) -> "TimeWindow":
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("Time window instants must be timezone aware")
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        return self


class CancellationRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    cutoff_at: datetime
    refund_percent: Decimal = Field(ge=0, le=100, decimal_places=2)
    reason_code: str = Field(min_length=1, max_length=80)

    @field_validator("cutoff_at")
    @classmethod
    def aware_cutoff(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("cutoff_at must be timezone aware")
        return value


class CancellationPolicySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    policy_id: str
    policy_version: str
    timezone: str
    source: str = "catalog"
    rules: tuple[CancellationRule, ...]

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        return value


class CancellationDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    refund_percent: Decimal = Field(ge=0, le=100)
    reason_code: str
    policy_version: str


class ActivityPlanItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    sequence_no: int = Field(ge=1)
    attraction_id: UUID | None = None
    slot_id: UUID | None = None
    title: str = Field(min_length=1, max_length=200)
    window: TimeWindow
    cost: Money
    route_from_previous_minutes: int | None = Field(default=None, ge=0)
    indoor: bool | None = None
    reservation_required: bool = False
    inventory_version: int | None = Field(default=None, ge=1)
    activity_version: int | None = Field(default=None, ge=1)
    constraint_status: ConstraintState = ConstraintState.NOT_EVALUATED
    citation_ids: tuple[str, ...] = ()


class TripPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    trip_id: UUID | None = None
    destination: str = Field(min_length=1, max_length=120)
    start_date: date
    end_date: date
    timezone: str
    party_size: int = Field(ge=1, le=100)
    items: tuple[ActivityPlanItem, ...]
    total_cost: Money
    feasible: bool
    generated_at: datetime
    planner_version: str
    hard_constraints: tuple[dict[str, Any], ...] = ()
    soft_preferences: tuple[dict[str, Any], ...] = ()

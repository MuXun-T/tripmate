from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]*\.v[1-9][0-9]*$")
    event_version: int = Field(ge=1)
    occurred_at: datetime
    tenant_id: UUID
    aggregate_type: str = Field(min_length=1, max_length=80)
    aggregate_id: UUID
    aggregate_version: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)
    run_id: UUID | None = None
    operation: str | None = None
    idempotency_key_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    original_event_id: UUID | None = None
    attempt: int = Field(default=1, ge=1)
    producer: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any]

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must be timezone aware")
        return value

    @field_validator("payload")
    @classmethod
    def forbid_sensitive_fields(cls, value: dict[str, Any]) -> dict[str, Any]:
        blocked = (
            "token",
            "hold_token",
            "confirmation_token",
            "authorization",
            "password",
            "raw_message",
            "prompt",
            "full_user_message",
            "diet",
            "allerg",
            "sensitive_preferences",
        )

        def inspect(item: Any) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if any(part in key.casefold() for part in blocked):
                        raise ValueError(f"sensitive event field is forbidden: {key}")
                    inspect(child)
            elif isinstance(item, list):
                for child in item:
                    inspect(child)

        inspect(value)
        return value


class RunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: Literal[
        "run_started",
        "node_started",
        "tool_start",
        "tool_end",
        "citation",
        "approval_required",
        "confirmation",
        "reservation_updated",
        "done",
        "completed",
        "error",
    ]
    payload: dict[str, Any]

    @field_validator("payload")
    @classmethod
    def forbid_sensitive_fields(cls, value: dict[str, Any]) -> dict[str, Any]:
        blocked = (
            "token",
            "hold_token",
            "confirmation_token",
            "authorization",
            "password",
            "raw_message",
            "prompt",
            "full_user_message",
            "diet",
            "allerg",
            "sensitive_preferences",
        )

        def inspect(item: Any) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if any(part in key.casefold() for part in blocked):
                        raise ValueError(f"sensitive run event field is forbidden: {key}")
                    inspect(child)
            elif isinstance(item, list):
                for child in item:
                    inspect(child)

        inspect(value)
        return value

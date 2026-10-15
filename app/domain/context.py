from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RequestContext:
    request_id: str
    trace_id: str
    tenant_id: UUID
    user_id: UUID | None
    roles: tuple[str, ...]
    deadline: datetime
    source: Literal["http", "agent", "worker", "replay"]
    run_id: UUID | None = None
    thread_id: str | None = None
    trip_id: UUID | None = None
    reservation_id: UUID | None = None
    idempotency_key: str | None = None
    operation: str | None = None
    action_id: UUID | None = None
    lease_owner: str | None = None
    fencing_version: int | None = None

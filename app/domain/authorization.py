from typing import Literal

from app.domain.errors import DomainError

TripRole = Literal["OWNER", "EDITOR", "VIEWER"]

# Resource permissions are intentionally small for the local MVP.
_PERMISSIONS: dict[str, frozenset[TripRole]] = {
    "read": frozenset({"OWNER", "EDITOR", "VIEWER"}),
    "plan": frozenset({"OWNER", "EDITOR"}),
    "confirm": frozenset({"OWNER", "EDITOR"}),
    "cancel": frozenset({"OWNER", "EDITOR"}),
    "manage_members": frozenset({"OWNER"}),
}


def allows(role: str, operation: str) -> bool:
    return role in _PERMISSIONS.get(operation, frozenset())


def require_role(role: str | None, operation: str) -> TripRole:
    if role not in {"OWNER", "EDITOR", "VIEWER"} or not allows(role, operation):
        raise DomainError("FORBIDDEN", "The authenticated role cannot perform this operation", 403)
    return role  # type: ignore[return-value]

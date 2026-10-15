from dataclasses import dataclass, field
from typing import Any


@dataclass(eq=False)
class DomainError(Exception):
    code: str
    message: str
    http_status: int = 409
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def invalid_argument(message: str, **details: Any) -> DomainError:
    return DomainError("INVALID_ARGUMENT", message, 400, False, details)


def not_found(resource: str) -> DomainError:
    return DomainError("NOT_FOUND", f"{resource} was not found", 404)


def tenant_scope_denied() -> DomainError:
    return DomainError("TENANT_SCOPE_DENIED", "Resource is outside the authenticated scope", 403)


def dependency_unavailable(message: str = "A required dependency is unavailable") -> DomainError:
    return DomainError("DEPENDENCY_UNAVAILABLE", message, 503, True)

import pytest

from app.domain.authorization import allows, require_role
from app.domain.errors import DomainError


def test_trip_role_matrix_is_explicit() -> None:
    assert all(allows(role, "read") for role in ("OWNER", "EDITOR", "VIEWER"))
    assert allows("OWNER", "manage_members")
    assert not allows("EDITOR", "manage_members")
    assert not allows("VIEWER", "plan")
    assert allows("EDITOR", "confirm")


def test_forbidden_role_is_a_stable_domain_error() -> None:
    with pytest.raises(DomainError, match="cannot perform") as error:
        require_role("VIEWER", "cancel")
    assert error.value.code == "FORBIDDEN"
    assert error.value.http_status == 403

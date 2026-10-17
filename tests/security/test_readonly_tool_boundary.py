from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain.context import RequestContext
from app.domain.errors import DomainError
from app.tools.gateway import PreferenceInput, ToolGateway, TripInput


def _gateway() -> ToolGateway:
    gateway = object.__new__(ToolGateway)
    gateway.registry = {
        "get_trip_context": (TripInput, None, SimpleNamespace(description="Read a Trip.")),
        "save_user_preference": (
            PreferenceInput,
            None,
            SimpleNamespace(description="Propose a preference."),
        ),
    }
    gateway.invoke = AsyncMock(return_value={"trip": {"status": "PLANNING"}})
    return gateway


def _context() -> RequestContext:
    return RequestContext(
        request_id="security-test",
        trace_id="security-test",
        tenant_id=uuid4(),
        user_id=uuid4(),
        roles=("user",),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        source="agent",
        run_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_readonly_gateway_hides_and_rejects_registered_write_tools() -> None:
    gateway = _gateway()
    catalog = gateway.readonly_tool_schemas()

    assert [item["tool_id"] for item in catalog] == ["get_trip_context"]
    with pytest.raises(DomainError, match="read-only"):
        await gateway.invoke_readonly(
            "save_user_preference",
            {
                "scope": "personal",
                "key": "travel_style.tags",
                "value": ["indoor"],
                "reason": "model request",
            },
            _context(),
        )
    gateway.invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_readonly_gateway_validates_arguments_before_dispatch() -> None:
    gateway = _gateway()

    with pytest.raises(ValidationError):
        await gateway.invoke_readonly(
            "get_trip_context",
            {"trip_id": str(uuid4()), "tenant_id": str(uuid4())},
            _context(),
        )
    gateway.invoke.assert_not_awaited()

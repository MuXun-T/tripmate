from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agent.worker import RunWorker
from app.domain.errors import DomainError
from app.events.runtime import InboxProcessor, event_domain
from app.reminders.service import _notification_text


def test_checkpoint_scope_and_schema_are_fail_closed() -> None:
    claim = {
        "run_id": uuid4(),
        "tenant_id": uuid4(),
        "user_id": uuid4(),
        "thread_id": "thread-1",
        "fencing_version": 4,
    }
    snapshot = SimpleNamespace(
        values={
            "schema_version": 1,
            "run_id": str(claim["run_id"]),
            "tenant_id": str(claim["tenant_id"]),
            "user_id": str(claim["user_id"]),
            "thread_id": claim["thread_id"],
            "fencing_version": 4,
        }
    )
    RunWorker._validate_checkpoint(claim, snapshot)

    snapshot.values["tenant_id"] = str(uuid4())
    with pytest.raises(DomainError, match="Checkpoint tenant_id"):
        RunWorker._validate_checkpoint(claim, snapshot)
    snapshot.values["tenant_id"] = str(claim["tenant_id"])
    snapshot.values["schema_version"] = 2
    with pytest.raises(DomainError, match="schema version"):
        RunWorker._validate_checkpoint(claim, snapshot)


def test_consumer_watermark_ordering_and_event_domain_are_deterministic() -> None:
    assert InboxProcessor._message_id_key("12-3") < InboxProcessor._message_id_key("12-4")
    assert InboxProcessor._message_id_key("12-4") < InboxProcessor._message_id_key("13-0")
    assert InboxProcessor._message_id_key("invalid") == (-1, -1)
    assert event_domain("reservation.waitlist_promotion_proposed.v1") == "reservations"


def test_reminder_copy_is_explicitly_a_local_notification() -> None:
    title, body = _notification_text("weather_check", datetime(2026, 10, 20, tzinfo=UTC))
    assert title == "天气提醒"
    assert body.startswith("提醒时间：2026-10-20T00:00:00+00:00")

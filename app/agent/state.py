from __future__ import annotations

from typing import Annotated, Any, TypedDict
from uuid import UUID


def merge_by_id(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values = {
        str(item.get("chunk_id", item.get("action_id", item.get("id", index)))): item
        for index, item in enumerate(left)
    }
    for index, item in enumerate(right):
        key = str(item.get("chunk_id", item.get("action_id", item.get("id", index))))
        values[key] = item
    return list(values.values())


def merge_mapping(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {**left, **right}


class AgentState(TypedDict, total=False):
    schema_version: int
    run_id: UUID
    tenant_id: UUID
    user_id: UUID
    thread_id: str
    conversation_id: UUID
    message_id: UUID
    lease_owner: str
    fencing_version: int
    trip_id: UUID | None
    message: str
    message_hash: str
    conversation_memory: dict[str, Any]
    intent: str
    missing_slots: list[str]
    preference_refs: list[dict[str, Any]]
    citations: Annotated[list[dict[str, Any]], merge_by_id]
    realtime_facts: Annotated[dict[str, Any], merge_mapping]
    inventory_facts: dict[str, dict[str, int]]
    trip_context: dict[str, Any]
    trip_plan: dict[str, Any]
    constraint_result: dict[str, Any]
    pending_actions: Annotated[list[dict[str, Any]], merge_by_id]
    awaiting_action_id: UUID | None
    approval_response: dict[str, Any]
    result: dict[str, Any]
    errors: Annotated[list[dict[str, Any]], merge_by_id]
    node: str

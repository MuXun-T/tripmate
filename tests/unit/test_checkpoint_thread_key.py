from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import GraphDependencies, build_graph
from app.agent.worker import RunWorker
from app.domain.ports import SystemClock


def _runs_for(user, thread, conversation, message):
    return SimpleNamespace(
        load_run_input=AsyncMock(
            return_value={
                "user_id": user,
                "thread_id": thread,
                "content_redacted": message,
                "content_hash": "a" * 64,
                "message_id": uuid4(),
                "conversation_id": conversation,
            }
        ),
        append_run_event=AsyncMock(),
    )


async def test_two_runs_in_same_conversation_both_complete() -> None:
    tenant, user, conversation = uuid4(), uuid4(), uuid4()
    thread = f"tenant:{tenant}:user:{user}:conversation:{conversation}"
    saver = InMemorySaver()
    deps = GraphDependencies(
        sessions=None,
        settings=SimpleNamespace(rag_top_k=3, llm_timeout_seconds=1),
        clock=SystemClock(),
        ids=None,
        runs=_runs_for(user, thread, conversation, "周末去杭州玩"),
        reservations=None,
        preferences=SimpleNamespace(list_for_user=AsyncMock(return_value=[])),
        short_term_memory=SimpleNamespace(
            get=AsyncMock(return_value=None), merge=AsyncMock(return_value=None)
        ),
        knowledge=SimpleNamespace(search=AsyncMock(return_value=[])),
        tools=None,
    )
    worker = RunWorker.__new__(RunWorker)
    worker.graph = build_graph(deps, saver)
    worker.runtime = SimpleNamespace(checkpointer=saver)
    worker.worker_id = "test-worker"
    for message in ("周末去杭州玩", "那换成室内的呢"):
        deps.runs = _runs_for(user, thread, conversation, message)
        claim = {
            "run_id": uuid4(),
            "tenant_id": tenant,
            "user_id": user,
            "thread_id": thread,
            "trip_id": None,
            "fencing_version": 1,
            "resume_command": None,
        }
        output = await worker._invoke_graph(claim)
        assert output["result"]["run_id"] == str(claim["run_id"])

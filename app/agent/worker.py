import asyncio
import json
import logging
import os
import socket
from contextlib import suppress
from typing import Any
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.agent.graph import GraphDependencies, build_graph
from app.domain.errors import DomainError
from app.events.runtime import InboxProcessor, OutboxPublisher, event_domain, outbox_loop
from app.events.schema import EventEnvelope
from app.runtime import Runtime

logger = logging.getLogger(__name__)
LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 15


class RunWorker:
    def __init__(self, runtime: Runtime, *, worker_id: str | None = None) -> None:
        self.runtime = runtime
        self.worker_id = worker_id or (f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}")
        self.graph: CompiledStateGraph[Any, Any, Any, Any] = build_graph(
            GraphDependencies(
                sessions=runtime.sessions,
                settings=runtime.settings,
                clock=runtime.reservations.clock,
                ids=runtime.reservations.ids,
                runs=runtime.runs,
                reservations=runtime.reservations,
                preferences=runtime.preferences,
                short_term_memory=runtime.short_term_memory,
                knowledge=runtime.knowledge,
                tools=runtime.tools,
            ),
            runtime.checkpointer,
        )
        self.inbox = InboxProcessor(
            runtime.sessions,
            runtime.redis,
            runtime.settings,
            consumer=self.worker_id,
            group="tripmate-workers-v1",
            handler=self._handle_event,
        )

    async def run_once(self) -> bool:
        candidates = await self.runtime.runs.list_claimable()
        for candidate in candidates:
            async with self.runtime.engine.connect() as lock_connection:
                acquired = await self._try_lock_thread(lock_connection, str(candidate["thread_id"]))
                if not acquired:
                    continue
                try:
                    claim = await self.runtime.runs.claim_run(
                        candidate["tenant_id"],
                        candidate["run_id"],
                        self.worker_id,
                        lease_seconds=LEASE_SECONDS,
                    )
                    if claim is None:
                        continue
                    await self._execute(claim)
                    return True
                finally:
                    await self._unlock_thread(lock_connection, str(candidate["thread_id"]))
        return False

    async def run_forever(self, stop: asyncio.Event) -> None:
        publisher = OutboxPublisher(
            self.runtime.sessions, self.runtime.redis, self.runtime.settings
        )
        rebuilt = await self.runtime.projection.rebuild_all()
        logger.info("inventory_projection_rebuilt", extra={"slots": rebuilt})
        publisher_task = asyncio.create_task(outbox_loop(publisher, stop))
        next_maintenance = asyncio.get_running_loop().time()
        next_heartbeat = next_maintenance
        try:
            while not stop.is_set():
                loop_time = asyncio.get_running_loop().time()
                if loop_time >= next_heartbeat:
                    await self._heartbeat()
                    next_heartbeat = loop_time + 15
                if loop_time >= next_maintenance:
                    await self._expire_due_holds()
                    await self._purge_tombstoned_private_cases()
                    next_maintenance = loop_time + 1
                try:
                    await self._consume_events(publisher)
                    processed = await self.run_once()
                except Exception:
                    logger.exception("agent_worker_iteration_failed")
                    processed = False
                if not processed:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=0.5)
                    except TimeoutError:
                        pass
        finally:
            stop.set()
            publisher_task.cancel()
            with suppress(asyncio.CancelledError):
                await publisher_task

    async def _consume_events(self, publisher: OutboxPublisher) -> None:
        async with self.runtime.sessions() as session:
            rows = await session.execute(
                text("""SELECT DISTINCT tenant_id,event_type FROM tripmate.outbox_event
                    WHERE status IN ('PENDING','PUBLISHED')
                      AND created_at>now()-interval '7 days' ORDER BY tenant_id,event_type""")
            )
            streams = {
                (row["tenant_id"], event_domain(row["event_type"])) for row in rows.mappings()
            }
        for tenant_id, domain in streams:
            stream = self.inbox.keys.stream(tenant_id, domain)
            if not await self.runtime.redis.exists(stream):
                replayed = await publisher.replay_stream(tenant_id, domain)
                if replayed:
                    logger.info(
                        "stream_rebuilt_from_outbox",
                        extra={"tenant_id": str(tenant_id), "domain": domain, "events": replayed},
                    )
            await self.inbox.claim_pending(tenant_id, domain)
            await self.inbox.consume_once(tenant_id, domain, block_ms=1)

    async def _handle_event(self, session: Any, event: EventEnvelope) -> None:
        if event.event_type.startswith("reservation."):
            reservation_id = event.payload.get("reservation_id")
            if reservation_id is None:
                raise DomainError("INVALID_EVENT", "Reservation event has no reservation id", 422)
            row_result = await session.execute(
                text("""SELECT r.reservation_id,r.slot_id,r.status,r.hold_token_hash,r.hold_expires_at,
                    i.total_quantity,i.available_quantity,i.held_quantity,i.confirmed_quantity,
                    i.status AS inventory_status,i.reconciliation_generation,i.version,i.updated_at
                    FROM tripmate.reservation r JOIN tripmate.inventory i
                      ON i.tenant_id=r.tenant_id AND i.slot_id=r.slot_id
                    WHERE r.tenant_id=:tenant AND r.reservation_id=:reservation"""),
                {"tenant": event.tenant_id, "reservation": reservation_id},
            )
            row = row_result.mappings().one_or_none()
            if row is None:
                return
            snapshot = {
                "total": row["total_quantity"],
                "available": row["available_quantity"],
                "held": row["held_quantity"],
                "confirmed": row["confirmed_quantity"],
                "status": row["inventory_status"],
                "generation": row["reconciliation_generation"],
                "version": row["version"],
                "updated_at": row["updated_at"].isoformat(),
            }
            hold = None
            if row["status"] == "HELD":
                hold = {
                    "token_hash": row["hold_token_hash"],
                    "expires_at": row["hold_expires_at"].timestamp(),
                    "ttl_seconds": max(
                        1, int((row["hold_expires_at"] - self.runtime.reservations.clock.now()).total_seconds())
                    ),
                }
            await self.runtime.projection.project(
                tenant_id=event.tenant_id,
                slot_id=row["slot_id"],
                reservation_id=row["reservation_id"],
                snapshot=snapshot,
                hold=hold,
            )
        elif event.event_type.startswith("agent.run."):
            run_id = event.payload.get("run_id") or str(event.aggregate_id)
            exists = await session.scalar(
                text("SELECT EXISTS(SELECT 1 FROM tripmate.agent_run WHERE tenant_id=:tenant AND run_id=:run)"),
                {"tenant": event.tenant_id, "run": run_id},
            )
            if not exists:
                raise DomainError("RUN_NOT_FOUND", "Run event references no persisted run", 404)

    async def _expire_due_holds(self, *, limit: int = 100) -> int:
        async with self.runtime.sessions() as session:
            rows = await session.execute(
                text("""SELECT tenant_id,reservation_id FROM tripmate.reservation
                    WHERE status='HELD' AND hold_expires_at<=now()
                    ORDER BY hold_expires_at,reservation_id LIMIT :limit"""),
                {"limit": limit},
            )
            due = [(row["tenant_id"], row["reservation_id"]) for row in rows.mappings()]
        expired = 0
        for tenant_id, reservation_id in due:
            expired += await self.runtime.reservations.expire(tenant_id, reservation_id)
        return expired

    async def _purge_tombstoned_private_cases(self, *, limit: int = 1000) -> int:
        async with self.runtime.sessions() as session, session.begin():
            result = await session.execute(
                text("""DELETE FROM tripmate.private_case_vector
                    WHERE vector_id IN (
                        SELECT vector_id FROM tripmate.private_case_vector
                        WHERE tombstoned_at IS NOT NULL
                        ORDER BY tombstoned_at,vector_id FOR UPDATE SKIP LOCKED LIMIT :limit
                    ) RETURNING vector_id"""),
                {"limit": limit},
            )
            return len(result.all())

    async def _execute(self, claim: dict[str, Any]) -> None:
        tenant_id = claim["tenant_id"]
        run_id = claim["run_id"]
        fence = int(claim["fencing_version"])
        lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(self._lease_heartbeat(tenant_id, run_id, fence, lost))
        graph_task = asyncio.create_task(self._invoke_graph(claim))
        lost_task = asyncio.create_task(lost.wait())
        try:
            done, _pending = await asyncio.wait(
                {graph_task, lost_task},
                timeout=300,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if graph_task in done:
                output = graph_task.result()
            elif lost_task in done:
                graph_task.cancel()
                await asyncio.gather(graph_task, return_exceptions=True)
                raise DomainError("STALE_LEASE", "Run lease was lost during execution", 409, True)
            else:
                graph_task.cancel()
                await asyncio.gather(graph_task, return_exceptions=True)
                raise DomainError(
                    "AGENT_TIMEOUT", "Agent run exceeded its execution deadline", 504, True
                )
            await self._persist_result(claim, output)
        except DomainError as exc:
            if exc.code != "STALE_LEASE":
                await self.runtime.runs.fail_run(tenant_id, run_id, self.worker_id, fence, exc.code)
            logger.warning(
                "agent_run_failed",
                extra={"tenant_id": str(tenant_id), "run_id": str(run_id), "code": exc.code},
            )
        except Exception:
            await self.runtime.runs.fail_run(
                tenant_id, run_id, self.worker_id, fence, "AGENT_EXECUTION_FAILED"
            )
            logger.exception(
                "agent_run_failed",
                extra={"tenant_id": str(tenant_id), "run_id": str(run_id)},
            )
        finally:
            heartbeat_task.cancel()
            lost_task.cancel()
            await asyncio.gather(heartbeat_task, lost_task, return_exceptions=True)

    async def _invoke_graph(self, claim: dict[str, Any]) -> dict[str, Any]:
        config: RunnableConfig = {
            "configurable": {"thread_id": claim["thread_id"]},
            "recursion_limit": 50,
        }
        snapshot = await self.graph.aget_state(config)
        if not snapshot.values:
            initial = {
                "schema_version": 1,
                "run_id": claim["run_id"],
                "tenant_id": claim["tenant_id"],
                "user_id": claim["user_id"],
                "thread_id": claim["thread_id"],
                "trip_id": claim["trip_id"],
                "lease_owner": self.worker_id,
                "fencing_version": int(claim["fencing_version"]),
            }
            output = await self.graph.ainvoke(initial, config)
            return output if isinstance(output, dict) else {}

        await self.graph.aupdate_state(
            config,
            {
                "lease_owner": self.worker_id,
                "fencing_version": int(claim["fencing_version"]),
            },
        )
        if not snapshot.next:
            return dict(snapshot.values)
        interrupted = any(getattr(task, "interrupts", ()) for task in snapshot.tasks)
        resume = claim.get("resume_command")
        if isinstance(resume, str):
            resume = json.loads(resume)
        if interrupted:
            if not isinstance(resume, dict):
                raise DomainError(
                    "CHECKPOINT_SCHEMA_MISMATCH",
                    "Run has a pending interrupt but no persisted resume command",
                    409,
                )
            command: Any = Command(resume=resume)
        else:
            command = None
        output = await self.graph.ainvoke(command, config)
        return output if isinstance(output, dict) else {}

    async def _persist_result(self, claim: dict[str, Any], output: dict[str, Any]) -> None:
        tenant_id = claim["tenant_id"]
        run_id = claim["run_id"]
        fence = int(claim["fencing_version"])
        interrupted = bool(output.get("__interrupt__"))
        summary = {
            key: output.get(key)
            for key in (
                "result",
                "trip_plan",
                "constraint_result",
                "citations",
                "pending_actions",
                "approval_response",
                "errors",
            )
            if output.get(key) is not None
        }
        stored = json.loads(json.dumps(summary, default=str))
        if interrupted:
            updated = await self.runtime.runs.release_waiting_run(
                tenant_id, run_id, self.worker_id, fence, stored
            )
        else:
            if "result" not in stored:
                stored["result"] = stored
            updated = await self.runtime.runs.finish_run(
                tenant_id, run_id, self.worker_id, fence, stored
            )
        if not updated:
            raise DomainError("STALE_LEASE", "Run result was fenced by a newer worker", 409, True)

    async def _lease_heartbeat(
        self, tenant_id: Any, run_id: Any, fence: int, lost: asyncio.Event
    ) -> None:
        while not lost.is_set():
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                renewed = await self.runtime.runs.renew_run_lease(
                    tenant_id,
                    run_id,
                    self.worker_id,
                    fence,
                    lease_seconds=LEASE_SECONDS,
                )
            except Exception:
                logger.exception("agent_run_lease_renewal_failed")
                lost.set()
                return
            if not renewed:
                lost.set()
                return

    async def _heartbeat(self) -> None:
        async with self.runtime.sessions() as session, session.begin():
            await session.execute(
                text("""INSERT INTO tripmate.worker_heartbeat(worker_name,instance_id,heartbeat_at,details)
                    VALUES('agent-worker',:instance,now(),CAST(:details AS jsonb))
                    ON CONFLICT(worker_name) DO UPDATE SET instance_id=EXCLUDED.instance_id,
                        heartbeat_at=EXCLUDED.heartbeat_at,details=EXCLUDED.details"""),
                {
                    "instance": self.worker_id,
                    "details": json.dumps({"host": socket.gethostname(), "pid": os.getpid()}),
                },
            )

    @staticmethod
    async def _try_lock_thread(connection: AsyncConnection, thread_id: str) -> bool:
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(hashtextextended(:thread_id, 0))"),
            {"thread_id": thread_id},
        )
        await connection.commit()
        return bool(acquired)

    @staticmethod
    async def _unlock_thread(connection: AsyncConnection, thread_id: str) -> None:
        try:
            await connection.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:thread_id, 0))"),
                {"thread_id": thread_id},
            )
            await connection.commit()
        except Exception:
            logger.exception("agent_thread_lock_release_failed")


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    runtime = await Runtime.create()
    stop = asyncio.Event()
    worker = RunWorker(runtime)
    try:
        await worker.run_forever(stop)
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())

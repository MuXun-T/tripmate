# Implementation Notes

## Design Conflict Log

| Conflict | Impact | Resolution |
|---|---|---|
| `architecture-review.md` sections 1-8 contain pre-revision designs, while section 9 and the v0.2 requirements freeze PostgreSQL-authoritative inventory, persisted approval, run outbox scheduling, and tenant-scoped constraints. | Following an earlier section could make Redis a reservation authority or replay side effects after graph resume. | Treat the v0.2 requirements and review section 9 as gates. Earlier review sections are retained as problem history only. |
| Design prose names the saver distribution `langgraph-redis`; the configured package index exposes `langgraph-checkpoint-redis==0.5.2`, while `langgraph-redis` has no matching distribution. | A literal dependency name cannot be installed or tested. | Pin the published `langgraph-checkpoint-redis` distribution and test its `RedisSaver.setup()`, thread configuration, and resume behavior against Redis Stack before claiming the saver gate passes. |
| Python 3.12 is the required runtime, but the current host only has Python 3.11 and has no Docker, PostgreSQL client, or Redis CLI. | Full-stack tests and the required interpreter cannot run on this host. | Keep deployment metadata on Python 3.12; run host-independent tests with the available interpreter where compatible, and retain Compose-backed real-service tests as explicit unrun gates. |
| The pinned mypy 1.16.1 crashes inside `langsmith._openapi_client` while traversing `TypeGuardedType`. | The static type gate cannot distinguish project errors from the checker failure. | Upgrade the development pin to mypy 1.20.2, then fix the project diagnostics it reports; `mypy app` passes with this pin. |
| Idempotency rows are retained until expiry cleanup, while the primary key continues to reserve the complete request scope after `expires_at`. | A valid request reusing an expired key could commit its business work and then fail the idempotency insert with a primary-key violation. | Under the existing complete-scope advisory lock, delete only the expired matching row in the same transaction before storing the new result; unknown-commit recovery ignores expired rows. |

## Work Checklist

- [x] Re-inspect the current workspace, project guidance, source, locks, migrations, Compose and tests. The directory has no Git metadata or `AGENTS.md`; existing project files are treated as user content.
- [x] Implement foundation: config, request context, error model, clock/ID ports, OpenTelemetry setup and migration runner.
- [x] Implement domain and PostgreSQL schema for tenant-safe catalog/trips/inventory/reservations/actions/runs/idempotency/audit/outbox/inbox.
- [x] Implement hard constraints, deterministic cancellation, reservation lifecycle, idempotency and commit-unknown recovery.
- [x] Implement Redis projection/rebuild, Lua coordination, Streams, outbox replay, inbox fencing, pending recovery and DLQ paths.
- [x] Implement LangChain gateway, LangGraph approval/resume, Redis saver integration, run leases, citations and real-time tool boundaries.
- [x] Implement tenant/user-scoped short-term conversation memory with RedisJSON CAS/TTL and PostgreSQL rebuild, and connect it to run planning.
- [x] Implement fixed-case replay persistence and evaluation of intent, tool sequence, constraints, citations and reservation status; reject cases with no valid expectations.
- [x] Implement FastAPI routes/OpenAPI, privacy deletion, health checks, Compose, fixtures and local verification commands.
- [ ] Run the required Python 3.12 Compose stack and real PostgreSQL/Redis Stack contracts. The current host has Python 3.11.9 and no Docker/database/Redis clients; code and integration contracts exist, but these runtime gates remain unverified.
- [ ] Run production-like concurrency, performance and RAG quality measurements. No such results are claimed.

## Validation Snapshot

Executed with the project virtual environment on Python 3.11.9:

- `python -m compileall -q app tests`, `ruff check app tests`, and `mypy app` (mypy 1.20.2): passed; mypy checked 50 source files.
- `pytest -q -rs`: 12 passed, 13 skipped. The integration gate was not enabled; real PostgreSQL and Redis Stack tests remain unrun.
- OpenAPI import generated 16 paths; the OpenAPI contract tests passed. `pip check` passed, and `pip install --dry-run --require-hashes --ignore-requires-python -r requirements-dev.lock` resolved successfully.
- PyYAML parsed `compose.yaml`. Docker is not installed, so `docker compose config --quiet` and Compose-backed services could not run.
- Python 3.12 execution, PostgreSQL migration/application and transaction tests, RedisJSON Lua/TTL and Stream/checkpointer tests, full recovery/concurrency integration, and measured performance/RAG quality remain unverified.

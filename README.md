# TripMate Agent

TripMate is an async FastAPI service for itinerary planning and reservation workflows. PostgreSQL is the authority for inventory, price snapshots, reservations, actions, and run state. Redis Stack holds rebuildable coordination projections, streams, and LangGraph checkpoints.

## Local setup

Requirements: Docker Compose and Python 3.12. On this workstation, the project-local environment is `.venv312`, backed by `.tools/python`.

Before running host-side Python commands in PowerShell, keep pip's cache and temporary files in the project and select the E-drive interpreter:

```powershell
$env:PIP_CACHE_DIR = Join-Path $PWD '.cache\pip'
$env:TEMP = Join-Path $PWD '.deps-tmp'
$env:TMP = $env:TEMP
$python = Join-Path $PWD '.venv312\Scripts\python.exe'
```

```powershell
Copy-Item .env.example .env
docker compose up --build
```

The Compose `init` service applies PostgreSQL migrations and deterministic demonstration data before the API and worker start. The API is available at `http://localhost:8000`; OpenAPI is at `/docs`. Requests require a signed bearer token. For local testing, issue a token for the seeded demo principal with `python -m app.security.dev_token` inside the API container.

Apply migrations and deterministic demonstration data manually when running outside Compose:

```powershell
docker compose exec api python -m app.persistence.postgres.migrate
docker compose exec api python -m app.persistence.postgres.seed
```

Replay the seeded fixed evaluation case while the Compose worker is running:

```powershell
docker compose exec api python -m app.evaluation.replay
```

The command starts a normal idempotent AgentRun, waits for its terminal result, stores the tool/constraint/citation/order report in PostgreSQL, and exits nonzero unless the case passes. Pass `--tenant-id`, `--case-id`, and `--timeout` to select another case or wait limit.

Run the host-independent checks with Python 3.12:

```powershell
& $python -m pip install --require-hashes -r requirements-dev.lock
& $python -m pip install --no-deps -e .
& $python -m pytest
& $python -m ruff check app tests
```

Refresh the pinned locks with `uv pip compile pyproject.toml --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --generate-hashes -o requirements.lock`. Generate the host-specific development lock with `uv pip compile pyproject.toml --extra dev --python-version 3.12 --generate-hashes -o requirements-dev.lock`. The runtime lock targets the Linux Compose image; the development lock targets the local platform.

Run the PostgreSQL and Redis Stack contract tests against the Compose services:

```powershell
$env:TRIPMATE_INTEGRATION=1
python -m pytest -m "postgres or redis_stack or integration"
```

`STREAM_AUTO_TRIM` defaults to `false`. Do not enable trimming until every consumer group's safe watermark and the outbox replay window have been verified in the deployment environment. Performance and retrieval quality remain measurements, not inferred guarantees.

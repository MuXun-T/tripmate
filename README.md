# TripMate Agent

TripMate 是一个面向实习项目展示的本地 Agent 工作流原型：用户用自然语言提出旅行需求，系统经过意图识别、偏好提取、工具调用和约束校验，生成可比较的行程方案；所有预约类副作用都先生成待确认动作，只有用户明确确认后才会改变预约状态。

## 架构总览

```text
客户端
  │ signed bearer + tenant/user context
  ▼
FastAPI API ── ToolGateway 信任边界 ── 只读查询 / 待确认动作
  │                                      │
  │                                      └─ 角色授权、严格输入、限流、超时、审计
  ▼
AgentRun + LangGraph Worker
  ├─ 意图识别 → 槽位排序 → 工具选择 → 多方案规划 → 约束校验
  ├─ PostgreSQL checkpoint / lease / CAS / idempotency
  └─ Run events → SSE（支持 Last-Event-ID 续接）
  │
  ├─ PostgreSQL：库存、价格快照、预约、动作、事件和 Run 的权威事实
  ├─ Redis Stack：可重建库存投影、Streams、watermark、限流和向量索引
  └─ Outbox / Inbox：发布重放、租约抢占、失败记录、DLQ 和租户隔离
```

## 模块能力

| 模块 | 主要实现 | 能力重点 |
| --- | --- | --- |
| API 与权限 | `app/api.py`, `app/domain/authorization.py` | 统一请求上下文、租户隔离、签名 Bearer、幂等键、版本 CAS、OpenAPI 与 readiness |
| ToolGateway | `app/tools/gateway.py` | 工具白名单、严格 Pydantic 输入、角色授权、scope 校验、限流、超时和审计；副作用工具只产生待确认动作 |
| Agent Run | `app/agent/graph.py`, `runs.py`, `worker.py` | LangGraph 工作流、可选 DeepSeek 分类器、本地规则回退、worker lease、checkpoint schema 校验、重复 resume 防护和 SSE 恢复 |
| 行程与规划 | `app/trips/`, `app/domain/constraints.py` | 多方案比较、软偏好评分、UNKNOWN 状态、重规划 diff，以及对已确认预约的时间/价格/版本快照保护 |
| 预约与库存 | `app/reservation/`, `app/inventory/` | PostgreSQL 权威库存事实、Redis 可重建投影、hold/confirm/cancel/expire CAS、对账、候补晋级和确认 token |
| 可靠事件 | `app/events/` | Outbox replay、Inbox lease/fencing、consumer watermark、失败记录、DLQ、Stream scope 和事件恢复 |
| 知识检索 | `app/knowledge/`, `knowledge/sources/` | 固定 curated 来源、版本 manifest、active alias、切换/回滚、删除 tombstone、租户隔离、Redis HNSW/PG fallback、引用定位与离线评估 |
| 偏好与提醒 | `app/memory/`, `app/reminders/` | 偏好和私有案例隔离/删除、站内通知 inbox、候补再次确认、失败重试；不发送邮件、短信或 push |
| 观测与验收 | `/health/*`, `/v1/operations/*` | 活性/就绪检查、故障与指标接口、迁移版本、worker 心跳、RAG alias、Redis 脚本和 checkpoint 索引检查 |

## 关键设计取舍

- **事实与投影分离**：预约、库存、事件和 Run 状态以 PostgreSQL 为准，Redis 数据可以从事实重建；投影失效不会被当作业务事实。
- **显式确认才能产生副作用**：规划阶段只返回方案和动作，确认时再次校验库存、价格、活动版本、预约版本和确认 token。
- **可恢复而非只追求一次执行**：Run 使用 lease 和 checkpoint，事件使用 outbox/inbox、watermark 与失败记录，SSE 可从 `Last-Event-ID` 续接。
- **信任边界在工具层**：所有 Agent 工具都经过白名单、输入校验、scope/角色检查、超时与审计，避免把模型输出直接当成授权操作。
- **本地 MVP 范围**：库存为本地模拟数据，路线时间为 Haversine 估算，提醒为站内 inbox，知识只来自仓库内固定 curated 文件；不包含真实支付、第三方库存、邮件短信或导航服务。

## 当前验证证据

本地 Python 3.12 验证已覆盖单元/API 合同、权限、恢复、规划、知识索引和 OpenAPI；固定知识数据集另有 `recall@5`、`MRR`、引用正确性和可定位性报告。PostgreSQL/Redis Stack 集成测试只应在专用 Compose 项目、数据库、Redis namespace、端口和 project-local volume 全部确认隔离后运行，不能使用本地 `.runtime` 数据。

详细实现范围、迁移和真实验证记录见 [`docs/implementation-notes.md`](docs/implementation-notes.md)，路线图见 [`docs/repair-roadmap.md`](docs/repair-roadmap.md)。

## Local setup

Requirements: Docker Compose and Python 3.12. On this workstation, the project-local environment is `.venv312`, backed by `.tools/python`.

Before running host-side Python commands in PowerShell, keep pip's cache and temporary files in the project and select the E-drive interpreter:

```powershell
$env:PIP_CACHE_DIR = Join-Path $PWD '.cache\pip'
$env:UV_CACHE_DIR = Join-Path $PWD '.cache\uv'
$env:TEMP = Join-Path $PWD '.deps-tmp'
$env:TMP = $env:TEMP
$python = Join-Path $PWD '.venv312\Scripts\python.exe'
```

To enable DeepSeek intent classification, set `LLM_API_KEY` in `.env` before starting Compose. The defaults are `https://api.deepseek.com` and `deepseek-flash`; without a key the worker uses its local rule-based classifier. Compose passes the key only to the worker.

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

The MVP keeps all side effects behind explicit confirmation. Reservations use PostgreSQL facts and confirmation tokens; Redis is a rebuildable projection. A sold-out activity can be put on the local waitlist, and the worker later creates a versioned promotion proposal. `GET /v1/waitlist/promotions` only reads proposals; `POST /v1/waitlist/promotions/{promotion_id}/confirm` requires the proposal token and an `Idempotency-Key`. Price, activity version, inventory version, and reservation version are checked again before confirmation.

Reminders are simulated in the in-app notification inbox. They do not send email, SMS, push notifications, or calls to an external inventory provider. Route durations are Haversine estimates, and knowledge sources are the fixed curated files under `knowledge/`.

`GET /v1/knowledge/search` returns citation metadata plus the active index version, tenant/deletion filters, retrieval scope and elapsed time. The fixed evaluator reports `recall@k`/`ndcg@k`; offline memory ranking and persisted manifest checks are labeled separately.

Run the host-independent checks with Python 3.12:

```powershell
& $python -m pip install --require-hashes -r requirements-dev.lock
& $python -m pip install --no-deps -e .
& $python -m pytest
& $python -m ruff check app tests
& $python -m mypy app
& $python -m app.knowledge.evaluation --dataset knowledge/evaluation/travelplanner-validation.json --top-k 5 --output knowledge/evaluation/travelplanner-validation.report.json
```

The knowledge command evaluates the pinned fixture offline; its latency fields describe in-memory ranking, not API or Redis service latency.

Refresh the pinned locks with `uv pip compile pyproject.toml --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --generate-hashes --no-python-downloads -o requirements.lock`. Generate the host-specific development lock with `uv pip compile pyproject.toml --extra dev --python-version 3.12 --generate-hashes --no-python-downloads -o requirements-dev.lock`. The runtime lock targets the Linux Compose image; the development lock targets the local platform.

Run the PostgreSQL and Redis Stack contract tests only against a dedicated test Compose project with project-local volumes, ports, database, and Redis prefix. Do not point these variables at the normal `.runtime` services:

```powershell
$env:TRIPMATE_TEST_DATABASE_URL = 'postgresql+asyncpg://.../tripmate_test'
$env:TRIPMATE_TEST_REDIS_URL = 'redis://.../test-db'
$env:TRIPMATE_INTEGRATION=1
& $python -m pytest -m "postgres or redis_stack or integration"
```

`STREAM_AUTO_TRIM` defaults to `false`. Do not enable trimming until every consumer group's safe watermark and the outbox replay window have been verified in the deployment environment. Performance and retrieval quality remain measurements, not inferred guarantees.

`/health/live` checks process liveness. `/health/ready` checks PostgreSQL, migration version, worker heartbeat, active RAG alias, the Redis Stack HNSW index, Redis projection scripts, and both LangGraph checkpoint indexes. A `503` response means at least one dependency is not ready. See [`docs/implementation-notes.md`](docs/implementation-notes.md) for the current evidence, skipped integration checks, migrations, and known limits.

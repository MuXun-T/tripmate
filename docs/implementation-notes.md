> 本文描述 v0 原型（已冻结）。v1 实施与结果见 docs/optimization-plan.md 与 reports/final/summary.md。其中 TravelPlanner 子集检索指标已证实无区分度（标题城市名规则基线即可满分），不再作为能力证据。

# 实现与验证现状

| 项目 | 内容 |
|---|---|
| 核对日期 | 2026-10-17 |
| 项目定位 | 本地实习作品，展示 Agent 设计、确认边界和可恢复流程；不包含部署或上线 |
| 证据规则 | 本文只记录当前源码、迁移和本轮实际命令结果；未运行的服务集成测试不计为通过 |

## 已实现能力

| 能力 | 当前实现与边界 | 关键位置 |
|---|---|---|
| ToolGateway 与授权 | 注册工具使用严格 Pydantic 输入；工具调用带租户/用户 scope、超时、限流、审计和稳定错误码。OWNER/EDITOR/VIEWER 矩阵由服务端判定，模型不能提供身份、价格、库存、版本或权限事实。 | `app/tools/gateway.py`、`app/domain/authorization.py`、`app/reservation/service.py` |
| Trip API | `GET /v1/trips/{trip_id}` 返回成员、item 和预约状态聚合；`POST /v1/trips/{trip_id}/confirm` 只确认方案/候选状态，不直接扣库存；幂等和版本 CAS 由服务端执行。 | `app/api.py`、`app/trips/service.py` |
| 预约与库存 | PostgreSQL 是库存和预约权威事实；hold/confirm/cancel/expire 使用版本 CAS、确认 token 和幂等记录。Redis 投影失败会写 `inventory_reconciliation`、暂停库存为 `RECONCILING`，worker 可从 PostgreSQL 重建。 | `app/reservation/service.py`、`app/inventory/redis_projection.py`、`migrations/V014__inventory_reconciliation_failures.sql` |
| Outbox/Stream | 已发布事件保留 replay cursor；inbox 保存 stream/group/message scope，只有 Redis stream 中从当前游标到目标消息全部对应 `PROCESSED/DLQ` 时才前移 group watermark；重复事件由 inbox `(event_id, consumer)` 幂等。Stream trim 默认关闭。 | `app/events/runtime.py`、`migrations/V008__event_audit.sql`、`migrations/V016__inbox_stream_scope.sql` |
| Run/checkpoint/SSE | Run 领取、续租和完成使用 fencing CAS；恢复前校验 tenant/user/thread/schema/fence/pending writes。持久化 Run event 支持 `Last-Event-ID` 续接，并按认证主体过滤。 | `app/agent/worker.py`、`app/agent/runs.py`、`app/events/run_log.py`、`app/api.py`、`migrations/V011__run_events_planning_knowledge.sql`、`migrations/V012__run_event_sse_names.sql` |
| 规划与重规划 | 软偏好评分、UNKNOWN、多个方案、候选比较和重规划 diff 已由确定性服务端逻辑约束；已确认预约的 slot、时间和价格快照不会被重规划覆盖。路线为 Haversine 估算，天气/开放时段为本地 adapter。 | `app/agent/graph.py`、`app/domain/constraints.py`、`app/trips/service.py`、`app/tools/realtime.py` |
| 知识索引 | 仓库内 curated fixture 可构建 manifest/chunk；PostgreSQL 保存来源、manifest 和 citation 权威事实，Redis Stack HNSW 保存可重建的 384 维向量。构建失败持久化 `FAILED` 和原因，active alias 原子切换，支持 rollback、租户过滤、删除 tombstone 后不可召回和 citation metadata；HNSW 未就绪时查询回退 PostgreSQL。`/v1/knowledge/search` 返回 active version、过滤条件、耗时、向量后端和 UNKNOWN 原因；持久化 manifest 评估会二次读取 chunk hash。评估动态报告 `recall@k`/`ndcg@k`，无 UNKNOWN 样本时标 N/A。 | `app/knowledge/index.py`、`app/knowledge/redis_index.py`、`app/knowledge/retriever.py`、`app/knowledge/evaluation.py`、`knowledge/` |
| 候补 | 库存不足时创建 `WAITLISTED` 预约；库存恢复后 worker 创建带价格、活动、库存、预约版本和一次性 token 的升级提案，写站内通知。用户确认前不占用库存；事实变化、过期或重复确认返回稳定冲突/同一幂等结果。 | `app/reservation/service.py`、`app/agent/worker.py`、`app/api.py`、`migrations/V015__waitlist_promotions.sql` |
| 提醒 | 站内通知为本地模拟发送；任务状态包含 `SCHEDULED/SENDING/RETRYING/SENT/FAILED`，有 lease、attempt、指数退避、过期 SENDING 重新领取和 notification ID 幂等。 | `app/reminders/service.py`、`migrations/V013__p1_waitlist_reminders_case_vectors.sql` |
| 私有案例 | 仅从允许复用的偏好生成脱敏内容和 384 维本地向量；查询按 tenant/user/generation/tombstone 过滤。偏好删除先 tombstone 并立即停止召回，worker 再物理清理。 | `app/memory/preferences.py`、`app/knowledge/retriever.py`、`app/agent/worker.py` |
| Readiness/观测 | `/health/live` 只表示进程存活；`/health/ready` 检查 PostgreSQL、最新 migration、worker heartbeat、活动 RAG alias、Redis、Redis HNSW 索引、投影脚本和 checkpoint saver 索引。故障面板和窗口指标 API 按 tenant 返回 outbox/inbox/DLQ/Run/预约/库存/提醒状态。 | `app/api.py` |

## 迁移

本轮新增前向迁移 `V014__inventory_reconciliation_failures.sql`、`V015__waitlist_promotions.sql` 和 `V016__inbox_stream_scope.sql`。旧迁移未改写；`V015` 仅扩展站内通知以支持候补提案，并增加提案的版本、token、状态和租户/用户外键；`V016` 为 inbox 增加事件流和 consumer group 进度字段。

## 本轮验证

使用项目内 `.venv312`（Python 3.12.15）执行：

- `./.venv312/bin/pytest -q`：`51 passed, 29 skipped`。跳过项是需要 PostgreSQL/Redis 的集成用例。
- `./.venv312/bin/pytest -q tests/api tests/unit`：`49 passed`；新增 OpenAPI、checkpoint scope、watermark 顺序、提醒文本、知识评估和 Redis 向量结果解析单测均在此范围内通过。
- `./.venv312/bin/ruff check app tests`：通过。
- `./.venv312/bin/mypy app`：通过。
- `./.venv312/bin/python -m compileall -q app tests`：通过。
- `./.venv312/bin/python -m app.knowledge.evaluation --dataset knowledge/evaluation/travelplanner-validation.json --top-k 5 --output knowledge/evaluation/travelplanner-validation.report.json`：通过；固定离线集得到 `recall@5=0.9444`、`mrr=0.9028`、`citation_correctness=0.8056`、`citation_locatability=1.0`。该集合没有 UNKNOWN 样本，UNKNOWN precision/recall 为 `N/A`，延迟字段仅表示离线内存排序。
- `git diff --check`：通过。

本轮未运行会写服务数据的 PostgreSQL/Redis 集成测试，也未运行 Compose。当前没有确认专用的测试数据库、Redis namespace、Compose project、端口和 project-local volume；因此不能把迁移升级、Lua 投影、Stream replay、真实 checkpoint 恢复、候补端到端和提醒故障测试标为通过。离线固定 fixture 的 Recall/MRR 结果由知识评估单测覆盖，但没有线上检索延迟、并发、P95 或外部天气服务可靠性测量证据；这些结果不代表生产质量或服务性能。

## MVP 边界

库存、候补和提醒均为本地模拟；路线是 Haversine 估算；知识来源固定为仓库内 curated fixture；没有真实库存供应商、支付、邮件短信、导航服务或生产部署。

> 本文描述 v0 原型（已冻结）。v1 实施与结果见 docs/optimization-plan.md 与 reports/final/summary.md。

# TripMate 修复实施路线图

> 用途：按阶段补齐设计与实现差距，形成可在本机运行、可用测试证明、可用于实习作品展示的 TripMate Agent。
> 范围：本地开发和演示，不包含生产部署、托管、真实支付或真实库存交易。
> 基线：当前工作区有未提交改动和 `.runtime` 本地数据。本文件是实施计划，不代表下列任务已经完成。

## 1. 完成目标

完成时，开发者应能在隔离的本地 PostgreSQL/Redis Stack 环境中运行一条完整演示：认证用户创建行程，Agent 根据硬约束和偏好生成方案，返回可定位的知识引用；用户确认后才执行预约副作用；Run 事件能通过 SSE 续接；重复请求、worker 中断和 Redis 投影丢失均有确定的恢复行为。候补和提醒可在本地模拟闭环中演示。

完成标准：

- P0 的权限、库存事实、用户确认、幂等和恢复边界由服务端强制执行，关键失败路径有测试。
- Trip、Run、预约、知识检索、候补和提醒均有清楚的 API/调用方、持久化状态和错误行为；未接入真实外部服务的结果显式标为模拟或估算。
- RAG 能从本地固定来源构建版本、切换、回滚和删除；引用可从已发布索引和数据库往返定位；评估报告名称与实际计算一致。
- 基础单元/API/静态检查通过；涉及 PostgreSQL/Redis 的集成和故障测试只在明确隔离的测试服务中通过。
- README 提供可复制的本地启动、演示、测试和限制说明；简历陈述只使用实际通过的测试或测量结果。

## 2. 固定范围和行为约定

开始编码前冻结以下 MVP 约定，避免把本地作品扩张成生产系统：

| 能力 | MVP 约定 | 明确不作为完成条件 |
|---|---|---|
| 库存和预约 | PostgreSQL 是权威事实；Redis 是可重建协调/查询投影；使用本地演示库存 | 第三方真实库存、支付或订单履约 |
| Agent 工具 | 模型最多提出注册的只读工具 ID 和已校验参数；ToolGateway 执行；预约/取消/偏好写入均走候选动作和用户确认 | 模型直接执行副作用或自行提供价格、库存、角色等事实 |
| 地图和实时数据 | 保留 Haversine 路线估算和可替换的本地天气/营业时间 adapter；结果标注数据来源和估算性质 | 真正导航精度、地图供应商 SLA、生产天气稳定性 |
| 知识来源 | 使用仓库内可复现的 curated fixture/source adapter；PostgreSQL 保存权威 manifest，Redis Stack 承担本地向量索引 | 通用网页爬虫、未审核网络抓取 |
| 候补 | 库存恢复时创建候补升级提案；价格/活动版本变化时重新展示并要求用户确认 | 后台静默自动转成已确认预约 |
| 提醒 | 持久化的站内通知模拟发送，具备重试和失败状态 | 邮件、短信、推送供应商接入 |
| 运行方式 | 本地 Compose 和项目目录内工具/数据 | 生产部署、托管、集群运维 |

如果实现时发现设计文档与上述约定冲突，先把契约写入需求/概要设计并同步实现说明，再改代码；不能以“以后再定”默默改变 API 或数据语义。

## 3. 工作区与数据保护规则

1. 每个阶段开始先检查 `git status --short`、目标文件当前内容和相关调用方；现有修改视为用户内容，不 stash、不 reset、不覆盖、不清理。
2. 只新增前向迁移（例如 `V014__...sql`）；不得改写可能已应用的旧迁移。迁移测试使用独立、可丢弃的测试 PostgreSQL。
3. 不对当前 `.runtime`、未确认归属的 PostgreSQL/Redis 执行测试写入、迁移、清理或重置。集成测试必须显式要求独立连接地址和独立存储，并在运行前检查服务标识、数据库名、Redis key prefix/namespace。
4. 新的虚拟环境、缓存、报告、临时文件和服务数据放在项目目录。安装依赖前确认缓存和工具目录位置；不操作其他项目或用户目录。
5. 每次编辑只改该阶段所需的源码、调用方、迁移、测试和必要配置；不为路线图顺带重排其他模块。
6. 单元/API 测试优先使用 mock/fake 验证逻辑；只有验证事务、锁、Redis Lua、Stream 或 checkpoint saver 契约时才启动隔离服务。

## 4. 实施阶段

### 阶段 0：建立基线并冻结契约

**目标**：确认本路线图对应的代码仍与当前工作区一致，统一设计文档中历史记录与现状表述。

**步骤**：

1. 阅读 `AGENTS.md`、五份设计/现状文档及本路线图；检查工作区状态，保留所有已有变更。
2. 按能力从 API/调用方追踪到业务服务、数据库/Redis、worker 和测试；优先核实 `app/api.py`、`app/agent/`、`app/tools/`、`app/reservation/`、`app/inventory/`、`app/events/`、`app/knowledge/`、`app/reminders/`。
3. 对照 `docs/implementation-notes.md` 核实 SSE、Run event、RAG index、候补和提醒的真实状态；该文件若与代码不一致，先更新状态和证据，不把历史待办当作现状。
4. 将 Trip confirm、Trip GET 返回结构、OWNER/EDITOR/VIEWER 权限矩阵、模型可选择的工具边界、提醒的站内模拟语义写成稳定契约。
5. 记录当前离线测试、lint、类型检查可运行的命令和结果；若环境不具备则记录原因，不启动可能连接默认服务的测试。

**验收**：每个后续任务都有当前入口、服务、持久化点、迁移、测试路径；未决产品语义已按第 2 节冻结。此阶段不改 `.runtime` 和现有服务数据。

### 阶段 1：工具信任边界、角色授权和 Trip API（P0）

**主要位置**：`app/tools/gateway.py`、`app/agent/graph.py`、`app/api.py`、`app/trips/service.py`、`app/domain/`、`tests/security/`、`tests/api/`。

**步骤**：

1. 列出所有注册工具的输入、输出、调用节点和副作用。工具选择结果只允许引用注册表中的工具 ID；输入通过现有 Pydantic schema；越权、未知工具、无效参数、超时、限流和 provider 异常返回稳定的领域错误。
2. 用户、租户、角色和资源 scope 从认证上下文及数据库读取，不接受模型或请求体覆盖。建立 OWNER、EDITOR、VIEWER 对读取、规划、确认、取消、成员管理的矩阵，并让 Gateway/服务统一使用该判定。
3. 预算工具只使用服务端查询的价格快照和已确认 reservation 快照；模型输入不能成为金额、库存、时间、用户 ID、tenant ID、角色或版本的事实来源。
4. 为工具结果定义并校验统一输出契约。只读工具按设计做有限重试；写操作不做盲目重试，必须依赖操作幂等记录。LLM 选择失败时回到固定图路由或确定性数据库顺序，并记录可观测原因。
5. 增加 `POST /v1/trips/{trip_id}/confirm`，明确它只确认方案/候选 action，不直接扣库存；预约确认仍走 Reservation API。扩展 `GET /v1/trips/{trip_id}` 返回约定的行程聚合、成员和 item 状态，并在服务层校验 owner/member 权限。
6. 为新增或收紧的 API 更新 OpenAPI 契约和调用方，保持错误码、版本、幂等 key 的语义一致。

**验证**：单测覆盖未知工具、schema 错误、模型伪造价格/身份字段、权限矩阵和确定性 fallback；API 测试覆盖 Trip owner/member/非成员、confirm 幂等和完整 GET 响应。测试断言不能只检查“调用了 mock”，还要验证数据库事实未被模型输入改变。

### 阶段 2：库存投影补偿和事件可恢复性（P0）

**主要位置**：`app/reservation/service.py`、`app/inventory/redis_projection.py`、`app/events/runtime.py`、`app/events/run_log.py`、`app/events/schema.py`、`app/persistence/postgres/migrate.py`、`migrations/`。

**步骤**：

1. 保持预约事实只由 PostgreSQL 事务更新。投影失败时写入持久化的待对账标记/任务，包含 tenant、slot/reservation、期望版本和失败信息；不能只记日志。
2. 增加受租户范围限制的 reconciliation worker：从 PostgreSQL 权威状态重建投影、比较版本/数量，成功后清除 pending 状态，失败时保留并退避重试。处于 RECONCILIATION_REQUIRED 的对象不能再盲目 hold/release。
3. 完成 outbox 已发布事件的保留窗口和 replay cursor。Redis 丢失后按安全起点使用原 event ID 重放；Inbox 幂等保证业务副作用不重复。
4. 在每个 consumer group 连续处理并提交后更新 watermark。只有所有相关 group 超过安全水位且不在 rebuild 时才允许 trim；验证前保持 trim 关闭。
5. 覆盖 outbox FAILED、发布结果未知、Redis 重建、重复事件、claim lease/fencing、死信状态和租户 scope；修正 SSE 消费事件与 Run 状态更新次序，使事件游标可续接且状态不倒退。
6. 所有 schema 变化使用前向迁移；为空库、前一迁移快照升级和数据一致性各写隔离测试。

**验证**：隔离 PostgreSQL/Redis Stack 集成测试模拟“PG 已提交、Redis 写失败”“Stream 写成功但 publisher 未确认”“Redis 数据丢失后恢复”“两个 worker 竞争过期 lease”。断言库存守恒、单一业务结果、事件最终可见、游标单调、旧 owner 被 fencing 拒绝。不得使用现有 `.runtime` 作为测试靶场。

### 阶段 3：Checkpoint、Run worker 与审批恢复（P0）

**主要位置**：`app/agent/worker.py`、`app/agent/runs.py`、`app/agent/state.py`、`app/agent/graph.py`、`app/events/run_log.py`、`tests/integration/`。

**步骤**：

1. 恢复前校验 Run tenant/user、thread namespace、checkpoint schema version、Run/state version、interrupt 节点和 pending writes；所有 scope 以 PostgreSQL Run 为准并与 saver thread_id 对照。
2. worker 领取/续租/完成 Run 使用 lease 和 fencing version CAS；同一 `(tenant_id, thread_id)` 串行执行。lease 丢失的 worker 不得继续提交状态或副作用。
3. 对 LangGraph 从 interrupt 节点起点重跑的行为做显式设计：节点内副作用通过 action ID、request hash 和服务端幂等边界保护；不能假设 interrupt 前代码只运行一次。
4. 偏好确认、拒绝、预约确认、过期确认和重复 resume 均使用同一确定状态机。确认记录绑定 user/tenant、action、参数 hash、版本、过期时间和一次性消费；重复请求返回同一终态，不重复写偏好或预约。
5. 确认拒绝时清除/终结对应 pending action；恢复失败、checkpoint 缺失/不匹配、saver 不可用和工具超时分别进入稳定可观察的失败或可重试状态，不假装成功。
6. Run SSE 的 Last-Event-ID 查询必须按认证主体、租户和 Run scope 过滤；终态事件与 DB Run 状态一致。连接重试后不能丢失游标之后的持久化事件。

**验证**：隔离集成测试在 interrupt 后重启 worker、重复 resume、确认与拒绝并发、旧 worker lease 过期、checkpoint schema 不匹配和 pending writes 未完成时运行。断言没有未确认副作用、同一 action 最多执行一次、Run 最终状态与事件日志一致、跨用户/租户无法续接。

### 阶段 4：规划、软偏好和安全重规划（P1）

**主要位置**：`app/agent/graph.py`、`app/domain/constraints.py`、`app/tools/realtime.py`、`app/memory/preferences.py`、`app/trips/service.py`、`tests/unit/`、`tests/integration/`。

**步骤**：

1. 把计划排序拆清楚：硬约束由服务端确定性校验；步行、室内比例、游玩风格为软评分；饮食禁忌和明确排除项仍是硬过滤。评分依据、权重和 `UNKNOWN` 处理可在结果中解释。
2. 输出多个可比较方案及稳定的评分明细；比较数据来自同一事实快照，避免方案间使用不同的价格/库存版本。
3. 增加由用户修改或天气变化触发的 replan 路径，结果包含变更前后 diff、未解决约束和重规划原因；天气/路线失败按 UNKNOWN 处理，不伪造数据。
4. 重规划时把已确认 reservation 视为不可覆盖的数据库事实：保留其 slot、时间和 reservation price snapshot。若新约束与其冲突，返回明确的不可满足/需用户处理结果，不自动取消或改价。
5. 将偏好确认、拒绝、修改、删除和重放放进同一版本状态流程；确认写入后才参与后续评分。

**验证**：单测覆盖软评分方向性、权重边界、UNKNOWN、方案稳定排序、变更 diff；集成测试验证天气变化不改数据库事实，重规划保留已确认预约与价格快照，用户未确认前没有 reservation/action 副作用。

### 阶段 5：知识更新、向量检索和有效评估（P1）

**主要位置**：`app/knowledge/`、`app/api.py`、`app/persistence/postgres/`、`migrations/`、`knowledge/`、`tests/unit/test_knowledge_index.py`。

**步骤**：

1. 固定本地来源清单和 adapter 输入格式；来源记录 tenant/source/version/hash，chunk 保留 citation ID、URL、标题、文本 hash 和来源版本。将网页文本视为不可信内容。
2. 明确 build 状态机：新 manifest/chunk/vector 完整写入并验证后才切换 active alias；任一步失败保留旧 active version、记录 FAILED 原因；提供原子 rollback。
3. PostgreSQL 保存来源、manifest 和发布事实，Redis Stack HNSW 保存可重建的向量索引；写入和查询均带 tenant、active version、deleted/tombstone 过滤。对 Redis 索引缺失有明确 rebuild 路径。
4. 删除来源/租户知识时先使其不可召回，再清理 PG/Redis 相关数据；验证另一租户索引和引用没有被误删。旧版本只有在 rollback/replay 保留期结束后才清理。
5. API 返回真实的 citation metadata 和检索诊断，包括 active version、过滤条件、耗时及 UNKNOWN 原因；调用方能从 citation ID/hash 反查实际持久化 chunk。
6. 评估集至少包含可回答、不可回答、租户隔离、旧版本/删除和引用缺失案例。`required_citation_ids` 必须参与预期引用评分；`citation_locatability` 必须做实际持久化/索引往返定位，而非只在候选内存集合比较 hash。
7. `--top-k` 与报告指标名称保持一致：动态输出 `recall@k`/`ndcg@k`，或固定指标名称并拒绝不同 k。UNKNOWN precision/recall 只在有正负样本时报告；没有有效样本时标为 N/A 并解释。
8. 延迟分别测 embedding、数据库/Redis 在线检索和端到端检索；离线内存排序耗时不得命名为线上检索延迟。报告保存配置、样本数、数据版本和运行时间。

**验证**：无外部网络的固定 fixture 测试发布/失败/切换/回滚/删除/租户隔离；测试引用从 API ID 回到持久化 chunk；人为制造 required citation 缺失、UNKNOWN 样本为空、top-k 与报告名不符时，评估器应拒绝或明确标为无效，而不是输出看似有效的分数。

### 阶段 6：候补、提醒和私有案例（P1）

**主要位置**：`app/reservation/service.py`、`app/reminders/`、`app/memory/preferences.py`、`app/knowledge/retriever.py`、相关 migration、API 和 worker 测试。

**步骤**：

1. 候补从创建、库存释放检测、候补提案、用户再次确认到确认预约形成完整流程。价格、slot 或活动版本变化时重建候选并要求再次确认；重复通知/确认幂等。
2. 提醒的 due 任务持久化并可重试，明确 `PENDING/PROCESSING/SENT/RETRYING/FAILED` 等状态、attempt、lease 和下次重试时间；站内 inbox 写入以通知 ID 幂等。provider 未接入时只标注模拟发送。
3. 私有案例从已确认且允许复用的偏好/行程生成脱敏记录；向量和查询都按 tenant/user/generation/deleted 状态过滤，不将原始个人对话直接索引。
4. 用户删除偏好/案例时先写 tombstone 并即时排除召回，再由 worker 清除旧 generation 的向量；失败可恢复且不会重新被召回。

**验证**：候补覆盖库存不足、恢复、过期、价格变化、再次确认和重复消费；提醒覆盖重复 due、worker 崩溃、重试耗尽和 inbox 幂等；案例覆盖租户/用户隔离、脱敏字段、删除立即生效和异步 purge 恢复。

### 阶段 7：Readiness、契约和观测（P0/P1）

**主要位置**：`app/api.py`、`app/runtime.py`、`app/config.py`、`app/observability/`、OpenAPI 与运行文档。

**步骤**：

1. readiness 探测真实 PostgreSQL、Redis、migration version、LangGraph saver setup/索引、worker/outbox consumer、活动 RAG alias；区分 liveness 和 readiness。不能把未检查的依赖硬编码成 UP。
2. 对关键操作记录 request/run/action/event ID、租户 scope（脱敏）、延迟、重试和失败原因；日志与 tracing 不包含 bearer token、confirmation token、完整私有偏好或敏感 prompt。
3. OpenAPI 契约覆盖 Run 创建/状态/事件流、Trip、预约/取消、偏好确认/拒绝、知识查询、候补和提醒可用接口；统一 401/403/404/409/422/5xx 行为。
4. 性能值只由固定机器、固定数据量、隔离服务的基准产生；未测并发/P95 时，文档和简历不得声称达到目标。

**验证**：依赖健康/不健康的 API 测试、saver 缺索引、迁移版本落后、RAG alias 缺失、日志脱敏和 OpenAPI schema 测试。健康检查不能仅检查端口可连，还要验证本应用实际依赖的能力。

### 阶段 8：端到端验收、演示和项目说明

**步骤**：

1. 建立独立的测试 Compose project 名称、独立 volume/端口和测试数据库；运行前显式打印并检查目标地址。不得复用默认 `.runtime` volume。
2. 执行干净初始化、迁移、seed、API/worker 启动和固定演示；演示至少包含一次偏好待确认/确认或拒绝、一次 Run SSE 续接、一次预约确认、一次幂等重试、一次知识引用定位。
3. 在隔离环境执行故障场景：Redis 投影删除后从 PostgreSQL 重建、outbox 重放、worker 在 interrupt 后重启、知识新版本构建失败后继续查询旧 active version。
4. 执行离线单测、API 测试、ruff、mypy；之后才运行被确认隔离的 PostgreSQL/Redis 集成测试。保存真实命令和结果，不把 skip 当成通过。
5. 更新 `docs/implementation-notes.md` 的状态和验证证据；更新 README 的本地启动、token、固定 demo、测试、限制和已知模拟行为。移除过时待办前先确认功能证据。
6. 最后检查 `git diff --check`、migration 列表、OpenAPI、无意写入 `.runtime`/秘密信息，并确认所有声称的完成项都能指向实现、测试或测量报告。

## 5. 最终验收清单

- [ ] 模型不能伪造或覆盖租户、用户、角色、库存、价格、日期事实和订单版本。
- [ ] 未确认的预约、取消和偏好写入没有副作用；确认有主体/参数/版本/时效/幂等校验。
- [ ] PostgreSQL 库存守恒；Redis 失败可对账重建，未对账对象会阻止危险重试。
- [ ] outbox 发布、replay、consumer watermark、DLQ 和 SSE Last-Event-ID 的 scope/状态有持久化契约和隔离测试。
- [ ] Run/checkpoint 恢复校验 scope/schema/version/fencing；重复 resume 和 worker 重启不会重复副作用。
- [ ] Trip API、角色矩阵、方案比较和重规划行为有 OpenAPI/API/业务测试证据。
- [ ] 知识版本切换/rollback/删除、租户隔离、引用定位和评估指标有效性有固定数据测试。
- [ ] 候补必须由用户确认；提醒失败可观察/重试；私有案例删除后立即停止召回。
- [ ] readiness 检查真实依赖；README 可让他人在隔离的本地环境复现主链路。
- [ ] 性能、UNKNOWN 检出能力、外部服务可靠性仅在有有效样本和可复现测量时对外宣称。

## 6. 可用于简历的证据边界

完成后可基于仓库实际证据描述：以 LangGraph、FastAPI、PostgreSQL 和 Redis Stack 构建可恢复的行程规划 Agent；通过 ToolGateway、人工确认、幂等状态机和租户授权保护预约副作用；以 PostgreSQL/outbox 和可重建 Redis 投影处理异步事件与失败恢复；提供带引用定位的本地 RAG 版本发布和固定评测。

只有在对应集成测试确实执行通过后，才写“故障恢复/重放通过”；只有在有并发和延迟报告后，才写具体吞吐、P95 或 Recall/MRR 数值。不要把“已编写测试”“接口存在”或“历史报告”写成当前运行保证。

## 7. 本路线图暂不覆盖

真实供应商库存、支付、邮件/短信渠道、生产部署、集群高可用、多城市容量、导航级路线和未经授权的外部抓取均不属于本项目达到可展示 MVP 的必要工作。当前采用本地模拟/curated 数据时，必须在 API、README 和演示结果中明确标记其边界。

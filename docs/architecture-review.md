> 本文描述 v0 原型（已冻结）。v1 实施与结果见 docs/optimization-plan.md 与 reports/final/summary.md。

# TripMate 周末出游管家 Agent 架构评审报告

> 评审日期：2026-10-13（Asia/Shanghai）  
> 评审范围：`requirements-analysis.md`、`overview-design.md`、`detailed-design.md`，以及当前工作区可见的工程物料。  
> 评审性质：架构和详细设计评审，不是业务代码实现。

> 历史快照说明：本报告第 1-9 节记录 2026-10-13 的设计评审过程；其中“没有源码、测试或运行物料”等陈述只适用于当日。2026-10-15 的代码实现核对和测试结果见第 10 节及 `docs/implementation-notes.md`。

## 1. 评审结论（修订前基线）

以下结论记录本报告首次评审时的设计状态，后续修订结果见第 9 节。首次评审结论为：**暂不能进入业务编码阶段**。当时只能进入设计修订、数据库/Redis 契约原型和测试夹具准备阶段；需要先完成 AR-BLK-001 至 AR-BLK-003 的设计修订。

阻断原因如下：

1. Redis Lua hold 只更新 Redis，PostgreSQL 的 `inventory` 在 hold 阶段没有同步 `available/held`；confirm 又直接执行 `held--/confirmed++`。该流程在库存为零时会产生数据库约束失败或 Redis/数据库事实分裂。confirm 和 expiry 的现有顺序还允许“数据库已确认、Redis 已释放”。
2. 设计承诺恢复时从 `reservation_execute` 开始而不重跑 interrupt 前代码，但锁定版本的 LangGraph 官方源码明确规定：恢复从包含 `interrupt()` 的节点起点重新执行该节点的全部逻辑。当前 `approval_required` 在 interrupt 前创建 action，恢复可能重复创建或重复执行节点副作用。
3. 202 响应后的 Run 没有明确的持久化调度事件、租约和同一 `thread_id` 的串行化机制。API 进程在返回 202 后退出，或两个不同消息同时更新同一 thread，都可能留下 RUNNING 但无人执行或互相覆盖的 Run。

截至 2026-10-13，评审工作区只有设计 Markdown 文档，没有可运行实现；当时引用的测试文件名是计划路径。此段是历史记录，不描述当前工作区。

严重程度定义：`阻断` 表示违反 P0 正确性/恢复约束，修订前不能编码；`高` 表示可能越权、错误订单或不可恢复，需要在 MVP 编码前修订；`中` 表示契约或实现缺口，需要在对应模块编码前冻结；`低` 表示可选优化或压测假设。

## 2. GitHub 核查证据

| 项目和锁定提交 | 核查文件/行 | 得到的官方或实现事实 | 对 TripMate 的适用边界 |
|---|---|---|---|
| [langgraph `afbe7e48b17f62e139b5c6337ab934549e6dc73b`](https://github.com/langchain-ai/langgraph/tree/afbe7e48b17f62e139b5c6337ab934549e6dc73b) | [`types.py#L906-L925`](https://github.com/langchain-ai/langgraph/blob/afbe7e48b17f62e139b5c6337ab934549e6dc73b/libs/langgraph/langgraph/types.py#L906-L925) | `interrupt()` 首次调用抛出可恢复中断；恢复从节点起点重新执行全部逻辑；使用 interrupt 必须启用 checkpointer。 | 这是所选 LangGraph 契约的直接约束，不是某个应用的建议。副作用必须放在可重放/幂等边界之后。 |
| 同上 | [`graph/state.py#L134-L175`](https://github.com/langchain-ai/langgraph/blob/afbe7e48b17f62e139b5c6337ab934549e6dc73b/libs/langgraph/langgraph/graph/state.py#L134-L175) | 状态 key 可用 reducer 合并并行节点的多个更新；示例使用 `Annotated[list, reducer]`。没有 reducer 的 key 使用默认单值通道。 | 不能把普通 `list` 字段当作并行合并协议；TripMate 要么定义 reducer，要么让实时查询在单个节点内合并。 |
| 同上 | [`channels/last_value.py#L20-L66`](https://github.com/langchain-ai/langgraph/blob/afbe7e48b17f62e139b5c6337ab934549e6dc73b/libs/langgraph/langgraph/channels/last_value.py#L20-L66) | `LastValue` 每个 step 最多接收一个值；多个更新会提示使用 `Annotated` key。 | 直接对应 `realtime_facts`、`completed_tool_refs` 的并行写入风险。 |
| 同上 | [`graph/state.py#L1195-L1214`](https://github.com/langchain-ai/langgraph/blob/afbe7e48b17f62e139b5c6337ab934549e6dc73b/libs/langgraph/langgraph/graph/state.py#L1195-L1214) | checkpointer 是版本化短期记忆；调用时必须提供 `configurable.thread_id`，该值用于存取 checkpoint。 | `thread_id` 必须是服务端构造并带租户/用户隔离的命名空间，不能只靠 state 中的字段。 |
| [langgraph-redis `bf189275303a0d0325bee886b312ff4fbf126e18`](https://github.com/redis-developer/langgraph-redis/tree/bf189275303a0d0325bee886b312ff4fbf126e18) | [`pyproject.toml#L2-L29`](https://github.com/redis-developer/langgraph-redis/blob/bf189275303a0d0325bee886b312ff4fbf126e18/pyproject.toml#L2-L29) | 该提交的包版本为 0.5.2，支持 Python 3.12，依赖 `langgraph>=1.0.10`、`redis>=5.2.1`。 | 只能用于冻结 TripMate 的依赖兼容矩阵，不能证明 TripMate 已经满足这些版本。 |
| 同上 | [`README.md#L40-L54`](https://github.com/redis-developer/langgraph-redis/blob/bf189275303a0d0325bee886b312ff4fbf126e18/README.md#L40-L54)、[`README.md#L127-L140`](https://github.com/redis-developer/langgraph-redis/blob/bf189275303a0d0325bee886b312ff4fbf126e18/README.md#L127-L140) | Redis < 8 需要 RedisJSON/RediSearch 模块；首次使用 Redis checkpointer 要调用 `setup()` 创建索引。 | Compose 必须锁定 Redis Stack/Redis 版本并执行 setup；当前没有 Compose 或依赖文件，尚未验证。 |
| 同上 | [`README.md#L673-L691`](https://github.com/redis-developer/langgraph-redis/blob/bf189275303a0d0325bee886b312ff4fbf126e18/README.md#L673-L691) | checkpointer 使用 checkpoint、channel values、writes 等索引；TTL 会作用于相关 checkpoint 和 writes。 | TripMate 的 checkpoint retention、pending writes 和删除流程必须围绕真实 saver 契约实现，不能只存自定义 RedisJSON。 |
| [redis-py `e81d4d6d71e7bbcbf426ebf47a2c1062bc4d9349`](https://github.com/redis/redis-py/tree/e81d4d6d71e7bbcbf426ebf47a2c1062bc4d9349) | [`core.py#L3839-L3840`](https://github.com/redis/redis-py/blob/e81d4d6d71e7bbcbf426ebf47a2c1062bc4d9349/redis/commands/core.py#L3839-L3840) | Cluster 客户端执行多 key 命令时，所有 key 必须在同一 hash slot。 | Lua 的 stock、hold、idempotency 和 expiry key 必须由同一 KeyCodec 生成并有启动检查。 |
| 同上 | [`core.py#L7218-L7290`](https://github.com/redis/redis-py/blob/e81d4d6d71e7bbcbf426ebf47a2c1062bc4d9349/redis/commands/core.py#L7218-L7290) | `XAUTOCLAIM` 用于认领 Consumer Group 的 idle pending 消息。 | 它只能转移 Stream PEL 所有权，不能代替 PostgreSQL inbox 的业务 lease/fencing。 |
| [Saleor `a31848549f5dc5843fe9e66422bf34a915559ee5`](https://github.com/saleor/saleor/tree/a31848549f5dc5843fe9e66422bf34a915559ee5) | [`reservations.py#L28-L37`](https://github.com/saleor/saleor/blob/a31848549f5dc5843fe9e66422bf34a915559ee5/saleor/warehouse/reservations.py#L28-L37)、[`#L112-L183`](https://github.com/saleor/saleor/blob/a31848549f5dc5843fe9e66422bf34a915559ee5/saleor/warehouse/reservations.py#L112-L183) | 真实电商项目在原子事务中锁定 stock，分别计算 allocation/reservation 后写入带 `reserved_until` 的 reservation；库存不足抛出明确异常。 | Saleor 是 Django/SQL 实现，不应把其服务拆分或技术栈照搬；这里只借鉴库存守恒和测试方法。 |
| 同上 | [`test_stock_reservations_management.py#L16-L37`](https://github.com/saleor/saleor/blob/a31848549f5dc5843fe9e66422bf34a915559ee5/saleor/warehouse/tests/test_stock_reservations_management.py#L16-L37)、[`#L215-L233`](https://github.com/saleor/saleor/blob/a31848549f5dc5843fe9e66422bf34a915559ee5/saleor/warehouse/tests/test_stock_reservations_management.py#L215-L233)、[`#L278-L318`](https://github.com/saleor/saleor/blob/a31848549f5dc5843fe9e66422bf34a915559ee5/saleor/warehouse/tests/test_stock_reservations_management.py#L278-L318) | 测试同时断言预占数量/过期时间、缺货失败，以及已有订单 allocation 和 reservation 对可用量的影响。 | 适用于 TripMate 的领域断言设计；不构成 TripMate 性能或分布式一致性的证据。 |

GitHub REST API 在本环境返回 403；上述资料通过 GitHub raw 文件和 commit Atom 取得并按 SHA 固定。没有从 README、Issue 或博客推导未核实的源码行为，也没有对无法读取的候选仓库虚构版本、测试或性能结果。

## 3. 按严重程度的问题清单

| 编号 | 严重程度 | 分类 | 主要关联 | 置信度 |
|---|---|---|---|---|
| AR-BLK-001 | 阻断 | 已确认设计缺陷 | FR-INV-002、FR-RES-001/002、NFR-DATA-001、NFR-REL-001/002 | 高 |
| AR-BLK-002 | 阻断 | 已确认设计缺陷 | FR-AGT-003/004、NFR-REL-002 | 高 |
| AR-BLK-003 | 阻断 | 实现细节缺失且会导致 P0 恢复失败 | FR-AGT-001/004、FR-API-001、NFR-REL-002 | 高 |
| AR-HIGH-001 | 高 | 已确认设计缺陷 | FR-USER-001、NFR-SEC-001、NFR-DATA-001 | 高 |
| AR-HIGH-002 | 高 | 已确认契约矛盾 | FR-RES-001、NFR-REL-001、NFR-SEC-001 | 高 |
| AR-HIGH-003 | 高 | 实现细节缺失 | FR-EVENT-001/002、NFR-REL-002 | 高 |
| AR-HIGH-004 | 高 | 已确认恢复缺口 | FR-EVENT-001/002、NFR-DATA-001、NFR-REL-002 | 高 |
| AR-HIGH-005 | 高 | 已确认契约缺失 | FR-AGT-001/004、NFR-REL-002 | 高 |
| AR-HIGH-006 | 高 | 实现细节缺失 | FR-AGT-003、FR-RES-001/002、NFR-SEC-001 | 中 |
| AR-HIGH-007 | 高 | 确定性规则缺失 | FR-RES-002、NFR-QUALITY-002 | 中 |
| AR-HIGH-008 | 高 | 已确认恢复缺口 | FR-INV-002、FR-RES-001、NFR-REL-001/002 | 高 |
| AR-HIGH-009 | 高 | 隐私/检索隔离字段缺失 | FR-USER-002、FR-MEM-002、NFR-SEC-001/002 | 高 |
| AR-MED-001 | 中 | 文档内部矛盾 | 多个 P0 FR | 高 |
| AR-MED-002 | 中 | 文档内部矛盾 | FR-EVENT-001/002、NFR-OBS-001 | 高 |
| AR-MED-003 | 中 | 文档/接口契约缺口 | FR-RES-001/002、FR-API-001 | 高 |
| AR-MED-004 | 中 | 实现细节缺失 | FR-EVENT-001、NFR-REL-002 | 高 |
| AR-MED-005 | 中 | 待验证部署/版本假设 | FR-KB-001/002/003、NFR-REPRO-001 | 高 |
| AR-MED-006 | 中 | 工具边界缺口 | FR-TRIP-002、FR-INV-001/002、NFR-QUALITY-002 | 高 |
| AR-MED-007 | 中 | P1 可靠性语义未定义 | FR-REM-001、NFR-REL-001 | 高 |
| AR-MED-008 | 中 | 可观测性契约矛盾 | NFR-OBS-001 | 高 |
| AR-MED-009 | 中 | Cluster 容量假设待验证 | FR-INV-002、FR-EVENT-001、NFR-PERF-001 | 高 |

### AR-BLK-001：Redis hold 与 PostgreSQL 库存状态不一致

- **定位**：`docs/overview-design.md:317-323, 350-382, 388-390`；`docs/detailed-design.md:184-196, 462-518, 723-766, 786-797`。
- **分类**：已确认设计缺陷。
- **问题与触发条件**：Lua 先把 Redis `available--/held++`，随后详细流程只 `INSERT reservation=HELD`，没有更新 PostgreSQL `inventory.available_quantity/held_quantity`。confirm 事务却执行 `held_quantity--/confirmed_quantity++`。数据库约束只保证单列非负和三项之和不超过 total，不能把两个存储中的 hold 关联起来。
- **可复现序列**：初始 PostgreSQL 为 `(total=1, available=1, held=0, confirmed=0, version=10)`，Redis 为 `available=1, held=0, version=10`。A/B 都读取 DB version 10；A Lua 成功后 Redis 变为 `available=0, held=1, version=11`，但 DB 仅插入 `HELD`，DB inventory 仍为 version 10。B 的 Lua 使用从 DB 读出的 expected_version=10 会得到 VERSION_CONFLICT；回源 DB 仍读到 10，重试仍冲突。若对账把 Redis 重建回 DB version 10，则 A 尚未过期的 hold 也可能被覆盖。A confirm 时 DB 执行 `held--/confirmed++`，`held` 变为负数并被 CHECK 拒绝，或者在缺少 CHECK 的实现中形成错误库存。若 expiry 先执行 Lua release、随后 confirm DB 提交，则 DB 为 CONFIRMED 而 Redis 已把数量放回 available，出现“已确认但库存被释放”。
- **影响**：P0 预约可能失败、重复售卖或在 Redis 重建后超卖；`Redis -> DB` 的未知提交结果也无法靠当前 `reservation` 行可靠判断。Saleor 的参考实现把 stock 锁定、现有 allocation/reservation 计算和 reservation 写入放在原子事务，并在测试中断言 reserved quantity 和过期时间，而不是只更新缓存。
- **最小修订**：冻结单一状态转换算法。推荐在 PostgreSQL 事务中锁定 `inventory` 再锁定 `reservation`：hold 时同时 `available-=qty, held+=qty`、插入 `HELD`、写 outbox；confirm 时 `held-=qty, confirmed+=qty`；expire/cancel 只在 `HELD -> EXPIRED` 或允许的取消 CAS 成功后 `available+=qty, held-=qty`。Redis Lua 保留为高并发 admission/cache，但 Lua 成功后 DB 写入超时必须先按 `(tenant, operation, idempotency_key)` 查询结果，结果未知时进入 reconciliation，不能盲目 release。expiry 必须先完成 PostgreSQL CAS，再执行带 token/代际的 Redis release；CAS 失败绝不 release。补充孤儿 Redis hold（无 DB reservation）的扫描分支。将库存不变量改为 `available + held + confirmed = total`，除非明确增加一个外部占用 bucket。
- **修订验证**：hold 成功后断言两边 `available/held` 同步；分别在 Lua 成功后、DB commit 前杀进程，在 DB commit 后丢响应，在 confirm/expiry 两侧同时执行；断言最终只能是 `CONFIRMED` 或 `EXPIRED` 之一，Redis 与 PostgreSQL 重建后数量守恒；数据库死锁/超时重试使用同一幂等键且不重复订单。
- **证据边界**：当前没有 migration 或实现可执行验证；这是由文档给出的明确事务步骤推出的高置信度缺陷。外部 Saleor 仅证明一种可测试的原子库存算法，不证明 TripMate 必须采用 Django。

### AR-BLK-002：LangGraph interrupt/resume 会重跑 interrupt 前节点代码

- **定位**：`docs/requirements-analysis.md:118-121`；`docs/overview-design.md:449-472`；`docs/detailed-design.md:991-1029`。
- **分类**：已确认设计缺陷。
- **问题与触发条件**：设计声称确认后“只从 `reservation_execute` 恢复”，并要求不重跑前置工具；但 `approval_required` 在 `interrupt` 前创建 `agent_action(PROPOSED)`、更新 Run、保存 checkpoint。锁定的 LangGraph 源码明确写明恢复从包含 `interrupt()` 的节点起点重新执行全部逻辑，并要求 checkpointer。
- **可复现序列**：Run 第一次进入 `approval_required`，写 action、CAS `RUNNING -> WAITING_CONFIRMATION`，调用 `interrupt()`。用户确认后用 `Command(resume=...)` 恢复；LangGraph 重新进入该节点。若 action insert 没有参数哈希幂等，产生第二个 action；若节点再次发布审批事件或调用前置工具，可能重复副作用。若应用自行 goto `reservation_execute`，则必须证明该跳转绕过了标准 resume 语义、确认状态已提交且 checkpoint 与 Run 版本一致。
- **影响**：不能满足 FR-AGT-004 的 P0 恢复保证；最坏情况下重复 hold/confirm，或新 action 覆盖旧审批上下文。
- **最小修订**：把 action 持久化和 `WAITING_CONFIRMATION` CAS 放在一个可重放幂等的节点中，并给 action 建立 `(tenant_id, run_id, action_fingerprint)` 唯一约束；将 interrupt 放到只读取已持久化 action 的专用节点，节点本身不写业务副作用。恢复时从该节点起点重跑也只能读取/校验，然后用已验证的 `Command` 路由到 execute/replan。所有只读工具引用也必须有结果 hash 和过期策略；不要用“从某节点继续”的自然语言承诺替代实际图边。
- **修订验证**：使用真实 Redis checkpointer 执行 interrupt/resume；断言 action 行数仍为 1、审批事件只生成一次、前置 tool call hash 序列不增加、同一 hold/订单不增加；拒绝和过期分别路由到 replan/error。
- **证据**：官方源码：[types.py#L912-L925](https://github.com/langchain-ai/langgraph/blob/afbe7e48b17f62e139b5c6337ab934549e6dc73b/libs/langgraph/langgraph/types.py#L912-L925)。置信度高。

### AR-BLK-003：202 Run 缺少持久化调度和同一 thread 的并发控制

- **定位**：`docs/detailed-design.md:1343-1350, 1414-1418, 1447-1456, 1474-1491`；`docs/overview-design.md:211-219`。
- **分类**：实现细节缺失，达到 P0 阻断。
- **问题与触发条件**：消息接口在写入 `AgentRun(RUNNING)` 后直接“启动 graph”并返回 202；文档没有 `agent_run_requested` outbox、worker lease、启动扫描或 run fencing。幂等 key 只防止同一请求重复，未防止同一 `thread_id` 的两个不同消息并行运行。
- **可复现序列**：请求 M1 创建 RUNNING 后 API 在调度 graph 前退出；重启后只能看到 RUNNING 行，设计没有说明谁会重新领取它。另一条序列是 M1/M2 使用不同幂等键同时读取同一 checkpoint，分别写入不同 `pending_actions`，后写 checkpoint 覆盖先写结果，或两个副作用 action 同时执行。
- **影响**：202 已返回但任务永久丢失，或会话状态、审批和库存操作交叉；服务重启和同 thread 并发均不能证明可恢复。
- **最小修订**：把 `agent.run.requested.v1` 与 `agent_run` 创建放在同一 PostgreSQL outbox 事务；worker 使用 `lease_owner/lease_until/fencing_version` 领取 RUN，启动时扫描可恢复状态。以 `(tenant_id, thread_id)` 建立数据库 advisory lock 或带 fencing token 的短租约；每次 checkpoint 和 Run 状态 CAS 必须带 version。明确一个 thread 是串行 mailbox 还是允许分支 checkpoint，不允许隐式 last-write-wins。
- **修订验证**：在 202 响应前后、outbox 发布前后、worker 执行中杀 API/worker；重启后最终只执行一次 graph。两个不同消息并发时，一个获得 thread lease，另一个得到稳定的排队/冲突结果；Run、checkpoint 和 action 的 version 单调递增。
- **证据边界**：LangGraph 官方要求用 `thread_id` 存取 checkpoint，但不替应用提供 HTTP 调度或业务租约；因此调度/fencing 必须由 TripMate 明确定义。

### AR-HIGH-001：数据库外键没有阻止跨租户关联

- **定位**：`docs/detailed-design.md:369-405, 407-458, 462-515, 522-567`。
- **分类**：已确认设计缺陷。
- **问题与触发条件**：`activity_slot.attraction_id`、`trip_member.trip_id`、`trip.owner_user_id`、`reservation.trip_id/user_id/slot_id` 等外键只引用单列 UUID，许多 user 字段甚至没有外键。`tenant_id` 只是各表独立字段，数据库不能阻止租户 A 的 reservation 关联租户 B 的 trip、user 或 slot。
- **可复现序列**：创建 tenant A/B；把 B 的 `trip_id` 和 A 的 `tenant_id` 插入 reservation。现有单列 FK 会通过；如果某个 repository 忘记 scope 条件，A 用户即可读取或修改 B 的行程/库存关系。
- **影响**：跨租户数据泄漏、错误扣库存和审计归属错误；仅靠 repository 约定或可选 RLS 不满足 NFR-SEC-001。
- **最小修订**：父表增加 `UNIQUE (tenant_id, <id>)`，子表使用 `(tenant_id, <id>)` 复合 FK；至少覆盖 user、trip、trip_member、attraction、activity_slot、inventory、reservation、agent_run/action、confirmation、trip_item。RLS 若启用只能作为第二层，并在每个事务中由服务端设置已验证 tenant，不接受客户端值。
- **修订验证**：每类关系执行跨租户 insert/update/delete，必须在数据库层失败；repository 与 API 再验证 403/404 不泄漏存在性。迁移测试从空库和旧 fixture 均通过。
- **证据**：文档自身要求跨租户阻断，但 SQL 结构未实现该约束。置信度高。

### AR-HIGH-002：幂等键作用域在数据库、Redis 和 API 之间矛盾

- **定位**：`docs/detailed-design.md:479-499, 630-635, 772-784, 1414-1418`；`docs/requirements-analysis.md:153-156`。
- **分类**：已确认契约矛盾。
- **问题与触发条件**：数据库唯一约束是 `(tenant_id,idempotency_key)`，服务 canonicalize 也是 `tenant_id:key`；Redis key 只有 tenant/key。API 章节又规定作用域为 `tenant+user+route+key`。Lua 伪代码发现 Redis idem key 后直接返回旧结果，没有先比较请求参数 hash。
- **可复现序列**：同一租户的用户 A 在 reservation route 使用 `K` 成功，用户 B 或 cancel route 复用 `K`。PG/Redis 可能返回 A 的历史结果，或不同参数被快速短路为第一次结果。即使客户端误用 key，服务也不能把另一个用户的 reservation 结果返回。
- **影响**：跨用户结果泄漏、错误路由复用和重复/错误状态转换；违背“同一 key、不同参数、不同接口和不同用户作用域明确”的评审要求。
- **最小修订**：定义操作级 scope，例如 `(tenant_id,user_id,route,operation,key_hash)`；数据库保存 `request_hash`、原始响应和 owner scope，Redis idem value 同时保存 hash 并在 Lua/服务端比较后才返回。Reservation 的业务去重另用 action/reservation scope，不把 HTTP message key 当业务订单 key。
- **修订验证**：同 key 同参数同用户重试返回完全相同结果；同 key 不同参数返回 `IDEMPOTENCY_KEY_REUSE`；不同用户、不同 route、不同 action 均不会读到另一作用域结果；Lua 快速命中路径也执行 hash 校验。
- **证据边界**：文档没有给出实际表/Redis adapter；冲突来自同一份详细设计的两个明确规则，置信度高。

### AR-HIGH-003：PROCESSING inbox 没有可安全认领的 lease/fencing 字段

- **定位**：`docs/detailed-design.md:556-567, 853-880`。
- **分类**：实现细节缺失。
- **问题与触发条件**：`inbox_event` 只有 `status`、`attempt_count`、`updated_at`，但消费流程要求 `PROCESSING` 超过 lease 后被恢复任务认领。没有 `lease_owner`、`lease_until` 或 claim token，无法证明旧 consumer 已停止，也无法让业务更新拒绝过期 owner。
- **可复现序列**：C1 写 `PROCESSING` 后暂停；lease 到期，C2 通过 `XAUTOCLAIM` 取得 Stream 消息并重新执行业务；C1 恢复并继续提交。两个 consumer 都可能执行外部提醒或不同投影；`(event_id,consumer)` 唯一键只能防同一 consumer 的重复记录，不能防两个 owner 并发。
- **影响**：重复副作用、错误 attempt 计数和不可解释的 DLQ；`XAUTOCLAIM` 只处理 Stream PEL，不提供 DB 业务 fencing。
- **最小修订**：增加 `lease_owner`、`lease_until`、`claim_token`、`started_at`；用带条件的 `UPDATE ... WHERE status IN (...) AND (lease_until < now() OR owner=...) RETURNING claim_token` 认领，业务状态和副作用记录必须带 claim token/CAS。外部通知仍需 provider 幂等键。
- **修订验证**：在业务事务前后暂停 C1、让 C2 认领，断言只有持有当前 token 的 consumer 能提交；旧 owner 提交得到 `LEASE_LOST`，消息保持 pending 或进入可审计重试。
- **证据**：redis-py 的 `XAUTOCLAIM` 仅定义 Stream pending 认领；不能替代上述 DB lease。置信度高。

### AR-HIGH-004：Redis 丢失后已发布 outbox 没有重建路径

- **定位**：`docs/detailed-design.md:569-584, 838-852`；`docs/overview-design.md:664-678`。
- **分类**：已确认恢复缺口。
- **问题与触发条件**：publisher 只选择 `PENDING/FAILED`，`XADD` 成功后把 outbox 标记 `PUBLISHED`。若之后 Redis 数据丢失，恢复动作只说重建 stock、memory、consumer group 和 alias，没有把 `PUBLISHED` 事件重新投递或从权威库重建投影的算法。
- **可复现序列**：DB 事务提交 outbox；publisher `XADD` 后更新 `PUBLISHED`；consumer 尚未处理时 Redis 重启且 Stream 丢失。重启 publisher 不再选择该事件，audit、notification、SSE 或 reconciliation 投影永久缺少它。
- **影响**：NFR-DATA-001 的“Redis 可重建”和 NFR-REL-002 的重启恢复不成立，且无法判断哪些通知/对账事件已处理。
- **最小修订**：为 outbox 增加可重放保留窗口和 replay watermark；Redis 重建时从该窗口重新 `XADD`，保留原 `event_id`，由 inbox 去重；或从 PostgreSQL 聚合状态生成明确的 rebuild 事件。为发布、重放、消费者 lag 和事件保留配置可观测断言，不能以 `PUBLISHED` 等于“所有派生状态已恢复”。
- **修订验证**：在 XADD 后、consumer 前删除 Redis；重建后所有必需 group 最终处理，inbox 不重复业务副作用，DB 最终状态和审计/提醒投影与无故障基线一致。
- **证据边界**：这是根据文档给出的 publisher 选择条件和故障动作推导的高置信度缺口，不把 Redis Stream 宣称为持久消息总线。

### AR-HIGH-005：AgentState 未定义并行 reducer，实时查询会丢更新或报错

- **定位**：`docs/detailed-design.md:926-950, 980-1012`；`docs/overview-design.md:478-496`。
- **分类**：已确认契约缺失。
- **问题与触发条件**：`AgentState` 的 `realtime_facts`、`route_estimates`、`errors`、`completed_tool_refs` 是普通 `TypedDict` 字段；同一设计又要求 weather/opening/inventory 并行执行并各自写结果。没有 `Annotated` reducer、唯一写入节点或合并顺序。
- **可复现序列**：三个并行任务在同一个 graph step 分别返回一个 `realtime_facts`；LangGraph 默认 `LastValue` 对同一 key 收到多个值会拒绝，或采用最后写入结果而丢掉另外两个事实。checkpoint 中的 completed tool refs 也会产生同样问题。
- **影响**：规划缺少天气/开放时间/库存事实，或 graph 在运行时失败；恢复后无法判断哪一个工具结果已完成。
- **最小修订**：要么把三个查询封装为一个节点，在单个返回值内确定性合并；要么为每个并行 key 定义带版本和去重的 reducer（按 tool name 排序），并给出 reducer 的 checkpoint/replay 语义。只读工具结果写入 `tool_call_id`、输入 hash、结果 hash 和 TTL。
- **修订验证**：并行运行多次，断言三类事实均存在且顺序稳定；重复恢复不新增 tool_call；单个工具超时只产生一个 UNKNOWN，不覆盖其他事实。
- **证据**：LangGraph 官方 [`state.py#L134-L175`](https://github.com/langchain-ai/langgraph/blob/afbe7e48b17f62e139b5c6337ab934549e6dc73b/libs/langgraph/langgraph/graph/state.py#L134-L175) 与 [`last_value.py#L20-L66`](https://github.com/langchain-ai/langgraph/blob/afbe7e48b17f62e139b5c6337ab934549e6dc73b/libs/langgraph/langgraph/channels/last_value.py#L20-L66)。置信度高。

### AR-HIGH-006：确认 token 没有完整的有效期、消费和主体绑定模型

- **定位**：`docs/requirements-analysis.md:153-156`；`docs/overview-design.md:255-256, 638-642`；`docs/detailed-design.md:506-515, 1016-1029, 1117-1128, 1321-1334`。
- **分类**：实现细节缺失。
- **问题与触发条件**：API 使用 `confirmation_token`，表中只有 `confirmation_hash`、`confirmed_at` 和 `UNIQUE(action_id)`，没有 token expiry、consumed/revoked、confirmed user/tenant 的完整约束；`agent_action` 也没有审批 deadline。详细恢复算法提到 confirmation deadline，但字段和 CAS 没有定义。
- **可复现序列**：action PROPOSED 后价格、库存版本或用户角色发生变化；旧 token 在没有明确过期字段的情况下再次调用 confirm/cancel。若实现只比较 hash 和 action 状态，过期审批可能被执行；两个并发确认请求也可能在 token 消费前同时通过应用层检查。
- **影响**：审批越权、过期确认或取消，且无法审计一次性 token 的消费结果。
- **最小修订**：增加 `action_confirmation(action_id, tenant_id, user_id, token_hash, parameters_hash, expires_at, consumed_at, status, version)`；确认事务锁定 action 并 CAS `PROPOSED -> CONFIRMED/REJECTED`，同时校验主体、参数、活动/价格版本和 `now < expires_at`。hold token、confirmation token、HTTP idempotency key 保持不同名称、哈希和生命周期。
- **修订验证**：过期 token、不同用户 token、参数 hash 改变、双确认并发各只允许一个确定结果；拒绝/过期后旧 token 永久不可执行。
- **置信度**：中高。需要最终 API 认证方案确认，但当前字段确实不足以证明需求所要求的有效期和一次性语义。

### AR-HIGH-007：退改规则仍依赖 RAG 文本，缺少可执行的版本化规则

- **定位**：`docs/requirements-analysis.md:74-80, 153-156`；`docs/detailed-design.md:768-770, 1148-1161, 1257-1259`。
- **分类**：确定性规则缺失。
- **问题与触发条件**：设计称由 `CancellationPolicyEvaluator` 确定性判断，但取消前又“读取当前 reservation 和 RAG policy citation”；没有 policy rule 表/DSL、版本、时区、effective window、价格/退款计算输入的 schema。RAG chunk 只能提供解释证据，不能作为状态转换条件。
- **可复现序列**：同一活动存在旧/新退改文档或跨时区边界；检索返回不同 chunk，服务没有固定 `policy_version` 和规则快照。两个重试请求可能得到不同可取消/退款结论。
- **影响**：取消状态和模拟退款不可重放，违背 FR-RES-002 的确定性和审计要求。
- **最小修订**：建立版本化 `cancellation_policy`/规则 JSON（适用活动、有效期、时区、状态、退款公式、policy_version），事务中保存使用的版本和输入快照；RAG 只用于展示 citation。规则缺失时返回 `UNKNOWN/POLICY_UNAVAILABLE`，不由模型解释后放行。
- **修订验证**：固定时区和边界时间的允许/拒绝/退款断言；文档更新后旧订单仍按保存的 policy_version 重放；RAG 检索结果变化不改变确定性结果。
- **置信度**：中。文档声明有 evaluator，但没有定义其可执行输入和持久化结构。

### AR-HIGH-008：幂等重试无法恢复仅返回一次的 hold token

- **定位**：`docs/detailed-design.md:630-635, 723-762, 774-784, 1337-1399, 1402-1406`。
- **分类**：已确认恢复缺口。
- **问题与触发条件**：设计要求数据库和日志只保存 token hash，hold token 只在第一次成功响应中返回；同时要求客户端在响应未知时用同一幂等键重试，并返回相同业务结果。若首次 HTTP 响应丢失，数据库/Redis 只有 token hash，`GET /reservations/{id}` 也不返回可恢复 token，客户端无法调用 confirm，只能等 expiry 释放库存。重试是否再次返回 plaintext 没有被定义，且无法由保存的 hash 重新计算。
- **可复现序列**：用户确认候选后 hold 的 Lua/PG 已成功，服务写入 token hash；网关在响应客户端前断开。客户端用同一 key 重试，服务回读 HELD，但因只保存 hash 不能重发 token；用户无法确认，slot 被占用直到过期。
- **影响**：合法成功的 hold 对用户不可恢复，造成库存闲置；若为了重试而保存 plaintext，又扩大凭证泄漏面。
- **最小修订**：优先取消把 hold token 当客户端必持秘密的设计：确认请求以已认证的 tenant/user + action/reservation + action confirmation 进行授权，服务端读取 DB token hash 与 Redis hold generation 校验；若仍需要 bearer token，则短期加密保存并按同一幂等 scope 仅向原主体重放，过期后销毁密文。明确 hold token、confirmation token、action id、Idempotency-Key 各自用途，不得复用。
- **修订验证**：模拟 hold commit 成功、HTTP response 丢失；同一主体同 key 重试后能安全继续 confirm，异主体不能恢复；日志、DB 明文字段和 Stream 不出现 token 原文；过期/已确认 hold 的旧 token 不能重放。
- **置信度**：高。根据“仅 hash 持久化 + 仅返回一次 + unknown result 重试”的设计规则直接推出。

### AR-HIGH-009：向量案例缺少 user/deleted 元数据，偏好删除不能证明不可召回

- **定位**：`docs/detailed-design.md:1189-1214, 1224-1242, 1432-1443`；`docs/overview-design.md:580-586`。
- **分类**：隐私/检索隔离字段缺失。
- **问题与触发条件**：设计允许 `RedactedCaseAdapter` 把历史 trip/case 写入 RAG，并要求 tenant/user、有效期和删除状态过滤；但 `ChunkRecord` 与 Redis Search schema 都没有 `tenant_id`、`user_id` 或 `deleted_at/tombstone_version` 字段。查询文字宣称租户 key 和 metadata 双重过滤，实际 schema 没有可过滤的 tenant field；同一 tenant 内的案例也无法按 user 删除后过滤。
- **可复现序列**：用户 A 的脱敏案例进入向量索引；用户删除偏好/历史案例，DB 写 tombstone，但异步索引删除尚未完成；用户 B 对相似问题检索时，没有 user/deleted filter，仍可能命中 A 的案例。若 active index 共用，也无法由 metadata query 拒绝跨租户 chunk。
- **影响**：偏好删除后仍可被召回，或跨用户案例信息被推荐给其他用户，违反 FR-USER-002、FR-MEM-002 和 NFR-SEC-002。
- **最小修订**：明确向量案例是仅用户私有还是 tenant 共享。私有数据必须在向量文档和索引里保存 tenant/user/deletion generation/effective expiry，查询必须过滤；删除采用 DB tombstone 先阻断检索，再异步物理删除/重建，并记录完成水位。tenant-only 的索引隔离不能替代同租户 user scope。
- **修订验证**：A/B 同 tenant、C 跨 tenant 插入可相似 fixture；分别执行删除事件在索引未完成、完成、回滚和 Redis 丢失后的检索，A 删除后任何用户查询都不能返回该 chunk；未删除 B 的数据仍可召回。
- **置信度**：高。字段清单与要求的过滤条件可直接逐项比对。

## 4. 中等严重度问题

### AR-MED-001：需求基线的 P0/P1 实施阶段互相矛盾

- **定位**：需求表 `docs/requirements-analysis.md:114-125` 将创建预约、取消、确认、interrupt/resume 和事件恢复列为 P0；同文 `:355-362` 却把“候选预约、interrupt/resume、取消流程”放入 P1。概要设计 `docs/overview-design.md:818-824` 和详细设计 `docs/detailed-design.md:1806-1812` 又把确认、取消、checkpoint resume 放回 P0。
- **分类**：文档内部矛盾。
- **触发与影响**：按第 15 节排期会在 P1 才实现 FR-RES-001/002、FR-AGT-003/004，但第 14 节验收要求 MVP 在未确认前无副作用、取消/过期遵守状态机、Stream 可恢复。团队可能错误地交付一个不能预约或不能安全恢复的“P0 MVP”。
- **最小修订**：不改写需求业务内容的前提下，冻结一张唯一的 phase mapping：P0 至少包含确认、候选 action、hold/confirm/cancel、checkpoint interrupt/resume、事件幂等/DLQ；P1 只保留长期偏好、提醒、候补、SSE、压测面板等明确增强项。
- **验证**：CI/评审脚本检查每个 P0 FR 在实施阶段、设计追踪和门禁表均为 P0；验收用例从需求第 14 节反向生成。
- **置信度**：高。

### AR-MED-002：事件 envelope 在概要和详细设计中不是同一个契约

- **定位**：`docs/overview-design.md:394-417` 与 `docs/detailed-design.md:801-824`。
- **分类**：文档内部矛盾。
- **问题**：概要 JSON 使用原始 `idempotency_key`，详细模型使用 `idempotency_key_hash`；详细要求 `producer`，概要没有；字段可空性、payload 校验和事件版本规则也没有单一来源。
- **影响**：publisher、consumer、审计和 OTel 不能共享稳定 schema，且原始 key 可能进入 Stream。重放时无法确定哪个字段是幂等作用域。
- **最小修订**：以一个版本化 `EventEnvelope` 为唯一契约，明确只传 hash、`producer`、`traceparent`/trace id、payload schema；概要图示和详细 Pydantic 模型由同一契约测试生成。
- **验证**：每种事件以 schema 校验；未知 `event_version` 入 DLQ；raw key/token 在序列化、日志和 Stream 中均不存在。
- **置信度**：高。

### AR-MED-003：API 基线缺少预约端点，概要/详细设计又新增了未追踪端点

- **定位**：需求 `docs/requirements-analysis.md:237-252` 只列 trips confirm 和 reservation cancel；概要 `docs/overview-design.md:628-646`、详细 `docs/detailed-design.md:1382-1406` 新增 `POST /v1/reservations` 和 `/confirm`。
- **分类**：文档/接口契约缺口。
- **问题与触发条件**：FR-RES-001 需要用户确认后创建，但需求 OpenAPI 基线没有创建/确认 reservation 的路径；实现者可能按需求漏掉 hold/confirm，或按详细设计实现未被验收、鉴权和错误码覆盖的接口。
- **最小修订**：在 API 契约决策中明确 `trip confirm` 是否只是 action approval，`reservation create` 是否是 hold，`reservation confirm` 是否是订单确认；把每个路径、请求/响应、错误码、幂等 scope 和权限写入同一 OpenAPI 基线及追踪矩阵。
- **验证**：OpenAPI snapshot、未确认 action 拒绝、hold/confirm/cancel 三条路径的 202/409/503/未知提交结果测试。
- **置信度**：高。

### AR-MED-004：Stream MAXLEN 和多 Consumer Group 的安全裁剪策略未定义

- **定位**：`docs/detailed-design.md:630-642, 826-880`；`docs/overview-design.md:419-433`。
- **分类**：实现细节缺失。
- **问题与触发条件**：Stream 的 `MAXLEN` 只写“由运维配置”，没有按最慢 group pending、DLQ 保留和 replay watermark 计算最小保留 ID。裁剪可能先于一个 group 的恢复或重放。
- **影响**：消息从主 Stream 消失后 `XAUTOCLAIM` 无法得到 payload；即使 PEL 仍存在，也只能看到缺失 entry，恢复依赖不存在的 outbox replay。
- **最小修订**：MVP 固定保留窗口并关闭自动裁剪，或按所有 group 的安全水位使用 `MINID`；裁剪前检查 oldest pending、DLQ 和 outbox replay window，记录 trim watermark。
- **验证**：一个 group 停止消费并让另一 group 持续写入，执行裁剪后恢复停机 group；断言 payload、attempt、DLQ 和 replay 仍可得到。
- **证据边界**：Redis/redis-py 的 `XAUTOCLAIM` 只认领 PEL，不保证被裁剪的 entry 仍有 payload。置信度高。

### AR-MED-005：RAG/Redis Stack 版本和向量格式仍是占位符

- **定位**：`docs/detailed-design.md:1180-1255, 1562-1598, 1795-1804`；`docs/overview-design.md:582-586, 834-845`。
- **分类**：待验证部署/版本假设。
- **问题与触发条件**：索引 schema 使用 `DIM <locked-dim>`，embedding model、维度、归一化、distance metric、Redis Stack 镜像、`ON HASH`/`ON JSON` 和 cluster/single-node 仍未锁定；当前没有依赖或 Compose 文件。`langgraph-redis` 锁定提交还要求 `setup()` 和 RedisJSON/RediSearch 模块。
- **影响**：P0 RAG citation、checkpoint index 和启动 readiness 无法复现；不同 Redis 版本可能在索引创建、向量编码或 checkpoint setup 时失败。
- **最小修订**：锁定 Python/Redis Stack/LangGraph/langgraph-redis/redis-py 版本，明确 FLOAT32 字节编码、维度、距离、索引 dialect 和单机部署；启动 setup 失败必须阻止相关 worker，并把 manifest 写入版本。
- **验证**：从空 Redis Stack 启动 setup、建索引、写入/查询一个 fixture、切换 alias、重启后恢复；同一 fixture 在 Python 3.12 环境可重复。
- **置信度**：高；缺少的是物料和锁定决策，不是对 Redis Search 行为的臆测。

### AR-MED-006：模型可见工具 schema 暴露了不应由模型提供的版本/金额输入

- **定位**：需求 `docs/requirements-analysis.md:212-235` 明确模型不能传入或覆盖库存版本、价格等事实；详细 `docs/detailed-design.md:1104-1121` 的 `BudgetInput.item_prices`、`ReservationProposalInput.inventory_version/activity_version` 暴露这些字段，虽在 `:1142, 1154-1161` 以文字要求服务端校验。
- **分类**：工具边界缺口。
- **问题与触发条件**：LLM 构造伪造价格或过期版本，若 adapter 直接使用 schema 值，预算和候选 action 会脱离实时工具结果；“重新校验”没有明确是忽略字段、仅作 expected value，还是拒绝。
- **最小修订**：模型侧输入只允许 `slot_id/quantity` 或 price snapshot reference；服务端从已签名工具结果/数据库读取 Money 和版本，或把版本作为不可覆盖的 context 注入。`calculate_trip_budget` 接受 snapshot IDs，不接受任意 `Money` 列表。
- **验证**：伪造价格、负/过期版本、跨 tenant snapshot 和 tool output hash 改变均被拒绝；合法工具结果计算使用 Decimal 并保存来源 hash。
- **置信度**：高。

### AR-MED-007：提醒“只发送一次”没有外部通知幂等语义

- **定位**：`docs/requirements-analysis.md:117`；`docs/detailed-design.md:882-884`；`docs/overview-design.md:658, 783`。
- **分类**：P1 可靠性语义未定义。
- **问题与触发条件**：worker 先调用发送适配器，成功后才 CAS `SCHEDULED -> SENT`。进程在外部发送成功、数据库提交前退出，lease 到期后会用相同 dedup key 再发送；文档没有要求通知 provider 支持幂等键。
- **影响**：无法承诺“重复消费不重复发送”；用户可能收到重复提醒。若 provider 不支持幂等，系统只能承诺 at-least-once。
- **最小修订**：首期使用可验证的模拟通知适配器，按 `dedup_key` 持久化幂等结果；真实 provider 必须明确 idempotency key/查询发送结果能力。对不具备该能力的渠道，API/验收改为“至少一次并可审计”。
- **验证**：在 send 成功后杀 worker、重复 claim、provider timeout/unknown result 场景，断言模拟 provider 只产生一个 message_id，或明确报告 duplicate_possible。
- **置信度**：高。

### AR-MED-008：可观测性指标同时违反和遵守高基数约束

- **定位**：`docs/detailed-design.md:1526-1558`；`docs/overview-design.md:690-696`。
- **分类**：文档内部契约矛盾。
- **问题**：详细设计说明高基数 ID 不应作为 metric label，但 `inventory_reconciliation_diff` 明确使用 `slot/tenant` 标签；`rag_retrieval_duration` 使用 `index/document_type`，随着版本和文档类型增长也可能膨胀。
- **影响**：指标后端基数和成本不可控，且将 reservation/slot ID 放入指标会掩盖真正的聚合趋势。
- **最小修订**：metric labels 限定为 operation/result/status/route/group；slot、tenant、reservation 只放 span/log hash 或 exemplar，并保留 reconciliation diff 的计数、大小分布和告警。
- **验证**：静态检查禁止 `_id`、raw key、slot、tenant 出现在 metric label；固定 tenant/slot 压测后检查时序数量上界。
- **置信度**：高。

### AR-MED-009：按 tenant hash tag 保证 Lua 同 slot 会把热门租户压到单个 Cluster slot

- **定位**：`docs/overview-design.md:289-307, 714`；`docs/detailed-design.md:628-642, 657-684`。
- **分类**：需要压测验证的部署假设，不是单机 MVP 的已确认缺陷。
- **问题与触发条件**：所有同租户库存 Lua key、tenant Stream 和其他业务 key 使用相同 `{tenant_id}` tag。这满足多 key Lua 的 same-slot 约束，但一个热门 tenant 的所有 hold、stream 和幂等操作集中在一个 Redis Cluster slot/节点；租户越热，横向扩展越不能分散该租户热点。
- **可复现/测量序列**：固定单个热门 tenant，对不同 slot 和不同 reservation_id 并发加压；记录该 tenant 所在 slot/node 的 CPU、命令队列、p99、Redis Cluster slot 分布和其他租户延迟。与 `{tenant_id:slot_id}` 分片方案比较，但每个 Lua 的 stock/hold/idem/expiry 仍必须同 tag。
- **影响**：500 并发目标可能受单 slot 限制；目前这是拓扑容量假设，不应提前宣称能靠 Cluster 扩容解决。
- **最小修订**：MVP Compose 明确单机 Redis Stack，不声称 Cluster 容量；若部署 Cluster，选择 `{tenant_id:slot_id}` 或库存 aggregate tag，并为 Stream/跨 slot 事件接受独立 key/处理流程。以真实流量分布压测后定策略，避免全局租户 hot key。
- **验证**：在固定 Redis Cluster 节点和热租户比例下，报告每个 slot 的命令/CPU 分布及 P95/P99；确认 hash tag 改动不会拆分单次 Lua KEYS。
- **置信度**：高（同 hash tag 的分布结果确定）；容量影响需压测。

## 5. 预约、库存、审批和事件故障窗口

下表区分“设计已有处理”和“仍有恢复漏洞”。它不把 Redis 的原子脚本误认为跨 PostgreSQL 的原子事务。

| 故障窗口 | 当前文档动作 | 评审结论和必须断言 |
|---|---|---|
| Lua 成功、DB INSERT/commit 前进程退出 | Redis hold 带 TTL；失败时 release；对账兜底（`overview:317-323`、`detailed:756-762`） | DB 可能没有 reservation 行，必须有 orphan hold 扫描、幂等查询和审计；不能只靠“进程失败”分支。归入 AR-BLK-001。 |
| DB commit 成功、客户端未收到响应 | 用同一 Idempotency-Key 查询/重试（`detailed:774-784,1414-1418`） | 只有在 AR-HIGH-002 的作用域和 request hash 冻结后才成立；断言返回原 HTTP 语义和同一 reservation。 |
| confirm 与 expiry 同时执行 | expiry 当前先 Lua release 后 DB CAS；confirm 只写 DB（`detailed:743-754,764-766,786-797`） | 可出现 CONFIRMED + Redis released；两者必须先在 DB 以同一锁顺序竞争，再按成功状态更新 Redis。归入 AR-BLK-001。 |
| cancel 与重复事件同时执行 | 状态 CAS、inbox `(event_id,consumer)`、最后 ACK（`detailed:768-770,853-872`） | DB CAS 能防部分重复，但 PROCESSING 没有 fencing；外部通知仍可能重复。需 AR-HIGH-003 和 AR-MED-007 的故障测试。 |
| Redis 重启/数据丢失 | DB 重建 stock/memory/consumer group/alias（`detailed:1462-1465`） | 已标记 PUBLISHED 的 outbox 不会被现有 publisher 重新选择；必须实现 replay/rebuild watermark。归入 AR-HIGH-004。 |
| 对账期间出现新 hold | 设计先暂停 slot、重建 Redis、复核后恢复（`detailed:786-797`） | 需要明确暂停是 PostgreSQL 状态和 Redis Lua 的同一代际 fencing；否则重建可能覆盖暂停前后的新 hold。至少测试“hold 与 pause/rebuild 交错”，当前为待验证。 |
| DB 死锁/事务超时 | `READ COMMITTED` + 固定锁顺序，有限抖动重试（`detailed:607-622`） | 方向正确，但 hold 的跨 Redis/DB 未知提交仍必须查询幂等结果后决定 release；需要真实 PostgreSQL 并发断言。 |
| Stream consumer 被杀死 | `XAUTOCLAIM`、attempt、DLQ、事务成功后 ACK（`detailed:853-880`） | Redis PEL 恢复不能代替 DB lease；裁剪和已发布 outbox 重建仍未定义。归入 AR-HIGH-003、AR-HIGH-004、AR-MED-004。 |
| checkpoint 损坏/TTL 到期 | 校验 schema/hash/version，失败转人工重试（`detailed:693-695,1469-1491`） | 需使用真实 `langgraph-redis` saver 的 pending writes/TTL 行为验证；不能用自定义 key 结构代替官方 setup。 |

## 6. 需求—概要—详细设计—测试追踪缺口

| 需求基线 | 概要设计 | 详细设计 | 计划测试 | 评审结论 |
|---|---|---|---|---|
| FR-INV-002/FR-RES-001/002 | Lua、CAS、对账和状态机 | 有 Lua 伪代码和事务文字，但 hold 未更新 DB inventory，expiry 顺序不安全 | `test_inventory_lua.py`、`test_reservation_idempotency.py` | 追踪存在，正确性未成立；先修 AR-BLK-001。 |
| FR-AGT-003/004 | approval/interruption、checkpoint resume | action 写入和 interrupt 顺序已写，但与官方重放语义冲突；普通 list 无 reducer | `test_approval_resume.py`、`test_replay.py` | 需要真实 checkpointer 回放，现有断言不足。 |
| FR-EVENT-001/002 | Consumer Group、pending、DLQ | 没有 DB lease/fencing 和 Redis 丢失后的已发布事件 replay | `test_event_recovery.py`、`test_event_dedup_dlq.py` | 只能验证正常路径，无法覆盖 AR-HIGH-003/004。 |
| FR-USER-001/NFR-SEC-001 | repository scope、可选 RLS | SQL 外键未带 tenant，用户/成员关系未由 DB 约束 | `test_user_scope.py`、`test_tool_boundary.py` | 应补跨租户数据库负向测试。 |
| FR-KB-001/002/003 | active alias、manifest、citation | 向量 DIM、编码、Redis ON HASH/JSON 和版本未锁定 | `test_knowledge_index.py`、`test_index_rollback.py` | 只有设计目标；无依赖/Compose 无法运行。 |
| FR-API-001/002 | API、P1 SSE、错误码 | 预约端点在设计新增，需求 API 表未列；SSE 事件保留/回放未落物料 | `test_trip_api.py`、`test_run_sse.py` | 需先冻结 OpenAPI，不能以 Markdown 表替代。 |
| NFR-PERF-001/002/003 | 500 并发、P95 目标 | 明确写成设计目标，但 `reservation_load.py` 不存在 | `reservation_load.py` | 未压测，不能宣称达标；需验证最终 DB 状态和库存守恒。 |
| NFR-OBS-001 | OTel 贯穿 HTTP/工具/Stream | 有字段和 span 名，但 metric label 与高基数规则冲突 | `test_trace_correlation.py` | 先冻结 metric cardinality，再实现 exporter。 |

文档中大量“测试名”是待实现路径，不是测试证据。当前没有单元、Lua、Repository、集成、回放、故障或安全测试可执行，故本报告不宣称任何性能、RAG 质量或并发正确率。

## 7. MVP 最小修订方案

建议保持现有 Python 3.12、FastAPI、LangChain、LangGraph、Redis Stack、PostgreSQL、OpenTelemetry、Docker Compose，不引入 Kafka、Kubernetes、Debezium 或微服务拆分。

### 7.1 编码前必须冻结的五个契约

1. **库存状态所有权**：PostgreSQL 事务同时维护 `available/held/confirmed` 和 Reservation 状态；Redis Lua 只作为协调/快速拒绝/短期 hold 索引，所有跨系统未知结果进入 reconciliation。confirm/expire/cancel 使用同一 DB 锁顺序和 CAS。
2. **审批和 Graph 恢复**：action、确认记录、有效期和参数 hash 先持久化；interrupt 节点无副作用；resume 从节点起点重跑也安全；同一 thread 有 lease/fencing，checkpoint 采用真实 Redis saver 的 setup、namespace 和 pending writes。
3. **租户和幂等边界**：复合外键/RLS、操作级幂等 scope、request hash、token 生命周期一次性冻结；API/工具不能让模型提供价格、库存版本或身份字段。
4. **事件恢复**：outbox replay window、inbox claim token/lease、Stream 保留水位、DLQ replay event 和 external notification idempotency key 一起定义。
5. **运行物料**：锁定 Python/Redis Stack/PostgreSQL/LangGraph/langgraph-redis/redis-py 版本；提供 migration、Compose healthcheck、Redis saver setup、固定 seed 和最小 CI。

### 7.2 建议的 MVP 边界

- P0 保留：用户/租户隔离、Trip/catalog、确定性约束、RAG citation、实时工具边界、PostgreSQL 权威库存/预约、用户确认、hold/confirm/cancel、幂等、基础 Stream outbox/inbox、checkpoint resume、健康/API、审计和固定回放。
- P1 推迟：长期偏好向量案例、候补自动转正、提醒真实渠道、SSE 完整事件面板、索引回滚演示、完整故障面板和 500 并发报告。P1 推迟不能改变 P0 的确认、取消和恢复语义。
- 演示部署先明确为单机 Redis Stack + PostgreSQL Compose；Redis Cluster 的 hash-tag 负载和热门租户集中问题在单独容量验证后再决定，不在 MVP 同时承诺两种拓扑。
- API、Stream worker、过期/对账 worker 可以先合并为一个受控 worker 进程；保持模块边界，不提前拆成独立微服务。

### 7.3 编码后的实现与联调门禁

以下断言是进入业务编码后必须完成的首批实现与联调门禁；未通过前不得宣称运行时正确性、恢复能力或性能达标：

1. 真实 PostgreSQL migration 下，hold/confirm/expire/cancel 的数量守恒、锁顺序、幂等和跨租户负向测试通过。
2. 真实 Redis Stack + `langgraph-redis` saver 下，interrupt/resume 重跑节点测试通过，action/tool/hold 均不重复。
3. 202 Run 的 outbox/worker lease 和同 thread 串行化故障测试通过，API 重启不丢 Run。
4. Stream pending claim、inbox fencing、DLQ replay、Redis 丢失后的 outbox replay 和安全裁剪测试通过。
5. OpenAPI、EventEnvelope、工具 schema、错误码和状态迁移生成契约测试通过。
6. 性能与 RAG 只报告固定环境实测值；500 并发、P95、Recall/MRR 在报告前均标记为目标或待验证。

## 8. 待验证事项和明确限制（截至 2026-10-13）

- 当日没有 TripMate 源码、migration、测试和 Compose，无法验证文档是否已被实现；该结论已由第 10 节更新。
- Redis Stack 单机与 Cluster 的 Vector Search、checkpoint saver、Stream trimming 和 key slot 行为必须在锁定镜像上运行契约测试；当前仅核查了上游源码/示例的适用边界。
- `langgraph-redis` 的 `0.5.2` 与 `langgraph>=1.0.10` 是核查提交的依赖事实，不等于 TripMate 的最终选型；最终版本必须写入锁定依赖并在 Python 3.12 上构建。
- 500 并发、不超卖、API/Agent P95、RAG Recall/MRR、提醒重复率都没有实测证据，不能从参考项目的测试或 README 推断。
- Saleor 的库存测试证明了 reserved-until、allocation、缺货和事务测试方法；它是 Django/SQL 项目，不能直接解释为 TripMate 应增加其他技术组件。

最终建议（修订前）：先修订 AR-BLK-001 至 AR-BLK-003 和上述契约，再交付 migration、Compose、最小 Redis/LangGraph 原型及故障测试。该建议已由第 9 节的修订结果 supersede。

## 9. 设计修订后的复核结果（2026-10-13）

本轮已按本报告的 AR-BLK-001 至 AR-BLK-003、AR-HIGH-001 至 AR-HIGH-009 和 AR-MED-001 至 AR-MED-009 修订 `requirements-analysis.md` v0.2、`overview-design.md` v0.2 和 `detailed-design.md` v0.2。文档层面已冻结以下编码契约：

- PostgreSQL 在固定 `inventory -> reservation` 锁顺序的同一事务中维护 `available/held/confirmed`、Reservation 状态、版本和 outbox，并满足 `available + held + confirmed = total`；Redis Lua 只做协调/投影，confirm、expire、cancel 先由 PostgreSQL CAS 决定，再更新 Redis；未知提交、孤儿 hold、对账 generation 和死锁重试均有明确分支。
- action 持久化、`action_confirmation` 的主体/参数 hash/有效期/一次性消费和 `WAITING_CONFIRMATION` CAS 位于幂等可重放步骤；`interrupt()` 所在节点只读已持久化 action/confirmation。标准 LangGraph resume 从该节点起点重跑仍不会重复 action、审批事件或业务工具；状态并行字段使用 reducer，checkpoint 使用真实 Redis saver、`setup()`、`configurable.thread_id` 和 pending writes 契约。
- 202 Run 与 `agent.run.requested.v1` 在同一 PostgreSQL 事务提交；worker、Run、checkpoint 和 inbox 使用 lease/fencing/version CAS；同一 `(tenant_id,thread_id)` 串行排队或返回稳定 `THREAD_BUSY`。
- 跨实体引用使用 tenant 复合唯一键/外键；幂等统一为 tenant/user/route/operation/key + request hash；action_id、HTTP 幂等键、confirmation token、hold token 和 event_id 分离。已批准 action 可由认证主体和 action/幂等结果恢复，hold token 不再是唯一恢复凭证。
- EventEnvelope、OpenAPI 和工具 schema 已统一方向；inbox claim/fencing、outbox replay watermark、按所有 Consumer Group 安全水位裁剪、DLQ replay 和外部通知幂等均已落到设计/测试门禁。RAG 用户案例包含 tenant/user、tombstone/generation，删除先阻断召回；退改由固化的 policy version/snapshot 和版本化确定性规则判断。
- MVP 拓扑明确为单机 Redis Stack + PostgreSQL Compose；Cluster 热租户容量只是待压测假设；依赖矩阵和 Compose 物料在编码前冻结，真实 migration、Redis saver、Stream replay 和并发故障测试作为实现/合入门禁。

因此，截至 2026-10-13，**设计已达到进入业务编码的文档门禁**：可以开始交付 migration、Compose、领域实现和对应契约测试。当日结论只表示设计契约已修订到可编码状态；后续实现和测试情况见第 10 节。

## 10. 当前实现核对（2026-10-15）

第 9 节的“仍没有源码”等结论已被后续实现取代。当前仓库已有应用代码、10 份 PostgreSQL migration、Compose、本地依赖锁和单元/API/集成测试；本地默认验证为 26 项通过、14 项集成用例跳过，Ruff 和 mypy 通过。该结果只覆盖实际运行的测试，不证明性能或检索质量目标。

Agent 目前具备 LangGraph 流程、结构化 LLM 意图识别、DeepSeek 对服务端候选 slot ID 的排序、会话记忆、PostgreSQL 文本检索及 citations、工具网关、硬约束校验、预约/取消/偏好候选动作、interrupt/checkpoint 和 Run worker 恢复路径。规划最多包含 6 项；步行上限、室内比例和游玩风格只作为排序提示，天气只查行程首日并作为提示，开放时段和路线估算通过 ToolGateway 查询，价格、库存、时间和预算由服务端及确定性校验决定。完整能力仍未达到目标设计：没有通用的模型驱动工具选择循环、多方案比较或修改后的重规划；没有完整步行/室内偏好评分、向量检索和知识同步流水线；偏好确认及 Agent 恢复的集成故障覆盖有限；未提供 SSE。

TripMate 的目标是本地实习作品，不需要生产部署、集群扩容或生产级容量压测。当前实现矩阵和待做顺序以 `docs/implementation-notes.md` 为准。

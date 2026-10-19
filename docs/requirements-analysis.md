> 本文描述 v0 原型（已冻结）。v1 实施与结果见 docs/optimization-plan.md 与 reports/final/summary.md。

# TripMate 周末出游管家 Agent 需求分析

| 项目 | 内容 |
|---|---|
| 项目名称 | TripMate 周末出游管家 Agent |
| 文档类型 | 软件需求规格说明书 / V 模型需求基线 |
| 文档版本 | 0.2 |
| 文档状态 | 本地实习作品的目标需求基线；当前实现状态见 `docs/implementation-notes.md` |
| 目标读者 | 开发人员、测试人员、评审人员和面试演示人员 |
| 技术路线 | Python、FastAPI、LangChain、LangGraph、Redis Stack、PostgreSQL |

## 1. 文档目的

本文定义 TripMate 的业务目标、用户场景、功能需求、数据边界、工具契约、状态约束、非功能要求和验收方法，并建立从需求到测试的 V 模型追踪关系。本文是概要设计、详细设计、编码、测试和项目演示的共同基线。

本文中的吞吐量、延迟、检索准确率和预约正确率均为首期验收目标，不能在未完成固定数据集和压测前宣称已经达到。评审修订后，本文同时冻结跨 PostgreSQL、Redis、LangGraph 和 Stream 的业务契约；实现不得用缓存、模型输出或自然语言解释替代这些契约。

本文描述目标能力，不是已完成情况清单。TripMate 是本地实习作品，重点在 Agent 设计和核心流程；不要求部署或上线。已实现、部分实现和未实现项以 `docs/implementation-notes.md` 为准。

## 2. 项目背景与目标

用户安排周末或短途旅行时，通常需要反复查询景点介绍、开放时间、预约规则、天气、交通距离和预算。多人同行时还需要协调偏好、时间和人数；热门景点在节假日可能出现库存紧张、重复提交和预约超卖问题。

TripMate 提供一个自然语言入口，将静态旅游知识、实时信息、用户偏好和预约流程组合起来，帮助用户形成可执行的行程。它不是单纯的聊天机器人，也不把模型当作库存或订单系统，而是让 Agent 负责编排和解释，让确定性服务负责时间、金额、库存、幂等和订单状态。

项目目标：

1. 支持用户用自然语言创建、修改和查询一日游或周末短途行程。
2. 结合用户偏好、天气、开放时间、距离、预算和同行人数生成可解释的行程方案。
3. 使用 RAG 检索景点说明、预约政策、退改规则和本地旅游知识，并返回可定位引用。
4. 使用 Redis 支持聊天记忆、LangGraph checkpoint、热门信息缓存、事件流、限流、提醒以及库存协调、可重建投影和过期索引；库存权威状态由 PostgreSQL 事务维护。
5. 使用人工确认、幂等键和状态机保证预约、取消等有状态操作不会被模型重复或越权执行。
6. 通过固定场景、故障注入和并发压测量化 Agent 的检索质量、记忆效果和高并发预约正确性。

## 3. 范围

### 3.1 首期范围

- 支持一个或多个演示城市的一日游、周末短途游和景点预约。
- 管理景点、活动、餐厅、开放时间、预约规则、价格和模拟库存。
- 接入公开旅游知识和天气、地理位置等开放数据；动态库存和订单使用本地模拟服务。
- 支持用户偏好记忆，例如预算、同行人群、步行接受程度、室内/室外偏好和饮食限制。
- 支持生成行程草案、约束校验、预算估算、方案比较和用户确认。
- 支持预约、取消、候补、提醒和出行后评价的闭环演示。
- 支持 Redis Streams、Consumer Group、Lua 库存投影/协调、ZSet 延迟提醒和 LangGraph 中断恢复。

### 3.2 明确不在范围内

- 不接入真实支付、真实身份证件、真实银行卡或真实第三方生产账号。
- 不保证真实景区的实时票价和库存；首期通过可复现的本地库存服务模拟。
- 不实现完整地图导航、路线规划引擎或交通预测系统；首期使用地理距离和受控路线适配器。
- 不允许模型直接调用任意 HTTP、Shell、数据库或支付接口。
- 不把向量相似度当作实时库存、价格、天气或订单状态的事实来源。
- 不在首期覆盖跨国签证、医疗安全和复杂旅行保险等高风险专业服务。

## 4. 利益相关者与角色

| 角色 | 目标 | 主要能力 |
|---|---|---|
| 出行用户 | 快速得到符合个人偏好的可执行方案 | 对话、查询、确认、取消、评价 |
| 组团发起人 | 协调多人时间、预算和偏好 | 创建共享行程、邀请成员、确认方案 |
| 运营人员 | 管理景点资料、库存和规则 | 更新知识、调整模拟库存、查看异常 |
| 系统管理员 | 保证服务、数据和权限安全 | 配置模型、限流、审计和数据保留 |
| 测试/评审人员 | 验证 Agent 质量和高并发正确性 | 回放固定案例、故障注入、压测和查看指标 |

## 5. 典型用例

### UC-01 创建周末行程

用户提出目的地、日期、同行人数、预算和偏好。Agent 读取用户记忆，检索景点和规则，查询天气与开放时间，生成至少一个可执行方案。方案必须列出时间段、地点、预计花费、交通或步行估计、约束冲突和引用来源。

### UC-02 根据实时情况调整行程

用户说“明天下雨”或“老人不想走太多路”。Agent 重新查询天气或路线约束，替换不满足条件的户外景点，保留已确认预约，并明确哪些内容发生变化。

### UC-03 查询并预约活动

用户确认某个方案后，系统查询指定日期、时段和人数的库存。预约前展示价格、退改规则和过期时间，得到用户确认后使用幂等键创建预约。库存不足时返回候补或替代方案，不得伪造成功。

### UC-04 取消或变更预约

用户提交取消或变更请求。系统先读取当前订单状态和退改规则，校验是否在允许时间内，再执行幂等操作并返回结果。已完成或已取消的订单不能被重复取消。

### UC-05 旅游政策问答

用户询问“景点几点关门”“儿童是否需要预约”“下雨能否退票”等问题。Agent 通过 RAG 返回答案、来源、文档版本和更新时间；知识库没有证据时必须说明未知并引导用户查询官方渠道。

### UC-06 使用历史偏好

用户多次出行后，Agent 可以根据已确认或明确表达的偏好推荐方案。用户可以查看、修改或删除记忆，记忆不能跨租户或跨用户泄漏。

### UC-07 节假日高并发预约

大量用户同时抢订同一活动。系统通过 PostgreSQL 固定锁序事务维护库存状态和预约状态，Redis 只提供协调、可重建投影和过期索引，保证库存不被扣成负数，不因重试产生重复预约，并能恢复未确认或超时的预占库存。

## 6. 功能需求

优先级约定：P0 为 MVP 必须完成，P1 为面试展示增强项，P2 为后续扩展。

| ID | 优先级 | 需求 | 验收要点 |
|---|---|---|---|
| FR-USER-001 | P0 | 创建和管理用户出行档案 | 用户、租户和共享行程边界明确；敏感字段脱敏保存 |
| FR-USER-002 | P0 | 保存、查看、修改和删除用户偏好 | 偏好有来源、时间和版本；删除后不得再次被检索 |
| FR-TRIP-001 | P0 | 创建行程草案 | 支持目的地、日期、人数、预算、同行类型和偏好 |
| FR-TRIP-002 | P0 | 生成结构化行程方案 | 每个时间段包含地点、开始/结束时间、依据、成本和约束状态 |
| FR-TRIP-003 | P0 | 校验硬约束和软偏好 | 不满足时间、开放时间、人数或预算硬约束的方案不得标记为可执行 |
| FR-TRIP-004 | P1 | 比较多个方案 | 能展示预算、步行量、室内比例、预约风险和主要取舍 |
| FR-TRIP-005 | P1 | 根据天气或用户修改重新规划 | 保留已确认预约，说明变更前后差异和未解决约束 |
| FR-KB-001 | P0 | 检索景点、活动和旅游政策知识 | 支持关键词、实体 ID 和元数据过滤，返回 citation |
| FR-KB-002 | P0 | 维护知识源版本 | 保存来源、URL、版本/commit、抓取时间、hash 和索引版本 |
| FR-KB-003 | P1 | 知识索引构建失败可回滚 | 新索引不完整时继续使用旧 active 版本 |
| FR-REAL-001 | P0 | 查询天气、开放时间和路线估计 | 实时工具结果带查询时间和数据来源；超时返回明确未知状态 |
| FR-REAL-002 | P0 | 区分静态知识和动态事实 | Agent 不得用 RAG 内容直接推断当前库存、价格或天气 |
| FR-INV-001 | P0 | 查询活动库存和价格 | 返回活动版本、时间段、剩余数量和价格快照 |
| FR-INV-002 | P0 | 原子预占和释放库存 | 并发下库存不为负；预占有 TTL，超时自动释放 |
| FR-RES-001 | P0 | 创建预约 | 用户确认后创建；支持幂等键和订单版本；重复请求返回同一结果 |
| FR-RES-002 | P0 | 取消预约 | 校验状态和退改时间；重复取消不产生副作用 |
| FR-RES-003 | P1 | 候补和替代方案 | 库存不足时创建候补或返回满足约束的替代活动 |
| FR-REM-001 | P1 | 创建和发送行程提醒 | 使用 ZSet 管理执行时间，重复消费不重复发送 |
| FR-AGT-001 | P0 | LangGraph 编排出游流程 | 支持意图识别、记忆加载、RAG、实时查询、规划和报告节点 |
| FR-AGT-002 | P0 | Agent 只能使用注册工具 | 工具参数经过 schema、用户范围、权限、限流和超时校验 |
| FR-AGT-003 | P0 | 有状态操作必须用户确认 | 预约、取消和偏好修改在未确认时只能生成候选动作 |
| FR-AGT-004 | P0 | 支持 interrupt/resume | 用户确认后可从 checkpoint 恢复，不重复执行前置工具 |
| FR-MEM-001 | P0 | 保存短期会话记忆 | 保存当前行程、已选活动、未解决问题和引用，具有 TTL |
| FR-MEM-002 | P1 | 保存长期偏好和关闭案例 | 支持租户隔离、删除、过期和来源标记 |
| FR-EVENT-001 | P0 | 通过 Redis Stream 处理业务事件 | 预约、取消、库存、提醒事件支持 Consumer Group 和 pending 恢复 |
| FR-EVENT-002 | P0 | 事件幂等和死信处理 | 重复事件不重复扣库存或创建订单；失败超过阈值进入 DLQ |
| FR-API-001 | P0 | 提供对话、行程、预约和健康 API | OpenAPI 契约稳定；错误含 request_id 和业务错误码 |
| FR-API-002 | P1 | 提供 Agent 运行事件流 | 支持 tool_start、citation、approval_required、done 和 error 事件 |
| FR-AUDIT-001 | P0 | 保存预约、工具和审批审计 | 可关联用户、run、工具、订单、参数 hash 和最终结果 |
| FR-EVAL-001 | P0 | 支持固定案例回放 | 记录检索、工具序列、约束结果、引用和最终订单状态 |

## 7. 业务规则与状态约束

### 7.1 行程约束

约束分为硬约束和软偏好：

- 硬约束：日期、人数、开放时间、活动时段冲突、库存、预算上限和必须保留的已确认预约。
- 软偏好：室内/室外比例、步行距离、景点热度、餐饮类型和方案风格。
- 任何硬约束未满足时，方案状态为 `INFEASIBLE`，Agent 必须列出冲突，不得描述为已安排。
- 软偏好可以权衡，但必须在报告中说明取舍。
- 金额、时间、库存和订单状态由确定性服务计算，LLM 只负责解释和组织结果。

### 7.2 预约状态

```text
AVAILABLE -> HELD -> CONFIRMED -> COMPLETED
              |         |
              |         +-> CANCELLED
              +-> EXPIRED
AVAILABLE -> WAITLISTED -> CONFIRMED | CANCELLED
```

- `HELD` 必须有过期时间和 hold token；过期后只能由释放任务回收。
- `CONFIRMED` 必须关联已消费的用户确认记录、价格快照、活动版本和幂等键；确认记录必须绑定主体、参数 hash、有效期和一次性消费状态。
- 创建 `HELD` 时必须固化版本化退改规则快照；取消由确定性 evaluator 按快照和当前时间判断，RAG citation 只能解释，不能决定允许取消或退款结果。
- 预约执行前校验用户权限、库存版本、活动版本和参数 hash。
- 同一个幂等键只能产生一个业务结果；重试必须返回原结果。
- 库存数量守恒为 `available + held + confirmed = total`。若将外部供应商占用纳入首期，必须显式增加 `external_held` bucket；不得以 `<= total` 掩盖漏记或重复记账。
- `HELD` 的创建、确认、过期和取消都必须先在同一个 PostgreSQL 事务中锁定 `inventory` 与 `reservation`，按固定顺序执行状态 CAS 和数量迁移，再写 outbox。Redis Lua 只做协调、快速拒绝和可重建投影，不是预约成功事实。
- `confirm`、`expire`、`cancel` 竞争时，只有 PostgreSQL CAS 成功的一方可以发布对应事件；CAS 失败的一方不得释放或增加 Redis 库存。Redis 更新失败进入对账，不反向改变 PostgreSQL 结果。
- PostgreSQL 事务提交结果超时或未知时，服务必须以统一操作幂等键查询 PostgreSQL；确认提交成功后才重试 Redis 投影，未确认前不得盲目再次 hold、confirm 或 release。孤儿 Redis hold 必须由带 fencing/代际的扫描任务处理。

### 7.3 Agent Run 状态

```text
RUNNING -> WAITING_CONFIRMATION -> RUNNING -> COMPLETED
                                      |-> FAILED
RUNNING -> CANCELLED
```

- `thread_id` 使用服务端构造的租户命名空间，例如 `tenant:{tenant_id}:trip:{trip_id}` 或 `tenant:{tenant_id}:user:{user_id}:conversation:{conversation_id}`；客户端不得提交或覆盖它。
- checkpoint 保存图状态、工具结果引用和中断节点；不保存支付凭证或未脱敏敏感信息。
- 用户拒绝候选预约后，Run 可以继续生成替代方案，但不能自动再次提交原预约。
- 同一 `(tenant_id, thread_id)` 同一时刻只允许一个执行者持有数据库 lease/fencing token；不同消息必须排队或返回稳定的 `THREAD_BUSY`，不得隐式 last-write-wins。
- 202 创建 Run 必须和 `agent.run.requested.v1` outbox 在同一 PostgreSQL 事务提交。worker 启动时扫描未完成 Run，使用 lease 恢复；Run 和 checkpoint 更新都带版本 CAS。

### 7.4 事件处理规则

- Redis Stream 消费者处理成功后，先提交 PostgreSQL 事务，再 ACK 消息。
- 业务唯一约束、订单版本 CAS 和幂等键是最终正确性保障，Redis 不提供“恰好一次”事实保证。
- 消费失败允许有限次重试；超过阈值写入 DLQ，并保存原始事件引用和错误原因。
- 不同租户的 key 和事件不能互相读取；Lua 脚本的所有 key 必须属于同一 Redis hash slot。
- `EventEnvelope` 的字段、事件类型版本和错误语义在概要/详细设计/API schema 中复用同一模型；事件 `event_id`、HTTP 幂等键、action_id、confirmation token 和 hold token 是不同标识，不能互换。
- inbox 处理必须有 `lease_owner`、`lease_until`、`claim_token` 和 fencing/version；Redis PEL 的 `XAUTOCLAIM` 只负责消息所有权转移，不能替代数据库业务 lease。
- Stream 裁剪只能删除所有 Consumer Group 都已越过的安全水位；已发布 outbox 必须保留可重放 watermark，Redis 丢失后可从 outbox/事件归档重建。

## 8. 外部数据与知识库要求

### 8.1 数据源

| 数据源 | 用途 | 数据性质 | 首期处理方式 |
|---|---|---|---|
| OpenStreetMap/Overpass | 景点、餐厅、道路和地理位置 | 公共地理数据 | 定期同步并保存查询时间 |
| Open-Meteo | 天气和预报 | 动态数据 | 工具实时查询并短 TTL 缓存 |
| 景点官方页面/公告 | 开放时间、预约、退改和入场要求 | 权威静态/半动态数据 | 抓取或人工整理后版本化索引 |
| 城市公共旅游数据 | 活动、公共交通和旅游服务 | 公共开放数据 | 保存发布版本和来源 URL |
| 本地模拟库存服务 | 价格、活动时段和库存 | 项目自有事实 | PostgreSQL 权威存储，Redis 做高频协调 |
| 用户行程与评价 | 偏好和成功案例 | 项目业务数据 | 脱敏后写入长期记忆，支持删除 |

RAG 适合回答“景点是什么、如何预约、退改规则是什么、有哪些游玩建议”；当前天气、库存、价格、订单状态和用户剩余配额必须使用实时工具查询。

### 8.2 知识文档最小字段

```json
{
  "document_id": "attraction:west-lake:booking-policy",
  "document_type": "booking_policy",
  "source": "official_site",
  "source_id": "west-lake",
  "source_url": "https://example.invalid/policy",
  "source_version": "2026-10-12",
  "title": "预约与退改规则",
  "city": "hangzhou",
  "attraction_id": "west-lake",
  "content_sha256": "...",
  "retrieved_at": "2026-10-12T00:00:00Z"
}
```

每个 chunk 必须保留 `document_id`、来源、标题、版本、URL、heading、chunk_id 和内容 hash。索引切换需要 manifest；新版本构建失败时不得删除旧索引。

## 9. LangChain 工具契约

工具必须使用 Pydantic 输入输出模型，并由工具网关注入用户、租户、角色、资产范围、超时和限流上下文。模型不能传入或覆盖 `user_id`、`tenant_id`、执行凭证、任意 URL 或库存版本。

### 9.1 只读工具

- `search_attractions`
- `get_weather`
- `get_opening_hours`
- `estimate_route_time`
- `check_ticket_inventory`
- `search_travel_knowledge`
- `get_trip_context`
- `get_reservation_history`

### 9.2 候选动作工具

- `calculate_trip_budget`
- `save_user_preference`
- `create_trip_reminder`
- `propose_reservation`
- `propose_cancellation`

候选动作只能写入待确认记录，不得直接扣减库存或修改订单。确认 API 负责生成授权结果，Reservation Service 负责校验状态、版本和幂等键后执行。

模型可见输入不得包含或接受服务端权威字段：`tenant_id`、`user_id`、价格金额、`activity_version`、`inventory_version`、hold token、confirmation token 和订单状态均由服务端注入或重新读取。模型只能提交 slot、数量、理由和候选引用；服务端以数据库当前价格和库存快照计算 `parameters_hash`。

## 10. 接口需求

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/health/live` | 进程存活检查 |
| GET | `/health/ready` | Redis、PostgreSQL 和关键依赖就绪检查 |
| POST | `/v1/conversations/{conversation_id}/messages` | 发送用户消息并启动/恢复 Agent Run |
| GET | `/v1/runs/{run_id}` | 查询 Agent Run、状态、引用和错误 |
| POST | `/v1/actions/{action_id}/confirmation` | 当前主体重新签发丢失或过期的 action confirmation；不执行业务副作用 |
| GET | `/v1/runs/{run_id}/events` | P1：订阅运行事件 |
| POST | `/v1/trips` | 创建行程草案 |
| POST | `/v1/trips/{trip_id}/confirm` | 确认行程方案或批准候选 action；不直接扣库存 |
| GET | `/v1/trips/{trip_id}` | 查询行程和预约状态 |
| POST | `/v1/reservations/{reservation_id}/cancel` | 取消预约 |
| POST | `/v1/reservations` | 在已批准 action 下创建短期库存 hold |
| POST | `/v1/reservations/{reservation_id}/confirm` | 使用已批准 action/confirmation command 确认预约；hold token 不是唯一恢复凭证 |
| GET | `/v1/reservations/{reservation_id}` | 查询预约和库存权威状态 |
| GET | `/v1/knowledge/search` | 调试和评估知识检索 |

所有写操作返回 `request_id`、业务状态、版本或幂等结果。写操作的幂等 scope 统一为 `tenant_id + user_id + route + operation + idempotency_key`；`key_hash` 必须对完整 scope（含原始 key）做 canonical SHA-256，不能只 hash 原始 key，并保存 canonical request hash；Redis 快速命中也必须比较 request hash。错误分为参数错误、权限错误、库存不足、状态冲突、依赖超时和系统错误，不能只返回模型自然语言。

## 11. Redis 与 PostgreSQL 需求边界

### 11.1 Redis

建议使用以下命名空间：

```text
tm:reservation:v1:{env}:{tenant_id}:stream:reservations
tm:inventory:v1:{env}:{tenant_id}:stream:inventory
tm:reservation:v1:{env}:{tenant_id:slot_id}:idem:{idempotency_key_hash}
tm:inventory:v1:{env}:{tenant_id:slot_id}:stock:{activity_id}:{slot_id}
tm:reminder:v1:{env}:{tenant_id}:zset:reminders
tm:memory:v1:{env}:{tenant_id}:conversation:{conversation_id}
langgraph-saver:{tenant_id}:{thread_id}:checkpoint/*
tm:rag:v1:{env}:{tenant_id}:index:{index_version}
```

Redis 用于高频访问、事件协调、短期状态、checkpoint、长期记忆和向量检索；Redis 丢失后可以根据 PostgreSQL、知识源 manifest 和用户显式数据重建。

长期偏好/案例向量必须带 `tenant_id`、`user_id`、`deleted_at/tombstone` 和 `generation` 过滤；删除先阻断召回，再物理删除或重建。LangGraph checkpoint 使用真实 saver 的 namespace、`configurable.thread_id`、`setup()` 和 pending writes 契约。已发布 outbox 保留 replay watermark，Stream trim 必须遵守所有 Consumer Group 的安全水位。

### 11.2 PostgreSQL 权威表

首期至少包括：`user_profile`、`user_preference`、`trip`、`trip_item`、`attraction`、`activity_slot`、`reservation`、`agent_run`、`agent_tool_call`、`audit_log`、`inbox_event`、`outbox_event` 和 `evaluation_case`。

预约、库存、价格和状态的最终一致性由 PostgreSQL 事务、唯一约束和版本 CAS 保证；MVP hold 先在 PostgreSQL 事务中迁移数量和预约状态，Redis Lua 只投影/协调，投影失败由补偿任务与数据库状态对账。Redis 丢失时，已发布 outbox 的 replay watermark、inventory 聚合和 RAG manifest 是重建输入；不能只依赖仍存在的 Redis Stream。

## 12. 非功能需求

| ID | 维度 | 首期要求 |
|---|---|---|
| NFR-PERF-001 | 预约并发 | 在固定硬件、固定活动和压测数据下支持至少 500 个并发预约请求，记录吞吐和 P95；目标为不超卖 |
| NFR-PERF-002 | API 延迟 | 只读库存查询 P95 目标不超过 300ms；预约写路径 P95 目标不超过 800ms，需单独统计外部工具和 LLM 延迟 |
| NFR-PERF-003 | Agent 延迟 | 记录首 token、工具调用和完整方案延迟；完整方案 P95 目标不超过 15 秒，模型异常时可降级为结构化查询 |
| NFR-QUALITY-001 | RAG | 固定政策问答集 Recall@5、MRR、citation 可定位率和引用正确率分别统计，目标值由基线测试确认 |
| NFR-QUALITY-002 | 规划 | 固定场景的硬约束满足率目标为 100%；预算、时间和库存不能由模型自行估算覆盖工具结果 |
| NFR-REL-001 | 幂等 | 重复预约、取消和事件重放不产生重复业务副作用 |
| NFR-REL-002 | 恢复 | Redis pending、Agent interrupt 和服务重启后可恢复；恢复过程不能跳过人工确认 |
| NFR-DATA-001 | 数据一致性 | PostgreSQL 是预约和库存事实源；Redis 缓存、Stream、memory 和 index 可重建 |
| NFR-SEC-001 | 权限 | 工具调用执行用户、租户、资源范围、速率、超时和 allowlist 校验 |
| NFR-SEC-002 | 隐私 | 用户偏好、同行信息和地址等敏感数据脱敏、加密或最小化保存，支持删除 |
| NFR-SEC-003 | 防注入 | 外部网页、攻略和用户消息均视为不可信文本，不能改变系统提示、工具权限或审批规则 |
| NFR-OBS-001 | 可观测性 | OpenTelemetry 关联 request、run、tool、stream、reservation 和 trace；日志不得泄漏密钥 |
| NFR-REPRO-001 | 可复现 | 数据源版本、索引 manifest、模拟库存种子、评估案例和压测参数可固定重放 |
| NFR-MAINT-001 | 可维护性 | schema、工具、状态机、Lua 脚本、migration 和错误码有自动化测试 |

可观测性指标的 label 只允许低基数枚举（route、status、operation、tool、event_type、result）；`tenant_id`、`user_id`、`slot_id`、`reservation_id`、`thread_id`、URL 和 token 只能作为脱敏 trace/span attribute 或日志 hash，不能作为 metric label。

## 13. V 模型开发与验证计划

TripMate 按以下 V 模型执行：

```text
用户需求与目标
      |                         验收测试：真实用户场景、指标报告
系统需求规格（本文）       <-> 系统测试：完整对话、预约和恢复闭环
概要设计：服务、数据和流程 <-> 集成测试：Redis、PostgreSQL、RAG、工具联调
详细设计：状态机、schema、Lua <-> 单元测试：规则、校验、key、工具和节点
      |                         
                    编码实现
```

### 13.1 需求到测试追踪

下表中的测试路径是目标测试规划，不保证当前均已创建。现有可执行测试和覆盖边界见 `docs/implementation-notes.md`。

| 需求集合 | 设计产物 | 验证层级 | 测试/报告 |
|---|---|---|---|
| FR-USER、FR-MEM | 用户/偏好模型、Redis memory、删除流程 | 单元 + 系统 | `tests/unit/test_memory_policy.py`、`tests/system/test_preference_recall.py` |
| FR-TRIP | Trip schema、约束校验器、LangGraph planner | 单元 + 系统 | `tests/unit/test_itinerary_constraints.py`、`tests/system/test_trip_planning.py` |
| FR-KB | 同步器、chunk、Redis index、citation | 集成 + 评估 | `tests/integration/test_knowledge_index.py`、`tests/evaluation/test_rag_quality.py` |
| FR-REAL | 天气、开放时间、路线适配器 | 集成 | `tests/integration/test_realtime_tools.py` |
| FR-INV、FR-RES | PostgreSQL 库存/预约事务、Redis Lua 协调、状态机 | 单元 + 集成 + 并发故障 | `tests/unit/test_inventory_lua.py`、`tests/integration/test_reservation_idempotency.py`、`tests/integration/test_inventory_reconciliation.py` |
| FR-AGT | LangGraph state/reducer、工具网关、interrupt/resume、Run worker lease | 单元 + 集成 + 系统 | `tests/unit/test_agent_graph.py`、`tests/system/test_approval_resume.py`、`tests/system/test_run_recovery.py` |
| FR-EVENT | Stream consumer、outbox replay、inbox lease/fencing、DLQ | 集成 + 故障注入 | `tests/integration/test_event_recovery.py`、`tests/fault/test_stream_rebuild.py` |
| FR-API | FastAPI route、错误码、鉴权 | 接口测试 | `tests/api/test_trip_api.py` |
| NFR-PERF | 压测脚本、指标和追踪 | 性能测试 | `tests/load/reservation_load.py`、性能报告 |
| NFR-SEC | 工具 allowlist、租户隔离、脱敏 | 安全测试 | `tests/security/test_tool_boundary.py` |
| NFR-REPRO | fixture、seed、manifest、回放器 | 回归测试 | `tests/evaluation/test_replay.py` |

### 13.2 测试数据要求

首期评估集至少包含：

1. 12 个自然语言规划案例，覆盖家庭、老人、低预算、雨天、少步行和多人同行。
2. 20 个政策问答案例，覆盖开放时间、预约、儿童票、退改和证据不足。
3. 10 个库存并发案例，包含重复请求、超时释放、服务重启和库存不足。
4. 10 个记忆案例，覆盖偏好新增、修改、删除和跨用户隔离。
5. 10 个安全案例，覆盖提示注入、越权预约、伪造库存和工具参数篡改。

所有案例都需要有输入、期望工具序列、硬约束结果、期望引用、订单结果和允许的未知项，不能只用最终自然语言相似度评分。

## 14. 验收标准

MVP 必须满足以下条件：

1. 用户可以通过自然语言创建一日游方案，方案结构化输出且硬约束校验可解释。
2. 政策问答能够返回来源、版本、URL 和引用片段；无证据时明确返回未知。
3. 天气、开放时间和库存来自工具结果，不能由模型凭空生成。
4. 同一幂等键重复预约只产生一个订单；并发抢订时库存不为负、不超卖。
5. 用户未确认前不产生真实或模拟预约副作用；真实 LangGraph Redis saver 的中断恢复可重跑 interrupt 节点而不重复 action、工具或库存副作用。
6. 取消、过期和重试遵守状态机，非法状态转换被拒绝并记录审计。
7. Redis Stream 消息在消费者异常退出后可以恢复、重试或进入 DLQ。
8. 用户偏好能够在后续会话中召回，删除后不可再被召回，跨用户读取被拒绝。
9. 固定评估集能输出 RAG、规划、记忆、工具和并发指标报告。
10. 所有性能数值以实际压测报告为准，未压测前只能标注为目标值。

## 15. 本地作品待办

本节是目标需求的实施顺序，不代表下列能力都尚未编码，也不包含上线/部署任务；逐项现状见 `docs/implementation-notes.md`。

1. **Agent 核心**：接入 LLM，补结构化意图和槽位识别、模型受控工具调用，以及使用偏好和实时事实的多景点规划。
2. **规划与检索**：把开放时间、路线和天气用于约束/重规划；补知识索引流程、语义检索和固定评测集。
3. **闭环验证**：补完整 Agent 图的确认/拒绝、checkpoint 恢复、偏好候选处理和固定案例评测。
4. **演示增强**：按展示需要实现多方案比较或 SSE；真实提醒渠道、集群容量、生产级压测等不属于本项目必做项。

已有 PostgreSQL、Redis Stack、Run worker 和工具边界的集成测试应作为 Agent 迭代的回归门禁；本地测试通过不等于达到文中的性能或检索质量目标。

概要和详细设计按需求 ID 追踪目标能力；代码与测试的实际覆盖范围见 `docs/implementation-notes.md`，不得把目标条款当作已实现证据。

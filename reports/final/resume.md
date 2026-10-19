# 简历条目（数字全部取自 reports/final/summary.md）

**TripMate：可验证约束的中文行程规划 Agent**｜Python、LangGraph、DeepSeek、bge、SQLite｜ChinaTravel（ICLR 2026）基准

1. 基于 LangGraph 构建中文行程规划 Agent：LLM 把自然语言需求编译成结构化约束（TripSpec → 官方 DSL），由 beam search 规划器生成计划，再用官方常识规则和 DSL 执行器校验，失败时按错误类型修复（≤3 轮 + 1 次反思）。在 ChinaTravel human-154 上 FPR 为 79.22%（95% CI 72.73–85.71），同模型（deepseek-flash）的官方 ReAct 复现为 0.00%（McNemar p=3.76e-37）；每条查询平均 1.28 次 LLM 调用、1,526 输入 token，ReAct 复现为 345,447（论文 ReAct one-shot 约 1,058K）。
2. 实现召回前过滤的混合检索：jieba-BM25 + bge-small-zh 稠密检索 + RRF（k=60）。在 POI-Ret test 上 nDCG@10 从 0.297（BM25）提升到 0.419；不做过滤时 8.98% 的返回结果违反硬约束，召回前过滤后为 0。bge-reranker 重排可把 nDCG@10 提升到 0.532，但 CPU 上的延迟超出预设门槛，经评测后决定不启用。
3. 设计分层记忆：SQLite 存储按轮版本化的会话约束状态，长期偏好带有效期（valid_from / valid_to / superseded_by），经 LangGraph interrupt 确认后才写入；每段对话一个独立的库，便于隔离和复现。在自建多轮集（由 ChinaTravel 派生，dev / test 隔离）上 FPR 为 76.62%（同批单轮 79.22%，差异不显著，p=0.481），约束保持率 0.9135；dev 上关闭记忆后 FPR 从 98.33% 降到 0.00%。
4. 构建可复现评测：官方评测器封装（与官方 CLI 指标一致）、LLM 响应缓存与回放包、配置哈希冻结、test 只运行一次（锁文件）、bootstrap 置信区间与 McNemar 检验、8 项单轮消融 + 2 项记忆消融（dev）。消融显示，同样的约束和候选交给 LLM 直接写计划时 FPR 只有 1.67%（本系统 100%，dev_mini 配对），改用纯规则抽取后 FPR 从 95.56% 降到 82.89%。单轮 test 结果可用回放包在不调用 API 的情况下逐条复现；全部实验 API 费用 15.179 元（按高峰价核算）。

## 填写说明

- 第 3 条 dev 上的 98.33% / 0.00% 和第 4 条的 1.67% / 100%、95.56% → 82.89%都来自 summary 第 8 节的 dev 消融表，不是 test 结果；面试时要说明清楚。
- 负结果照实保留：
  - 修复环、DSL 校验、单一检索通道、召回后过滤、特征打分的消融在 dev 上都不显著；
  - 重排因延迟不启用；
  - 多轮回放中有 1 段对话因规划器的墙钟时间预算未能逐条复现（summary 第 7 节）。
- 延迟和存储写入耗时受本机负载影响，不写数值，只讲设计：
  - 每次 LLM 调用经过统一客户端（缓存、费用账本、预算上限）；
  - 检索使用精确向量检索并先过滤；
  - 规划设置时间预算。
- 可能被追问的弱点：
  - test 上的主要失败是约束抽取（常识通过而逻辑约束失败的 27 条），例如 must_visit 召回率 0.462、attraction_types 精确率 0.567（summary 第 6 节）；
  - 官方 RuleNeSy 在 test 上每条都撞到 300 s 时限，提前终止，没有列入对比。

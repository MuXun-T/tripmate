# 最终结果（ChinaTravel human-154 test，只运行一次）

- 冻结配置：config_hash `f898a57f6bfae82dcfd957861474dd5b441e0f0a3aaace8d8c913cb269c95f45`（git `e52a7db`，见 reports/final/FREEZE.json）；test 锁文件见 reports/final/TEST_LOCK_*.json。
- 模型：deepseek-flash，thinking 关闭（`extra_body={"thinking":{"type":"disabled"}}`），temperature 0；本系统与 ReAct0 复现使用同一模型。
- 指标由官方评测函数计算（与官方 CLI 一致，见 reports/p0_metric_consistency.md）；95% CI 为 bootstrap 1,000 次（seed 20261018）；McNemar 为按 uid 配对的双侧精确检验，b01 = 该方法失败且本系统通过的条数，b10 = 该方法通过且本系统失败的条数。

## 1. 主表（单轮）

| 方法 | run_id | n | DR | EPR-micro | EPR-macro | LPR-micro | LPR-macro | C-LPR | FPR [95% CI] | 本系统 vs 该方法 b01/b10, p |
|---|---|---|---|---|---|---|---|---|---|---|
| 本系统（full） | p5-full-test | 154 | 96.75 | 96.75 | 96.75 | 92.68 | 79.22 | 92.68 | 79.22 [72.73, 85.71] | - |
| 官方 ReAct0 复现（deepseek-flash，zero-shot） | p5-react0-test | 154 | 93.51 | 63.97 | 0.00 | 79.50 | 21.43 | 0.00 | 0.00 [0.00, 0.00] | 122/0, p=3.76e-37 |
| 论文 ReAct（one-shot，DeepSeek） | - | 154 | - | - | - | - | - | - | 2.6 | 参考值 |
| 论文 NeSy Planning（DeepSeek-V3） | - | 154 | - | - | - | - | - | - | 37.0 | 参考值 |

- 论文参考值来源：arXiv 2412.13682v4，human-154；模型与本项目不同，只作参考，不做显著性检验。
- ReAct0 复现：保存计划 154/154 条，其中报错 1 条；未保存的条目按空计划计分。
- 官方 RuleNeSy（无 LLM）：在 test 上运行了前 12 条后终止，未列入主表。这 12 条中搜索成功 0 条，每条耗时 300–302 s（官方搜索时限 300 s），按此估算全量需要十几个小时；它不读自然语言、只用结构化字段，作为参照的价值有限。未写锁文件（run_id p5-rulenesy-test）。

## 2. 成本与 token（每条查询平均值）

| 方法 | run_id | 实际 API 调用/条 | 输入 token/条 | 其中 DeepSeek 缓存命中 | 输出 token/条 | 费用/条（标准价，元） | 费用/条（高峰价，元） | 本次合计（高峰价，元） |
|---|---|---|---|---|---|---|---|---|
| 本系统（full，单轮） | p5-full-test | 1.28 | 1,526 | 80.7% | 132 | 0.0008 | 0.0017 | 0.261 |
| 本系统（多轮） | p5-mt-full-test | 2.93 | 3,505 | 82.8% | 263 | 0.0017 | 0.0034 | 0.527 |
| 官方 ReAct0 复现 | p5-react0-test | 53.41 | 345,447 | 95.8% | 4,552 | 0.0392 | 0.0784 | 12.076 |

- 论文中 ReAct（one-shot）每条查询约 1,058K 输入 token（参考值，arXiv 2412.13682v4，human-154；模型与本项目不同，只作参考，不做显著性检验）。
- 只统计真正发往 API 的调用（本地缓存命中不计）；“DeepSeek 缓存命中”是 API 端上下文缓存命中的输入 token 占比。
- 调用数和 token 以费用账本（runs/cost_ledger.jsonl）为准，其中包括返回未通过 schema 校验、最终退回规则解析的调用；trace 不记录这部分，所以 runs/<run_id>/metrics.json 里的每条调用数略低。
- 项目全部实验累计 API 费用：高峰价核算 15.179 元，标准价 7.589 元；按 scope（高峰价）：p0_smoke 0.000，p1 0.635，p3 0.570，p4 0.918，p5_baseline 12.264，p5_ours 0.792。

## 3. 多轮（MT-test，154 段对话，由 human-154 派生）

| 配置 | run_id | n | DR | EPR-macro | LPR-macro | FPR [95% CI] | 约束保持率 | 含偏好会话的对话 |
|---|---|---|---|---|---|---|---|---|
| 本系统（多轮） | p5-mt-full-test | 154 | 90.91 | 88.96 | 77.27 | 76.62 [69.48, 83.12] | 0.9135 | 1 |
| 同一批 uid 的单轮结果 | p5-full-test | 154 | - | - | - | 79.22 [72.73, 85.71] | - | - |

- 多轮相对单轮：b01/b10 = 7/11（b01 = 单轮失败而多轮通过，b10 = 单轮通过而多轮失败），p=0.481。
- 关闭记忆（M1）、关闭长期偏好（M2）的消融只在 dev 上做（reports/p4_mt_dev.md）；test 中含偏好会话的对话很少，跨会话偏好在 test 上基本无法检验。

## 4. 检索（POI-Ret test，由 oracle 派生）

| 配置 | 类型子查询 n | nDCG@10 [95% CI] | Success@10 | P@10 | 定位子查询 n | Success@1 | MRR@10 |
|---|---|---|---|---|---|---|---|
| bm25 | 99 | 0.297 [0.236, 0.368] | 0.788 | 0.290 | 39 | 0.231 | 0.334 |
| dense | 99 | 0.383 [0.325, 0.440] | 0.838 | 0.340 | 39 | 0.231 | 0.382 |
| hybrid | 99 | 0.419 [0.349, 0.492] | 0.798 | 0.393 | 39 | 0.308 | 0.394 |
| hybrid+rerank | 99 | 0.532 [0.458, 0.611] | 0.818 | 0.480 | 39 | 0.359 | 0.543 |

| 过滤方式 | 子查询数 | 不足率 | 返回结果违规率 | 成功率 |
|---|---|---|---|---|
| 召回前过滤（本系统） | 154 | 0.00% | 0.00% | 100.00% |
| 召回后过滤 | 154 | 0.00% | 0.00% | 100.00% |
| 不过滤 | 154 | 0.00% | 8.98% | 100.00% |

- 重排（bge-reranker-base）在 dev 上提升了 nDCG@10，但 CPU 上的检索延迟超出 T2.6 的门槛，冻结配置中关闭（reports/p2_retrieval_dev.md）；这里的 hybrid+rerank 行只作组件评测。

## 5. 常识失败分布（单轮 test）

| 常识规则（失败条数） | 本系统（full） | 官方 ReAct0 复现（deepseek-flash，zero-shot） |
|---|---|---|
| Incorrect cost Information of Restruants | 5 | 124 |
| Incorrect Cost on Intercity Transportation | 5 | 103 |
| Repeated Restruants Choices | 5 | 103 |
| Inappropriate Meal Times | 5 | 100 |
| Incorrect cost Information of attraction | 5 | 97 |
| Incorrect Information of Accommodation on price or room type | 5 | 96 |
| Commonsense Evaluator Exception | 0 | 92 |
| Repeated attraction Choices | 5 | 88 |
| Visiting Restruants in their closed time | 5 | 86 |
| Repeated Meal Types in One Day | 5 | 82 |
| Incorrect price Information of Restruants | 5 | 81 |
| Unavailable Restruants | 5 | 80 |
| Unavailable Inner-City Transport | 5 | 60 |
| Incorrect Information of Inner-City Transporton on price, distance, and duration | 5 | 57 |
| Visiting attraction in their closed time | 5 | 55 |
| Does not follow Chronological Order | 5 | 49 |
| Incorrect cost Information of Accommodation | 5 | 43 |
| Incorrect price Information of attraction | 5 | 37 |
| Incorrect cost information of Inner-City Transport | 0 | 35 |
| Unavailable attractions | 5 | 32 |
| Invalid Transport information across positions | 5 | 30 |
| Inccorrect cost information of Inner-City Transport | 5 | 25 |
| Unavailable Accommodation | 5 | 11 |
| Accomondation is necessary for trips longer than one day | 5 | 11 |
| Invalid Trains or Airplanes, given TrainID/FlightID, origin and destination | 5 | 10 |
| Incorrect Information of Intercity Transport on price or duration | 5 | 10 |
| Invalid or ungrounded activity entity | 5 | 9 |
| Intercity transportation events must occur | 5 | 2 |
| Invalid duration information of each activity | 5 | 1 |

## 6. 本系统运行概况（单轮 test）

- final_status：{'passed': 148, 'no_plan': 5, 'best_effort': 1}；修复轮数均值 0.019；反思触发率 0.0649。
- 约束抽取（对 oracle 标签）：micro F1 0.8768，精确匹配率 0.7208。
- DSL 校验最终仍失败的约束类型：{'attraction_types': 1}。

| 失败层（互斥，按先后判定） | 条数 | 占比 |
|---|---|---|
| 无计划或不符合 schema | 5 | 3.2% |
| 常识规则失败 | 0 | 0.0% |
| 常识通过、逻辑约束失败 | 27 | 17.5% |
| 全部通过（FPR） | 122 | 79.2% |

| TripSpec 字段（对 oracle 标签） | P | R | F1 | tp / fp / fn |
|---|---|---|---|---|
| room_type | 0.000 | 0.000 | 0.000 | 0 / 3 / 0 |
| rooms | 0.286 | 1.000 | 0.444 | 2 / 5 / 0 |
| innercity_mode | 0.333 | 1.000 | 0.500 | 4 / 8 / 0 |
| hotel_features | 0.429 | 0.750 | 0.545 | 3 / 4 / 1 |
| cuisines | 0.543 | 0.704 | 0.613 | 19 / 16 / 8 |
| must_visit | 1.000 | 0.462 | 0.632 | 18 / 0 / 21 |
| attraction_types | 0.567 | 0.868 | 0.686 | 59 / 45 / 9 |
| budget_total | 0.839 | 0.959 | 0.895 | 47 / 9 / 2 |
| intercity | 0.816 | 1.000 | 0.899 | 31 / 7 / 0 |
| days | 1.000 | 1.000 | 1.000 | 154 / 0 / 0 |
| people_number | 1.000 | 1.000 | 1.000 | 154 / 0 / 0 |

- 端到端延迟 P50 3.8 s / P95 16.8 s（4 线程并发；本机有其他常驻负载，绝对值仅供参考，不作为对外数字）。

## 7. 复现（回放包，不调用 API）

命令：`python -m tripmate_planner.eval.replay --tag <后缀>`；回放包 `reports/final/llm_replay.jsonl.gz`，每次回放只加载对应运行自己记录的响应。

| 原始运行 | 回放运行 | 官方指标一致 | 逐条通过情况一致 | 计划文件不同的条数 | 回放 FPR | 回放 API 费用 |
|---|---|---|---|---|---|---|
| p5-full-test | p5-full-test-replay-20261019b | 是 | 是 | 0 | 79.22 | 0.0 |
| p5-mt-full-test | p5-mt-full-test-replay-20261019b | 否 | 否 | 1 | 77.27 | 0.0 |

- p5-mt-full-test / h20241029143502525564：原始 final_status=best_effort，回放 best_effort；修复 3 轮、反思 True；回放中 LLM 调用全部命中回放缓存=True，调用序列与原始相同=True。差异来自规划器的墙钟时间预算（time_budget_s=20）：这条已用完全部修复轮次的难例中，搜索在时间预算内到达的候选随机器负载变化。冻结后不再修改规划器，报告中的数字均取原始运行。

## 8. dev 消融（引用，未在 test 上运行）

详见 reports/p3_dev.md 与 reports/p4_mt_dev.md。主表复制如下（由同一脚本从 dev 运行结果生成）：

| 配置 | run_id | n | DR | EPR-macro | LPR-macro | C-LPR | FPR [95% CI] | vs 基线 b01/b10, p | 抽取 F1 | 调用/条 | 输入 token/条 | P50 / P95 ms | 本次费用（高峰价） |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| full | p3-final-full-dev | 450 | 99.33 | 99.11 | 95.56 | 98.49 | 95.56 [93.56, 97.33] | - | 0.9840 | 0.0 | 1238.2 | 3723 / 7624 | 0.0 |
| A1 去掉修复环 | p3-A1_no_repair-dev | 450 | 99.33 | 99.11 | 95.56 | 98.49 | 95.56 [93.56, 97.33] | 0/0, p=1 | 0.9840 | 0.0 | 1221.7 | 3545 / 6931 | 0.0 |
| A2 去掉 DSL 硬约束校验 | p3-A2_no_spec_verify-dev | 450 | 99.33 | 99.11 | 95.33 | 98.45 | 95.33 [93.11, 97.33] | 0/1, p=1 | 0.9840 | 0.0 | 1230.0 | 3423 / 6882 | 0.0 |
| A3 纯规则改写 | p3-A3_rule_rewrite-dev | 450 | 94.89 | 92.44 | 82.89 | 90.89 | 82.89 [79.33, 86.44] | 0/57, p=1.39e-17 | 0.9500 | 0.0 | 0.0 | 3440 / 7429 | 0 |
| A4 只用 BM25 | p3-A4_bm25-dev | 450 | 99.33 | 99.11 | 94.89 | 98.35 | 94.89 [92.67, 96.89] | 1/4, p=0.375 | 0.9840 | 0.002 | 1241.2 | 1846 / 6688 | 0.0017 |
| A5 只用稠密 | p3-A5_dense-dev | 450 | 98.67 | 98.44 | 95.33 | 97.71 | 95.33 [93.33, 97.33] | 3/4, p=1 | 0.9840 | 0.007 | 1246.6 | 2070 / 5721 | 0.0038 |
| A7 召回后过滤 | p3-A7_postfilter-dev | 450 | 99.11 | 98.89 | 95.56 | 98.31 | 95.56 [93.33, 97.33] | 1/1, p=1 | 0.9840 | 0.0 | 1240.9 | 2059 / 5094 | 0.0 |
| A8 去掉特征打分 | p3-A8_no_feature_score-dev | 450 | 99.33 | 99.11 | 95.33 | 98.45 | 95.33 [93.33, 97.33] | 0/1, p=1 | 0.9840 | 0.0 | 1238.2 | 2256 / 5591 | 0.0 |

LLM 直接写计划（A9，dev_mini 60 条配对；LLM 只决定活动、顺序和时间，交通与费用由程序补全，不做校验和修复）：

| 配置 | run_id | n | DR | EPR-macro | LPR-macro | C-LPR | FPR [95% CI] | vs 基线 b01/b10, p | 抽取 F1 | 调用/条 | 输入 token/条 | P50 / P95 ms | 本次费用（高峰价） |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| full | p3-final-full-devmini | 60 | 100.00 | 100.00 | 100.00 | 100.00 | 100.00 [100.00, 100.00] | - | 0.9898 | 0.0 | 1153.6 | 3032 / 6268 | 0.0 |
| A9 LLM 直接写计划 | p3-A9_llm_direct-devmini | 60 | 100.00 | 1.67 | 78.33 | 1.21 | 1.67 [0.00, 5.00] | 0/59, p=3.47e-18 | 0.9898 | 1.0 | 3668.4 | 2558 / 3607 | 0.5499 |

多轮记忆消融（MT dev，60 段对话）：

| 配置 | run_id | n | DR | EPR-macro | LPR-macro | C-LPR | FPR [95% CI] | vs 基线 b01/b10, p | 抽取 F1 | 调用/条 | 输入 token/条 | P50 / P95 ms | 本次费用（高峰价） |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| full（多轮） | p4-mt-full-dev | 60 | 100.00 | 100.00 | 98.33 | 99.61 | 98.33 [95.00, 100.00] | - | 0.9924 | 2.917 | 3610.0 | 7737 / 14898 | 0.2093 |
| M1 关闭记忆（只给最后一轮） | p4-mt-M1-dev | 60 | 21.67 | 0.00 | 1.67 | 0.00 | 0.00 [0.00, 0.00] | 0/59, p=3.47e-18 | 0.4618 | 1.7 | 2520.0 | 2361 / 22817 | 0.1168 |
| M2 关闭长期偏好 | p4-mt-M2-dev | 60 | 100.00 | 100.00 | 93.33 | 99.03 | 93.33 [86.67, 100.00] | 0/3, p=0.25 | 0.9767 | 0.583 | 3629.0 | 6072 / 11854 | 0.0416 |

A6（打开重排）未运行：重排在冻结配置中关闭，按计划只在启用时才做 A6。

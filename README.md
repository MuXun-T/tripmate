# TripMate：可验证约束的中文行程规划 Agent

TripMate v1 在公开基准 [ChinaTravel](https://github.com/LAMDA-NeSy/ChinaTravel)（ICLR 2026）上实现一个中文行程规划 Agent。流程是：LLM 把自然语言需求编译成结构化约束（TripSpec → 官方 DSL），检索候选 POI，由符号规划器生成计划，再用官方常识规则和 DSL 执行器校验，失败时按错误类型有限修复。系统配有分层记忆，支持多轮对话。

LLM 只做一件事：理解需求（改写与反思），使用 deepseek-flash，并关闭 thinking。交通、时间和费用都由程序按沙盒数据计算，不交给 LLM 生成。

## 结果

完整结果、置信区间和显著性检验见 [reports/final/summary.md](reports/final/summary.md)，简历条目见 [reports/final/resume.md](reports/final/resume.md)。test 集（human-154）只运行了一次。

| 指标（ChinaTravel human-154） | 本系统 | 官方 ReAct0 复现（同模型） | 论文参考值（模型不同） |
|---|---|---|---|
| FPR（全部约束通过率） | **79.22%**（95% CI 72.73–85.71） | 0.00%（McNemar p=3.76e-37） | ReAct one-shot 2.6；NeSy Planning 37.0 |
| 每条查询输入 token | 1,526（1.28 次 LLM 调用） | 345,447（53.41 次调用） | ReAct one-shot 约 1,058K |

- 多轮（由 human-154 派生的 154 段对话）：FPR 76.62%（95% CI 69.48–83.12），同一批 uid 的单轮结果为 79.22%（差异不显著，p=0.481）。
- 检索（POI-Ret test）：nDCG@10 为 hybrid 0.419、BM25 0.297、稠密 0.383。
- 全部实验的 API 费用：15.179 元（按高峰价核算）。
- 复现：单轮 test 结果可用回放包在不调用 API 的情况下逐条复现；多轮中有 1 段对话受规划器墙钟时间预算影响，没能逐条复现（见 summary 第 7 节）。
- 官方 RuleNeSy 在 test 上每条都撞到 300 s 搜索时限，提前终止，没有列入对比。
- dev 消融（summary 第 8 节）：LLM 直接写计划时 FPR 为 1.67%，本系统为 100%（dev_mini 60 条配对）；改用纯规则改写后 FPR 从 95.56% 降到 82.89%。修复环、DSL 校验、单一检索通道等组件在 dev 上的差异不显著，已如实报告。

## 架构

```
用户话语（单轮或多轮）
  ▼ load_memory    SQLite：上一轮 TripSpec 状态、最近 6 轮原文、生效的长期偏好
  ▼ rewrite        规则解析 + 1 次 deepseek-flash（JSON，枚举受限）→ TripSpec
  ▼ apply_memory   本轮明确说的 > 长期偏好 > 默认值（确定性合并）
  ▼ compile        TripSpec → 官方 DSL 约束（确定性，不读 oracle）
  ▼ retrieve       召回前过滤 → jieba-BM25 / bge-small-zh 稠密检索 → RRF(k=60) → 候选池
  ▼ plan           beam search：城际交通 → 酒店 → 逐日餐饮 / 景点 → 市内交通（官方 goto）
  ▼ verify         官方 8 个常识检查函数 + 官方 DSL 执行器
  ├ 失败 → repair（按错误类型调整，≤3 轮）→ plan；仍失败 → reflect（1 次 LLM 重抽 TripSpec）
  ▼ confirm_memory 有长期偏好候选时 LangGraph interrupt，等用户确认（评测时自动确认）
  ▼ save_memory → 官方 schema 的计划 JSON + trace
```

| 模块 | 位置 | 要点 |
|---|---|---|
| 约束抽取 | `tripmate_planner/spec/` | 规则解析与 LLM 改写合并；城市、天数、人数以规则结果为准；Pydantic 严格校验，失败时纠错重试 1 次，仍失败则退回规则解析 |
| DSL 编译 | `spec/compile_dsl.py` | TripSpec 到官方约束模板的确定性映射；与 oracle 约束做等价性测试 |
| 检索 | `tripmate_planner/retrieval/` | 先按城市、类型、价格等硬条件过滤，再做 BM25 与稠密检索，最后用 RRF 融合；重排可选，冻结配置中关闭 |
| 规划 | `tripmate_planner/plan/` | beam search 加时间轴模拟；预算下界剪枝；必选 POI 组；每个完整候选都先经过官方常识自检 |
| 校验与修复 | `verify/`、`repair/` | 直接复用官方校验函数；失败类型映射到修复动作 |
| 编排 | `graph.py` | LangGraph 状态图，偏好确认通过 interrupt 实现；CLI：`python -m tripmate_planner.cli plan|chat` |
| 记忆 | `memory/store.py` | SQLite 两层：会话约束状态按轮版本化；长期偏好带 valid_from / valid_to / superseded_by，经确认后才写入 |
| 评测 | `tripmate_planner/eval/` | 官方指标封装、LLM 响应缓存与回放、费用账本与上限、bootstrap CI、McNemar、test 锁 |

设计要点：

- 存储选 SQLite，是因为单机评测不需要部署服务，每段对话一个库，便于隔离和复现；
- 检索使用精确向量检索（语料规模不需要近似索引），先过滤再召回，避免召回后过滤导致候选数不足；
- 所有 LLM 请求都经过同一个客户端，统一处理缓存、费用账本和费用上限。

## 复现

```bash
source scripts/planner_env.sh          # 所有缓存都放在项目目录内；只从 .env 读取 LLM_API_KEY
bash scripts/setup_planner.sh          # 官方仓库 @f445e00、沙盒数据、查询数据集与模型（固定版本）
python scripts/check_planner_env.py

pytest tests_planner -q                # 离线测试
python -m tripmate_planner.eval.run_benchmark --config configs/planner/full.json --split dev --run-id my-dev
python -m tripmate_planner.eval.mt_eval --config configs/planner/full.json --split dev --run-id my-mt-dev

# test 只能运行一次（已运行，锁文件在 reports/final/TEST_LOCK_*.json）
bash scripts/run_final_test.sh ours
bash scripts/run_final_test.sh baselines

# 不调用 API，用回放包复现 test 结果（单轮与多轮），并逐条比对指标与计划
python -m tripmate_planner.eval.replay --tag <任意后缀>
python -m tripmate_planner.eval.final_summary
```

版本固定见 `configs/planner/versions.json` 与 `requirements-planner.lock`；冻结配置及其哈希见 `reports/final/FREEZE.json`。

## 评测协议

- **划分**：dev = easy + medium（450 条），用于开发、调参和全部消融；dev_mini 是从 dev 分层抽出的 60 条；test = human（154 条），在配置冻结后只运行一次。
- **指标**：官方 DR、EPR、LPR、C-LPR、FPR，由官方评测函数计算，与官方命令行结果逐项一致（reports/p0_metric_consistency.md）。
- **统计**：bootstrap 95% CI（1,000 次）；配对比较用精确 McNemar 检验。
- **oracle 隔离**：标注字段只允许在评测代码（`eval/oracle.py` 等 4 个文件）中读取，由测试强制检查。
- **派生集**：POI-Ret（检索评测）和 MT（多轮评测）都由 oracle 派生，dev 与 test 隔离；MT 用 LLM 生成一次后冻结。
- **成本**：每次 API 调用都记入 `runs/cost_ledger.jsonl`，按 DeepSeek 高峰价核算并设上限。

## 实现边界与不做事项

- 不做 v1 的 HTTP API、worker 接入和前端，也不做鉴权和多租户；
- 不训练模型，不用向量数据库或近似索引，不做 HyDE 或多查询改写；
- 主系统不是多 Agent，也不是 LLM 逐步决策的 ReAct；
- 记忆不使用 Redis、PostgreSQL 或向量记忆，也不做衰减模型；
- 不评测 human1000（没有 oracle）和 preference 子集；
- 基线只复现官方 zero-shot ReAct（ReAct0）和 RuleNeSy，其他方法只引用论文数字；
- 由 test 派生的多轮集只有 1 段对话含偏好会话，跨会话偏好的效果主要依据 dev。

## v0 原型（已冻结）

`app/` 下是早期的 v0 原型：FastAPI、LangGraph worker、PostgreSQL 和 Redis 组成的"周末出游管家"。它已冻结，除修复多轮 bug 的一行改动外不再修改。v0 的设计文档在 `docs/` 下，开头都有冻结标注；原先的运行说明见 git 历史中的 README。

审阅时发现的问题：

| 问题 | 位置 |
|---|---|
| 同一会话的第二条消息一定失败（`CHECKPOINT_SCOPE_MISMATCH`），多轮不可用。**已在 T0.3 修复**（`app/agent/worker.py` 一行改动，并补了回归测试 `tests/unit/test_checkpoint_thread_key.py`） | `app/agent/runs.py:63`；`app/agent/worker.py` |
| "向量"是哈希 n-gram 加手写同义词，不是语义模型 | `app/knowledge/embedding.py` |
| RAG 评测没有区分度：只判断"查询里是否出现标题城市名"的规则基线 recall@5 为 1.0，高于系统的 0.9444 | `knowledge/evaluation/travelplanner-validation.json` |
| 线上知识库只有 4 个 chunk；回答是模板字符串 | `knowledge/sources/tripmate-core.json`；`app/agent/graph.py` |
| LLM 只做意图分类和槽位排序；工具选择的结果被丢弃；没有查询改写，prompt 中没有对话历史 | `app/agent/graph.py` |
| citation_locatability 拿同一候选的哈希和自己比较，恒为 1.0 | `app/knowledge/index.py` |

## 数据与许可

- ChinaTravel：代码采用 MIT 许可，数据采用 CC BY 4.0。本项目使用官方仓库 @f445e00、HF 查询数据集 @a7d893a 和沙盒数据 @885c7da，均由安装脚本下载，不随仓库分发。
- 模型：BAAI/bge-small-zh-v1.5、BAAI/bge-reranker-base（版本固定见 versions.json）。

```bibtex
@inproceedings{shao2026chinatravel,
  title     = {ChinaTravel: An Open-Ended Travel Planning Benchmark with Compositional Constraint Validation for Language Agents},
  author    = {Jie-Jing Shao and Bo-Wen Zhang and Xiao-Wen Yang and Baizhi Chen and Siyu Han and Pang Jinghao and Wen-Da Wei and Guohao Cai and Zhenhua Dong and Lan-Zhe Guo and Yu-Feng Li},
  booktitle = {The Fourteenth International Conference on Learning Representations},
  year      = {2026},
  url       = {https://openreview.net/forum?id=0YRVlxY9BH}
}
```

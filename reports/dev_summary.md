# dev 结果汇总（T5.1 冻结前）

- 冻结配置：`configs/planner/full.json`，config_hash `f898a57f6bfae82dcfd957861474dd5b441e0f0a3aaace8d8c913cb269c95f45`，git `e52a7db`（见 reports/final/FREEZE.json）。
- 累计 API 费用（高峰价核算）：2.123 元；按 scope：p0_smoke 0.000，p1 0.635，p3 0.570，p4 0.918。

| 项目 | 结果 | 出处 |
|---|---|---|
| 官方指标封装一致性 | 与官方 CLI 6 项指标一致 | reports/p0_metric_consistency.md |
| 约束抽取（dev 450） | llm+rule F1 0.984 / 精确匹配 0.909；rule F1 0.950 / 0.756 | reports/p1_extraction_dev.md |
| 检索（POI-Ret dev 307） | hybrid nDCG@10 0.624（bm25 0.502 / dense 0.459）；重排 +0.152 但 P95 超限→关闭 | reports/p2_retrieval_dev.md |
| 端到端（dev 450） | FPR 95.56 [93.56, 97.33]；DR 99.33；EPR-macro 99.11 | reports/p3_dev.md |
| 关键消融 | 纯规则改写 82.89（−12.7）；LLM 直出 1.67（dev_mini，full 100） | reports/p3_dev.md |
| 多轮（MT dev 60） | full 98.33（同批单轮 98.33）；关闭记忆 0.00；关闭长期偏好 93.33 | reports/p4_mt_dev.md |
| 延迟 | 单轮 P50 3.7 s / P95 7.6 s（4 线程、机器有其他负载）；瓶颈为规划和检索 | reports/p4_latency.md |
| 每条查询 token（dev，改写） | 约 1238 输入 token（含缓存回放） | reports/p3_dev.md |

## 注意事项

- dev（easy+medium）是模板生成的查询，规则解析和规划器都在 dev 上调过参（允许的范围内），dev 指标偏乐观；human（test）是口语化表达，预计指标会下降。
- 修复环、DSL 校验、检索通道、召回后过滤、特征打分的消融在 dev 上都不显著；按 R14 原样报告。
- 尚未运行任何 test 相关评测（R6）。

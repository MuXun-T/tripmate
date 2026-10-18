# 进度登记

| 时间 | 任务 | run_id / commit | 结果 | 本次费用（高峰价，元） | 累计费用 | 备注 |
|---|---|---|---|---|---|---|
| 2026-10-18 | T0.1 | (本提交) | 分支/骨架/env 脚本/AGENTS.md 完成；旧测试 51 passed 29 skipped | 0 | 0 | |
| 2026-10-18 | T0.2 | (本提交) | .venv-planner/官方仓库@f445e00/沙盒@885c7da/查询@a7d893a/两模型就绪；酒店 3,866 条（论文 4,124，差异如实记录） | 0 | 0 | 偏差：pyarrow 锁 21.0.0（官方 numpy 1.26.4 与 pyarrow≥22 冲突）；hf_xet 卡住→HF_HUB_DISABLE_XET=1，reranker 用 curl 断点续传；新增 tripmate_root.pth 让 tripmate_planner 可导入 |
| 2026-10-18 | T0.3 | (本提交) | worker.py 一行修复 + 回归测试；旧测试 52 passed 29 skipped | 0 | 0 | |
| 2026-10-18 | T0.4 | smoke | LLM 客户端 8 项离线测试通过；冒烟：thinking=disabled、无 reasoning_content | 0.0001 | 0.0001 | 首次冒烟遇 1 次瞬时连接错误，重跑成功 |
| 2026-10-18 | T0.6 | (本提交) | docs/planner/sandbox-schema.md、official-rules.md；check_rules_doc 29 列全覆盖 | 0 | 0.6348 | |
| 2026-10-18 | T0.7 | (本提交) | stats/oracle 隔离测试/离线护栏 | 0 | 0.6348 | T0.5 一致性校验等待 RuleNeSy 跑完，后补 |
| 2026-10-18 | T1.1 | (本提交) | 枚举：景点类型 14、菜系 42、酒店特色 74；TripSpec + 规范化 | 0 | 0.6348 | |
| 2026-10-18 | T1.2 | p1-extract-devmini-llm, -v2 | 规则解析 20 例 + 改写单测通过；dev_mini F1 0.967→0.990（修正单程交通沿用） | 0.093 | 0.6348 | 偏差：新增 meal_price_max_per_person；v1 有 8 条网络瞬断导致退回规则 |
| 2026-10-18 | T1.3 | p1-extract-dev-rule-v2, p1-extract-dev-llm | dev：rule F1 0.950/EM 0.756；llm+rule F1 0.984/EM 0.909；覆盖率 0.999 | 0.539 | 0.6348 | 报告 reports/p1_extraction_dev.md |
| 2026-10-18 | T1.4 | (本提交) | TripSpec→DSL 编译 + 官方执行器；等价性测试通过（16 个同域计划 + 费用×10 变体，30+ 个 uid） | 0 | 0.64 | 新增自定义模板：avoid_cuisines |
| 2026-10-18 | T0.5 | p0-rulenesy-easy20 | 封装与官方 CLI 6 项指标一致（easy 300，RuleNeSy 20 条）；RuleNeSy 不依赖 oracle，可作 test 零成本基线 | 0 | 0.6348 | 偏差：官方 eval_exp 需两处加载器 monkeypatch（见 reports/p0_metric_consistency.md） |
| 2026-10-18 | T2.1–T2.6 | p2-retrieval-dev | 语料 11,934；hybrid nDCG@10 0.624（bm25 0.502 / dense 0.459）；重排 +0.152 但 P95 超限→关闭；召回后过滤 0 不足（负结果） | 0 | 0.64 | 报告 reports/p2_retrieval_dev.md；POI-Ret test 已构建、未评测 |
| 2026-10-19 | T3.6（进行中） | p3-full-dev | dev 全量 full（旧权重）：FPR 94.89 [92.67, 96.89]，DR 99.33，EPR-macro 99.11，门禁通过 | 0.008 | 1.1986 | 期间修正：允许跨夜城际车次、取消返程 14:00 限制和一日游 4 小时限制 |
| 2026-10-19 | T3.6 网格 | p3-grid_*-devmini | w_transit=0.01 三组均 100%；并列时保持默认 w_price=0.002，写回 full.json | 0.01 | 1.1986 | 60 条样本差 2 条属噪声，以 dev 全量复核为准 |
| 2026-10-19 | T4.1–T4.4 | data/mt | SQLite 记忆库 + 15 场景单测；CLI；MT dev 60/60、test 154/154（test 仅 1 条含偏好） | 0.55 | 1.1986 | 人工抽检 dev 30 条：2 条交通方式被篡改，已加检查并重新生成；报告 reports/p4_mt_build.md |
| 2026-10-19 | T3.6 | p3-final-full-dev, p3-A1…A8-dev, p3-A9_llm_direct-devmini | full dev FPR 95.56 [93.56, 97.33]；A3 纯规则 82.89（p≈1e-17）；A9 LLM 直出 1.67（dev_mini）；A1/A2/A4/A5/A7/A8 差异 ≤0.7 个点、不显著 | 0.56 | 2.1228 | A6 按规则不运行（重排已关闭）；报告 reports/p3_dev.md |
| 2026-10-19 | T4.5 | p4-mt-full-dev, M1, M2 | MT dev：full 98.33（等于同批单轮）；M1 0.00；M2 93.33（p=0.25） | 0.37 | 2.1228 | 报告 reports/p4_mt_dev.md |
| 2026-10-19 | T4.6 | - | 规划 46.6%、检索 42.0%；SQLite 读写 P50 58/193 ms（NTFS fuseblk 单次提交约 34 ms），约为 LLM 调用的 1/20 和 1/6 | 0 | 2.1228 | 报告 reports/p4_latency.md |
| 2026-10-19 | T5.1 预检 | p5-precheck-replay-devmini | mypy/ruff/54 测试通过；replay_only 重跑 dev_mini 60/60 计划逐条一致 | 0 | 2.1228 | |

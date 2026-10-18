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

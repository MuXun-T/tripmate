# 进度登记

| 时间 | 任务 | run_id / commit | 结果 | 本次费用（高峰价，元） | 累计费用 | 备注 |
|---|---|---|---|---|---|---|
| 2026-10-18 | T0.1 | (本提交) | 分支/骨架/env 脚本/AGENTS.md 完成；旧测试 51 passed 29 skipped | 0 | 0 | |
| 2026-10-18 | T0.2 | (本提交) | .venv-planner/官方仓库@f445e00/沙盒@885c7da/查询@a7d893a/两模型就绪；酒店 3,866 条（论文 4,124，差异如实记录） | 0 | 0 | 偏差：pyarrow 锁 21.0.0（官方 numpy 1.26.4 与 pyarrow≥22 冲突）；hf_xet 卡住→HF_HUB_DISABLE_XET=1，reranker 用 curl 断点续传；新增 tripmate_root.pth 让 tripmate_planner 可导入 |

# TripMate 工作规则（Codex）
- 项目根目录：/media/zzq/新加卷/tripmate（Ubuntu 22.04，无 Docker，无 GPU）。
- 执行依据：docs/optimization-plan.md。按任务编号顺序执行，遵守其中 R1–R15。
- 每次开始工作先 `source scripts/planner_env.sh`；所有缓存、虚拟环境、模型、数据都放在项目目录内。
- v0 代码（app/、tests/、migrations/、redis/、knowledge/ 及旧 lock 文件）已冻结，除文档点名的例外外不得修改。
- v1 代码在 tripmate_planner/，测试在 tests_planner/，虚拟环境 .venv-planner。
- 密钥只从 LLM_API_KEY 环境变量读取，禁止打印、记录或提交。
- 不确定时停止并询问用户，不要扩大范围。

# P0 官方指标一致性校验

- 计划来源：官方 RuleNeSy（不调用 LLM、不使用 oracle 翻译），在 easy 前 20 条上运行（run_id `p0-rulenesy-easy20`）；easy 其余 280 条为空计划。
- 官方 CLI：`python scripts/run_official_eval_cli.py --splits easy --method RuleNeSy_rule --lang zh`（输出 runs/p0-cli-eval-full.txt）。
- 本项目封装：`compute_official_metrics(easy 300 个 uid, plans)`。

| 指标 | 官方 CLI | 本项目封装 | 是否一致（4 位小数） |
|---|---|---|---|
| DR（Schema Pass Rate） | 6.6667 | 6.6667 | 是 |
| EPR_micro（commonsense micro） | 6.5926 | 6.5926 | 是 |
| EPR_macro（commonsense macro） | 4.6667 | 4.6667 | 是 |
| LPR_micro（logical micro） | 6.2065 | 6.2065 | 是 |
| LPR_macro（logical macro） | 6.0000 | 6.0000 | 是 |
| C_LPR（Conditional LPR） | 4.3699 | 4.3699 | 是 |
| FPR（All pass ratio） | 4.3333 | 4.3333 | 是 |

## 偏差说明

官方 `eval_exp.py`（commit f445e00）在固定的 HF 数据（a7d893a）上无法直接运行，原因有两个：
1. HF CSV 中 `hard_logic_py` 是字符串，官方加载器要求 list，会抛出 ValueError；
2. `eval_exp.py` 的参数里没有 `oracle_translation`，`load_query` 会先剥掉 oracle 字段，评测硬约束时随即 KeyError。
包装脚本只 monkeypatch 了这两处加载逻辑（字符串 → list；评测入口不剥离），第三方文件未做任何修改（R3）。

RuleNeSy 在这 20 条上：常识检查全部通过 14 条；结果用作 `docs/planner/plan-example.json`（uid e20241028160251109186）。
RuleNeSy 不使用 oracle 时内部翻译得到空约束，只按常识规划，因此可以作为零成本官方基线（它读取官方的结构化字段）。

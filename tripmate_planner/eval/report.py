"""Aggregate run metrics into a markdown table with 95% CI and paired McNemar tests vs a baseline run."""

import argparse
import json
from pathlib import Path
from typing import Any

from tripmate_planner.eval.stats import mcnemar_exact
from tripmate_planner.paths import RUNS


def load(run_id: str) -> dict[str, Any]:
    return json.loads((RUNS / run_id / "metrics.json").read_text(encoding="utf-8"))


def table(run_ids: list[str], baseline: str | None, labels: dict[str, str] | None = None) -> str:
    labels = labels or {}
    base = load(baseline) if baseline else None
    rows = ["| 配置 | run_id | n | DR | EPR-macro | LPR-macro | C-LPR | FPR [95% CI] | vs 基线 b01/b10, p | 抽取 F1 | 调用/条 | 输入 token/条 | P50 / P95 ms | 本次费用（高峰价） |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for rid in run_ids:
        m = load(rid)
        o, ci = m["official"], m["ci95_pct"]["FPR"]
        cmp = "-"
        if base is not None and rid != baseline:
            common = [u for u in m["per_uid"] if u in base["per_uid"]]
            b01, b10, p = mcnemar_exact([base["per_uid"][u]["final"] for u in common], [m["per_uid"][u]["final"] for u in common])
            cmp = f"{b01}/{b10}, p={p:.3g}"
        rows.append(f"| {labels.get(rid, m.get('config_name', rid))} | {rid} | {m['n']} | {o['DR']:.2f} | {o['EPR_macro']:.2f} | "
                    f"{o['LPR_macro']:.2f} | {o['C_LPR']:.2f} | {o['FPR']:.2f} [{ci[1]:.2f}, {ci[2]:.2f}] | {cmp} | "
                    f"{m['extraction']['micro']['f1']:.4f} | {m['llm_calls_per_query']} | {m['prompt_tokens_per_query']} | "
                    f"{m['latency_ms']['p50']:.0f} / {m['latency_ms']['p95']:.0f} | {m['cost_peak_this_run']} |")
    return "\n".join(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--baseline", default=None)
    parser.add_argument("--out", required=True)
    parser.add_argument("--title", default="评测结果")
    args = parser.parse_args()
    text = f"# {args.title}\n\nb01 = 基线失败且该配置通过的条数；b10 = 基线通过且该配置失败的条数；p 为双侧精确 McNemar 检验。\n\n" + table(args.runs, args.baseline) + "\n"
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()

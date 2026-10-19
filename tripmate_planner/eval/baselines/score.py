"""Score a baseline run directory (runs/<run_id>/plans) with the official metrics; missing plans count as empty.

Writes runs/<run_id>/metrics.json with the same core keys as run_benchmark.summarize (official, ci95_pct, per_uid,
commonsense_fail_counts) plus cost/token figures taken from the plans and the cost ledger.
"""

import argparse
import json
import statistics
from typing import Any

from tripmate_planner.eval.official_metrics import compute_official_metrics
from tripmate_planner.eval.stats import bootstrap_ci
from tripmate_planner.paths import COST_LEDGER, DATA, RUNS


def score(run_id: str, uids: list[str]) -> dict[str, Any]:
    out = RUNS / run_id
    plans: dict[str, Any] = {}
    for u in uids:
        f = out / "plans" / f"{u}.json"
        if f.exists():
            plans[u] = json.loads(f.read_text(encoding="utf-8"))
    om = compute_official_metrics(uids, plans)
    per = om.per_uid
    ci = {k: [round(100 * x, 2) for x in bootstrap_ci([per[u][k] for u in uids])]
          for k in ("schema", "commonsense", "logic", "final")}
    ledger = [json.loads(line) for line in COST_LEDGER.read_text(encoding="utf-8").splitlines() if line.strip()]
    calls = [r for r in ledger if r["run_id"] == run_id]
    n = len(uids)
    plan_vals = [p for p in plans.values() if isinstance(p, dict)]
    result: dict[str, Any] = {
        "run_id": run_id,
        "n": n,
        "plans_saved": len(plans),
        "plans_with_error": sum(1 for p in plan_vals if "error" in p),
        "official": {k: round(v, 2) for k, v in om.to_dict().items() if isinstance(v, float)},
        "ci95_pct": {"DR": ci["schema"], "EPR_macro": ci["commonsense"], "LPR_macro": ci["logic"], "FPR": ci["final"]},
        "per_uid": per,
        "commonsense_fail_counts": {k: v for k, v in om.commonsense_fail_counts.items() if v},
        "llm_calls_per_query": round(len(calls) / n, 3),
        "prompt_tokens_per_query": round(sum(r["prompt_tokens"] for r in calls) / n, 1),
        "cache_hit_tokens_per_query": round(sum(r["hit_tokens"] for r in calls) / n, 1),
        "completion_tokens_per_query": round(sum(r["completion_tokens"] for r in calls) / n, 1),
        "cost_std_this_run": round(sum(r["cost_std"] for r in calls), 4),
        "cost_peak_this_run": round(sum(r["cost_peak"] for r in calls), 4),
    }
    if plan_vals and any("input_token_count" in p for p in plan_vals):
        result["official_input_token_count_mean"] = round(
            statistics.mean(p.get("input_token_count", 0) for p in plan_vals), 1)
    (out / "metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--uids-file", default=None, help="default: data/splits/test.txt")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    path = args.uids_file or str(DATA / "splits" / "test.txt")
    uids = [u.strip() for u in open(path, encoding="utf-8") if u.strip()][: args.limit]
    r = score(args.run_id, uids)
    print(json.dumps({k: v for k, v in r.items() if k != "per_uid"}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

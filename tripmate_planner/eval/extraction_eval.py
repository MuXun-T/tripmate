"""T1.3: TripSpec extraction quality against oracle-derived labels (rewrite only, no planning)."""

import argparse
import json
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from tripmate_planner.config import config_hash, load_config
from tripmate_planner.data.splits import load_queries
from tripmate_planner.env.enums import get_enums
from tripmate_planner.eval.labels import Label, gold_from_record, spec_to_labels
from tripmate_planner.eval.oracle import load_oracle_records
from tripmate_planner.llm.client import build_client
from tripmate_planner.paths import RUNS
from tripmate_planner.spec.rewrite import rewrite
from tripmate_planner.spec.tripspec import TripSpec


def extraction_metrics(gold: dict[str, set[Label]], pred: dict[str, set[Label]],
                       non_default: dict[str, int], uncovered: dict[str, list[str]]) -> dict[str, Any]:
    per_field: dict[str, Counter[str]] = defaultdict(Counter)
    exact = 0
    for uid, g in gold.items():
        p = pred[uid]
        gold_fields = {lab.field for lab in g}
        for lab in p & g:
            per_field[lab.field]["tp"] += 1
        for lab in p - g:
            per_field[lab.field]["fp"] += 1
        for lab in g - p:
            per_field[lab.field]["fn"] += 1
        if g <= p and not {lab for lab in p - g if lab.field in gold_fields}:
            exact += 1

    def prf(c: Counter[str]) -> dict[str, float]:
        p = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else 0.0
        r = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else 0.0
        f = 2 * p * r / (p + r) if p + r else 0.0
        return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f, 4), **dict(c)}

    total: Counter[str] = Counter()
    for c in per_field.values():
        total.update(c)
    n_constraints = sum(non_default.values())
    n_uncovered = sum(len(v) for v in uncovered.values())
    return {"n": len(gold), "micro": prf(total), "per_field": {k: prf(v) for k, v in sorted(per_field.items())},
            "exact_match_rate": round(exact / len(gold), 4) if gold else 0.0,
            "coverage": round(1 - n_uncovered / n_constraints, 4) if n_constraints else 1.0,
            "uncovered_count": n_uncovered}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", required=True, choices=["dev", "dev_mini"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--mode", choices=["rule", "llm+rule"], default=None)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scope", default="p1")
    args = parser.parse_args()
    cfg = load_config(args.config)
    if args.mode:
        cfg["rewrite"]["mode"] = args.mode
    out_dir = RUNS / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    enums = get_enums()
    llm = None
    if cfg["rewrite"]["mode"] != "rule":
        llm = build_client(cfg["llm"])
        llm.set_context(run_id=args.run_id, scope=args.scope)
    queries = load_queries(args.split)
    records = load_oracle_records([q.uid for q in queries])

    def run_one(q: Any) -> dict[str, Any]:
        started = time.perf_counter()
        res = rewrite(q.text, enums=enums, cfg=cfg, llm=llm)
        return {"uid": q.uid, "spec": res.spec.model_dump(), "source": res.source, "issues": res.issues,
                "ms": (time.perf_counter() - started) * 1000,
                "llm": [r.__dict__ for r in res.llm_records]}

    with ThreadPoolExecutor(max_workers=min(4, int(cfg["runtime"]["max_workers"]))) as pool:
        rows = list(pool.map(run_one, queries))
    gold, pred, nd, unc = {}, {}, {}, {}
    for row in rows:
        g, n_nd, u = gold_from_record(records[row["uid"]])
        gold[row["uid"]], nd[row["uid"]], unc[row["uid"]] = g, n_nd, u
        pred[row["uid"]] = spec_to_labels(TripSpec.model_validate(row["spec"]))
    metrics = extraction_metrics(gold, pred, nd, unc)
    llm_rows = [r for row in rows for r in row["llm"]]
    ms = sorted(row["ms"] for row in rows)
    metrics.update({
        "config_hash": config_hash(cfg), "mode": cfg["rewrite"]["mode"], "split": args.split,
        "sources": dict(Counter(row["source"] for row in rows)),
        "latency_ms_p50": round(ms[len(ms) // 2], 1), "latency_ms_p95": round(ms[int(len(ms) * 0.95) - 1], 1),
        "llm_calls": len(llm_rows),
        "prompt_tokens_mean": round(sum(r["prompt_tokens"] for r in llm_rows) / max(len(rows), 1), 1),
        "completion_tokens_mean": round(sum(r["completion_tokens"] for r in llm_rows) / max(len(rows), 1), 1),
        "cache_hit_tokens_share": round(sum(r["hit_tokens"] for r in llm_rows)
                                        / max(sum(r["prompt_tokens"] for r in llm_rows), 1), 4),
        "cost_peak": round(sum(r["cost_peak"] for r in llm_rows), 4),
        "uncovered_examples": Counter(c.split("\n")[-1][:80] for v in unc.values() for c in v).most_common(10),
    })
    with (out_dir / "specs.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    (out_dir / "extraction_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: metrics[k] for k in ("n", "micro", "exact_match_rate", "coverage", "sources", "cost_peak",
                                               "latency_ms_p50", "latency_ms_p95")}, ensure_ascii=False))


if __name__ == "__main__":
    main()

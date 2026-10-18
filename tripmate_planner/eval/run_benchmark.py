"""T3.6 / T5.3: run the planner graph on a split, then compute official + extraction + cost/latency metrics."""

import argparse
import json
import statistics
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from tripmate_planner.config import config_hash, load_config
from tripmate_planner.data.splits import load_queries
from tripmate_planner.env.enums import get_enums
from tripmate_planner.eval.labels import gold_from_record, spec_to_labels
from tripmate_planner.eval.official_metrics import compute_official_metrics
from tripmate_planner.eval.oracle import load_oracle_records
from tripmate_planner.eval.stats import bootstrap_ci
from tripmate_planner.graph import run_turn
from tripmate_planner.llm.client import build_client
from tripmate_planner.paths import REPORTS, RUNS
from tripmate_planner.retrieval.retriever import Retriever
from tripmate_planner.spec.tripspec import TripSpec


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    return round(v[min(len(v) - 1, max(0, int(round(q * len(v))) - 1))], 1)


def summarize(uids: list[str], plans: dict[str, Any], traces: dict[str, Any], specs: dict[str, Any]) -> dict[str, Any]:
    from tripmate_planner.eval.extraction_eval import extraction_metrics

    om = compute_official_metrics(uids, plans)
    per = om.per_uid
    ci = {k: [round(100 * x, 2) for x in bootstrap_ci([per[u][k] for u in uids])] for k in ("schema", "commonsense", "logic", "final")}
    records = load_oracle_records(uids)
    gold, pred, nd, unc = {}, {}, {}, {}
    for u in uids:
        gold[u], nd[u], unc[u] = gold_from_record(records[u])
        pred[u] = spec_to_labels(TripSpec.model_validate(specs[u])) if specs.get(u) else set()
    llm = [r for u in uids for r in traces[u].get("llm", [])]
    node_ms: dict[str, list[float]] = {}
    for u in uids:
        for n in traces[u].get("nodes", []):
            node_ms.setdefault(n["name"], []).append(n["ms"])
    spec_fail = Counter(t for u in uids for t in (traces[u].get("verify_history") or [{}])[-1].get("spec_failed", []))
    return {
        "official": {k: round(v, 2) for k, v in om.to_dict().items() if isinstance(v, float)},
        "ci95_pct": {"DR": ci["schema"], "EPR_macro": ci["commonsense"], "LPR_macro": ci["logic"], "FPR": ci["final"]},
        "per_uid": per,
        "commonsense_fail_counts": {k: v for k, v in om.commonsense_fail_counts.items() if v},
        "final_spec_fail_counts": dict(spec_fail),
        "extraction": extraction_metrics(gold, pred, nd, unc),
        "final_status": dict(Counter(traces[u].get("final_status") for u in uids)),
        "repair_rounds_mean": round(statistics.mean(traces[u].get("repair_rounds", 0) for u in uids), 3),
        "reflect_rate": round(sum(bool(traces[u].get("reflect_used")) for u in uids) / len(uids), 4),
        "llm_calls_per_query": round(len([r for r in llm if not r["local_cache_hit"]]) / len(uids), 3),
        "llm_calls_per_query_incl_cache": round(len(llm) / len(uids), 3),
        "prompt_tokens_per_query": round(sum(r["prompt_tokens"] for r in llm) / len(uids), 1),
        "completion_tokens_per_query": round(sum(r["completion_tokens"] for r in llm) / len(uids), 1),
        "cost_std_this_run": round(sum(r["cost_std"] for r in llm), 4),
        "cost_peak_this_run": round(sum(r["cost_peak"] for r in llm), 4),
        "latency_ms": {"p50": _pct([traces[u]["total_ms"] for u in uids], 0.5),
                       "p95": _pct([traces[u]["total_ms"] for u in uids], 0.95)},
        "node_ms": {k: {"p50": _pct(v, 0.5), "p95": _pct(v, 0.95), "mean": round(statistics.mean(v), 1)}
                    for k, v in sorted(node_ms.items())},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--split", required=True, choices=["dev", "dev_mini", "test"])
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--max-workers", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--scope", default="p3")
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--config-hash", default=None)
    parser.add_argument("--cache-mode", default=None, choices=["read_write", "replay_only", "off"])
    args = parser.parse_args()
    cfg = load_config(args.config)
    chash = config_hash(cfg)
    if args.split == "test":
        if not args.final or args.config_hash != chash:
            raise SystemExit(f"test split requires --final --config-hash {chash} (rule R6)")
        if args.limit:
            raise SystemExit("--limit is not allowed on test")
        lock = REPORTS / "final" / f"TEST_LOCK_benchmark_{cfg['name']}.json"
        if lock.exists():
            raise SystemExit(f"{lock} exists; test already run (R6)")
    run_id = args.run_id or f"{datetime.now():%Y%m%d-%H%M}-{cfg['name']}-{args.split}"
    out = RUNS / run_id
    (out / "plans").mkdir(parents=True, exist_ok=True)
    (out / "traces").mkdir(parents=True, exist_ok=True)
    (out / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "config_hash").write_text(chash + "\n")
    queries = load_queries(args.split)[: args.limit] if args.limit else load_queries(args.split)
    enums = get_enums()
    retriever = Retriever(cfg["retrieval"])
    llm = None
    if cfg["rewrite"]["mode"] != "rule" or cfg["planner"]["mode"] == "llm_direct":
        llm = build_client(cfg["llm"], cache_mode=args.cache_mode)
        llm.set_context(run_id=run_id, scope=args.scope)
    plans: dict[str, Any] = {}
    traces: dict[str, Any] = {}
    specs: dict[str, Any] = {}

    def one(q: Any) -> None:
        try:
            res = run_turn(q.text, user_id=q.uid, conversation_id=q.uid, cfg=cfg, enums=enums, llm=llm,
                           retriever=retriever, store=None)
            plans[q.uid], traces[q.uid], specs[q.uid] = res.plan or {}, res.trace, res.spec
        except Exception as exc:  # BudgetExceeded must stop everything
            from tripmate_planner.llm.errors import BudgetExceeded

            if isinstance(exc, BudgetExceeded):
                raise
            plans[q.uid], specs[q.uid] = {}, {}
            traces[q.uid] = {"error": f"{type(exc).__name__}: {exc}", "tb": traceback.format_exc()[-2000:],
                             "total_ms": 0.0, "final_status": "error"}
        (out / "plans" / f"{q.uid}.json").write_text(json.dumps(plans[q.uid], ensure_ascii=False), encoding="utf-8")
        (out / "traces" / f"{q.uid}.json").write_text(json.dumps(traces[q.uid], ensure_ascii=False, default=str), encoding="utf-8")

    started = time.time()
    workers = min(4, args.max_workers or int(cfg["runtime"]["max_workers"]))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, queries))
    uids = [q.uid for q in queries]
    metrics = summarize(uids, plans, traces, specs)
    metrics.update({"run_id": run_id, "config_name": cfg["name"], "config_hash": chash, "split": args.split,
                    "n": len(uids), "wall_s": round(time.time() - started, 1)})
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if args.split == "test":
        lock = REPORTS / "final" / f"TEST_LOCK_benchmark_{cfg['name']}.json"
        lock.write_text(json.dumps({"run_id": run_id, "config_hash": chash}), encoding="utf-8")
    print(json.dumps({"run_id": run_id, **metrics["official"], "ci95_FPR": metrics["ci95_pct"]["FPR"],
                      "status": metrics["final_status"], "cost_peak": metrics["cost_peak_this_run"],
                      "lat": metrics["latency_ms"], "wall_s": metrics["wall_s"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

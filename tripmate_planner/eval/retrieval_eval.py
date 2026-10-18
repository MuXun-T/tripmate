"""T2.6: retrieval evaluation on POI-Ret (pure retrieval, filter experiment, latency)."""

import argparse
import copy
import json
import math
import statistics
import time
from collections import defaultdict
from typing import Any

from tripmate_planner.config import load_config
from tripmate_planner.eval.stats import bootstrap_ci
from tripmate_planner.paths import CONFIGS, DATA, REPORTS, RUNS
from tripmate_planner.retrieval.retriever import Retriever, _match, build_subqueries
from tripmate_planner.spec.tripspec import TripSpec

CONFIGS_TO_RUN = {"bm25": ("bm25", False), "dense": ("dense", False), "hybrid": ("hybrid", False),
                  "hybrid+rerank": ("hybrid", True)}


def _retriever(base: dict[str, Any], mode: str, rerank: bool) -> Retriever:
    rcfg = copy.deepcopy(base)
    rcfg["mode"], rcfg["rerank"]["enabled"] = mode, rerank
    return Retriever(rcfg)


def _ndcg10(ranked: list[str], rel: set[str]) -> float:
    dcg = sum(1 / math.log2(i + 2) for i, d in enumerate(ranked[:10]) if d in rel)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(10, len(rel))))
    return dcg / ideal if ideal else 0.0


def pure_retrieval(items: list[dict[str, Any]], retr: Retriever) -> dict[str, Any]:
    per: dict[str, list[dict[str, float]]] = defaultdict(list)
    lat: list[float] = []
    for it in items:
        started = time.perf_counter()
        hits = retr.search(it["city"], it["kind"], it["query_text"], [], 50)
        lat.append((time.perf_counter() - started) * 1000)
        ranked = [retr.docs[d].doc_id for d in hits]
        rel = set(it["relevant_doc_ids"])
        if it["ctype"] == "position":
            first = next((i for i, d in enumerate(ranked[:10], 1) if d in rel), None)
            per["position"].append({"success@1": float(bool(ranked) and ranked[0] in rel),
                                    "mrr@10": 1 / first if first else 0.0, "success@10": float(first is not None)})
        else:
            top = ranked[:10]
            per["type"].append({"success@10": float(any(d in rel for d in top)),
                                "p@10": sum(d in rel for d in top) / 10, "ndcg@10": _ndcg10(ranked, rel)})
    out: dict[str, Any] = {}
    for group, rows in per.items():
        out[group] = {"n": len(rows)}
        for key in rows[0]:
            mean, lo, hi = bootstrap_ci([r[key] for r in rows])
            out[group][key] = {"mean": round(mean, 4), "ci95": [round(lo, 4), round(hi, 4)]}
    all_ndcg = [r["ndcg@10"] for r in per["type"]]
    out["ndcg@10_type"] = round(statistics.mean(all_ndcg), 4) if all_ndcg else 0.0
    lat.sort()
    out["latency_ms"] = {"p50": round(lat[len(lat) // 2], 1), "p95": round(lat[int(len(lat) * 0.95) - 1], 1)}
    return out


def filter_experiment(spec_rows: list[dict[str, Any]], retr: Retriever) -> dict[str, Any]:
    stats: dict[str, dict[str, float]] = {}
    for mode in ("pre", "post", "none"):
        n = short = short_eligible = viol_docs = ret_docs = succ = 0
        for row in spec_rows:
            spec = TripSpec.model_validate(row["spec"])
            if spec.target_city is None:
                continue
            for sq in build_subqueries(spec, row["text"], retr.cfg["pool_sizes"]):
                if sq.constraint_ref is None or sq.constraint_ref.startswith("position:") or not sq.filters:
                    continue
                n += 1
                satisfying = sum(1 for d in retr.docs if d.city == spec.target_city and d.kind == sq.kind
                                 and all(_match(d, f) for f in sq.filters))
                filters = [] if mode == "none" else sq.filters
                hits = retr.search(spec.target_city, sq.kind, sq.text, filters, sq.pool_size,
                                   filter_mode="pre" if mode == "none" else mode)
                ok = [all(_match(retr.docs[d], f) for f in sq.filters) for d in hits]
                ret_docs += len(hits)
                viol_docs += sum(not x for x in ok)
                succ += any(ok)
                if satisfying >= sq.pool_size:
                    short_eligible += 1
                    short += len(hits) < sq.pool_size
        stats[mode] = {"subqueries": n, "shortfall_rate": round(short / short_eligible, 4) if short_eligible else 0.0,
                       "violation_rate": round(viol_docs / ret_docs, 4) if ret_docs else 0.0,
                       "success@pool": round(succ / n, 4) if n else 0.0}
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "test"], required=True)
    parser.add_argument("--specs", required=True, help="specs.jsonl from an extraction run on the same split")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    if args.split == "test" and not args.final:
        raise SystemExit("test split requires --final (rule R6)")
    if args.split == "test":
        lock = REPORTS / "final" / "TEST_LOCK_retrieval.json"
        if lock.exists():
            raise SystemExit(f"{lock} exists; test already evaluated (R6)")
    cfg = load_config(CONFIGS / "full.json")
    items = [json.loads(line) for line in (DATA / "poi_ret" / f"{args.split}.jsonl").read_text(encoding="utf-8").splitlines()]
    from tripmate_planner.data.splits import load_queries

    texts = {q.uid: q.text for q in load_queries(args.split)}
    spec_rows = []
    for line in open(args.specs, encoding="utf-8"):
        row = json.loads(line)
        spec_rows.append({"spec": row["spec"], "text": texts[row["uid"]]})
    result: dict[str, Any] = {"split": args.split, "n_items": len(items), "pure": {}}
    for name, (mode, rerank) in CONFIGS_TO_RUN.items():
        retr = _retriever(cfg["retrieval"], mode, rerank)
        retr.search(items[0]["city"], items[0]["kind"], "预热", [], 5)  # warm-up
        result["pure"][name] = pure_retrieval(items, retr)
        print(name, json.dumps(result["pure"][name], ensure_ascii=False)[:300], flush=True)
    hybrid = _retriever(cfg["retrieval"], "hybrid", False)
    result["filter"] = filter_experiment(spec_rows, hybrid)
    print("filter", result["filter"])
    out = RUNS / args.run_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "retrieval_metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.split == "test":
        (REPORTS / "final" / "TEST_LOCK_retrieval.json").write_text(json.dumps({"run_id": args.run_id}), encoding="utf-8")


if __name__ == "__main__":
    main()

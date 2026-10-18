"""T4.5 / T5.3: multi-turn evaluation with per-dialogue SQLite memory; final plan scored by the official evaluator."""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from tripmate_planner.config import config_hash, load_config
from tripmate_planner.env.enums import get_enums
from tripmate_planner.eval.run_benchmark import summarize
from tripmate_planner.graph import run_turn
from tripmate_planner.llm.client import build_client
from tripmate_planner.memory.store import MemoryStore
from tripmate_planner.paths import DATA, REPORTS, RUNS
from tripmate_planner.retrieval.retriever import Retriever


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--split", choices=["dev", "test"], required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scope", default="p4")
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--config-hash", default=None)
    args = parser.parse_args()
    cfg = load_config(args.config)
    chash = config_hash(cfg)
    lock = REPORTS / "final" / f"TEST_LOCK_mt_{cfg['name']}.json"
    if args.split == "test":
        if not args.final or args.config_hash != chash:
            raise SystemExit(f"test split requires --final --config-hash {chash} (rule R6)")
        if lock.exists():
            raise SystemExit(f"{lock} exists; test already run (R6)")
    dialogues = [json.loads(line) for line in (DATA / "mt" / f"{args.split}.jsonl").read_text(encoding="utf-8").splitlines()]
    out = RUNS / args.run_id
    for sub in ("plans", "traces", "memory"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    (out / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    enums, retriever = get_enums(), Retriever(cfg["retrieval"])
    llm = build_client(cfg["llm"])
    llm.set_context(run_id=args.run_id, scope=args.scope)
    plans: dict[str, Any] = {}
    traces: dict[str, Any] = {}
    specs: dict[str, Any] = {}
    no_memory = not cfg["memory"]["enabled"]

    def one(d: dict[str, Any]) -> None:
        uid = d["uid"]
        store = MemoryStore(out / "memory" / f"{uid}.sqlite3")
        turn_traces = []
        started = time.perf_counter()
        for text in d["preference_session"]:
            r = run_turn(text, user_id=uid, conversation_id="pref", cfg=cfg, enums=enums, llm=llm, retriever=retriever,
                         store=store, plan_needed=False)
            turn_traces.append(r.trace)
        turns = d["turns"]
        if no_memory:  # M1: only the last utterance reaches the system, no state, no preferences
            turns = turns[-1:]
        res = None
        for i, text in enumerate(turns):
            last = i == len(turns) - 1
            if not last and not cfg["memory"]["plan_on_intermediate_turns"]:
                r = run_turn(text, user_id=uid, conversation_id="trip", cfg=cfg, enums=enums, llm=llm,
                             retriever=retriever, store=store, plan_needed=False)
            else:
                r = run_turn(text, user_id=uid, conversation_id="trip", cfg=cfg, enums=enums, llm=llm,
                             retriever=retriever, store=store, plan_needed=last)
            turn_traces.append(r.trace)
            res = r
        assert res is not None
        llm_all = [x for t in turn_traces for x in t.get("llm", [])]
        nodes_all = [x for t in turn_traces for x in t.get("nodes", [])]
        trace = {**res.trace, "llm": llm_all, "nodes": nodes_all, "turns": len(turn_traces),
                 "total_ms": round((time.perf_counter() - started) * 1000, 1),
                 "prefs_stored": len(store.active_preferences(uid))}
        plans[uid], traces[uid], specs[uid] = res.plan or {}, trace, res.spec
        (out / "plans" / f"{uid}.json").write_text(json.dumps(plans[uid], ensure_ascii=False), encoding="utf-8")
        (out / "traces" / f"{uid}.json").write_text(json.dumps(trace, ensure_ascii=False, default=str), encoding="utf-8")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one, dialogues))
    uids = [d["uid"] for d in dialogues]
    metrics = summarize(uids, plans, traces, specs)
    with_pref = [d["uid"] for d in dialogues if d["preference_session"]]
    metrics.update({"run_id": args.run_id, "config_name": cfg["name"], "config_hash": chash, "split": args.split,
                    "n": len(uids), "with_preference": len(with_pref),
                    "prefs_stored_rate": round(sum(traces[u]["prefs_stored"] > 0 for u in with_pref) / max(len(with_pref), 1), 4),
                    "constraint_retention": metrics["extraction"]["micro"]["recall"]})
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if args.split == "test":
        lock.write_text(json.dumps({"run_id": args.run_id, "config_hash": chash}), encoding="utf-8")
    print(json.dumps({"run_id": args.run_id, **metrics["official"], "retention": metrics["constraint_retention"],
                      "prefs_stored_rate": metrics["prefs_stored_rate"], "cost": metrics["cost_peak_this_run"]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Official RuleNeSy baseline (no LLM). Without oracle translation its rule LLM yields no
constraints, so it plans with commonsense only; it reads the official structured query fields."""

import argparse
import copy
import json
import os
import sys
import time

from tripmate_planner.eval.oracle import load_oracle_records
from tripmate_planner.paths import CT_ROOT, DATA, REPORTS, RUNS


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uids-file", required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--final", action="store_true", help="required for the test split (R6)")
    parser.add_argument("--also-official-results", action="store_true",
                        help="copy plans into third_party results/<method>/ for the official CLI check")
    args = parser.parse_args()

    from chinatravel.agent.load_model import create_agent_runtime
    from chinatravel.data.load_datasets import _strip_oracle_fields, save_json_file

    uids = [u.strip() for u in open(args.uids_file, encoding="utf-8") if u.strip()][: args.limit]
    test_uids = {u.strip() for u in (DATA / "splits" / "test.txt").read_text(encoding="utf-8").splitlines() if u.strip()}
    lock = REPORTS / "final" / "TEST_LOCK_rulenesy.json"
    is_test = bool(test_uids & set(uids))
    if is_test and (not args.final or args.limit or lock.exists() or set(uids) != test_uids):
        raise SystemExit("test requires --final, the full test split, no --limit, and no existing lock (R6)")
    os.chdir(CT_ROOT)  # official agents use repository-relative paths
    records = load_oracle_records(uids)
    runtime = create_agent_runtime("RuleNeSy", "rule", project_root_path=str(CT_ROOT), lang="zh",
                                   oracle_translation=False, preference_search=False, refine_steps=10)
    out = RUNS / args.run_id / "plans"
    out.mkdir(parents=True, exist_ok=True)
    timing = {}
    for i, uid in enumerate(uids):
        query = _strip_oracle_fields(copy.deepcopy(records[uid]))
        started = time.perf_counter()
        try:
            succ, plan = runtime.agent.run(query, load_cache=True, oralce_translation=False,
                                           preference_search=False)
        except Exception as exc:  # official agent failure -> empty plan, recorded
            succ, plan = 0, {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            sys.stdout = sys.__stdout__
        timing[uid] = {"succ": bool(succ), "s": round(time.perf_counter() - started, 2)}
        save_json_file(plan, str(out / f"{uid}.json"))
        if args.also_official_results:
            save_json_file(plan, f"{runtime.result_dir}/{uid}.json")
        print(f"[{i + 1}/{len(uids)}] {uid} succ={bool(succ)} {timing[uid]['s']}s", flush=True)
    (RUNS / args.run_id / "timing.json").write_text(json.dumps({"method": runtime.method, "timing": timing},
                                                               ensure_ascii=False, indent=1))
    print("method:", runtime.method)
    if is_test:
        lock.write_text(json.dumps({"run_id": args.run_id}), encoding="utf-8")


if __name__ == "__main__":
    main()

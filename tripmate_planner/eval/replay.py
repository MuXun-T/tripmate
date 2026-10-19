"""T5.4: reproduce the test results from reports/final/llm_replay.jsonl.gz without any API call.

Loads the pack into a fresh cache database, reruns the single-turn and multi-turn test evaluations with
--replay (cache_mode=replay_only, no lock written), and checks that the official metrics, the per-uid pass/fail
vectors and the plan files are identical to the original runs. Result: reports/final/replay_check.json.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tripmate_planner.config import config_hash, load_config
from tripmate_planner.eval.replay_pack import PACK, load
from tripmate_planner.paths import CONFIGS, REPORTS, RUNS


def _compare(orig: str, new: str) -> dict[str, Any]:
    a = json.loads((RUNS / orig / "metrics.json").read_text(encoding="utf-8"))
    b = json.loads((RUNS / new / "metrics.json").read_text(encoding="utf-8"))
    plan_diff = [f.name for f in sorted((RUNS / orig / "plans").glob("*.json"))
                 if f.read_text(encoding="utf-8") != (RUNS / new / "plans" / f.name).read_text(encoding="utf-8")]
    return {"original": orig, "replay": new, "official_equal": a["official"] == b["official"],
            "per_uid_equal": a["per_uid"] == b["per_uid"], "plans_differing": plan_diff,
            "official": b["official"], "api_cost_peak_replay": b["cost_peak_this_run"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True, help="suffix for the replay run ids, e.g. 20261019")
    parser.add_argument("--pack", default=str(PACK))
    args = parser.parse_args()
    chash = config_hash(load_config(CONFIGS / "full.json"))
    work = RUNS / f"replay-{args.tag}"
    work.mkdir(parents=True, exist_ok=False)
    jobs = {
        "p5-full-test": ["tripmate_planner.eval.run_benchmark", "--config", "configs/planner/full.json",
                         "--split", "test", "--scope", "p5_ours"],
        "p5-mt-full-test": ["tripmate_planner.eval.mt_eval", "--config", "configs/planner/full.json",
                            "--split", "test", "--scope", "p5_ours"],
    }
    results = []
    for orig, cmd in jobs.items():
        new = f"{orig}-replay-{args.tag}"
        db = work / f"{orig}.sqlite3"  # only the responses recorded by this run
        print(orig, "loaded", load(Path(args.pack), db, run=orig), flush=True)
        env = {**os.environ, "TRIPMATE_LLM_CACHE_DB": str(db)}
        with (work / f"{orig}.log").open("w", encoding="utf-8") as log:
            subprocess.run([sys.executable, "-m", *cmd, "--replay", "--config-hash", chash, "--run-id", new],
                           env=env, check=True, stdout=log, stderr=subprocess.STDOUT)
        results.append(_compare(orig, new))
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    ok = all(r["official_equal"] and r["per_uid_equal"] for r in results)
    (REPORTS / "final" / "replay_check.json").write_text(
        json.dumps({"pack": str(Path(args.pack).relative_to(REPORTS.parent)), "config_hash": chash,
                    "all_metrics_identical": ok, "runs": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("ALL METRICS IDENTICAL" if ok else "MISMATCH", flush=True)
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

"""T5.4: export the LLM responses used by the test runs into reports/final/llm_replay.jsonl.gz, and load such a
pack into a fresh cache database for replay_only reproduction.

Each exported row is labelled with the test run that created it: the window of a run spans from its
config.json to its metrics.json. Only this project's client writes the cache, so every row in those windows
belongs to exactly one run. A replay loads only the rows of the run it reproduces, so a response that a later
run fetched (after the original request had failed) cannot change the replayed result.
Rows hold the request body (model, messages, sampling args), response content and usage; never the API key.
"""

import argparse
import gzip
import json
import re
import sqlite3
from pathlib import Path

from tripmate_planner.llm.cache import ResponseCache, request_key
from tripmate_planner.paths import LLM_CACHE_DB, REPORTS, RUNS

PACK = REPORTS / "final" / "llm_replay.jsonl.gz"


TEST_RUNS = ("p5-full-test", "p5-mt-full-test", "p5-extract-test")


def _windows(runs: tuple[str, ...]) -> dict[str, tuple[float, float]]:
    """Runs executed one after another (scripts/run_final_test.sh ours); a run without config.json starts
    where the previous one ended."""
    out: dict[str, tuple[float, float]] = {}
    prev_end = 0.0
    for r in runs:
        d = RUNS / r
        end = (d / "metrics.json" if (d / "metrics.json").exists() else d / "extraction_metrics.json").stat().st_mtime
        start = (d / "config.json").stat().st_mtime if (d / "config.json").exists() else prev_end
        if start < prev_end or end < start:
            raise ValueError(f"run windows overlap or are out of order at {r}")
        out[r] = (start, end)
        prev_end = end
    return out


def export(runs: tuple[str, ...] = TEST_RUNS, out: Path = PACK) -> dict[str, int]:
    windows = _windows(runs)
    start, end = windows[runs[0]][0] - 1, windows[runs[-1]][1] + 1
    with sqlite3.connect(LLM_CACHE_DB) as conn:
        rows = conn.execute("SELECT key, request_json, content, usage_json, created_at FROM response "
                            "WHERE created_at >= ? AND created_at <= ? ORDER BY created_at", (start, end)).fetchall()
    counts: dict[str, int] = {}
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        for key, req, content, usage, created in rows:
            owners = [r for r, (a, b) in windows.items() if a <= created <= b] or \
                [r for r, (a, b) in windows.items() if a - 1 <= created <= b + 1][:1]
            if len(owners) != 1:
                raise ValueError(f"cache row {key} at {created} belongs to {owners or 'no run'}")
            counts[owners[0]] = counts.get(owners[0], 0) + 1
            request = json.loads(req)
            if request_key(request) != key:
                raise ValueError(f"cache row {key} does not match its request body")
            if "api_key" in req or re.search(r"sk-[A-Za-z0-9]{16,}", req):
                raise ValueError("refusing to export a row that looks like it contains a key")
            fh.write(json.dumps({"key": key, "run": owners[0], "request": request, "content": content,
                                 "usage": json.loads(usage)}, ensure_ascii=False, sort_keys=True) + "\n")
    return counts


def load(pack: Path, db: Path, run: str | None = None) -> int:
    if db.exists():
        raise SystemExit(f"{db} exists; use a fresh path")
    cache = ResponseCache(path=db, mode="read_write")
    n = 0
    with gzip.open(pack, "rt", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if run is not None and row["run"] != run:
                continue
            cache.put(row["key"], row["request"], row["content"], row["usage"], 0.0)
            n += 1
    return n


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export")
    l_ = sub.add_parser("load")
    l_.add_argument("--pack", default=str(PACK))
    l_.add_argument("--db", required=True)
    l_.add_argument("--run", default=None)
    args = parser.parse_args()
    if args.cmd == "export":
        print("exported", export())
    else:
        print("loaded", load(Path(args.pack), Path(args.db), args.run))


if __name__ == "__main__":
    main()

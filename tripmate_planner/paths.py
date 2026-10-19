"""Fixed project paths (everything stays inside the project root)."""

import os
from pathlib import Path

ROOT = Path("/media/zzq/新加卷/tripmate")
RUNS = ROOT / "runs"
CACHE = ROOT / ".cache"
CONFIGS = ROOT / "configs" / "planner"
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
CT_ROOT = ROOT / "third_party" / "ChinaTravel"
COST_LEDGER = RUNS / "cost_ledger.jsonl"
# TRIPMATE_LLM_CACHE_DB lets a replay run read a cache rebuilt from reports/final/llm_replay.jsonl.gz.
LLM_CACHE_DB = Path(os.environ.get("TRIPMATE_LLM_CACHE_DB") or CACHE / "llm" / "responses.sqlite3")

"""Fixed project paths (everything stays inside the project root)."""

from pathlib import Path

ROOT = Path("/media/zzq/新加卷/tripmate")
RUNS = ROOT / "runs"
CACHE = ROOT / ".cache"
CONFIGS = ROOT / "configs" / "planner"
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
CT_ROOT = ROOT / "third_party" / "ChinaTravel"
COST_LEDGER = RUNS / "cost_ledger.jsonl"
LLM_CACHE_DB = CACHE / "llm" / "responses.sqlite3"

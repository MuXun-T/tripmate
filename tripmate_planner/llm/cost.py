"""DeepSeek deepseek-flash cost accounting and spending caps (peak-price based)."""

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from tripmate_planner.llm.errors import BudgetExceeded
from tripmate_planner.paths import COST_LEDGER

# 元 / token
STD = {"miss": 1e-6, "hit": 0.02e-6, "out": 4e-6}
PEAK = {"miss": 2e-6, "hit": 0.04e-6, "out": 8e-6}
SCOPE_CAPS = {
    "p0_smoke": 1.0,
    "p1": 10.0,
    "p3": 25.0,
    "p4": 10.0,
    "p5_ours": 5.0,
    "p5_baseline": 30.0,
}
TOTAL_CAP = 100.0


def cost_of(hit: int, miss: int, out: int, prices: dict[str, float]) -> float:
    return hit * prices["hit"] + miss * prices["miss"] + out * prices["out"]


@dataclass
class CallRecord:
    ts: float
    run_id: str
    scope: str
    purpose: str
    local_cache_hit: bool
    prompt_tokens: int
    hit_tokens: int
    miss_tokens: int
    completion_tokens: int
    cost_std: float
    cost_peak: float
    ms: float


class CostLedger:
    def __init__(self, path: Path = COST_LEDGER) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, record: CallRecord) -> None:
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    def totals(self) -> tuple[dict[str, float], float]:
        per_scope: dict[str, float] = {}
        total = 0.0
        if not self.path.exists():
            return per_scope, total
        with self._lock:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                per_scope[row["scope"]] = per_scope.get(row["scope"], 0.0) + row["cost_peak"]
                total += row["cost_peak"]
        return per_scope, total


class Budget:
    def __init__(self, ledger: CostLedger, caps: dict[str, float] | None = None,
                 total_cap: float = TOTAL_CAP) -> None:
        self.ledger = ledger
        self.caps = caps or SCOPE_CAPS
        self.total_cap = total_cap

    def check(self, scope: str, estimate_peak: float) -> None:
        if scope not in self.caps:
            raise BudgetExceeded(f"unknown cost scope {scope!r}")
        per_scope, total = self.ledger.totals()
        used = per_scope.get(scope, 0.0)
        if used + estimate_peak > self.caps[scope]:
            raise BudgetExceeded(f"scope {scope} would exceed cap {self.caps[scope]} (used {used:.4f})")
        if total + estimate_peak > self.total_cap:
            raise BudgetExceeded(f"project total would exceed cap {self.total_cap} (used {total:.4f})")


def now() -> float:
    return time.time()

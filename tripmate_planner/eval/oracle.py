"""The ONLY entry point that loads evaluator-only oracle fields (hard_logic*). Rule R4."""

import ast
import json
from functools import lru_cache
from typing import Any

from tripmate_planner.paths import CONFIGS

HF_SPLITS = ("easy", "medium", "human")


@lru_cache(maxsize=4)
def _records(split: str) -> dict[str, dict[str, Any]]:
    from datasets import load_dataset

    q = json.loads((CONFIGS / "versions.json").read_text(encoding="utf-8"))["query_dataset"]
    ds = load_dataset(q["repo"], name="default", revision=q["revision"])[split]
    out: dict[str, dict[str, Any]] = {}
    for row in ds:
        rec = dict(row)
        logic = rec.get("hard_logic_py")
        if isinstance(logic, str):
            rec["hard_logic_py"] = ast.literal_eval(logic)
        for key in ("days", "people_number"):
            if key in rec and rec[key] is not None:
                rec[key] = int(rec[key])
        out[str(rec["uid"])] = rec
    return out


def load_oracle_records(uids: list[str]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for split in HF_SPLITS:
        merged.update(_records(split))
    missing = [u for u in uids if u not in merged]
    if missing:
        raise KeyError(f"unknown uids: {missing[:3]}")
    return {u: merged[u] for u in uids}


def split_of(uid: str) -> str:
    for split in HF_SPLITS:
        if uid in _records(split):
            return split
    raise KeyError(uid)

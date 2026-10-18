"""Agent-side query loading: returns uid + nature_language only (rule R5)."""

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from tripmate_planner.paths import CONFIGS, DATA

HF_SPLITS = ("easy", "medium", "human")


@dataclass(frozen=True)
class QueryText:
    uid: str
    text: str


@lru_cache(maxsize=1)
def _versions() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((CONFIGS / "versions.json").read_text(encoding="utf-8"))
    return data


@lru_cache(maxsize=4)
def _hf_split(split: str) -> tuple[QueryText, ...]:
    from datasets import load_dataset

    q = _versions()["query_dataset"]
    ds = load_dataset(q["repo"], name="default", revision=q["revision"])[split]
    return tuple(QueryText(uid=str(r["uid"]), text=str(r["nature_language"])) for r in ds)


def uid_list(name: str) -> list[str]:
    return [line.strip() for line in (DATA / "splits" / f"{name}.txt").read_text(encoding="utf-8").splitlines()
            if line.strip()]


def load_queries(split: str) -> list[QueryText]:
    if split in HF_SPLITS:
        return list(_hf_split(split))
    by_uid = {q.uid: q for s in HF_SPLITS for q in _hf_split(s)}
    names = {"dev": "dev", "dev_mini": "dev_mini", "test": "test"}
    if split not in names:
        raise ValueError(f"unknown split {split!r}")
    return [by_uid[u] for u in uid_list(names[split])]

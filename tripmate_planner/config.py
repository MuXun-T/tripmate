"""Planner configuration loading: full.json plus optional ablation overrides."""

import hashlib
import json
from pathlib import Path
from typing import Any

from tripmate_planner.paths import CONFIGS


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | Path) -> dict[str, Any]:
    """Load a config; files under ablations/ are merged on top of full.json."""
    path = Path(path)
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if path.parent.name == "ablations":
        full = json.loads((CONFIGS / "full.json").read_text(encoding="utf-8"))
        data = deep_merge(full, data)
    return data


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def config_hash(cfg: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(cfg).encode("utf-8")).hexdigest()

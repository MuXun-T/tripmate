"""SQLite response cache for deterministic replay of LLM calls (never stores the API key)."""

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Literal

from tripmate_planner.paths import LLM_CACHE_DB

CacheMode = Literal["read_write", "replay_only", "off"]


def request_key(request: dict[str, Any]) -> str:
    canonical = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ResponseCache:
    def __init__(self, path: Path = LLM_CACHE_DB, mode: CacheMode = "read_write") -> None:
        self.mode = mode
        self.path = path
        self._lock = threading.Lock()
        if mode != "off":
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS response(key TEXT PRIMARY KEY, request_json TEXT NOT NULL,"
                    " content TEXT NOT NULL, usage_json TEXT NOT NULL, created_at REAL NOT NULL)"
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def get(self, key: str) -> tuple[str, dict[str, Any]] | None:
        if self.mode == "off":
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT content, usage_json FROM response WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        return row[0], json.loads(row[1])

    def put(self, key: str, request: dict[str, Any], content: str, usage: dict[str, Any], ts: float) -> None:
        if self.mode != "read_write":
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO response VALUES (?,?,?,?,?)",
                (key, json.dumps(request, ensure_ascii=False, sort_keys=True), content,
                 json.dumps(usage), ts),
            )

    def export(self, keys: set[str]) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT key, request_json, content, usage_json FROM response").fetchall()
        return [
            {"key": k, "request": json.loads(r), "content": c, "usage": json.loads(u)}
            for k, r, c, u in rows
            if k in keys
        ]

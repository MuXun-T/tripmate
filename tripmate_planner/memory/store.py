"""SQLite memory: per-turn versioned conversation state + versioned long-term preferences (T4.1)."""

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_turn(
  conversation_id TEXT NOT NULL, turn_no INTEGER NOT NULL, user_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
  created_at TEXT NOT NULL, PRIMARY KEY(conversation_id, turn_no, role));
CREATE TABLE IF NOT EXISTS conversation_state(
  conversation_id TEXT NOT NULL, turn_no INTEGER NOT NULL, user_id TEXT NOT NULL,
  trip_spec_json TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(conversation_id, turn_no));
CREATE TABLE IF NOT EXISTS user_preference(
  pref_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, key TEXT NOT NULL, value_json TEXT NOT NULL,
  source TEXT NOT NULL CHECK(source='explicit_confirmed'), evidence TEXT NOT NULL,
  valid_from TEXT NOT NULL, valid_to TEXT, superseded_by TEXT, deleted_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_pref ON user_preference(user_id, key)
  WHERE valid_to IS NULL AND deleted_at IS NULL;
"""

MODES_INNER = {"metro", "taxi", "walk"}
PREF_KEYS = {"innercity_mode", "intercity_mode", "cuisines_prefer", "cuisines_avoid", "hotel_features",
             "room_type", "pace"}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def validate_preference(key: str, value: Any, enums: Any | None = None) -> Any:
    if key not in PREF_KEYS:
        raise ValueError(f"preference key not allowed: {key}")
    if key == "innercity_mode" and value in MODES_INNER:
        return value
    if key == "intercity_mode" and value in {"train", "airplane"}:
        return value
    if key == "room_type" and value in (1, 2):
        return int(value)
    if key == "pace" and value in {"relaxed", "normal", "intense"}:
        return value
    if key in {"cuisines_prefer", "cuisines_avoid", "hotel_features"}:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
            raise ValueError(f"invalid list value for {key}")
        if enums is not None:
            allowed = set(enums.union("cuisines" if key.startswith("cuisines") else "hotel_features"))
            if not set(value) <= allowed:
                raise ValueError(f"non-enum value for {key}: {value}")
        return sorted(set(value))
    raise ValueError(f"invalid value for {key}: {value!r}")


class MemoryStore:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    # ---- conversation ----
    def append_turn(self, conversation_id: str, turn_no: int, user_id: str, role: str, content: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO conversation_turn VALUES (?,?,?,?,?,?)",
                      (conversation_id, turn_no, user_id, role, content, now_iso()))

    def save_state(self, conversation_id: str, turn_no: int, user_id: str, spec: dict[str, Any]) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO conversation_state VALUES (?,?,?,?,?)",
                      (conversation_id, turn_no, user_id, json.dumps(spec, ensure_ascii=False), now_iso()))

    def latest_state(self, conversation_id: str, user_id: str) -> tuple[int, dict[str, Any]] | None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT turn_no, trip_spec_json FROM conversation_state WHERE conversation_id=? AND user_id=?"
                            " ORDER BY turn_no DESC LIMIT 1", (conversation_id, user_id)).fetchone()
        return (int(row[0]), json.loads(row[1])) if row else None

    def next_turn_no(self, conversation_id: str) -> int:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT MAX(turn_no) FROM conversation_turn WHERE conversation_id=?", (conversation_id,)).fetchone()
        return int(row[0] or 0) + 1

    def recent_turns(self, conversation_id: str, user_id: str, max_turns: int = 6, max_chars: int = 1800) -> list[str]:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT turn_no, role, content FROM conversation_turn WHERE conversation_id=? AND user_id=?"
                             " ORDER BY turn_no DESC, role DESC", (conversation_id, user_id)).fetchall()
        turns = sorted({r[0] for r in rows}, reverse=True)[:max_turns]
        lines: list[str] = []
        for turn, role, content in rows:
            if turn not in turns:
                continue
            line = ("用户：" if role == "user" else "助手：") + content
            if sum(len(x) for x in lines) + len(line) > max_chars:
                break
            lines.insert(0, line)
        return lines

    # ---- preferences ----
    def add_preference(self, user_id: str, key: str, value: Any, evidence: str, enums: Any | None = None,
                       at: str | None = None) -> str:
        value = validate_preference(key, value, enums)
        ts = at or now_iso()
        with self._lock, self._conn() as c:
            old = c.execute("SELECT pref_id, value_json FROM user_preference WHERE user_id=? AND key=?"
                            " AND valid_to IS NULL AND deleted_at IS NULL", (user_id, key)).fetchone()
            if old and json.loads(old[1]) == value:
                return str(old[0])
            pref_id = uuid.uuid4().hex
            if old:
                c.execute("UPDATE user_preference SET valid_to=?, superseded_by=? WHERE pref_id=?", (ts, pref_id, old[0]))
            c.execute("INSERT INTO user_preference VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (pref_id, user_id, key, json.dumps(value, ensure_ascii=False), "explicit_confirmed", evidence,
                       ts, None, None, None))
        return pref_id

    def delete_preference(self, user_id: str, key: str, at: str | None = None) -> bool:
        with self._lock, self._conn() as c:
            cur = c.execute("UPDATE user_preference SET deleted_at=? WHERE user_id=? AND key=? AND valid_to IS NULL"
                            " AND deleted_at IS NULL", (at or now_iso(), user_id, key))
            return cur.rowcount > 0

    def active_preferences(self, user_id: str, as_of: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._conn() as c:
            if as_of is None:
                rows = c.execute("SELECT key, value_json, evidence, valid_from FROM user_preference WHERE user_id=?"
                                 " AND valid_to IS NULL AND deleted_at IS NULL ORDER BY key", (user_id,)).fetchall()
            else:
                rows = c.execute("SELECT key, value_json, evidence, valid_from FROM user_preference WHERE user_id=?"
                                 " AND valid_from<=? AND (valid_to IS NULL OR valid_to>?)"
                                 " AND (deleted_at IS NULL OR deleted_at>?) ORDER BY key",
                                 (user_id, as_of, as_of, as_of)).fetchall()
        return [{"key": k, "value": json.loads(v), "evidence": e, "valid_from": f} for k, v, e, f in rows]

    def preference_history(self, user_id: str, key: str) -> list[dict[str, Any]]:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT pref_id, value_json, valid_from, valid_to, superseded_by, deleted_at FROM user_preference"
                             " WHERE user_id=? AND key=? ORDER BY valid_from", (user_id, key)).fetchall()
        return [{"pref_id": p, "value": json.loads(v), "valid_from": f, "valid_to": t, "superseded_by": s,
                 "deleted_at": d} for p, v, f, t, s, d in rows]

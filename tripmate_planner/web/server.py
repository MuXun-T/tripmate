"""TripMate workbench: a local stdlib HTTP server around run_turn (no framework, no build step).

    python -m tripmate_planner.web.server            # http://127.0.0.1:8765
    python -m tripmate_planner.web.server --rule     # no API calls: rule-based understanding only

Conversations are kept as JSON under runs/web/conversations/; trip state and long-term preferences live in the
planner's own SQLite memory (runs/web/memory.sqlite3). Preferences are confirmed in two steps: a turn returns the
candidates the LLM proposed, and the page writes the ones the user accepts (the same rule as confirm_memory:
nothing is stored without a yes).
"""

import argparse
import json
import mimetypes
import os
import threading
import time
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from tripmate_planner.config import config_hash, load_config
from tripmate_planner.paths import CONFIGS, RUNS
from tripmate_planner.web.view import build_result, history_rows, preference_rows

STATIC = Path(__file__).parent / "static"
USER = "demo"
MAX_TEXT = 800


class Workbench:
    """Holds the loaded planner (config, enums, retriever, LLM client, memory) and the saved conversations."""

    def __init__(self, data_dir: Path, *, rule_only: bool = False, runner: Any = None) -> None:
        self.data_dir = data_dir
        (data_dir / "conversations").mkdir(parents=True, exist_ok=True)
        self.cfg = load_config(CONFIGS / "full.json")
        frozen = config_hash(self.cfg)[:8]  # the frozen config, before the one interactive switch below
        self.cfg["memory"]["auto_confirm"] = False  # the page asks the user instead
        self.rule_only = rule_only or not os.environ.get("LLM_API_KEY")
        self.engine = {"model": self.cfg["llm"]["model"], "mode": "rule" if self.rule_only else "llm",
                       "config_hash": frozen}
        self._lock = threading.Lock()
        self._runner = runner
        self._loaded: dict[str, Any] | None = None

    # ---------- planner ----------
    def _planner(self) -> dict[str, Any]:
        if self._loaded is None:
            from tripmate_planner.env.enums import get_enums
            from tripmate_planner.llm.client import build_client
            from tripmate_planner.memory.store import MemoryStore
            from tripmate_planner.retrieval.retriever import Retriever

            llm = None
            if not self.rule_only:
                llm = build_client(self.cfg["llm"])
                llm.set_context(run_id="web", scope="p4")
            self._loaded = {"enums": get_enums(), "retriever": Retriever(self.cfg["retrieval"]), "llm": llm,
                            "store": MemoryStore(self.data_dir / "memory.sqlite3")}
        return self._loaded

    def _run(self, text: str, conversation_id: str) -> dict[str, Any]:
        if self._runner is not None:  # tests inject a fake runner
            result: dict[str, Any] = self._runner(text, conversation_id)
            return result
        from tripmate_planner.graph import run_turn

        p = self._planner()
        candidates: list[dict[str, Any]] = []

        def confirm(cands: list[dict[str, Any]]) -> list[int]:
            candidates.extend(cands)
            return []  # decided later on the page

        res = run_turn(text, user_id=USER, conversation_id=conversation_id, cfg=self.cfg, enums=p["enums"],
                       llm=p["llm"], retriever=p["retriever"], store=p["store"], confirm=confirm)
        return build_result(plan=res.plan, status=res.status, spec=res.spec, trace=res.trace, candidates=candidates)

    # ---------- conversations ----------
    def _path(self, cid: str) -> Path:
        if not cid.replace("-", "").isalnum():
            raise ValueError("bad conversation id")
        return self.data_dir / "conversations" / f"{cid}.json"

    def load(self, cid: str) -> dict[str, Any] | None:
        path = self._path(cid)
        if not path.exists():
            return None
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def save(self, conv: dict[str, Any]) -> None:
        self._path(conv["id"]).write_text(json.dumps(conv, ensure_ascii=False), encoding="utf-8")

    def conversations(self) -> list[dict[str, Any]]:
        rows = []
        for f in (self.data_dir / "conversations").glob("*.json"):
            c = json.loads(f.read_text(encoding="utf-8"))
            last = next((t for t in reversed(c["turns"]) if t["role"] == "assistant"), None)
            rows.append({"id": c["id"], "title": c["title"], "updated_at": c["updated_at"],
                         "status": last["result"]["status"] if last else None})
        return sorted(rows, key=lambda r: r["updated_at"], reverse=True)[:12]

    def turn(self, cid: str | None, text: str) -> dict[str, Any]:
        text = text.strip()[:MAX_TEXT]
        if not text:
            raise ValueError("empty message")
        with self._lock:
            conv = self.load(cid) if cid else None
            if conv is None:
                conv = {"id": uuid.uuid4().hex[:12], "title": "新行程", "turns": []}
            now = datetime.now().isoformat(timespec="seconds")
            conv["turns"].append({"role": "user", "text": text, "at": now})
            started = time.perf_counter()
            result = self._run(text, conv["id"])
            result["wall_ms"] = round((time.perf_counter() - started) * 1000, 1)
            conv["turns"].append({"role": "assistant", "at": datetime.now().isoformat(timespec="seconds"),
                                  "result": result})
            conv["title"] = result["title"] if result["title"] != "新行程" else conv["title"]
            conv["updated_at"] = conv["turns"][-1]["at"]
            self.save(conv)
        return conv

    # ---------- preferences ----------
    def preferences(self) -> list[dict[str, Any]]:
        if self._runner is not None and self._loaded is None:
            return []
        return preference_rows(self._planner()["store"].active_preferences(USER))

    def history(self) -> list[dict[str, Any]]:
        if self._runner is not None and self._loaded is None:
            return []
        from tripmate_planner.memory.store import PREF_KEYS

        store = self._planner()["store"]
        return history_rows([(k, e) for k in sorted(PREF_KEYS) for e in store.preference_history(USER, k)])

    def decide(self, cid: str, turn: int, index: int, accept: bool) -> dict[str, Any]:
        with self._lock:
            conv = self.load(cid)
            if conv is None:
                raise KeyError(cid)
            cand = conv["turns"][turn]["result"]["candidates"][index]
            if cand["decision"] is None:
                if accept:
                    p = self._planner()
                    p["store"].add_preference(USER, cand["key"], cand["value"], cand.get("evidence", ""), p["enums"])
                cand["decision"] = "accepted" if accept else "declined"
                self.save(conv)
        return conv

    def forget(self, key: str) -> None:
        with self._lock:
            self._planner()["store"].delete_preference(USER, key)

    def state(self, cid: str | None) -> dict[str, Any]:
        return {"engine": self.engine, "user": USER, "conversations": self.conversations(),
                "conversation": self.load(cid) if cid else None, "preferences": self.preferences(),
                "history": self.history()}


def make_handler(app: Workbench) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "TripMate"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
            return

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/api/state":
                ids = parse_qs(url.query).get("conversation") or []
                cid = ids[0] if ids else None
                try:
                    self._json(HTTPStatus.OK, app.state(cid))
                except ValueError as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if url.path in ("/", "/index.html"):
                name = "index.html"
            elif url.path.startswith("/static/"):
                name = url.path.removeprefix("/static/")
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            path = (STATIC / name).resolve()
            if STATIC.resolve() not in path.parents or not path.is_file():  # no escaping the static folder
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(HTTPStatus.OK, path.read_bytes(), ctype)

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 64_000)) or b"{}")
                path = urlparse(self.path).path
                if path == "/api/turn":
                    self._json(HTTPStatus.OK, {"conversation": app.turn(body.get("conversation_id"), str(body.get("text", "")))})
                elif path == "/api/preferences/decide":
                    conv = app.decide(str(body["conversation_id"]), int(body["turn"]), int(body["index"]),
                                      bool(body["accept"]))
                    self._json(HTTPStatus.OK, {"conversation": conv, "preferences": app.preferences(), "history": app.history()})
                elif path == "/api/preferences/forget":
                    app.forget(str(body["key"]))
                    self._json(HTTPStatus.OK, {"preferences": app.preferences(), "history": app.history()})
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except (ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": f"{type(exc).__name__}: {exc}"})
            except Exception as exc:  # the planner failed; the page shows this inside the block
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(exc).__name__}: {str(exc)[:200]}"})

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(prog="tripmate_planner.web.server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--rule", action="store_true", help="rule-based understanding only (no API calls)")
    parser.add_argument("--data", default=str(RUNS / "web"))
    args = parser.parse_args()
    app = Workbench(Path(args.data), rule_only=args.rule)
    print(f"加载检索模型与沙盒数据……（{'规则模式' if app.rule_only else 'LLM 模式'}）", flush=True)
    app._planner()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(f"TripMate 工作台：http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

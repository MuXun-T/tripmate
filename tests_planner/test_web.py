"""Workbench: view-model labels and the stdlib HTTP server, offline (a fake runner stands in for run_turn)."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from tripmate_planner.web.server import Workbench, make_handler
from tripmate_planner.web.view import (
    commonsense_report,
    constraint_rows,
    describe_action,
    history_rows,
    repair_rows,
    spec_rows,
    title_of,
    value_text,
)

SPEC = {"start_city": "上海", "target_city": "杭州", "days": 2, "people_number": 2, "budget_total": 3000.0,
        "innercity_mode": "metro", "cuisines": ["江浙菜"], "pace": "normal",
        "field_sources": {"start_city": "rule", "target_city": "rule", "days": "rule", "people_number": "rule",
                          "budget_total": "llm", "innercity_mode": "memory", "cuisines": "llm"}}


def test_spec_rows_label_value_and_source() -> None:
    rows = {r["key"]: r for r in spec_rows(SPEC)}
    assert rows["budget_total"]["value"] == "3,000.00 元" and rows["budget_total"]["source_label"] == "LLM 理解"
    assert rows["innercity_mode"]["value"] == "地铁" and rows["innercity_mode"]["source_label"] == "长期偏好"
    assert "pace" not in rows  # the default pace is not a requirement the user stated
    assert value_text("room_type", 2) == "两张床" and title_of(SPEC) == "杭州 2 日行程" and title_of({}) == "新行程"


def test_constraints_follow_the_last_check() -> None:
    rows = {r["tag"]: r for r in constraint_rows(SPEC, ["budget"])}
    assert rows["budget"]["passed"] is False and rows["budget"]["label"] == "总预算"
    assert rows["days"]["passed"] is True and rows["cuisines"]["passed"] is True


def test_repairs_history_and_empty_checks() -> None:
    trace = {"verify_history": [{"round": 0, "reflect": False, "n_failures": 2}, {"round": 1, "reflect": False, "n_failures": 0}],
             "repair_actions": [["mode_override=taxi", "force must_visit"]]}
    assert repair_rows(trace) == [{"round": 1, "reflect": False, "failures": 2,
                                   "actions": ["市内交通改为打车", "把要求的必去景点强制排进行程"]}]
    assert describe_action("price_caps={'hotel': 200}") == "压低酒店、餐饮和门票的价格上限"
    pending = commonsense_report(None, SPEC)
    assert len(pending) == 8 and all(c["passed"] is None for c in pending)
    hist = history_rows([("pace", {"value": "relaxed", "valid_from": "2026-01-01", "valid_to": "2026-02-01", "superseded_by": "x"}),
                         ("pace", {"value": "intense", "valid_from": "2026-02-01", "valid_to": None, "superseded_by": None})])
    assert [h["status"] for h in hist] == ["生效中", "已被新值替换"] and hist[0]["value_text"] == "紧凑"


def _fake_runner(text: str, cid: str) -> dict[str, Any]:
    return {"status": "passed", "title": "杭州 2 日行程", "plan": None, "spec": [], "budget": None, "people": 2,
            "start_city": "上海", "target_city": "杭州", "days": 2, "commonsense": [], "constraints": [],
            "repairs": [], "reflect_used": False, "run": {"total_ms": 1.0, "nodes": []}, "issues": [], "candidates": []}


@pytest.fixture()
def server(tmp_path: Path) -> Any:
    app = Workbench(tmp_path, rule_only=True, runner=_fake_runner)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", app
    httpd.shutdown()


def _post(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_server_turns_static_and_errors(server: Any) -> None:
    base, app = server
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        assert r.status == 200 and "TripMate" in r.read().decode("utf-8")
    for bad in ("/static/../server.py", "/static/%2e%2e/server.py", "/etc/passwd"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + bad, timeout=10)
        assert e.value.code == 404
    status, out = _post(base + "/api/turn", {"conversation_id": None, "text": "当前位置上海。两个人去杭州玩两天。"})
    conv = out["conversation"]
    assert status == 200 and conv["title"] == "杭州 2 日行程" and [t["role"] for t in conv["turns"]] == ["user", "assistant"]
    status, out = _post(base + "/api/turn", {"conversation_id": conv["id"], "text": "改成三天"})
    assert status == 200 and len(out["conversation"]["turns"]) == 4
    with urllib.request.urlopen(f"{base}/api/state?conversation={conv['id']}", timeout=10) as r:
        state = json.loads(r.read())
    assert state["conversations"][0]["id"] == conv["id"] and state["engine"]["mode"] == "rule"
    assert _post(base + "/api/turn", {"text": "   "})[0] == 400
    assert _post(base + "/api/preferences/decide", {"conversation_id": conv["id"], "turn": 1, "index": 9, "accept": True})[0] == 400
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + "/api/state?conversation=../x", timeout=10)
    assert e.value.code == 400

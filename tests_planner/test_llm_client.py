import json
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from tripmate_planner.llm.cache import ResponseCache
from tripmate_planner.llm.client import LLMClient, LLMConfig
from tripmate_planner.llm.cost import Budget, CostLedger
from tripmate_planner.llm.errors import BudgetExceeded, CacheMiss, LLMOutputError, LLMRequestError

FAKE_KEY = "test-FAKEKEY-123456"


class Out(BaseModel):
    ok: bool


def _completion(content: str) -> dict:
    return {
        "id": "x", "object": "chat.completion", "created": 0, "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                  "prompt_cache_hit_tokens": 60, "prompt_cache_miss_tokens": 40},
    }


def _client(tmp_path: Path, handler, mode="read_write", caps=None) -> tuple[LLMClient, list]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request, len(seen))

    http = httpx.Client(transport=httpx.MockTransport(wrapped))
    ledger = CostLedger(tmp_path / "ledger.jsonl")
    cache = ResponseCache(tmp_path / "cache.sqlite3", mode=mode)
    budget = Budget(ledger, caps or {"p0_smoke": 1.0}, total_cap=1.0)
    client = LLMClient(LLMConfig(), ledger, cache, budget, http_client=http, sleep=lambda s: None)
    return client, seen


MSG = [{"role": "user", "content": "输出 json"}]


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)


def test_request_has_thinking_disabled_and_cost(tmp_path: Path) -> None:
    client, seen = _client(tmp_path, lambda r, n: httpx.Response(200, json=_completion("{\"ok\": true}")))
    out, records = client.chat_json(purpose="t", messages=MSG, schema=Out, max_tokens=20)
    body = json.loads(seen[0].content)
    assert body["thinking"] == {"type": "disabled"} and body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert out.ok is True
    assert records[0].cost_std == pytest.approx(60 * 0.02e-6 + 40 * 1e-6 + 10 * 4e-6)
    assert records[0].cost_peak == pytest.approx(2 * records[0].cost_std)


def test_retry_on_429_then_success(tmp_path: Path) -> None:
    def handler(r, n):
        return httpx.Response(429, json={"error": {"message": "slow"}}) if n == 1 else \
            httpx.Response(200, json=_completion("{\"ok\": true}"))
    client, seen = _client(tmp_path, handler)
    client.chat_json(purpose="t", messages=MSG, schema=Out, max_tokens=20)
    assert len(seen) == 2


def test_no_retry_on_400(tmp_path: Path) -> None:
    client, seen = _client(tmp_path, lambda r, n: httpx.Response(400, json={"error": {"message": "bad"}}))
    with pytest.raises(LLMRequestError):
        client.chat_json(purpose="t", messages=MSG, schema=Out, max_tokens=20)
    assert len(seen) == 1


def test_corrective_retry_success_and_failure(tmp_path: Path) -> None:
    client, seen = _client(tmp_path, lambda r, n: httpx.Response(
        200, json=_completion("not json" if n == 1 else "{\"ok\": false}")))
    out, records = client.chat_json(purpose="t", messages=MSG, schema=Out, max_tokens=20)
    assert out.ok is False and len(records) == 2
    retry_body = json.loads(seen[1].content)
    assert "上一次输出不符合要求" in retry_body["messages"][-1]["content"]
    client2, _ = _client(tmp_path / "b", lambda r, n: httpx.Response(200, json=_completion("{}")))
    with pytest.raises(LLMOutputError):
        client2.chat_json(purpose="t", messages=MSG, schema=Out, max_tokens=20)


def test_cache_hit_makes_no_request_and_replay_only_miss(tmp_path: Path) -> None:
    client, seen = _client(tmp_path, lambda r, n: httpx.Response(200, json=_completion("{\"ok\": true}")))
    client.chat_json(purpose="t", messages=MSG, schema=Out, max_tokens=20)
    _, records = client.chat_json(purpose="t", messages=MSG, schema=Out, max_tokens=20)
    assert len(seen) == 1 and records[0].local_cache_hit and records[0].cost_peak == 0
    replay, seen2 = _client(tmp_path, lambda r, n: httpx.Response(500), mode="replay_only")
    with pytest.raises(CacheMiss):
        replay.chat_json(purpose="t", messages=[{"role": "user", "content": "另一个 json"}],
                         schema=Out, max_tokens=20)
    assert not seen2


def test_budget_blocks_request(tmp_path: Path) -> None:
    client, seen = _client(tmp_path, lambda r, n: httpx.Response(200, json=_completion("{\"ok\": true}")),
                           caps={"p0_smoke": 1e-9})
    with pytest.raises(BudgetExceeded):
        client.chat_json(purpose="t", messages=MSG, schema=Out, max_tokens=20)
    assert not seen


def test_key_never_persisted(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, lambda r, n: httpx.Response(200, json=_completion("{\"ok\": true}")))
    client.chat_json(purpose="t", messages=MSG, schema=Out, max_tokens=20)
    for path in tmp_path.iterdir():
        assert FAKE_KEY.encode() not in path.read_bytes()


def test_prompt_must_mention_json(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, lambda r, n: httpx.Response(200, json=_completion("{}")))
    with pytest.raises(ValueError):
        client.chat_json(purpose="t", messages=[{"role": "user", "content": "hi"}], schema=None,
                         max_tokens=5)

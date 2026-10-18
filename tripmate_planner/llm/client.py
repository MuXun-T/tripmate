"""DeepSeek deepseek-flash JSON client: thinking disabled, retries, cache, cost ledger, budget.

Every planner LLM call goes through LLMClient.chat_json (rule R7).
"""

import argparse
import json
import os
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import openai
from pydantic import BaseModel, ValidationError

from tripmate_planner.llm.cache import CacheMode, ResponseCache, request_key
from tripmate_planner.llm.cost import PEAK, STD, Budget, CallRecord, CostLedger, cost_of
from tripmate_planner.llm.errors import CacheMiss, LLMOutputError, LLMRequestError
from tripmate_planner.paths import RUNS

THINKING_OFF = {"thinking": {"type": "disabled"}}
RETRYABLE = (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError,
             openai.InternalServerError)
BACKOFF_SECONDS = (1.0, 4.0)


@dataclass
class LLMConfig:
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    temperature: float = 0.0
    timeout_s: float = 60.0
    max_retries: int = 2
    cache_mode: CacheMode = "read_write"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LLMConfig":
        keys = {"model", "base_url", "temperature", "timeout_s", "max_retries", "cache_mode"}
        return cls(**{k: v for k, v in data.items() if k in keys})


class LLMClient:
    def __init__(self, cfg: LLMConfig, ledger: CostLedger, cache: ResponseCache, budget: Budget,
                 *, http_client: httpx.Client | None = None, sleep: Any = time.sleep) -> None:
        if cfg.model != "deepseek-flash":
            raise ValueError("only deepseek-flash is allowed (R7)")
        self.cfg = cfg
        self.ledger = ledger
        self.cache = cache
        self.budget = budget
        self.run_id = "adhoc"
        self.scope = "p0_smoke"
        self.used_keys: set[str] = set()
        self._sleep = sleep
        self._rng = random.Random(20261018)
        self._lock = threading.Lock()
        self._http_client = http_client
        self._client: openai.OpenAI | None = None

    def set_context(self, *, run_id: str, scope: str) -> None:
        self.run_id = run_id
        self.scope = scope

    def _openai(self) -> openai.OpenAI:
        if self._client is None:
            key = os.environ.get("LLM_API_KEY", "")
            if not key:
                raise LLMRequestError("LLM_API_KEY is empty")
            self._client = openai.OpenAI(base_url=self.cfg.base_url, api_key=key,
                                         timeout=self.cfg.timeout_s, max_retries=0,
                                         http_client=self._http_client)
        return self._client

    def _request(self, messages: list[dict[str, str]], max_tokens: int) -> dict[str, Any]:
        return {
            "model": self.cfg.model,
            "base_url": self.cfg.base_url,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "extra_body": THINKING_OFF,
        }

    def _send(self, request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        attempt = 0
        while True:
            try:
                resp = self._openai().chat.completions.create(
                    model=request["model"], messages=request["messages"],
                    temperature=request["temperature"], max_tokens=request["max_tokens"],
                    response_format=request["response_format"], extra_body=request["extra_body"],
                )
                break
            except RETRYABLE as exc:
                if attempt >= self.cfg.max_retries:
                    raise LLMRequestError(f"retries exhausted: {type(exc).__name__}: {str(exc)[:200]}") from exc
                with self._lock:
                    jitter = self._rng.uniform(0, 0.5)
                self._sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)] + jitter)
                attempt += 1
            except openai.APIStatusError as exc:
                raise LLMRequestError(f"request failed with status {exc.status_code}") from exc
        content = resp.choices[0].message.content or ""
        usage = resp.usage.model_dump() if resp.usage is not None else {}
        return content, usage

    def _call(self, purpose: str, messages: list[dict[str, str]], max_tokens: int) -> tuple[str, CallRecord]:
        request = self._request(messages, max_tokens)
        key = request_key(request)
        with self._lock:
            self.used_keys.add(key)
        started = time.perf_counter()
        cached = self.cache.get(key)
        if cached is not None:
            content, usage = cached
            record = self._record(purpose, usage, local_hit=True, ms=(time.perf_counter() - started) * 1000)
            return content, record
        if self.cache.mode == "replay_only":
            raise CacheMiss(f"no cached response for {purpose}")
        prompt_chars = sum(len(m["content"]) for m in messages)
        self.budget.check(self.scope, prompt_chars * PEAK["miss"] + max_tokens * PEAK["out"])
        content, usage = self._send(request)
        self.cache.put(key, request, content, usage, time.time())
        record = self._record(purpose, usage, local_hit=False, ms=(time.perf_counter() - started) * 1000)
        self.ledger.append(record)
        return content, record

    def _record(self, purpose: str, usage: dict[str, Any], *, local_hit: bool, ms: float) -> CallRecord:
        prompt = int(usage.get("prompt_tokens") or 0)
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss = int(usage.get("prompt_cache_miss_tokens") or max(prompt - hit, 0))
        out = int(usage.get("completion_tokens") or 0)
        std = 0.0 if local_hit else cost_of(hit, miss, out, STD)
        peak = 0.0 if local_hit else cost_of(hit, miss, out, PEAK)
        return CallRecord(ts=time.time(), run_id=self.run_id, scope=self.scope, purpose=purpose,
                          local_cache_hit=local_hit, prompt_tokens=prompt, hit_tokens=hit,
                          miss_tokens=miss, completion_tokens=out, cost_std=std, cost_peak=peak, ms=ms)

    def chat_json(self, *, purpose: str, messages: list[dict[str, str]],
                  schema: type[BaseModel] | None, max_tokens: int) -> tuple[Any, list[CallRecord]]:
        """Return the parsed (and validated) JSON plus every call record made for it."""
        if not any("json" in m["content"].lower() for m in messages):
            raise ValueError("DeepSeek JSON mode requires the word json in the prompt")
        records: list[CallRecord] = []
        content, record = self._call(purpose, messages, max_tokens)
        records.append(record)
        try:
            return self._parse(content, schema), records
        except (ValueError, ValidationError) as exc:
            error = str(exc)[:500]
        retry_messages = [*messages, {"role": "assistant", "content": content},
                          {"role": "user",
                           "content": f"上一次输出不符合要求：{error}。请只输出修正后的完整 JSON。"}]
        content, record = self._call(f"{purpose}:retry", retry_messages, max_tokens)
        records.append(record)
        try:
            return self._parse(content, schema), records
        except (ValueError, ValidationError) as exc:
            raise LLMOutputError(str(exc)[:500]) from exc

    @staticmethod
    def _parse(content: str, schema: type[BaseModel] | None) -> Any:
        if not content.strip():
            raise ValueError("empty content")
        data = json.loads(content)
        return schema.model_validate(data) if schema is not None else data


def build_client(llm_cfg: dict[str, Any], *, cache_mode: CacheMode | None = None,
                 http_client: httpx.Client | None = None) -> LLMClient:
    cfg = LLMConfig.from_dict(llm_cfg)
    if cache_mode is not None:
        cfg.cache_mode = cache_mode
    ledger = CostLedger()
    return LLMClient(cfg, ledger, ResponseCache(mode=cfg.cache_mode), Budget(ledger), http_client=http_client)


def _shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [type(value[0]).__name__] if value else []
    return type(value).__name__


def smoke() -> None:
    out_dir = RUNS / "smoke"
    out_dir.mkdir(parents=True, exist_ok=True)
    captured: dict[str, Any] = {}

    def on_request(request: httpx.Request) -> None:
        body = json.loads(request.content or b"{}")
        captured["shape"] = _shape({k: v for k, v in body.items() if k not in {"messages", "extra_body"}})
        captured["thinking"] = body.get("thinking")

    http_client = httpx.Client(event_hooks={"request": [on_request]})
    client = build_client({}, cache_mode="off", http_client=http_client)
    client.set_context(run_id="smoke", scope="p0_smoke")
    messages = [{"role": "user", "content": "只输出 json：{\"ok\": true}"}]
    request = client._request(messages, 20)
    resp = client._openai().chat.completions.create(
        model=request["model"],
        messages=messages,  # type: ignore[arg-type]
        temperature=0,
        max_tokens=20,
        response_format=request["response_format"],
        extra_body=request["extra_body"],
    )
    message_extra = resp.choices[0].message.model_extra or {}
    reasoning = message_extra.get("reasoning_content")
    usage = resp.usage.model_dump() if resp.usage else {}
    record = client._record("smoke", usage, local_hit=False, ms=0.0)
    client.ledger.append(record)
    result = {"request_shape": captured.get("shape"), "thinking": captured.get("thinking"),
              "reasoning_content_present": bool(reasoning), "content": resp.choices[0].message.content,
              "usage": usage, "cost_peak": record.cost_peak}
    Path(out_dir / "request_shape.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                    encoding="utf-8")
    assert captured.get("thinking") == {"type": "disabled"}, captured
    assert not reasoning, "reasoning_content returned; thinking not disabled"
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true")
    if parser.parse_args().smoke:
        smoke()

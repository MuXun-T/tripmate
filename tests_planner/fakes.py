"""Test doubles: FakeLLM returns queued JSON outputs through the same interface as LLMClient."""

from typing import Any

from tripmate_planner.llm.errors import LLMOutputError


class FakeLLM:
    def __init__(self, outputs: list[Any]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    def chat_json(self, *, purpose: str, messages: list[dict[str, str]], schema: Any, max_tokens: int):
        self.calls.append({"purpose": purpose, "messages": messages})
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return (schema.model_validate(out) if schema is not None else out), []


__all__ = ["FakeLLM", "LLMOutputError"]

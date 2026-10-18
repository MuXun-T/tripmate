"""CLI: `plan --text ...` (single request) and `chat --user U --conversation C` (multi-turn with memory)."""

import argparse
import json
import os
import sys
from typing import Any

from tripmate_planner.config import load_config
from tripmate_planner.env.enums import get_enums
from tripmate_planner.graph import run_turn
from tripmate_planner.llm.client import build_client
from tripmate_planner.memory.store import MemoryStore
from tripmate_planner.paths import CONFIGS, RUNS
from tripmate_planner.retrieval.retriever import Retriever


def _llm(cfg: dict[str, Any]) -> Any:
    if os.environ.get("TRIPMATE_FAKE_LLM") == "1" or cfg["rewrite"]["mode"] == "rule":
        return None  # rule-only rewrite, no API calls
    client = build_client(cfg["llm"])
    client.set_context(run_id="cli", scope="p4")
    return client


def _summary(plan: dict[str, Any] | None, status: str) -> str:
    if not plan:
        return f"状态：{status}（未生成行程）"
    total = sum(float(a.get("cost", 0)) + sum(float(t.get("cost", 0)) for t in a.get("transports", []))
                for d in plan["itinerary"] for a in d["activities"])
    return f"状态：{status}；{len(plan['itinerary'])} 天；总费用约 {total:.0f} 元"


def main() -> None:
    parser = argparse.ArgumentParser(prog="tripmate_planner.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--text", required=True)
    p.add_argument("--config", default=str(CONFIGS / "full.json"))
    c = sub.add_parser("chat")
    c.add_argument("--user", default="demo")
    c.add_argument("--conversation", default="c1")
    c.add_argument("--config", default=str(CONFIGS / "full.json"))
    args = parser.parse_args()
    cfg = load_config(args.config)
    enums, retriever, llm = get_enums(), Retriever(cfg["retrieval"]), _llm(cfg)
    if args.cmd == "plan":
        res = run_turn(args.text, user_id="cli", conversation_id="cli-plan", cfg=cfg, enums=enums, llm=llm,
                       retriever=retriever, store=None)
        out = RUNS / "cli" / "last_plan.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(res.plan or {}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(_summary(res.plan, res.status), "->", out)
        return
    cfg["memory"]["auto_confirm"] = False
    store = MemoryStore(RUNS / "cli" / "memory.sqlite3")

    def confirm(cands: list[dict[str, Any]]) -> list[int]:
        accepted = []
        for i, cand in enumerate(cands):
            answer = input(f"是否记住：{cand['key']}={json.dumps(cand['value'], ensure_ascii=False)}"
                           f"（依据：\"{cand.get('evidence', '')}\"）[y/n] ").strip().lower()
            if answer.startswith("y"):
                accepted.append(i)
        return accepted

    for line in sys.stdin if not sys.stdin.isatty() else iter(lambda: input("你："), "/quit"):
        text = line.strip()
        if not text or text == "/quit":
            break
        res = run_turn(text, user_id=args.user, conversation_id=args.conversation, cfg=cfg, enums=enums, llm=llm,
                       retriever=retriever, store=store, confirm=confirm)
        print("助手：" + _summary(res.plan, res.status))


if __name__ == "__main__":
    main()

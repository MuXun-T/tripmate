"""T5.2: official zero-shot ReAct (ReAct0) baseline with deepseek-flash, thinking disabled (lowest-cost baseline).

No third-party file is modified: we register a model alias, wrap _send_request (assert thinking is disabled,
enforce the spending cap) and _record_usage (write DeepSeek usage, incl. cache hit/miss tokens, to our ledger).
Input to the agent is nature_language only, as in run_exp.py's ReAct branch.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from tripmate_planner.data.splits import load_queries
from tripmate_planner.llm.cost import PEAK, STD, Budget, CallRecord, CostLedger, cost_of
from tripmate_planner.llm.errors import BudgetExceeded
from tripmate_planner.paths import CT_ROOT, REPORTS, RUNS

THINKING_OFF = {"thinking": {"type": "disabled"}}


def _patch(run_id: str, scope: str, out: Path) -> dict[str, Any]:
    import chinatravel.agent.llms as L

    alias = L.ModelAlias(name="deepseek-flash-nothink", model="deepseek-flash", base_url="https://api.deepseek.com",
                         api_key_env="DEEPSEEK_API_KEY",
                         default_request_args={"temperature": 0, "top_p": 1e-8, "extra_body": THINKING_OFF})
    L.MODEL_ALIASES["deepseek-flash-nothink"] = alias
    ledger = CostLedger()
    budget = Budget(ledger)
    orig_send, orig_record = L.OpenAICompatibleLLM._send_request, L.OpenAICompatibleLLM._record_usage
    # The official wrapper swallows request exceptions (returns REQUEST_ERROR_RESPONSE), so a cap hit is
    # remembered here, every later request fails fast without calling the API, and main() stops the loop.
    state: dict[str, Any] = {"first": True, "budget_hit": False, "calls": 0}

    def send(self: Any, messages: list[dict[str, Any]], request_args: dict[str, Any]) -> dict[str, Any]:
        if request_args.get("extra_body") != THINKING_OFF:
            raise RuntimeError("thinking must be disabled for the ReAct0 baseline (R7)")
        if state["first"]:
            (out / "request_shape.json").write_text(json.dumps(sorted(request_args), ensure_ascii=False))
            state["first"] = False
        if state["budget_hit"]:
            raise BudgetExceeded("p5_baseline cap already reached")
        chars = sum(len(str(m.get("content", ""))) for m in messages)
        try:
            budget.check(scope, chars * PEAK["miss"] + int(request_args.get("max_tokens", 4096)) * PEAK["out"])
        except BudgetExceeded:
            state["budget_hit"] = True
            raise
        state["calls"] += 1
        self._t0 = time.perf_counter()
        result: dict[str, Any] = orig_send(self, messages, request_args)
        return result

    def record(self: Any, completion: Any) -> None:
        orig_record(self, completion)
        usage = getattr(completion, "usage", None)
        if usage is None:
            return
        extra = getattr(usage, "model_extra", None) or {}
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        hit = int(extra.get("prompt_cache_hit_tokens") or 0)
        miss = int(extra.get("prompt_cache_miss_tokens") or max(prompt - hit, 0))
        outt = int(getattr(usage, "completion_tokens", 0) or 0)
        ledger.append(CallRecord(ts=time.time(), run_id=run_id, scope=scope, purpose="react0", local_cache_hit=False,
                                 prompt_tokens=prompt, hit_tokens=hit, miss_tokens=miss, completion_tokens=outt,
                                 cost_std=cost_of(hit, miss, outt, STD), cost_peak=cost_of(hit, miss, outt, PEAK),
                                 ms=(time.perf_counter() - getattr(self, "_t0", time.perf_counter())) * 1000))

    L.OpenAICompatibleLLM._send_request = send
    L.OpenAICompatibleLLM._record_usage = record
    return state


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["easy", "test"], required=True, help="easy only for the 2-query smoke")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    if args.split == "test":
        lock = REPORTS / "final" / "TEST_LOCK_react0.json"
        if not args.final or args.limit or lock.exists():
            raise SystemExit("test requires --final, no --limit, and no existing lock (R6)")
    if args.split == "easy" and (args.limit is None or args.limit > 2):
        raise SystemExit("easy is only for the smoke run (--limit ≤ 2)")
    out = RUNS / args.run_id
    (out / "plans").mkdir(parents=True, exist_ok=True)
    (out / "logs").mkdir(parents=True, exist_ok=True)
    scope = "p5_baseline"
    state = _patch(args.run_id, scope, out)
    queries = load_queries("human" if args.split == "test" else "easy")[: args.limit]
    os.chdir(CT_ROOT)
    from chinatravel.agent.load_model import create_agent_runtime
    from chinatravel.data.load_datasets import save_json_file

    runtime = create_agent_runtime("ReAct0", "deepseek-flash-nothink", project_root_path=str(CT_ROOT), lang="zh",
                                   oracle_translation=False, preference_search=False, refine_steps=10)
    agent = runtime.agent
    timing: dict[str, float] = {}
    for i, q in enumerate(queries):
        started = time.perf_counter()
        calls_before = state["calls"]
        try:
            plan_log = agent(q.text)
            plan = plan_log["ans"]
            if isinstance(plan, str):
                try:
                    plan = json.loads(plan)
                except Exception:
                    plan = {"plan": plan}
            # AgentReturnInfo supports item access only (run_exp.py uses plan_log["log"]).
            save_json_file(json_data=plan_log["log"], file_path=str(out / "logs" / f"{q.uid}.json"))
        except Exception as exc:
            plan = {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            sys.stdout = sys.__stdout__
        if state["budget_hit"]:
            # This query was cut by the cap: no plan is saved, so it is scored as an empty plan.
            print("budget cap reached; stopping (this and remaining queries count as empty plans)", flush=True)
            break
        if isinstance(plan, dict):
            # agent.run() resets the counters per query, so these are per-query values (as in run_exp.py).
            plan["input_token_count"] = agent.backbone_llm.input_token_count
            plan["output_token_count"] = agent.backbone_llm.output_token_count
            plan["input_token_maxx"] = agent.backbone_llm.input_token_maxx
            plan["llm_calls"] = state["calls"] - calls_before
        save_json_file(json_data=plan, file_path=str(out / "plans" / f"{q.uid}.json"))
        timing[q.uid] = round((time.perf_counter() - started) * 1000, 1)
        print(f"[{i + 1}/{len(queries)}] {q.uid} {timing[q.uid] / 1000:.1f}s", flush=True)
    (out / "timing.json").write_text(json.dumps({"method": "ReAct0_deepseek-flash-nothink", "ms": timing,
                                                 "stopped_by_budget": state["budget_hit"]}, ensure_ascii=False, indent=1))
    if args.split == "test":
        (REPORTS / "final" / "TEST_LOCK_react0.json").write_text(json.dumps({"run_id": args.run_id}))


if __name__ == "__main__":
    main()

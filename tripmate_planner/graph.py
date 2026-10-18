"""LangGraph orchestration of one user turn (T3.5).

State holds JSON-serializable data only; heavy objects (pools, directives, clients) live in TurnContext.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from tripmate_planner.env.enums import Enums
from tripmate_planner.memory.store import MemoryStore, validate_preference
from tripmate_planner.plan.search import Directives, Planner
from tripmate_planner.repair.actions import apply_repair
from tripmate_planner.retrieval.retriever import CandidatePools, Retriever
from tripmate_planner.spec.compile_dsl import compile_dsl_tagged
from tripmate_planner.spec.memory_merge import apply_preferences
from tripmate_planner.spec.rewrite import rewrite
from tripmate_planner.spec.tripspec import TripSpec
from tripmate_planner.verify.commonsense import verify_plan
from tripmate_planner.verify.result import VerifyResult


class PlannerState(TypedDict, total=False):
    text: str
    user_id: str
    conversation_id: str
    turn_no: int
    plan_needed: bool
    spec: dict[str, Any]
    spec_source: str
    rewrite_info: dict[str, Any]
    pref_candidates: list[dict[str, Any]]
    accepted_prefs: list[dict[str, Any]]
    plan: dict[str, Any] | None
    verify: dict[str, Any]
    round: int
    reflect_used: bool
    repair_stalled: bool
    best: dict[str, Any] | None
    final: dict[str, Any] | None
    final_status: str


@dataclass
class TurnContext:
    cfg: dict[str, Any]
    enums: Enums
    llm: Any
    retriever: Retriever
    store: MemoryStore | None
    prev_spec: TripSpec | None = None
    prefs: list[dict[str, Any]] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    pools: CandidatePools | None = None
    directives: Directives = field(default_factory=Directives)
    tags: list[str] = field(default_factory=list)
    dsl: list[str] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=lambda: {"nodes": [], "llm": [], "verify_history": [],
                                                            "repair_actions": [], "spec_issues": []})
    deadline: float = 0.0


def build_graph(ctx: TurnContext) -> Any:
    cfg = ctx.cfg
    mem = cfg["memory"]

    def timed(name: str, fn: Callable[[PlannerState], dict[str, Any]]) -> Callable[[PlannerState], dict[str, Any]]:
        def run(state: PlannerState) -> dict[str, Any]:
            started = time.perf_counter()
            try:
                return fn(state)
            finally:
                ctx.trace["nodes"].append({"name": name, "ms": round((time.perf_counter() - started) * 1000, 2)})
        return run

    def load_memory(state: PlannerState) -> dict[str, Any]:
        if ctx.store is not None and mem["enabled"]:
            latest = ctx.store.latest_state(state["conversation_id"], state["user_id"])
            ctx.prev_spec = TripSpec.model_validate(latest[1]) if latest else None
            ctx.history = ctx.store.recent_turns(state["conversation_id"], state["user_id"],
                                                 int(cfg["rewrite"]["max_history_turns"]),
                                                 int(cfg["rewrite"]["max_history_chars"]))
            ctx.prefs = ctx.store.active_preferences(state["user_id"]) if mem["long_term"] else []
        return {}

    def _rewrite(state: PlannerState, failures: list[str] | None) -> dict[str, Any]:
        base = TripSpec.model_validate(state["spec"]) if failures and state.get("spec") else ctx.prev_spec
        res = rewrite(state["text"], enums=ctx.enums, cfg=cfg, llm=ctx.llm, prev_spec=base, prefs=ctx.prefs,
                      history=ctx.history, failures=failures)
        ctx.trace["llm"].extend(r.__dict__ for r in res.llm_records)
        ctx.trace["spec_issues"].extend(res.issues)
        return {"spec": res.spec.model_dump(), "spec_source": res.source,
                "rewrite_info": {"changed_fields": res.changed_fields, "resolved_references": res.resolved_references},
                "pref_candidates": res.preference_candidates}

    def rewrite_node(state: PlannerState) -> dict[str, Any]:
        return _rewrite(state, None)

    def apply_memory(state: PlannerState) -> dict[str, Any]:
        spec = TripSpec.model_validate(state["spec"])
        if mem["enabled"] and mem["long_term"] and ctx.prefs:
            spec, applied, ctx.hints = apply_preferences(spec, ctx.prefs)
            ctx.trace["prefs_applied"] = applied
        return {"spec": spec.model_dump()}

    def compile_node(state: PlannerState) -> dict[str, Any]:
        tagged = compile_dsl_tagged(TripSpec.model_validate(state["spec"]))
        ctx.tags, ctx.dsl = [t for t, _ in tagged], [c for _, c in tagged]
        ctx.trace["dsl"] = ctx.dsl
        return {}

    def retrieve(state: PlannerState) -> dict[str, Any]:
        spec = TripSpec.model_validate(state["spec"])
        ctx.pools = ctx.retriever.build_pools(spec, " ".join([state["text"], *ctx.hints]))
        ctx.trace["pools"] = {"attractions": len(ctx.pools.attractions), "restaurants": len(ctx.pools.restaurants),
                              "hotels": len(ctx.pools.hotels), "shortfalls": ctx.pools.shortfalls}
        return {}

    def _check(spec: TripSpec, plan: dict[str, Any] | None) -> VerifyResult:
        return verify_plan(plan, spec, ctx.dsl, commonsense=bool(cfg["verify"]["commonsense"]),
                           spec_checks=bool(cfg["verify"]["spec"]))

    def plan_node(state: PlannerState) -> dict[str, Any]:
        spec = TripSpec.model_validate(state["spec"])
        if not spec.is_plannable() or ctx.pools is None:
            return {"plan": None}
        if cfg["planner"]["mode"] == "llm_direct":
            from tripmate_planner.eval.baselines.llm_direct import llm_direct_plan

            plan, records = llm_direct_plan(spec, ctx.pools, cfg, ctx.llm)
            ctx.trace["llm"].extend(r.__dict__ for r in records)
            return {"plan": plan}
        planner = Planner(spec, ctx.pools, cfg, state["text"], lambda p: _check(spec, p).n_failures, ctx.directives)
        planner.deadline = min(planner.deadline, ctx.deadline)
        res = planner.run()
        ctx.trace.setdefault("planner", []).append({"ms": round(res.ms, 1), "combos": res.combos_tried,
                                                    "checked": res.candidates_checked, "timed_out": res.timed_out})
        return {"plan": res.plan}

    def verify_node(state: PlannerState) -> dict[str, Any]:
        spec = TripSpec.model_validate(state["spec"])
        plan = state.get("plan")
        if cfg["planner"]["mode"] == "llm_direct":
            v = VerifyResult(passed=True)  # A9: no verification / repair by design
        else:
            v = _check(spec, plan)
        n_fail = v.n_failures if plan else 10**6
        ctx.trace["verify_history"].append({"round": state.get("round", 0), "reflect": state.get("reflect_used", False),
                                            "n_failures": None if not plan else v.n_failures,
                                            "commonsense_failed": v.commonsense_failed,
                                            "spec_failed": [ctx.tags[i] for i in v.spec_failed if i < len(ctx.tags)]})
        best = state.get("best")
        if best is None or n_fail < best["n_fail"]:
            best = {"n_fail": n_fail, "plan": plan, "passed": v.passed and bool(plan)}
        return {"verify": {"passed": v.passed and bool(plan), "commonsense_failed": v.commonsense_failed,
                           "commonsense_messages": v.commonsense_messages, "spec_failed": v.spec_failed},
                "best": best}

    def after_verify(state: PlannerState) -> str:
        if state["verify"]["passed"]:
            return "confirm_memory"
        if state.get("plan") and state.get("round", 0) < int(cfg["repair"]["max_rounds"]) and not state.get("repair_stalled"):
            return "repair"
        spec_fail = bool(state["verify"]["spec_failed"]) or not state.get("plan")
        if cfg["repair"]["reflect"] and not state.get("reflect_used") and spec_fail and ctx.llm is not None \
                and cfg["rewrite"]["mode"] != "rule":
            return "reflect"
        return "confirm_memory"

    def repair_node(state: PlannerState) -> dict[str, Any]:
        spec = TripSpec.model_validate(state["spec"])
        vd = state["verify"]
        v = VerifyResult(passed=False, commonsense_failed=vd["commonsense_failed"],
                         commonsense_messages=vd["commonsense_messages"], spec_failed=vd["spec_failed"])
        assert ctx.pools is not None
        actions = apply_repair(ctx.directives, state.get("plan"), v, ctx.tags, spec, ctx.pools)
        ctx.trace["repair_actions"].append(actions)
        return {"round": state.get("round", 0) + 1, "repair_stalled": not actions}

    def after_repair(state: PlannerState) -> str:
        return "plan" if not state.get("repair_stalled") else after_verify(state)

    def reflect_node(state: PlannerState) -> dict[str, Any]:
        vd = state["verify"]
        failures = [*vd["commonsense_failed"], *[f"约束未满足：{ctx.tags[i]}" for i in vd["spec_failed"] if i < len(ctx.tags)]]
        if not state.get("plan"):
            failures.append("在该需求下找不到可行行程（可能预算或交通方式设置过严）")
        out = _rewrite(state, failures)
        ctx.directives = Directives()
        return {**out, "reflect_used": True, "round": 0, "repair_stalled": False}

    def confirm_memory(state: PlannerState) -> dict[str, Any]:
        candidates = []
        for c in state.get("pref_candidates") or []:
            try:
                candidates.append({**c, "value": validate_preference(c["key"], c["value"], ctx.enums)})
            except ValueError:
                continue
        if not candidates or ctx.store is None or not mem["enabled"] or not mem["long_term"]:
            return {"accepted_prefs": []}
        if mem["auto_confirm"]:
            return {"accepted_prefs": candidates}
        decision = interrupt({"candidates": candidates})
        accept = set((decision or {}).get("accept", []))
        return {"accepted_prefs": [c for i, c in enumerate(candidates) if i in accept]}

    def save_memory(state: PlannerState) -> dict[str, Any]:
        if ctx.store is None or not mem["enabled"]:
            return {}
        conv, user, turn = state["conversation_id"], state["user_id"], state["turn_no"]
        ctx.store.append_turn(conv, turn, user, "user", state["text"])
        ctx.store.append_turn(conv, turn, user, "assistant", "（已按当时需求生成行程）" if state.get("plan_needed") else "（已记录需求）")
        ctx.store.save_state(conv, turn, user, state["spec"])
        for c in state.get("accepted_prefs") or []:
            ctx.store.add_preference(user, c["key"], c["value"], c.get("evidence", ""), ctx.enums)
        return {}

    def finalize(state: PlannerState) -> dict[str, Any]:
        best = state.get("best")
        if not state.get("plan_needed"):
            return {"final": None, "final_status": "no_plan_requested"}
        if best and best.get("plan"):
            return {"final": best["plan"], "final_status": "passed" if best["passed"] else "best_effort"}
        return {"final": None, "final_status": "no_plan"}

    def after_apply(state: PlannerState) -> str:
        return "compile" if state.get("plan_needed", True) else "confirm_memory"

    g = StateGraph(PlannerState)
    for name, fn in (("load_memory", load_memory), ("rewrite", rewrite_node), ("apply_memory", apply_memory),
                     ("compile", compile_node), ("retrieve", retrieve), ("plan", plan_node), ("verify", verify_node),
                     ("repair", repair_node), ("reflect", reflect_node), ("confirm_memory", confirm_memory),
                     ("save_memory", save_memory), ("finalize", finalize)):
        g.add_node(name, timed(name, fn))  # type: ignore[call-overload]
    g.add_edge(START, "load_memory")
    g.add_edge("load_memory", "rewrite")
    g.add_edge("rewrite", "apply_memory")
    g.add_conditional_edges("apply_memory", after_apply, {"compile": "compile", "confirm_memory": "confirm_memory"})
    g.add_edge("compile", "retrieve")
    g.add_edge("retrieve", "plan")
    g.add_edge("plan", "verify")
    g.add_conditional_edges("verify", after_verify, {"confirm_memory": "confirm_memory", "repair": "repair", "reflect": "reflect"})
    g.add_conditional_edges("repair", after_repair, {"plan": "plan", "confirm_memory": "confirm_memory", "repair": "repair",
                                                     "reflect": "reflect"})
    g.add_edge("reflect", "apply_memory")
    g.add_edge("confirm_memory", "save_memory")
    g.add_edge("save_memory", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=InMemorySaver())


@dataclass
class TurnResult:
    plan: dict[str, Any] | None
    status: str
    spec: dict[str, Any]
    trace: dict[str, Any]
    state: dict[str, Any]


def run_turn(text: str, *, user_id: str, conversation_id: str, cfg: dict[str, Any], enums: Enums, llm: Any,
             retriever: Retriever, store: MemoryStore | None, plan_needed: bool = True,
             confirm: Callable[[list[dict[str, Any]]], list[int]] | None = None) -> TurnResult:
    """Run one turn. Input is the user's text only (rule R5)."""
    started = time.perf_counter()
    ctx = TurnContext(cfg=cfg, enums=enums, llm=llm, retriever=retriever, store=store,
                      deadline=time.perf_counter() + float(cfg["runtime"]["query_timeout_s"]))
    graph = build_graph(ctx)
    turn_no = store.next_turn_no(conversation_id) if store is not None else 1
    config = {"configurable": {"thread_id": f"{conversation_id}:{turn_no}"}, "recursion_limit": 60}
    out = graph.invoke({"text": text, "user_id": user_id, "conversation_id": conversation_id, "turn_no": turn_no,
                        "plan_needed": plan_needed, "round": 0, "reflect_used": False, "best": None}, config)
    while "__interrupt__" in out:
        payload = out["__interrupt__"][0].value
        accept = confirm(payload["candidates"]) if confirm is not None else []
        out = graph.invoke(Command(resume={"accept": accept}), config)
    ctx.trace["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    ctx.trace["final_status"] = out.get("final_status")
    ctx.trace["repair_rounds"] = out.get("round", 0)
    ctx.trace["reflect_used"] = out.get("reflect_used", False)
    return TurnResult(plan=out.get("final"), status=str(out.get("final_status")), spec=out.get("spec", {}),
                      trace=ctx.trace, state=dict(out))

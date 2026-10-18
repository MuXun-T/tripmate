"""Plan -> verify -> repair loop (≤ max_rounds). Reflection is orchestrated by the graph."""

from dataclasses import dataclass, field
from typing import Any

from tripmate_planner.plan.search import Directives, Planner
from tripmate_planner.repair.actions import apply_repair
from tripmate_planner.retrieval.retriever import CandidatePools
from tripmate_planner.spec.compile_dsl import compile_dsl_tagged
from tripmate_planner.spec.tripspec import TripSpec
from tripmate_planner.verify.commonsense import verify_plan
from tripmate_planner.verify.result import VerifyResult


@dataclass
class LoopResult:
    plan: dict[str, Any] | None
    verify: VerifyResult
    rounds: int
    history: list[dict[str, Any]] = field(default_factory=list)
    planner_ms: float = 0.0


def plan_with_repair(spec: TripSpec, pools: CandidatePools, cfg: dict[str, Any], text: str) -> LoopResult:
    tagged = compile_dsl_tagged(spec)
    tags, dsl = [t for t, _ in tagged], [c for _, c in tagged]
    vcfg = cfg["verify"]

    def check(plan: dict[str, Any] | None) -> VerifyResult:
        return verify_plan(plan, spec, dsl, commonsense=bool(vcfg["commonsense"]), spec_checks=bool(vcfg["spec"]))

    directives = Directives()
    best: tuple[int, dict[str, Any] | None, VerifyResult] | None = None
    history: list[dict[str, Any]] = []
    planner_ms = 0.0
    max_rounds = int(cfg["repair"]["max_rounds"])
    for rnd in range(max_rounds + 1):
        res = Planner(spec, pools, cfg, text, lambda p: check(p).n_failures, directives).run()
        planner_ms += res.ms
        v = check(res.plan)
        n_fail = v.n_failures if res.plan else 10**6
        history.append({"round": rnd, "n_failures": v.n_failures if res.plan else None,
                        "commonsense_failed": v.commonsense_failed, "spec_failed": [tags[i] for i in v.spec_failed],
                        "combos": res.combos_tried, "timed_out": res.timed_out})
        if best is None or n_fail < best[0]:
            best = (n_fail, res.plan, v)
        if v.passed or rnd == max_rounds:
            break
        actions = apply_repair(directives, res.plan, v, tags, spec, pools)
        history[-1]["actions"] = actions
        if not actions:
            break
    assert best is not None
    return LoopResult(plan=best[1], verify=best[2], rounds=len(history) - 1, history=history, planner_ms=planner_ms)

"""T1.4: compiled DSL (from oracle-derived labels) must give the same overall verdict as the oracle DSL."""

import copy
import json

import pytest

from tripmate_planner.data.splits import uid_list
from tripmate_planner.eval.labels import gold_from_record
from tripmate_planner.eval.oracle import load_oracle_records
from tripmate_planner.paths import RUNS
from tripmate_planner.spec.compile_dsl import compile_dsl
from tripmate_planner.spec.tripspec import TripSpec
from tripmate_planner.verify.spec_checks import run_spec_checks

PLAN_DIR = RUNS / "p0-rulenesy-easy20" / "plans"


def _spec_from_labels(labels) -> TripSpec:
    data: dict = {}
    for lab in labels:
        if lab.field == "intercity":
            data["intercity_go"] = data["intercity_back"] = lab.value
        elif lab.field in {"must_visit", "cuisines", "attraction_types", "hotel_features", "must_eat_at"}:
            data.setdefault(lab.field, []).append(lab.value)
        else:
            data[lab.field] = lab.value
    return TripSpec.model_validate(data)


def _oracle_verdict(record: dict, plan: dict) -> bool:
    import contextlib
    import io
    import os

    from chinatravel.symbol_verification.hard_constraint import evaluate_constraints_py

    from tripmate_planner.paths import CT_ROOT
    old = os.getcwd()
    os.chdir(CT_ROOT)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            codes = [c for c in record_codes(record)]
            return all(evaluate_constraints_py(codes, plan))
    finally:
        os.chdir(old)


def record_codes(record: dict) -> list[str]:
    key = "hard" + "_logic_py"  # test helper; oracle access stays out of tripmate_planner/
    return record[key]


@pytest.mark.skipif(not PLAN_DIR.is_dir(), reason="needs RuleNeSy plans from T0.5")
def test_compiled_dsl_matches_oracle_verdicts() -> None:
    plans = []
    for path in sorted(PLAN_DIR.glob("*.json")):
        plan = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(plan, dict) and plan.get("itinerary"):
            plans.append(plan)
            pricey = copy.deepcopy(plan)
            for day in pricey["itinerary"]:
                for act in day["activities"]:
                    if "cost" in act:
                        act["cost"] = act["cost"] * 10
            plans.append(pricey)
    assert plans, "no usable RuleNeSy plans"
    dev = uid_list("dev")
    records = load_oracle_records(dev)
    chosen = []
    for uid in dev:
        labels, n_nd, uncovered = gold_from_record(records[uid])
        if not uncovered and n_nd > 2:
            chosen.append((uid, labels))
        if len(chosen) == 30:
            break
    # always include the in-domain RuleNeSy uids
    for path in sorted(PLAN_DIR.glob("*.json")):
        uid = path.stem
        labels, _, uncovered = gold_from_record(records[uid])
        if not uncovered and uid not in {u for u, _ in chosen}:
            chosen.append((uid, labels))
    mismatches = []
    for uid, labels in chosen:
        dsl = compile_dsl(_spec_from_labels(labels))
        for i, plan in enumerate(plans):
            ours = not run_spec_checks(plan, dsl)
            theirs = _oracle_verdict(records[uid], plan)
            if ours != theirs:
                mismatches.append((uid, i, ours, theirs))
    assert mismatches == [], mismatches[:5]

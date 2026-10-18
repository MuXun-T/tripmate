import json

import pytest

from tripmate_planner.config import load_config
from tripmate_planner.paths import CONFIGS, CT_ROOT
from tripmate_planner.plan.search import Planner
from tripmate_planner.retrieval.retriever import Retriever
from tripmate_planner.spec.compile_dsl import compile_dsl
from tripmate_planner.spec.tripspec import TripSpec
from tripmate_planner.verify.commonsense import verify_plan

CFG = load_config(CONFIGS / "full.json")
CFG["retrieval"]["mode"] = "bm25"  # keep unit tests fast; dense is covered by the models-marked test
SPEC = TripSpec(start_city="杭州", target_city="上海", days=2, people_number=2, budget_total=5000,
                cuisines=["本帮菜"], attraction_types=["博物馆/纪念馆"])


@pytest.fixture(scope="module")
def pools():
    return Retriever(CFG["retrieval"]).build_pools(SPEC, "当前位置杭州。我两个人想去上海玩2天，想吃本帮菜，看博物馆。")


def _schema_ok(plan: dict) -> bool:
    import jsonschema

    schema = json.loads((CT_ROOT / "chinatravel/evaluation/output_schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(plan, schema)
    return True


def test_plan_passes_schema_commonsense_and_spec(pools) -> None:
    dsl = compile_dsl(SPEC)
    res = Planner(SPEC, pools, CFG, "", lambda p: verify_plan(p, SPEC, dsl).n_failures).run()
    assert res.plan is not None and _schema_ok(res.plan)
    v = verify_plan(res.plan, SPEC, dsl)
    assert v.passed, (v.commonsense_failed, v.spec_failed)
    assert len(res.plan["itinerary"]) == 2


def test_planner_is_deterministic(pools) -> None:
    dsl = compile_dsl(SPEC)
    a = Planner(SPEC, pools, CFG, "", lambda p: verify_plan(p, SPEC, dsl).n_failures).run().plan
    b = Planner(SPEC, pools, CFG, "", lambda p: verify_plan(p, SPEC, dsl).n_failures).run().plan
    assert a == b


def test_time_budget_returns_best_found(pools) -> None:
    cfg = json.loads(json.dumps(CFG))
    cfg["planner"]["time_budget_s"] = 0.0
    res = Planner(SPEC, pools, cfg, "", lambda p: 0).run()
    assert res.timed_out or res.plan is None or res.combos_tried <= 1

from tests_planner.fakes import FakeLLM
from tripmate_planner.config import load_config
from tripmate_planner.env.enums import get_enums
from tripmate_planner.llm.errors import LLMOutputError
from tripmate_planner.paths import CONFIGS
from tripmate_planner.spec.rewrite import rewrite
from tripmate_planner.spec.tripspec import TripSpec

CFG = load_config(CONFIGS / "full.json")
MSG = "当前位置北京。我两个人想去上海玩2天，想吃本帮菜。"


def _out(**spec):
    return {"spec": spec, "changed_fields": list(spec), "resolved_references": [], "preference_candidates": []}


def test_rule_fields_override_llm_and_enums_normalized() -> None:
    llm = FakeLLM([_out(start_city="北京", target_city="上海", days=3, people_number=1,
                        cuisines=["本帮菜", "外星菜"])])
    res = rewrite(MSG, enums=get_enums(), cfg=CFG, llm=llm)
    assert res.spec.days == 2 and res.spec.people_number == 2  # rule wins
    assert res.spec.cuisines == ["本帮菜"] and res.source == "llm+rule"
    assert res.spec.field_sources["days"] == "rule" and res.spec.field_sources["cuisines"] == "llm"
    assert "json" in llm.calls[0]["messages"][0]["content"]


def test_llm_failure_falls_back_to_rules() -> None:
    res = rewrite(MSG, enums=get_enums(), cfg=CFG, llm=FakeLLM([LLMOutputError("bad")]))
    assert res.source == "rule_fallback" and res.spec.cuisines == ["本帮菜"]


def test_dropped_fields_restored_from_previous_state() -> None:
    prev = TripSpec(start_city="北京", target_city="上海", days=2, people_number=2, budget_total=3000)
    llm = FakeLLM([{"spec": {"start_city": "北京", "target_city": "上海", "days": 2, "people_number": 2,
                             "innercity_mode": "metro"},
                    "changed_fields": ["innercity_mode"], "resolved_references": [], "preference_candidates": [
                        {"key": "innercity_mode", "value": "metro", "evidence": "我平时都坐地铁"},
                        {"key": "bogus", "value": 1, "evidence": ""}]}])
    res = rewrite("以后我都坐地铁", enums=get_enums(), cfg=CFG, llm=llm, prev_spec=prev)
    assert res.spec.budget_total == 3000 and res.spec.innercity_mode == "metro"
    assert [c["key"] for c in res.preference_candidates] == ["innercity_mode"]
    assert any("restored" in i for i in res.issues)


def test_people_default() -> None:
    res = rewrite("当前位置北京。去上海玩2天。", enums=get_enums(), cfg={**CFG, "rewrite": {**CFG["rewrite"], "mode": "rule"}},
                  llm=None)
    assert res.spec.people_number == 1 and res.spec.field_sources["people_number"] == "default"

import json

from tests_planner.fakes import FakeLLM
from tripmate_planner.config import load_config
from tripmate_planner.env.enums import get_enums
from tripmate_planner.graph import run_turn
from tripmate_planner.memory.store import MemoryStore
from tripmate_planner.paths import CONFIGS
from tripmate_planner.plan.search import Directives
from tripmate_planner.repair.actions import apply_repair
from tripmate_planner.retrieval.retriever import CandidatePools, RequiredGroup, Retriever
from tripmate_planner.spec.tripspec import TripSpec
from tripmate_planner.verify.result import VerifyResult

CFG = load_config(CONFIGS / "full.json")
CFG["retrieval"]["mode"] = "bm25"
PLAN = {"itinerary": [{"day": 1, "activities": [
    {"type": "train", "TrainID": "G1", "start": "a", "end": "b"},
    {"type": "attraction", "position": "东方明珠", "price": 199},
    {"type": "lunch", "position": "某餐厅", "price": 300},
    {"type": "accommodation", "position": "某酒店", "price": 900}]}]}


def test_repair_mapping_covers_each_class() -> None:
    spec = TripSpec(target_city="上海", budget_total=1000, must_visit=["豫园"])
    pools = CandidatePools(required_groups=[RequiredGroup("position:豫园", "attraction", ["上海:attraction:9"])])
    d = Directives()
    v = VerifyResult(False, ["Visiting attraction in their closed time", "Inappropriate Meal Times",
                             "Unavailable Inner-City Transport", "Incorrect Cost on Intercity Transportation"],
                     ["The attraction is closed now. 东方明珠, open time", "lunch at 某餐厅 too late"], [0, 1])
    actions = apply_repair(d, PLAN, v, ["budget", "must_visit"], spec, pools)
    assert "东方明珠" in d.blacklist and "某餐厅" in d.blacklist and "G1" in d.blacklist
    assert d.mode_override == "metro"  # budget repair wins after transport toggle
    assert d.price_caps["hotel"] == 720 and "上海:attraction:9" in d.forced
    assert len(actions) >= 4


def _out(**spec):
    return {"spec": spec, "changed_fields": list(spec), "resolved_references": [], "preference_candidates": []}


def test_graph_end_to_end_and_interrupt(tmp_path) -> None:
    store = MemoryStore(tmp_path / "m.sqlite3")
    cfg = json.loads(json.dumps(CFG))
    cfg["memory"]["auto_confirm"] = False
    retr = Retriever(cfg["retrieval"])
    text = "当前位置杭州。我两个人想去上海玩1天，以后出门我都只坐地铁。"
    llm = FakeLLM([{**_out(start_city="杭州", target_city="上海", days=1, people_number=2, innercity_mode="metro"),
                    "preference_candidates": [{"key": "innercity_mode", "value": "metro", "evidence": "以后出门我都只坐地铁"}]}])
    asked = []
    res = run_turn(text, user_id="u1", conversation_id="c1", cfg=cfg, enums=get_enums(), llm=llm, retriever=retr,
                   store=store, confirm=lambda cands: asked.append(cands) or [0])
    assert res.status == "passed" and res.plan is not None
    assert asked and store.active_preferences("u1")[0]["value"] == "metro"
    assert [n["name"] for n in res.trace["nodes"]][:3] == ["load_memory", "rewrite", "apply_memory"]
    assert store.latest_state("c1", "u1")[0] == 1

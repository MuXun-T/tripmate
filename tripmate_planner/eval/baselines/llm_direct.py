"""A9 ablation: the LLM writes the itinerary directly from candidate tables (one call, no verify/repair).

Mechanical fields (inner-city transports from goto, prices/costs/tickets from the sandbox) are filled
deterministically so the comparison isolates planning decisions, not formatting.
"""

import json
import math
from typing import Any

from pydantic import BaseModel, ConfigDict

from tripmate_planner.llm.cost import CallRecord
from tripmate_planner.llm.errors import LLMOutputError, LLMRequestError
from tripmate_planner.plan.search import _intercity, _intercity_activity, _num
from tripmate_planner.plan.timeline import t2m, transport
from tripmate_planner.retrieval.retriever import CandidatePools
from tripmate_planner.spec.tripspec import TripSpec

SYSTEM = """你是行程规划助手。根据用户需求和候选表，直接写出完整行程，只输出 json。
规则：第 1 天第一个活动必须是去程（train 或 airplane，填 id），最后一天最后一个活动必须是返程；多日行程每晚住酒店（accommodation，结束时间 24:00），次日可在酒店吃早餐（breakfast，position 填酒店名，08:00-08:30）；午餐在 11:00-14:00、晚餐在 17:00-20:00 之间；只能使用候选表中的名称和班次；景点和餐厅在营业时间内；同一景点或餐厅不重复。
输出格式：{"itinerary": [{"day": 1, "activities": [{"type": "train|airplane|attraction|breakfast|lunch|dinner|accommodation", "id": "车次或航班号（仅城际交通）", "position": "名称（非城际交通）", "start_time": "HH:MM", "end_time": "HH:MM"}]}]}"""


class _Act(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str
    id: str | None = None
    position: str | None = None
    start_time: str
    end_time: str


class _Day(BaseModel):
    model_config = ConfigDict(extra="ignore")
    day: int
    activities: list[_Act]


class _Out(BaseModel):
    model_config = ConfigDict(extra="ignore")
    itinerary: list[_Day]


def _table(rows: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)


def llm_direct_plan(spec: TripSpec, pools: CandidatePools, cfg: dict[str, Any], llm: Any) -> tuple[dict[str, Any] | None, list[CallRecord]]:
    people = int(spec.people_number or 1)
    go = _intercity(spec, str(spec.start_city), str(spec.target_city), spec.intercity_go, False, set())[:5]
    back = _intercity(spec, str(spec.target_city), str(spec.start_city), spec.intercity_back, True, set())[:5]
    attr = [{"name": c.doc.name, "type": c.doc.meta["type"], "price": _num(c.doc.meta["price"]),
             "open": c.doc.meta["opentime"], "close": c.doc.meta["endtime"],
             "hours": _num(c.doc.meta["recommendmintime"])} for c in pools.attractions[:15]]
    rest = [{"name": c.doc.name, "cuisine": c.doc.meta["cuisine"], "price": _num(c.doc.meta["price"]),
             "open": c.doc.meta["opentime"], "close": c.doc.meta["endtime"]} for c in pools.restaurants[:15]]
    hotels = [{"name": c.doc.name, "feature": c.doc.meta["featurehoteltype"], "price": _num(c.doc.meta["price"]),
               "beds": int(c.doc.meta["numbed"])} for c in pools.hotels[:8]]
    def legs(opts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"id": o.get("TrainID") or o.get("FlightID"), "type": o["kind"], "from": o["From"], "to": o["To"],
                 "depart": o["BeginTime"], "arrive": o["EndTime"], "price": _num(o["Cost"])} for o in opts]
    user = (f"【需求】{json.dumps(spec.core().model_dump(exclude_defaults=True), ensure_ascii=False)}\n"
            f"【去程候选】\n{_table(legs(go))}\n【返程候选】\n{_table(legs(back))}\n【景点】\n{_table(attr)}\n"
            f"【餐厅】\n{_table(rest)}\n【酒店】\n{_table(hotels)}\n请输出 json 行程。")
    try:
        out, records = llm.chat_json(purpose="llm_direct", messages=[{"role": "system", "content": SYSTEM},
                                                                     {"role": "user", "content": user}],
                                     schema=_Out, max_tokens=int(cfg["llm"]["max_tokens"]["llm_direct"]))
    except (LLMOutputError, LLMRequestError):
        return None, []
    by_id = {(o.get("TrainID") or o.get("FlightID")): o for o in [*go, *back]}
    meta = {c.doc.name: (c.doc.kind, c.doc.meta) for c in [*pools.attractions, *pools.restaurants, *pools.hotels]}
    days: list[dict[str, Any]] = []
    pos: str | None = None
    prev_end = 0
    for d in out.itinerary:
        acts: list[dict[str, Any]] = []
        for a in d.activities:
            if a.type in ("train", "airplane") and a.id in by_id:
                o = by_id[a.id]
                segs: list[dict[str, Any]] = []
                if pos is not None and pos != o["From"]:
                    tr = transport(str(spec.target_city), pos, o["From"], prev_end, people, spec.innercity_mode, 1.0)
                    segs = tr[0] if tr else []
                acts.append(_intercity_activity(o, people, segs))
                pos, prev_end = o["To"], t2m(o["EndTime"]) if t2m(o["EndTime"]) > t2m(o["BeginTime"]) else 0
                continue
            if not a.position:
                continue
            kind, m = meta.get(a.position, (None, None))
            segs = []
            if pos is not None and pos != a.position:
                tr = transport(str(spec.target_city), pos, a.position, prev_end, people, spec.innercity_mode, 1.0)
                segs = tr[0] if tr else []
            act: dict[str, Any] = {"type": a.type, "position": a.position, "start_time": a.start_time,
                                   "end_time": a.end_time, "transports": segs}
            if a.type == "breakfast" and kind == "hotel":
                act.update(price=0, cost=0)
            elif m is not None:
                price = _num(m["price"])
                act["price"] = price
                if a.type == "accommodation":
                    rooms = int(spec.rooms or math.ceil(people / max(int(m["numbed"]), 1)))
                    act.update(cost=_num(float(price) * rooms), rooms=rooms, room_type=int(m["numbed"]))
                else:
                    act["cost"] = _num(float(price) * people)
                    if a.type == "attraction":
                        act["tickets"] = people
            acts.append(act)
            pos = a.position
            try:
                prev_end = t2m(a.end_time) if a.end_time != "24:00" else 0
            except ValueError:
                prev_end = 0
        days.append({"day": d.day, "activities": acts})
    plan = {"people_number": people, "start_city": spec.start_city, "target_city": spec.target_city, "itinerary": days}
    return plan, records

"""Deterministic beam-search itinerary planner over retrieved candidate pools (T3.2).

The LLM is never called here; feasibility comes from sandbox tools and official checks.
"""

import itertools
import math
import time
from dataclasses import dataclass, field, replace
from typing import Any

from tripmate_planner.env.sandbox import coords, get_sandbox
from tripmate_planner.plan.scoring import Weights, candidate_score
from tripmate_planner.plan.timeline import DAY_END, haversine_km, m2t, t2m, transport
from tripmate_planner.retrieval.retriever import Candidate, CandidatePools
from tripmate_planner.spec.tripspec import TripSpec

LUNCH_START, LUNCH_LATEST = 11 * 60, 13 * 60
DINNER_START, DINNER_LATEST = 17 * 60, 19 * 60
BREAKFAST = ("08:00", "08:30")
DAY_START = t2m(BREAKFAST[1])


@dataclass
class Directives:
    blacklist: set[str] = field(default_factory=set)  # POI names or TrainID/FlightID
    forced: set[str] = field(default_factory=set)  # doc_ids
    price_caps: dict[str, float] = field(default_factory=dict)  # hotel / restaurant / attraction unit price
    mode_override: str | None = None


@dataclass
class State:
    days: list[list[dict[str, Any]]]
    time: int
    pos: str
    cost: float
    used: frozenset[str]
    meals: frozenset[str]
    n_attr: int
    satisfied: frozenset[str]
    score: float


@dataclass
class PlanResult:
    plan: dict[str, Any] | None
    candidates_checked: int
    combos_tried: int
    timed_out: bool
    ms: float
    best_failures: int | None = None


def _num(x: Any) -> float | int:
    f = float(x)
    return int(f) if f.is_integer() else round(f, 2)


def _intercity(spec: TripSpec, src: str, dst: str, kind: str | None, back: bool,
               blacklist: set[str]) -> list[dict[str, Any]]:
    sb = get_sandbox()
    kinds = [kind] if kind else ["train", "airplane"]
    opts = []
    for k in kinds:
        for row in sb.intercity_options(src, dst, k):
            ident = row.get("TrainID") or row.get("FlightID")
            dep, arr = t2m(row["BeginTime"]), t2m(row["EndTime"])
            if ident in blacklist:
                continue
            # official time check allows next-day arrival for intercity legs (CC Is_time_correct)
            overnight = arr <= dep
            opts.append({**row, "kind": k, "_dep": dep, "_arr": arr, "_overnight": overnight})
    if back:
        # any departure time is allowed; afternoon/evening departures (more time on the last day) are preferred
        opts.sort(key=lambda o: (o["_overnight"], 0 if 16 * 60 <= o["_dep"] <= 21 * 60 else 1 if o["_dep"] >= 12 * 60 else 2,
                                 float(o["Cost"]), -o["_dep"]))
    else:
        # same-day arrival by 18:00, or an overnight train/flight arriving 05:00-12:00 (day 1 starts on arrival)
        opts = [o for o in opts if (not o["_overnight"] and 5 * 60 <= o["_dep"] and o["_arr"] <= 18 * 60)
                or (o["_overnight"] and 5 * 60 <= o["_arr"] <= 12 * 60)]
        opts.sort(key=lambda o: (o["_overnight"], 0 if 6 * 60 <= o["_dep"] <= 12 * 60 else 1, float(o["Cost"]), o["_arr"]))
    return opts


def _intercity_activity(opt: dict[str, Any], people: int, segs: list[dict[str, Any]]) -> dict[str, Any]:
    price = _num(opt["Cost"])
    act = {"type": opt["kind"], "start": opt["From"], "end": opt["To"], "start_time": opt["BeginTime"],
           "end_time": opt["EndTime"], "price": price, "cost": _num(price * people), "tickets": people,
           "transports": segs}
    act["TrainID" if opt["kind"] == "train" else "FlightID"] = opt.get("TrainID") or opt.get("FlightID")
    return act


class Planner:
    def __init__(self, spec: TripSpec, pools: CandidatePools, cfg: dict[str, Any], text: str,
                 checker: Any, directives: Directives | None = None) -> None:
        self.spec, self.pools, self.cfg, self.text = spec, pools, cfg, text
        self.pc = cfg["planner"]
        self.w = Weights.from_cfg(self.pc["weights"])
        self.dir = directives or Directives()
        self.people = int(spec.people_number or 1)
        self.city = str(spec.target_city)
        self.mode = self.dir.mode_override or spec.innercity_mode
        self.checker = checker  # callable(plan) -> n_failures
        self.groups: dict[str, set[str]] = {}
        for g in pools.required_groups:
            for d in g.doc_ids:
                self.groups.setdefault(d, set()).add(g.constraint_ref)
        self.all_refs = {g.constraint_ref for g in pools.required_groups}
        self.budget = spec.budget_total
        self.deadline = time.perf_counter() + float(self.pc["time_budget_s"])

    # ---------- candidate helpers ----------
    def _cands(self, kind: str) -> list[Candidate]:
        out = []
        cap = self.dir.price_caps.get(kind)
        meal_cap = self.spec.meal_price_max_per_person if kind == "restaurant" else None
        for c in self.pools.by_kind(kind):
            price = float(c.doc.meta["price"])
            if c.doc.name in self.dir.blacklist:
                continue
            if cap is not None and price > cap:
                continue
            if meal_cap is not None and price > meal_cap:
                continue
            if kind == "restaurant" and self.spec.avoid_cuisines and c.doc.meta.get("cuisine") in self.spec.avoid_cuisines:
                continue
            out.append(c)
        return out

    def _approx_minutes(self, a: str, b: str) -> float:
        ca, cb = coords(self.city, a), coords(self.city, b)
        if ca is None or cb is None:
            return 20.0
        return haversine_km(ca[0], ca[1], cb[0], cb[1]) / 25 * 60 + 5

    # ---------- day expansion ----------
    def _expand(self, s: State, last_day: bool, back: dict[str, Any] | None) -> list[State]:
        out: list[State] = []
        max_attr = int(self.pc["attractions_per_day"][self.spec.pace])
        options: list[tuple[str, Candidate]] = []
        if "lunch" not in s.meals and s.time <= LUNCH_LATEST:
            options += [("lunch", c) for c in self._cands("restaurant")]
        if "dinner" not in s.meals and s.time <= DINNER_LATEST and s.time >= 15 * 60:
            options += [("dinner", c) for c in self._cands("restaurant")]
        if s.n_attr < max_attr:
            options += [("attraction", c) for c in self._cands("attraction")]
        pre = []
        for typ, c in options:
            if c.doc.name in s.used:
                continue
            new_req = len(self.groups.get(c.doc.doc_id, set()) - s.satisfied)
            approx = self._approx_minutes(s.pos, c.doc.name)
            price = float(c.doc.meta["price"]) * self.people
            sc = candidate_score(self.w, rel_rank=c.rel_rank, transit_min=approx, total_price=price,
                                 budget_set=self.budget is not None, new_required=new_req,
                                 forced=c.doc.doc_id in self.dir.forced)
            pre.append((sc, typ, c))
        pre.sort(key=lambda x: -x[0])
        per_type: dict[str, int] = {}
        m = int(self.pc["candidates_per_step"])
        for _, typ, c in pre:
            if per_type.get(typ, 0) >= m:
                continue
            nxt = self._visit(s, typ, c, last_day, back)
            if nxt is not None:
                per_type[typ] = per_type.get(typ, 0) + 1
                out.append(nxt)
        return out

    def _visit(self, s: State, typ: str, c: Candidate, last_day: bool, back: dict[str, Any] | None) -> State | None:
        meta = c.doc.meta
        tr = transport(self.city, s.pos, c.doc.name, s.time, self.people, self.mode, float(self.pc["walk_max_km"]))
        if tr is None:
            return None
        segs, arrival, tcost = tr
        open_m, close_m = t2m(str(meta["opentime"])), t2m(str(meta["endtime"]))
        if close_m <= open_m:
            close_m += 24 * 60
        start = max(arrival, open_m)
        if typ == "attraction":
            dur = int(min(max(float(meta["recommendmintime"]) * 60, 60), 180))
        else:
            start = max(start, LUNCH_START if typ == "lunch" else DINNER_START)
            if start > (LUNCH_LATEST if typ == "lunch" else DINNER_LATEST):
                return None
            dur = int(self.pc["meal_minutes"])
        end = start + dur
        if end > close_m or end >= DAY_END - 30:
            return None
        if last_day and back is not None and end + 30 > back["_dep"] - int(self.pc["back_buffer_min"]):
            return None
        price = _num(meta["price"])
        act: dict[str, Any] = {"type": typ, "position": c.doc.name, "start_time": m2t(start), "end_time": m2t(end),
                               "price": price, "cost": _num(float(price) * self.people), "transports": segs}
        if typ == "attraction":
            act["tickets"] = self.people
        cost = s.cost + tcost + float(act["cost"])
        if self._over_budget(cost, s, last_day):
            return None
        new_refs = self.groups.get(c.doc.doc_id, set())
        approx = (arrival - s.time)
        sc = candidate_score(self.w, rel_rank=c.rel_rank, transit_min=approx, total_price=float(act["cost"]),
                             budget_set=self.budget is not None, new_required=len(new_refs - s.satisfied),
                             forced=c.doc.doc_id in self.dir.forced)
        days = [d[:] for d in s.days]
        days[-1].append(act)
        return State(days=days, time=end, pos=c.doc.name, cost=cost, used=s.used | {c.doc.name},
                     meals=s.meals | ({typ} if typ != "attraction" else set()),
                     n_attr=s.n_attr + (typ == "attraction"), satisfied=s.satisfied | new_refs, score=s.score + sc + 1.0)

    def _over_budget(self, cost: float, s: State, last_day: bool) -> bool:
        if self.budget is None:
            return False
        rest = self._cands("restaurant")
        min_meal = min((float(c.doc.meta["price"]) for c in rest), default=0.0) * self.people
        remaining_days = self.n_days - len(s.days)
        lb = min_meal * 2 * remaining_days
        return cost + self.fixed_remaining + lb > float(self.budget) + 1e-6

    def _finish_day(self, s: State, last_day: bool, hotel: dict[str, Any] | None,
                    back: dict[str, Any] | None) -> State | None:
        days = [d[:] for d in s.days]
        if last_day:
            assert back is not None
            tr = transport(self.city, s.pos, back["From"], s.time, self.people, self.mode, float(self.pc["walk_max_km"]))
            if tr is None:
                return None
            segs, arrival, tcost = tr
            if arrival > back["_dep"] - int(self.pc["back_buffer_min"]):
                return None
            days[-1].append(_intercity_activity(back, self.people, segs))
            return replace(s, days=days, time=DAY_END, cost=s.cost + tcost + float(back["Cost"]) * self.people)
        assert hotel is not None
        tr = transport(self.city, s.pos, hotel["name"], s.time, self.people, self.mode, float(self.pc["walk_max_km"]))
        if tr is None:
            return None
        segs, arrival, tcost = tr
        if arrival >= DAY_END - 1:
            return None
        days[-1].append({"type": "accommodation", "position": hotel["name"], "start_time": m2t(arrival),
                         "end_time": "24:00", "price": _num(hotel["price"]),
                         "cost": _num(float(hotel["price"]) * hotel["_rooms"]), "rooms": hotel["_rooms"],
                         "room_type": int(hotel["numbed"]), "transports": segs})
        return replace(s, days=days, cost=s.cost + tcost, pos=hotel["name"])

    def _plan_days(self, go: dict[str, Any], back: dict[str, Any], hotel: dict[str, Any] | None) -> list[State]:
        segs: list[dict[str, Any]] = []
        first = [_intercity_activity(go, self.people, segs)]
        self.fixed_remaining = float(back["Cost"]) * self.people + (
            float(hotel["price"]) * hotel["_rooms"] * (self.n_days - 1) if hotel else 0.0)
        beam = [State(days=[first], time=go["_arr"], pos=go["To"], cost=float(go["Cost"]) * self.people,
                      used=frozenset(), meals=frozenset(), n_attr=0, satisfied=frozenset(), score=0.0)]
        width = int(self.pc["beam_width"])
        for day in range(1, self.n_days + 1):
            last = day == self.n_days
            nights_left = self.n_days - day  # nights not yet paid after this day's breakfast
            self.fixed_remaining = float(back["Cost"]) * self.people + (
                float(hotel["price"]) * hotel["_rooms"] * nights_left if hotel else 0.0)
            if day > 1:
                assert hotel is not None
                beam = [replace(s, days=[*s.days, [{"type": "breakfast", "position": hotel["name"],
                                                     "start_time": BREAKFAST[0], "end_time": BREAKFAST[1],
                                                     "price": 0, "cost": 0, "transports": []}]],
                                time=DAY_START, pos=hotel["name"], meals=frozenset({"breakfast"}), n_attr=0,
                                cost=s.cost + float(hotel["price"]) * hotel["_rooms"])
                        for s in beam]
            finished: list[State] = []
            frontier = beam
            for _ in range(12):
                if time.perf_counter() > self.deadline:
                    break
                nxt: list[State] = []
                for s in frontier:
                    done = self._finish_day(s, last, hotel, back if last else None)
                    if done is not None:
                        finished.append(done)
                    nxt.extend(self._expand(s, last, back if last else None))
                if not nxt:
                    break
                nxt.sort(key=lambda s: -s.score)
                frontier = nxt[:width]
            finished.sort(key=lambda s: -(s.score - 10 * len(self.all_refs - s.satisfied)))
            beam = finished[:width]
            if not beam:
                return []
        return beam

    def _hotels(self) -> list[dict[str, Any]]:
        hotel_refs = {g.constraint_ref for g in self.pools.required_groups if g.kind == "hotel"}
        out = []
        for c in self._cands("hotel"):
            refs = self.groups.get(c.doc.doc_id, set())
            if hotel_refs and not hotel_refs <= refs:
                continue
            if self.spec.room_type is not None and int(c.doc.meta["numbed"]) != self.spec.room_type:
                continue
            row = dict(c.doc.meta)
            row["name"] = c.doc.name
            numbed = max(int(row["numbed"]), 1)
            row["_rooms"] = int(self.spec.rooms or math.ceil(self.people / numbed))
            row["_rel"] = c.rel_rank
            out.append(row)
        key = (lambda r: (float(r["price"]), r["_rel"])) if self.budget is not None else (lambda r: (r["_rel"], float(r["price"])))
        return sorted(out, key=key)[:3]

    def run(self) -> PlanResult:
        started = time.perf_counter()
        spec = self.spec
        self.n_days = int(spec.days or 1)
        go_opts = _intercity(spec, str(spec.start_city), self.city, spec.intercity_go, False, self.dir.blacklist)
        back_opts = _intercity(spec, self.city, str(spec.start_city), spec.intercity_back, True, self.dir.blacklist)
        k = int(self.pc["intercity_candidates"])
        go_opts, back_opts = go_opts[:k], back_opts[:k]
        hotels: list[dict[str, Any] | None] = list(self._hotels()) if self.n_days > 1 else [None]
        combos = []
        for go, back, hotel in itertools.product(go_opts, back_opts, hotels):
            if self.n_days == 1 and (go["_overnight"] or back["_dep"] <= go["_arr"]):
                continue
            fixed = (float(go["Cost"]) + float(back["Cost"])) * self.people + (
                float(hotel["price"]) * hotel["_rooms"] * (self.n_days - 1) if hotel else 0.0)
            if self.budget is not None and fixed > float(self.budget):
                continue
            combos.append((fixed, go, back, hotel))
        combos.sort(key=lambda x: x[0])
        best: tuple[int, float, dict[str, Any]] | None = None
        checked = tried = 0
        timed_out = False
        for _fixed, go, back, hotel in combos[: int(self.pc["max_combos"])]:
            if time.perf_counter() > self.deadline:
                timed_out = True
                break
            tried += 1
            for s in self._plan_days(go, back, hotel)[:3]:
                plan = {"people_number": self.people, "start_city": spec.start_city, "target_city": self.city,
                        "itinerary": [{"day": i + 1, "activities": acts} for i, acts in enumerate(s.days)]}
                n_fail = self.checker(plan)
                checked += 1
                if best is None or (n_fail, s.cost) < (best[0], best[1]):
                    best = (n_fail, s.cost, plan)
                if n_fail == 0:
                    return PlanResult(plan, checked, tried, False, (time.perf_counter() - started) * 1000, 0)
        return PlanResult(best[2] if best else None, checked, tried, timed_out,
                          (time.perf_counter() - started) * 1000, best[0] if best else None)

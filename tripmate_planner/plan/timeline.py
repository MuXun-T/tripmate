"""Time helpers and official-format inner-city transport segments built from Transportation.goto."""

import math
from typing import Any

from tripmate_planner.env.sandbox import get_sandbox

DAY_END = 24 * 60


def t2m(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def m2t(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def cars_for(people: int) -> int:
    return math.ceil(people / 4)


def _segments(raw: tuple[dict[str, Any], ...], people: int) -> list[dict[str, Any]]:
    """Copy goto output exactly; add price/tickets/cars and cost per docs/planner/official-rules.md."""
    out = []
    for seg in raw:
        s = {"start": seg["start"], "end": seg["end"], "mode": seg["mode"], "start_time": seg["start_time"],
             "end_time": seg["end_time"], "distance": float(seg["distance"])}
        unit = float(seg["cost"])
        if seg["mode"] == "walk":
            s["price"], s["cost"] = 0, 0
        elif seg["mode"] == "metro":
            s["price"], s["tickets"], s["cost"] = unit, people, round(unit * people, 2)
        else:  # taxi
            cars = cars_for(people)
            s["price"], s["cars"], s["cost"] = unit, cars, round(unit * cars, 2)
        out.append(s)
    return out


def transport(city: str, start: str, end: str, start_min: int, people: int, mode: str | None,
              walk_max_km: float) -> tuple[list[dict[str, Any]], int, float] | None:
    """Return (segments, arrival minute, cost) or None if infeasible. Same position -> no transport."""
    if start == end:
        return [], start_min, 0.0
    if start_min >= DAY_END:
        return None
    sb = get_sandbox()
    t = m2t(start_min)
    chosen: tuple[dict[str, Any], ...] | None
    if mode is None:
        walk = sb.goto(city, start, end, t, "walk")
        if walk is not None and len(walk) == 1 and float(walk[0]["distance"]) <= walk_max_km:
            chosen = walk
        else:
            metro = sb.goto(city, start, end, t, "metro")
            chosen = metro if metro is not None and len(metro) == 3 else sb.goto(city, start, end, t, "taxi")
    else:
        chosen = sb.goto(city, start, end, t, mode)
        expected = 3 if mode == "metro" else 1
        if chosen is not None and len(chosen) != expected:
            return None
    if chosen is None:
        return None
    segs = _segments(chosen, people)
    arrival = t2m(segs[-1]["end_time"])
    if arrival < start_min or arrival >= DAY_END:
        return None
    return segs, arrival, float(sum(s["cost"] for s in segs))

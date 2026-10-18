"""Parse oracle hard_logic_py constraints into TripSpec-aligned labels (evaluation only, rule R4)."""

import re
from dataclasses import dataclass

from tripmate_planner.spec.tripspec import TripSpec

DEFAULT_PATTERNS = (r"activity_tickets\(activity\)!=", r"taxi_cars\(activity_transports")


@dataclass(frozen=True)
class Label:
    field: str
    value: object


def _set_values(code: str) -> list[str]:
    m = re.search(r"\{([^}]*)\}\s*(?:<=|==)", code) or re.search(r"<=\s*\{([^}]*)\}", code)
    return re.findall(r"'([^']*)'", m.group(1)) if m else []


def parse_constraint(code: str) -> tuple[str, list[Label]]:
    """Return (kind, labels) where kind is 'default', 'covered' or 'uncovered'."""
    if any(re.search(p, code) for p in DEFAULT_PATTERNS):
        return "default", []
    if m := re.fullmatch(r"result=\(day_count\(plan\)==(\d+)\)", code.strip()):
        return "covered", [Label("days", int(m.group(1)))]
    if m := re.fullmatch(r"result=\(people_count\(plan\)==(\d+)\)", code.strip()):
        return "covered", [Label("people_number", int(m.group(1)))]
    if "total_cost+=activity_cost" in code and (m := re.search(r"total_cost<=\s*(\d+(?:\.\d+)?)", code)):
        return "covered", [Label("budget_total", float(m.group(1)))]
    if m := re.search(r"room_count\(activity\)!=(\d+)", code):
        return "covered", [Label("rooms", int(m.group(1)))]
    if m := re.search(r"room_type\(activity\)!=(\d+)", code):
        return "covered", [Label("room_type", int(m.group(1)))]
    if "intercity_transport_set" in code and "==intercity_transport_set" in code:
        vals = _set_values(code)
        if len(vals) == 1:
            return "covered", [Label("intercity", vals[0])]
    table = {"attraction_name_set": "must_visit", "restaurant_type_set": "cuisines",
             "attraction_type_set": "attraction_types", "accommodation_type_set": "hotel_features",
             "restaurant_name_set": "must_eat_at", "accommodation_name_set": "must_stay_at"}
    for var, field in table.items():
        if f"{var}.add" in code and re.search(r"\}\s*<=\s*" + var, code):
            return "covered", [Label(field, v) for v in _set_values(code)]
    if "innercity_transport_set" in code:
        vals = _set_values(code)
        if vals == ["metro"] or vals == ["taxi"] or vals == ["walk"]:
            return "covered", [Label("innercity_mode", vals[0])]
    if "food_cost/food_count/people_count(plan)" in code and (m := re.search(r"<=\s*(\d+(?:\.\d+)?)", code)):
        return "covered", [Label("meal_price_max_per_person", float(m.group(1)))]
    return "uncovered", []


def oracle_labels(hard_logic_py: list[str]) -> tuple[set[Label], int, list[str]]:
    """Return (covered labels, number of non-default constraints, uncovered raw constraints)."""
    labels: set[Label] = set()
    non_default = 0
    uncovered: list[str] = []
    for code in hard_logic_py:
        kind, items = parse_constraint(code)
        if kind == "default":
            continue
        non_default += 1
        if kind == "uncovered":
            uncovered.append(code)
        labels.update(items)
    return labels, non_default, uncovered


def spec_to_labels(spec: TripSpec) -> set[Label]:
    labels: set[Label] = set()
    for field in ("days", "people_number", "rooms", "room_type", "innercity_mode"):
        value = getattr(spec, field)
        if value is not None:
            labels.add(Label(field, value))
    for field in ("budget_total", "meal_price_max_per_person"):
        value = getattr(spec, field)
        if value is not None:
            labels.add(Label(field, float(value)))
    if spec.intercity_go is not None and spec.intercity_go == spec.intercity_back:
        labels.add(Label("intercity", spec.intercity_go))
    for field in ("must_visit", "cuisines", "attraction_types", "hotel_features", "must_eat_at"):
        labels.update(Label(field, v) for v in getattr(spec, field))
    if spec.must_stay_at:
        labels.add(Label("must_stay_at", spec.must_stay_at))
    return labels


def gold_from_record(record: dict) -> tuple[set[Label], int, list[str]]:
    """Gold labels for one oracle record (the only place that reads the oracle field)."""
    return oracle_labels(record["hard_logic_py"])

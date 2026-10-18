"""Deterministic TripSpec -> official DSL compiler (templates mirror dev oracle wording, T1.4).

Example uids for each template are taken from dev (see docs/planner/official-rules.md §2-§3).
Each constraint carries a tag so the repair loop can map a failed index to an action.
"""

import math

from tripmate_planner.spec.tripspec import TripSpec

_LOOP = "result=True\nfor activity in allactivities(plan):\n"


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _set(values: list[str]) -> str:
    return "{" + ", ".join(repr(v) for v in sorted(values)) + "}"


def _subset(var: str, add_cond: str, add_expr: str, values: list[str]) -> str:
    return (f"{var}=set()\nfor activity in allactivities(plan):\n  if {add_cond}: {var}.add({add_expr})\n"
            f"result=({_set(values)}<={var})")


def compile_dsl_tagged(spec: TripSpec) -> list[tuple[str, str]]:
    people = spec.people_number or 1
    meal = "activity_type(activity) in ['breakfast', 'lunch', 'dinner']"
    out: list[tuple[str, str]] = []
    if spec.days is not None:  # e20241028160248698752
        out.append(("days", f"result=(day_count(plan)=={spec.days})"))
    out.append(("people", f"result=(people_count(plan)=={people})"))
    out.append(("tickets", _LOOP
                + f"  if activity_type(activity) in ['attraction', 'airplane', 'train'] and activity_tickets(activity)!={people}: result=False\n"
                + f"  if innercity_transport_type(activity_transports(activity))=='metro'and metro_tickets(activity_transports(activity))!={people}: result=False"))
    out.append(("cars", _LOOP + "  if innercity_transport_type(activity_transports(activity))=='taxi'and "
                f"taxi_cars(activity_transports(activity))!={math.ceil(people / 4)}: result=False"))
    if spec.budget_total is not None:  # e20241028160248698752
        out.append(("budget", "total_cost=0 \nfor activity in allactivities(plan):\n    total_cost+=activity_cost(activity)\n"
                    f"    total_cost += innercity_transport_cost(activity_transports(activity))\nresult=(total_cost<={_num(spec.budget_total)})"))
    if spec.rooms is not None:  # h20241204223651527057
        out.append(("rooms", _LOOP + f"  if activity_type(activity)=='accommodation' and room_count(activity)!={spec.rooms}: result=False"))
    if spec.room_type is not None:  # h20241204223651527057
        out.append(("room_type", _LOOP + f"  if activity_type(activity)=='accommodation' and room_type(activity)!={spec.room_type}: result=False"))
    modes = sorted({m for m in (spec.intercity_go, spec.intercity_back) if m is not None})
    if modes:  # t20241206164116321945
        out.append(("intercity", "intercity_transport_set=set()\nfor activity in allactivities(plan):\n"
                    "  if activity_type(activity) in ['train', 'airplane']: intercity_transport_set.add(intercity_transport_type(activity))\n"
                    f"result=({_set(modes)}==intercity_transport_set)"))
    if spec.must_visit:  # m20241028164822843097
        out.append(("must_visit", _subset("attraction_name_set", "activity_type(activity)=='attraction'",
                                          "activity_position(activity)", spec.must_visit)))
    if spec.cuisines:  # m20241028164637595271
        out.append(("cuisines", _subset("restaurant_type_set", meal, "restaurant_type(activity, target_city(plan))", spec.cuisines)))
    if spec.avoid_cuisines:  # 本项目自定义：忌口（oracle 中未见此模板）
        out.append(("avoid_cuisines", f"restaurant_type_set=set()\nfor activity in allactivities(plan):\n  if {meal}: "
                    "restaurant_type_set.add(restaurant_type(activity, target_city(plan)))\n"
                    f"result=(len({_set(spec.avoid_cuisines)} & restaurant_type_set)==0)"))
    if spec.attraction_types:  # m20241028164620332052
        out.append(("attraction_types", _subset("attraction_type_set", "activity_type(activity)=='attraction'",
                                                "attraction_type(activity, target_city(plan))", spec.attraction_types)))
    if spec.hotel_features:  # h20241204223651527057
        out.append(("hotel_features", _subset("accommodation_type_set", "activity_type(activity) == 'accommodation'",
                                              "accommodation_type(activity, target_city(plan))", spec.hotel_features)))
    if spec.must_eat_at:  # m20241028164905266860
        out.append(("must_eat_at", _subset("restaurant_name_set", meal, "activity_position(activity)", spec.must_eat_at)))
    if spec.must_stay_at:  # m20241028164947135130
        out.append(("must_stay_at", _subset("accommodation_name_set", "activity_type(activity)=='accommodation'",
                                            "activity_position(activity)", [spec.must_stay_at])))
    if spec.innercity_mode is not None:  # t20241206171807314229（"只用"语义，兼容"至少用过"）
        out.append(("innercity", "innercity_transport_set=set()\nfor activity in allactivities(plan):\n"
                    "  if activity_transports(activity)!=[]: innercity_transport_set.add(innercity_transport_type(activity_transports(activity)))\n"
                    f"result=(innercity_transport_set<={_set([spec.innercity_mode])})"))
    if spec.meal_price_max_per_person is not None:  # e20241028161340383778
        out.append(("meal", "food_cost,food_count=0,0\nfor activity in allactivities(plan):\n"
                    f"    if {meal}:\n        food_cost+=activity_cost(activity)\n        food_count+=1\n"
                    f"result=(food_cost/food_count/people_count(plan)<={_num(spec.meal_price_max_per_person)})"))
    return out


def compile_dsl(spec: TripSpec) -> list[str]:
    return [code for _, code in compile_dsl_tagged(spec)]

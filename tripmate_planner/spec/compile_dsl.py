"""Deterministic TripSpec -> official DSL compiler (templates mirror dev oracle wording, T1.4).

Example uids for each template are taken from dev (see docs/planner/official-rules.md §2-§3).
"""

import math

from tripmate_planner.spec.tripspec import TripSpec


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _set(values: list[str]) -> str:
    return "{" + ", ".join(repr(v) for v in sorted(values)) + "}"


def compile_dsl(spec: TripSpec) -> list[str]:
    people = spec.people_number or 1
    out: list[str] = []
    if spec.days is not None:
        out.append(f"result=(day_count(plan)=={spec.days})")  # e20241028160248698752
    out.append(f"result=(people_count(plan)=={people})")
    out.append(  # default: tickets == people
        "result=True\nfor activity in allactivities(plan):\n"
        f"  if activity_type(activity) in ['attraction', 'airplane', 'train'] and activity_tickets(activity)!={people}: result=False\n"
        f"  if innercity_transport_type(activity_transports(activity))=='metro'and metro_tickets(activity_transports(activity))!={people}: result=False")
    out.append(  # default: taxi cars == ceil(people / 4)
        "result=True\nfor activity in allactivities(plan):\n"
        f"  if innercity_transport_type(activity_transports(activity))=='taxi'and taxi_cars(activity_transports(activity))!={math.ceil(people / 4)}: result=False")
    if spec.budget_total is not None:  # e20241028160248698752
        out.append("total_cost=0 \nfor activity in allactivities(plan):\n    total_cost+=activity_cost(activity)\n"
                   f"    total_cost += innercity_transport_cost(activity_transports(activity))\nresult=(total_cost<={_num(spec.budget_total)})")
    if spec.rooms is not None:  # h20241204223651527057
        out.append("result=True\nfor activity in allactivities(plan):\n"
                   f"  if activity_type(activity)=='accommodation' and room_count(activity)!={spec.rooms}: result=False")
    if spec.room_type is not None:  # h20241204223651527057
        out.append("result=True\nfor activity in allactivities(plan):\n"
                   f"  if activity_type(activity)=='accommodation' and room_type(activity)!={spec.room_type}: result=False")
    modes = {m for m in (spec.intercity_go, spec.intercity_back) if m is not None}
    if modes:  # t20241206164116321945
        out.append("intercity_transport_set=set()\nfor activity in allactivities(plan):\n"
                   "  if activity_type(activity) in ['train', 'airplane']: intercity_transport_set.add(intercity_transport_type(activity))\n"
                   f"result=({_set(sorted(modes))}==intercity_transport_set)")
    if spec.must_visit:  # m20241028164822843097
        out.append("attraction_name_set=set()\nfor activity in allactivities(plan):\n"
                   "  if activity_type(activity)=='attraction': attraction_name_set.add(activity_position(activity))\n"
                   f"result=({_set(spec.must_visit)}<=attraction_name_set)")
    if spec.cuisines:  # m20241028164637595271
        out.append("restaurant_type_set=set()\nfor activity in allactivities(plan):\n"
                   "  if activity_type(activity) in ['breakfast', 'lunch', 'dinner']: restaurant_type_set.add(restaurant_type(activity, target_city(plan)))\n"
                   f"result=({_set(spec.cuisines)}<=restaurant_type_set)")
    if spec.avoid_cuisines:  # 本项目自定义：忌口（oracle 中未见此模板）
        out.append("restaurant_type_set=set()\nfor activity in allactivities(plan):\n"
                   "  if activity_type(activity) in ['breakfast', 'lunch', 'dinner']: restaurant_type_set.add(restaurant_type(activity, target_city(plan)))\n"
                   f"result=(len({_set(spec.avoid_cuisines)} & restaurant_type_set)==0)")
    if spec.attraction_types:  # m20241028164620332052
        out.append("attraction_type_set=set()\nfor activity in allactivities(plan):\n"
                   "  if activity_type(activity)=='attraction': attraction_type_set.add(attraction_type(activity, target_city(plan)))\n"
                   f"result=({_set(spec.attraction_types)}<=attraction_type_set)")
    if spec.hotel_features:  # h20241204223651527057
        out.append("accommodation_type_set=set()\nfor activity in allactivities(plan):\n"
                   "  if activity_type(activity) == 'accommodation': accommodation_type_set.add(accommodation_type(activity, target_city(plan)))\n"
                   f"result=({_set(spec.hotel_features)}<=accommodation_type_set)")
    if spec.must_eat_at:  # m20241028164905266860
        out.append("restaurant_name_set=set()\nfor activity in allactivities(plan):\n"
                   "  if activity_type(activity) in ['breakfast', 'lunch', 'dinner']: restaurant_name_set.add(activity_position(activity))\n"
                   f"result=({_set(spec.must_eat_at)}<=restaurant_name_set)")
    if spec.must_stay_at:  # m20241028164947135130
        out.append("accommodation_name_set=set()\nfor activity in allactivities(plan):\n"
                   "  if activity_type(activity)=='accommodation': accommodation_name_set.add(activity_position(activity))\n"
                   f"result=({_set([spec.must_stay_at])}<=accommodation_name_set)")
    if spec.innercity_mode is not None:  # t20241206171807314229（"只用"语义，兼容"至少用过"）
        out.append("innercity_transport_set=set()\nfor activity in allactivities(plan):\n"
                   "  if activity_transports(activity)!=[]: innercity_transport_set.add(innercity_transport_type(activity_transports(activity)))\n"
                   f"result=(innercity_transport_set<={_set([spec.innercity_mode])})")
    if spec.meal_price_max_per_person is not None:  # e20241028161340383778
        out.append("food_cost,food_count=0,0\nfor activity in allactivities(plan):\n"
                   "    if activity_type(activity) in ['breakfast', 'lunch', 'dinner']:\n"
                   "        food_cost+=activity_cost(activity)\n        food_count+=1\n"
                   f"result=(food_cost/food_count/people_count(plan)<={_num(spec.meal_price_max_per_person)})")
    return out

"""Map verification failures to planner directives (T3.4 mapping table)."""

from typing import Any

from tripmate_planner.plan.search import Directives
from tripmate_planner.retrieval.retriever import CandidatePools
from tripmate_planner.spec.tripspec import TripSpec
from tripmate_planner.verify.result import VerifyResult

ATTRACTION_COLS = {"Visiting attraction in their closed time", "Unavailable attractions", "Repeated attraction Choices",
                   "Incorrect price Information of attraction", "Incorrect cost Information of attraction"}
RESTAURANT_COLS = {"Visiting Restruants in their closed time", "Unavailable Restruants", "Repeated Restruants Choices",
                   "Inappropriate Meal Times", "Repeated Meal Types in One Day", "Incorrect price Information of Restruants",
                   "Incorrect cost Information of Restruants"}
TRANSPORT_COLS = {"Invalid Transport information across positions", "Unavailable Inner-City Transport",
                  "Incorrect Information of Inner-City Transporton on price, distance, and duration",
                  "Incorrect cost information of Inner-City Transport", "Inccorrect cost information of Inner-City Transport"}
INTERCITY_COLS = {"Intercity transportation events must occur",
                  "Invalid Trains or Airplanes, given TrainID/FlightID, origin and destination",
                  "Incorrect Information of Intercity Transport on price or duration", "Incorrect Cost on Intercity Transportation"}
HOTEL_COLS = {"Unavailable Accommodation", "Incorrect Information of Accommodation on price or room type",
              "Incorrect cost Information of Accommodation"}
REQUIRED_TAGS = {"must_visit": "position", "cuisines": "restaurant_type", "attraction_types": "attraction_type",
                 "hotel_features": "accommodation_type", "must_eat_at": "position", "must_stay_at": "position"}


def _acts(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [a for day in plan.get("itinerary", []) for a in day.get("activities", [])]


def apply_repair(d: Directives, plan: dict[str, Any] | None, v: VerifyResult, tags: list[str], spec: TripSpec,
                 pools: CandidatePools) -> list[str]:
    """Mutate directives in place; return the list of actions taken (for the trace)."""
    actions: list[str] = []
    acts = _acts(plan) if plan else []
    msgs = " ".join(v.commonsense_messages)
    failed = set(v.commonsense_failed)
    for typ, cols in (("attraction", ATTRACTION_COLS), ("restaurant", RESTAURANT_COLS)):
        if failed & cols:
            kinds = {"attraction"} if typ == "attraction" else {"breakfast", "lunch", "dinner"}
            names = [a["position"] for a in acts if a.get("type") in kinds and a.get("position") and a["position"] in msgs]
            if not names and acts:  # repeated choices: blacklist duplicates
                seen: set[str] = set()
                for a in acts:
                    if a.get("type") in kinds and a.get("position"):
                        if a["position"] in seen:
                            names.append(a["position"])
                        seen.add(a["position"])
            for n in names:
                d.blacklist.add(n)
            actions.append(f"blacklist {typ}: {names[:5]}")
    if failed & TRANSPORT_COLS and spec.innercity_mode is None:
        d.mode_override = "taxi" if d.mode_override in (None, "metro") else "metro"
        actions.append(f"mode_override={d.mode_override}")
    if failed & INTERCITY_COLS or failed & HOTEL_COLS:
        for a in acts:
            ident = a.get("TrainID") or a.get("FlightID")
            if ident:
                d.blacklist.add(str(ident))
            if a.get("type") == "accommodation" and failed & HOTEL_COLS:
                d.blacklist.add(a["position"])
        actions.append("blacklist intercity/hotel")
    failed_tags = {tags[i] for i in v.spec_failed if i < len(tags)}
    if "budget" in failed_tags or "meal" in failed_tags:
        for kind, types in (("hotel", {"accommodation"}), ("restaurant", {"lunch", "dinner"}), ("attraction", {"attraction"})):
            prices = [float(a["price"]) for a in acts if a.get("type") in types and float(a.get("price", 0)) > 0]
            if prices:
                d.price_caps[kind] = min(d.price_caps.get(kind, max(prices)), max(prices)) * 0.8
        if spec.innercity_mode is None:
            d.mode_override = "metro"
        actions.append(f"price_caps={ {k: round(v2) for k, v2 in d.price_caps.items()} }")
    for tag in failed_tags & set(REQUIRED_TAGS):
        for g in pools.required_groups:
            if g.constraint_ref.startswith(REQUIRED_TAGS[tag] + ":") and g.doc_ids:
                d.forced.add(g.doc_ids[0])
        actions.append(f"force {tag}")
    if "innercity" in failed_tags and spec.innercity_mode:
        d.mode_override = spec.innercity_mode
        actions.append(f"mode_override={spec.innercity_mode}")
    return actions

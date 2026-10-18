"""Deterministic merge of long-term preferences into a TripSpec (T4.2).

Priority: explicitly stated in this conversation > long-term preference > default.
cuisines_prefer is a soft hint only (added to retrieval text), never a hard constraint.
"""

from typing import Any

from tripmate_planner.spec.tripspec import TripSpec

EXPLICIT = {"rule", "llm"}


def _explicit(spec: TripSpec, field: str) -> bool:
    return spec.field_sources.get(field) in EXPLICIT


def apply_preferences(spec: TripSpec, prefs: list[dict[str, Any]]) -> tuple[TripSpec, list[str], list[str]]:
    """Return (spec, applied keys, soft retrieval hints)."""
    spec = spec.model_copy(deep=True)
    applied: list[str] = []
    hints: list[str] = []
    for pref in prefs:
        key, value = pref["key"], pref["value"]
        if key == "innercity_mode" and spec.innercity_mode is None and not _explicit(spec, "innercity_mode"):
            spec.innercity_mode = value
            spec.field_sources["innercity_mode"] = "memory"
            applied.append(key)
        elif key == "intercity_mode" and spec.intercity_go is None and spec.intercity_back is None:
            spec.intercity_go = spec.intercity_back = value
            spec.field_sources["intercity_go"] = spec.field_sources["intercity_back"] = "memory"
            applied.append(key)
        elif key == "cuisines_avoid" and not spec.avoid_cuisines:
            spec.avoid_cuisines = list(value)
            spec.field_sources["avoid_cuisines"] = "memory"
            applied.append(key)
        elif key == "hotel_features" and not spec.hotel_features:
            spec.hotel_features = list(value)
            spec.field_sources["hotel_features"] = "memory"
            applied.append(key)
        elif key == "room_type" and spec.room_type is None:
            spec.room_type = 1 if int(value) == 1 else 2
            spec.field_sources["room_type"] = "memory"
            applied.append(key)
        elif key == "pace" and spec.pace == "normal" and not _explicit(spec, "pace"):
            spec.pace = value
            spec.field_sources["pace"] = "memory"
            applied.append(key)
        elif key == "cuisines_prefer":
            hints.extend(value)
            applied.append(key)
    return spec, applied, hints

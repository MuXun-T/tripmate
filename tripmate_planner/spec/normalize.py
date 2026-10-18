"""Normalize a TripSpec against sandbox enums and exact POI names."""

from fuzzywuzzy import fuzz, process

from tripmate_planner.env.enums import Enums
from tripmate_planner.spec.tripspec import TripSpec

FUZZY_CUTOFF = 85
ENUM_FIELDS = {"attraction_types": "attraction_types", "cuisines": "cuisines",
               "avoid_cuisines": "cuisines", "hotel_features": "hotel_features"}
NAME_FIELDS = {"must_visit": "attraction", "must_eat_at": "restaurant"}


def _match_name(name: str, candidates: tuple[str, ...]) -> str | None:
    if name in candidates:
        return name
    best = process.extractOne(name, candidates, scorer=fuzz.ratio)
    if best is not None and best[1] >= FUZZY_CUTOFF:
        return str(best[0])
    return None


def normalize_spec(spec: TripSpec, enums: Enums) -> tuple[TripSpec, list[str]]:
    data = spec.model_dump()
    issues: list[str] = []
    city = spec.target_city
    for field, enum_field in ENUM_FIELDS.items():
        allowed = set(enums.for_city(enum_field, city))
        kept = []
        for value in data[field]:
            if value in allowed:
                if value not in kept:
                    kept.append(value)
            else:
                issues.append(f"{field}: dropped non-enum value {value!r}")
        data[field] = kept
    for field, kind in NAME_FIELDS.items():
        kept = []
        for name in data[field]:
            matched = _match_name(name, enums.names[(city, kind)]) if city else None
            if matched is None:
                issues.append(f"{field}: dropped unmatched name {name!r}")
            elif matched not in kept:
                if matched != name:
                    issues.append(f"{field}: {name!r} -> {matched!r}")
                kept.append(matched)
        data[field] = kept
    if data["must_stay_at"]:
        matched = _match_name(data["must_stay_at"], enums.names[(city, "hotel")]) if city else None
        if matched is None:
            issues.append(f"must_stay_at: dropped unmatched name {data['must_stay_at']!r}")
        data["must_stay_at"] = matched
    return TripSpec.model_validate(data), issues

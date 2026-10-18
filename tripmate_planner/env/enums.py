"""Enumerations and POI name indexes built from the sandbox."""

from dataclasses import dataclass
from functools import lru_cache

from tripmate_planner.env.sandbox import CITIES, get_sandbox

KIND_FIELD = {"attraction": "type", "restaurant": "cuisine", "hotel": "featurehoteltype"}


@dataclass(frozen=True)
class Enums:
    attraction_types: dict[str, tuple[str, ...]]
    cuisines: dict[str, tuple[str, ...]]
    hotel_features: dict[str, tuple[str, ...]]
    names: dict[tuple[str, str], tuple[str, ...]]

    def union(self, field: str) -> tuple[str, ...]:
        table = getattr(self, field)
        return tuple(sorted({v for values in table.values() for v in values}))

    def for_city(self, field: str, city: str | None) -> tuple[str, ...]:
        if city is None:
            return self.union(field)
        return getattr(self, field)[city]


@lru_cache(maxsize=1)
def get_enums() -> Enums:
    sb = get_sandbox()
    values: dict[str, dict[str, tuple[str, ...]]] = {k: {} for k in KIND_FIELD}
    names: dict[tuple[str, str], tuple[str, ...]] = {}
    for city in CITIES:
        for kind, field in KIND_FIELD.items():
            df = sb.table(city, kind)
            values[kind][city] = tuple(sorted({str(v).strip() for v in df[field] if str(v).strip()}))
            names[(city, kind)] = tuple(str(n) for n in df["name"])
    return Enums(attraction_types=values["attraction"], cuisines=values["restaurant"],
                 hotel_features=values["hotel"], names=names)

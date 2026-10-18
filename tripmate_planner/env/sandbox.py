"""Thin, cached wrapper over the official ChinaTravel tool classes (single process-wide instance)."""

import os
from functools import lru_cache
from typing import Any

import pandas as pd

from tripmate_planner.paths import CT_ROOT

CITIES = ("北京", "上海", "南京", "苏州", "杭州", "深圳", "成都", "武汉", "广州", "重庆")


class Sandbox:
    def __init__(self) -> None:
        old = os.getcwd()
        os.chdir(CT_ROOT)
        try:
            from chinatravel.environment.tools import (
                Accommodations,
                Attractions,
                IntercityTransport,
                Restaurants,
                Transportation,
            )

            self.attractions = Attractions()
            self.restaurants = Restaurants()
            self.accommodations = Accommodations()
            self.intercity = IntercityTransport()
            self.transport = Transportation()
            from chinatravel.environment.tools import Poi

            self.poi = Poi()
        finally:
            os.chdir(old)

    @lru_cache(maxsize=64)  # noqa: B019 - process-wide singleton
    def table(self, city: str, kind: str) -> pd.DataFrame:
        tool = {"attraction": self.attractions, "restaurant": self.restaurants,
                "hotel": self.accommodations}[kind]
        return tool.select(city, "name", lambda x: True).reset_index(drop=True)

    @lru_cache(maxsize=200_000)  # noqa: B019 - process-wide singleton
    def goto(self, city: str, start: str, end: str, start_time: str, mode: str) -> tuple[dict[str, Any], ...] | None:
        try:
            result = self.transport.goto(city, start, end, start_time, mode)
        except Exception:
            return None
        if not isinstance(result, list) or not result:
            return None
        return tuple(dict(seg) for seg in result)

    @lru_cache(maxsize=4096)  # noqa: B019 - process-wide singleton
    def intercity_options(self, start_city: str, end_city: str, kind: str) -> tuple[dict[str, Any], ...]:
        try:
            df = self.intercity.select(start_city, end_city, kind, "00:00")
        except Exception:
            return ()
        if df is None or len(df) == 0:
            return ()
        return tuple(df.to_dict("records"))


@lru_cache(maxsize=1)
def get_sandbox() -> Sandbox:
    return Sandbox()


def coords(city: str, name: str) -> tuple[float, float] | None:
    return _coords(city, name)


@lru_cache(maxsize=100_000)
def _coords(city: str, name: str) -> tuple[float, float] | None:
    try:
        lat, lon = get_sandbox().poi.search(city, name)
        return float(lat), float(lon)
    except Exception:
        return None

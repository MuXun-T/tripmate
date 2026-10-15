import math
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.context import RequestContext

CITY_COORDINATES: dict[str, tuple[float, float]] = {
    "hangzhou": (30.2741, 120.1551),
    "杭州": (30.2741, 120.1551),
    "shanghai": (31.2304, 121.4737),
    "上海": (31.2304, 121.4737),
    "suzhou": (31.2989, 120.5853),
    "苏州": (31.2989, 120.5853),
}


class RealtimeAdapters:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.http = http or httpx.AsyncClient(
            timeout=settings.external_http_timeout_seconds, follow_redirects=False
        )

    async def search_attractions(
        self, ctx: RequestContext, query: str, city: str | None = None
    ) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            result = await session.execute(
                text("""SELECT attraction_id,city,name,category,description,latitude,longitude,source_version
                    FROM tripmate.attraction WHERE tenant_id=:tenant AND status='ACTIVE'
                    AND (:city IS NULL OR lower(city)=lower(:city))
                    AND (name ILIKE :pattern OR category ILIKE :pattern OR COALESCE(description,'') ILIKE :pattern)
                    ORDER BY name LIMIT 20"""),
                {
                    "tenant": ctx.tenant_id,
                    "city": city,
                    "pattern": f"%{' '.join(query.split())[:150]}%",
                },
            )
            return [dict(row) for row in result.mappings()]

    async def get_weather(self, city: str, visit_date: date) -> dict[str, Any]:
        coordinates = CITY_COORDINATES.get(city.strip().casefold()) or CITY_COORDINATES.get(
            city.strip()
        )
        queried_at = datetime.now(UTC)
        if coordinates is None:
            return {
                "status": "UNKNOWN",
                "reason": "CITY_NOT_CONFIGURED",
                "queried_at": queried_at,
                "source": "open-meteo",
            }
        try:
            response = await self.http.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": coordinates[0],
                    "longitude": coordinates[1],
                    "daily": "weather_code,temperature_2m_max,precipitation_probability_max",
                    "timezone": "Asia/Shanghai",
                    "start_date": visit_date.isoformat(),
                    "end_date": visit_date.isoformat(),
                },
            )
            response.raise_for_status()
            data = response.json()
            return {
                "status": "OK",
                "date": visit_date.isoformat(),
                "daily": data.get("daily", {}),
                "queried_at": queried_at,
                "source": "Open-Meteo",
                "source_url": "https://open-meteo.com/",
            }
        except (httpx.HTTPError, ValueError):
            return {
                "status": "UNKNOWN",
                "reason": "PROVIDER_UNAVAILABLE",
                "queried_at": queried_at,
                "source": "Open-Meteo",
            }

    async def get_opening_hours(
        self, ctx: RequestContext, attraction_id: UUID, visit_date: date
    ) -> dict[str, Any]:
        async with self.sessions() as session:
            rows = await session.execute(
                text("""SELECT slot_id,start_at,end_at,status,activity_version FROM tripmate.activity_slot
                    WHERE tenant_id=:tenant AND attraction_id=:attraction AND start_at::date=:visit_date
                      AND status='ACTIVE' ORDER BY start_at"""),
                {"tenant": ctx.tenant_id, "attraction": attraction_id, "visit_date": visit_date},
            )
            slots = [dict(row) for row in rows.mappings()]
            return {
                "status": "OK" if slots else "UNKNOWN",
                "attraction_id": str(attraction_id),
                "date": visit_date.isoformat(),
                "available_windows": slots,
                "queried_at": datetime.now(UTC),
                "source": "PostgreSQL activity schedule",
            }

    async def estimate_route_time(
        self, ctx: RequestContext, origin_id: UUID, destination_id: UUID
    ) -> dict[str, Any]:
        async with self.sessions() as session:
            result = await session.execute(
                text("""SELECT a.attraction_id,a.latitude,a.longitude FROM tripmate.attraction a
                    WHERE a.tenant_id=:tenant AND a.attraction_id IN (:origin,:destination)
                      AND a.status='ACTIVE'"""),
                {"tenant": ctx.tenant_id, "origin": origin_id, "destination": destination_id},
            )
            points = {row["attraction_id"]: row for row in result.mappings()}
        origin, destination = points.get(origin_id), points.get(destination_id)
        if (
            origin is None
            or destination is None
            or None
            in (
                origin["latitude"],
                origin["longitude"],
                destination["latitude"],
                destination["longitude"],
            )
        ):
            return {
                "status": "UNKNOWN",
                "reason": "COORDINATES_UNAVAILABLE",
                "queried_at": datetime.now(UTC),
                "source": "local haversine estimate",
            }
        distance_km = self._haversine_km(
            float(origin["latitude"]),
            float(origin["longitude"]),
            float(destination["latitude"]),
            float(destination["longitude"]),
        )
        return {
            "status": "OK",
            "distance_km": round(distance_km, 2),
            "estimated_minutes": max(1, round(distance_km / 4 * 60)),
            "queried_at": datetime.now(UTC),
            "source": "local straight-line walking estimate",
        }

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        earth_radius_km = 6371.0088
        dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
        )
        return 2 * earth_radius_km * math.asin(math.sqrt(a))

    async def close(self) -> None:
        await self.http.aclose()

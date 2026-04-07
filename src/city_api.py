"""Async client for the RedAlert Cities Catalog API."""

import logging
from typing import Optional

import aiohttp

from src.config import REDALERT_CITIES_URL, api_headers

logger = logging.getLogger(__name__)


class CityAPI:
    """Queries the RedAlert /api/data/cities endpoint."""

    def __init__(self):
        self._base_url = REDALERT_CITIES_URL

    async def search(self, query: str, limit: int = 8) -> list[dict]:
        """Search cities by partial Hebrew name."""
        return await self._fetch({"search": query, "limit": limit})

    async def get_by_zone(self, zone: str, limit: int = 500) -> list[dict]:
        """Get all cities in a specific zone/region."""
        return await self._fetch({"zone": zone, "limit": limit})

    async def get_all_zones(self) -> list[str]:
        """Fetch all unique zone names, paginating as needed."""
        zones: set[str] = set()
        offset = 0
        while True:
            batch = await self._fetch({"limit": 500, "offset": offset})
            for c in batch:
                z = c.get("zone")
                if z:
                    zones.add(z)
            if len(batch) < 500:
                break
            offset += 500
        return sorted(zones)

    async def get_all_coords(self) -> dict[str, dict]:
        """Fetch all cities with coordinates. Returns {name: {lat, lng, zone}}."""
        result: dict[str, dict] = {}
        offset = 0
        while True:
            batch = await self._fetch({"limit": 500, "offset": offset, "include": "coords"})
            for c in batch:
                name = c.get("name", "")
                lat, lng = c.get("lat"), c.get("lng")
                if name and lat is not None and lng is not None:
                    result[name] = {"lat": lat, "lng": lng, "zone": c.get("zone", "")}
            if len(batch) < 500:
                break
            offset += 500
        logger.info(f"Loaded coordinates for {len(result)} cities from API.")
        return result

    async def _fetch(self, params: dict) -> list[dict]:
        try:
            async with aiohttp.ClientSession(headers=api_headers()) as session:
                async with session.get(
                    self._base_url, params=params, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        body = await resp.json()
                        return body.get("data", []) if isinstance(body, dict) else body
                    logger.error(f"Cities API returned {resp.status}")
                    return []
        except Exception as e:
            logger.error(f"Cities API error: {e}")
            return []
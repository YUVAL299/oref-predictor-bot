"""
Async wrapper around the RedAlert Cities Catalog API.

GET https://redalert.orielhaim.com/api/data/cities
    ?search=<partial name>  – fuzzy Hebrew search
    &zone=<exact zone>      – filter by region
    &limit=N                – max results (1-500)
    &include=translations,coords,countdown

Returns objects like:
    { "id": 123, "name": "רמת גן - מערב", "zone": "דן" }
"""
import logging
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://redalert.orielhaim.com/api/data/cities"


async def search_cities(query: str, limit: int = 8) -> list[dict]:
    """Search cities by partial Hebrew name."""
    params = {"search": query, "limit": limit}
    return await _fetch(params)


async def get_cities_by_zone(zone: str, limit: int = 500) -> list[dict]:
    """Get all cities in a specific zone/region."""
    params = {"zone": zone, "limit": limit}
    return await _fetch(params)


async def get_all_zones() -> list[str]:
    """Fetch all cities and extract unique zone names."""
    params = {"limit": 500}
    cities = await _fetch(params)
    zones: set[str] = set()
    for c in cities:
        z = c.get("zone")
        if z:
            zones.add(z)

    # The API only returns 500 at a time; paginate to get all zones
    if len(cities) == 500:
        offset = 500
        while True:
            params = {"limit": 500, "offset": offset}
            batch = await _fetch(params)
            if not batch:
                break
            for c in batch:
                z = c.get("zone")
                if z:
                    zones.add(z)
            if len(batch) < 500:
                break
            offset += 500

    return sorted(zones)


async def _fetch(params: dict) -> list[dict]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(BASE_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    return body.get("data", []) if isinstance(body, dict) else body
                else:
                    logger.error(f"Cities API returned {resp.status}")
                    return []
    except Exception as e:
        logger.error(f"Cities API error: {e}")
        return []
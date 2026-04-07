"""Check alert counts for a city from the API vs predictor data.

Usage:
    python -m tools.check_city "רמת גן - מערב"
    python -m tools.check_city "קריית שמונה" missiles
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import aiohttp

from src.config import REDALERT_HISTORY_URL, DATA_START_DATE, CITY_BASE_RATES_FILE, api_headers


async def check(city: str, category: str | None = None):
    headers = api_headers()
    params = {"cityName": city, "startDate": f"{DATA_START_DATE}T00:00:00Z", "limit": 1}
    if category:
        params["category"] = category

    async with aiohttp.ClientSession(headers=headers) as s:
        # Get total from API
        async with s.get(REDALERT_HISTORY_URL, params=params) as r:
            if r.status != 200:
                print(f"API error: {r.status}")
                return
            data = await r.json()
            total = data["pagination"]["total"]

        # Breakdown by type
        if not category:
            for cat in ["missiles", "hostileAircraftIntrusion", "newsFlash"]:
                params["category"] = cat
                async with s.get(REDALERT_HISTORY_URL, params=params) as r:
                    if r.status == 200:
                        d = await r.json()
                        count = d["pagination"]["total"]
                        if count > 0:
                            print(f"  {cat}: {count}")
            del params["category"]

    print(f"\n📊 API totals for: {city}")
    print(f"  {'Total ' + category if category else 'All types'}: {total}")

    # Compare with predictor data
    if os.path.exists(CITY_BASE_RATES_FILE):
        with open(CITY_BASE_RATES_FILE) as f:
            rates = json.load(f)
        rg = rates.get(city, {})
        if rg:
            print(f"\n📈 Predictor data:")
            print(f"  Early warnings: {rg['warned']}")
            print(f"  Led to alarm:   {rg['hit']} ({rg['hit']/rg['warned']*100:.1f}%)")
            print(f"  No alarm:       {rg['warned'] - rg['hit']}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python -m tools.check_city "רמת גן - מערב" [category]')
        sys.exit(1)
    asyncio.run(check(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
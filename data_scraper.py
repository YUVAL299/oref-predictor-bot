"""
Fetches ALL historical alerts from the RedAlert History API (from 28/2/2026
to now) and rebuilds the predictor's data files.

API:  GET /api/stats/history
      ?startDate=2026-02-28T00:00:00Z
      &limit=100  &offset=N
      &sort=timestamp  &order=asc

Each record:
    { "id": 10523, "timestamp": "...", "type": "missiles",
      "origin": "gaza", "cities": [{"id": 45, "name": "תל אביב - יפו"}] }

Usage:
    python data_scraper.py          # fetch everything from API
"""
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta

import aiohttp
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
logger = logging.getLogger(__name__)

API_BASE = "https://redalert.orielhaim.com/api/stats/history"
PAGE_SIZE = 100
START_DATE = "2026-02-28"
ISRAEL_TZ = timezone(timedelta(hours=3))
API_KEY = os.environ.get("REDALERT_API_KEY", "")


# ═══════════════════════════════════════════════════════════════════
#  1.  FETCH — paginate through the entire history API
# ═══════════════════════════════════════════════════════════════════
async def fetch_all_alerts() -> list[dict]:
    """Pulls every alert from START_DATE until now."""
    params = {
        "startDate": f"{START_DATE}T00:00:00Z",
        "limit": PAGE_SIZE,
        "offset": 0,
        "sort": "timestamp",
        "order": "asc",
    }

    all_alerts: list[dict] = []

    headers = {}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
        params["apiKey"] = API_KEY

    async with aiohttp.ClientSession(headers=headers) as session:
        while True:
            logger.info(f"  Fetching offset {params['offset']} ...")
            try:
                async with session.get(
                    API_BASE, params=params, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 401:
                        logger.error("  ❌ HTTP 401 — API key is missing or invalid.")
                        logger.error("  Make sure REDALERT_API_KEY is set in your .env file.")
                        return all_alerts
                    if resp.status != 200:
                        logger.error(f"  API returned HTTP {resp.status}")
                        break
                    body = await resp.json()
            except Exception as e:
                logger.error(f"  Request failed: {e}")
                break

            data = body.get("data", [])
            pagination = body.get("pagination", {})
            all_alerts.extend(data)

            total = pagination.get("total", "?")
            logger.info(f"  Got {len(data)} records  (total so far: {len(all_alerts)}/{total})")

            if not pagination.get("hasMore", False) or len(data) == 0:
                break
            params["offset"] += PAGE_SIZE

    logger.info(f"✅ Fetched {len(all_alerts)} total alerts from API.")
    if all_alerts:
        logger.info(f"  Sample record: {json.dumps(all_alerts[0], ensure_ascii=False, default=str)[:500]}")
    return all_alerts


# ═══════════════════════════════════════════════════════════════════
#  2.  CLASSIFY — map API type strings to our categories
# ═══════════════════════════════════════════════════════════════════
def classify_type(api_type: str) -> str:
    t = (api_type or "").lower().strip()
    if t in ("missiles", "rockets"):
        return "missiles"
    if t in ("hostileaircraftintrusion", "drones", "hostile_aircraft"):
        return "hostile_aircraft"
    if t in ("newsflash", "early_warning", "system"):
        return "early_warning"
    if t in ("endalert", "all_clear"):
        return "all_clear"
    if t in ("terroristinfiltration",):
        return "terrorist_infiltration"
    if t in ("earthquakes",):
        return "earthquakes"
    return t or "general_alert"


# ═══════════════════════════════════════════════════════════════════
#  3.  BUILD — group early warnings into incidents, compute outcomes
# ═══════════════════════════════════════════════════════════════════
def build_history(alerts: list[dict]) -> tuple[list, dict, dict]:
    # Debug: show all unique type values from the API
    raw_types = {}
    for a in alerts:
        t = a.get("type", "")
        raw_types[t] = raw_types.get(t, 0) + 1
    logger.info(f"  Raw API type values: {raw_types}")

    rows = []

    for a in alerts:
        atype = classify_type(a.get("type", ""))
        raw_cities = a.get("cities", [])

        # For newsFlash: filter out nationwide broadcasts.
        # The API attaches {"name": "ברחבי הארץ", "zone": null} to general advisories.
        # Real targeted early warnings have cities with actual zone values.
        if atype == "early_warning":
            real_cities = [
                c["name"] for c in raw_cities
                if c.get("name")
                and c.get("zone") is not None         # has a real zone
                and c["name"] != "ברחבי הארץ"
                and c["name"] != "כל הארץ"
            ]
            if not real_cities:
                continue  # purely a nationwide "stay near shelter" broadcast
            city_names = real_cities
        else:
            city_names = [c["name"] for c in raw_cities if c.get("name")]

        rows.append({
            "timestamp": a.get("timestamp", ""),
            "alert_type": atype,
            "cities": city_names,
        })

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)

    ew_df = df[df["alert_type"] == "early_warning"]
    miss_df = df[df["alert_type"] == "missiles"]
    end_df = df[df["alert_type"] == "all_clear"]
    logger.info(f"  Classified: {len(ew_df)} early_warning, {len(miss_df)} missiles, {len(end_df)} endAlert, {len(df)} total")

    # ── Per-city event tracking ──────────────────────────────────────
    #
    # Each city has its OWN event lifecycle:
    #   newsFlash [city]  → city's event OPENS
    #   missiles  [city]  → city records a HIT (only if event is open)
    #   endAlert  [city]  → city's event CLOSES → save result
    #
    # Example:
    #   newsFlash [רמת גן, ת"א]   → both open
    #   missiles  [רמת גן]        → רמת גן hit
    #   endAlert  [ת"א]           → ת"א closes (warned, not hit)
    #   newsFlash [רמת גן]        → רמת גן still open, polygon grows
    #   missiles  [רמת גן]        → רמת גן hit again
    #   endAlert  [רמת גן]        → רמת גן closes (warned + hit)
    #
    # Safety: force-close if open >90 min without endAlert.

    MAX_OPEN_SECONDS = 5400  # 90 min

    city_stats: dict[str, dict] = {}  # city → {warned, hit}

    # Per-city open state: {city: {"start": ts, "hit": bool, "polygon": set, "ev_key": str}}
    open_events: dict[str, dict] = {}

    # Event polygons for the Jaccard predictor (grouped by time)
    event_polygons: dict[str, dict] = {}  # ev_key → {start, warned_cities, hit_cities}

    relevant = df[df["alert_type"].isin(["early_warning", "missiles", "all_clear"])].copy()
    relevant = relevant.sort_values("timestamp").reset_index(drop=True)

    def _close_city(city: str, was_hit: bool):
        city_stats.setdefault(city, {"warned": 0, "hit": 0})
        city_stats[city]["warned"] += 1
        if was_hit:
            city_stats[city]["hit"] += 1

    for _, row in relevant.iterrows():
        atype = row["alert_type"]
        cities = [c for c in row["cities"] if len(c) < 50]
        t = row["timestamp"]

        # Safety: force-close cities open too long
        expired = [c for c, ev in open_events.items()
                   if (t - ev["start"]).total_seconds() > MAX_OPEN_SECONDS]
        for c in expired:
            ev = open_events.pop(c)
            _close_city(c, ev["hit"])

        if atype == "early_warning":
            # Group simultaneous newsFlashes into one polygon (5-min bucket)
            ev_key = str(t.floor("5min"))
            if ev_key not in event_polygons:
                event_polygons[ev_key] = {"start": t, "warned_cities": set(), "hit_cities": set()}
            event_polygons[ev_key]["warned_cities"].update(cities)

            for city in cities:
                if city not in open_events:
                    open_events[city] = {"start": t, "hit": False, "ev_key": ev_key}

        elif atype == "missiles":
            for city in cities:
                if city in open_events:
                    open_events[city]["hit"] = True
                    ev_key = open_events[city].get("ev_key", "")
                    if ev_key in event_polygons:
                        event_polygons[ev_key]["hit_cities"].add(city)

        elif atype == "all_clear":
            for city in cities:
                if city in open_events:
                    ev = open_events.pop(city)
                    _close_city(city, ev["hit"])

    # Close any remaining open events
    for city, ev in open_events.items():
        _close_city(city, ev["hit"])
    open_events.clear()

    # Build events_out from event_polygons (for Jaccard predictor)
    events_out: list[dict] = []
    for ev_key, ev in sorted(event_polygons.items()):
        if not ev["warned_cities"]:
            continue
        events_out.append({
            "timestamp": ev["start"].isoformat(),
            "hour": ev["start"].hour,
            "polygon_size": len(ev["warned_cities"]),
            "warned_cities": sorted(ev["warned_cities"]),
            "hit_cities": sorted(ev["hit_cities"]),
            "total_hit": len(ev["hit_cities"]),
        })

    logger.info(f"  Built {len(events_out)} event polygons, tracking {len(city_stats)} cities")

    base_rates = {}
    for city, s in sorted(city_stats.items()):
        if s["warned"] > 0:
            base_rates[city] = {
                "warned": s["warned"],
                "hit": s["hit"],
                "rate": round(s["hit"] / s["warned"], 4),
            }

    # Metadata
    timestamps = df["timestamp"].dropna()
    meta = {
        "start_date": START_DATE,
        "end_date": str(timestamps.max().date()) if len(timestamps) else "",
        "last_updated": datetime.now(ISRAEL_TZ).isoformat(),
        "total_alerts": len(df),
        "total_events": len(events_out),
        "total_cities": len(base_rates),
    }

    return events_out, base_rates, meta


# ═══════════════════════════════════════════════════════════════════
#  4.  SAVE — write the three JSON files used by the predictor
# ═══════════════════════════════════════════════════════════════════
def save(events: list, base_rates: dict, meta: dict):
    with open("event_history.json", "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False)
    logger.info(f"  Saved {len(events)} events → event_history.json")

    with open("city_base_rates.json", "w", encoding="utf-8") as f:
        json.dump(base_rates, f, ensure_ascii=False)
    logger.info(f"  Saved {len(base_rates)} city base rates → city_base_rates.json")

    with open("data_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    logger.info(f"  Saved metadata → data_meta.json")

    logger.info(
        f"\n✅ Done!  {meta['total_events']} events, {meta['total_cities']} cities, "
        f"data from {meta['start_date']} to {meta['end_date']}"
    )


# ═══════════════════════════════════════════════════════════════════
#  5.  MAIN
# ═══════════════════════════════════════════════════════════════════
RAW_CACHE_FILE = "raw_alerts_cache.json"


async def main():
    use_cache = "--local" in sys.argv

    if use_cache and os.path.exists(RAW_CACHE_FILE):
        logger.info(f"Loading cached alerts from {RAW_CACHE_FILE} ...")
        with open(RAW_CACHE_FILE, "r", encoding="utf-8") as f:
            alerts = json.load(f)
        logger.info(f"  Loaded {len(alerts)} cached alerts.")
    else:
        logger.info(f"Pulling all alerts from {START_DATE} to now ...")
        alerts = await fetch_all_alerts()

        if not alerts:
            logger.error("No data received from API. Check your internet connection.")
            sys.exit(1)

        # Always cache the raw response
        with open(RAW_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(alerts, f, ensure_ascii=False)
        logger.info(f"  Cached raw alerts → {RAW_CACHE_FILE}")

    events, base_rates, meta = build_history(alerts)
    save(events, base_rates, meta)


if __name__ == "__main__":
    asyncio.run(main())
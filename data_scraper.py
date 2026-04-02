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
    # Names that indicate a nationwide broadcast, not a targeted early warning
    NATIONWIDE_NAMES = {"ברחבי הארץ", "כל הארץ"}

    for a in alerts:
        city_names = [c["name"] for c in a.get("cities", []) if c.get("name")]

        # Filter: if a newsFlash only has nationwide placeholder cities, skip it
        atype = classify_type(a.get("type", ""))
        if atype == "early_warning":
            real_cities = [c for c in city_names if c not in NATIONWIDE_NAMES]
            if not real_cities:
                continue  # skip "שהו בקרבת מרחב מוגן" type broadcasts
            city_names = real_cities

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
    logger.info(f"  Classified: {len(ew_df)} early_warning, {len(miss_df)} missiles, {len(df)} total")

    # Group early warnings into incidents (>30 min gap = new incident)
    events_raw: list[dict] = []
    cur_start = None
    cur_warns: list = []
    for _, row in ew_df.iterrows():
        t = row["timestamp"]
        if cur_start is None or (t - cur_start).total_seconds() > 1800:
            if cur_warns:
                events_raw.append({"start": cur_start, "warnings": cur_warns})
            cur_start = t
            cur_warns = [row]
        else:
            cur_warns.append(row)
    if cur_warns:
        events_raw.append({"start": cur_start, "warnings": cur_warns})

    logger.info(f"  Identified {len(events_raw)} early-warning incidents")

    # For each incident — who was warned, who was hit?
    events_out: list[dict] = []
    city_stats: dict[str, dict] = {}
    skipped_nationwide = 0

    for ev in events_raw:
        warned: set[str] = set()
        for w in ev["warnings"]:
            warned.update(w["cities"])
        warned = {c for c in warned if len(c) < 50}
        if not warned:
            continue

        # Skip nationwide broadcasts (>1400 cities = entire country)
        if len(warned) > 1400:
            skipped_nationwide += 1
            continue

        w_start = ev["start"] + pd.Timedelta(minutes=1)
        w_end = ev["start"] + pd.Timedelta(minutes=20)
        window = miss_df[(miss_df["timestamp"] >= w_start) & (miss_df["timestamp"] <= w_end)]

        hit: set[str] = set()
        for _, m in window.iterrows():
            hit.update(m["cities"])
        hit_in_polygon = hit & warned

        events_out.append({
            "timestamp": ev["start"].isoformat(),
            "hour": ev["start"].hour,
            "polygon_size": len(warned),
            "warned_cities": sorted(warned),
            "hit_cities": sorted(hit_in_polygon),
            "total_hit": len(hit_in_polygon),
        })

        for city in warned:
            city_stats.setdefault(city, {"warned": 0, "hit": 0})
            city_stats[city]["warned"] += 1
            if city in hit_in_polygon:
                city_stats[city]["hit"] += 1

    # Base rates
    if skipped_nationwide:
        logger.info(f"  Skipped {skipped_nationwide} nationwide broadcasts (>1400 cities)")
    logger.info(f"  Kept {len(events_out)} targeted early-warning events")

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
async def main():
    logger.info(f"Pulling all alerts from {START_DATE} to now ...")
    alerts = await fetch_all_alerts()

    if not alerts:
        logger.error("No data received from API. Check your internet connection.")
        sys.exit(1)

    events, base_rates, meta = build_history(alerts)
    save(events, base_rates, meta)


if __name__ == "__main__":
    asyncio.run(main())
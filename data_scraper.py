"""
Fetches historical alert data from the RedAlert History API and builds
the event_history.json + city_base_rates.json used by the predictor.

API:  GET /api/stats/history
      ?startDate=ISO8601  &endDate=ISO8601
      &limit=100          &offset=N
      &category=...       &sort=timestamp &order=asc

Each record:  { id, timestamp, type, origin, cities: [{id, name}] }

Usage:
    python data_scraper.py                   # default: from 2026-02-28
    python data_scraper.py 2026-03-01        # custom start date
"""
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
logger = logging.getLogger(__name__)

API_BASE = "https://redalert.orielhaim.com/api/stats/history"
PAGE_SIZE = 100                    # max allowed by the API
DEFAULT_START = "2026-02-28"       # project start date


# ── Fetch all pages ────────────────────────────────────────────────
async def fetch_all_alerts(
    start_date: str = DEFAULT_START,
    end_date: Optional[str] = None,
) -> list[dict]:
    """Paginates through the history API and returns every alert record."""
    params = {
        "startDate": f"{start_date}T00:00:00Z",
        "limit": PAGE_SIZE,
        "offset": 0,
        "sort": "timestamp",
        "order": "asc",
    }
    if end_date:
        params["endDate"] = f"{end_date}T23:59:59Z"

    all_alerts: list[dict] = []

    async with aiohttp.ClientSession() as session:
        while True:
            logger.info(f"Fetching offset {params['offset']} ...")
            async with session.get(API_BASE, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    logger.error(f"API returned {resp.status}")
                    break
                body = await resp.json()

            data = body.get("data", [])
            pagination = body.get("pagination", {})
            all_alerts.extend(data)

            if not pagination.get("hasMore", False) or len(data) < PAGE_SIZE:
                break
            params["offset"] += PAGE_SIZE

    logger.info(f"Fetched {len(all_alerts)} total alert records.")
    return all_alerts


# ── Classify alert type ───────────────────────────────────────────
def classify_type(record: dict) -> str:
    """Map API type field to our internal categories."""
    api_type = (record.get("type") or "").lower()
    if api_type in ("missiles", "רקטות"):
        return "missiles"
    if api_type in ("drones", "hostile_aircraft", "כלי טיס עוין"):
        return "hostile_aircraft"
    # The API might use different names for early warnings
    if api_type in ("early_warning", "system", "מבזק"):
        return "early_warning"
    if api_type in ("all_clear",):
        return "all_clear"
    # Fallback: check origin hints
    return api_type or "general_alert"


# ── Build events & base rates ─────────────────────────────────────
def build_history(alerts: list[dict]):
    """
    Groups early_warning alerts into incidents (>30 min gap),
    then checks which cities were actually hit by missiles within 1-20 min.

    Returns (events_list, city_base_rates, metadata).
    """
    import pandas as pd

    # Normalize into flat rows
    rows = []
    for a in alerts:
        ts = a.get("timestamp", "")
        atype = classify_type(a)
        city_names = [c.get("name", "") for c in a.get("cities", []) if c.get("name")]
        rows.append({
            "timestamp": ts,
            "alert_type": atype,
            "cities": city_names,
        })

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)

    ew_df = df[df["alert_type"] == "early_warning"]
    miss_df = df[df["alert_type"] == "missiles"]

    logger.info(f"Classified: {len(ew_df)} early_warning, {len(miss_df)} missiles, {len(df)} total")

    # Group early warnings into incidents
    events_raw = []
    cur_start = None
    cur_warns = []
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

    logger.info(f"Identified {len(events_raw)} early-warning incidents")

    # For each incident, compute polygon and outcomes
    events_out = []
    city_stats: dict[str, dict] = {}

    for ev in events_raw:
        warned_cities: set[str] = set()
        for w in ev["warnings"]:
            warned_cities.update(w["cities"])
        warned_cities = {c for c in warned_cities if len(c) < 50}
        if not warned_cities:
            continue

        w_start = ev["start"] + pd.Timedelta(minutes=1)
        w_end = ev["start"] + pd.Timedelta(minutes=20)
        window = miss_df[(miss_df["timestamp"] >= w_start) & (miss_df["timestamp"] <= w_end)]

        hit_cities: set[str] = set()
        for _, m in window.iterrows():
            hit_cities.update(m["cities"])

        hit_in_polygon = hit_cities & warned_cities

        events_out.append({
            "timestamp": ev["start"].isoformat(),
            "hour": ev["start"].hour,
            "polygon_size": len(warned_cities),
            "warned_cities": sorted(warned_cities),
            "hit_cities": sorted(hit_in_polygon),
            "total_hit": len(hit_in_polygon),
        })

        for city in warned_cities:
            city_stats.setdefault(city, {"warned": 0, "hit": 0})
            city_stats[city]["warned"] += 1
            if city in hit_in_polygon:
                city_stats[city]["hit"] += 1

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
        "start_date": str(timestamps.min().date()) if len(timestamps) else "",
        "end_date": str(timestamps.max().date()) if len(timestamps) else "",
        "last_updated": datetime.now(timezone(timedelta(hours=3))).isoformat(),
        "total_alerts": len(df),
        "total_events": len(events_out),
        "total_cities": len(base_rates),
    }

    return events_out, base_rates, meta


# ── Save to disk ──────────────────────────────────────────────────
def save(events, base_rates, meta):
    with open("event_history.json", "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False)
    logger.info(f"Saved {len(events)} events → event_history.json")

    with open("city_base_rates.json", "w", encoding="utf-8") as f:
        json.dump(base_rates, f, ensure_ascii=False)
    logger.info(f"Saved {len(base_rates)} city base rates → city_base_rates.json")

    with open("data_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved metadata → data_meta.json")


# ── Fallback: build from local CSV ────────────────────────────────
def build_from_csv(csv_path: str = "alerts_raw_data.csv"):
    """Fallback if the API is unavailable — uses the locally scraped CSV."""
    import pandas as pd

    logger.info(f"Building from local CSV: {csv_path}")
    df = pd.read_csv(csv_path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    # Convert CSV rows to API-like records
    alerts = []
    for _, row in df.iterrows():
        cities = []
        if pd.notna(row.get("cities", "")):
            cities = [{"name": c.strip()} for c in str(row["cities"]).split(",") if c.strip() and len(c.strip()) < 50]
        alerts.append({
            "timestamp": str(row["timestamp"]),
            "type": row.get("alert_type", ""),
            "cities": cities,
        })

    events, base_rates, meta = build_history(alerts)
    save(events, base_rates, meta)


# ── Main ──────────────────────────────────────────────────────────
async def main():
    start = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_START
    end = sys.argv[2] if len(sys.argv) > 2 else None

    logger.info(f"Scraping alerts from {start} ...")

    try:
        alerts = await fetch_all_alerts(start_date=start, end_date=end)
        if not alerts:
            raise RuntimeError("API returned no data")
        events, base_rates, meta = build_history(alerts)
        save(events, base_rates, meta)
    except Exception as e:
        logger.warning(f"API fetch failed ({e}), falling back to local CSV...")
        build_from_csv()


if __name__ == "__main__":
    asyncio.run(main())
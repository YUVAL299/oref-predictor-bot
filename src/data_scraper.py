"""
Fetches historical alerts from the RedAlert API and builds
the predictor's data files (event_history, city_base_rates, metadata).

Usage:
    python -m src.data_scraper           # fetch from API
    python -m src.data_scraper --local   # rebuild from cached data
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from collections import defaultdict

import aiohttp
import pandas as pd

from src.config import (
    REDALERT_HISTORY_URL, DATA_START_DATE, HISTORY_PAGE_SIZE,
    EW_MERGE_WINDOW_SEC, MAX_OPEN_EVENT_SEC, ISRAEL_TZ,
    EVENT_HISTORY_FILE, CITY_BASE_RATES_FILE, DATA_META_FILE,
    RAW_CACHE_FILE, api_headers,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
logger = logging.getLogger(__name__)


class AlertClassifier:
    """Maps API alert type strings to internal categories."""

    TYPE_MAP = {
        "missiles": "missiles", "rockets": "missiles",
        "hostileaircraftintrusion": "hostile_aircraft", "drones": "hostile_aircraft",
        "newsflash": "early_warning", "early_warning": "early_warning", "system": "early_warning",
        "endalert": "all_clear",
        "terroristinfiltration": "terrorist_infiltration",
        "earthquakes": "earthquakes",
    }

    @classmethod
    def classify(cls, api_type: str) -> str:
        return cls.TYPE_MAP.get((api_type or "").lower().strip(), api_type or "general_alert")


class AlertFetcher:
    """Paginates through the RedAlert History API."""

    async def fetch_all(self) -> list[dict]:
        params = {
            "startDate": f"{DATA_START_DATE}T00:00:00Z",
            "limit": HISTORY_PAGE_SIZE,
            "offset": 0,
            "sort": "timestamp",
            "order": "asc",
        }
        all_alerts: list[dict] = []
        headers = api_headers()

        async with aiohttp.ClientSession(headers=headers) as session:
            while True:
                logger.info(f"  Fetching offset {params['offset']} ...")
                try:
                    async with session.get(
                        REDALERT_HISTORY_URL, params=params,
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        if resp.status == 401:
                            logger.error("  ❌ HTTP 401 — check REDALERT_API_KEY in .env")
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
                logger.info(f"  Got {len(data)} records (total: {len(all_alerts)}/{total})")

                if not pagination.get("hasMore", False) or len(data) == 0:
                    break
                params["offset"] += HISTORY_PAGE_SIZE

        logger.info(f"✅ Fetched {len(all_alerts)} alerts from API.")
        return all_alerts


class EventBuilder:
    """Processes raw alerts into per-city events with outcomes."""

    def build(self, alerts: list[dict]) -> tuple[list[dict], dict, dict]:
        """Returns (events_list, city_base_rates, metadata)."""
        # Parse and classify
        id_to_name = self._build_id_map(alerts)
        rows = self._parse_rows(alerts, id_to_name)

        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").reset_index(drop=True)

        # Merge early warnings within EW_MERGE_WINDOW_SEC
        df = self._merge_early_warnings(df)

        # Per-city event tracking
        city_stats, event_polygons = self._track_events(df)

        # Build outputs
        events_out = self._build_events_output(event_polygons, id_to_name)
        base_rates = self._build_base_rates(city_stats, id_to_name)
        meta = self._build_metadata(df, events_out, base_rates)

        return events_out, base_rates, meta

    def _build_id_map(self, alerts: list[dict]) -> dict[int, str]:
        id_to_name: dict[int, str] = {}
        for a in alerts:
            for c in a.get("cities", []):
                cid, cname = c.get("id"), c.get("name", "")
                if cid is not None and cname:
                    id_to_name[cid] = cname
        logger.info(f"  ID→name mapping: {len(id_to_name)} cities")
        return id_to_name

    def _parse_rows(self, alerts: list[dict], id_to_name: dict) -> list[dict]:
        rows = []
        for a in alerts:
            atype = AlertClassifier.classify(a.get("type", ""))
            raw_cities = a.get("cities", [])

            if atype == "early_warning":
                city_ids = [c["id"] for c in raw_cities
                            if c.get("id") is not None and c.get("zone") is not None
                            and c.get("name", "") not in ("ברחבי הארץ", "כל הארץ")]
                if not city_ids:
                    continue
            else:
                city_ids = [c["id"] for c in raw_cities if c.get("id") is not None]

            rows.append({"timestamp": a.get("timestamp", ""), "alert_type": atype, "city_ids": city_ids})

        # Log type distribution
        from collections import Counter
        type_counts = Counter(r["alert_type"] for r in rows)
        logger.info(f"  Types: {dict(type_counts)}")
        return rows

    def _merge_early_warnings(self, df: pd.DataFrame) -> pd.DataFrame:
        ew_mask = df["alert_type"] == "early_warning"
        ew_rows = df[ew_mask].copy()

        if len(ew_rows) <= 1:
            return df

        merged = []
        current_ts = ew_rows.iloc[0]["timestamp"]
        current_ids = list(ew_rows.iloc[0]["city_ids"])

        for i in range(1, len(ew_rows)):
            row = ew_rows.iloc[i]
            gap = (row["timestamp"] - current_ts).total_seconds()
            if gap <= EW_MERGE_WINDOW_SEC:
                current_ids.extend(row["city_ids"])
            else:
                merged.append({"timestamp": current_ts, "alert_type": "early_warning", "city_ids": list(set(current_ids))})
                current_ts = row["timestamp"]
                current_ids = list(row["city_ids"])
        merged.append({"timestamp": current_ts, "alert_type": "early_warning", "city_ids": list(set(current_ids))})

        non_ew = df[~ew_mask]
        merged_df = pd.DataFrame(merged)
        merged_df["timestamp"] = pd.to_datetime(merged_df["timestamp"], utc=True)
        df = pd.concat([non_ew, merged_df], ignore_index=True).sort_values("timestamp").reset_index(drop=True)
        logger.info(f"  Merged {len(ew_rows)} EW records → {len(merged)} waves")
        return df

    def _track_events(self, df: pd.DataFrame) -> tuple[dict, dict]:
        city_stats: dict[int, dict] = {}
        open_events: dict[int, dict] = {}
        event_polygons: dict[str, dict] = {}

        relevant = df[df["alert_type"].isin(["early_warning", "missiles", "all_clear"])]

        for _, row in relevant.iterrows():
            atype, city_ids, t = row["alert_type"], row["city_ids"], row["timestamp"]

            # Force-close expired
            for cid in [c for c, ev in open_events.items()
                        if (t - ev["start"]).total_seconds() > MAX_OPEN_EVENT_SEC]:
                ev = open_events.pop(cid)
                city_stats.setdefault(cid, {"warned": 0, "hit": 0})
                city_stats[cid]["warned"] += 1
                if ev["hit"]:
                    city_stats[cid]["hit"] += 1

            if atype == "early_warning":
                ev_key = str(t.floor("5min"))
                if ev_key not in event_polygons:
                    event_polygons[ev_key] = {"start": t, "warned_ids": set(), "hit_ids": set()}
                event_polygons[ev_key]["warned_ids"].update(city_ids)
                for cid in city_ids:
                    if cid not in open_events:
                        open_events[cid] = {"start": t, "hit": False, "ev_key": ev_key}

            elif atype == "missiles":
                for cid in city_ids:
                    if cid in open_events:
                        open_events[cid]["hit"] = True
                        ev_key = open_events[cid].get("ev_key", "")
                        if ev_key in event_polygons:
                            event_polygons[ev_key]["hit_ids"].add(cid)

            elif atype == "all_clear":
                for cid in city_ids:
                    if cid in open_events:
                        ev = open_events.pop(cid)
                        city_stats.setdefault(cid, {"warned": 0, "hit": 0})
                        city_stats[cid]["warned"] += 1
                        if ev["hit"]:
                            city_stats[cid]["hit"] += 1

        # Close remaining
        for cid, ev in open_events.items():
            city_stats.setdefault(cid, {"warned": 0, "hit": 0})
            city_stats[cid]["warned"] += 1
            if ev["hit"]:
                city_stats[cid]["hit"] += 1

        return city_stats, event_polygons

    def _build_events_output(self, event_polygons: dict, id_to_name: dict) -> list[dict]:
        events = []
        for ev_key, ev in sorted(event_polygons.items()):
            if not ev["warned_ids"]:
                continue
            warned_names = sorted(id_to_name.get(cid, str(cid)) for cid in ev["warned_ids"])
            hit_names = sorted(id_to_name.get(cid, str(cid)) for cid in ev["hit_ids"])
            events.append({
                "timestamp": ev["start"].isoformat(),
                "hour": ev["start"].hour,
                "polygon_size": len(warned_names),
                "warned_cities": warned_names,
                "hit_cities": hit_names,
                "total_hit": len(hit_names),
            })
        logger.info(f"  Built {len(events)} event polygons")
        return events

    def _build_base_rates(self, city_stats: dict, id_to_name: dict) -> dict:
        base_rates = {}
        for cid, s in city_stats.items():
            name = id_to_name.get(cid, str(cid))
            if s["warned"] > 0:
                base_rates[name] = {
                    "warned": s["warned"],
                    "hit": s["hit"],
                    "rate": round(s["hit"] / s["warned"], 4),
                }
        return base_rates

    def _build_metadata(self, df: pd.DataFrame, events: list, base_rates: dict) -> dict:
        timestamps = df["timestamp"].dropna()
        return {
            "start_date": DATA_START_DATE,
            "end_date": str(timestamps.max().date()) if len(timestamps) else "",
            "last_updated": datetime.now(ISRAEL_TZ).isoformat(),
            "total_alerts": len(df),
            "total_events": len(events),
            "total_cities": len(base_rates),
        }


class DataScraper:
    """Orchestrates fetching, processing, and saving alert data."""

    def __init__(self):
        self.fetcher = AlertFetcher()
        self.builder = EventBuilder()

    async def run(self, use_cache: bool = False):
        if use_cache and os.path.exists(RAW_CACHE_FILE):
            logger.info(f"Loading from cache: {RAW_CACHE_FILE}")
            with open(RAW_CACHE_FILE, "r", encoding="utf-8") as f:
                alerts = json.load(f)
            logger.info(f"  Loaded {len(alerts)} cached alerts.")
        else:
            logger.info(f"Pulling alerts from {DATA_START_DATE} ...")
            alerts = await self.fetcher.fetch_all()
            if not alerts:
                logger.error("No data from API.")
                sys.exit(1)
            with open(RAW_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(alerts, f, ensure_ascii=False)
            logger.info(f"  Cached → {RAW_CACHE_FILE}")

        events, base_rates, meta = self.builder.build(alerts)
        self._save(events, base_rates, meta)

    @staticmethod
    def _save(events: list, base_rates: dict, meta: dict):
        for path, data in [
            (EVENT_HISTORY_FILE, events),
            (CITY_BASE_RATES_FILE, base_rates),
        ]:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            logger.info(f"  Saved → {path}")

        with open(DATA_META_FILE, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        logger.info(f"  Saved → {DATA_META_FILE}")
        logger.info(f"\n✅ Done! {meta['total_events']} events, {meta['total_cities']} cities")


async def main():
    scraper = DataScraper()
    await scraper.run(use_cache="--local" in sys.argv)


if __name__ == "__main__":
    asyncio.run(main())
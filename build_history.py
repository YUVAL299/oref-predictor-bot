"""
Builds event_history.json from alerts_raw_data.csv.

For each early-warning incident, stores:
  - The warned polygon (set of regions + set of cities)
  - Which cities actually got hit by missiles within 1-20 min
  - Metadata (hour, polygon size, etc.)

Also builds city_base_rates.json for fast lookups.

Run once after scraping new data:
    python build_history.py [alerts_raw_data.csv]
"""
import pandas as pd
import json
import sys


def clean_regions(r):
    if pd.isna(r):
        return []
    return [p.strip() for p in r.split(",") if p.strip() and not p.strip().startswith("תעשייה")]


def get_cities(c):
    if pd.isna(c):
        return []
    return [x.strip() for x in c.split(",") if x.strip() and len(x.strip()) < 40 and ":" not in x]


def build(csv_path="alerts_raw_data.csv"):
    print(f"Reading {csv_path} ...")
    df = pd.read_csv(csv_path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    ew_df = df[df["alert_type"] == "early_warning"]
    miss_df = df[df["alert_type"] == "missiles"]
    print(f"Rows: {len(df)} | Early warnings: {len(ew_df)} | Missiles: {len(miss_df)}")

    # ── Group early warnings into incidents (>30 min gap) ──────────
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
    print(f"Early-warning incidents: {len(events_raw)}")

    # ── Build event history ────────────────────────────────────────
    events_out: list[dict] = []
    city_stats: dict[str, dict] = {}  # city → {warned, hit}

    for ev in events_raw:
        warned_regions: set[str] = set()
        warned_cities: set[str] = set()
        for w in ev["warnings"]:
            warned_regions.update(clean_regions(w["regions"]))
            warned_cities.update(get_cities(w["cities"]))
        if not warned_cities:
            continue

        w_start = ev["start"] + pd.Timedelta(minutes=1)
        w_end = ev["start"] + pd.Timedelta(minutes=20)
        window = miss_df[(miss_df["timestamp"] >= w_start) & (miss_df["timestamp"] <= w_end)]

        hit_cities: set[str] = set()
        for _, m in window.iterrows():
            hit_cities.update(get_cities(m["cities"]))

        hit_in_polygon = hit_cities & warned_cities

        events_out.append({
            "timestamp": ev["start"].isoformat(),
            "hour": ev["start"].hour,
            "regions": sorted(warned_regions),
            "polygon_size": len(warned_cities),
            "num_regions": len(warned_regions),
            "hit_cities": sorted(hit_in_polygon),
            "total_hit": len(hit_in_polygon),
        })

        # Accumulate per-city stats
        for city in warned_cities:
            city_stats.setdefault(city, {"warned": 0, "hit": 0})
            city_stats[city]["warned"] += 1
            if city in hit_in_polygon:
                city_stats[city]["hit"] += 1

    # ── Save event history ─────────────────────────────────────────
    with open("event_history.json", "w", encoding="utf-8") as f:
        json.dump(events_out, f, ensure_ascii=False)
    print(f"Saved {len(events_out)} events → event_history.json")

    # ── Save city base rates ───────────────────────────────────────
    base_rates: dict[str, dict] = {}
    for city, s in sorted(city_stats.items()):
        if s["warned"] > 0:
            base_rates[city] = {
                "warned": s["warned"],
                "hit": s["hit"],
                "rate": round(s["hit"] / s["warned"], 4),
            }

    with open("city_base_rates.json", "w", encoding="utf-8") as f:
        json.dump(base_rates, f, ensure_ascii=False)
    print(f"Saved {len(base_rates)} city base rates → city_base_rates.json")


if __name__ == "__main__":
    csv = sys.argv[1] if len(sys.argv) > 1 else "alerts_raw_data.csv"
    build(csv)
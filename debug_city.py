"""
Export all events for a specific city to a readable text file.
Shows each early warning, whether it led to a hit, and the endAlert timing.

Usage:
    python debug_city.py "רמת גן - מערב"
    python debug_city.py "קריית שמונה"
"""
import json
import sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict

ISRAEL_TZ = timezone(timedelta(hours=3))
CACHE_FILE = "raw_alerts_cache.json"


def classify_type(api_type: str) -> str:
    t = (api_type or "").lower().strip()
    if t in ("missiles", "rockets"):
        return "missiles"
    if t in ("newsflash", "early_warning", "system"):
        return "early_warning"
    if t in ("endalert", "all_clear"):
        return "all_clear"
    if t in ("hostileaircraftintrusion",):
        return "hostile_aircraft"
    return t or "other"


def to_israel_time(ts_str: str) -> str:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.astimezone(ISRAEL_TZ).strftime("%d/%m %H:%M:%S")
    except:
        return ts_str


def main():
    if len(sys.argv) < 2:
        print("Usage: python debug_city.py \"רמת גן - מערב\"")
        sys.exit(1)

    target_city = sys.argv[1]

    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        raw_alerts = json.load(f)

    # Filter alerts that contain the target city
    relevant = []
    for a in raw_alerts:
        atype = classify_type(a.get("type", ""))
        if atype not in ("early_warning", "missiles", "all_clear"):
            continue

        cities = [c["name"] for c in a.get("cities", []) if c.get("name")]

        # For early_warning, filter out nationwide broadcasts
        if atype == "early_warning":
            real_cities = [c for c in cities if c not in ("ברחבי הארץ", "כל הארץ")]
            has_zone = any(c.get("zone") is not None for c in a.get("cities", []) if c.get("name") and c["name"] not in ("ברחבי הארץ", "כל הארץ"))
            if not real_cities or not has_zone:
                continue
            cities = real_cities

        if target_city not in cities:
            continue

        relevant.append({
            "timestamp": a.get("timestamp", ""),
            "type": atype,
            "cities_count": len(cities),
        })

    # Sort by timestamp
    relevant.sort(key=lambda x: x["timestamp"])

    # Merge early warnings within 30 seconds
    merged = []
    i = 0
    while i < len(relevant):
        r = relevant[i]
        if r["type"] == "early_warning":
            # Collect all EWs within 30 seconds
            group_cities = r["cities_count"]
            j = i + 1
            while j < len(relevant) and relevant[j]["type"] == "early_warning":
                from datetime import datetime as dt
                t1 = dt.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
                t2 = dt.fromisoformat(relevant[j]["timestamp"].replace("Z", "+00:00"))
                if abs((t2 - t1).total_seconds()) <= 30:
                    group_cities += relevant[j]["cities_count"]
                    j += 1
                else:
                    break
            merged.append({
                "timestamp": r["timestamp"],
                "type": "early_warning",
                "cities_count": group_cities,
            })
            i = j
        else:
            merged.append(r)
            i += 1
    relevant = merged

    # Now simulate per-city event tracking
    lines = []
    lines.append(f"{'='*70}")
    lines.append(f"  Events for: {target_city}")
    lines.append(f"  Total relevant records: {len(relevant)}")
    lines.append(f"  Period: {to_israel_time(relevant[0]['timestamp'])} → {to_israel_time(relevant[-1]['timestamp'])}")
    lines.append(f"{'='*70}\n")

    event_num = 0
    is_open = False
    event_start = ""
    was_hit = False
    missiles_in_event = 0

    ew_count = 0
    hit_count = 0
    no_hit_count = 0

    for r in relevant:
        ts = to_israel_time(r["timestamp"])
        atype = r["type"]

        if atype == "early_warning":
            if not is_open:
                # New event starts
                event_num += 1
                is_open = True
                event_start = ts
                was_hit = False
                missiles_in_event = 0
                lines.append(f"── Event #{event_num} ──────────────────────────────────")

            ew_count += 1
            lines.append(f"  ⚠️  {ts}  EARLY WARNING  ({r['cities_count']} cities in polygon)")

        elif atype == "missiles" and is_open:
            was_hit = True
            missiles_in_event += 1
            lines.append(f"  🔴  {ts}  MISSILES       ({r['cities_count']} cities hit)")

        elif atype == "missiles" and not is_open:
            lines.append(f"  ❗  {ts}  MISSILES (NO ACTIVE EW!)  ({r['cities_count']} cities)")

        elif atype == "all_clear":
            if is_open:
                result = "✅ HIT" if was_hit else "⬜ NO HIT"
                if was_hit:
                    hit_count += 1
                else:
                    no_hit_count += 1
                lines.append(f"  🏁  {ts}  END ALERT      → {result}  ({missiles_in_event} missile alerts)")
                lines.append("")
                is_open = False
            else:
                lines.append(f"  🏁  {ts}  END ALERT (no open event)")

    # Close remaining
    if is_open:
        result = "✅ HIT" if was_hit else "⬜ NO HIT"
        if was_hit:
            hit_count += 1
        else:
            no_hit_count += 1
        lines.append(f"  ⏰  (event still open at end of data) → {result}")
        lines.append("")

    lines.append(f"\n{'='*70}")
    lines.append(f"  SUMMARY for {target_city}")
    lines.append(f"{'='*70}")
    lines.append(f"  Total events:        {event_num}")
    lines.append(f"  Events with hit:     {hit_count}")
    lines.append(f"  Events without hit:  {no_hit_count}")
    if event_num > 0:
        lines.append(f"  Hit rate:            {hit_count}/{event_num} = {hit_count/event_num*100:.1f}%")
    lines.append(f"  Total EW records:    {ew_count}")
    lines.append(f"{'='*70}")

    output = "\n".join(lines)

    # Save to file
    filename = f"debug_{target_city.replace(' ', '_').replace('-', '')}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(output)

    print(output)
    print(f"\nSaved to: {filename}")


if __name__ == "__main__":
    main()
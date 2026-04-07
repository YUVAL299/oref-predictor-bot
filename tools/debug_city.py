"""Export all events for a specific city to a readable text file.

Usage:
    python -m tools.debug_city "רמת גן - מערב"
"""
import json
import sys
from datetime import datetime, timezone, timedelta

ISRAEL_TZ = timezone(timedelta(hours=3))
CACHE_FILE = "raw_alerts_cache.json"


def classify(api_type: str) -> str:
    t = (api_type or "").lower().strip()
    if t in ("missiles", "rockets"): return "missiles"
    if t in ("newsflash",): return "early_warning"
    if t in ("endalert",): return "all_clear"
    return t


def to_il(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ISRAEL_TZ).strftime("%d/%m %H:%M:%S")
    except:
        return ts


def main():
    if len(sys.argv) < 2:
        print('Usage: python -m tools.debug_city "רמת גן - מערב"')
        sys.exit(1)
    target = sys.argv[1]

    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)

    relevant = []
    for a in raw:
        atype = classify(a.get("type", ""))
        if atype not in ("early_warning", "missiles", "all_clear"):
            continue
        cities = [c["name"] for c in a.get("cities", []) if c.get("name")]
        if atype == "early_warning":
            cities = [c for c in cities if c not in ("ברחבי הארץ", "כל הארץ")]
            has_zone = any(c.get("zone") is not None for c in a.get("cities", [])
                          if c.get("name") and c["name"] not in ("ברחבי הארץ", "כל הארץ"))
            if not cities or not has_zone:
                continue
        if target not in cities:
            continue
        relevant.append({"timestamp": a["timestamp"], "type": atype, "cities_count": len(cities)})

    relevant.sort(key=lambda x: x["timestamp"])
    if not relevant:
        print(f"No data for {target}")
        return

    lines = [f"{'='*70}", f"  Events for: {target}", f"  Records: {len(relevant)}",
             f"  Period: {to_il(relevant[0]['timestamp'])} → {to_il(relevant[-1]['timestamp'])}", f"{'='*70}\n"]

    event_num = is_open = missiles_in = hit_count = no_hit = ew_count = 0
    was_hit = False

    for r in relevant:
        ts, atype = to_il(r["timestamp"]), r["type"]
        if atype == "early_warning":
            if not is_open:
                event_num += 1
                is_open = True
                was_hit = False
                missiles_in = 0
                lines.append(f"── Event #{event_num} ──────────────────────────────────")
            ew_count += 1
            lines.append(f"  ⚠️  {ts}  EARLY WARNING  ({r['cities_count']} cities)")
        elif atype == "missiles" and is_open:
            was_hit = True
            missiles_in += 1
            lines.append(f"  🔴  {ts}  MISSILES       ({r['cities_count']} cities)")
        elif atype == "missiles":
            lines.append(f"  ❗  {ts}  MISSILES (NO ACTIVE EW!)  ({r['cities_count']} cities)")
        elif atype == "all_clear":
            if is_open:
                result = "✅ HIT" if was_hit else "⬜ NO HIT"
                hit_count += was_hit
                no_hit += not was_hit
                lines.append(f"  🏁  {ts}  END ALERT      → {result}  ({missiles_in} missiles)")
                lines.append("")
                is_open = False

    if is_open:
        result = "✅ HIT" if was_hit else "⬜ NO HIT"
        hit_count += was_hit
        no_hit += not was_hit
        lines.append(f"  ⏰  (still open) → {result}\n")

    lines += [f"\n{'='*70}", f"  SUMMARY: {event_num} events, {hit_count} hit, {no_hit} no hit",
              f"  Hit rate: {hit_count}/{event_num} = {hit_count/max(event_num,1)*100:.1f}%",
              f"  EW records: {ew_count}", f"{'='*70}"]

    output = "\n".join(lines)
    filename = f"debug_{target.replace(' ', '_').replace('-', '')}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(output)
    print(output)
    print(f"\nSaved to: {filename}")


if __name__ == "__main__":
    main()
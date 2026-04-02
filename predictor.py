"""
Polygon-aware predictor.

When a live early warning fires, the predictor:
1. Receives the exact polygon (list of warned city names).
2. Finds all past events where the user's city was in the warning polygon.
3. Weights each past event by how similar its polygon is to the current one
   (Jaccard similarity on the set of warned cities).
4. Computes a similarity-weighted hit probability.

Falls back to the city's overall base rate when polygon info is unavailable.
"""
from __future__ import annotations

import json
import os
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

ISRAEL_TZ = timezone(timedelta(hours=3))


class Predictor:

    def __init__(
        self,
        history_file: str = "event_history.json",
        base_rates_file: str = "city_base_rates.json",
        meta_file: str = "data_meta.json",
    ):
        self.events: list[dict] = []
        self.base_rates: dict[str, dict] = {}
        self.meta: dict = {}
        self._meta_file = meta_file
        self._load(history_file, base_rates_file, meta_file)

    # ── Loading ────────────────────────────────────────────────────
    def _load(self, history_file: str, base_rates_file: str, meta_file: str):
        if os.path.exists(history_file):
            with open(history_file, "r", encoding="utf-8") as f:
                self.events = json.load(f)
            logger.info(f"Loaded {len(self.events)} historical events.")
        else:
            logger.warning(f"{history_file} not found. Run data_scraper.py first.")

        if os.path.exists(base_rates_file):
            with open(base_rates_file, "r", encoding="utf-8") as f:
                self.base_rates = json.load(f)
            logger.info(f"Loaded base rates for {len(self.base_rates)} cities.")

        if os.path.exists(meta_file):
            with open(meta_file, "r", encoding="utf-8") as f:
                self.meta = json.load(f)
            logger.info(f"Data range: {self.meta.get('start_date')} → {self.meta.get('end_date')}")

    # ── Metadata helpers ───────────────────────────────────────────
    def _read_meta(self) -> dict:
        """Re-read meta from disk each time so it reflects the latest scrape."""
        if os.path.exists(self._meta_file):
            with open(self._meta_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return self.meta

    def start_date_text(self) -> str:
        """Fixed start date for the /start message."""
        m = self._read_meta()
        raw = m.get("start_date", "")
        if not raw:
            return "?"
        try:
            d = datetime.strptime(raw, "%Y-%m-%d")
            return d.strftime("%d/%m/%Y")
        except Exception:
            return raw

    def last_updated(self) -> str:
        """Last-updated in Israel time, re-read from disk."""
        m = self._read_meta()
        raw = m.get("last_updated", "")
        if not raw:
            return "לא ידוע"
        try:
            dt = datetime.fromisoformat(raw).astimezone(ISRAEL_TZ)
            return dt.strftime("%d/%m/%Y %H:%M")
        except Exception:
            return raw

    def total_events(self) -> int:
        m = self._read_meta()
        return m.get("total_events", len(self.events))

    # ── Core prediction ────────────────────────────────────────────
    @staticmethod
    def _jaccard(a: set, b: set) -> float:
        if not a and not b:
            return 1.0
        union = a | b
        if not union:
            return 0.0
        return len(a & b) / len(union)

    def predict(self, city: str, live_polygon_cities: list[str] | None = None) -> dict:
        """
        Predict P(alarm in city | early warning).

        If live_polygon_cities is provided, uses polygon-similarity weighting
        on the city sets of historical events.
        Otherwise, returns the city's base rate.
        """
        base = self.base_rates.get(city)
        base_rate = base["rate"] if base else None
        base_warned = base["warned"] if base else 0
        base_hit = base["hit"] if base else 0

        result = {
            "city": city,
            "probability": None,
            "base_rate": base_rate,
            "total_warnings": base_warned,
            "total_hits": base_hit,
            "method": "none",
            "similar_events": 0,
        }

        # ── Polygon-aware prediction ──────────────────────────────
        if live_polygon_cities and self.events:
            live_set = set(live_polygon_cities)
            weighted_hits = 0.0
            weighted_total = 0.0
            similar_count = 0

            for ev in self.events:
                ev_warned = set(ev.get("warned_cities", []))
                ev_hit = set(ev.get("hit_cities", []))

                # Only consider events where the city was in the polygon
                if city not in ev_warned:
                    continue

                sim = self._jaccard(live_set, ev_warned)
                if sim < 0.05:
                    continue

                city_hit = 1.0 if city in ev_hit else 0.0
                weighted_hits += sim * city_hit
                weighted_total += sim
                similar_count += 1

            if weighted_total > 0:
                prob = weighted_hits / weighted_total
                result["probability"] = round(prob * 100, 1)
                result["method"] = "polygon_similarity"
                result["similar_events"] = similar_count
                return result

        # ── Fallback: base rate ────────────────────────────────────
        if base_rate is not None:
            result["probability"] = round(base_rate * 100, 1)
            result["method"] = "base_rate"

        return result

    # ── Formatting ─────────────────────────────────────────────────
    @staticmethod
    def _risk(prob: float) -> tuple[str, str]:
        if prob >= 80:
            return "גבוה מאוד", "🔴"
        if prob >= 60:
            return "גבוה", "🟠"
        if prob >= 40:
            return "בינוני", "🟡"
        if prob >= 20:
            return "נמוך", "🟢"
        return "נמוך מאוד", "⚪"

    def format_status(self, city: str) -> str:
        pred = self.predict(city)
        if pred["probability"] is None:
            return f"❓ אין מספיק נתונים עבור: {city}"

        risk_level, emoji = self._risk(pred["probability"])
        return (
            f"{emoji} *{city}*\n"
            f"\n"
            f"📊 *סיכוי לאזעקה בעקבות התרעה מוקדמת:*\n"
            f"*{pred['probability']}%*\n"
            f"\n"
            f"רמת סיכון: {risk_level}\n"
            f"מבוסס על {pred['total_warnings']} התרעות מוקדמות\n"
            f"מתוכן {pred['total_hits']} הובילו לאזעקה בפועל\n"
            f"\n"
            f"🔄 עדכון אחרון: {self.last_updated()}"
        )

    def format_live_warning(self, city: str, polygon_cities: list[str]) -> str:
        pred = self.predict(city, live_polygon_cities=polygon_cities)
        if pred["probability"] is None:
            return f"🚨 *התרעה מוקדמת פעילה*\nאין נתונים עבור {city}"

        risk_level, emoji = self._risk(pred["probability"])
        advice = "היכנסו למרחב המוגן!" if pred["probability"] >= 60 else "שהו בקרבת מרחב מוגן."

        method_line = ""
        if pred["method"] == "polygon_similarity":
            method_line = f"\n📐 חישוב מבוסס על {pred['similar_events']} אירועים דומים"

        return (
            f"🚨 *התרעה מוקדמת פעילה!*\n"
            f"\n"
            f"{emoji} *{city}*\n"
            f"סיכוי לאזעקה: *{pred['probability']}%*\n"
            f"רמת סיכון: *{risk_level}*"
            f"{method_line}\n"
            f"\n"
            f"💡 *המלצה:* {advice}"
        )
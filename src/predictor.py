"""
Hybrid predictor: empirical distance curve + Random Forest.

- Base rate: city's historical P(alarm | early_warning)
- Live prediction: fits an ellipse to the polygon, computes 12 features,
  and uses a Random Forest trained on all past events.
- Empirical curve: fallback when RF is unavailable.
"""
from __future__ import annotations

import json
import logging
import math
import os
from collections import defaultdict
from datetime import datetime

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier

from src.config import (
    EVENT_HISTORY_FILE, CITY_BASE_RATES_FILE, CITY_COORDS_FILE,
    DATA_META_FILE, MODEL_FILE, ISRAEL_TZ,
)
from src.ellipse import EllipseFitter

logger = logging.getLogger(__name__)


class Predictor:
    """Predicts P(alarm | early_warning) for a city."""

    def __init__(self):
        self.events: list[dict] = []
        self.base_rates: dict[str, dict] = {}
        self.coords: dict[str, dict] = {}
        self.model: RandomForestClassifier | None = None
        self.maha_curve: list[dict] = []

        self._load_data()

    # Data loading
    def _load_data(self):
        for path, attr, label in [
            (EVENT_HISTORY_FILE, "events", "events"),
            (CITY_BASE_RATES_FILE, "base_rates", "city base rates"),
            (CITY_COORDS_FILE, "coords", "city coordinates"),
        ]:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    setattr(self, attr, json.load(f))
                logger.info(f"Loaded {len(getattr(self, attr))} {label}.")

        self._build_empirical_curve()

        if os.path.exists(MODEL_FILE):
            self.model = joblib.load(MODEL_FILE)
            logger.info(f"Loaded model from {MODEL_FILE}.")
        else:
            self._train_model()

    # Empirical curve
    def _build_empirical_curve(self, n_bins: int = 20):
        """Build Mahalanobis distance → hit rate lookup from history."""
        if not self.events or not self.coords:
            return

        all_maha, all_hits = [], []

        for ev in self.events:
            wc = [(c, self.coords[c]) for c in ev["warned_cities"] if c in self.coords]
            if len(wc) < 10:
                continue
            points = np.array([[co["lat"], co["lng"]] for _, co in wc])
            ellipse = EllipseFitter(points)
            if not ellipse.valid:
                continue
            hit_set = set(ev["hit_cities"])
            for city, co in wc:
                m = ellipse.mahalanobis(co["lat"], co["lng"])
                if m is not None:
                    all_maha.append(m)
                    all_hits.append(1 if city in hit_set else 0)

        if len(all_maha) < 100:
            return

        all_maha, all_hits = np.array(all_maha), np.array(all_hits)
        sorted_idx = np.argsort(all_maha)
        bin_size = len(all_maha) // n_bins

        self.maha_curve = []
        for i in range(n_bins):
            s = i * bin_size
            e = s + bin_size if i < n_bins - 1 else len(all_maha)
            idx = sorted_idx[s:e]
            self.maha_curve.append({
                "maha": float(np.median(all_maha[idx])),
                "hit_rate": float(all_hits[idx].mean()),
            })

        logger.info(
            f"Empirical curve: {self.maha_curve[0]['hit_rate']*100:.0f}% (center) → "
            f"{self.maha_curve[-1]['hit_rate']*100:.0f}% (edge)"
        )

    def _lookup_curve(self, maha: float) -> float:
        if not self.maha_curve:
            return 0.35
        if maha <= self.maha_curve[0]["maha"]:
            return self.maha_curve[0]["hit_rate"]
        if maha >= self.maha_curve[-1]["maha"]:
            return self.maha_curve[-1]["hit_rate"]
        for i in range(len(self.maha_curve) - 1):
            m0, m1 = self.maha_curve[i]["maha"], self.maha_curve[i + 1]["maha"]
            if m0 <= maha <= m1:
                t = (maha - m0) / (m1 - m0) if m1 > m0 else 0
                return self.maha_curve[i]["hit_rate"] + t * (self.maha_curve[i + 1]["hit_rate"] - self.maha_curve[i]["hit_rate"])
        return self.maha_curve[-1]["hit_rate"]

    # Model training
    def _train_model(self):
        if not self.events or not self.coords:
            return

        city_warned = defaultdict(int)
        city_hit = defaultdict(int)
        X_rows, y_rows = [], []

        for ev in self.events:
            hit_set = set(ev["hit_cities"])
            wc = [(c, self.coords[c]) for c in ev["warned_cities"] if c in self.coords]
            if len(wc) < 10:
                continue

            points = np.array([[co["lat"], co["lng"]] for _, co in wc])
            ellipse = EllipseFitter(points)
            if not ellipse.valid:
                continue

            for city, co in wc:
                w, h = city_warned[city], city_hit[city]
                base_rate = h / w if w > 0 else 0.35
                maha = ellipse.mahalanobis(co["lat"], co["lng"]) or 5.0

                X_rows.append([
                    maha, base_rate, w, len(ev["warned_cities"]),
                    co["lat"], co["lng"],
                    ellipse.euclidean(co["lat"], co["lng"]),
                    ev.get("hour", 12),
                    ellipse.lat_spread, ellipse.lng_spread,
                    ellipse.eccentricity,
                    ellipse.angle(co["lat"], co["lng"]),
                ])
                y_rows.append(1 if city in hit_set else 0)

            for city in ev["warned_cities"]:
                city_warned[city] += 1
                if city in hit_set:
                    city_hit[city] += 1

        if len(X_rows) < 100:
            return

        logger.info(f"Training Random Forest on {len(X_rows)} samples...")
        self.model = RandomForestClassifier(
            n_estimators=200, max_depth=12, random_state=42, n_jobs=-1
        )
        self.model.fit(np.array(X_rows), np.array(y_rows))
        joblib.dump(self.model, MODEL_FILE)
        logger.info(f"Model saved to {MODEL_FILE}.")

    # Feature computation
    def _compute_features(self, city: str, polygon_cities: list[str]) -> tuple[np.ndarray | None, float | None]:
        if city not in self.coords:
            return None, None

        wc = [(c, self.coords[c]) for c in polygon_cities if c in self.coords]
        if len(wc) < 5:
            return None, None

        points = np.array([[co["lat"], co["lng"]] for _, co in wc])
        ellipse = EllipseFitter(points)
        if not ellipse.valid:
            return None, None

        co = self.coords[city]
        base = self.base_rates.get(city, {})
        maha = ellipse.mahalanobis(co["lat"], co["lng"])

        features = np.array([[
            maha or 5.0,
            base.get("rate", 0.35),
            base.get("warned", 0),
            len(polygon_cities),
            co["lat"], co["lng"],
            ellipse.euclidean(co["lat"], co["lng"]),
            datetime.now(ISRAEL_TZ).hour,
            ellipse.lat_spread, ellipse.lng_spread,
            ellipse.eccentricity,
            ellipse.angle(co["lat"], co["lng"]),
        ]])

        return features, maha

    # Prediction
    def predict(self, city: str, live_polygon: list[str] | None = None) -> dict:
        base = self.base_rates.get(city)
        result = {
            "city": city,
            "probability": None,
            "base_rate": round(base["rate"] * 100, 1) if base else None,
            "total_warnings": base["warned"] if base else 0,
            "total_hits": base["hit"] if base else 0,
            "method": "none",
            "mahalanobis": None,
            "position": None,
        }

        if live_polygon:
            features, maha = self._compute_features(city, live_polygon)
            if features is not None and maha is not None:
                result["mahalanobis"] = round(maha, 2)
                result["position"] = "במרכז" if maha < 0.8 else "באמצע" if maha < 1.3 else "בשולי"

                if self.model is not None:
                    prob = float(self.model.predict_proba(features)[0][1])
                    result["method"] = "ml_model"
                else:
                    prob = self._lookup_curve(maha)
                    result["method"] = "ellipse_curve"

                result["probability"] = round(prob * 100, 1)
                return result

        if base:
            result["probability"] = round(base["rate"] * 100, 1)
            result["method"] = "base_rate"

        return result

    # Metadata
    @staticmethod
    def _read_meta() -> dict:
        if os.path.exists(DATA_META_FILE):
            with open(DATA_META_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def start_date_text(self) -> str:
        raw = self._read_meta().get("start_date", "")
        try:
            return datetime.strptime(raw, "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            return raw or "?"

    def last_updated(self) -> str:
        raw = self._read_meta().get("last_updated", "")
        try:
            return datetime.fromisoformat(raw).astimezone(ISRAEL_TZ).strftime("%d/%m/%Y %H:%M")
        except Exception:
            return raw or "לא ידוע"

    def total_events(self) -> int:
        return self._read_meta().get("total_events", len(self.events))

    # Formatting
    @staticmethod
    def risk_level(prob: float) -> tuple[str, str]:
        """Returns (Hebrew label, emoji) for a probability."""
        if prob >= 80: return "גבוה מאוד", "🔴"
        if prob >= 60: return "גבוה", "🟠"
        if prob >= 40: return "בינוני", "🟡"
        if prob >= 20: return "נמוך", "🟢"
        return "נמוך מאוד", "⚪"

    def format_status(self, city: str) -> str:
        pred = self.predict(city)
        if pred["probability"] is None:
            return f"❓ אין מספיק נתונים עבור: {city}"
        level, emoji = self.risk_level(pred["probability"])
        return (
            f"{emoji} *{city}*\n\n"
            f"📊 *סיכוי לאזעקה בעקבות התרעה מוקדמת:*\n"
            f"*{pred['probability']}%*\n\n"
            f"רמת סיכון: {level}\n"
            f"מבוסס על {pred['total_warnings']} התרעות מוקדמות\n"
            f"מתוכן {pred['total_hits']} הובילו לאזעקה בפועל\n\n"
            f"🔄 עדכון אחרון: {self.last_updated()}"
        )

    def format_live_warning(self, city: str, polygon: list[str]) -> str:
        pred = self.predict(city, live_polygon=polygon)
        if pred["probability"] is None:
            return f"🚨 *התרעה מוקדמת פעילה*\nאין נתונים עבור {city}"
        level, emoji = self.risk_level(pred["probability"])
        advice = "היכנסו למרחב המוגן!" if pred["probability"] >= 60 else "שהו בקרבת מרחב מוגן."
        geo = f"\n📍 המיקום שלך {pred['position']} אליפסת ההתרעה" if pred["position"] else ""
        return (
            f"🚨 *התרעה מוקדמת פעילה!*\n\n"
            f"{emoji} *{city}*\n"
            f"סיכוי לאזעקה: *{pred['probability']}%*\n"
            f"רמת סיכון: *{level}*{geo}\n\n"
            f"💡 *המלצה:* {advice}"
        )
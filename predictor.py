"""
Hybrid predictor: empirical curve + Random Forest.

- /status (no live polygon): shows the city's base hit rate
- Live early warning: fits an ellipse, computes 12 features,
  and uses a Random Forest trained on all past events

Features:
  1.  Mahalanobis distance from ellipse center
  2.  City's historical base hit rate
  3.  Times the city has been warned before
  4.  Polygon size (number of warned cities)
  5.  City's latitude
  6.  City's longitude
  7.  Euclidean distance from center
  8.  Hour of day
  9.  Polygon latitude spread
  10. Polygon longitude spread
  11. Ellipse eccentricity (how elongated)
  12. Angle from center (directional pattern)

The RF is trained once on startup and saved to disk.
The empirical curve is used as a transparent fallback.
"""
from __future__ import annotations

import json
import os
import logging
import math
import joblib
import numpy as np
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from sklearn.ensemble import RandomForestClassifier

logger = logging.getLogger(__name__)

ISRAEL_TZ = timezone(timedelta(hours=3))
MODEL_FILE = "alert_model.pkl"


class Predictor:

    def __init__(
        self,
        history_file: str = "event_history.json",
        base_rates_file: str = "city_base_rates.json",
        coords_file: str = "city_coords.json",
        meta_file: str = "data_meta.json",
    ):
        self.events: list[dict] = []
        self.base_rates: dict[str, dict] = {}
        self.coords: dict[str, dict] = {}
        self.meta: dict = {}
        self._meta_file = meta_file
        self.model: RandomForestClassifier | None = None
        self.maha_curve: list[dict] = []

        self._load(history_file, base_rates_file, coords_file, meta_file)

    # ── Loading ────────────────────────────────────────────────────
    def _load(self, history_file, base_rates_file, coords_file, meta_file):
        if os.path.exists(history_file):
            with open(history_file, "r", encoding="utf-8") as f:
                self.events = json.load(f)
            logger.info(f"Loaded {len(self.events)} historical events.")

        if os.path.exists(base_rates_file):
            with open(base_rates_file, "r", encoding="utf-8") as f:
                self.base_rates = json.load(f)
            logger.info(f"Loaded base rates for {len(self.base_rates)} cities.")

        if os.path.exists(coords_file):
            with open(coords_file, "r", encoding="utf-8") as f:
                self.coords = json.load(f)
            logger.info(f"Loaded coordinates for {len(self.coords)} cities.")

        if os.path.exists(meta_file):
            with open(meta_file, "r", encoding="utf-8") as f:
                self.meta = json.load(f)

        # Build empirical curve (always, for fallback)
        self._build_curve()

        # Load or train RF model
        if os.path.exists(MODEL_FILE):
            self.model = joblib.load(MODEL_FILE)
            logger.info(f"Loaded RF model from {MODEL_FILE}")
        else:
            self._train_model()

    # ── Empirical curve (fallback) ─────────────────────────────────
    def _build_curve(self, n_bins: int = 20):
        if not self.events or not self.coords:
            return

        all_maha = []
        all_hits = []

        for ev in self.events:
            warned = ev["warned_cities"]
            hit_set = set(ev["hit_cities"])
            wc = [(c, self.coords[c]) for c in warned if c in self.coords]
            if len(wc) < 10:
                continue

            points = np.array([[co["lat"], co["lng"]] for _, co in wc])
            center = points.mean(axis=0)
            cov = np.cov(points.T)
            try:
                cov_inv = np.linalg.inv(cov)
            except np.linalg.LinAlgError:
                continue

            for city, co in wc:
                diff = np.array([co["lat"], co["lng"]]) - center
                all_maha.append(float(np.sqrt(diff @ cov_inv @ diff)))
                all_hits.append(1 if city in hit_set else 0)

        if len(all_maha) < 100:
            return

        all_maha = np.array(all_maha)
        all_hits = np.array(all_hits)
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

        logger.info(f"Built empirical curve: {self.maha_curve[0]['hit_rate']*100:.0f}% → {self.maha_curve[-1]['hit_rate']*100:.0f}%")

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

    # ── RF Training ────────────────────────────────────────────────
    def _train_model(self):
        if not self.events or not self.coords:
            logger.warning("No data to train model.")
            return

        city_warned = defaultdict(int)
        city_hit = defaultdict(int)
        X_rows = []
        y_rows = []

        for ev in self.events:
            warned = ev["warned_cities"]
            hit_set = set(ev["hit_cities"])
            wc = [(c, self.coords[c]) for c in warned if c in self.coords]
            if len(wc) < 10:
                continue

            points = np.array([[co["lat"], co["lng"]] for _, co in wc])
            center = points.mean(axis=0)
            cov = np.cov(points.T)
            try:
                cov_inv = np.linalg.inv(cov)
            except np.linalg.LinAlgError:
                continue

            eigenvalues = np.linalg.eigvalsh(cov)
            eccentricity = math.sqrt(1 - eigenvalues.min() / eigenvalues.max()) if eigenvalues.max() > 0 else 0

            for city, co in wc:
                w = city_warned[city]
                h = city_hit[city]
                base_rate = h / w if w > 0 else 0.35

                pt = np.array([co["lat"], co["lng"]])
                diff = pt - center
                maha = float(np.sqrt(diff @ cov_inv @ diff))
                eucl = float(np.sqrt((diff ** 2).sum()))
                angle = float(np.arctan2(diff[1], diff[0]))

                X_rows.append([
                    maha, base_rate, w, len(warned),
                    co["lat"], co["lng"], eucl,
                    ev.get("hour", 12),
                    float(np.std(points[:, 0])),
                    float(np.std(points[:, 1])),
                    eccentricity, angle,
                ])
                y_rows.append(1 if city in hit_set else 0)

            for city in warned:
                city_warned[city] += 1
                if city in hit_set:
                    city_hit[city] += 1

        if len(X_rows) < 100:
            logger.warning("Not enough data for RF.")
            return

        X = np.array(X_rows)
        y = np.array(y_rows)

        logger.info(f"Training RF on {len(X)} samples...")
        self.model = RandomForestClassifier(
            n_estimators=200, max_depth=12, random_state=42, n_jobs=-1
        )
        self.model.fit(X, y)
        joblib.dump(self.model, MODEL_FILE)
        logger.info(f"Model saved to {MODEL_FILE}")

    # ── Feature computation ────────────────────────────────────────
    def _compute_features(self, city: str, polygon_cities: list[str], hour: int = -1) -> tuple[np.ndarray | None, float | None]:
        """Returns (feature_vector, mahalanobis_distance) or (None, None)."""
        if city not in self.coords:
            return None, None

        wc = [(c, self.coords[c]) for c in polygon_cities if c in self.coords]
        if len(wc) < 5:
            return None, None

        points = np.array([[co["lat"], co["lng"]] for _, co in wc])
        center = points.mean(axis=0)
        cov = np.cov(points.T)
        try:
            cov_inv = np.linalg.inv(cov)
        except np.linalg.LinAlgError:
            return None, None

        eigenvalues = np.linalg.eigvalsh(cov)
        eccentricity = math.sqrt(1 - eigenvalues.min() / eigenvalues.max()) if eigenvalues.max() > 0 else 0

        co = self.coords[city]
        pt = np.array([co["lat"], co["lng"]])
        diff = pt - center
        maha = float(np.sqrt(diff @ cov_inv @ diff))
        eucl = float(np.sqrt((diff ** 2).sum()))
        angle = float(np.arctan2(diff[1], diff[0]))

        base = self.base_rates.get(city, {})
        base_rate = base.get("rate", 0.35)
        warned_n = base.get("warned", 0)

        if hour < 0:
            hour = datetime.now(ISRAEL_TZ).hour

        features = np.array([[
            maha, base_rate, warned_n, len(polygon_cities),
            co["lat"], co["lng"], eucl, hour,
            float(np.std(points[:, 0])),
            float(np.std(points[:, 1])),
            eccentricity, angle,
        ]])

        return features, maha

    # ── Core prediction ────────────────────────────────────────────
    def predict(self, city: str, live_polygon_cities: list[str] | None = None) -> dict:
        base = self.base_rates.get(city)
        base_rate = base["rate"] if base else None
        base_warned = base["warned"] if base else 0
        base_hit = base["hit"] if base else 0

        result = {
            "city": city,
            "probability": None,
            "base_rate": round(base_rate * 100, 1) if base_rate is not None else None,
            "total_warnings": base_warned,
            "total_hits": base_hit,
            "method": "none",
            "mahalanobis": None,
            "position": None,
        }

        if live_polygon_cities:
            features, maha = self._compute_features(city, live_polygon_cities)

            if features is not None and maha is not None:
                # Position label
                if maha < 0.8:
                    position = "במרכז"
                elif maha < 1.3:
                    position = "באמצע"
                else:
                    position = "בשולי"

                # Use RF if available, otherwise fall back to empirical curve
                if self.model is not None:
                    prob = float(self.model.predict_proba(features)[0][1])
                    result["method"] = "ml_model"
                else:
                    prob = self._lookup_curve(maha)
                    result["method"] = "ellipse_curve"

                result["probability"] = round(prob * 100, 1)
                result["mahalanobis"] = round(maha, 2)
                result["position"] = position
                return result

        # Fallback: base rate
        if base_rate is not None:
            result["probability"] = round(base_rate * 100, 1)
            result["method"] = "base_rate"

        return result

    # ── Metadata helpers ───────────────────────────────────────────
    def _read_meta(self) -> dict:
        if os.path.exists(self._meta_file):
            with open(self._meta_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return self.meta

    def start_date_text(self) -> str:
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
            f"(הנתונים מחושבים החל מהתאריך 15/03/2026)\n"
            f"🔄 עדכון נתונים אחרון: {self.last_updated()}"
        )

    def format_live_warning(self, city: str, polygon_cities: list[str]) -> str:
        pred = self.predict(city, live_polygon_cities=polygon_cities)
        if pred["probability"] is None:
            return f"🚨 *התרעה מוקדמת פעילה*\nאין נתונים עבור {city}"

        risk_level, emoji = self._risk(pred["probability"])
        advice = "היכנסו למרחב המוגן!" if pred["probability"] >= 60 else "שהו בקרבת מרחב מוגן."

        geo_line = ""
        if pred["position"]:
            geo_line = f"\n📍 המיקום שלך {pred['position']} אליפסת ההתרעה"

        return (
            f"🚨 *התרעה מוקדמת פעילה!*\n"
            f"\n"
            f"{emoji} *{city}*\n"
            f"סיכוי לאזעקה: *{pred['probability']}%*\n"
            f"רמת סיכון: *{risk_level}*"
            f"{geo_line}\n"
            f"\n"
            f"💡 *המלצה:* {advice}"
        )
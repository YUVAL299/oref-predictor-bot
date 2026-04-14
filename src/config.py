"""Centralized configuration and constants."""

import os
from datetime import timezone, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# API
REDALERT_API_KEY = os.environ.get("REDALERT_API_KEY", "")
REDALERT_BASE_URL = "https://api.siren.co.il"
REDALERT_HISTORY_URL = f"{REDALERT_BASE_URL}/api/stats/history"
REDALERT_CITIES_URL = f"{REDALERT_BASE_URL}/api/data/cities"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Data
DATA_START_DATE = "2026-03-15"
HISTORY_PAGE_SIZE = 100
EW_MERGE_WINDOW_SEC = 30
MAX_OPEN_EVENT_SEC = 5400  # 90 min safety cap
ALERT_BUFFER_SEC = 10      # seconds to wait before processing live alerts

# Files
EVENT_HISTORY_FILE = "event_history.json"
CITY_BASE_RATES_FILE = "city_base_rates.json"
CITY_COORDS_FILE = "city_coords.json"
DATA_META_FILE = "data_meta.json"
RAW_CACHE_FILE = "raw_alerts_cache.json"
MODEL_FILE = "alert_model.pkl"
DB_FILE = "subscriptions.db"

# Bot UI
CITIES_PER_PAGE = 8
REGIONS_PER_PAGE = 8

# Timezone
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

# API Auth
def api_headers() -> dict:
    """Returns authorization headers for the RedAlert API."""
    if REDALERT_API_KEY:
        return {"Authorization": f"Bearer {REDALERT_API_KEY}"}
    return {}
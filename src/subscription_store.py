"""SQLite-backed subscription storage."""

import sqlite3
import logging

from src.config import DB_FILE

logger = logging.getLogger(__name__)


class SubscriptionStore:
    """Persists user → city subscriptions in SQLite."""

    def __init__(self, db_path: str = DB_FILE):
        self._db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS subscriptions "
            "(user_id INTEGER PRIMARY KEY, city TEXT NOT NULL, zone TEXT NOT NULL DEFAULT '')"
        )
        conn.commit()
        conn.close()

    def load_all(self) -> dict[int, dict]:
        """Returns {user_id: {"city": ..., "zone": ...}}."""
        conn = sqlite3.connect(self._db_path)
        rows = conn.execute("SELECT user_id, city, zone FROM subscriptions").fetchall()
        conn.close()
        subs = {r[0]: {"city": r[1], "zone": r[2]} for r in rows}
        logger.info(f"Loaded {len(subs)} subscriptions.")
        return subs

    def save(self, user_id: int, city: str, zone: str):
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "INSERT OR REPLACE INTO subscriptions (user_id, city, zone) VALUES (?, ?, ?)",
            (user_id, city, zone),
        )
        conn.commit()
        conn.close()

    def delete(self, user_id: int):
        conn = sqlite3.connect(self._db_path)
        conn.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()

    def count(self) -> int:
        conn = sqlite3.connect(self._db_path)
        (n,) = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()
        conn.close()
        return n
"""Maintains a persistent WebSocket connection to the RedAlert API."""

import logging
from typing import Callable, Awaitable

import socketio

from src.config import REDALERT_API_KEY, REDALERT_BASE_URL

logger = logging.getLogger(__name__)


class AlertStreamer:
    """
    Connects to the RedAlert WebSocket and fires a callback
    on every incoming alert.
    """

    def __init__(self, on_alert: Callable[[dict], Awaitable[None]]):
        self._sio = socketio.AsyncClient(
            reconnection=True, reconnection_attempts=0, reconnection_delay=1
        )
        self._on_alert = on_alert
        self._setup_listeners()

    def _setup_listeners(self):
        @self._sio.on("connect")
        async def on_connect():
            logger.info("🟢 Connected to RedAlert WebSocket.")

        @self._sio.on("disconnect")
        async def on_disconnect():
            logger.warning("🔴 Disconnected from RedAlert WebSocket. Reconnecting...")

        @self._sio.on("alert")
        async def on_alert(alerts):
            if isinstance(alerts, list):
                for alert in alerts:
                    logger.info(f"Incoming: {alert.get('type')} — {len(alert.get('cities', []))} cities")
                    await self._on_alert(alert)
            else:
                await self._on_alert(alerts)

        @self._sio.on("endAlert")
        async def on_end_alert(alert):
            logger.info(f"✅ Event ended: {len(alert.get('cities', []))} cities")

    async def start(self):
        """Connects and blocks until disconnected."""
        try:
            auth = {"apiKey": REDALERT_API_KEY} if REDALERT_API_KEY else {}
            await self._sio.connect(REDALERT_BASE_URL, auth=auth)
            await self._sio.wait()
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
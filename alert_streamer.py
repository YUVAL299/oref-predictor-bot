import os
import logging
import socketio
from typing import Callable, Awaitable
from dotenv import load_dotenv

load_dotenv()


class AlertStreamer:
    """
    Maintains a persistent real-time WebSocket connection to the RedAlert API.
    Fires a callback function the millisecond a new alert is pushed.
    """

    def __init__(self, on_alert_callback: Callable[[dict], Awaitable[None]]):
        self.sio = socketio.AsyncClient(reconnection=True, reconnection_attempts=0, reconnection_delay=1)
        self.on_alert_callback = on_alert_callback

        self.api_key = os.getenv("REDALERT_API_KEY")
        if not self.api_key:
            logging.warning("REDALERT_API_KEY is missing from .env!")

        self._setup_event_listeners()

    def _setup_event_listeners(self):
        @self.sio.on('connect')
        async def on_connect():
            logging.info("🟢 Connected to RedAlert WebSocket API!")

        @self.sio.on('disconnect')
        async def on_disconnect():
            logging.warning("🔴 Disconnected from RedAlert API. Attempting to reconnect...")

        @self.sio.on('alert')
        async def on_alert(alerts):
            if isinstance(alerts, list):
                for alert in alerts:
                    logging.info(f"Incoming Alert: {alert.get('type')} - {alert.get('cities', [])}")
                    await self.on_alert_callback(alert)
            else:
                await self.on_alert_callback(alerts)

        @self.sio.on('endAlert')
        async def on_end_alert(alert):
            logging.info(f"✅ Event Ended: {alert.get('cities', [])}")

    async def start(self):
        """Initiates the connection to the server."""
        try:
            auth_data = {'apiKey': self.api_key} if self.api_key else {}
            await self.sio.connect('https://redalert.orielhaim.com', auth=auth_data)
            await self.sio.wait()
        except Exception as e:
            logging.error(f"WebSocket Connection Error: {e}")
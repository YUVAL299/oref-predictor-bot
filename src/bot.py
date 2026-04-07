"""Telegram bot: command handlers, keyboard builders, and live alert processing."""
from __future__ import annotations

import asyncio
import logging
import math

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

from src.config import TELEGRAM_BOT_TOKEN, CITIES_PER_PAGE, REGIONS_PER_PAGE, ALERT_BUFFER_SEC
from src.predictor import Predictor
from src.alert_streamer import AlertStreamer
from src.subscription_store import SubscriptionStore
from src.city_api import CityAPI

logger = logging.getLogger(__name__)


class OrefPredictorBot:
    """Main bot orchestrator."""

    def __init__(self):
        if not TELEGRAM_BOT_TOKEN:
            raise ValueError("TELEGRAM_BOT_TOKEN is missing from .env")

        self.predictor = Predictor()
        self.city_api = CityAPI()
        self.store = SubscriptionStore()
        self.streamer = AlertStreamer(on_alert=self._on_live_alert)

        self.subscriptions: dict[int, dict] = self.store.load_all()
        self.zones: list[str] = []

        # Alert buffering (merges burst newsFlash records)
        self._alert_buffer: list[str] = []
        self._buffer_task: asyncio.Task | None = None

        self.app = (
            Application.builder()
            .token(TELEGRAM_BOT_TOKEN)
            .post_init(self._on_boot)
            .build()
        )
        self._register_handlers()

    # Lifecycle
    async def _on_boot(self, application: Application) -> None:
        self.zones = await self.city_api.get_all_zones()
        logger.info(f"Loaded {len(self.zones)} zones from API.")
        application.create_task(self.streamer.start())

    def run(self):
        logger.info("Bot starting...")
        self.app.run_polling()

    # Registration helpers
    def _subscribe(self, user_id: int, city: str, zone: str):
        self.subscriptions[user_id] = {"city": city, "zone": zone}
        self.store.save(user_id, city, zone)

    def _unsubscribe(self, user_id: int) -> str | None:
        sub = self.subscriptions.pop(user_id, None)
        if sub:
            self.store.delete(user_id)
            return sub["city"]
        return None

    # Handler registration
    def _register_handlers(self):
        h = self.app.add_handler
        h(CommandHandler("start", self.cmd_start))
        h(CommandHandler("select", self.cmd_select))
        h(CommandHandler("status", self.cmd_status))
        h(CommandHandler("stats", self.cmd_stats))
        h(CommandHandler("howto", self.cmd_howto))
        h(CommandHandler("contact", self.cmd_contact))
        h(CommandHandler("unsubscribe", self.cmd_unsubscribe))

        h(CallbackQueryHandler(self.cb_zone_page, pattern=r"^zp:"))
        h(CallbackQueryHandler(self.cb_pick_zone, pattern=r"^zn:"))
        h(CallbackQueryHandler(self.cb_city_page, pattern=r"^cp:"))
        h(CallbackQueryHandler(self.cb_pick_city, pattern=r"^ct:"))
        h(CallbackQueryHandler(self.cb_back_zones, pattern=r"^back_zones$"))
        h(CallbackQueryHandler(self.cb_noop, pattern=r"^noop$"))

        h(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))

    # Keyboard builders
    def _zone_keyboard(self, page: int = 0) -> InlineKeyboardMarkup:
        total_pages = max(1, math.ceil(len(self.zones) / REGIONS_PER_PAGE))
        page = max(0, min(page, total_pages - 1))
        start = page * REGIONS_PER_PAGE
        page_zones = self.zones[start: start + REGIONS_PER_PAGE]

        rows = []
        for i in range(0, len(page_zones), 2):
            rows.append([InlineKeyboardButton(z, callback_data=f"zn:{z}") for z in page_zones[i:i+2]])

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ הקודם", callback_data=f"zp:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("הבא ▶️", callback_data=f"zp:{page + 1}"))
        rows.append(nav)
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _cities_keyboard(cities: list[dict], zone: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(c["name"], callback_data=f"ct:{c['name']}")] for c in cities]
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"cp:{zone}|{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("▶️", callback_data=f"cp:{zone}|{page + 1}"))
        rows.append(nav)
        rows.append([InlineKeyboardButton("🔙 חזרה לאזורים", callback_data="back_zones")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _search_keyboard(cities: list[dict]) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"{c.get('name', '')}  ({c.get('zone', '')})" if c.get("zone") else c.get("name", ""),
                callback_data=f"ct:{c['name']}"
            )] for c in cities
        ])

    # Commands
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🛡️ *בוט ניתוח התרעות מקדימות*\n"
            "\n"
            "כשמתקבלת התרעה מוקדמת, הבוט מחשב את הסיכוי "
            "שתתקבל אזעקה ביישוב שלכם - על בסיס ניתוח "
            "אירועים קודמים והמיקום שלכם בתוך אליפסת ההתרעה.\n"
            "\n"
            "*להפעלת הבוט ביחרו תחילה את שם היישוב:*\n"
            "🔹 *הקלידו שם יישוב* - חיפוש חופשי\n"
            "🔹 /select - או ביחרו מהרשימה\n"
            "\n"
            "ניתן לשנות יישוב בכל עת על ידי שליחת שם יישוב חדש.\n"
            "\n"
            "*פקודות נוספות:*\n"
            "🔹 /status - הסיכוי ליישוב שלכם\n"
            "🔹 /stats - סטטיסטיקות ערים נבחרות\n"
            "🔹 /howto - איך האלגוריתם עובד\n"
            "🔹 /contact - יצירת קשר\n"
            "🔹 /unsubscribe - ביטול רישום\n"
            "\n"
            "⚠️ *שימו לב:* הבוט מספק *הערכה סטטיסטית בלבד* "
            "ואינו מהווה תחליף להנחיות פיקוד העורף. "
            "*היכנסו תמיד למרחב המוגן בעת אזעקה*, "
            "ללא קשר להערכת הבוט.",
            parse_mode="Markdown",
        )

    async def cmd_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.zones:
            self.zones = await self.city_api.get_all_zones()
        await update.message.reply_text(
            "📍 *שלב 1: בחרו אזור*",
            reply_markup=self._zone_keyboard(0),
            parse_mode="Markdown",
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        sub = self.subscriptions.get(update.message.from_user.id)
        if not sub:
            await update.message.reply_text("⚠️ לא בחרתם יישוב עדיין.\nלחצו /select או הקלידו שם יישוב.")
            return
        await update.message.reply_text(self.predictor.format_status(sub["city"]), parse_mode="Markdown")

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        sample = ["תל אביב - מרכז העיר", "ירושלים - מרכז", "חיפה - מערב",
                   "באר שבע - צפון", "רמת גן - מערב", "פתח תקווה",
                   "נתניה - מערב", "קריית שמונה", "אילת"]
        lines = ["📊 *סיכויי אזעקה – ערים נבחרות:*\n"]
        for city in sample:
            pred = self.predictor.predict(city)
            if pred["probability"] is not None:
                _, em = self.predictor.risk_level(pred["probability"])
                lines.append(f"{em} {city}: *{pred['probability']}%*")
        lines.append(f"\n🔄 עדכון אחרון: {self.predictor.last_updated()}")
        lines.append("\nהקלידו שם יישוב או לחצו /select לראות את היישוב שלכם")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_howto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🔬 *איך האלגוריתם עובד?*\n\n"
            "*1. מה זה התרעה מוקדמת?*\n"
            "פיקוד העורף שולח התרעה מוקדמת לאזורים נרחבים "
            "כדי להזהיר אנשים להתקרב למרחב מוגן. "
            "לאחר מספר דקות, חלק מהאזורים מקבלים אזעקת צבע אדום.\n\n"
            "*2. התאמת אליפסה*\n"
            "כשמתקבלת התרעה מוקדמת, הבוט מתאים אליפסה "
            "לפוליגון של היישובים שקיבלו התרעה, "
            "על בסיס הקואורדינטות שלהם.\n\n"
            "*3. מרחק מהמרכז*\n"
            "הבוט מחשב את המרחק של היישוב שלכם ממרכז האליפסה "
            "(מרחק מהלנוביס — המתחשב בצורת האליפסה ובכיוון שלה).\n\n"
            "*4. מודל חיזוי*\n"
            "מודל Random Forest משלב את המרחק מהמרכז עם מאפיינים נוספים: "
            "היסטוריית היישוב, גודל הפוליגון, שעה ביום, צורת האליפסה ועוד — "
            "ומחזיר את ההסתברות לאזעקה.\n\n"
            f"📅 נתונים מ-{self.predictor.start_date_text()}\n"
            f"📊 {self.predictor.total_events()} אירועי התרעה מנותחים\n"
            f"🔄 עדכון אחרון: {self.predictor.last_updated()}\n\n"
            "⚠️ *שימו לב:* הבוט מספק *הערכה סטטיסטית בלבד* "
            "ואינו מהווה תחליף להנחיות פיקוד העורף.",
            parse_mode="Markdown",
        )

    async def cmd_contact(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "📬 *יצירת קשר*\n\n"
            "לשאלות, הערות, דיווח על באגים או הצעות לשיפור:\n\n"
            "📧 OrefPredictorBot@outlook.com",
            parse_mode="Markdown",
        )

    async def cmd_unsubscribe(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        city = self._unsubscribe(update.message.from_user.id)
        if not city:
            await update.message.reply_text("ℹ️ אינכם רשומים כרגע.")
            return
        await update.message.reply_text(
            f"✅ הרישום ל-*{city}* בוטל.\n"
            "לא תקבלו יותר התראות.\n\n"
            "להרשמה מחדש - שלחו שם יישוב או לחצו /select.",
            parse_mode="Markdown",
        )

    # Free-text city search
    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.message.text.strip()
        if len(query) < 2:
            return

        results = await self.city_api.search(query, limit=8)
        if not results:
            await update.message.reply_text(
                f"🔍 לא נמצאו תוצאות עבור *{query}*\nנסו /select לבחירה מהרשימה.",
                parse_mode="Markdown",
            )
            return

        if len(results) == 1:
            city, zone = results[0]["name"], results[0].get("zone", "")
            self._subscribe(update.message.from_user.id, city, zone)
            await update.message.reply_text(
                f"✅ *נרשמתם ל: {city}*\n\n{self.predictor.format_status(city)}",
                parse_mode="Markdown",
            )
            return

        await update.message.reply_text(
            f"🔍 נמצאו {len(results)} תוצאות עבור *{query}*:",
            reply_markup=self._search_keyboard(results),
            parse_mode="Markdown",
        )

    # Callback handlers
    async def cb_zone_page(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        await q.edit_message_reply_markup(reply_markup=self._zone_keyboard(int(q.data.replace("zp:", ""))))

    async def cb_pick_zone(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer("טוען ערים...")
        zone = q.data.replace("zn:", "")
        cities = await self.city_api.get_by_zone(zone, limit=500)
        if not cities:
            await q.edit_message_text(f"⚠️ לא נמצאו ערים באזור {zone}")
            return
        context.user_data["zone_cities"] = cities
        context.user_data["current_zone"] = zone
        total_pages = max(1, math.ceil(len(cities) / CITIES_PER_PAGE))
        await q.edit_message_text(
            f"📍 *שלב 2: בחרו עיר באזור {zone}*\n({len(cities)} ערים)",
            reply_markup=self._cities_keyboard(cities[:CITIES_PER_PAGE], zone, 0, total_pages),
            parse_mode="Markdown",
        )

    async def cb_city_page(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        zone, page = q.data.replace("cp:", "").split("|")
        page = int(page)
        cities = context.user_data.get("zone_cities") or await self.city_api.get_by_zone(zone, limit=500)
        total_pages = max(1, math.ceil(len(cities) / CITIES_PER_PAGE))
        page = max(0, min(page, total_pages - 1))
        start = page * CITIES_PER_PAGE
        await q.edit_message_reply_markup(
            reply_markup=self._cities_keyboard(cities[start:start + CITIES_PER_PAGE], zone, page, total_pages)
        )

    async def cb_pick_city(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        city = q.data.replace("ct:", "")
        zone = context.user_data.get("current_zone", "")
        if not zone:
            results = await self.city_api.search(city, limit=1)
            zone = results[0].get("zone", "") if results else ""
        self._subscribe(q.from_user.id, city, zone)
        await q.edit_message_text(
            f"✅ *נרשמתם ל: {city}*\n\n{self.predictor.format_status(city)}\n\n"
            "תתקבל התראה כשתהיה התרעה מוקדמת באזור שלכם.\n"
            "/unsubscribe לביטול הרשמה לבוט.",
            parse_mode="Markdown",
        )

    async def cb_back_zones(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        await q.edit_message_text("📍 *שלב 1: בחרו אזור*", reply_markup=self._zone_keyboard(0), parse_mode="Markdown")

    async def cb_noop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.callback_query.answer()

    # Live alert processing with buffering
    async def _on_live_alert(self, alert_data: dict):
        """Called by AlertStreamer for each incoming WebSocket event."""
        if alert_data.get("type") not in ("system", "early_warning", "newsFlash"):
            return

        cities = alert_data.get("cities", [])
        if isinstance(cities, str):
            cities = [c.strip() for c in cities.split(",")]
        normalized = [c.get("name", "") if isinstance(c, dict) else str(c).strip() for c in cities]
        new_cities = [c for c in normalized if c]

        if not new_cities:
            return

        self._alert_buffer.extend(new_cities)
        logger.info(f"📥 Buffered {len(new_cities)} cities (total: {len(set(self._alert_buffer))} unique)")

        if self._buffer_task and not self._buffer_task.done():
            self._buffer_task.cancel()
        self._buffer_task = asyncio.create_task(self._flush_after_delay())

    async def _flush_after_delay(self):
        await asyncio.sleep(ALERT_BUFFER_SEC)
        await self._process_merged_polygon()

    async def _process_merged_polygon(self):
        """Process all buffered cities as one merged polygon."""
        if not self._alert_buffer:
            return

        polygon = list(set(self._alert_buffer))
        self._alert_buffer = []
        polygon_set = set(polygon)

        logger.info(f"🚨 Processing merged polygon: {len(polygon)} unique cities")

        for user_id, sub in self.subscriptions.items():
            if sub.get("city") not in polygon_set:
                continue
            try:
                msg = self.predictor.format_live_warning(sub["city"], polygon)
                await self.app.bot.send_message(chat_id=user_id, text=msg, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed to notify user {user_id}: {e}")
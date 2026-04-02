import logging
import math
import os
import sqlite3
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from predictor import Predictor
from alert_streamer import AlertStreamer
import city_api

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

CITIES_PER_PAGE = 8
REGIONS_PER_PAGE = 8
DB_FILE = "subscriptions.db"


# ── SQLite helpers ─────────────────────────────────────────────────
def _init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS subscriptions "
        "(user_id INTEGER PRIMARY KEY, city TEXT NOT NULL, zone TEXT NOT NULL DEFAULT '')"
    )
    conn.commit()
    conn.close()


def _load_subscriptions() -> dict[int, dict]:
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute("SELECT user_id, city, zone FROM subscriptions").fetchall()
    conn.close()
    return {r[0]: {"city": r[1], "zone": r[2]} for r in rows}


def _save_subscription(user_id: int, city: str, zone: str):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "INSERT OR REPLACE INTO subscriptions (user_id, city, zone) VALUES (?, ?, ?)",
        (user_id, city, zone),
    )
    conn.commit()
    conn.close()


class OrefPredictorBot:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not self.token:
            raise ValueError("TELEGRAM_BOT_TOKEN is missing from .env")

        self.predictor = Predictor()
        self.streamer = AlertStreamer(on_alert_callback=self.process_live_alert)

        # Persistent subscriptions
        _init_db()
        self.user_subscriptions: dict[int, dict] = _load_subscriptions()
        logger.info(f"Loaded {len(self.user_subscriptions)} saved subscriptions.")

        # Zone cache
        self.zones: list[str] = []

        self.application = (
            Application.builder()
            .token(self.token)
            .post_init(self._on_boot)
            .build()
        )
        self._register_handlers()

    # ── Lifecycle ──────────────────────────────────────────────────
    async def _on_boot(self, application: Application) -> None:
        self.zones = await city_api.get_all_zones()
        logger.info(f"Loaded {len(self.zones)} zones from API")
        application.create_task(self.streamer.start())

    def _subscribe(self, user_id: int, city: str, zone: str):
        self.user_subscriptions[user_id] = {"city": city, "zone": zone}
        _save_subscription(user_id, city, zone)

    def _register_handlers(self) -> None:
        self.application.add_handler(CommandHandler("start", self.cmd_start))
        self.application.add_handler(CommandHandler("select", self.cmd_select))
        self.application.add_handler(CommandHandler("status", self.cmd_status))
        self.application.add_handler(CommandHandler("stats", self.cmd_stats))
        self.application.add_handler(CommandHandler("mock", self.cmd_mock))

        self.application.add_handler(CallbackQueryHandler(self.cb_zone_page, pattern=r"^zp:"))
        self.application.add_handler(CallbackQueryHandler(self.cb_pick_zone, pattern=r"^zn:"))
        self.application.add_handler(CallbackQueryHandler(self.cb_city_page, pattern=r"^cp:"))
        self.application.add_handler(CallbackQueryHandler(self.cb_pick_city, pattern=r"^ct:"))
        self.application.add_handler(CallbackQueryHandler(self.cb_back_zones, pattern=r"^back_zones$"))
        self.application.add_handler(CallbackQueryHandler(self.cb_noop, pattern=r"^noop$"))

        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))

    # ── Keyboard builders ──────────────────────────────────────────
    def _zone_keyboard(self, page: int = 0) -> InlineKeyboardMarkup:
        zones = self.zones
        total_pages = max(1, math.ceil(len(zones) / REGIONS_PER_PAGE))
        page = max(0, min(page, total_pages - 1))
        start = page * REGIONS_PER_PAGE
        page_zones = zones[start: start + REGIONS_PER_PAGE]

        rows: list[list[InlineKeyboardButton]] = []
        for i in range(0, len(page_zones), 2):
            row = []
            for z in page_zones[i: i + 2]:
                row.append(InlineKeyboardButton(z, callback_data=f"zn:{z}"))
            rows.append(row)

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
        rows: list[list[InlineKeyboardButton]] = []
        for c in cities:
            name = c["name"]
            rows.append([InlineKeyboardButton(name, callback_data=f"ct:{name}")])

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
        rows = []
        for c in cities:
            name = c.get("name", "")
            zone = c.get("zone", "")
            label = f"{name}  ({zone})" if zone else name
            rows.append([InlineKeyboardButton(label, callback_data=f"ct:{name}")])
        return InlineKeyboardMarkup(rows)

    # ── Commands ───────────────────────────────────────────────────
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        start_date = self.predictor.start_date_text()
        n_events = self.predictor.total_events()

        await update.message.reply_text(
            "🛡️ *ברוכים הבאים למערכת ניתוח התרעות פיקוד העורף*\n"
            "\n"
            "המערכת מנתחת נתונים היסטוריים ומחשבת:\n"
            "מה הסיכוי שאחרי *התרעה מוקדמת* תהיה *אזעקה* בעיר שלכם?\n"
            "\n"
            "כשמגיעה התרעה חיה, החישוב מתחשב\n"
            "ב*פוליגון הספציפי* של ההתרעה הנוכחית.\n"
            "\n"
            f"📅 מבוסס על נתונים החל מהתאריך {start_date}\n"
            f"📊 {n_events} אירועי התרעה מנותחים\n"
            "\n"
            "*שתי דרכים לבחור עיר:*\n"
            "🔹 *הקלידו שם עיר* — חיפוש חופשי\n"
            "🔹 /select — בחירה מהרשימה (אזור → עיר)\n"
            "\n"
            "🔹 /status — הסיכוי לעיר שבחרתם\n"
            "🔹 /stats — סטטיסטיקות ערים נבחרות\n"
            #"🔹 /mock — בדיקת התרעה מדומה\n"
            "\n"
            "⚠️ *שימו לב:* הבוט מספק הערכה סטטיסטית בלבד "
            "על בסיס נתוני העבר, ואינו מהווה תחליף להנחיות "
            "פיקוד העורף. תמיד היכנסו למרחב המוגן בעת אזעקה.",
            parse_mode="Markdown",
        )

    async def cmd_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.zones:
            self.zones = await city_api.get_all_zones()
        await update.message.reply_text(
            "📍 *שלב 1: בחרו אזור*",
            reply_markup=self._zone_keyboard(0),
            parse_mode="Markdown",
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.message.from_user.id
        sub = self.user_subscriptions.get(user_id)
        if not sub:
            await update.message.reply_text("⚠️ לא בחרתם עיר עדיין.\nלחצו /select או הקלידו שם עיר.")
            return
        msg = self.predictor.format_status(sub["city"])
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sample_cities = [
            "תל אביב - מרכז העיר", "ירושלים - מרכז", "חיפה - מערב",
            "באר שבע - צפון", "רמת גן - מערב", "פתח תקווה",
            "נתניה - מערב", "קריית שמונה", "אילת",
        ]
        lines = ["📊 *סיכויי אזעקה – ערים נבחרות (בהינתן התרעה מוקדמת): *\n"]
        for city in sample_cities:
            pred = self.predictor.predict(city)
            if pred["probability"] is not None:
                _, em = self.predictor._risk(pred["probability"])
                lines.append(f"{em} {city}: *{pred['probability']}%*")
        lines.append(f"\n🔄 עדכון אחרון: {self.predictor.last_updated()}")
        lines.append("\nהקלידו שם עיר או לחצו /select לראות את העיר שלכם")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_mock(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.message.from_user.id
        sub = self.user_subscriptions.get(user_id)
        if not sub:
            await update.message.reply_text("⚠️ לא בחרתם עיר. לחצו /select תחילה.")
            return
        await update.message.reply_text("🛠️ מדמה התרעה מוקדמת...")
        zone = sub.get("zone", "")
        # Build a mock polygon of all cities in the user's zone
        zone_cities = await city_api.get_cities_by_zone(zone, limit=500) if zone else []
        polygon_city_names = [c["name"] for c in zone_cities]
        mock_payload = {
            "type": "early_warning",
            "cities": polygon_city_names,
        }
        await self.process_live_alert(mock_payload)

    # ── Free-text city search ──────────────────────────────────────
    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.message.text.strip()
        if len(query) < 2:
            return

        results = await city_api.search_cities(query, limit=8)
        if not results:
            await update.message.reply_text(
                f"🔍 לא נמצאו תוצאות עבור *{query}*\nנסו /select לבחירה מהרשימה.",
                parse_mode="Markdown",
            )
            return

        if len(results) == 1:
            city = results[0]["name"]
            zone = results[0].get("zone", "")
            user_id = update.message.from_user.id
            self._subscribe(user_id, city, zone)
            msg = self.predictor.format_status(city)
            await update.message.reply_text(f"✅ *נרשמתם ל: {city}*\n\n{msg}", parse_mode="Markdown")
            return

        keyboard = self._search_keyboard(results)
        await update.message.reply_text(
            f"🔍 נמצאו {len(results)} תוצאות עבור *{query}*:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    # ── Callback handlers ──────────────────────────────────────────
    async def cb_zone_page(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        page = int(q.data.replace("zp:", ""))
        await q.edit_message_reply_markup(reply_markup=self._zone_keyboard(page))

    async def cb_pick_zone(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer("טוען ערים...")
        zone = q.data.replace("zn:", "")

        cities = await city_api.get_cities_by_zone(zone, limit=500)
        if not cities:
            await q.edit_message_text(f"⚠️ לא נמצאו ערים באזור {zone}")
            return

        context.user_data["zone_cities"] = cities
        context.user_data["current_zone"] = zone

        total_pages = max(1, math.ceil(len(cities) / CITIES_PER_PAGE))
        page_cities = cities[:CITIES_PER_PAGE]

        await q.edit_message_text(
            f"📍 *שלב 2: בחרו עיר באזור {zone}*\n({len(cities)} ערים)",
            reply_markup=self._cities_keyboard(page_cities, zone, 0, total_pages),
            parse_mode="Markdown",
        )

    async def cb_city_page(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        parts = q.data.replace("cp:", "").split("|")
        zone = parts[0]
        page = int(parts[1])

        cities = context.user_data.get("zone_cities", [])
        if not cities:
            cities = await city_api.get_cities_by_zone(zone, limit=500)
            context.user_data["zone_cities"] = cities

        total_pages = max(1, math.ceil(len(cities) / CITIES_PER_PAGE))
        page = max(0, min(page, total_pages - 1))
        start = page * CITIES_PER_PAGE
        page_cities = cities[start: start + CITIES_PER_PAGE]

        await q.edit_message_reply_markup(
            reply_markup=self._cities_keyboard(page_cities, zone, page, total_pages)
        )

    async def cb_pick_city(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        city = q.data.replace("ct:", "")
        user_id = q.from_user.id

        zone = context.user_data.get("current_zone", "")
        if not zone:
            results = await city_api.search_cities(city, limit=1)
            if results:
                zone = results[0].get("zone", "")

        self._subscribe(user_id, city, zone)

        msg = self.predictor.format_status(city)
        await q.edit_message_text(
            f"✅ *נרשמתם ל: {city}*\n\n{msg}\n\n"
            "תקבלו התראה כשתהיה התרעה מוקדמת באזור שלכם.\n"
            "ניתן ללחוץ /start בכל עת.",
            parse_mode="Markdown",
        )

    async def cb_back_zones(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        await q.edit_message_text(
            "📍 *שלב 1: בחרו אזור*",
            reply_markup=self._zone_keyboard(0),
            parse_mode="Markdown",
        )

    async def cb_noop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.callback_query.answer()

    # ── Live alert processing ──────────────────────────────────────
    async def process_live_alert(self, alert_data: dict) -> None:
        alert_type = alert_data.get("type", "")
        if alert_type not in ("system", "early_warning"):
            return

        # The WebSocket payload has "cities" as a list of city name strings
        polygon_cities = alert_data.get("cities", [])
        if isinstance(polygon_cities, str):
            polygon_cities = [c.strip() for c in polygon_cities.split(",")]
        # Normalize: could be list of strings or list of dicts
        normalized = []
        for c in polygon_cities:
            if isinstance(c, dict):
                normalized.append(c.get("name", ""))
            else:
                normalized.append(str(c).strip())
        polygon_cities = [c for c in normalized if c]

        if not polygon_cities:
            return

        polygon_set = set(polygon_cities)
        logger.info(f"🚨 Early warning – {len(polygon_cities)} cities in polygon")

        for user_id, sub in self.user_subscriptions.items():
            user_city = sub.get("city", "")

            if user_city not in polygon_set:
                continue

            try:
                msg = self.predictor.format_live_warning(user_city, polygon_cities)
                await self.application.bot.send_message(
                    chat_id=user_id, text=msg, parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed to notify user {user_id}: {e}")

    # ── Run ────────────────────────────────────────────────────────
    def run(self) -> None:
        logger.info("Bot starting...")
        self.application.run_polling()


if __name__ == "__main__":
    bot = OrefPredictorBot()
    bot.run()
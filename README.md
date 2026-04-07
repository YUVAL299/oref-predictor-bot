# Oref Predictor Bot

A Telegram bot that predicts the probability of an alarm following an early warning from the IDF Home Front Command (Pikud HaOref).

## How It Works

When the Home Front Command issues an early warning, it covers a polygon of cities. The bot:

1. **Fits an ellipse** to the warned cities' geographic coordinates
2. **Computes the Mahalanobis distance** of the user's city from the ellipse center — accounting for the shape and orientation of the warning zone
3. **Uses a Random Forest model** (trained on historical data) to predict the probability of an actual alarm, combining:
   - Distance from ellipse center (most predictive feature)
   - City's historical hit rate
   - Polygon size, shape, and spread
   - Time of day
   - And more (12 features total)

Key insight from the data: cities at the **center** of the early warning ellipse were hit ~65% of the time, while cities on the **edge** were hit only ~15%.

## Architecture

```
src/
├── config.py              # Centralized settings and constants
├── bot.py                 # Telegram bot (OrefPredictorBot)
├── predictor.py           # ML prediction engine (Predictor)
├── ellipse.py             # Gaussian ellipse fitting (EllipseFitter)
├── city_api.py            # RedAlert Cities API client (CityAPI)
├── alert_streamer.py      # WebSocket real-time alerts (AlertStreamer)
├── subscription_store.py  # SQLite user subscriptions (SubscriptionStore)
└── data_scraper.py        # Historical data pipeline (DataScraper)
```

## Setup

```bash
# Clone and setup
git clone https://github.com/YOUR_USERNAME/oref-predictor-bot.git
cd oref-predictor-bot
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your TELEGRAM_BOT_TOKEN and REDALERT_API_KEY

# Fetch historical data
python -m src.data_scraper

# Run the bot
python main.py
```

## Deployment (Oracle Cloud Free Tier)

```bash
# On the server
sudo nano /etc/systemd/system/oref-bot.service
# Paste the service config, then:
sudo systemctl daemon-reload
sudo systemctl enable oref-bot
sudo systemctl start oref-bot

# Auto-refresh data every 6 hours
crontab -e
# Add: 0 */6 * * * cd /home/ubuntu/oref-predictor-bot && venv/bin/python -m src.data_scraper && rm -f alert_model.pkl
```

## Tools

```bash
python -m tools.check_city "רמת גן - מערב"       # Check city stats
python -m tools.debug_city "רמת גן - מערב"       # Debug event timeline
python -m src.data_scraper --local                 # Rebuild from cache (no API call)
```

## Tech Stack

- **Python 3.12+**
- **python-telegram-bot** — Telegram Bot API
- **scikit-learn** — Random Forest classifier
- **NumPy** — Ellipse fitting and Mahalanobis distance
- **aiohttp** — Async HTTP client for RedAlert API
- **python-socketio** — WebSocket for real-time alerts
- **SQLite** — User subscription persistence
- **Oracle Cloud** — Free tier VM hosting

## Data Source

All alert data is sourced from the [RedAlert API](https://redalert.orielhaim.com), which provides real-time and historical data from the IDF Home Front Command.

## Disclaimer

This bot provides **statistical estimates only** and is not a substitute for official Home Front Command instructions. **Always enter a protected space when an alarm sounds**, regardless of the bot's prediction.

## Author

© Yuval Lichtman 2026

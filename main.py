"""Entry point for the Oref Predictor Bot."""

import logging

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

from src.bot import OrefPredictorBot

if __name__ == "__main__":
    bot = OrefPredictorBot()
    bot.run()
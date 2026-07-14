"""Cron entrypoint for the Telegram PDF-ingest bot.

Poll Telegram once, file any authorized PDF/photo into Paperless. Runs on a
1-minute cron (see crontab), same shape as gajana's run_telegram_bot.py.
"""

from __future__ import annotations

import json
import logging
import os

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("telegram_bot")

from plugins.telegram_bot.bot import TelegramDocBot, _load_token  # noqa: E402
from src.constants import STATE_DIR  # noqa: E402

SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "plugins", "telegram_bot", "settings.json")
STATE_PATH = os.path.join(STATE_DIR, "telegram_bot_state.json")


def main() -> None:
    # Unconfigured is a clean no-op, not an error: the cron can ship before the
    # bot is set up without spamming failures.
    if not os.path.exists(SETTINGS_PATH):
        logger.info(f"{SETTINGS_PATH} not found; telegram bot not configured yet.")
        return
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        settings = json.load(f)

    token = _load_token()
    if not token:
        logger.info("No Telegram bot token yet (secrets/telegram.json); skipping.")
        return

    bot = TelegramDocBot(settings, token, STATE_PATH)
    filed = bot.run_once()
    if filed:
        logger.info(f"Telegram bot filed {filed} document(s).")


if __name__ == "__main__":
    main()

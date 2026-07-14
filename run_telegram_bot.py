"""Cron entrypoint for the Telegram PDF-ingest + Q&A bot.

Poll Telegram once, file any authorized PDF/photo into Paperless (or answer a
question over health.db). Runs on a 1-minute cron (see crontab), same shape as
gajana's run_telegram_bot.py.

get_updates() now long-polls (see plugins.telegram_bot.bot.LONG_POLL_SECONDS),
so a single invocation can legitimately run most of the 60s tick answering a
message, instead of returning in well under a second like a short poll does.
If a tick ever runs long (a slow LLM round-trip, a network hiccup), the NEXT
cron tick must not start a second instance on top of it -- both would share
one offset in telegram_bot_state.json and could double-process or double-reply
to the same message. A non-blocking file lock makes that tick a clean skip
instead of a race.
"""

from __future__ import annotations

import fcntl
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
LOCK_PATH = os.path.join(STATE_DIR, "telegram_bot.lock")


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

    os.makedirs(STATE_DIR, exist_ok=True)
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # The previous tick's long poll is still running -- skip, don't queue
        # behind it. It already holds the current offset; a second instance
        # here would race it, not help it.
        logger.info("Previous telegram_bot tick still running; skipping this one.")
        return

    try:
        bot = TelegramDocBot(settings, token, STATE_PATH)
        filed = bot.run_once()
        if filed:
            logger.info(f"Telegram bot filed {filed} document(s).")
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


if __name__ == "__main__":
    main()

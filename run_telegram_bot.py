"""Cron entrypoint for the chat-bridged PDF-ingest + Q&A bot.

Registers the ``medical`` channel on the host chat bridge Unix socket
(``/run/agentic/chat.sock``), pulls pre-authorized events via long polling
(see plugins.telegram_bot.bot.LONG_POLL_SECONDS), files any PDF/photo into
Drive + Paperless (or answers a question over health.db), and acknowledges each
event after processing. Runs on a 1-minute cron (see crontab).

A non-blocking file lock makes overlapping cron ticks skip cleanly instead of
racing on ``telegram_bot_state.json``'s ``last_seq``.
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

from plugins.telegram_bot.bot import TelegramDocBot  # noqa: E402
from src.chat_client import ChatGatewayUnavailableError, ChatSocketError  # noqa: E402
from src.constants import STATE_DIR  # noqa: E402

SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "plugins", "telegram_bot", "settings.json")
STATE_PATH = os.path.join(STATE_DIR, "telegram_bot_state.json")
LOCK_PATH = os.path.join(STATE_DIR, "telegram_bot.lock")


def main() -> None:
    settings: dict = {}
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            settings = json.load(f)

    if not settings.get("enabled", True):
        logger.info("Chat bot disabled in settings; skipping.")
        return

    os.makedirs(STATE_DIR, exist_ok=True)
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # The previous tick's long poll is still running -- skip, don't queue
        # behind it. A second instance here would race on last_seq.
        logger.info("Previous telegram_bot tick still running; skipping this one.")
        lock_file.close()
        return

    try:
        bot = TelegramDocBot(settings, STATE_PATH)
        filed = bot.run_once()
        if filed:
            logger.info(f"Chat bot filed {filed} document(s).")
    except ChatGatewayUnavailableError as e:
        logger.warning(f"Chat bridge unavailable; skipping tick: {e}")
    except ChatSocketError as e:
        logger.error(f"Chat bridge error: {e}")
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


if __name__ == "__main__":
    main()

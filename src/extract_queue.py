"""FIFO queue for on-demand extraction after a Telegram-filed document.

Filing (Drive write + Paperless upload) happens synchronously in the bot's
Telegram tick and is fast. Extraction is an LLM call -- 10-30s+, more under
free-tier pacing -- and would blow the tick's ~60s budget, so it is queued
here instead and drained on its own cron tick (run_extract_queue.py). See
docs/telegram_ingest_queue.md for the full design, including why a document
can never be marked done if Paperless is unreachable.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from typing import Any

from src.constants import STATE_DIR

logger = logging.getLogger(__name__)

QUEUE_PATH = os.path.join(STATE_DIR, "pending_extract.json")


def load() -> list[dict[str, Any]]:
    if not os.path.exists(QUEUE_PATH):
        return []
    try:
        with open(QUEUE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Bad extract queue file; starting fresh: {e}")
        return []


def save(items: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(QUEUE_PATH) or ".", exist_ok=True)
    with open(QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)


def enqueue(
    *,
    rel: str,
    correspondent: str,
    tag: str,
    title: str,
    date: str,
    chat_id: int,
) -> None:
    """Append one filed document to the queue, to be extracted on the next
    run_extract_queue.py tick."""
    item = {
        "rel": rel,
        "correspondent": correspondent,
        "tag": tag,
        "title": title,
        "date": date,
        "chat_id": chat_id,
        "queued_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "attempts": 0,
    }
    items = load()
    items.append(item)
    save(items)

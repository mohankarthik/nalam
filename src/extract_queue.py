"""FIFO queue for on-demand extraction after a Telegram-filed document.

Filing (Drive write + Paperless upload) happens synchronously in the bot's
Telegram tick and is fast. Extraction is an LLM call -- 10-30s+, more under
free-tier pacing -- and would blow the tick's ~60s budget, so it is queued
here instead and drained on its own cron tick (run_extract_queue.py). See
docs/telegram_ingest_queue.md for the full design, including why a document
can never be marked done if Paperless is unreachable.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import logging
import os
from typing import Any, Iterator

from src.constants import STATE_DIR

logger = logging.getLogger(__name__)

QUEUE_PATH = os.path.join(STATE_DIR, "pending_extract.json")
# Guards every read-modify-write of the queue file. The bot's enqueue() and the
# drain's final write-back run in DIFFERENT processes; without a shared lock a
# document filed mid-drain is clobbered when the drain writes back its snapshot.
_LOCK_PATH = QUEUE_PATH + ".lock"


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    os.makedirs(os.path.dirname(_LOCK_PATH) or ".", exist_ok=True)
    fd = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def _item_key(item: dict[str, Any]) -> str:
    """Identity of a queue item across a load/save round-trip. queued_at is an
    ISO timestamp with microseconds, so rel+queued_at is effectively unique."""
    return f"{item.get('rel')}|{item.get('queued_at')}"


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
    # Write-then-rename so a crash/disk-full mid-write can never leave a torn file
    # that load() would read as empty and then overwrite -- silently losing the
    # whole backlog. os.replace() is atomic within a filesystem.
    os.makedirs(os.path.dirname(QUEUE_PATH) or ".", exist_ok=True)
    tmp = QUEUE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, QUEUE_PATH)


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
    with _locked():
        items = load()
        items.append(item)
        save(items)


def commit_drain(snapshot: list[dict[str, Any]], remaining: list[dict[str, Any]]) -> None:
    """Write back the queue after a drain, preserving anything enqueued meanwhile.

    A drain loads a `snapshot`, spends tens of seconds extracting, then wants to
    write back only the items still pending (`remaining`). Overwriting the file
    with `remaining` would drop any document the bot enqueued during that window.
    So, under the lock, re-read the live queue and keep: (a) `remaining` -- the
    snapshot items still pending, carrying their bumped attempt counts -- plus
    (b) every live item that was NOT in the snapshot, i.e. filed mid-drain.
    """
    snap_keys = {_item_key(i) for i in snapshot}
    with _locked():
        live = load()
        appended = [i for i in live if _item_key(i) not in snap_keys]
        save(remaining + appended)

"""Cron entrypoint: drain the on-demand extraction queue.

A Telegram-filed document is written to Drive and uploaded to Paperless
immediately (plugins/telegram_bot/bot.py), but extraction is an LLM call
(10-30s+) and would blow the bot's ~60s tick budget -- so filing enqueues the
document (src/extract_queue.py) instead of extracting inline, and this script
drains that queue on its own cron tick.

Two distinct failure modes, handled differently -- see docs/telegram_ingest_queue.md
for the full design and why the distinction matters:

  Paperless unreachable       -> skip the WHOLE tick, queue left untouched,
                                  retried forever (never counted against any
                                  item's retry budget), push a DOWN heartbeat
                                  so a human is paged. An outage must never be
                                  papered over by extracting without a chance
                                  at independent corroboration.
  Paperless up, but this      -> wait per item, up to OCR_WAIT_MINUTES, then
  doc's OCR isn't ready yet      fall back to text-layer-only extraction --
                                  the same fallback ingest_lab/etc already use
                                  for scanned image PDFs. Uncorroborated
                                  results still land in review, never
                                  auto-committed (existing rule, not new).

A document is only ever popped from the queue after ingest_document() actually
returns (a commit or a review-queue write -- both are real DB writes) with
Paperless confirmed reachable at the time. Nothing is ever marked done because
a timer expired.
"""

from __future__ import annotations

import datetime
import fcntl
import logging
import os

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("extract_queue")

from plugins.telegram_bot.bot import _load_token, send_message  # noqa: E402
from src import db, extract_queue, monitor  # noqa: E402
from src.constants import STATE_DIR  # noqa: E402
from src.drive_sync import Doc, _key  # noqa: E402
from src.ingest import ingest_document  # noqa: E402
from src.paperless import Paperless, id_for, ocr_for  # noqa: E402
from src.people import source_path  # noqa: E402

LOCK_PATH = os.path.join(STATE_DIR, "extract_queue.lock")

# Paperless usually OCRs a freshly-consumed document within low minutes. This
# is generous headroom before falling back to text-layer-only extraction --
# see the module docstring for why that fallback is still safe.
OCR_WAIT_MINUTES = 30

# ingest_document() failing 3 times in a row (a Gemini error, a bad response --
# not a Paperless problem, that's handled separately above) means retrying
# forever would rot silently. Pop it and tell the person who sent it instead.
MAX_ATTEMPTS = 3


def _doc_from_item(item: dict) -> Doc:
    path = source_path(item["rel"])
    return Doc(
        path=path,
        rel=item["rel"],
        person=item["correspondent"],
        correspondent=item["correspondent"],
        tag=item["tag"],
        title=item["title"],
        created=item["date"],
        suffix=os.path.splitext(item["rel"])[1],
        key=_key(path, item["rel"]),
    )


def _result_text(result: dict) -> str:
    # .get() throughout, deliberately: this formats whatever ingest_document()
    # handed back, in a process that must never crash mid-tick and strand the
    # item un-popped (see ingest_document()'s classify()-fallback comment for
    # the bug this guards against -- a doc_type string with no matching keys).
    doc_type = result.get("doc_type")
    if doc_type == "lab":
        return (
            f"Extracted: {result.get('committed', 0)} observations, "
            f"{result.get('review', 0)} to review."
        )
    if doc_type in ("discharge", "prescription") and "medications" in result:
        note = (
            f" (document names {result['misfiled']} -- filed there)"
            if result.get("misfiled")
            else ""
        )
        if doc_type == "discharge":
            return (
                f"Extracted: {result.get('medications', 0)} medications, "
                f"{result.get('encounters', 0)} encounter{note}."
            )
        return (
            f"Extracted: {result.get('medications', 0)} medications, "
            f"{result.get('uncorroborated', 0)} not corroborated (-> review){note}."
        )
    if doc_type == "radiology":
        if result.get("reports"):
            note = (
                f" (document names {result['misfiled']} -- filed there)"
                if result.get("misfiled")
                else ""
            )
            return f"Extracted: imaging report filed{note}."
        if result.get("unreadable"):
            return "Imaging report could not be read (no text layer or OCR) -- sent to review."
        return "Imaging report -- sent to review."
    if doc_type == "encrypted":
        return "Not extracted: password-protected PDF."
    if doc_type == "unsupported":
        return "Not extracted: not a PDF."
    return f"Not extracted: no extractor for {doc_type or 'this'} documents yet."


def run_once() -> None:
    up, msg = monitor.check_paperless()
    monitor.push_paperless(up, msg)
    if not up:
        logger.warning(f"Paperless unreachable ({msg}); leaving the extraction queue untouched.")
        return

    items = extract_queue.load()
    if not items:
        return

    token = _load_token()
    paperless = Paperless()
    ocr_index = paperless.ocr_index()
    paperless_ids = paperless.document_id_index()
    con = db.connect()

    now = datetime.datetime.now(datetime.timezone.utc)
    remaining = []
    for item in items:
        # The whole body is guarded, not just ingest_document: a malformed or
        # legacy item (missing key, naive queued_at) that threw during _doc_from_item
        # or the age math used to escape the loop before commit_drain ran, leaving
        # the poison item at the head to re-crash every tick and wedge the queue.
        # Now it is retried and, past MAX_ATTEMPTS, dropped like any other failure.
        try:
            doc = _doc_from_item(item)
            ocr_text = ocr_for(ocr_index, doc.correspondent, doc.rel)
            queued_at = datetime.datetime.fromisoformat(item["queued_at"])
            age_minutes = (now - queued_at).total_seconds() / 60

            if ocr_text is None and age_minutes < OCR_WAIT_MINUTES:
                remaining.append(item)  # Paperless is up; just give OCR more time
                continue
            if ocr_text is None:
                logger.warning(
                    f"{doc.rel}: Paperless never OCR'd this document in {OCR_WAIT_MINUTES}m "
                    "(confirmed reachable throughout); extracting with text-layer only."
                )

            result = ingest_document(
                con,
                doc,
                ocr_text=ocr_text,
                paperless_id=id_for(paperless_ids, doc.correspondent, doc.rel),
            )
        except Exception as e:
            item["attempts"] = item.get("attempts", 0) + 1
            logger.error(
                f"{item.get('rel')}: extraction failed (attempt {item['attempts']}): {e}",
                exc_info=True,
            )
            chat_id = item.get("chat_id")
            if item["attempts"] >= MAX_ATTEMPTS:
                if chat_id is not None:
                    send_message(
                        token,
                        chat_id,
                        f"✗ Extraction failed for {item.get('title')} after "
                        f"{MAX_ATTEMPTS} attempts: {e}",
                    )
            else:
                remaining.append(item)
            continue

        send_message(token, item["chat_id"], _result_text(result))

    # Preserve anything the bot enqueued while this drain was extracting.
    extract_queue.commit_drain(items, remaining)


def main() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Extraction can run long (LLM round-trips) -- a second tick starting
        # on top of it would double-process items from the shared queue file.
        logger.info("Previous extract_queue tick still running; skipping this one.")
        return
    try:
        run_once()
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


if __name__ == "__main__":
    main()

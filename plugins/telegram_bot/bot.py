"""Chat-bridged document-ingest and Q&A bot for nalam.

Cron-polled via the host unified chat bridge Unix socket
(``/run/agentic/chat.sock``, channel ``"medical"``), same shape as gajana's
bot: each run pulls pre-authorized events via long polling, and any PDF/photo
whose caption parses cleanly is filed onto the Drive mount in the exact place
drive_sync.py expects to find it -- then uploaded to Paperless immediately
(not left for the 6-hourly cron), and marked synced so that cron doesn't
double-post it.

Host ``chat-bridge`` owns bot credentials, ``allowed_chat_id``, and
``allowed_users`` and spools inbound attachments before publishing events.

This matters: Phase-1 extraction (run_extract.py) reads ONLY from the Drive
mount (src.drive_sync.collect()), never from Paperless. A document that
reaches Paperless without ever touching Drive is archived and searchable but
never turns into rows in health.db. So this bot writes the file to Drive
first -- everything downstream (Paperless filing, doc_type classification,
extraction) is then the same pipeline every other document goes through.

Caption format, pipe-delimited::

    Name | Type | Date | Title

* Name  -- required. Resolved via ``people.resolve()`` against the aliases in
  data/people.json. No fuzzy matching: an unresolved name is rejected, never
  guessed, because a document filed against the wrong person is the worst
  failure this system can have (see CLAUDE.md, "the one rule that matters").
* Type  -- optional. A key in data/specialties.json's tag map (case
  insensitive) -- the SPECIALTY SUBFOLDER a Drive-sourced document would sit
  under (Reports, Pulmonary, Dermatology, ...), not the lab/prescription/
  radiology/discharge content category: that is auto-classified from the
  document's own text by run_extract.py's classifier, same as every other
  document, so it is never asked for here. Given but unrecognized -> rejected
  (typo protection); omitted -> filed directly under the person (no
  subfolder), same as an unclassified Drive drop.
* Date  -- optional YYYY-MM-DD, document date. Omitted -> today. Future dates
  are rejected (can't be a document date).
* Title -- optional free text. Omitted -> the Type token, or "General".
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from typing import Any, Optional

from src import extract_queue, qa
from src.chat_client import (
    ChatGatewayUnavailableError,
    ChatSocketClient,
    ChatSocketError,
)
from src.constants import CONSUMABLE_EXT, DOCUMENT_TYPE, MAGIC, SPECIALTIES_CONFIG
from src.drive_sync import _key, load_state, save_state
from src.paperless import Paperless
from src.people import resolve, source_path

logger = logging.getLogger(__name__)

CHANNEL = "medical"

# Long polling: chat-bridge holds pull_events open server-side for up to this
# many seconds, returning the instant an event arrives instead of an empty
# result -- so a message sent mid-tick gets answered in ~seconds, not up to a
# minute later on the NEXT cron tick. Kept comfortably under the 1-minute cron
# interval (see crontab) so a tick reliably finishes before the next one fires.
LONG_POLL_SECONDS = 45

_MIME_TO_EXT = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
}


def _load_specialties() -> tuple[dict[str, str], str]:
    with open(SPECIALTIES_CONFIG, encoding="utf-8") as f:
        raw = json.load(f)
    return raw["tags"], raw["default_tag"]


class ParsedCaption:
    def __init__(
        self,
        folder: str,
        correspondent: str,
        tag: str,
        subfolder: Optional[str],
        date: str,
        title: str,
    ) -> None:
        self.folder = folder  # the person's Drive folder name
        self.correspondent = correspondent  # Paperless correspondent = patient
        self.tag = tag
        self.subfolder = subfolder  # specialty subfolder, or None for the person's root
        self.date = date
        self.title = title


class CaptionError(ValueError):
    """Caption failed validation. Message is safe to send back to the user."""


def parse_caption(caption: str, tag_map: dict[str, str], default_tag: str) -> ParsedCaption:
    """Parse and validate 'Name | Type | Date | Title'. Raises CaptionError."""
    parts = [p.strip() for p in (caption or "").split("|")]
    if not parts or not parts[0]:
        raise CaptionError(
            "No name. Format: Name | Type | Date | Title (e.g. Dad | Reports | 2026-07-10)."
        )
    name = parts[0]
    person = resolve(name)
    if person is None:
        raise CaptionError(f"Unknown name {name!r}. Send /help for valid names.")

    type_token = parts[1].strip() if len(parts) > 1 and parts[1].strip() else ""
    subfolder: Optional[str] = None
    if type_token:
        match = next((k for k in tag_map if k.lower() == type_token.lower()), None)
        if match is None:
            raise CaptionError(f"Unknown type {type_token!r}. Send /help for valid types.")
        subfolder, tag = match, tag_map[match]
    else:
        tag = default_tag

    date_token = parts[2].strip() if len(parts) > 2 and parts[2].strip() else ""
    if date_token:
        try:
            date = datetime.date.fromisoformat(date_token)
        except ValueError:
            raise CaptionError(f"Bad date {date_token!r}. Use YYYY-MM-DD.")
        if date > datetime.date.today():
            raise CaptionError(f"Date {date_token} is in the future.")
        date_iso = date.isoformat()
    else:
        date_iso = datetime.date.today().isoformat()

    title = parts[3].strip() if len(parts) > 3 and parts[3].strip() else (type_token or "General")

    return ParsedCaption(
        folder=person.folder,
        correspondent=person.correspondent,
        tag=tag,
        subfolder=subfolder,
        date=date_iso,
        title=title,
    )


def _suffix(filename: Optional[str], content: bytes) -> Optional[str]:
    """The file's real extension, or None if Paperless can't consume it.

    Same logic as drive_sync.sniff(), but on in-memory bytes -- no re-download.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in CONSUMABLE_EXT:
        return ext
    for magic, suffix in MAGIC.items():
        if content.startswith(magic):
            return suffix
    return None


def _candidate_suffix(attachment: dict[str, Any]) -> Optional[str]:
    """Best-effort extension from event attachment metadata for pre-fetch collision checks."""
    filename = attachment.get("filename") or ""
    ext = os.path.splitext(filename)[1].lower()
    if ext in CONSUMABLE_EXT:
        return ext
    mime = str(attachment.get("mime_type") or "").lower().split(";")[0].strip()
    return _MIME_TO_EXT.get(mime)


# Telegram rejects any sendMessage over 4096 characters with a 400 -- and the
# advisory path gathers broadly and can easily exceed this (a long answer plus
# dozens of Source links), so replies are split into chunks under this limit. A
# margin below 4096 leaves room and avoids off-by-one surprises around
# multi-byte characters.
TELEGRAM_MAX_CHARS = 3900


def _chunk(text: str, limit: int = TELEGRAM_MAX_CHARS) -> list[str]:
    """Split `text` into pieces no longer than `limit`, preferring line boundaries so a
    question or a Source link is never cut mid-token. A single line longer than `limit`
    (rare) is hard-split as a last resort."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # one enormous line: hard-split it
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


def send_message(
    text: str,
    chat_client: Optional[ChatSocketClient] = None,
    *,
    reply_to: Optional[str | int] = None,
) -> bool:
    """Send plain text to the medical channel via ChatSocketClient, chunked under
    TELEGRAM_MAX_CHARS.

    Never raises on ChatSocketError or OSError: logs a warning and returns False.
    In run_extract_queue.py, ingest_document() commits to health.db BEFORE calling
    send_message() and extract_queue.commit_drain() -- raising here would skip
    commit_drain() and re-ingest the same document on every subsequent cron tick.

    A free function, not just a method, so run_extract_queue.py's follow-up
    messages (sent from a separate cron tick, with no TelegramDocBot instance
    around) can reuse it too.
    """
    client = chat_client if chat_client is not None else ChatSocketClient()
    for piece in _chunk(text):
        try:
            if reply_to is not None:
                client.send_message(CHANNEL, piece, reply_to=reply_to)
            else:
                client.send_message(CHANNEL, piece)
        except (ChatSocketError, OSError) as e:
            logger.warning(f"Failed to send chat bridge reply: {e}")
            return False
    return True


class TelegramDocBot:
    def __init__(
        self,
        settings: dict[str, Any],
        state_path: str,
        chat_client: Optional[ChatSocketClient] = None,
        paperless: Optional[Paperless] = None,
    ) -> None:
        self.settings = settings
        self.transport = str(settings.get("transport") or "telegram")
        self.state_path = state_path
        self.chat_client = chat_client if chat_client is not None else ChatSocketClient()
        self.state = self._load_state()
        self.paperless = paperless if paperless is not None else Paperless()

    def send_message(self, text: str, reply_to: Optional[str | int] = None) -> bool:
        client = getattr(self, "chat_client", None)
        return send_message(text, client, reply_to=reply_to)

    # --- State ---------------------------------------------------------------
    def _load_state(self) -> dict[str, Any]:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                logger.warning("Bad telegram state file; starting fresh.")
        return {}

    def _save_state(self) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)

    # --- Help ------------------------------------------------------------------
    def _help_text(self, tag_map: dict[str, str]) -> str:
        from src.people import load_people

        names = sorted(
            {p.correspondent for p in load_people().values()}
            | {a for p in load_people().values() for a in p.aliases}
        )
        types = sorted(tag_map.keys())
        return (
            "Send a PDF or photo with a caption: Name | Type | Date | Title\n"
            "Only Name is required. Type is the specialty folder (e.g. Reports for "
            "labs) -- what KIND of visit it is (lab/prescription/...) gets figured "
            "out automatically, you don't type it.\n"
            "Example: Dad | Reports | 2026-07-10\n\n"
            f"Names: {', '.join(names)}\n"
            f"Types: {', '.join(types)} (omit to file directly under the person)\n\n"
            'Or just ask a question, e.g. "What\'s Dad on for BP?" or '
            '"Mom\'s latest HbA1c" -- name the person, it answers from health.db.\n'
            "Or /advise <person> <what to prepare for> -- drafts questions to ask a "
            "doctor, suggested reading and plain-language notes from that person's "
            "records (reasoning, not medical advice)."
        )

    # --- Event handling ------------------------------------------------------
    def process_event(self, event: dict[str, Any]) -> bool:
        """Handle one pre-authorized bridge message event. Returns True if a document was filed."""
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        raw_text = str(
            event.get("text")
            or payload.get("text")
            or event.get("caption")
            or payload.get("caption")
            or ""
        ).strip()
        attachments = event.get("attachments") or payload.get("attachments") or []
        attachment_error = event.get("attachment_error") or payload.get("attachment_error")

        if attachment_error:
            self.send_message(f"✗ Attachment could not be retrieved: {attachment_error}")
            return False

        if not attachments:
            cmd = raw_text.lower().split("@")[0]
            if cmd in ("/start", "/help"):
                tag_map, _ = _load_specialties()
                self.send_message(self._help_text(tag_map))
                return False

            # /advise <person> <what to prepare for> -- the grounded advisory/synthesis
            # path (src/qa.advise), distinct from a bare factual question below. Reached
            # ONLY by this explicit command, so the factual path is never widened by a
            # phrase heuristic misfiring.
            first_token = raw_text.split(maxsplit=1)[0] if raw_text else ""
            if first_token.lower().split("@")[0] == "/advise":
                remainder = raw_text[len(first_token) :].strip()
                if not remainder:
                    self.send_message(
                        "Usage: /advise <person> <what to prepare for> -- e.g. "
                        "/advise Dad prepare for the cardiology follow-up."
                    )
                    return False
                try:
                    answer = qa.advise(remainder)
                except Exception as e:
                    logger.error(f"Advisory failed: {e}", exc_info=True)
                    answer = f"✗ Could not put that together: {e}"
                self.send_message(answer)
                return False

            # No attachment -> not an ingest. Plain text that isn't a slash
            # command is a question, e.g. "Dad's latest HbA1c" -- answer it
            # over health.db instead of staying silent.
            if raw_text and not raw_text.startswith("/"):
                try:
                    answer = qa.answer_question(raw_text)
                except Exception as e:
                    logger.error(f"Q&A failed: {e}", exc_info=True)
                    answer = f"✗ Could not answer that: {e}"
                self.send_message(answer)
            return False

        tag_map, default_tag = _load_specialties()
        try:
            parsed = parse_caption(raw_text, tag_map, default_tag)
        except CaptionError as e:
            self.send_message(f"✗ {e}")
            return False

        att = attachments[0]
        candidate_suffix = _candidate_suffix(att)
        if candidate_suffix is not None:
            candidate_rel = os.path.join(
                parsed.folder,
                parsed.subfolder or "",
                f"{parsed.date} - {parsed.title}{candidate_suffix}",
            )
            if os.path.exists(source_path(candidate_rel)):
                self.send_message(f"✗ Already have {candidate_rel} -- use a different title.")
                return False

        attachment_id = att.get("attachment_id")
        if not attachment_id:
            self.send_message("✗ Attachment metadata missing attachment_id.")
            return False

        try:
            att_resp = self.chat_client.fetch_attachment(CHANNEL, str(attachment_id))
        except ChatGatewayUnavailableError:
            raise
        except Exception as e:
            logger.error(f"Download failed: {e}", exc_info=True)
            self.send_message(f"✗ Download failed: {e}")
            return False

        content = att_resp["data"]
        filename = att_resp.get("filename") or att.get("filename") or ""
        suffix = _suffix(filename, content)
        if suffix is None:
            self.send_message("✗ Not a PDF or image Paperless can read.")
            return False

        rel = os.path.join(
            parsed.folder, parsed.subfolder or "", f"{parsed.date} - {parsed.title}{suffix}"
        )
        path = source_path(rel)
        if os.path.exists(path):
            self.send_message(f"✗ Already have {rel} -- use a different title.")
            return False

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(content)
        except OSError as e:
            logger.error(f"Write to Drive failed: {e}", exc_info=True)
            self.send_message(f"✗ Could not save to Drive: {e}")
            return False

        # Same upload drive_sync.py would do on its next 6-hourly pass -- done
        # now instead of waiting, then marked synced so that pass skips it.
        try:
            corr_id = self.paperless.correspondent_id(parsed.correspondent, create=False)
            tag_id = self.paperless.tag_id(parsed.tag)
            doc_type_id = self.paperless.document_type_id(DOCUMENT_TYPE)
            task = self.paperless.upload(
                content=content,
                filename=os.path.basename(rel),
                title=f"{parsed.date} - {parsed.title}",
                correspondent=corr_id,
                document_type=doc_type_id,
                tags=[tag_id],
                created=parsed.date,
            )
        except Exception as e:
            # The Drive file is already saved -- the 6-hourly cron will pick it
            # up and retry, so this is a delay, not a loss.
            logger.error(f"Paperless upload failed: {e}", exc_info=True)
            self.send_message(
                f"Saved to Drive as {rel}, but Paperless upload failed ({e}). "
                "Will retry on the next sync.",
            )
            return True

        sync_state = load_state()
        sync_state[_key(path, rel)] = rel
        save_state(sync_state)

        # Extraction is an LLM call (10-30s+) and would blow this tick's
        # ~60s budget -- queue it instead, drained by run_extract_queue.py on
        # its own cron tick. See docs/telegram_ingest_queue.md.
        extract_queue.enqueue(
            rel=rel,
            correspondent=parsed.correspondent,
            tag=parsed.tag,
            title=parsed.title,
            date=parsed.date,
        )

        self.send_message(
            f"✓ Filed: {parsed.correspondent} · {parsed.tag} · {parsed.date} ({task}). "
            "Extraction queued -- I'll follow up shortly.",
        )
        return True

    def process_message(self, message: dict[str, Any]) -> bool:
        """Alias for process_event() for callers/tests passing an event dict."""
        return self.process_event(message)

    # --- Orchestration -----------------------------------------------------
    def _ack_and_save(self, seq: int) -> None:
        try:
            self.chat_client.ack_event(CHANNEL, seq)
        except (ChatSocketError, OSError) as e:
            logger.warning(f"Failed to ack event seq={seq}: {e}")
        prev = int(self.state.get("last_seq", 0))
        if seq > prev:
            self.state["last_seq"] = seq
        self._save_state()

    def run_once(self) -> int:
        """Register channel, pull events, process each message, ack and persist state.

        Returns the number of documents actually filed.
        """
        self.chat_client.register_channel(CHANNEL, transport=self.transport)
        last_seq = int(self.state.get("last_seq", 0))
        events = self.chat_client.pull_events(CHANNEL, after_seq=last_seq, wait_s=LONG_POLL_SECONDS)
        if not events:
            return 0

        filed = 0
        for event in events:
            seq = event.get("seq")
            event_type = event.get("type")
            if event_type is not None and event_type != "message":
                logger.debug(f"Skipping non-message event type={event_type!r} seq={seq}")
                if seq is not None:
                    self._ack_and_save(int(seq))
                continue

            try:
                if self.process_event(event):
                    filed += 1
            except ChatGatewayUnavailableError:
                raise
            except Exception as e:  # never let one bad message wedge the loop
                logger.error(f"Error processing event seq={seq}: {e}", exc_info=True)
            if seq is not None:
                self._ack_and_save(int(seq))
        return filed

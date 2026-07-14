"""Telegram document-ingest bot for nalam.

Cron-polled (no webhook, no exposed port), same shape as gajana's cash bot:
each run fetches new messages via ``getUpdates``, and any PDF/photo whose
caption parses cleanly is filed onto the Drive mount, in the exact place
drive_sync.py expects to find it -- then uploaded to Paperless immediately
(not left for the 6-hourly cron), and marked synced so that cron doesn't
double-post it.

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

import requests

from src.constants import CONSUMABLE_EXT, DOCUMENT_TYPE, MAGIC, MEDICAL_ROOT, SPECIALTIES_CONFIG
from src.drive_sync import _key, load_state, save_state
from src.paperless import Paperless
from src.people import resolve

logger = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/{method}"
FILE_URL = "https://api.telegram.org/file/bot{token}/{file_path}"
HTTP_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 120


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


def _suffix(filename: str, content: bytes) -> Optional[str]:
    """The file's real extension, or None if Paperless can't consume it.

    Same logic as drive_sync.sniff(), but on in-memory bytes -- no re-download.
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext in CONSUMABLE_EXT:
        return ext
    for magic, suffix in MAGIC.items():
        if content.startswith(magic):
            return suffix
    return None


def _load_token() -> str:
    from src.constants import SECRETS_DIR

    path = os.path.join(SECRETS_DIR, "telegram.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("token") or "").strip()
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


class TelegramDocBot:
    def __init__(self, settings: dict[str, Any], token: str, state_path: str) -> None:
        self.token = token
        self.state_path = state_path
        self.allowed_chat_id = settings.get("allowed_chat_id")
        self.allowed_users = {int(k): v for k, v in settings.get("allowed_users", {}).items()}
        self.state = self._load_state()
        self.paperless = Paperless()

    # --- Telegram transport --------------------------------------------------
    def _api(self, method: str) -> str:
        return API_URL.format(token=self.token, method=method)

    def get_updates(self) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
        if self.state.get("offset"):
            params["offset"] = self.state["offset"]
        resp = requests.get(self._api("getUpdates"), params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("result", [])

    def send_message(self, chat_id: int, text: str) -> None:
        """Plain text, no parse_mode -- error text often carries a file path or
        exception message with underscores/parens/etc that break Telegram's
        Markdown parser. A malformed-entity 400 doesn't raise (requests only
        raises on connection errors), so an unchecked send silently vanishes."""
        try:
            resp = requests.post(
                self._api("sendMessage"),
                json={"chat_id": chat_id, "text": text},
                timeout=HTTP_TIMEOUT,
            )
            if not resp.ok:
                logger.warning(f"Telegram reply rejected ({resp.status_code}): {resp.text[:200]}")
        except requests.RequestException as e:
            logger.warning(f"Failed to send Telegram reply: {e}")

    def _download(self, file_id: str) -> tuple[bytes, str]:
        resp = requests.get(self._api("getFile"), params={"file_id": file_id}, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        file_path = resp.json()["result"]["file_path"]
        url = FILE_URL.format(token=self.token, file_path=file_path)
        content = requests.get(url, timeout=DOWNLOAD_TIMEOUT).content
        return content, os.path.basename(file_path)

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

    # --- Authorization -------------------------------------------------------
    def _authorized(self, message: dict[str, Any]) -> bool:
        chat_id = message.get("chat", {}).get("id")
        user_id = message.get("from", {}).get("id")
        if self.allowed_chat_id in (None, 0, ""):
            logger.info(
                f"[setup] saw message in chat_id={chat_id} from user_id={user_id} "
                f"({message.get('from', {}).get('first_name')}). Set allowed_chat_id "
                "in settings to enable."
            )
            return False
        return chat_id == self.allowed_chat_id and user_id in self.allowed_users

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
            f"Types: {', '.join(types)} (omit to file directly under the person)"
        )

    # --- Message handling ------------------------------------------------------
    def _file_ref(self, message: dict[str, Any]) -> Optional[tuple[str, str]]:
        """(file_id, suggested filename) for a document or the largest photo."""
        if "document" in message:
            doc = message["document"]
            return doc["file_id"], doc.get("file_name") or "document.pdf"
        if "photo" in message:
            largest = max(message["photo"], key=lambda p: p.get("file_size", 0))
            return largest["file_id"], "photo.jpg"
        return None

    def process_message(self, message: dict[str, Any]) -> bool:
        """Handle one message. Returns True if a document was filed."""
        chat_id = message["chat"]["id"]
        text = str(message.get("text", "")).strip().lower().split("@")[0]
        if text in ("/start", "/help"):
            tag_map, _ = _load_specialties()
            self.send_message(chat_id, self._help_text(tag_map))
            return False

        file_ref = self._file_ref(message)
        if file_ref is None:
            return False  # chatter, not an attachment -> stay silent

        caption = message.get("caption", "")
        tag_map, default_tag = _load_specialties()
        try:
            parsed = parse_caption(caption, tag_map, default_tag)
        except CaptionError as e:
            self.send_message(chat_id, f"✗ {e}")
            return False

        file_id, filename = file_ref
        try:
            content, _ = self._download(file_id)
        except Exception as e:
            logger.error(f"Download failed: {e}", exc_info=True)
            self.send_message(chat_id, f"✗ Download failed: {e}")
            return False

        suffix = _suffix(filename, content)
        if suffix is None:
            self.send_message(chat_id, "✗ Not a PDF or image Paperless can read.")
            return False

        rel = os.path.join(
            parsed.folder, parsed.subfolder or "", f"{parsed.date} - {parsed.title}{suffix}"
        )
        path = os.path.join(MEDICAL_ROOT, rel)
        if os.path.exists(path):
            self.send_message(chat_id, f"✗ Already have {rel} -- use a different title.")
            return False

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(content)
        except OSError as e:
            logger.error(f"Write to Drive failed: {e}", exc_info=True)
            self.send_message(chat_id, f"✗ Could not save to Drive: {e}")
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
                chat_id,
                f"Saved to Drive as {rel}, but Paperless upload failed ({e}). "
                "Will retry on the next sync.",
            )
            return True

        sync_state = load_state()
        sync_state[_key(path, rel)] = rel
        save_state(sync_state)

        self.send_message(
            chat_id,
            f"✓ Filed: {parsed.correspondent} · {parsed.tag} · {parsed.date} ({task}). "
            "Extraction runs on the next daily pass.",
        )
        return True

    # --- Orchestration -----------------------------------------------------
    def run_once(self) -> int:
        """Poll once, process authorized messages, persist state. Returns the
        number of documents actually filed."""
        updates = self.get_updates()
        if not updates:
            return 0
        self.state["offset"] = max(u["update_id"] for u in updates) + 1

        authorized = [
            u["message"] for u in updates if "message" in u and self._authorized(u["message"])
        ]
        filed = 0
        for message in authorized:
            try:
                if self.process_message(message):
                    filed += 1
            except Exception as e:  # never let one bad message wedge the loop
                logger.error(f"Error processing message: {e}", exc_info=True)
        self._save_state()
        return filed

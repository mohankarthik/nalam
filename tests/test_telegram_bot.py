"""Caption parsing for the Telegram ingest bot.

The bot is the newest way a document can reach Paperless, so it inherits the
one rule that matters: an unresolved or ambiguous name is REJECTED, never
guessed. These tests exercise parse_caption() directly -- no network, no
Telegram, no Paperless.
"""

from __future__ import annotations

import datetime

import pytest

from plugins.telegram_bot import bot
from src.people import Person

ALICE = Person("Alice", "Alice Example", "female", False, aliases=("wife", "mom"))
BOB = Person("Bob", "Bob Example", "male", False, aliases=("me", "self"))

TAG_MAP = {"Lab": "Medical/Reports", "Vaccination": "Medical/Vaccination"}
DEFAULT_TAG = "Medical/General"


@pytest.fixture(autouse=True)
def people(monkeypatch: pytest.MonkeyPatch) -> None:
    directory = {
        "alice example": ALICE,
        "wife": ALICE,
        "mom": ALICE,
        "bob example": BOB,
        "me": BOB,
        "self": BOB,
    }

    def fake_resolve(who: str):
        return directory.get((who or "").strip().lower())

    monkeypatch.setattr(bot, "resolve", fake_resolve)


class TestName:
    def test_resolves_by_alias(self) -> None:
        parsed = bot.parse_caption("Wife | Lab | 2026-01-01", TAG_MAP, DEFAULT_TAG)
        assert parsed.correspondent == "Alice Example"
        assert parsed.folder == "Alice"

    def test_case_insensitive(self) -> None:
        parsed = bot.parse_caption("WIFE", TAG_MAP, DEFAULT_TAG)
        assert parsed.correspondent == "Alice Example"

    def test_unknown_name_rejected_not_guessed(self) -> None:
        with pytest.raises(bot.CaptionError, match="Unknown name"):
            bot.parse_caption("Uncle Bob | Lab", TAG_MAP, DEFAULT_TAG)

    def test_missing_name_rejected(self) -> None:
        with pytest.raises(bot.CaptionError, match="No name"):
            bot.parse_caption("", TAG_MAP, DEFAULT_TAG)
        with pytest.raises(bot.CaptionError, match="No name"):
            bot.parse_caption(None, TAG_MAP, DEFAULT_TAG)  # type: ignore[arg-type]


class TestType:
    def test_known_type_maps_to_tag_and_subfolder(self) -> None:
        parsed = bot.parse_caption("Wife | Lab", TAG_MAP, DEFAULT_TAG)
        assert parsed.tag == "Medical/Reports"
        assert parsed.subfolder == "Lab"

    def test_type_case_insensitive_uses_canonical_folder_casing(self) -> None:
        parsed = bot.parse_caption("Wife | vaccination", TAG_MAP, DEFAULT_TAG)
        assert parsed.tag == "Medical/Vaccination"
        assert parsed.subfolder == "Vaccination"

    def test_omitted_type_falls_back_to_default_tag_and_no_subfolder(self) -> None:
        parsed = bot.parse_caption("Wife", TAG_MAP, DEFAULT_TAG)
        assert parsed.tag == DEFAULT_TAG
        assert parsed.subfolder is None

    def test_unknown_type_rejected_not_silently_defaulted(self) -> None:
        """A typo in Type must not silently become Medical/General."""
        with pytest.raises(bot.CaptionError, match="Unknown type"):
            bot.parse_caption("Wife | Labb", TAG_MAP, DEFAULT_TAG)


class TestDate:
    def test_omitted_date_defaults_to_today(self) -> None:
        parsed = bot.parse_caption("Wife | Lab", TAG_MAP, DEFAULT_TAG)
        assert parsed.date == datetime.date.today().isoformat()

    def test_explicit_date_used(self) -> None:
        parsed = bot.parse_caption("Wife | Lab | 2020-05-01", TAG_MAP, DEFAULT_TAG)
        assert parsed.date == "2020-05-01"

    def test_bad_date_rejected(self) -> None:
        with pytest.raises(bot.CaptionError, match="Bad date"):
            bot.parse_caption("Wife | Lab | not-a-date", TAG_MAP, DEFAULT_TAG)

    def test_future_date_rejected(self) -> None:
        future = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        with pytest.raises(bot.CaptionError, match="future"):
            bot.parse_caption(f"Wife | Lab | {future}", TAG_MAP, DEFAULT_TAG)


class TestTitle:
    def test_explicit_title_used(self) -> None:
        parsed = bot.parse_caption("Wife | Lab | 2020-01-01 | Thyroid Panel", TAG_MAP, DEFAULT_TAG)
        assert parsed.title == "Thyroid Panel"

    def test_omitted_title_falls_back_to_type(self) -> None:
        parsed = bot.parse_caption("Wife | Lab", TAG_MAP, DEFAULT_TAG)
        assert parsed.title == "Lab"

    def test_omitted_title_and_type_falls_back_to_general(self) -> None:
        parsed = bot.parse_caption("Wife", TAG_MAP, DEFAULT_TAG)
        assert parsed.title == "General"


class TestProcessMessageEnqueuesExtraction:
    """A filed document is queued for extraction, not extracted inline.

    Extraction is an LLM call (10-30s+) and would blow the bot's ~60s tick
    budget if run inline -- see docs/telegram_ingest_queue.md. process_message()
    must enqueue and reply immediately, never block on ingest_document().
    """

    def _bot(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> bot.TelegramDocBot:
        monkeypatch.setattr(bot, "source_path", lambda rel: str(tmp_path / rel))
        monkeypatch.setattr(bot, "Paperless", lambda: _FakePaperless())
        monkeypatch.setattr(bot, "load_state", lambda: {})
        monkeypatch.setattr(bot, "save_state", lambda state: None)

        instance = bot.TelegramDocBot.__new__(bot.TelegramDocBot)
        instance.token = "test-token"
        instance.state_path = str(tmp_path / "state.json")
        instance.allowed_chat_id = 1
        instance.allowed_users = {1: "Someone"}
        instance.state = {}
        instance.paperless = _FakePaperless()
        return instance

    def test_success_enqueues_and_does_not_extract_inline(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = self._bot(tmp_path, monkeypatch)

        enqueued = {}
        monkeypatch.setattr(bot.extract_queue, "enqueue", lambda **kw: enqueued.update(kw))
        sent = []
        monkeypatch.setattr(instance, "send_message", lambda chat_id, text: sent.append(text))
        monkeypatch.setattr(instance, "_download", lambda file_id: (b"%PDF-1.4 fake", "scan.pdf"))

        message = {
            "chat": {"id": 1},
            "from": {"id": 1},
            "caption": "Wife | | 2026-01-01 | Thyroid",
            "document": {"file_id": "abc", "file_name": "scan.pdf"},
        }
        result = instance.process_message(message)

        assert result is True
        assert enqueued["correspondent"] == "Alice Example"
        assert enqueued["chat_id"] == 1
        assert enqueued["title"] == "Thyroid"
        assert any("queued" in t.lower() for t in sent)
        assert not any("daily pass" in t.lower() for t in sent)


class _FakePaperless:
    def correspondent_id(self, name: str, create: bool = False) -> int:
        return 1

    def tag_id(self, name: str) -> int:
        return 2

    def document_type_id(self, name: str) -> int:
        return 3

    def upload(self, **kwargs) -> str:
        return "task-id"

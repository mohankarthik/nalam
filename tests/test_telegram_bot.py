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

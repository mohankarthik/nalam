"""run_extract_queue.run_once() -- the Paperless-down-vs-slow-OCR split.

The whole point of this module is that those two situations must never be
handled the same way (see docs/telegram_ingest_queue.md's table). Offline:
Paperless, ingest_document() and Telegram are all faked; nothing here touches
a network or an LLM.
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

import run_extract_queue as rq
from src import db, extract_queue


def _item(rel: str, chat_id: int, minutes_ago: int) -> dict[str, Any]:
    queued_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        minutes=minutes_ago
    )
    return {
        "rel": rel,
        "correspondent": "Alice Example",
        "tag": "Medical/Reports",
        "title": "Labs",
        "date": "2026-01-01",
        "chat_id": chat_id,
        "queued_at": queued_at.isoformat(),
        "attempts": 0,
    }


@pytest.fixture(autouse=True)
def stub_common(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"sent": [], "ingest_calls": 0, "pushed": None}

    monkeypatch.setattr(rq, "_load_token", lambda: "tok")
    # _key() stats the real file on the Drive mount (used for sync-state
    # dedup elsewhere); irrelevant to routing/OCR-wait logic under test here.
    monkeypatch.setattr(rq, "_key", lambda path, rel: "fake-key")
    monkeypatch.setattr(
        rq, "send_message", lambda token, chat_id, text: calls["sent"].append((chat_id, text))
    )
    real_connect = db.connect
    monkeypatch.setattr(rq.db, "connect", lambda: real_connect(":memory:"))
    monkeypatch.setattr(
        rq.monitor, "push_paperless", lambda up, msg: calls.__setitem__("pushed", (up, msg))
    )
    return calls


class TestResultText:
    """_result_text() must never crash on an unrecognized doc_type/shape --
    that's exactly what stranded a queue item in production (2026-07-15): a
    'discharge' result missing the 'medications' key raised KeyError inside
    send_message(), so the item was never popped and every subsequent tick
    re-raised on the same document."""

    def test_missing_keys_on_a_doc_type_that_normally_has_them_does_not_raise(self) -> None:
        text = rq._result_text({"doc_type": "discharge", "note": "no extractor for this type"})
        assert "no extractor" in text

    def test_unknown_doc_type_does_not_raise(self) -> None:
        text = rq._result_text({"doc_type": "insurance"})
        assert "insurance" in text

    def test_lab_result_formats(self) -> None:
        assert rq._result_text({"doc_type": "lab", "committed": 2, "review": 1}) == (
            "Extracted: 2 observations, 1 to review."
        )

    def test_discharge_result_formats(self) -> None:
        result = {"doc_type": "discharge", "medications": 3, "encounters": 1, "misfiled": None}
        assert rq._result_text(result) == "Extracted: 3 medications, 1 encounter."


class TestPaperlessDown:
    def test_skips_whole_tick_queue_untouched(
        self, monkeypatch: pytest.MonkeyPatch, stub_common: dict[str, Any]
    ) -> None:
        monkeypatch.setattr(rq.monitor, "check_paperless", lambda: (False, "unreachable: refused"))
        items = [_item("a.pdf", 1, minutes_ago=0), _item("b.pdf", 2, minutes_ago=60)]
        monkeypatch.setattr(extract_queue, "load", lambda: items)
        saved = {}
        monkeypatch.setattr(extract_queue, "save", lambda items: saved.setdefault("items", items))

        rq.run_once()

        assert "items" not in saved  # queue never touched, not even re-saved as-is
        assert stub_common["sent"] == []
        assert stub_common["pushed"] == (False, "unreachable: refused")


class TestPaperlessUp:
    def _fake_paperless(self, ocr_present: bool) -> MagicMock:
        p = MagicMock()
        p.ocr_index.return_value = {("Alice Example", "a"): "ocr text"} if ocr_present else {}
        p.document_id_index.return_value = {}
        return p

    def test_ocr_present_extracts_and_pops(
        self, monkeypatch: pytest.MonkeyPatch, stub_common: dict[str, Any]
    ) -> None:
        monkeypatch.setattr(rq.monitor, "check_paperless", lambda: (True, "OK"))
        monkeypatch.setattr(rq, "Paperless", lambda: self._fake_paperless(True))
        monkeypatch.setattr(
            rq,
            "ingest_document",
            lambda con, doc, ocr_text=None, paperless_id=None: {
                "doc_type": "lab",
                "committed": 2,
                "review": 0,
            },
        )
        items = [_item("a.pdf", 1, minutes_ago=0)]
        monkeypatch.setattr(extract_queue, "load", lambda: items)
        saved = {}
        monkeypatch.setattr(extract_queue, "save", lambda items: saved.setdefault("items", items))

        rq.run_once()

        assert saved["items"] == []  # popped
        assert stub_common["sent"] == [(1, "Extracted: 2 observations, 0 to review.")]

    def test_ocr_missing_recent_waits_not_extracted(
        self, monkeypatch: pytest.MonkeyPatch, stub_common: dict[str, Any]
    ) -> None:
        monkeypatch.setattr(rq.monitor, "check_paperless", lambda: (True, "OK"))
        monkeypatch.setattr(rq, "Paperless", lambda: self._fake_paperless(False))
        called = []
        monkeypatch.setattr(
            rq,
            "ingest_document",
            lambda con, doc, ocr_text=None, paperless_id=None: called.append(1) or {},
        )
        items = [_item("a.pdf", 1, minutes_ago=5)]  # well under OCR_WAIT_MINUTES
        monkeypatch.setattr(extract_queue, "load", lambda: items)
        saved = {}
        monkeypatch.setattr(extract_queue, "save", lambda items: saved.setdefault("items", items))

        rq.run_once()

        assert called == []  # never extracted
        assert len(saved["items"]) == 1  # left in the queue, retried next tick
        assert stub_common["sent"] == []

    def test_ocr_missing_past_wait_falls_back_to_text_layer(
        self, monkeypatch: pytest.MonkeyPatch, stub_common: dict[str, Any]
    ) -> None:
        monkeypatch.setattr(rq.monitor, "check_paperless", lambda: (True, "OK"))
        monkeypatch.setattr(rq, "Paperless", lambda: self._fake_paperless(False))
        monkeypatch.setattr(
            rq,
            "ingest_document",
            lambda con, doc, ocr_text=None, paperless_id=None: {
                "doc_type": "lab",
                "committed": 1,
                "review": 1,
            },
        )
        items = [_item("a.pdf", 1, minutes_ago=rq.OCR_WAIT_MINUTES + 1)]
        monkeypatch.setattr(extract_queue, "load", lambda: items)
        saved = {}
        monkeypatch.setattr(extract_queue, "save", lambda items: saved.setdefault("items", items))

        rq.run_once()

        assert saved["items"] == []  # popped -- extracted with what we had
        assert stub_common["sent"] == [(1, "Extracted: 1 observations, 1 to review.")]

    def test_ingest_failure_retries_then_notifies_after_max_attempts(
        self, monkeypatch: pytest.MonkeyPatch, stub_common: dict[str, Any]
    ) -> None:
        monkeypatch.setattr(rq.monitor, "check_paperless", lambda: (True, "OK"))
        monkeypatch.setattr(rq, "Paperless", lambda: self._fake_paperless(True))

        def boom(con, doc, ocr_text=None, paperless_id=None):
            raise RuntimeError("gemini blew up")

        monkeypatch.setattr(rq, "ingest_document", boom)

        item = _item("a.pdf", 1, minutes_ago=0)
        item["attempts"] = rq.MAX_ATTEMPTS - 1  # one more failure hits the cap
        monkeypatch.setattr(extract_queue, "load", lambda: [item])
        saved = {}
        monkeypatch.setattr(extract_queue, "save", lambda items: saved.setdefault("items", items))

        rq.run_once()

        assert saved["items"] == []  # popped after exhausting retries
        assert len(stub_common["sent"]) == 1
        assert "failed" in stub_common["sent"][0][1].lower()

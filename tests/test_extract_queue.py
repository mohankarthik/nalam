"""The on-demand extraction queue -- offline, no network, no LLM.

Just a FIFO on disk: enqueue after a Telegram filing, drained by
run_extract_queue.py. See docs/telegram_ingest_queue.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import extract_queue


@pytest.fixture(autouse=True)
def isolated_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "pending_extract.json"
    monkeypatch.setattr(extract_queue, "QUEUE_PATH", str(path))
    return path


class TestLoad:
    def test_missing_file_is_empty(self) -> None:
        assert extract_queue.load() == []

    def test_corrupt_file_is_empty_not_a_crash(self, isolated_queue: Path) -> None:
        isolated_queue.write_text("{not json", encoding="utf-8")
        assert extract_queue.load() == []


class TestEnqueue:
    def test_roundtrip(self) -> None:
        extract_queue.enqueue(
            rel="Alice/2026-01-01 - Labs.pdf",
            correspondent="Alice Example",
            tag="Medical/Reports",
            title="Labs",
            date="2026-01-01",
            chat_id=42,
        )
        items = extract_queue.load()
        assert len(items) == 1
        item = items[0]
        assert item["rel"] == "Alice/2026-01-01 - Labs.pdf"
        assert item["correspondent"] == "Alice Example"
        assert item["chat_id"] == 42
        assert item["attempts"] == 0
        assert "queued_at" in item

    def test_appends_not_overwrites(self) -> None:
        extract_queue.enqueue(
            rel="a.pdf", correspondent="Alice", tag="t", title="A", date="2026-01-01", chat_id=1
        )
        extract_queue.enqueue(
            rel="b.pdf", correspondent="Bob", tag="t", title="B", date="2026-01-02", chat_id=2
        )
        items = extract_queue.load()
        assert [i["rel"] for i in items] == ["a.pdf", "b.pdf"]


class TestSave:
    def test_save_then_load(self, isolated_queue: Path) -> None:
        extract_queue.save([{"rel": "x.pdf"}])
        assert extract_queue.load() == [{"rel": "x.pdf"}]
        assert json.loads(isolated_queue.read_text(encoding="utf-8")) == [{"rel": "x.pdf"}]

    def test_creates_parent_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        nested = tmp_path / "nested" / "pending_extract.json"
        monkeypatch.setattr(extract_queue, "QUEUE_PATH", str(nested))
        extract_queue.save([])
        assert nested.exists()

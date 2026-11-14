"""Tests for the chat-bridged document-ingest + Q&A bot.

The bot inherits the one rule that matters: an unresolved or ambiguous name is
REJECTED, never guessed. These tests exercise parse_caption(),
TelegramDocBot.process_event(), ChatSocketClient polling/ack ordering,
attachment ingest, Q&A/advisory routing, and run_telegram_bot.main() -- all
offline, no network, no Paperless.
"""

from __future__ import annotations

import base64
import datetime
import json
import socket
import threading
from pathlib import Path
from typing import Any, Optional

import pytest

import run_telegram_bot as rtb
from plugins.telegram_bot import bot
from src.chat_client import (
    ChatGatewayUnavailableError,
    ChatSocketClient,
    ChatSocketError,
)
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


class _FakePaperless:
    def __init__(self, trace: Optional[list[str]] = None) -> None:
        self.uploads: list[dict[str, Any]] = []
        self._trace = trace

    def correspondent_id(self, name: str, create: bool = False) -> int:
        return 1

    def tag_id(self, name: str) -> int:
        return 2

    def document_type_id(self, name: str) -> int:
        return 3

    def upload(self, **kwargs) -> str:
        if self._trace is not None:
            self._trace.append("paperless_upload")
        self.uploads.append(kwargs)
        return "task-id"


class _FakeChatClient:
    def __init__(
        self,
        events: Optional[list[dict[str, Any]]] = None,
        attachments: Optional[dict[str, dict[str, Any]]] = None,
        trace: Optional[list[str]] = None,
    ) -> None:
        self.events = events or []
        self.attachments = attachments or {}
        self.trace = trace if trace is not None else []
        self.registered: list[tuple[str, Optional[str]]] = []
        self.pulled: list[tuple[str, int, float]] = []
        self.fetched: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str]] = []
        self.acked: list[tuple[str, int]] = []
        self.fetch_error: Optional[Exception] = None
        self.send_error: Optional[Exception] = None

    def register_channel(self, channel: str, transport: Optional[str] = None) -> dict[str, Any]:
        self.trace.append("register")
        self.registered.append((channel, transport))
        return {"ok": True}

    def pull_events(
        self, channel: str, after_seq: int = 0, wait_s: float = 0.0, limit: int = 50
    ) -> list[dict[str, Any]]:
        self.trace.append("pull")
        self.pulled.append((channel, after_seq, wait_s))
        return [e for e in self.events if int(e.get("seq", 0)) > after_seq]

    def fetch_attachment(self, channel: str, attachment_id: str) -> dict[str, Any]:
        self.trace.append("fetch_attachment")
        self.fetched.append((channel, attachment_id))
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.attachments[attachment_id]

    def send_message(self, channel: str, text: str) -> dict[str, Any]:
        self.trace.append("send_message")
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((channel, text))
        return {"ok": True}

    def ack_event(self, channel: str, seq: int) -> dict[str, Any]:
        self.trace.append("ack")
        self.acked.append((channel, seq))
        return {"ok": True}


def _make_bot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chat_client: _FakeChatClient,
    *,
    settings: Optional[dict[str, Any]] = None,
    trace: Optional[list[str]] = None,
) -> tuple[bot.TelegramDocBot, _FakePaperless, dict[str, str]]:
    monkeypatch.setattr(bot, "source_path", lambda rel: str(tmp_path / "drive" / rel))
    monkeypatch.setattr(bot, "_key", lambda path, rel: f"key:{rel}")
    sync_state: dict[str, str] = {}
    monkeypatch.setattr(bot, "load_state", lambda: dict(sync_state))
    monkeypatch.setattr(bot, "save_state", lambda st: sync_state.update(st))
    monkeypatch.setattr(
        bot,
        "_load_specialties",
        lambda: ({"Reports": "Medical/Reports", "Vaccination": "Medical/Vaccination"}, DEFAULT_TAG),
    )
    paperless = _FakePaperless(trace=trace)
    instance = bot.TelegramDocBot(
        settings=settings or {},
        state_path=str(tmp_path / "telegram_bot_state.json"),
        chat_client=chat_client,  # type: ignore[arg-type]
        paperless=paperless,  # type: ignore[arg-type]
    )
    return instance, paperless, sync_state


class TestProcessEventEnqueuesExtraction:
    """A filed document is queued for extraction, not extracted inline."""

    def test_success_enqueues_and_does_not_extract_inline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chat = _FakeChatClient(
            attachments={
                "att-1": {
                    "attachment_id": "att-1",
                    "filename": "scan.pdf",
                    "mime_type": "application/pdf",
                    "data": b"%PDF-1.4 fake",
                }
            }
        )
        instance, paperless, sync_state = _make_bot(tmp_path, monkeypatch, chat)

        enqueued: dict[str, Any] = {}
        monkeypatch.setattr(bot.extract_queue, "enqueue", lambda **kw: enqueued.update(kw))

        event = {
            "seq": 1,
            "type": "message",
            "text": "Wife | | 2026-01-01 | Thyroid",
            "attachments": [
                {"attachment_id": "att-1", "filename": "scan.pdf", "mime_type": "application/pdf"}
            ],
        }
        result = instance.process_event(event)

        assert result is True
        assert enqueued["correspondent"] == "Alice Example"
        assert enqueued["title"] == "Thyroid"
        assert "chat_id" not in enqueued
        assert len(paperless.uploads) == 1
        assert (
            tmp_path / "drive" / "Alice" / "2026-01-01 - Thyroid.pdf"
        ).read_bytes() == b"%PDF-1.4 fake"
        assert "key:Alice/2026-01-01 - Thyroid.pdf" in sync_state
        assert any("queued" in text.lower() for _, text in chat.sent)
        assert not any("daily pass" in text.lower() for _, text in chat.sent)

    def test_image_magic_bytes_sniffed_when_filename_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        jpeg_bytes = b"\xff\xd8\xff\xe0fake-jpeg-data"
        chat = _FakeChatClient(
            attachments={
                "att-img": {
                    "attachment_id": "att-img",
                    "filename": None,
                    "mime_type": "image/jpeg",
                    "data": jpeg_bytes,
                }
            }
        )
        instance, paperless, _ = _make_bot(tmp_path, monkeypatch, chat)
        monkeypatch.setattr(bot.extract_queue, "enqueue", lambda **kw: None)

        event = {
            "seq": 2,
            "type": "message",
            "payload": {
                "text": "Wife | Reports | 2026-02-02 | Scan",
                "attachments": [{"attachment_id": "att-img", "mime_type": "image/jpeg"}],
            },
        }
        result = instance.process_event(event)

        assert result is True
        saved_path = tmp_path / "drive" / "Alice" / "Reports" / "2026-02-02 - Scan.jpg"
        assert saved_path.read_bytes() == jpeg_bytes
        assert paperless.uploads[0]["filename"] == "2026-02-02 - Scan.jpg"


class TestChatBridgePollingAndAck:
    def test_run_once_registers_pulls_processes_and_acks_after_fetch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trace: list[str] = []
        chat = _FakeChatClient(
            events=[
                {
                    "seq": 10,
                    "type": "message",
                    "text": "Wife | Reports | 2026-03-01 | CBC",
                    "attachments": [
                        {
                            "attachment_id": "att-10",
                            "filename": "cbc.pdf",
                            "mime_type": "application/pdf",
                        }
                    ],
                }
            ],
            attachments={
                "att-10": {
                    "attachment_id": "att-10",
                    "filename": "cbc.pdf",
                    "mime_type": "application/pdf",
                    "data": b"%PDF-1.4 cbc",
                }
            },
            trace=trace,
        )
        # Seed legacy state with old Telegram "offset" key to verify safe migration.
        state_path = tmp_path / "telegram_bot_state.json"
        state_path.write_text(json.dumps({"offset": 99999}), encoding="utf-8")

        instance, _, _ = _make_bot(
            tmp_path, monkeypatch, chat, settings={"transport": "matrix"}, trace=trace
        )
        monkeypatch.setattr(bot.extract_queue, "enqueue", lambda **kw: None)

        filed = instance.run_once()

        assert filed == 1
        assert chat.registered == [("medical", "matrix")]
        assert chat.pulled == [("medical", 0, bot.LONG_POLL_SECONDS)]
        assert chat.acked == [("medical", 10)]
        assert trace == [
            "register",
            "pull",
            "fetch_attachment",
            "paperless_upload",
            "send_message",
            "ack",
        ]
        saved_state = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved_state["last_seq"] == 10

    def test_ack_not_called_when_fetch_raises_gateway_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chat = _FakeChatClient(
            events=[
                {
                    "seq": 5,
                    "type": "message",
                    "text": "Wife | Reports | 2026-03-01 | CBC",
                    "attachments": [{"attachment_id": "att-5", "filename": "cbc.pdf"}],
                }
            ]
        )
        chat.fetch_error = ChatGatewayUnavailableError(
            "bridge restarting", error_type="unavailable"
        )
        instance, _, _ = _make_bot(tmp_path, monkeypatch, chat)

        with pytest.raises(ChatGatewayUnavailableError):
            instance.run_once()

        assert chat.acked == []
        assert not (tmp_path / "telegram_bot_state.json").exists()

    def test_caption_error_and_collision_checked_before_fetch_attachment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        existing = tmp_path / "drive" / "Alice" / "Reports" / "2026-03-01 - CBC.pdf"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_bytes(b"old")

        chat = _FakeChatClient(
            events=[
                {
                    "seq": 1,
                    "type": "message",
                    "text": "UnknownPerson | Reports",
                    "attachments": [{"attachment_id": "att-1", "filename": "a.pdf"}],
                },
                {
                    "seq": 2,
                    "type": "message",
                    "text": "Wife | Reports | 2026-03-01 | CBC",
                    "attachments": [{"attachment_id": "att-2", "filename": "cbc.pdf"}],
                },
            ]
        )
        instance, _, _ = _make_bot(tmp_path, monkeypatch, chat)

        filed = instance.run_once()

        assert filed == 0
        assert chat.fetched == []  # never fetched either rejected attachment
        assert chat.acked == [("medical", 1), ("medical", 2)]
        assert "Unknown name" in chat.sent[0][1]
        assert "Already have" in chat.sent[1][1]

    def test_attachment_error_replies_helpfully_and_acks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chat = _FakeChatClient(
            events=[
                {
                    "seq": 7,
                    "type": "message",
                    "text": "Wife | Reports",
                    "attachments": [],
                    "attachment_error": "file exceeds 20MB limit",
                }
            ]
        )
        instance, _, _ = _make_bot(tmp_path, monkeypatch, chat)
        qa_called: list[str] = []
        monkeypatch.setattr(bot.qa, "answer_question", lambda q: qa_called.append(q) or "")

        filed = instance.run_once()

        assert filed == 0
        assert qa_called == []
        assert chat.acked == [("medical", 7)]
        assert "file exceeds 20MB limit" in chat.sent[0][1]

    def test_help_advise_and_qa_via_run_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chat = _FakeChatClient(
            events=[
                {"seq": 1, "type": "message", "text": "/help", "attachments": []},
                {"seq": 2, "type": "message", "text": "/advise mom cardio", "attachments": []},
                {"seq": 3, "type": "message", "text": "What is Dad's HbA1c?", "attachments": []},
            ]
        )
        instance, _, _ = _make_bot(tmp_path, monkeypatch, chat)
        monkeypatch.setattr(bot, "resolve", lambda who: ALICE)
        monkeypatch.setattr("src.people.load_people", lambda: {"alice": ALICE})
        monkeypatch.setattr(bot.qa, "advise", lambda q: f"ADVICE:{q}")
        monkeypatch.setattr(bot.qa, "answer_question", lambda q: f"QA:{q}")

        filed = instance.run_once()

        assert filed == 0
        assert chat.acked == [("medical", 1), ("medical", 2), ("medical", 3)]
        assert "Send a PDF or photo" in chat.sent[0][1]
        assert chat.sent[1] == ("medical", "ADVICE:mom cardio")
        assert chat.sent[2] == ("medical", "QA:What is Dad's HbA1c?")


class TestSendMessageNonRaising:
    @pytest.mark.parametrize(
        "exc",
        [
            ChatSocketError("timeout"),
            ChatGatewayUnavailableError("down", error_type="unavailable"),
            OSError("broken pipe"),
        ],
    )
    def test_send_message_catches_socket_errors_and_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        chat = _FakeChatClient()
        chat.send_error = exc
        instance, _, _ = _make_bot(tmp_path, monkeypatch, chat)

        assert instance.send_message("hello") is False
        assert bot.send_message("hello", chat_client=chat) is False  # type: ignore[arg-type]


class TestRealUnixSocketRoundTrip:
    def test_end_to_end_via_chat_socket_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sock_path = tmp_path / "chat.sock"
        ops: list[dict[str, Any]] = []
        pdf_bytes = b"%PDF-1.4 unix-sock-pdf"

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        srv.listen(8)
        stop = threading.Event()

        def serve() -> None:
            while not stop.is_set():
                srv.settimeout(0.2)
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                with conn:
                    raw = b""
                    while True:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        raw += chunk
                        if b"\n" in raw:
                            break
                    req = json.loads(raw.decode("utf-8").strip())
                    ops.append(req)
                    op = req["op"]
                    if op == "register_channel":
                        resp: dict[str, Any] = {"ok": True}
                    elif op == "pull_events":
                        resp = {
                            "ok": True,
                            "events": [
                                {
                                    "seq": 42,
                                    "type": "message",
                                    "text": "Wife | Reports | 2026-04-01 | Lipid",
                                    "attachments": [
                                        {
                                            "attachment_id": "a42",
                                            "filename": "lipid.pdf",
                                            "mime_type": "application/pdf",
                                        }
                                    ],
                                }
                            ],
                        }
                    elif op == "fetch_attachment":
                        resp = {
                            "ok": True,
                            "attachment_id": "a42",
                            "filename": "lipid.pdf",
                            "mime_type": "application/pdf",
                            "data_b64": base64.b64encode(pdf_bytes).decode("ascii"),
                        }
                    elif op in ("send_message", "ack_event"):
                        resp = {"ok": True}
                    else:
                        resp = {"ok": False, "error": f"unknown op {op}"}
                    conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            monkeypatch.setenv("AGENTIC_CHAT_SOCKET", str(sock_path))
            monkeypatch.setattr(bot, "source_path", lambda rel: str(tmp_path / "drive" / rel))
            monkeypatch.setattr(bot, "_key", lambda path, rel: f"key:{rel}")
            monkeypatch.setattr(bot, "load_state", lambda: {})
            monkeypatch.setattr(bot, "save_state", lambda st: None)
            monkeypatch.setattr(
                bot, "_load_specialties", lambda: ({"Reports": "Medical/Reports"}, DEFAULT_TAG)
            )
            monkeypatch.setattr(bot.extract_queue, "enqueue", lambda **kw: None)

            client = ChatSocketClient()
            instance = bot.TelegramDocBot(
                settings={"transport": "telegram"},
                state_path=str(tmp_path / "state.json"),
                chat_client=client,
                paperless=_FakePaperless(),  # type: ignore[arg-type]
            )
            assert instance.run_once() == 1
        finally:
            stop.set()
            srv.close()
            thread.join(timeout=2)

        assert [op["op"] for op in ops] == [
            "register_channel",
            "pull_events",
            "fetch_attachment",
            "send_message",
            "ack_event",
        ]
        assert ops[0]["transport"] == "telegram"
        assert ops[4]["seq"] == 42


class TestRunTelegramBotEntrypoint:
    def test_missing_settings_runs_with_defaults_and_handles_unavailable_socket(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rtb, "SETTINGS_PATH", str(tmp_path / "missing.json"))
        monkeypatch.setattr(rtb, "STATE_PATH", str(tmp_path / "state.json"))
        monkeypatch.setattr(rtb, "LOCK_PATH", str(tmp_path / "bot.lock"))

        seen: list[tuple[dict[str, Any], str]] = []

        class _FakeBot:
            def __init__(self, settings: dict[str, Any], state_path: str) -> None:
                seen.append((settings, state_path))

            def run_once(self) -> int:
                raise ChatGatewayUnavailableError("no sock", error_type="unavailable")

        monkeypatch.setattr(rtb, "TelegramDocBot", _FakeBot)
        rtb.main()  # must not raise
        assert seen == [({}, str(tmp_path / "state.json"))]

    def test_disabled_in_settings_skips_bot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"enabled": False}), encoding="utf-8")
        monkeypatch.setattr(rtb, "SETTINGS_PATH", str(settings_file))

        called = []
        monkeypatch.setattr(rtb, "TelegramDocBot", lambda *a, **kw: called.append(1))
        rtb.main()
        assert called == []

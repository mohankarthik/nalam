"""End-to-end pipeline test: chat.sock -> Drive -> Paperless -> extract_queue -> health.db -> QA.

Supports three modes:
  1. Default offline pytest mode: verifies prompt_schema_sha256 and replays
     tests/fixtures/e2e_recorded_llm.json over a local AF_UNIX llm.sock.
  2. Live pytest mode (AGENTIC_LIVE_LLM_E2E=1): talks to the host LLM gateway
     Unix socket with AGENTIC_CALLER=e2e-test.
  3. Recording CLI mode (python -m tests.test_e2e_pipeline --record-llm):
     proxies to the live host LLM gateway, runs all ground-truth assertions, and
     writes tests/fixtures/e2e_recorded_llm.json.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Optional
from unittest import mock

import pytest
from pypdf import PdfReader

import run_extract_queue
from plugins.telegram_bot import bot
from src import cache, constants, db, drive_sync, extract_queue, llm, people, qa
from src.chat_client import ChatSocketClient
from src.llm_client import LLMClient, LLMGatewayError, default_socket_path
from src.paperless import fold_filename

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "e2e_recorded_llm.json"
LAB_CONFIG_PATH = REPO_ROOT / "data" / "configs" / "lab.json"
ANALYTES_PATH = REPO_ROOT / "data" / "analytes.json"
UNITS_PATH = REPO_ROOT / "data" / "units.json"

PDF_LINES = [
    "Patient Name: Alice Example",
    "Age / Sex: 34 Years / Female",
    "Collection Date: 2026-04-01",
    "COMPLETE BLOOD COUNT",
    "Hemoglobin: 13.4 g/dL (Ref: 12.0 - 15.5)",
    "Platelet Count: 215 x10^3/uL (Ref: 150 - 400)",
]


def build_cbc_pdf_bytes() -> bytes:
    """Generate a clean 1-page digital-born PDF containing Alice Example's CBC report."""
    stream_lines = ["BT", "/F1 12 Tf", "50 740 Td"]
    for idx, line in enumerate(PDF_LINES):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if idx > 0:
            stream_lines.append("0 -20 Td")
        stream_lines.append(f"({escaped}) Tj")
    stream_lines.append("ET")
    stream_bytes = ("\n".join(stream_lines) + "\n").encode("ascii")

    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n"
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\n"
            b"endobj\n"
        ),
        (b"4 0 obj\n" b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\n" b"endobj\n"),
        (
            f"5 0 obj\n<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii")
            + stream_bytes
            + b"endstream\nendobj\n"
        ),
    ]

    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for obj in objects:
        offsets.append(buf.tell())
        buf.write(obj)

    xref_pos = buf.tell()
    buf.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    buf.write(b"0000000000 65535 f \n")
    for off in offsets:
        buf.write(f"{off:010d} 00000 n \n".encode("ascii"))
    buf.write(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        ).encode("ascii")
    )
    pdf_bytes = buf.getvalue()

    extracted = PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text() or ""
    for line in PDF_LINES:
        assert line in extracted, f"Missing line {line!r} in PDF text layer: {extracted!r}"
    return pdf_bytes


def prompt_schema_sha256() -> str:
    """Hash the lab extraction prompt and Q&A prompt/tool schemas."""
    lab_cfg = json.loads(LAB_CONFIG_PATH.read_text(encoding="utf-8"))
    payload = {
        "lab_prompt": lab_cfg["prompt"],
        "qa_system_prompt": qa.SYSTEM_PROMPT,
        "qa_tools": qa.TOOLS,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _UnixJsonServer:
    """Minimal threaded AF_UNIX newline-delimited JSON RPC server."""

    def __init__(
        self, sock_path: Path, handler: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> None:
        self.sock_path = sock_path
        self.handler = handler
        self.requests: list[dict[str, Any]] = []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(sock_path))
        self._srv.listen(16)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            if self._stop:
                conn.close()
                break
            with conn:
                chunks: list[bytes] = []
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    nl = chunk.find(b"\n")
                    if nl != -1:
                        chunks.append(chunk[: nl + 1])
                        break
                    chunks.append(chunk)
                raw = b"".join(chunks).decode("utf-8").strip()
                if not raw:
                    continue
                req = json.loads(raw)
                self.requests.append(req)
                try:
                    resp = self.handler(req)
                except Exception as exc:  # pragma: no cover - defensive mock server guard
                    resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))

    def close(self) -> None:
        self._stop = True
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
                wake.settimeout(0.1)
                wake.connect(str(self.sock_path))
        except OSError:
            pass
        try:
            self._srv.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


class _FakeChatBridge:
    """In-process AF_UNIX chat bridge server for E2E pipeline tests."""

    def __init__(self, sock_path: Path, pdf_bytes: bytes) -> None:
        self.pdf_bytes = pdf_bytes
        self.events_delivered = False
        self.sent_messages: list[tuple[str, str]] = []
        self.acked_events: list[tuple[str, int]] = []
        self._server = _UnixJsonServer(sock_path, self._handle)

    def _handle(self, req: dict[str, Any]) -> dict[str, Any]:
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "service": "chat_bridge"}
        if op == "register_channel":
            return {"ok": True}
        if op == "pull_events":
            if self.events_delivered:
                return {"ok": True, "events": []}
            self.events_delivered = True
            return {
                "ok": True,
                "events": [
                    {
                        "seq": 1,
                        "type": "message",
                        "text": "Alice | Reports | 2026-04-01 | CBC Panel",
                        "attachments": [
                            {
                                "attachment_id": "att-cbc-1",
                                "filename": "cbc.pdf",
                                "mime_type": "application/pdf",
                            }
                        ],
                    }
                ],
            }
        if op == "fetch_attachment":
            return {
                "ok": True,
                "attachment_id": req.get("attachment_id"),
                "filename": "cbc.pdf",
                "mime_type": "application/pdf",
                "size_bytes": len(self.pdf_bytes),
                "data_b64": base64.b64encode(self.pdf_bytes).decode("ascii"),
            }
        if op == "send_message":
            self.sent_messages.append((str(req.get("channel")), str(req.get("text"))))
            return {"ok": True}
        if op == "ack_event":
            self.acked_events.append((str(req.get("channel")), int(req.get("seq"))))
            return {"ok": True}
        return {"ok": False, "error": f"unsupported op {op!r}", "error_type": "protocol"}

    def close(self) -> None:
        self._server.close()


class _FakePaperless:
    """Fake Paperless client returning paperless_id=77 and the PDF's text layer as OCR."""

    def __init__(self, ocr_text: str, paperless_id: int = 77) -> None:
        self.ocr_text = ocr_text
        self.paperless_id = paperless_id
        self.uploads: list[dict[str, Any]] = []

    def correspondent_id(self, name: str, create: bool = False) -> int:
        return 1

    def tag_id(self, name: str, create: bool = True) -> int:
        return 2

    def document_type_id(self, name: str, create: bool = True) -> int:
        return 3

    def upload(self, **kwargs: Any) -> str:
        self.uploads.append(kwargs)
        return f"task-{self.paperless_id}"

    def ocr_index(self) -> dict[tuple[str, str], str]:
        return {
            ("Alice Example", fold_filename(u["filename"])): self.ocr_text for u in self.uploads
        }

    def document_id_index(self) -> dict[tuple[str, str], int]:
        return {
            ("Alice Example", fold_filename(u["filename"])): self.paperless_id for u in self.uploads
        }


def _ground_truth_expectations() -> tuple[str, str, str, float]:
    """Read canonical names, units, and conversion factor from analytes.json and units.json."""
    analytes = json.loads(ANALYTES_PATH.read_text(encoding="utf-8"))
    units = json.loads(UNITS_PATH.read_text(encoding="utf-8"))

    hb_name = analytes["718-7"]["name"]
    plt_name = analytes["777-3"]["name"]
    hb_unit = units[hb_name]["canonical"]
    plt_factor = float(units[plt_name]["convert"]["x10^3/uL"])
    return hb_name, hb_unit, plt_name, plt_factor


def _run_pipeline(tmp_path: Path, llm_socket_path: Optional[Path]) -> tuple[_FakeChatBridge, str]:
    """Execute the full E2E pipeline in tmp_path and assert fixed ground truth."""
    pdf_bytes = build_cbc_pdf_bytes()
    pdf_text = PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text() or ""

    people_file = tmp_path / "people.json"
    people_file.write_text(
        json.dumps(
            {
                "people": {
                    "Alice": {
                        "correspondent": "Alice Example",
                        "sex": "female",
                        "child": False,
                        "aliases": ["alice"],
                        "directory": str(tmp_path / "drive" / "Alice"),
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "health.db"
    llm_dir = tmp_path / "llm"
    chat_sock = tmp_path / "chat.sock"

    chat_bridge = _FakeChatBridge(chat_sock, pdf_bytes)
    fake_paperless = _FakePaperless(ocr_text=pdf_text, paperless_id=77)
    orig_connect = db.connect

    env_updates: dict[str, str] = {
        "AGENTIC_CHAT_SOCKET": str(chat_sock),
        "CHAT_BRIDGE_SOCKET": str(chat_sock),
        "AGENTIC_CALLER": "e2e-test",
    }
    if llm_socket_path is not None:
        env_updates["AGENTIC_LLM_SOCKET"] = str(llm_socket_path)
        env_updates["LLM_GATEWAY_SOCKET"] = str(llm_socket_path)

    with contextlib.ExitStack() as stack:
        stack.callback(chat_bridge.close)
        stack.enter_context(mock.patch.dict(os.environ, env_updates, clear=False))
        stack.enter_context(mock.patch.object(constants, "PEOPLE_CONFIG", str(people_file)))
        stack.enter_context(mock.patch.object(people, "PEOPLE_CONFIG", str(people_file)))
        stack.enter_context(mock.patch.object(drive_sync, "PEOPLE_CONFIG", str(people_file)))
        stack.enter_context(mock.patch.object(drive_sync, "STATE_DIR", str(state_dir)))
        stack.enter_context(
            mock.patch.object(drive_sync, "SYNC_STATE", str(state_dir / "synced.json"))
        )
        stack.enter_context(
            mock.patch.object(extract_queue, "QUEUE_PATH", str(state_dir / "pending_extract.json"))
        )
        stack.enter_context(
            mock.patch.object(
                extract_queue, "_LOCK_PATH", str(state_dir / "pending_extract.json.lock")
            )
        )
        stack.enter_context(mock.patch.object(cache, "LLM_DIR", str(llm_dir)))
        stack.enter_context(mock.patch.object(db, "DB_PATH", str(db_path)))
        stack.enter_context(
            mock.patch.object(
                db,
                "connect",
                lambda path=None: orig_connect(str(db_path) if path is None else path),
            )
        )
        stack.enter_context(mock.patch.object(qa, "STATE_DIR", str(state_dir)))
        stack.enter_context(mock.patch.object(qa, "QA_LOG", str(state_dir / "qa_log.jsonl")))
        stack.enter_context(mock.patch.object(qa, "VIEWER_URL", "https://paperless.example.com"))
        stack.enter_context(mock.patch.object(run_extract_queue, "STATE_DIR", str(state_dir)))
        stack.enter_context(
            mock.patch.object(run_extract_queue, "LOCK_PATH", str(state_dir / "extract_queue.lock"))
        )
        stack.enter_context(
            mock.patch.object(run_extract_queue.monitor, "check_paperless", lambda: (True, "OK"))
        )
        stack.enter_context(
            mock.patch.object(run_extract_queue.monitor, "push_paperless", lambda is_up, msg: None)
        )
        stack.enter_context(
            mock.patch.object(run_extract_queue, "Paperless", lambda *a, **kw: fake_paperless)
        )

        # Step (a): Bot pulls message + PDF over chat.sock, saves to Drive, uploads, enqueues.
        doc_bot = bot.TelegramDocBot(
            settings={"transport": "telegram"},
            state_path=str(state_dir / "telegram_bot_state.json"),
            chat_client=ChatSocketClient(),
            paperless=fake_paperless,  # type: ignore[arg-type]
        )
        filed = doc_bot.run_once()
        assert filed == 1
        assert len(extract_queue.load()) == 1
        assert (
            tmp_path / "drive" / "Alice" / "Reports" / "2026-04-01 - CBC Panel.pdf"
        ).read_bytes() == pdf_bytes
        assert chat_bridge.acked_events == [("medical", 1)]
        assert any("Filed: Alice Example" in text for _, text in chat_bridge.sent_messages)

        # Step (b): Queue drain extracts via llm.sock, validates, normalizes, commits to health.db.
        run_extract_queue.run_once()
        assert extract_queue.load() == []
        assert any(
            text == "Extracted: 2 observations, 0 to review."
            for _, text in chat_bridge.sent_messages
        )

        # Step (c): QA tool loop queries health.db via llm.sock and returns answer + citation.
        reply = qa.answer_question("What are Alice's hemoglobin and platelet count?")

    # Step (d): Verify ground truth in health.db and QA reply.
    hb_name, hb_unit, plt_name, plt_factor = _ground_truth_expectations()
    con = orig_connect(str(db_path))
    try:
        obs_rows = con.execute(
            """SELECT o.subject, o.analyte, o.effective, o.value_num, o.raw_value,
                      o.unit, o.status, d.paperless_id
               FROM observations o
               JOIN documents d ON d.id = o.document_id
               ORDER BY o.analyte"""
        ).fetchall()
        assert len(obs_rows) == 2
        by_analyte = {r["analyte"]: dict(r) for r in obs_rows}
        assert set(by_analyte) == {hb_name, plt_name}

        hb = by_analyte[hb_name]
        assert hb["subject"] == "Alice Example"
        assert hb["effective"] == "2026-04-01"
        assert hb["value_num"] == pytest.approx(13.4)
        assert hb["unit"].lower() == hb_unit.lower()
        assert hb["status"] == "ok"
        assert hb["paperless_id"] == 77

        plt = by_analyte[plt_name]
        assert plt["subject"] == "Alice Example"
        assert plt["effective"] == "2026-04-01"
        assert plt["value_num"] == pytest.approx(215.0 * plt_factor)
        assert plt["raw_value"] == "215"
        assert plt["status"] == "ok"
        assert plt["paperless_id"] == 77

        review_count = con.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0]
        assert review_count == 0
    finally:
        con.close()

    assert "13.4" in reply
    assert "215" in reply
    assert "/documents/77/details" in reply
    return chat_bridge, reply


def test_recorded_fixture_schema_matches_current_prompts() -> None:
    assert FIXTURE_PATH.exists(), f"Missing recorded fixture: {FIXTURE_PATH}"
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fixture["prompt_schema_sha256"] == prompt_schema_sha256()


def test_e2e_pipeline_offline(tmp_path: Path) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fixture["prompt_schema_sha256"] == prompt_schema_sha256()

    qa_turns = list(fixture["qa_turns"])
    callers: list[Any] = []
    llm_sock = tmp_path / "llm.sock"

    def handle_llm(req: dict[str, Any]) -> dict[str, Any]:
        if req.get("op") == "ping":
            return {"ok": True, "service": "llm_gateway"}
        callers.append(req.get("caller"))
        title = req.get("title", "")
        if title == "nalam_lab" or req.get("attachments"):
            text = fixture["lab_extract_text"]
            return {
                "ok": True,
                "text": text,
                "data": json.loads(llm._strip_fences(text)),
                "meta": {"model": fixture["recorded_models"]["lab"]},
            }
        if title == "nalam_qa" or "tools" in req:
            turn = qa_turns.pop(0)
            return {
                "ok": True,
                "turn": turn["turn"],
                "meta": turn.get("meta") or {"model": fixture["recorded_models"]["qa"]},
            }
        return {"ok": False, "error": f"unexpected llm request: {title!r}"}

    srv = _UnixJsonServer(llm_sock, handle_llm)
    try:
        _run_pipeline(tmp_path, llm_socket_path=llm_sock)
    finally:
        srv.close()

    assert qa_turns == []
    assert callers and all(c == "e2e-test" for c in callers)


@pytest.mark.skipif(
    os.environ.get("AGENTIC_LIVE_LLM_E2E") != "1",
    reason="set AGENTIC_LIVE_LLM_E2E=1 for live LLM gateway E2E",
)
def test_e2e_pipeline_live(tmp_path: Path) -> None:
    _run_pipeline(tmp_path, llm_socket_path=None)


def _record_llm() -> None:
    """Run against the live host LLM gateway via a recording proxy and save the fixture."""
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("AGENTIC_LLM_SOCKET", None)
        os.environ.pop("LLM_GATEWAY_SOCKET", None)
        live_sock = default_socket_path()

    if not live_sock.exists():
        raise SystemExit(f"Live LLM gateway socket not found at {live_sock}")

    live_client = LLMClient(socket_path=live_sock)
    recorded_models: dict[str, str] = {}
    lab_extract_text: Optional[str] = None
    qa_turns: list[dict[str, Any]] = []
    qa_family_seen: Optional[str] = None

    with tempfile.TemporaryDirectory(prefix="nalam-e2e-record-") as td:
        tmp_path = Path(td)
        proxy_sock = tmp_path / "record_llm.sock"

        def proxy_handler(req: dict[str, Any]) -> dict[str, Any]:
            nonlocal lab_extract_text, qa_family_seen
            if req.get("caller") != "e2e-test":
                raise AssertionError(f"Expected caller='e2e-test', got {req.get('caller')!r}")
            try:
                resp = live_client._request(
                    req, timeout_s=float(req.get("timeout_s", 300.0)) + 15.0
                )
            except LLMGatewayError as exc:
                return exc.response or {
                    "ok": False,
                    "error": str(exc),
                    "error_code": exc.error_code or "unavailable",
                }
            meta = dict(resp.get("meta") or {})
            title = req.get("title", "")
            if title == "nalam_lab":
                lab_extract_text = str(resp["text"])
                recorded_models["lab"] = str(meta.get("model") or req.get("family") or "unknown")
            elif title == "nalam_qa":
                req_family = req.get("family")
                if qa_family_seen is not None and req_family != qa_family_seen:
                    # Fallback model started a fresh loop; keep only the winning loop's turns.
                    qa_turns.clear()
                qa_family_seen = str(req_family)
                qa_turns.append({"turn": resp["turn"], "meta": meta})
                if (resp.get("turn") or {}).get("type") == "final":
                    recorded_models["qa"] = str(meta.get("model") or req_family or "unknown")
            return resp

        srv = _UnixJsonServer(proxy_sock, proxy_handler)
        try:
            _run_pipeline(tmp_path, llm_socket_path=proxy_sock)
        finally:
            srv.close()

    if not lab_extract_text or not qa_turns:
        raise RuntimeError("Failed to record both lab extraction and QA turns")

    fixture = {
        "prompt_schema_sha256": prompt_schema_sha256(),
        "recorded_models": recorded_models,
        "lab_extract_text": lab_extract_text,
        "qa_turns": qa_turns,
    }
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(
        json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Wrote {FIXTURE_PATH} (models={recorded_models}, qa_turns={len(qa_turns)})")


def main() -> int:
    parser = argparse.ArgumentParser(description="Nalam E2E pipeline runner")
    parser.add_argument(
        "--record-llm",
        action="store_true",
        help="Run against live host llm.sock and record tests/fixtures/e2e_recorded_llm.json",
    )
    args = parser.parse_args()
    if args.record_llm:
        _record_llm()
        return 0
    return pytest.main([__file__, "-q"])


if __name__ == "__main__":
    raise SystemExit(main())

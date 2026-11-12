"""Offline tests for src.llm_client."""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from src import llm_client as lc
from src.llm_client import (
    LLMClient,
    LLMGatewayError,
    LLMProtocolError,
    LLMToolLoopError,
    LLMUnavailableError,
)

_MODULE_TMP: tempfile.TemporaryDirectory[str] | None = None
_SAVED_ENV: dict[str, str | None] = {}
_ENV_KEYS = ("AGENTIC_STATE_DIR", "AGENTIC_CONFIG_DIR", "AGENTIC_AGY_HOME")


def setUpModule() -> None:
    global _MODULE_TMP
    _MODULE_TMP = tempfile.TemporaryDirectory(prefix="agentic-llm-client-test-")
    base = Path(_MODULE_TMP.name)
    for key in _ENV_KEYS:
        _SAVED_ENV[key] = os.environ.get(key)
    os.environ["AGENTIC_STATE_DIR"] = str(base / "state")
    os.environ["AGENTIC_CONFIG_DIR"] = str(base / "config")
    os.environ["AGENTIC_AGY_HOME"] = str(base / "agy")


def tearDownModule() -> None:
    global _MODULE_TMP
    for key, val in _SAVED_ENV.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    _SAVED_ENV.clear()
    if _MODULE_TMP is not None:
        _MODULE_TMP.cleanup()
        _MODULE_TMP = None


class _MockGatewayServer:
    def __init__(self, sock_path: Path, handler):
        self.sock_path = sock_path
        self.handler = handler
        self.requests: list[dict] = []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(sock_path))
        self._srv.listen(8)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            if self._stop:
                conn.close()
                break
            with conn:
                data = b""
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                    if b"\n" in data:
                        break
                line = data.splitlines()[0].decode("utf-8") if data else "{}"
                req = json.loads(line)
                self.requests.append(req)
                resp = self.handler(req)
                if isinstance(resp, bytes):
                    conn.sendall(resp)
                else:
                    conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))

    def close(self):
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


class LLMClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sock_path = self.tmp / "llm.sock"

    def start_server(self, handler) -> _MockGatewayServer:
        srv = _MockGatewayServer(self.sock_path, handler)
        self.addCleanup(srv.close)
        return srv

    def test_default_socket_path_resolution(self):
        with mock.patch.dict(os.environ, {"AGENTIC_LLM_SOCKET": "/custom/llm.sock"}, clear=False):
            self.assertEqual(lc.default_socket_path(), Path("/custom/llm.sock"))

        with mock.patch.dict(os.environ, {"AGENTIC_STATE_DIR": str(self.tmp / "st")}, clear=False):
            os.environ.pop("AGENTIC_LLM_SOCKET", None)
            os.environ.pop("LLM_GATEWAY_SOCKET", None)
            with mock.patch.object(Path, "exists", return_value=True):
                self.assertEqual(lc.default_socket_path(), Path("/run/agentic/llm.sock"))
            with mock.patch.object(Path, "exists", return_value=False):
                self.assertEqual(lc.default_socket_path(), self.tmp / "st" / "gateway" / "llm.sock")

    def test_ping_availability_and_connect_errors(self):
        client = LLMClient(socket_path=self.sock_path)
        self.assertFalse(client.is_available(timeout_s=0.2))
        with self.assertRaises(LLMUnavailableError):
            client.ping(timeout_s=0.2)

        srv = self.start_server(
            lambda req: {"ok": True, "service": "llm_gateway", "op": req.get("op")}
        )
        self.assertTrue(client.is_available())
        self.assertEqual(client.ping()["service"], "llm_gateway")
        self.assertEqual(srv.requests[-1], {"op": "ping"})
        self.assertTrue(lc.is_available(socket_path=self.sock_path))
        self.assertEqual(lc.ping(socket_path=self.sock_path)["service"], "llm_gateway")

    def test_error_mapping_and_protocol_framing(self):
        responses = [
            {"ok": False, "error_code": "unavailable", "error": "all pools exhausted"},
            {"ok": False, "error_code": "timeout", "error": "request timed out after 5s"},
            {"ok": False, "error_code": "invalid_request", "error": "bad schema"},
            b"not-json\n",
            b"",
        ]
        srv = self.start_server(lambda _req: responses.pop(0))
        client = LLMClient(socket_path=self.sock_path)

        with self.assertRaises(LLMUnavailableError) as u_ctx:
            client.complete_text("hi", timeout_s=5.0)
        self.assertIn("all pools exhausted", str(u_ctx.exception))
        self.assertEqual(srv.requests[-1]["timeout_s"], 5.0)

        with self.assertRaises(LLMGatewayError) as t_ctx:
            client.complete_text("hi", timeout_s=5.0)
        self.assertEqual(t_ctx.exception.error_code, "timeout")
        self.assertIn("request timed out", str(t_ctx.exception))

        with self.assertRaises(LLMGatewayError) as g_ctx:
            client.complete_text("hi")
        self.assertNotIsInstance(g_ctx.exception, LLMUnavailableError)
        self.assertIn("bad schema", str(g_ctx.exception))

        with self.assertRaises(LLMProtocolError):
            client.complete_text("hi")

        with self.assertRaises(LLMProtocolError):
            client.complete_text("hi")

        # Verify complete() passes eff_timeout + SOCKET_TIMEOUT_GRACE_S to _request
        with mock.patch.object(
            client, "_request", return_value={"ok": True, "text": "ok"}
        ) as m_req:
            client.complete_text("grace check", timeout_s=12.0)
            self.assertEqual(m_req.call_args.args[0]["timeout_s"], 12.0)
            self.assertEqual(m_req.call_args.kwargs["timeout_s"], 12.0 + lc.SOCKET_TIMEOUT_GRACE_S)

    def test_complete_text_json_and_pdf_with_attachment_normalization(self):
        def handler(req):
            if req.get("response_format") == "json":
                return {
                    "ok": True,
                    "data": {"title": "Paper", "pages": 3},
                    "meta": {
                        "runtime": "antigravity",
                        "family": "gemini",
                        "model": "gemini-3.8-flash-high",
                    },
                }
            return {
                "ok": True,
                "text": "hello world",
                "meta": {"runtime": "claude", "family": "claude", "model": "sonnet"},
            }

        srv = self.start_server(handler)
        client = LLMClient(socket_path=self.sock_path)

        text, meta = client.complete_text(
            "Say hello", system="Be brief", payloads=[{"k": "v"}], tier="fast"
        )
        self.assertEqual(text, "hello world")
        self.assertEqual(meta["model"], "sonnet")
        self.assertEqual(srv.requests[-1]["system"], "Be brief")
        self.assertEqual(srv.requests[-1]["payloads"], [{"k": "v"}])

        data, jmeta = client.complete_json("Extract", json_schema={"type": "object"})
        self.assertEqual(data, {"title": "Paper", "pages": 3})
        self.assertEqual(jmeta["family"], "gemini")
        self.assertEqual(srv.requests[-1]["response_format"], "json")

        pdf_bytes = b"%PDF-1.4 fake pdf bytes"
        pdata, _ = client.complete_pdf(
            "Parse PDF", pdf_bytes, filename="test.pdf", tier="reasoning"
        )
        self.assertEqual(pdata["title"], "Paper")
        att = srv.requests[-1]["attachments"][0]
        self.assertEqual(att["filename"], "test.pdf")
        self.assertEqual(att["media_type"], "application/pdf")
        self.assertEqual(base64.b64decode(att["data_b64"]), pdf_bytes)

        # Pre-encoded data_b64 is also accepted
        client.complete(
            prompt="check b64",
            attachments=[
                {"filename": "a.png", "data_b64": att["data_b64"], "media_type": "image/png"}
            ],
        )
        self.assertEqual(srv.requests[-1]["attachments"][0]["filename"], "a.png")

    def test_run_tool_loop_success_unknown_tool_and_max_rounds(self):
        turns = [
            {
                "ok": True,
                "turn": {
                    "type": "tool_calls",
                    "calls": [
                        {"id": "call_1", "name": "add", "arguments": {"a": 2, "b": 3}},
                        {"id": "call_2", "name": "missing_tool", "arguments": {}},
                        {"id": "call_3", "name": "add", "arguments": {"wrong_kw": 1}},
                        {"id": "call_4", "name": "boom", "arguments": {}},
                        {"id": "call_5", "name": "add", "arguments": ["not-a-dict"]},
                    ],
                },
                "meta": {"round": 1},
            },
            {
                "ok": True,
                "turn": {"type": "final", "content": "The sum is 5."},
                "meta": {"round": 2},
            },
        ]
        srv = self.start_server(lambda _req: turns.pop(0))
        client = LLMClient(socket_path=self.sock_path)
        initial = [{"role": "user", "content": "What is 2 + 3?"}]
        tools = [
            {"name": "add", "description": "Add two numbers", "parameters": {"type": "object"}},
            {
                "name": "boom",
                "description": "Raises RuntimeError",
                "parameters": {"type": "object"},
            },
        ]

        def boom():
            raise RuntimeError("database offline")

        ans, meta = client.run_tool_loop(
            initial, tools, {"add": lambda a, b: {"sum": a + b}, "boom": boom}, max_rounds=4
        )
        self.assertEqual(ans, "The sum is 5.")
        self.assertEqual(meta, {"round": 2})
        self.assertEqual(len(initial), 1)  # input list was not mutated
        second_msgs = srv.requests[1]["messages"]
        self.assertEqual(second_msgs[1]["role"], "assistant")
        self.assertEqual(
            second_msgs[2], {"role": "tool", "tool_call_id": "call_1", "content": '{"sum": 5}'}
        )
        self.assertIn("no such tool: missing_tool", second_msgs[3]["content"])
        self.assertIn("TypeError:", second_msgs[4]["content"])
        self.assertIn("RuntimeError: database offline", second_msgs[5]["content"])
        self.assertIn("tool arguments must be a JSON object", second_msgs[6]["content"])

        # Exceeding max_rounds raises LLMToolLoopError
        infinite_call = {
            "ok": True,
            "turn": {
                "type": "tool_calls",
                "calls": [{"id": "c1", "name": "add", "arguments": {"a": 1, "b": 1}}],
            },
            "meta": {},
        }
        srv2_path = self.tmp / "loop.sock"
        srv2 = _MockGatewayServer(srv2_path, lambda _req: infinite_call)
        self.addCleanup(srv2.close)
        with self.assertRaises(LLMToolLoopError):
            lc.run_tool_loop(
                initial,
                tools,
                {"add": lambda a, b: a + b},
                max_rounds=2,
                socket_path=srv2_path,
            )


if __name__ == "__main__":
    unittest.main()

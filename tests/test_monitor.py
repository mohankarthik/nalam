"""Paperless health probe + Uptime-Kuma push -- offline, mocked HTTP.

check_paperless() feeds run_extract_queue.py's decision to skip the whole
tick during an outage (docs/telegram_ingest_queue.md); push() must never raise
regardless of what the network does, since a monitoring failure must not fail
the pipeline it watches.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from src import monitor


class TestCheckPaperless:
    def test_up_on_2xx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = MagicMock(ok=True)
        resp.raise_for_status.return_value = None
        fake_session = MagicMock()
        fake_session.get.return_value = resp
        fake_paperless = MagicMock(url="http://paperless.local", session=fake_session)
        monkeypatch.setattr(monitor, "Paperless", lambda: fake_paperless)

        up, msg = monitor.check_paperless()
        assert up is True
        assert msg == "OK"

    def test_down_on_connection_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_session = MagicMock()
        fake_session.get.side_effect = requests.ConnectionError("refused")
        fake_paperless = MagicMock(url="http://paperless.local", session=fake_session)
        monkeypatch.setattr(monitor, "Paperless", lambda: fake_paperless)

        up, msg = monitor.check_paperless()
        assert up is False
        assert "unreachable" in msg

    def test_down_on_5xx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = MagicMock(ok=False)
        resp.raise_for_status.side_effect = requests.HTTPError("500")
        fake_session = MagicMock()
        fake_session.get.return_value = resp
        fake_paperless = MagicMock(url="http://paperless.local", session=fake_session)
        monkeypatch.setattr(monitor, "Paperless", lambda: fake_paperless)

        up, msg = monitor.check_paperless()
        assert up is False

    def test_down_on_bad_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom() -> Any:
            raise monitor.PaperlessError("no creds")

        monkeypatch.setattr(monitor, "Paperless", boom)
        up, msg = monitor.check_paperless()
        assert up is False
        assert "credentials" in msg


class TestPush:
    def test_noop_without_url(self) -> None:
        monitor.push(None, True, "OK")  # must not raise

    def test_merges_status_and_msg_into_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = {}

        def fake_get(url: str, timeout: int) -> Any:
            captured["url"] = url
            resp = MagicMock(ok=True)
            resp.raise_for_status.return_value = None
            return resp

        monkeypatch.setattr(monitor.requests, "get", fake_get)
        monitor.push("https://kuma.local/api/push/abc?existing=1", False, "Paperless unreachable")

        assert "status=down" in captured["url"]
        assert "existing=1" in captured["url"]
        assert "msg=Paperless" in captured["url"]

    def test_never_raises_on_network_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_get(url: str, timeout: int) -> Any:
            raise requests.ConnectionError("refused")

        monkeypatch.setattr(monitor.requests, "get", fake_get)
        monitor.push("https://kuma.local/api/push/abc", True, "OK")  # must not raise


class TestPushPaperless:
    def test_reads_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = []
        monkeypatch.setattr(monitor, "push", lambda url, up, msg: calls.append((url, up, msg)))
        monkeypatch.setenv(monitor.ENV_PAPERLESS_PUSH_URL, "https://kuma.local/x")

        monitor.push_paperless(True, "OK")
        assert calls == [("https://kuma.local/x", True, "OK")]

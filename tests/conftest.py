"""Shared pytest fixtures for offline test isolation."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_llm_gateway_socket(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Prevent offline pytest runs from ever touching the live host gateway sockets."""
    nonexistent = str(tmp_path / "nonexistent.sock")
    monkeypatch.setenv("LLM_GATEWAY_SOCKET", nonexistent)
    monkeypatch.setenv("AGENTIC_LLM_SOCKET", nonexistent)
    no_chat = str(tmp_path / "no-chat.sock")
    monkeypatch.setenv("AGENTIC_CHAT_SOCKET", no_chat)
    monkeypatch.setenv("CHAT_BRIDGE_SOCKET", no_chat)

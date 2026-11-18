"""Shared pytest fixtures for offline test isolation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_llm_gateway_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """Prevent offline pytest runs from ever touching the live host gateway sockets."""
    allow_live = (
        os.environ.get("AGENTIC_LIVE_LLM_E2E") == "1"
        and Path(str(request.node.path)).name == "test_e2e_pipeline.py"
    )
    if not allow_live:
        nonexistent = str(tmp_path / "nonexistent.sock")
        monkeypatch.setenv("LLM_GATEWAY_SOCKET", nonexistent)
        monkeypatch.setenv("AGENTIC_LLM_SOCKET", nonexistent)
    no_chat = str(tmp_path / "no-chat.sock")
    monkeypatch.setenv("AGENTIC_CHAT_SOCKET", no_chat)
    monkeypatch.setenv("CHAT_BRIDGE_SOCKET", no_chat)

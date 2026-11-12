"""Shared pytest fixtures for offline test isolation."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_llm_gateway_socket(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Prevent offline pytest runs from ever touching the live host gateway socket."""
    nonexistent = str(tmp_path / "nonexistent.sock")
    monkeypatch.setenv("LLM_GATEWAY_SOCKET", nonexistent)
    monkeypatch.setenv("AGENTIC_LLM_SOCKET", nonexistent)

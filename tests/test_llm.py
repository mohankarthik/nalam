"""Offline unit tests for src/llm.py."""

from __future__ import annotations

from typing import Any

import pytest

from src import llm
from src.llm_client import LLMUnavailableError


class TestModelToFamily:
    def test_maps_gemini_and_claude_variants(self) -> None:
        assert llm._model_to_family("gemini") == "gemini"
        assert llm._model_to_family("gemini/gemini-2.5-flash") == "gemini"
        assert llm._model_to_family("claude") == "claude"
        assert llm._model_to_family("claude-sonnet-4-6") == "claude"
        assert llm._model_to_family("anthropic/claude-sonnet-4-6") == "claude"
        assert llm._model_to_family("other-model") is None


class TestCallWithPdf:
    def test_cache_hit_skips_gateway_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            llm.cache,
            "load",
            lambda source, doc_type, prompt: {
                "raw": '```json\n{"patient": {"name": "Alice Example"}}\n```',
                "model": "cached-gemini",
            },
        )

        def _unexpected_complete(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("llm_client.complete must not be called on cache hit")

        monkeypatch.setattr(llm.llm_client, "complete", _unexpected_complete)

        parsed, model = llm.call_with_pdf(
            "extract lab",
            b"%PDF-1.4",
            source="Alice Example/Reports/lab.pdf",
            doc_type="lab",
            use_cache=True,
        )
        assert parsed == {"patient": {"name": "Alice Example"}}
        assert model == "cached-gemini"

    def test_cache_save_persists_raw_parsed_and_served_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(llm.cache, "load", lambda source, doc_type, prompt: None)
        saved: dict[str, Any] = {}

        def _fake_save(**kwargs: Any) -> None:
            saved.update(kwargs)

        monkeypatch.setattr(llm.cache, "save", _fake_save)

        calls: list[dict[str, Any]] = []

        def _fake_complete(**kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {
                "ok": True,
                "data": {"doc_type": "lab"},
                "text": '{"doc_type": "lab"}',
                "meta": {"model": "gemini-3-flash"},
            }

        monkeypatch.setattr(llm.llm_client, "complete", _fake_complete)

        parsed, served_model = llm.call_with_pdf(
            "classify prompt",
            b"%PDF-1.4 fake",
            source="Alice Example/Reports/doc.pdf",
            doc_type="classify",
            use_cache=True,
        )
        assert parsed == {"doc_type": "lab"}
        assert served_model == "gemini-3-flash"
        assert len(calls) == 1
        assert calls[0]["family"] == "gemini"
        assert calls[0]["response_format"] == "json"
        assert calls[0]["attachments"][0]["filename"] == "classify.pdf"
        assert saved == {
            "source": "Alice Example/Reports/doc.pdf",
            "doc_type": "classify",
            "prompt": "classify prompt",
            "raw": '{"doc_type": "lab"}',
            "parsed": {"doc_type": "lab"},
            "model": "gemini-3-flash",
        }

    def test_primary_to_fallback_failover(self, monkeypatch: pytest.MonkeyPatch) -> None:
        families: list[Any] = []

        def _fake_complete(**kwargs: Any) -> dict[str, Any]:
            families.append(kwargs.get("family"))
            if kwargs.get("family") == "gemini":
                raise LLMUnavailableError("gemini pool exhausted")
            return {
                "ok": True,
                "data": {"doc_type": "discharge"},
                "meta": {"model": "claude-sonnet-4-6"},
            }

        monkeypatch.setattr(llm.llm_client, "complete", _fake_complete)

        parsed, served_model = llm.call_with_pdf(
            "extract discharge",
            b"%PDF-1.4 fake",
            use_cache=False,
        )
        assert families == ["gemini", "claude"]
        assert parsed == {"doc_type": "discharge"}
        assert served_model == "claude-sonnet-4-6"

    def test_all_models_failed_raises_runtime_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _always_fail(**kwargs: Any) -> dict[str, Any]:
            raise LLMUnavailableError("gateway unreachable")

        monkeypatch.setattr(llm.llm_client, "complete", _always_fail)

        with pytest.raises(RuntimeError, match="All models failed"):
            llm.call_with_pdf("extract lab", b"%PDF-1.4 fake", use_cache=False)

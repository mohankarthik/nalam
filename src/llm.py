"""LLM access for nalam via the host CLI gateway Unix socket.

The prompt is the safety mechanism, not a formality:

  The model TRANSCRIBES. It never interprets, converts, computes or reformats.

A lab value is copied exactly as printed -- "6.8", "4.0 - 5.6", "mg/dL" -- and
deterministic code does every conversion afterwards. An LLM that is allowed to
convert mmol/L to mg/dL, or to "tidy" 5.20 into 5.2, is an LLM that can quietly
put a wrong number in a medical record. So it is never allowed to.

Primary model family is Gemini (and the bulk of these PDFs are digital-born
text). Claude is the escalation, used on retry -- same call site, so wiring in a
second opinion later costs nothing.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from src import cache, llm_client

logger = logging.getLogger(__name__)

PRIMARY_MODEL = "gemini"
FALLBACK_MODEL = "claude"


def _model_to_family(model: str) -> Optional[str]:
    norm = (model or "").strip().lower()
    if norm == "gemini" or norm.startswith("gemini/") or norm.startswith("gemini"):
        return "gemini"
    if norm == "claude" or norm.startswith("claude") or norm.startswith("anthropic/"):
        return "claude"
    return None


def configure_api_keys() -> None:
    """Backward-compatible no-op; the host LLM gateway manages CLI sessions."""
    return None


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def call_with_pdf(
    prompt: str,
    pdf_bytes: bytes,
    models: Optional[list[str]] = None,
    source: Optional[str] = None,
    doc_type: Optional[str] = None,
    use_cache: bool = True,
) -> tuple[dict[str, Any], str]:
    """Run `prompt` against a PDF. Returns (parsed json, model that answered).

    When `source` and `doc_type` are given the raw response is cached to
    data/llm/ and reused. Parsing is free and gets rewritten; the LLM call costs
    money and does not. Changing the prompt invalidates the entry, because a
    different question deserves a different answer.
    """
    if use_cache and source and doc_type:
        hit = cache.load(source, doc_type, prompt)
        if hit is not None:
            logger.info(f"cache hit: {source} [{doc_type}]")
            return json.loads(_strip_fences(hit["raw"])), str(hit.get("model", "cached"))

    configure_api_keys()

    for model in models or [PRIMARY_MODEL, FALLBACK_MODEL]:
        try:
            resp = llm_client.complete(
                prompt=prompt,
                attachments=[
                    {
                        "filename": f"{doc_type or 'document'}.pdf",
                        "data": pdf_bytes,
                        "media_type": "application/pdf",
                    }
                ],
                response_format="json",
                tier="fast",
                family=_model_to_family(model),
                title=f"nalam_{doc_type or 'pdf'}",
            )
            raw = resp.get("text")
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("Expected non-empty verbatim JSON text from gateway")
            parsed = json.loads(_strip_fences(raw))
            if not isinstance(parsed, dict):
                raise ValueError(f"Expected JSON object, got {type(parsed).__name__}")
            served_model = str((resp.get("meta") or {}).get("model") or model)
            if use_cache and source and doc_type:
                # Verbatim, before anything is made of it. If our parsing and
                # the model's words ever disagree, the words are the truth.
                cache.save(
                    source=source,
                    doc_type=doc_type,
                    prompt=prompt,
                    raw=raw,
                    parsed=parsed,
                    model=served_model,
                )
            return parsed, served_model
        except Exception as e:
            logger.warning(f"{model} failed: {e}")

    raise RuntimeError("All models failed")

"""LLM access for nalam. Lifted from gajana's PDFParser, same contract.

The prompt is the safety mechanism, not a formality:

  The model TRANSCRIBES. It never interprets, converts, computes or reformats.

A lab value is copied exactly as printed -- "6.8", "4.0 - 5.6", "mg/dL" -- and
deterministic code does every conversion afterwards. An LLM that is allowed to
convert mmol/L to mg/dL, or to "tidy" 5.20 into 5.2, is an LLM that can quietly
put a wrong number in a medical record. So it is never allowed to.

Primary model is Gemini (free tier, and the bulk of these PDFs are digital-born
text). Claude is the escalation, used on retry -- same call site, so wiring in a
second opinion later costs nothing.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from typing import Any, Optional

import litellm

from src import cache
from src.constants import SECRETS_DIR

logger = logging.getLogger(__name__)

PRIMARY_MODEL = "gemini/gemini-2.5-flash"
FALLBACK_MODEL = "anthropic/claude-sonnet-4-6"

# Gemini 2.5 Flash's free tier allows ~10 requests/minute. Pace BELOW that, not
# at it: a 4s interval is 15 RPM and walks straight into a 429 on every batch.
_PRIMARY_MIN_INTERVAL = 7.0
_last_primary_call = 0.0

# A rate limit is TRANSIENT (per-minute), not terminal. Treating it as terminal
# -- blacklisting the model for the rest of the process -- sends an entire
# backfill to the paid fallback after a single 429. That happened: 8 documents
# in, every remaining one was routed to Claude. So back off and retry instead,
# and only fall through to the paid model once the free one has genuinely
# stopped answering.
_RATE_LIMIT_RETRIES = 3
_RATE_LIMIT_BACKOFF = 65.0  # seconds; the quota window is per-minute
_cooldown_until: dict[str, float] = {}


def _load_key(filename: str, env_var: str) -> str:
    """Read an API key. Tolerates the loose JSON gajana's secrets use."""
    path = os.path.join(SECRETS_DIR, filename)
    if os.path.exists(path):
        raw = open(path, encoding="utf-8").read()
        m = re.search(r'"api_key"\s*:\s*"([^"]+)"', raw)
        if m:
            return m.group(1).strip()
    return os.environ.get(env_var, "")


def configure_api_keys() -> None:
    for filename, env_var in (
        ("gemini.json", "GEMINI_API_KEY"),
        ("anthropic.json", "ANTHROPIC_API_KEY"),
    ):
        key = _load_key(filename, env_var)
        if key:
            os.environ.setdefault(env_var, key)


def _is_primary(model: str) -> bool:
    return not ("anthropic" in model or "claude" in model)


def _throttle(model: str) -> None:
    """Space out calls to the primary model to stay under its rate limit.

    The default paces for Gemini's FREE tier. On a paid key the limit is orders
    of magnitude higher and this is pure dead time -- set
    NALAM_GEMINI_MIN_INTERVAL=0 to turn it off.
    """
    global _last_primary_call
    interval = float(os.environ.get("NALAM_GEMINI_MIN_INTERVAL", _PRIMARY_MIN_INTERVAL))
    if not _is_primary(model) or interval <= 0:
        return
    wait = interval - (time.monotonic() - _last_primary_call)
    if wait > 0:
        time.sleep(wait)
    _last_primary_call = time.monotonic()


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
    b64 = base64.b64encode(pdf_bytes).decode("utf-8")

    for model in models or [PRIMARY_MODEL, FALLBACK_MODEL]:
        if "anthropic" in model or "claude" in model:
            doc: dict[str, Any] = {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": b64,
                },
            }
        else:
            doc = {
                "type": "image_url",
                "image_url": {"url": f"data:application/pdf;base64,{b64}"},
            }

        # Retry a rate-limited model rather than abandoning it -- the limit is a
        # per-minute window, and abandoning the free model costs real money.
        attempts = _RATE_LIMIT_RETRIES if _is_primary(model) else 1
        for attempt in range(1, attempts + 1):
            _throttle(model)
            try:
                resp = litellm.completion(
                    model=model,
                    messages=[
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": prompt}, doc],
                        }
                    ],
                    response_format={"type": "json_object"},
                )
                content = resp.choices[0].message.content or ""
                parsed = json.loads(_strip_fences(content))
                if use_cache and source and doc_type:
                    # Verbatim, before anything is made of it. If our parsing and
                    # the model's words ever disagree, the words are the truth.
                    cache.save(
                        source=source,
                        doc_type=doc_type,
                        prompt=prompt,
                        raw=content,
                        parsed=parsed,
                        model=model,
                    )
                return parsed, model
            except litellm.RateLimitError:
                if attempt < attempts:
                    logger.warning(
                        f"{model}: rate limited, waiting {_RATE_LIMIT_BACKOFF:.0f}s "
                        f"(attempt {attempt}/{attempts}) rather than falling back "
                        f"to the paid model."
                    )
                    time.sleep(_RATE_LIMIT_BACKOFF)
                else:
                    logger.warning(
                        f"{model}: still rate limited after {attempts} attempts; "
                        f"falling through to the next model."
                    )
                    _cooldown_until[model] = time.monotonic() + _RATE_LIMIT_BACKOFF
            except Exception as e:
                logger.warning(f"{model} failed: {e}")
                break

    raise RuntimeError("All models failed")

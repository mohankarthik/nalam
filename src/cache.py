"""Persist what the model actually said. The raw response is the expensive artefact.

Parsing is free and will be got wrong several times. The LLM call is neither.

This was learned the hard way: a schema change mid-backfill meant re-extracting
146 documents through the paid API, purely because the raw output had been thrown
away the moment it was parsed. Nothing about those documents had changed. We had
simply not kept the only part that cost money.

So every response is written to `data/llm/`, mirroring the source tree:

    data/llm/<source path>.<doc_type>.json

and re-runs read from there. Re-parsing, fixing a normaliser, adding a field to
the schema, backfilling a new column -- all free, all offline.

The cache is keyed on the PROMPT as well as the document. Change the prompt and
you have asked a different question, so the old answer is not an answer to it:
the entry is ignored and the document is re-extracted. A cache half-built from
two different prompts is a silent correctness trap.

Lives under data/ so the homelab's restic job backs it up to B2 alongside
health.db. Gitignored -- it is verbatim medical text.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
from typing import Any, Optional

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

LLM_DIR = os.path.join(DATA_DIR, "llm")


def prompt_fingerprint(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def path_for(source: str, doc_type: str) -> str:
    """Mirror the source tree, so a cached response is findable by eye.

    data/llm/Person/Specialty/2026-04-02 - Consult.pdf.prescription.json
    """
    return os.path.join(LLM_DIR, f"{source}.{doc_type}.json")


def load(source: str, doc_type: str, prompt: str) -> Optional[dict[str, Any]]:
    """The cached response, or None if absent or asked with a different prompt."""
    path = path_for(source, doc_type)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            entry = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Unreadable cache entry {path}: {e}")
        return None

    if entry.get("prompt_sha") != prompt_fingerprint(prompt):
        # A different question was asked. The old answer does not answer it.
        return None
    return entry


def save(
    source: str,
    doc_type: str,
    prompt: str,
    raw: str,
    parsed: Any,
    model: str,
    oracle_source: str = "",
) -> None:
    """Write the model's response verbatim, next to what we made of it.

    `raw` is the point of this file: the model's own words, exactly as returned.
    `parsed` is a convenience -- it can always be recomputed from `raw`, and if
    the two ever disagree, `raw` is the truth.
    """
    path = path_for(source, doc_type)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "source": source,
                "doc_type": doc_type,
                "model": model,
                "prompt_sha": prompt_fingerprint(prompt),
                "oracle_source": oracle_source,
                "extracted_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "raw": raw,
                "parsed": parsed,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )


def stats() -> dict[str, int]:
    counts: dict[str, int] = {}
    for root, _dirs, files in os.walk(LLM_DIR):
        for name in files:
            if not name.endswith(".json"):
                continue
            parts = name.rsplit(".", 3)
            doc_type = parts[-2] if len(parts) >= 2 else "?"
            counts[doc_type] = counts.get(doc_type, 0) + 1
    return counts

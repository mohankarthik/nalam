"""Colloquial <-> clinical condition names, for meds.for_condition().

"What did she get for a cold" and a discharge summary that says "AURTI" are the
same fact, worded two different ways -- a family member uses the colloquial
term, a clinician writes the shorthand. A literal substring search matches
neither to the other, which is exactly the bug that made the Telegram bot say
"no trustworthy record" for a medicine that had, in fact, been given.

data/conditions.json is hand-curated, generic medical terminology only (same
shape and same rule as data/aliases.json): no names, no dates, nothing from
anyone's actual record. It expands the QUERY, never invents a diagnosis that
isn't already in the returned rows -- the widened search still only matches
against what a document actually says.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from src.constants import CONDITIONS_CONFIG


@lru_cache(maxsize=1)
def load_conditions() -> dict[str, tuple[str, ...]]:
    with open(CONDITIONS_CONFIG, encoding="utf-8") as f:
        raw = json.load(f)
    return {k: tuple(v) for k, v in raw.items() if not k.startswith("_")}


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def expand(condition: str) -> list[str]:
    """The condition as typed, plus every colloquial/clinical synonym on file.

    Matching is WHOLE-WORD token containment, not raw substring search: a raw
    substring check makes a 2-letter clinical abbreviation like "RA" match
    inside ordinary words ("ra" is a substring of "rare", "library" -- a real
    bug caught by test_conditions.py). A bucket matches only when ALL of one
    of its terms' words are present as their OWN tokens in the condition, so
    "cold" matches "a really bad cold" (token "cold" present) and "URTI"
    matches the "cold" bucket back (its own synonym "URTI" is one token), but
    "RA" never fires on a sentence that merely contains the letters r-a. No
    match on file -> the condition alone, unchanged -- an unmapped term is
    searched literally, not dropped and not guessed at.
    """
    want_tokens = _tokens(condition)
    if not want_tokens:
        return [condition]

    terms = {condition}
    for key, synonyms in load_conditions().items():
        bucket = (key, *synonyms)
        if any(_tokens(term) and _tokens(term) <= want_tokens for term in bucket):
            terms.add(key)
            terms.update(synonyms)
    return sorted(terms)

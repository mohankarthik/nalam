"""Colloquial <-> clinical condition names, for meds.for_condition().

"What did she get for a cold" and a discharge summary that says "AURTI" are the
same fact, worded two different ways -- a family member uses the colloquial
term, a clinician writes the shorthand. A literal substring search matches
neither to the other, which is exactly the bug that made the Telegram bot say
"no trustworthy record" for a medicine that had, in fact, been given.

data/conditions.json is hand-curated, generic medical terminology only (same
shape and same rule as data/aliases.json): no names, no dates, nothing from
anyone's actual record. Each bucket now carries a standard **ICD-10-CM identity**
(``icd10`` 3-char category list + ``icd10_title``) exactly the way Phase A gave
each analyte a LOINC code: the bucket KEY stays the runtime identity every
consumer keys on, and the standard code is an added FIELD -- no re-keying. The
``aliases`` list is the fuzzy layer; it expands the QUERY, never invents a
diagnosis that isn't already in the returned rows.
"""

from __future__ import annotations

import re
from functools import lru_cache

import os

from src import config
from src.constants import (
    CONDITIONS_CONFIG,
    CONDITIONS_IGNORE_CONFIG,
    ICD10_CATEGORIES_CONFIG,
)


@lru_cache(maxsize=1)
def load_conditions() -> dict[str, dict]:
    """{bucket_key -> {"icd10": [...], "icd10_title": str, "aliases": [...]}}.

    The ``_``-prefixed comment key is stripped by config.load. Every value is the
    new dict shape; aliases default to empty so a bare bucket still matches on its
    own key.
    """
    out: dict[str, dict] = {}
    for key, entry in config.load(CONDITIONS_CONFIG).items():
        out[key] = {
            "icd10": tuple(entry.get("icd10") or ()),
            "icd10_title": entry.get("icd10_title") or "",
            "aliases": tuple(entry.get("aliases") or ()),
        }
    return out


@lru_cache(maxsize=1)
def _categories() -> dict[str, str]:
    """ICD-10-CM 3-char category -> official title, baked into the image.

    Runtime validation of printed codes stays offline: the licensed source table
    (data/icd10cm.csv) is gitignored and NOT in the image; this generic subset is.
    """
    return config.load(ICD10_CATEGORIES_CONFIG)


def _terms(entry: dict) -> tuple[str, ...]:
    """A bucket's matchable strings: the key's aliases (the key itself is added by
    the caller, which owns it)."""
    return entry["aliases"]


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

    A colloquial umbrella that spans several 1:1 clinical buckets ("heart
    disease" -> angina / heart attack / coronary artery disease) is carried as
    an alias in each of them, so it still widens across all of them.
    """
    want_tokens = _tokens(condition)
    if not want_tokens:
        return [condition]

    terms = {condition}
    for key, entry in load_conditions().items():
        bucket = (key, *_terms(entry))
        if any(_tokens(term) and _tokens(term) <= want_tokens for term in bucket):
            terms.add(key)
            terms.update(_terms(entry))
    return sorted(terms)


def canonical(diagnosis: str) -> str | None:
    """The bucket key a printed diagnosis belongs to, or None if unmapped.

    The inverse of expand(): "TYPE 2 DIABETES MELLITUS" -> "type 2 diabetes",
    "K/C/O HTN" -> "high blood pressure". Same whole-word token containment,
    so a generic committed alias ("HTN") matches a messy real string
    ("K/C/O HTN") without the map ever holding that real string -- the reason
    consolidation can run over personal records while the map stays generic.

    First bucket wins (dict insertion order); this is how a bare colloquial
    umbrella resolves to a chosen default (a plain "diabetes" -> type 2, listed
    first). Unmapped -> None, and the caller keeps the raw text; nothing is
    guessed or dropped.
    """
    dx = _tokens(diagnosis)
    if not dx:
        return None
    for key, entry in load_conditions().items():
        for term in (key, *_terms(entry)):
            t = _tokens(term)
            if t and t <= dx:
                return key
    return None


def canonical_labels(diagnoses: list[str]) -> list[str]:
    """Consolidate an encounter's diagnoses to a sorted, de-duplicated set of
    filter labels for the Encounters dropdown:

    * mapped   -> the bucket key (variants collapse to one label);
    * ignored  -> DROPPED (a generic non-condition like "BMI - 35-7" is not a
      filterable diagnosis -- it stays visible in the card's raw diagnoses, just
      not as a filter chip);
    * unmapped -> its own trimmed text, so a genuinely-unreviewed diagnosis still
      appears and stays filterable.

    The card body still renders the verbatim diagnoses either way, so nothing is
    hidden from the record -- only the filter labels are curated."""
    labels: set[str] = set()
    for dx in diagnoses:
        dx = (dx or "").strip()
        if not dx:
            continue
        bucket = canonical(dx)
        if bucket:
            labels.add(bucket)
        elif not is_ignored(dx):
            labels.add(dx)
    return sorted(labels)


def icd10_for(bucket_key: str) -> tuple[str, ...]:
    """The ICD-10-CM category codes assigned to a bucket, or () if none/unknown."""
    entry = load_conditions().get(bucket_key)
    return entry["icd10"] if entry else ()


@lru_cache(maxsize=1)
def load_ignore() -> tuple[str, ...]:
    """Generic clinical findings we chose NOT to give a bucket (data/conditions_ignore.json).

    Same role as data/ignored_analytes.json: a printed diagnosis that matches one is
    dropped from the review queue instead of nagging forever. Committed, generic terms
    only -- never a person's record-specific string.
    """
    if not os.path.exists(CONDITIONS_IGNORE_CONFIG):
        return ()
    return tuple(config.load(CONDITIONS_IGNORE_CONFIG).get("ignore") or ())


def is_ignored(diagnosis: str) -> bool:
    """True if a generic ignore term matches the diagnosis by whole-word token
    containment (the same matching the buckets use, so "flat foot" silences
    "B/L flat foot")."""
    dx = _tokens(diagnosis)
    if not dx:
        return False
    return any(_tokens(term) and _tokens(term) <= dx for term in load_ignore())


def is_undecided(diagnosis: str) -> bool:
    """A diagnosis still awaiting review: it maps to no bucket and matches no ignore
    term. This is exactly what the UI badge counts and the review worksheet lists --
    nothing decided, nothing guessed. Record-specific junk is meant to be FIXED (its
    text cleaned against the PDF) until it either maps or reduces to a generic term
    that can be bucketed or ignored; there is no commit-the-raw-string escape hatch."""
    dx = (diagnosis or "").strip()
    if not dx:
        return False
    return canonical(dx) is None and not is_ignored(dx)


# A printed ICD-10 code: a letter, two more characters (2nd is a digit), an
# optional decimal detail. Word-bounded so it never fires mid-token ("T2DM" does
# not yield "T2D"). Real codes are then kept ONLY if their 3-char parent is a
# genuine category -- this is what rejects vitamin "B12"/"D3" that appear verbatim
# in real diagnosis strings but are not ICD categories.
_ICD_RE = re.compile(r"\b([A-Z][0-9][0-9A-Z](?:\.[0-9A-Z]{1,4})?)\b")


def category_of(code: str) -> str:
    """The 3-char category parent of a printed code: "H25.13" -> "H25"."""
    return code.replace(".", "")[:3].upper()


def parse_icd10(text: str) -> list[str]:
    """Every real ICD-10-CM code printed in a verbatim diagnosis string, in order,
    de-duplicated. Validated against the baked category table so nutrition tokens
    like "B12" are refused rather than captured as a fake code -- refusing is safe,
    guessing is not. Returns the full printed code ("H25.13"), not just the parent.
    """
    cats = _categories()
    out: list[str] = []
    seen: set[str] = set()
    for m in _ICD_RE.finditer((text or "").upper()):
        code = m.group(1)
        if category_of(code) in cats and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def reconcile(diagnosis: str) -> tuple[list[str], list[str]]:
    """(printed_codes, mismatches) for one verbatim diagnosis string.

    Parses the printed ICD-10 code(s), then -- if the string also maps to a
    bucket that carries an assigned code -- flags any printed code whose 3-char
    parent is NOT in that bucket's ``icd10`` list. A mismatch is DATA, not an
    error to swallow: the caller surfaces it. No printed code, or no mapped
    bucket, yields no mismatch.
    """
    printed = parse_icd10(diagnosis)
    if not printed:
        return [], []
    bucket = canonical(diagnosis)
    assigned = set(icd10_for(bucket)) if bucket else set()
    if not assigned:
        return printed, []
    mismatches = [c for c in printed if category_of(c) not in assigned]
    return printed, mismatches

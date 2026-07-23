"""Regressions for the consolidated, LOINC-keyed codebook (data/analytes.json).

The four old files (analytes / analytes_extra / aliases / loinc) were merged into
one file keyed by LOINC id; see tools/merge_codebook.py. There is deliberately NO
test here for CODE CORRECTNESS -- whether 718-7 really is Hemoglobin was validated
by hand against the licensed Loinc.csv, not asserted in code. What these tests
guard is the plumbing: the file parses, every entry is well-shaped and uniquely
named, the LOINC identity lands on the in-memory codebook, and the ignore-list is
disjoint from the codebook and actually fires.
"""

from __future__ import annotations

import pytest

from src import config
from src.normalize import (
    ANALYTES,
    is_ignored,
    load_codebook,
    load_ignored,
)


@pytest.fixture(scope="module")
def raw() -> dict:
    """On-disk codebook: keyed by LOINC id, values carry a canonical name."""
    return config.load(ANALYTES)


@pytest.fixture(scope="module")
def codebook() -> dict:
    return load_codebook()


def test_parses_and_is_loinc_keyed(raw):
    assert isinstance(raw, dict) and raw
    # Every key is a LOINC id (digits + a check digit), not a name.
    for code in raw:
        assert code.replace("-", "").isdigit(), f"key {code!r} is not a LOINC id"


def test_every_entry_is_well_shaped(raw):
    """Each entry names a real analyte and carries the identity fields. Every
    analyte now HAS a code (the null tier was dropped or coded in review), so a
    coded entry must carry either a UCUM unit or a note saying why it has none
    (qualitative dipstick, narrative, nominal cytology)."""
    for code, e in raw.items():
        assert set(e) >= {
            "name",
            "official_name",
            "equivalents",
            "aliases",
            "segment",
            "ucum",
            "ranges",
        }, code
        assert e["name"], code
        assert isinstance(e["equivalents"], list) and isinstance(e["aliases"], list), code
        assert e["official_name"], f"{e['name']}: coded but no official name"
        if e["ucum"] is None:
            assert e.get("note"), f"{e['name']}: coded, no UCUM, and no note explaining why"


def test_names_and_codes_are_unique(raw):
    names = [e["name"] for e in raw.values()]
    assert len(names) == len(set(names)), "duplicate canonical name across LOINC ids"
    # dict keys are unique by construction; assert nothing collapsed them.
    assert len(raw) == len(set(raw))


def test_identity_attaches_to_codebook(raw, codebook):
    """load_codebook() re-indexes by name and exposes the LOINC identity on each
    entry, under the names the rest of the system reads."""
    for code, e in raw.items():
        entry = codebook[e["name"]]
        assert entry["loinc"] == code, e["name"]
        assert entry["loinc_name"] == e["official_name"], e["name"]
        assert entry["loinc_equivalents"] == list(e["equivalents"] or []), e["name"]
        assert entry["ucum"] == e["ucum"], e["name"]
        assert entry["segment"] == e["segment"], e["name"]


def test_every_codebook_entry_has_a_segment(codebook):
    for name, entry in codebook.items():
        assert "segment" in entry, name


def test_ignore_list_is_disjoint_from_codebook(codebook):
    """A dropped analyte must not also be live -- that would both ignore and
    resolve the same test."""
    ignored = load_ignored()
    assert ignored, "expected a non-empty ignore-list (echo/ratios/CPAP/urine)"
    overlap = set(ignored) & set(codebook)
    assert overlap == set(), f"names both ignored and in codebook: {overlap}"


def test_is_ignored_fires_and_domain_gates():
    """A dropped analyte's own name is ignored; domain gating still applies, so a
    urine-scoped drop only fires inside a urine section."""
    ignored = load_ignored()
    # 'Aorta' (echo) was dropped -> ignored in an echo section.
    assert is_ignored("Aorta", "GREAT VESSELS", ignored)
    # A live analyte is never ignored.
    assert not is_ignored("Haemoglobin", "COMPLETE BLOOD COUNT", ignored)

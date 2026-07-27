"""Colloquial <-> clinical condition expansion (src/conditions.py).

"What did she get for a cold" and a discharge summary saying "AURTI" are the
same fact worded two ways -- this is what bridges them for meds.for_condition().
Phase C added a standard ICD-10-CM identity per bucket without re-keying: the
bucket key is still the runtime identity, the code is an added field.
"""

from __future__ import annotations

from src.conditions import (
    canonical,
    canonical_labels,
    category_of,
    expand,
    icd10_for,
    is_ignored,
    is_undecided,
    load_conditions,
    parse_icd10,
    reconcile,
)
from src.conditions import _categories, load_ignore


class TestExpand:
    def test_colloquial_term_expands_to_clinical_shorthand(self) -> None:
        terms = expand("cold")
        assert "AURTI" in terms
        assert "URTI" in terms
        assert "cold" in terms

    def test_clinical_shorthand_expands_back_to_the_bucket(self) -> None:
        """Typing the shorthand directly still finds its siblings -- useful if
        the model itself calls this tool with the coded term."""
        terms = expand("URTI")
        assert "cold" in terms

    def test_phrase_containing_a_known_term_still_matches(self) -> None:
        terms = expand("a really bad cold")
        assert "AURTI" in terms

    def test_colloquial_umbrella_widens_across_its_split_buckets(self) -> None:
        """A colloquial phrase repeated as an alias across 1:1 clinical buckets
        still widens across all of them: 'heart disease' -> CAD + MI + angina."""
        terms = expand("heart disease")
        assert {"CAD", "MI", "angina"} <= set(terms)

    def test_unmapped_term_is_returned_alone(self) -> None:
        """No guessing: an unmapped term is searched literally, not dropped."""
        assert expand("some rare condition nobody mapped") == ["some rare condition nobody mapped"]

    def test_empty_condition_is_returned_alone(self) -> None:
        assert expand("") == [""]


class TestCanonical:
    def test_clinical_variants_collapse_to_one_bucket(self) -> None:
        """The whole point: three ways of writing type 2 diabetes -> one label."""
        for dx in ("T2DM", "Type 2 DM", "TYPE 2 DIABETES MELLITUS"):
            assert canonical(dx) == "type 2 diabetes", dx

    def test_messy_real_string_matches_generic_alias(self) -> None:
        """The map holds only 'HTN'; the real record says 'K/C/O HTN'."""
        assert canonical("K/C/O HTN") == "high blood pressure"

    def test_bare_umbrella_resolves_to_the_first_listed_child(self) -> None:
        """A plain 'diabetes' (no type) resolves to the default child listed
        first (type 2), never a coin flip."""
        assert canonical("diabetes") == "type 2 diabetes"

    def test_unmapped_diagnosis_is_none(self) -> None:
        # A synthetic nonsense string, never a real bucket -- so the condition-review
        # loop legitimately adding new buckets can never make this assertion flap.
        assert canonical("xyzzy fnord syndrome") is None

    def test_empty_is_none(self) -> None:
        assert canonical("") is None

    def test_short_abbreviation_does_not_match_inside_a_word(self) -> None:
        """Token containment, not substring: a bucket term must appear as its
        own whole word, never buried inside another (the expand() guarantee)."""
        assert canonical("arachnoid cyst in the cerebellum") is None


class TestCanonicalLabels:
    def test_variants_dedupe_to_a_single_label(self) -> None:
        assert canonical_labels(["T2DM", "TYPE 2 DIABETES MELLITUS"]) == ["type 2 diabetes"]

    def test_unmapped_kept_raw_and_sorted_with_buckets(self) -> None:
        labels = canonical_labels(["T2DM", "xyzzy fnord syndrome"])
        assert labels == sorted(["type 2 diabetes", "xyzzy fnord syndrome"])

    def test_blanks_are_dropped(self) -> None:
        assert canonical_labels(["", "  ", "HTN"]) == ["high blood pressure"]

    def test_ignored_diagnosis_is_not_a_filter_chip(self, monkeypatch) -> None:
        """An ignored generic (e.g. a BMI reading) maps to no bucket but must NOT
        leak into the dropdown as a raw label -- it stays in the card body only."""
        monkeypatch.setattr("src.conditions.load_ignore", lambda: ("BMI",))
        assert canonical_labels(["BMI - 35-7", "T2DM"]) == ["type 2 diabetes"]


class TestIcd10Identity:
    def test_every_bucket_has_valid_category_codes(self) -> None:
        """Each bucket's icd10 codes must be real 3-char categories in the baked
        table -- the same 'unknown refuses' guarantee LOINC gets in Phase A."""
        cats = _categories()
        for key, entry in load_conditions().items():
            assert entry["icd10"], f"{key} has no icd10 code"
            for code in entry["icd10"]:
                assert code in cats, f"{key}: {code} is not a real ICD-10-CM category"

    def test_icd10_for_returns_the_assigned_code(self) -> None:
        assert icd10_for("high blood pressure") == ("I10",)
        assert icd10_for("type 2 diabetes") == ("E11",)

    def test_icd10_for_unknown_bucket_is_empty(self) -> None:
        assert icd10_for("nonexistent bucket") == ()


class TestParseIcd10:
    def test_extracts_a_printed_code_with_detail(self) -> None:
        assert parse_icd10("Age-related nuclear cataract, bilateral - H25.13") == ["H25.13"]

    def test_extracts_multiple_codes_in_order_deduped(self) -> None:
        codes = parse_icd10("Presence of intraocular lens - Z96.1; palsy - H49.12; Z96.1 again")
        assert codes == ["Z96.1", "H49.12"]

    def test_rejects_vitamin_tokens_that_are_not_categories(self) -> None:
        """The real trap: 'B12' is printed verbatim in diagnosis strings but is
        not an ICD category, so it must be refused, not captured as a code."""
        assert parse_icd10("Wt D def: low nard B12. Vit D deficiency") == []

    def test_does_not_fire_mid_token(self) -> None:
        """'T2DM' must not yield a spurious 'T2D'."""
        assert parse_icd10("T2DM on metformin") == []

    def test_no_code_present_is_empty(self) -> None:
        assert parse_icd10("Type 2 diabetes mellitus") == []

    def test_category_of_strips_detail(self) -> None:
        assert category_of("H25.13") == "H25"
        assert category_of("i10") == "I10"


class TestReconcile:
    def test_printed_code_matching_bucket_has_no_mismatch(self) -> None:
        """'K/C/O HTN - I10' maps to high blood pressure (I10); the printed code
        agrees, so no mismatch is surfaced."""
        printed, mismatches = reconcile("K/C/O HTN - I10")
        assert printed == ["I10"]
        assert mismatches == []

    def test_printed_code_conflicting_with_bucket_is_surfaced(self) -> None:
        """A diagnosis that maps to hypertension (I10) but prints a diabetes code
        (E11) is a data conflict -- surfaced, not swallowed."""
        printed, mismatches = reconcile("HTN - E11.9")
        assert printed == ["E11.9"]
        assert mismatches == ["E11.9"]

    def test_printed_code_without_a_mapped_bucket_is_captured_not_flagged(self) -> None:
        """A cataract code (no bucket tracks cataract) is captured but nothing to
        reconcile against -- no mismatch."""
        printed, mismatches = reconcile("Age-related nuclear cataract, bilateral - H25.13")
        assert printed == ["H25.13"]
        assert mismatches == []

    def test_no_printed_code_is_empty(self) -> None:
        assert reconcile("Type 2 diabetes mellitus") == ([], [])


class TestReviewState:
    """The condition-review triage: mapped / ignored / undecided."""

    def test_mapped_diagnosis_is_not_undecided(self) -> None:
        assert is_undecided("K/C/O HTN") is False

    def test_unmapped_diagnosis_is_undecided(self) -> None:
        # Synthetic nonsense, never a real bucket -- so triage adding buckets can't
        # flap this (an earlier version used "??rsi", which later became a real RSI
        # bucket and broke the test -- the whole reason to use a synthetic here).
        assert is_undecided("xyzzy fnord syndrome") is True
        assert is_undecided("qwzzq blorptitis") is True

    def test_blank_is_not_undecided(self) -> None:
        assert is_undecided("") is False
        assert is_undecided("   ") is False

    def test_ignore_uses_whole_word_token_containment(self, monkeypatch) -> None:
        """A generic ignore term silences its variants ('flat foot' -> 'B/L flat foot')
        but never fires as a substring inside another word."""
        monkeypatch.setattr("src.conditions.load_ignore", lambda: ("flat foot",))
        assert is_ignored("B/L flat foot") is True
        assert is_ignored("flat foot deformity") is True
        assert is_ignored("football injury") is False  # 'foot' not a whole token here
        assert is_undecided("B/L flat foot") is False  # ignored -> decided

    def test_default_ignore_list_is_generic_and_loads(self) -> None:
        """Committed ignore list must load (empty is fine) and never assign a bucket."""
        assert isinstance(load_ignore(), tuple)

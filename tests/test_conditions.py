"""Colloquial <-> clinical condition expansion (src/conditions.py).

"What did she get for a cold" and a discharge summary saying "AURTI" are the
same fact worded two ways -- this is what bridges them for meds.for_condition().
"""

from __future__ import annotations

from src.conditions import canonical, canonical_labels, expand


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

    def test_unmapped_term_is_returned_alone(self) -> None:
        """No guessing: an unmapped term is searched literally, not dropped."""
        assert expand("some rare condition nobody mapped") == ["some rare condition nobody mapped"]

    def test_empty_condition_is_returned_alone(self) -> None:
        assert expand("") == [""]


class TestCanonical:
    def test_clinical_variants_collapse_to_one_bucket(self) -> None:
        """The whole point: three ways of writing diabetes -> one label."""
        for dx in ("T2DM", "Type 2 DM", "TYPE 2 DIABETES MELLITUS"):
            assert canonical(dx) == "diabetes", dx

    def test_messy_real_string_matches_generic_alias(self) -> None:
        """The map holds only 'HTN'; the real record says 'K/C/O HTN'."""
        assert canonical("K/C/O HTN") == "high blood pressure"

    def test_unmapped_diagnosis_is_none(self) -> None:
        assert canonical("MDS/MPN overlap syndrome") is None

    def test_empty_is_none(self) -> None:
        assert canonical("") is None

    def test_short_abbreviation_does_not_match_inside_a_word(self) -> None:
        """Token containment, not substring: a bucket term must appear as its
        own whole word, never buried inside another (the expand() guarantee)."""
        assert canonical("arachnoid cyst in the cerebellum") is None


class TestCanonicalLabels:
    def test_variants_dedupe_to_a_single_label(self) -> None:
        assert canonical_labels(["T2DM", "TYPE 2 DIABETES MELLITUS"]) == ["diabetes"]

    def test_unmapped_kept_raw_and_sorted_with_buckets(self) -> None:
        labels = canonical_labels(["T2DM", "MDS/MPN overlap syndrome"])
        assert labels == sorted(["diabetes", "MDS/MPN overlap syndrome"])

    def test_blanks_are_dropped(self) -> None:
        assert canonical_labels(["", "  ", "HTN"]) == ["high blood pressure"]

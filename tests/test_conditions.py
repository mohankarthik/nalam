"""Colloquial <-> clinical condition expansion (src/conditions.py).

"What did she get for a cold" and a discharge summary saying "AURTI" are the
same fact worded two ways -- this is what bridges them for meds.for_condition().
"""

from __future__ import annotations

from src.conditions import expand


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

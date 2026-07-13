"""Reference ranges: whose range applies, and in which units.

Two bugs live here, both of which flag healthy people as sick:

  * The range separator is a minus sign to a naive regex. '0.6 - 1.2' parsed as
    (0.6, -1.2), so every upper bound in the database was negative and nearly
    every result would have been flagged 'high'. 1604 rows were affected.

  * The lab's range is in the LAB's units; the stored value is converted into
    the codebook's. Checking a T3 of 1.13 ng/mL against the lab's 1.30-3.10
    nmol/L band calls a normal thyroid low.
"""

from __future__ import annotations

import pytest

from src.ingest import _reference_range
from src.normalize import load_codebook
from src.people import Person, flag, flag_observation, reference_range


@pytest.fixture(scope="module")
def codebook() -> dict:
    return load_codebook()


ADULT_M = Person("A", "Adult Male", "male", False)
ADULT_F = Person("B", "Adult Female", "female", False)
CHILD = Person("C", "A Child", "male", True)


class TestRangeParsing:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("0.6 - 1.2", (0.6, 1.2)),  # the separator is NOT a minus sign
            ("0.6-1.2", (0.6, 1.2)),
            ("4.0 – 5.6", (4.0, 5.6)),  # en-dash
            ("70 -100", (70.0, 100.0)),
            ("1,50,000 - 4,10,000", (150000.0, 410000.0)),  # Indian grouping
            ("< 200", (None, 200.0)),
            ("> 40", (40.0, None)),
            ("", (None, None)),
        ],
    )
    def test_parses_without_inventing_negatives(self, text, expected) -> None:
        assert _reference_range(text) == expected

    def test_never_returns_a_negative_upper_bound(self) -> None:
        low, high = _reference_range("0.6 - 1.2")
        assert high is not None and high > 0


class TestWhoseRangeApplies:
    def test_lab_range_wins_over_the_codebook(self, codebook) -> None:
        """Labs revise their ranges and their methods; a frozen sheet does not."""
        low, high, source = reference_range(
            ADULT_M, "Creatinine", codebook, lab_low=0.6, lab_high=1.2
        )
        assert (low, high, source) == (0.6, 1.2, "lab")

    def test_codebook_is_the_fallback_when_the_lab_printed_none(self, codebook) -> None:
        low, high, source = reference_range(ADULT_M, "Creatinine", codebook)
        assert source == "codebook (male)" and (low, high) == (0.7, 1.4)

    def test_sex_specific(self, codebook) -> None:
        _m_low, _m_high, _ = reference_range(ADULT_M, "Uric Acid", codebook)
        f_low, _f_high, _ = reference_range(ADULT_F, "Uric Acid", codebook)
        assert _m_low == 3.5 and f_low == 2.4

    def test_children_get_no_adult_range(self, codebook) -> None:
        """An adult range on a child's bloodwork is confidently wrong in both
        directions. Say nothing instead."""
        low, high, source = reference_range(CHILD, "Creatinine", codebook)
        assert (low, high, source) == (None, None, "none")

    def test_children_do_get_the_labs_own_range(self, codebook) -> None:
        low, high, source = reference_range(
            CHILD, "Creatinine", codebook, lab_low=0.3, lab_high=0.7
        )
        assert (low, high, source) == (0.3, 0.7, "lab")


class TestUnitsMustAgree:
    def test_lab_range_is_checked_against_the_printed_value(self, codebook) -> None:
        """T3 printed as 1.73 nmol/L converts to 1.13 ng/mL for trending. The
        lab's band is 1.30-3.10 nmol/L. Comparing the converted value against the
        lab's band would call a normal thyroid LOW."""
        result, source = flag_observation(
            ADULT_M,
            "T3",
            raw_value="1.73",  # as printed, nmol/L
            value_num=1.13,  # converted, ng/mL
            lab_low=1.30,
            lab_high=3.10,  # the lab's band, nmol/L
            codebook=codebook,
        )
        assert source == "lab"
        assert result == "normal", "must compare printed value to printed range"


class TestFlag:
    @pytest.mark.parametrize(
        "value, low, high, expected",
        [
            (9.47, 0.0, 5.7, "high"),
            (0.52, 0.64, 1.52, "low"),
            (5.2, 4.0, 6.5, "normal"),
            (5.2, None, None, ""),  # no range -> no opinion
            (None, 1.0, 2.0, ""),
        ],
    )
    def test_flag(self, value, low, high, expected) -> None:
        assert flag(value, low, high) == expected


class TestDegenerateLabRange:
    """low == high is not a range.

    Labs print interpretive TABLES for cholesterol, HbA1c and vitamin D
    ("Desirable <200 / Borderline 200-239 / High >240") rather than a band, and
    the parser picks two numbers out of the table. 100 observations carried a
    band like 6.0-6.0. Flagging against that is flagging against nonsense.
    """

    def test_degenerate_lab_range_falls_back_to_the_codebook(self, codebook) -> None:
        low, high, source = reference_range(ADULT_M, "HbA1c", codebook, lab_low=6.0, lab_high=6.0)
        assert source == "codebook (male)"
        assert (low, high) == (0.0, 5.7)

    def test_a_real_band_is_still_used(self, codebook) -> None:
        low, high, source = reference_range(ADULT_M, "HbA1c", codebook, lab_low=4.0, lab_high=5.6)
        assert (low, high, source) == (4.0, 5.6, "lab")

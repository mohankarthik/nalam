"""Medicines: brand->molecule, and the list that only a human can keep true.

The two rules this file exists to defend:

  1. NEVER guess a molecule. An unconfirmed brand keeps its brand name and goes
     to review. A plausible-looking wrong generic is the most dangerous output
     this system could produce.

  2. 'Not listed' is NOT 'stopped'. A discharge summary that omits a long-term
     drug has not stopped it -- it simply didn't repeat it. Inferring otherwise
     would quietly delete drugs a person is still taking.
"""

from __future__ import annotations

import datetime

import pytest

from src.drugs import load_drugs, lookup, molecules
from src.meds import Med, course_ends


@pytest.fixture(scope="module")
def table() -> dict:
    return load_drugs()


class TestBrandToMolecule:
    @pytest.mark.parametrize(
        "printed, expected",
        [
            ("TENEPRIDE M", ["Teneligliptin", "Metformin"]),
            ("GALVUS MET", ["Vildagliptin", "Metformin"]),
            ("GLUCONORM G2", ["Glimepiride", "Metformin"]),
            ("CLOPILET", ["Clopidogrel"]),
            ("Tab. Lasix", ["Furosemide"]),          # form prefix is not the brand
            ("CAP. REALCEF 200MG", ["Cefixime"]),    # strength glued to the name
        ],
    )
    def test_maps(self, table, printed, expected) -> None:
        assert molecules(printed, table) == expected

    def test_display_keeps_the_brand(self, table) -> None:
        """The brand is what's on the strip in the cupboard. Never lose it."""
        d = lookup("TENEPRIDE M", table)
        assert d is not None
        assert d.display == "Teneligliptin + Metformin (TENEPRIDE M)"

    def test_unconfirmed_brand_yields_no_molecule(self, table) -> None:
        d = lookup("GB 29 SR", table)
        assert d is not None and not d.confirmed
        assert molecules("GB 29 SR", table) == []
        assert d.display == "GB 29 SR", "must not acquire a plausible-looking generic"

    def test_unknown_brand_is_not_invented(self, table) -> None:
        assert lookup("SOMETHING NOBODY HAS HEARD OF", table) is None
        assert molecules("SOMETHING NOBODY HAS HEARD OF", table) == []

    def test_a_device_is_not_a_drug(self, table) -> None:
        d = lookup("BIPAP at night", table)
        assert d is not None and d.device
        assert molecules("BIPAP at night", table) == []

    def test_longest_match_wins(self, table) -> None:
        """ECOSPRIN AV (aspirin+atorvastatin) must not be swallowed by ECOSPIRIN."""
        assert molecules("ECOSPRIN AV", table) == ["Aspirin", "Atorvastatin"]
        assert molecules("ECOSPIRIN", table) == ["Aspirin"]


class TestSameDrugDifferentBrand:
    def test_molecule_is_the_identity(self) -> None:
        """LOSAR and LOSARTAN are one drug. GLUCONORM G2 and GEMER are one drug."""
        a = Med("LOSAR", "Losartan", "50 MG", "0-0-1", None, "2023-02-25", "prescribed", "ok")
        b = Med("LOSARTAN", "Losartan", "50 mg", "1-0-1", None, "2023-08-16", "prescribed", "ok")
        assert a.key == b.key

    def test_unknown_molecule_falls_back_to_the_brand(self) -> None:
        """Honest: if we don't know the molecule we cannot claim two brands match."""
        a = Med("GB 29 SR", None, None, "0-1-0", None, "2023-02-25", "prescribed", "review")
        b = Med("HEPAGRSS", None, None, "1-0-1", None, "2023-02-25", "prescribed", "review")
        assert a.key != b.key


class TestCourseEnds:
    """A stated duration is data, not inference. Reading it is not guessing."""

    def test_a_seven_day_course_ends(self) -> None:
        m = Med("UDILIV", None, "300MG", "1-0-1", "X 7. DAYS AF", "2023-02-25", "prescribed", "ok")
        assert course_ends(m) == datetime.date(2023, 3, 4)

    def test_one_month(self) -> None:
        m = Med("PROHANCE", None, None, "1-1-1", "FOR 1 MONTH AP", "2023-02-25", "prescribed", "ok")
        assert course_ends(m) == datetime.date(2023, 3, 27)

    def test_no_duration_stays_open(self) -> None:
        """No stated end means we do NOT know it ended. Keep it, flag it stale."""
        m = Med("ATORVA", "Atorvastatin", "40 mg", "0-0-1", None, "2023-08-16", "prescribed", "ok")
        assert course_ends(m) is None

    def test_to_continue_stays_open(self) -> None:
        m = Med("X", None, None, None, "to continue", "2023-08-16", "prescribed", "ok")
        assert course_ends(m) is None

    @pytest.mark.parametrize(
        "duration, days",
        [
            ("X 7. DAYS AF", 7),     # a stray period between number and unit
            ("X 10DAYS A", 10),      # no space at all
            ("FOR 7 DAY BF", 7),     # singular
            ("X 7 DAYS WITH WATER-", 7),
        ],
    )
    def test_real_handwritten_durations(self, duration, days) -> None:
        """These are verbatim from the discharge summaries. Doctors do not write
        durations the way a regex would like."""
        m = Med("X", None, None, None, duration, "2023-02-25", "prescribed", "ok")
        assert course_ends(m) == datetime.date(2023, 2, 25) + datetime.timedelta(days=days)

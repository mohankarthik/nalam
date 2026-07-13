"""Whose document is this? The highest-stakes question the system asks.

Two opposite failures, and the fix for one CAUSED the other:

  1. A mother's surgical discharge filed in her child's folder, because the folder
     is organised around the pregnancy. The DOCUMENT is right; re-file it.

  2. A premature baby's retinopathy report, filed correctly in the child's folder,
     labelled "B/O ALICE DOE" -- because neonatal records are named for the
     mother. The FOLDER is right; the document names the parent only to identify
     the child. Re-filing it moved an infant's records into his mother's.

So "the document wins over the folder" is true EXCEPT when the document names a
baby. Get this wrong in either direction and one person's medical history ends up
in another person's record.
"""

from __future__ import annotations

import pytest

from src.validator import names_a_baby, patient_matches


class TestNeonatalNaming:
    """'B/O X' means the patient is X's BABY, not X."""

    @pytest.mark.parametrize(
        "printed",
        [
            "B/O BABY OF ALICE DOE",
            "B/O Alice Doe",
            "BABY OF ALICE DOE",
            "Baby of Alice Example",
            "Blo Alice doe",  # OCR: the slash in B/O becomes an l
            "BIO ALICE EXAMPLE",  # OCR: B/O -> BIO
            "Newborn of Alice",
        ],
    )
    def test_recognised_as_a_baby(self, printed: str) -> None:
        assert names_a_baby(printed), f"{printed!r} names a baby, not an adult"

    @pytest.mark.parametrize(
        "printed",
        ["Alice Doe", "MR BOB EXAMPLE", "Mrs. Alice Example", "Bob Example"],
    )
    def test_an_adult_is_not_a_baby(self, printed: str) -> None:
        assert not names_a_baby(printed)

    @pytest.mark.parametrize("age", ["1 Month 14 Days", "3 Days", "6 Weeks", "2 Months"])
    def test_an_infant_age_gives_it_away(self, age: str) -> None:
        """Nobody's mother is six weeks old."""
        assert names_a_baby("Some Ambiguous Name", age)

    @pytest.mark.parametrize("age", ["34 Yr(s)", "84 Year(s)", "45", ""])
    def test_an_adult_age_does_not(self, age: str) -> None:
        assert not names_a_baby("Alice Example", age)


class TestTheDocumentStillWinsOtherwise:
    """The original rule must survive: a mother's surgery in a child's folder
    still gets re-filed to the mother."""

    def test_a_named_adult_still_matches(self) -> None:
        assert patient_matches("Mrs. Alice Doe", "Alice Doe")
        assert patient_matches("ALICE DOE", "Alice Doe")

    def test_a_different_adult_does_not(self) -> None:
        assert not patient_matches("Alice Doe", "Bob Smith")

    def test_a_baby_reference_is_not_a_match_for_the_parent(self) -> None:
        """patient_matches() alone matches 'B/O Alice Doe' to Alice -- which is
        precisely how an infant's records ended up in his mother's file. The
        names_a_baby() guard must run FIRST."""
        assert patient_matches(
            "B/O ALICE DOE", "Alice Doe"
        ), "the name tokens do match -- which is exactly why the guard is needed"
        assert names_a_baby("B/O ALICE DOE"), "and the guard must catch it"


class TestSharedSurnames:
    """A family is where everyone is called the same thing.

    Matching on a shared surname alone once made one relative's document match
    another. In a family health record that is the worst available failure, in
    the one setting where it is most likely.
    """

    SHARED = {"doe"}  # both Alice Doe and Carol Doe have it

    def test_a_surname_alone_is_not_an_identity(self) -> None:
        assert patient_matches("Carol Doe", "Alice Doe"), "tokens do overlap..."
        assert not patient_matches(
            "Carol Doe", "Alice Doe", self.SHARED
        ), "...but only on the surname, which every relative shares"

    def test_a_distinguishing_token_still_matches(self) -> None:
        assert patient_matches("MRS. ALICE DOE", "Alice Doe", self.SHARED)

    def test_completely_different_names_never_match(self) -> None:
        assert not patient_matches("Bob Smith", "Alice Doe", self.SHARED)


class TestAgeIsTheDiscriminator:
    """The label is what handwriting destroys. The age is what settles it.

    A prefix check on "B/O" caught the printed neonatal charts. It did NOT catch
    a handwritten one, where OCR turned "B/O Alice Doe" into "Rlo Alice Dohe"
    -- whose only legible token is the mother's given name. No prefix pattern will
    ever catch that.

    But the same document said the patient was "3m" old. Nobody's mother is three
    months old. Parse the age, and the question answers itself.
    """

    @pytest.mark.parametrize(
        "age, years",
        [
            ("3m", 0.25),
            ("1 Month 14 Days", 1 / 12 + 14 / 365),
            ("6 Weeks", 6 / 52),
            ("3 Days", 3 / 365),
            ("2y", 2.0),
            ("34 Yr(s)", 34.0),
            ("84 Year(s)", 84.0),
        ],
    )
    def test_ages_are_parsed_however_they_are_written(self, age, years) -> None:
        from src.validator import parse_age_years

        got = parse_age_years(age)
        assert got is not None and abs(got - years) < 0.02

    def test_the_document_that_broke_it(self) -> None:
        """OCR of a handwritten neonatal chart. The name is unrecognisable; the
        age is not."""
        assert names_a_baby("Rlo Alice Dohe", "3m"), "3 months old is not a mother"

    def test_an_adult_age_settles_it_the_other_way(self) -> None:
        """A mother's delivery summary genuinely does sit in the child's folder.
        An adult age must still re-file it to her."""
        assert not names_a_baby("Alice Doe", "34 Yr(s)")

    def test_no_age_falls_back_to_the_name(self) -> None:
        assert names_a_baby("B/O ALICE DOE", "")
        assert not names_a_baby("Alice Doe", "")

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
            ("Tab. Lasix", ["Furosemide"]),  # form prefix is not the brand
            ("CAP. REALCEF 200MG", ["Cefixime"]),  # strength glued to the name
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
        """An unconfirmed brand keeps its own name rather than acquiring a
        plausible-looking generic. (GB 29 SR used to live here; a human has since
        confirmed it as methylcobalamin + pregabalin, which is the system working.)"""
        d = lookup("PLATIFY", table)
        assert d is not None and not d.confirmed
        assert molecules("PLATIFY", table) == []
        assert d.display == "PLATIFY", "must not acquire a plausible-looking generic"

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
            ("X 7. DAYS AF", 7),  # a stray period between number and unit
            ("X 10DAYS A", 10),  # no space at all
            ("FOR 7 DAY BF", 7),  # singular
            ("X 7 DAYS WITH WATER-", 7),
        ],
    )
    def test_real_handwritten_durations(self, duration, days) -> None:
        """These are verbatim from the discharge summaries. Doctors do not write
        durations the way a regex would like."""
        m = Med("X", None, None, None, duration, "2023-02-25", "prescribed", "ok")
        assert course_ends(m) == datetime.date(2023, 2, 25) + datetime.timedelta(days=days)


class TestReconciliationRestraint:
    """Only surface what needs deciding.

    Hundreds of prescriptions x half a dozen drugs is thousands of events. If
    every one became a card the reviewer would rubber-stamp them, and a
    rubber-stamped review is worse than none: it launders a guess into a
    decision. So a repeat prescription produces no card, and a self-expiring
    course produces no card.
    """

    def test_a_finite_course_is_not_long_term(self) -> None:
        from src.meds import is_long_term

        antibiotic = Med(
            "FARONEM", "Faropenem", "300", "1-0-1", "X 7 DAYS", "2023-02-25", "prescribed", "ok"
        )
        assert not is_long_term(antibiotic), "the prescription already says when it ends"

    def test_an_open_ended_drug_is_long_term(self) -> None:
        from src.meds import is_long_term

        statin = Med(
            "ATORVA", "Atorvastatin", "40 mg", "0-0-1", None, "2023-08-16", "prescribed", "ok"
        )
        assert is_long_term(statin), "nothing says when this ends, so a human must"

    def test_to_continue_is_long_term(self) -> None:
        from src.meds import is_long_term

        m = Med("X", None, None, None, "to continue", "2023-08-16", "prescribed", "ok")
        assert is_long_term(m)


class TestHistoryHidesNothing:
    """--list and --reconcile filter. --history must not.

    An expired antibiotic is not a CURRENT medication and needs no DECISION, so
    both of those views drop it. But "when did we last give her cetirizine" and
    "what did she get for hand-foot-and-mouth" are exactly what a family health
    record is for, and they need the whole log: short courses, children, one-offs.

    Hidden is not deleted. If this test ever fails, someone has confused the two.
    """

    @pytest.fixture()
    def con(self):
        from src import db

        con = db.connect(":memory:")
        doc = db.upsert_document(
            con,
            subject="A Child",
            source_path="x.pdf",
            doc_type="prescription",
            doc_date="2024-03-01",
            model="test",
            text_layer=True,
        )
        con.execute(
            """INSERT INTO medication_events
                 (document_id, subject, drug, generic, strength, frequency, duration,
                  event, effective, raw_text, entered_by, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                doc,
                "A Child",
                "CETZINE",
                "Cetirizine",
                "5mg",
                "0-0-1",
                "5 days",
                "prescribed",
                "2024-03-01",
                "{}",
                "extractor",
                "ok",
            ),
        )
        con.commit()
        return con

    def test_an_expired_course_is_not_current(self, con) -> None:
        from src import meds

        assert meds.current(con, "A Child") == [], "the 5-day course ended in 2024"

    def test_but_it_is_still_in_the_history(self, con) -> None:
        from src import meds

        rows = meds.history(con, subject="A Child")
        assert len(rows) == 1
        assert rows[0]["effective"] == "2024-03-01"

    def test_searchable_by_molecule_not_just_brand(self, con) -> None:
        from src import meds

        assert len(meds.history(con, drug="cetirizine")) == 1, "molecule"
        assert len(meds.history(con, drug="CETZINE")) == 1, "brand"


class TestCombinationBrandsAreNotTheirBaseDrug:
    """GLYCOMET is metformin. GLYCOMET GP1 is metformin AND glimepiride.

    A two-pass lookup matched the shorter brand first and returned there, so a
    combination antidiabetic was recorded as plain metformin. That is not a
    shorter name for the same drug -- it is a different drug, missing a
    sulfonylurea. Longest match must win.
    """

    def test_the_combination_wins_over_its_base(self, table) -> None:
        from src.drugs import molecules

        assert molecules("T. Glycomet GP1", table) == ["Metformin", "Glimepiride"]
        assert molecules("GLYCOMET", table) == ["Metformin"]

    def test_indian_single_letter_form_prefixes(self, table) -> None:
        """'T.' means tablet. Without stripping it, 'T. Glycomet GP1' does not
        start with 'GLYCOMET' and maps to nothing at all."""
        from src.drugs import molecules

        assert molecules("T. Glycomet GP1", table) == ["Metformin", "Glimepiride"]
        assert molecules("C. Becosules", table) == ["B-complex vitamins"]

    def test_a_drug_merely_starting_with_t_is_safe(self, table) -> None:
        """The single-letter prefix requires the dot, so TELMA is not read as
        'T' + 'ELMA'."""
        from src.drugs import molecules

        assert molecules("TELMA", table) == ["Telmisartan"]


class TestBrandSpelling:
    """A typo in drugs.json is a drug that does not exist.

    'ECOSPIRIN' was written where the brand is 'Ecosprin'. The misspelling
    matched; the correct spelling did not. A post-stroke antiplatelet was
    therefore absent from every "is he on aspirin?" query, silently.
    """

    def test_the_real_spelling_maps(self, table) -> None:
        from src.drugs import molecules

        assert molecules("Ecosprin", table) == ["Aspirin"]
        assert molecules("ECOSPRIN 75", table) == ["Aspirin"]

    def test_the_combination_still_wins(self, table) -> None:
        from src.drugs import molecules

        assert molecules("ECOSPRIN AV", table) == ["Aspirin", "Atorvastatin"]


class TestPunctuationIsNotIdentity:
    """The table says FOLVITE-MB; the prescription says FOLVITE MB.

    Same drug, different punctuation. Left unfolded, a folic-acid + methylcobalamin
    supplement mapped to nothing at all.
    """

    def test_hyphen_and_space_are_the_same_drug(self, table) -> None:
        from src.drugs import molecules

        assert molecules("FOLVITE-MB", table) == ["Folic acid", "Methylcobalamin"]
        assert molecules("TAB FOLVITE MB", table) == ["Folic acid", "Methylcobalamin"]
        assert molecules("Folvite MB", table) == ["Folic acid", "Methylcobalamin"]

    def test_folding_does_not_merge_different_drugs(self, table) -> None:
        """PAN and PAN-DSR are different formulations and must stay apart."""
        from src.drugs import molecules

        assert molecules("PAN", table) == ["Pantoprazole"]
        assert molecules("TAB. PAN - DSR", table) == ["Pantoprazole", "Domperidone"]


class TestKeyIsOrderIndependent:
    """'Methylcobalamin + Pregabalin' and 'Pregabalin + Methylcobalamin' are one
    drug written two ways. Counting them separately put the same molecule on the
    live medication list twice."""

    def test_molecule_order_does_not_create_a_second_drug(self) -> None:
        a = Med(
            "GB 29 SR",
            "Methylcobalamin + Pregabalin",
            None,
            "0-1-0",
            None,
            "2023-02-25",
            "prescribed",
            "ok",
        )
        b = Med(
            "Pevesca Plus",
            "Pregabalin + Methylcobalamin",
            "75mg",
            "0-0-1",
            None,
            "2024-01-01",
            "prescribed",
            "ok",
        )
        assert a.key == b.key

    def test_different_molecules_stay_apart(self) -> None:
        a = Med(
            "X", "Pregabalin + Methylcobalamin", None, None, None, "2024-01-01", "prescribed", "ok"
        )
        b = Med(
            "Y", "Pregabalin + Nortriptyline", None, None, None, "2024-01-01", "prescribed", "ok"
        )
        assert a.key != b.key

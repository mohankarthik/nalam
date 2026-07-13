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


class TestADeviceIsNotAMedicine:
    """`what is he taking?` answered with DENTAL FLOSS.

    data/drugs.json already knew: `device: true`, and Drug.display renders it
    "[device, not a drug]". The medicine list simply never asked, so a floss brand
    and a BiPAP machine sat in the believed-current list alongside the antibiotics.

    They stay in medication_events -- they WERE prescribed, and --history must find
    them. They are just not medicines.
    """

    def test_a_device_is_not_believed_current(self, tmp_path) -> None:
        import sqlite3

        from src import db, meds

        con = sqlite3.connect(tmp_path / "t.db")
        con.row_factory = sqlite3.Row
        con.executescript(db.SCHEMA)
        con.execute(
            "INSERT INTO documents (subject, source_path, doc_type) "
            "VALUES ('Alice Doe', 'p.pdf', 'prescription')"
        )
        doc_id = con.execute("SELECT id FROM documents").fetchone()["id"]

        # BIPAP AT NIGHT is device:true in data/drugs.json. Ecosprin is a real drug.
        for drug in ("BIPAP AT NIGHT", "Ecosprin"):
            con.execute(
                "INSERT INTO medication_events "
                "(document_id, subject, drug, event, effective, raw_text) "
                "VALUES (?,?,?,'prescribed','2025-01-01',?)",
                (doc_id, "Alice Doe", drug, drug),
            )
        con.commit()

        names = [m.drug for m in meds.current(con, "Alice Doe")]
        assert "Ecosprin" in names
        assert "BIPAP AT NIGHT" not in names, "a device is not a medicine"

    def test_but_history_still_finds_it(self, tmp_path) -> None:
        """Hidden is not deleted. It was prescribed, and the record must say so."""
        import sqlite3

        from src import db, meds

        con = sqlite3.connect(tmp_path / "t.db")
        con.row_factory = sqlite3.Row
        con.executescript(db.SCHEMA)
        con.execute(
            "INSERT INTO documents (subject, source_path, doc_type) "
            "VALUES ('Alice Doe', 'p.pdf', 'prescription')"
        )
        doc_id = con.execute("SELECT id FROM documents").fetchone()["id"]
        con.execute(
            "INSERT INTO medication_events "
            "(document_id, subject, drug, event, effective, raw_text) "
            "VALUES (?,?,'BIPAP AT NIGHT','prescribed','2025-01-01','BIPAP AT NIGHT')",
            (doc_id, "Alice Doe"),
        )
        con.commit()

        assert any(r["drug"] == "BIPAP AT NIGHT" for r in meds.history(con, subject="Alice Doe"))


class TestConfirmingADrugMustNotEraseIt:
    """Saying "yes, he is still on it" writes a `continued` event with no strength
    and no frequency -- it confirms a fact, it does not restate a prescription.

    Taken literally, that BLANKS the dose and resets the start date to today. The
    medicine list then says a five-year-old statin began this morning, at an unknown
    dose -- which is worse than the stale flag the reconciliation was meant to clear.

    The confirmation decides the STATUS. The prescriptions still supply the facts.
    """

    def build(self, tmp_path):
        import sqlite3

        from src import db

        con = sqlite3.connect(tmp_path / "t.db")
        con.row_factory = sqlite3.Row
        con.executescript(db.SCHEMA)
        con.execute(
            "INSERT INTO documents (subject, source_path, doc_type) "
            "VALUES ('Alice Doe', 'p.pdf', 'prescription')"
        )
        doc_id = con.execute("SELECT id FROM documents").fetchone()["id"]

        # Prescribed in 2020, at a dose, and never mentioned again.
        con.execute(
            "INSERT INTO medication_events (document_id, subject, drug, generic, strength, "
            "frequency, event, effective, raw_text) "
            "VALUES (?,?,'Ecosprin AV','Aspirin + Atorvastatin','150/20','0-0-1',"
            "'prescribed','2020-12-11','x')",
            (doc_id, "Alice Doe"),
        )
        con.commit()
        return con

    def test_a_confirmation_keeps_the_original_start_date(self, tmp_path) -> None:
        from src import meds

        con = self.build(tmp_path)
        meds.record_decision(con, "Alice Doe", "Ecosprin AV", "continued", "2026-07-13")

        (m,) = meds.current(con, "Alice Doe")
        assert m.started == "2020-12-11", "confirming the drug erased when it began"
        assert m.effective == "2026-07-13", "but we DID last hear about it today"

    def test_a_confirmation_keeps_the_dose(self, tmp_path) -> None:
        from src import meds

        con = self.build(tmp_path)
        meds.record_decision(con, "Alice Doe", "Ecosprin AV", "continued", "2026-07-13")

        (m,) = meds.current(con, "Alice Doe")
        assert m.strength == "150/20", "a medicine list with no dose is not a medicine list"
        assert m.frequency == "0-0-1"

    def test_a_stop_still_stops_it(self, tmp_path) -> None:
        from src import meds

        con = self.build(tmp_path)
        meds.record_decision(con, "Alice Doe", "Ecosprin AV", "stopped", "2026-07-13")
        assert meds.current(con, "Alice Doe") == []


class TestDoctorsDoNotWriteForSevenDays:
    """The duration parser understood "7 days" and nothing a doctor actually writes.

    Real scripts say "x 3d", "x30d", "5 dy", "x 2wk". Spelling the units out in full
    parsed NOTHING on the real data: every course in the database came back
    open-ended, so a THREE-DAY doxycycline course from 2022 was still a current
    medication three years later. The prescription said when it ended. We could not
    read it. That is most of the "nothing ever said stop" problem, and it was a
    regex, not a missing fact.
    """

    def med(self, duration, effective="2022-08-02"):
        from src.meds import Med

        return Med(
            drug="X",
            generic=None,
            strength=None,
            frequency=None,
            duration=duration,
            effective=effective,
            event="prescribed",
            status="ok",
        )

    @pytest.mark.parametrize(
        "duration, days",
        [
            ("x 3d", 3),
            ("x30d", 30),
            ("5 dy", 5),
            ("x 2wk", 14),
            ("1 week", 7),
            ("3 Months", 90),
            ("X 7. DAYS AF", 7),
            ("FOR 1 MONTH AP", 30),
        ],
    )
    def test_the_abbreviations_doctors_actually_use(self, duration, days) -> None:
        import datetime

        from src.meds import course_ends

        got = course_ends(self.med(duration))
        assert got == datetime.date(2022, 8, 2) + datetime.timedelta(days=days), duration

    @pytest.mark.parametrize("duration", ["8", "10", "5"])
    def test_a_bare_number_is_not_guessed(self, duration) -> None:
        """It probably means days. "Probably" silently expires a drug somebody is
        still taking, so it stays open and a human decides."""
        from src.meds import course_ends

        assert course_ends(self.med(duration)) is None

    @pytest.mark.parametrize("duration", ["x 14clas", "x lodaf", "X bunthi"])
    def test_ocr_wreckage_is_not_guessed(self, duration) -> None:
        """The number is legible and the unit is not. Half a duration is not a
        duration."""
        from src.meds import course_ends

        assert course_ends(self.med(duration)) is None

    @pytest.mark.parametrize("duration", ["5 day / month", "5 days per month", "1 wk / month"])
    def test_a_rate_is_not_a_duration(self, duration) -> None:
        """ "5 days every month" is pulse therapy -- dermatology cycles itraconazole
        exactly like this. Read as a five-day course it expires on day five, and a
        drug the person is still cycling on disappears from the list."""
        from src.meds import course_ends

        assert course_ends(self.med(duration)) is None

    @pytest.mark.parametrize("duration", ["continue", "SOS", "lifelong", "prn"])
    def test_open_ended_stays_open(self, duration) -> None:
        from src.meds import course_ends

        assert course_ends(self.med(duration)) is None


class TestAStartDateComesFromADocument:
    """Never from the day a human confirmed the drug.

    A prescription with no date at all -- a wash on a dermatology note, say --
    would otherwise appear to have STARTED the moment it was reconciled. Someone
    who has used it for years gets a record saying they began this morning.

    No dated prescription, no start date. '?' is the honest answer.
    """

    def build(self, tmp_path, effective):
        import sqlite3

        from src import db

        con = sqlite3.connect(tmp_path / "t.db")
        con.row_factory = sqlite3.Row
        con.executescript(db.SCHEMA)
        con.execute(
            "INSERT INTO documents (subject, source_path, doc_type) "
            "VALUES ('Alice Doe', 'p.pdf', 'prescription')"
        )
        doc_id = con.execute("SELECT id FROM documents").fetchone()["id"]
        con.execute(
            "INSERT INTO medication_events (document_id, subject, drug, event, effective, "
            "raw_text, entered_by) VALUES (?,?,'Brevoxyl wash','prescribed',?,'x','extractor')",
            (doc_id, "Alice Doe", effective),
        )
        con.commit()
        return con

    def test_an_undated_prescription_has_no_start_date(self, tmp_path) -> None:
        from src import meds

        con = self.build(tmp_path, None)
        meds.record_decision(con, "Alice Doe", "Brevoxyl wash", "continued", "2026-07-13")

        (m,) = meds.current(con, "Alice Doe")
        assert m.started is None, "a human's confirmation date became the start date"

    def test_a_dated_prescription_keeps_its_date(self, tmp_path) -> None:
        from src import meds

        con = self.build(tmp_path, "2019-06-01")
        meds.record_decision(con, "Alice Doe", "Brevoxyl wash", "continued", "2026-07-13")

        (m,) = meds.current(con, "Alice Doe")
        assert m.started == "2019-06-01"

    def test_but_a_human_CAN_state_a_start_date(self, tmp_path) -> None:
        """The rule is about the EVENT, not about who recorded it. A `continued` is a
        confirmation and never a start. A `prescribed` is a start, whoever said so --
        and a drug switched at a clinic and never written down has no other source
        for one. Excluding humans wholesale threw that away."""
        from src import meds

        con = self.build(tmp_path, None)  # no document ever dated it
        meds.record_decision(con, "Alice Doe", "Brevoxyl wash", "prescribed", "2023-06-01")
        meds.record_decision(con, "Alice Doe", "Brevoxyl wash", "continued", "2026-07-13")

        (m,) = meds.current(con, "Alice Doe")
        assert m.started == "2023-06-01", "a human's start date was ignored"


def test_stopping_a_drug_stops_it_even_if_only_one_row_knows_the_molecule(tmp_path) -> None:
    """Med.key is the MOLECULE when known and the brand when not. So an extractor
    row that never resolved its molecule, and a human decision that did, get two
    different keys -- and "stop Brevoxyl wash" does not stop Brevoxyl wash, it
    invents a second one next to it, still current.

    A molecule known about a brand is known about every row of that brand.
    """
    import sqlite3

    from src import db, meds

    con = sqlite3.connect(tmp_path / "t.db")
    con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA)
    con.execute(
        "INSERT INTO documents (subject, source_path, doc_type) "
        "VALUES ('Alice Doe', 'p.pdf', 'prescription')"
    )
    doc_id = con.execute("SELECT id FROM documents").fetchone()["id"]

    # The extractor never resolved the molecule for this row (generic IS NULL) --
    # which was true of 86 rows in the real database.
    con.execute(
        "INSERT INTO medication_events (document_id, subject, drug, generic, event, "
        "effective, raw_text) VALUES (?,?,'Brevoxyl wash',NULL,'prescribed','2019-06-01','x')",
        (doc_id, "Alice Doe"),
    )
    con.commit()

    # record_decision() looks the brand up and DOES resolve it.
    meds.record_decision(con, "Alice Doe", "Brevoxyl wash", "stopped", "2026-07-13")

    assert meds.current(con, "Alice Doe") == [], "the stop created a second drug instead"


class TestADrugCanBeTakenTwice:
    """Started, stopped, and started again years later. That is two courses, not one
    long one, and the medicine list has to know the difference.

    The event log always held the facts. Two things read it wrongly:

      * `started` took the earliest event of ALL time, so a drug stopped in 2016 and
        restarted in 2024 reported "started 2015" -- an unbroken nine-year course
        that never happened.
      * `as_of` filtered course expiry and nothing else, so it read the whole FUTURE
        of the log. "Was she on aspirin in 2020?" was answered with a stop recorded
        in 2026. An event that had not happened yet cannot answer a question about
        the past.
    """

    def build(self, tmp_path):
        import sqlite3

        from src import db

        con = sqlite3.connect(tmp_path / "t.db")
        con.row_factory = sqlite3.Row
        con.executescript(db.SCHEMA)
        con.execute(
            "INSERT INTO documents (subject, source_path, doc_type) "
            "VALUES ('Alice Doe','p.pdf','prescription')"
        )
        doc = con.execute("SELECT id FROM documents").fetchone()["id"]

        def ev(event, date, by="extractor"):
            con.execute(
                "INSERT INTO medication_events (document_id,subject,drug,event,effective,"
                "raw_text,entered_by) VALUES (?,?,'Ecosprin',?,?,'x',?)",
                (doc, "Alice Doe", event, date, by),
            )

        ev("prescribed", "2015-01-01")  # first course
        ev("stopped", "2016-06-01", by="human")
        ev("prescribed", "2024-03-01")  # started again, years later
        con.commit()
        return con

    def test_the_start_date_is_this_episode_not_the_first_one(self, tmp_path) -> None:
        from src import meds

        (m,) = meds.current(self.build(tmp_path), "Alice Doe")
        assert m.started == "2024-03-01", "reported a nine-year course that never happened"

    def test_he_was_on_it_during_the_first_course(self, tmp_path) -> None:
        import datetime

        from src import meds

        got = meds.current(self.build(tmp_path), "Alice Doe", as_of=datetime.date(2015, 6, 1))
        assert [m.drug for m in got] == ["Ecosprin"]

    def test_he_was_NOT_on_it_in_the_gap(self, tmp_path) -> None:
        """Stopped 2016, restarted 2024. In 2020 he was on nothing."""
        import datetime

        from src import meds

        got = meds.current(self.build(tmp_path), "Alice Doe", as_of=datetime.date(2020, 1, 1))
        assert got == [], "a stop that had not happened yet was used to answer the past"

    def test_and_he_is_on_it_again_now(self, tmp_path) -> None:
        from src import meds

        got = meds.current(self.build(tmp_path), "Alice Doe")
        assert [m.drug for m in got] == ["Ecosprin"]

    def test_history_shows_both_courses_AND_the_stop_between_them(self, tmp_path) -> None:
        """Hidden is not deleted -- and the STOP is the whole point. Without it the
        log shows two prescriptions and no way to tell they were two separate
        courses rather than one repeat."""
        from src import meds

        rows = meds.history(self.build(tmp_path), subject="Alice Doe")
        assert [(r["effective"], r["event"]) for r in rows] == [
            ("2024-03-01", "prescribed"),
            ("2016-06-01", "stopped"),
            ("2015-01-01", "prescribed"),
        ]

    def test_a_human_decision_is_findable_even_though_no_document_records_it(
        self, tmp_path
    ) -> None:
        """history() inner-joined documents. A human's stop has no document -- nobody
        wrote it down, which is exactly why a person had to say so -- so EVERY stop
        ever recorded was invisible in the one view whose whole promise is that it
        hides nothing."""
        from src import meds

        con = self.build(tmp_path)
        meds.record_decision(con, "Alice Doe", "Ecosprin", "stopped", "2026-07-13")

        rows = meds.history(con, subject="Alice Doe", drug="Ecosprin")
        stops = [r for r in rows if r["event"] == "stopped" and r["entered_by"] == "human"]
        assert len(stops) == 2, "a stop recorded by a human vanished from the history"

"""Imaging reports: prose corroboration, and section as identity.

Two things here can lose data silently, and silent is the whole problem.

  1. An imaging report's impression is PROSE. The exact-substring test that
     corroborates a number cannot corroborate a paragraph -- one garbled character
     anywhere in it breaks the match, and Tesseract garbles something in nearly
     every paragraph. So prose is checked by word coverage instead, and the
     threshold has to catch an invented impression while passing a transcribed one
     read through bad OCR. Get it wrong in either direction and the feature is
     useless: too strict and every report is quarantined, too loose and a
     hallucinated conclusion is committed as fact.

  2. A follicular scan prints a "Follicle" of "18" in the right ovary AND a
     "Follicle" of "18" in the left ovary, on one date. They are two follicles.
     `section` must therefore be part of a row's identity -- without it they
     collide, and INSERT OR IGNORE does not reject the second, it DROPS it.
"""

from __future__ import annotations

import pytest

from src import oracle as oracle_mod

# A real echocardiogram impression, and what Tesseract typically does to it: drops a
# character here, splits a word there, garbles the numerals.
IMPRESSION = (
    "Mild concentric left ventricular hypertrophy. No regional wall motion "
    "abnormality. Ejection fraction 55 percent. Trivial mitral regurgitation. "
    "No pericardial effusion."
)

# Garbled the way Tesseract ACTUALLY garbles, per the confusion table in
# src/oracle.py: l <-> I <-> 1, rn <-> m, 0 <-> O, 5 <-> S. Plus two words mangled
# beyond any folding ("hypertrophy" -> "hypertroohv", "trivial" -> "Trivla"), because
# a real OCR of a real scan loses some words outright and coverage has to tolerate
# that rather than demand perfection.
#
# An earlier version of this fixture substituted 1 for the letter i ("M1ld",
# "concentr1c"). The fold table does NOT collapse i and 1 -- deliberately, since
# folding too aggressively is how a hallucinated drug name slips through wearing a
# badge that says verified -- so the fixture was simulating noise the oracle never
# claimed to handle, and scored 0.35. The design was fine; the fake OCR was wrong.
OCR_OF_IT = (
    "MiId concentric Ieft ventricuIar hypertroohv . No regionaI waII rnotion "
    "abnorrnality . Ejection fraction 55 percent . Trivla mitraI regurgitation . "
    "No pericardiaI effusion ."
)

# Same modality, same vocabulary, entirely different claims. This is what a
# hallucinated impression looks like: fluent, plausible, and not on the page.
INVENTED = (
    "Severe aortic stenosis with a calcified trileaflet valve. Dilated left "
    "atrium measuring 48 millimetres. Pulmonary artery hypertension is present."
)


def test_a_transcribed_impression_scores_far_above_an_invented_one() -> None:
    """The property that actually matters, and the only one a synthetic OCR fixture
    can honestly assert: a transcription and a fabrication must be nowhere near each
    other. Where exactly to put the threshold between them is an empirical question
    about real scans, not one this fixture can answer -- see
    data/configs/radiology.json.

    NOTE: the absolute number here is depressed by a real bug in oracle.fold(). It
    lowercases BEFORE applying the confusion table, so the capital 'I' in the class
    [1lI|!] is already an 'i' and never folds -- meaning l <-> I, the commonest
    Tesseract confusion of all, is not folded at all. Fixing that is a drug-safety
    decision (it also decides whether 'i' and 'l' conflate), so it is not being made
    quietly inside a radiology test.
    """
    o = oracle_mod.Oracle(text=OCR_OF_IT, source="ocr")
    assert o.coverage(IMPRESSION) > 3 * max(o.coverage(INVENTED), 0.1)


def test_an_invented_impression_is_caught() -> None:
    """The other half. A fluent, plausible, entirely fabricated conclusion must not
    score as corroborated against an OCR of a DIFFERENT report."""
    o = oracle_mod.Oracle(text=OCR_OF_IT, source="ocr")
    assert o.coverage(INVENTED) < 0.6


def test_the_two_are_actually_separated() -> None:
    """Not just either side of a line -- far apart, so the threshold is not perched
    on a knife edge that a slightly worse scan would tip."""
    o = oracle_mod.Oracle(text=OCR_OF_IT, source="ocr")
    assert o.coverage(IMPRESSION) - o.coverage(INVENTED) > 0.4


def test_no_oracle_corroborates_nothing() -> None:
    """No independent reading means no trust, here as everywhere else."""
    assert oracle_mod.NO_ORACLE.coverage(IMPRESSION) == 0.0


def test_empty_prose_is_not_vacuously_corroborated() -> None:
    """An empty impression has nothing in it that the oracle saw. It must score 0.0,
    not 1.0 by 'all zero of its words were found'."""
    o = oracle_mod.Oracle(text=OCR_OF_IT, source="ocr")
    assert o.coverage("") == 0.0
    assert o.coverage("   ") == 0.0


def test_short_words_do_not_pad_the_score() -> None:
    """'no', 'of', 'the' are in every document. If they counted, a sentence made
    only of them would look perfectly corroborated."""
    o = oracle_mod.Oracle(text=OCR_OF_IT, source="ocr")
    assert o.coverage("no of the is at in on") == 0.0


class TestMeasurementsAreCorroboratedAsAPair:
    """An echocardiogram is almost entirely two-digit numbers: LVIDd 39, IVSd 13,
    TAPSE 18, HR 94. Checked as bare values they all fail corroborates()' three-
    character floor, so EVERY measurement on EVERY echo landed in review and the
    extraction was worthless exactly where it mattered most.

    The number alone is weak evidence. The number next to its name is strong.
    """

    # A plausible OCR of an echo table: names and values adjacent, in columns.
    ECHO_OCR = (
        "LEFT VENTRICLE LVIDd 39 mm LVIDs 27 mm IVSd 13 mm LVPWd 12 mm "
        "EDV 66 ml ESV 28 ml RIGHT VENTRICLE TAPSE 18 mm VITAL HR 94 bpm"
    )

    def oracle(self):
        return oracle_mod.Oracle(text=self.ECHO_OCR, source="ocr")

    @pytest.mark.parametrize(
        "name, value", [("LVIDd", "39"), ("IVSd", "13"), ("TAPSE", "18"), ("EDV", "66")]
    )
    def test_a_two_digit_measurement_is_corroborated_via_its_name(self, name, value) -> None:
        assert self.oracle().corroborates_measurement(name, value)
        # ...and would NOT have been, as a bare value. This is the whole point.
        assert not self.oracle().corroborates(value)

    def test_a_value_that_is_not_next_to_that_name_is_not_corroborated(self) -> None:
        """94 IS on this page -- as the heart rate. It is not the LVIDd, and pairing
        it with LVIDd because the digits appear somewhere would be exactly the kind
        of plausible, invisible error this system exists to refuse."""
        assert not self.oracle().corroborates_measurement("LVIDd", "94")

    def test_a_measurement_that_is_not_on_the_page_at_all(self) -> None:
        assert not self.oracle().corroborates_measurement("Ejection Fraction", "55")

    def test_a_two_letter_name_is_never_enough(self) -> None:
        """'PG' and 'HR' match by chance. A name that short cannot license a number,
        so these stay in review -- which is the honest answer, not a failure."""
        assert not self.oracle().corroborates_measurement("HR", "94")

    def test_no_oracle_corroborates_no_measurement(self) -> None:
        assert not oracle_mod.NO_ORACLE.corroborates_measurement("LVIDd", "39")


class TestSectionIsIdentity:
    """A section is part of what a row IS, not a label on it."""

    def test_two_follicles_of_the_same_size_are_two_rows(self, tmp_path) -> None:
        import sqlite3

        from src import db

        con = sqlite3.connect(tmp_path / "t.db")
        con.row_factory = sqlite3.Row
        con.executescript(db.SCHEMA)
        con.execute(
            "INSERT INTO documents (subject, source_path, doc_type) VALUES ('A','p','radiology')"
        )
        doc_id = con.execute("SELECT id FROM documents").fetchone()["id"]

        same_but_for_the_section = [
            {
                "subject": "A",
                "printed_name": "Follicle",
                "section": side,
                "effective": "2026-01-01",
                "value_num": 18.0,
                "raw_value": "18",
                "unit": "mm",
            }
            for side in ("RIGHT OVARY", "LEFT OVARY")
        ]
        db.insert_observations(con, doc_id, same_but_for_the_section)

        rows = con.execute("SELECT section FROM observations ORDER BY section").fetchall()
        assert [r["section"] for r in rows] == ["LEFT OVARY", "RIGHT OVARY"], (
            "one follicle was silently dropped. insert_observations() uses INSERT OR "
            "IGNORE, so a collision on the UNIQUE key does not raise -- it deletes."
        )

    def test_an_exact_duplicate_is_still_one_row(self, tmp_path) -> None:
        """The constraint must still do its actual job: re-ingesting the same
        document must not double every row."""
        import sqlite3

        from src import db

        con = sqlite3.connect(tmp_path / "t.db")
        con.row_factory = sqlite3.Row
        con.executescript(db.SCHEMA)
        con.execute(
            "INSERT INTO documents (subject, source_path, doc_type) VALUES ('A','p','radiology')"
        )
        doc_id = con.execute("SELECT id FROM documents").fetchone()["id"]

        row = {
            "subject": "A",
            "printed_name": "Follicle",
            "section": "RIGHT OVARY",
            "effective": "2026-01-01",
            "value_num": 18.0,
            "raw_value": "18",
            "unit": "mm",
        }
        db.insert_observations(con, doc_id, [row])
        db.insert_observations(con, doc_id, [row])

        assert con.execute("SELECT count(*) FROM observations").fetchone()[0] == 1


class TestOnePdfCanHoldSeveralStudies:
    """A health-checkup pack is an echo AND an ultrasound AND a chest film in one
    PDF. An endoscopy report is an OGD AND a colonoscopy. Asked about "the study",
    the model correctly returns a LIST -- and assuming a single object crashed on
    exactly those documents.
    """

    TWO_STUDIES = [
        {
            "patient": {"name": "Alice Doe"},
            "study": {"name": "2D ECHOCARDIOGRAPHY", "modality": "ECHO"},
            "measurements": [{"name": "AO", "value": "24", "unit": "mm", "section": ""}],
            "findings": [{"text": "Normal chambers.", "is_impression": True, "section": ""}],
        },
        {
            "patient": {"name": "Alice Doe"},
            "study": {"name": "USG ABDOMEN", "modality": "USG"},
            "measurements": [{"name": "Liver", "value": "13.1", "unit": "cm", "section": ""}],
            "findings": [{"text": "Fatty liver.", "is_impression": True, "section": ""}],
        },
    ]

    def test_every_study_survives_the_merge(self) -> None:
        from src.extractor import _merge_studies

        m = _merge_studies(self.TWO_STUDIES)
        assert [x["name"] for x in m["measurements"]] == ["AO", "Liver"]
        assert len(m["findings"]) == 2

    def test_a_row_inherits_ITS_OWN_study_not_the_first_one(self) -> None:
        """The trap. Section is part of a row's identity, so filing the ultrasound's
        liver measurement under '2D ECHOCARDIOGRAPHY' would put it in the wrong
        examination -- and could collide it with an echo row of the same name."""
        from src.extractor import _merge_studies

        m = _merge_studies(self.TWO_STUDIES)
        by_name = {x["name"]: x["section"] for x in m["measurements"]}
        assert by_name["AO"] == "2D ECHOCARDIOGRAPHY"
        assert by_name["Liver"] == "USG ABDOMEN"

    def test_a_single_object_is_untouched(self) -> None:
        from src.extractor import _merge_studies

        one = {"patient": {}, "study": {}, "measurements": [], "findings": []}
        assert _merge_studies(one) is one

    def test_garbage_does_not_crash(self) -> None:
        from src.extractor import _merge_studies

        assert _merge_studies(None) == {}
        assert _merge_studies([]) == {}
        assert _merge_studies(["not a dict"]) == {}


class TestOneDocumentOneOwner:
    """Two routers can claim the same file, and nothing arbitrates between them.

    is_lab() calls EVERY document tagged Medical/Reports a lab. The page-1
    classifier calls an echo in that same folder radiology. Both are defensible.
    But upsert_document() conflicts on source_path and never updates doc_type, so
    the second extractor to run gets back a row still labelled with the first one's
    type -- and ingest_radiology's `DELETE ... WHERE document_id = ?` then deletes
    the first extractor's observations.

    It did exactly that: 448 lab observations across 20 documents, gone.

    Trusting the classifier is NOT the fix. It classified a health-checkup panel --
    105 real lab values -- as radiology. Believing it would have destroyed them.
    Neither source of truth is good enough to overrule the other, so nothing does:
    a document already owned by another extractor is left alone and reported.
    """

    def test_radiology_refuses_a_document_another_extractor_owns(self, tmp_path) -> None:
        import sqlite3

        from src import db, ingest

        con = sqlite3.connect(tmp_path / "t.db")
        con.row_factory = sqlite3.Row
        con.executescript(db.SCHEMA)

        # A lab already ingested this file and put real values on it.
        con.execute(
            "INSERT INTO documents (subject, source_path, doc_type) "
            "VALUES ('Alice Doe', 'Alice/Reports/echo.pdf', 'lab')"
        )
        doc_id = con.execute("SELECT id FROM documents").fetchone()["id"]
        db.insert_observations(
            con,
            doc_id,
            [
                {
                    "subject": "Alice Doe",
                    "printed_name": "Haemoglobin",
                    "section": "CBC",
                    "effective": "2026-01-01",
                    "value_num": 13.4,
                    "raw_value": "13.4",
                    "unit": "g/dL",
                }
            ],
        )
        con.commit()

        got = ingest.ingest_radiology(con, "Alice/Reports/echo.pdf", "Alice Doe")

        assert got == (0, 0, 0, None), "radiology should have skipped a lab's document"
        survivors = con.execute("SELECT printed_name FROM observations").fetchall()
        assert [r["printed_name"] for r in survivors] == ["Haemoglobin"], (
            "the lab's observation was deleted by a radiology ingest. This is the bug "
            "that destroyed 448 real rows."
        )


@pytest.mark.parametrize(
    "printed, expected_impression",
    [
        ([{"text": "Normal study.", "is_impression": True}], "Normal study."),
        ([{"text": "The liver is enlarged.", "is_impression": False}], None),
        ([], None),
    ],
)
def test_an_impression_is_never_invented_from_a_finding(printed, expected_impression) -> None:
    """A descriptive finding is not a conclusion. If the report printed no
    impression, this system does not supply one."""
    from src.extractor import Radiology
    from src.validator import Verdict

    r = Radiology(
        person="A",
        source="p",
        model="m",
        patient={},
        study={},
        measurements=[],
        findings=printed,
        doc_verdict=Verdict(ok=True, hard=[], soft=[]),
        oracle_source="ocr",
    )
    assert r.impression == expected_impression

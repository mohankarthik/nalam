"""Regressions for the analyte matcher.

Every case here is a bug that actually shipped into a run and was caught by the
golden test. They are cheap to keep and expensive to rediscover: each one is a
wrong number quietly landing in a medical record.
"""

from __future__ import annotations

import pytest

from src.normalize import load_codebook, match, parse_value, values_agree


@pytest.fixture(scope="module")
def codebook() -> dict:
    return load_codebook()


class TestSectionGating:
    """A Master Health Checkup bundles five examinations. Names collide across
    them and mean entirely different things.

    These once asserted None -- refuse rather than guess -- because the codebook
    had no urine analytes to route to. It has them now, so the same input
    resolves to the RIGHT test instead of being discarded. The safety property is
    unchanged and is what actually matters: a urine dipstick never becomes a
    blood count.
    """

    @pytest.mark.parametrize(
        "printed, section, expected",
        [
            # 'RBC' under URINE ROUTINE is a dipstick finding ("Negative"),
            # under CBC it is a cell count (5.83). Matching them together once
            # wrote "Negative" where a blood count belonged.
            ("RBC", "URINE ROUTINE", "Urine RBC"),
            ("RBC", "COMPLETE BLOOD COUNT", "RBC"),
            ("ALBUMIN", "URINE ROUTINE", "Urine Protein"),
            ("ALBUMIN", "BIOCHEMISTRY", "Albumin"),
            # 'Impression' exists in both the eye exam and the abdominal USG.
            ("Impression", "ULTRASOUND ABDOMEN", None),
            ("Impression", "OPHTHALMOLOGY", "Impression"),
            ("EF", "2D ECHOCARDIOGRAPHY", "EF"),
        ],
    )
    def test_cross_section_matches_are_refused(
        self, codebook: dict, printed: str, section: str, expected: str | None
    ) -> None:
        assert match(printed, codebook, section) == expected


class TestQualifierIsIdentity:
    """'serum', 'direct', 'total' are NOT noise words.

    Stripping them once reduced 'Direct Bilirubin' to {bilirubin}, which then
    also matched Total Bilirubin and Indirect Bilirubin -- putting one test's
    value under another test's name.
    """

    @pytest.mark.parametrize(
        "printed, expected",
        [
            ("DIRECT BILIRUBIN", "Direct Bilirubin"),
            ("TOTAL BILIRUBIN", "Total Bilrubin"),  # codebook spells it 'Bilrubin'
            # Now a tracked analyte (data/analytes_extra.json); the invariant it
            # guards is that it resolves to Indirect and never to Direct/Total.
            ("INDIRECT BILIRUBIN", "Indirect Bilirubin"),
        ],
    )
    def test_qualifiers_distinguish_tests(
        self, codebook: dict, printed: str, expected: str | None
    ) -> None:
        assert match(printed, codebook, "BIOCHEMISTRY") == expected


class TestAliases:
    @pytest.mark.parametrize(
        "printed, expected",
        [
            ("HbA1c (Glycosylated Hemoglobin)", "HbA1c"),
            ("Glycosylated Haemoglobin", "HbA1c"),
            ("AST", "SGOT"),
            ("ALT", "SGPT"),
            ("TIBC", "Iron Binding Capacity (PSAP)"),
            ("PCV", "Packed cell volume"),
            ("Thyroid Stimulating Hormone", "TSH"),
            ("Serum FERRITIN", "Serum Ferritin"),
            ("PROSTATE SPECIFIC ANTIGEN (PSA)", "PSA"),
        ],
    )
    def test_lab_names_reach_the_codebook(
        self, codebook: dict, printed: str, expected: str
    ) -> None:
        assert match(printed, codebook, "BIOCHEMISTRY") == expected


class TestValueParsing:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("112 #", 112.0),  # labs flag abnormal results; the flag is not data
            ("206*", 206.0),
            ("15.4 H", 15.4),
            ("1,81,000", 181000.0),  # Indian digit grouping
            ("5.20", 5.20),
            ("< 0.5", 0.5),  # censored: keep the bound, raw text preserved elsewhere
        ],
    )
    def test_numbers(self, raw: str, expected: float) -> None:
        assert parse_value(raw)[0] == expected

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Not Reactive", "negative"),
            ("Negative", "negative"),
            ("Nil", "negative"),
            ("Reactive", "positive"),
            ("Positive", "positive"),
        ],
    )
    def test_qualitative_synonyms(self, raw: str, expected: str) -> None:
        assert parse_value(raw)[1] == expected

    def test_synonyms_agree(self) -> None:
        # The sheet says "Not Reactive"; the lab printed "Negative". Same result.
        assert values_agree("Negative", "Not Reactive")

    def test_formatting_differences_agree(self) -> None:
        assert values_agree("18.00", "18")
        assert values_agree("0.380", "0.38")

    def test_real_differences_disagree(self) -> None:
        assert not values_agree("63", "81")
        assert not values_agree("Negative", "5.83")


class TestSubSectionHeadings:
    """Reports label sections with sub-headings, not domain words.

    'GREAT VESSELS' and 'M-MODE MEASUREMENTS' never say "echo", so every echo
    measurement the user actually tracks (EF, LVIDD, Aorta) fell back to 'blood'
    and could not match its own analyte. And 'MICROSCOPIC EXAMINATION' never says
    "urine", so a urine RBC was classified as blood -- one duplicate-guard away
    from being recorded as a blood cell count.
    """

    @pytest.mark.parametrize(
        "printed, section, expected",
        [
            ("AORTA", "GREAT VESSELS", "Aorta"),
            ("LVIDd", "M-MODE MEASUREMENTS", "LVIDD"),
            ("IVSd", "M-MODE MEASUREMENTS", "IVSD"),
            ("EF", "M-MODE MEASUREMENTS", "EF"),
            ("MITRAL VALVE", "VALVES", "Mitral Valve"),
            # Urine findings -- must reach the urine analyte, NEVER the blood one.
            ("RBC", "MICROSCOPIC EXAMINATION", "Urine RBC"),
            ("RBC", "Microscopy", "Urine RBC"),
            ("Glucose", "Complete Urine Analysis", "Urine Glucose"),
            ("RBC", "COMPLETE BLOOD COUNT", "RBC"),  # the blood one still lands
        ],
    )
    def test_subsection_headings_resolve_to_the_right_domain(
        self, codebook: dict, printed: str, section: str, expected: str | None
    ) -> None:
        assert match(printed, codebook, section) == expected


class TestUrineVsBloodRouting:
    """A urine dipstick and a blood count share printed names but are not the
    same test. The codebook is keyed by name, so the urine ones are named
    'Urine X' and carry 'X' as an alias; domain gating routes the printed name.

    If this ever breaks, a dipstick reading lands in a blood cell count.
    """

    @pytest.mark.parametrize(
        "printed, section, expected",
        [
            ("RBC", "MICROSCOPIC EXAMINATION", "Urine RBC"),
            ("RBC", "COMPLETE BLOOD COUNT", "RBC"),
            ("WBC", "Microscopy", "Urine Pus Cells"),
            ("WBC", "COMPLETE BLOOD COUNT", "WBC"),
            ("Protein", "Complete Urine Analysis", "Urine Protein"),
            ("Albumin", "Complete Urine Analysis", "Urine Protein"),
            ("ALBUMIN", "BIOCHEMISTRY", "Albumin"),
            ("Glucose", "Complete Urine Analysis", "Urine Glucose"),
        ],
    )
    def test_same_name_routes_by_section(
        self, codebook: dict, printed: str, section: str, expected: str
    ) -> None:
        assert match(printed, codebook, section) == expected


class TestExtraAnalytes:
    """Categories the master sheet never had: other people in the family have
    tests it was never built for."""

    @pytest.mark.parametrize(
        "printed, section, expected",
        [
            ("Average AHI", "Auto Bi-Level Summary", "AHI"),
            ("Average 90% IPAP", "Sleep Therapy", "IPAP"),
            ("Average Hypopnea Index", "Hypopnea And RERA Index", "Hypopnea Index"),
            ("RDW-CV", "HAEMATOLOGY", "RDW-CV"),
            ("MPV", "HAEMATOLOGY", "MPV"),
            # The absolute count is a different test from the percentage, and
            # must not swallow it.
            ("Absolute Neutrophils Count", "HAEMATOLOGY", "Absolute Neutrophil Count"),
            ("Neutrophils", "HAEMATOLOGY", "Neutrophil"),
            ("LVEF", "M-MODE MEASUREMENTS", "EF"),
            ("QRS", "ELECTROCARDIOGRAPHY", "QRS Duration"),
        ],
    )
    def test_new_categories_resolve(
        self, codebook: dict, printed: str, section: str, expected: str
    ) -> None:
        assert match(printed, codebook, section) == expected

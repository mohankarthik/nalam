"""Regressions for unit conversion.

A 2021 report printed Vitamin D as "31.29 nmol/L" while the codebook keeps it in
ng/mL with a 30-80 range. Stored unconverted, that reading looks normal against
a scale it does not belong to. This is the single easiest way to put a
plausible, badly wrong number into a medical record.
"""

from __future__ import annotations

import pytest

from src.units import convert, load_units


@pytest.fixture(scope="module")
def units() -> dict:
    return load_units()


class TestConversion:
    @pytest.mark.parametrize(
        "analyte, value, unit, expected",
        [
            ("Vitamin D", 31.29, "nmol/L", 12.53),  # the one that started this
            ("Platelet Count", 215.0, "10^3/uL", 215000.0),
            ("WBC", 6.8, "10^3/uL", 6800.0),
            ("T4", 8.08, "micg/dl", 103.99),
            ("Fasting Blood Sugar", 5.5, "mmol/L", 99.09),
        ],
    )
    def test_converts_to_codebook_units(
        self, units: dict, analyte: str, value: float, unit: str, expected: float
    ) -> None:
        got, _canonical, reason = convert(analyte, value, unit, units)
        assert reason is None
        assert got == pytest.approx(expected, rel=0.001)

    @pytest.mark.parametrize("unit", ["mg/dL", "mg/dl", "MG/DL", "mg / dL"])
    def test_unit_spelling_is_irrelevant(self, units: dict, unit: str) -> None:
        got, _canonical, reason = convert("Cholesterol", 170.0, unit, units)
        assert reason is None and got == 170.0

    def test_matching_unit_passes_through(self, units: dict) -> None:
        got, canonical, reason = convert("HbA1c", 5.2, "%", units)
        assert (got, canonical, reason) == (5.2, "%", None)


class TestRefusal:
    """An unknown unit is never guessed. Guessing is how a value ends up 10x off."""

    def test_unknown_unit_refuses(self, units: dict) -> None:
        got, _canonical, reason = convert("Vitamin D", 31.29, "IU/mL", units)
        assert got is None
        assert reason and "unknown unit" in reason

    def test_missing_unit_refuses(self, units: dict) -> None:
        got, _canonical, reason = convert("Cholesterol", 170.0, "", units)
        assert got is None
        assert reason and "no unit" in reason

    def test_analyte_without_units_passes_through(self, units: dict) -> None:
        # Imaging measurements and ratios have no unit table entry. Nothing to
        # convert means nothing to get wrong.
        got, _canonical, reason = convert("EF", 60.0, "%", units)
        assert got == 60.0 and reason is None

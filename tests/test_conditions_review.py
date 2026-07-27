"""The alias guard in tools/conditions_review_apply.

Standing up a bucket must never commit a record-specific string (a date, a
quantity) into the generic codebook, and must not add a redundant alias the
bucket key already matches. This is what keeps the review loop safe to run over
real diagnoses.
"""

from __future__ import annotations

from tools.conditions_review_apply import maybe_alias


class TestMaybeAlias:
    def test_skips_alias_the_key_already_matches(self) -> None:
        """'cholecystitis' key already whole-word matches the raw string, so no
        record-specific alias is committed."""
        entry = {"icd10": ["K81"], "icd10_title": "Cholecystitis", "aliases": []}
        maybe_alias(entry, "Gangrenous cholecystitis with septicemia", "cholecystitis", [])
        assert entry["aliases"] == []

    def test_refuses_a_string_carrying_a_digit(self) -> None:
        """A dated/quantified string is record-specific -> warn, don't commit."""
        entry = {"icd10": ["K81"], "icd10_title": "Cholecystitis", "aliases": []}
        warn: list = []
        maybe_alias(entry, "cholecystitis Mar 2023", "gallstone", warn)
        assert entry["aliases"] == []
        assert warn and "FIX" in warn[0]

    def test_adds_a_needed_generic_alias(self) -> None:
        """The key does NOT match the wording and it is clean -> add it as an alias."""
        entry = {"icd10": ["N40"], "icd10_title": "Benign prostatic hyperplasia", "aliases": []}
        maybe_alias(entry, "Bladder Outlet Obstruction", "bph", [])
        assert entry["aliases"] == ["Bladder Outlet Obstruction"]

    def test_skips_when_an_existing_alias_already_matches(self) -> None:
        entry = {"icd10": ["D69"], "icd10_title": "Purpura", "aliases": ["low platelet"]}
        maybe_alias(entry, "Low platelet count", "thrombocytopenia", [])
        assert entry["aliases"] == ["low platelet"]

"""The golden test: does the extractor reproduce years of hand-typed values?

the master sheet holds 824 values the user typed by hand from the original
reports. 481 of them have a source PDF still in Drive. This test extracts those
PDFs and diffs the result against what he typed.

That makes it the only thing in this project that can tell us whether the LLM is
quietly lying. Most systems like this ship with no ground truth at all.

Two distinct failures, and they are NOT the same severity:

  MISMATCH  -- we produced a different value than the human did. This is the
               dangerous one: a wrong number in a medical record. Fails the test.

  UNCOVERED -- the human has a value we did not produce. Usually a missing alias
               (the lab prints a name the codebook doesn't know yet), sometimes a
               value quarantined for good reason. Reported, does not fail --
               a gap is not a lie.

Reads the cached extractions (tests/fixtures/extracted/), so it is offline and
free. Regenerate the cache with `python -m tools.extract_golden` after changing
the prompt or the model.
"""

from __future__ import annotations

import glob
import json
import os

import pytest

from src.normalize import ABSENT, load_codebook, parse_value, resolve, values_agree
from src.units import convert, load_units

CACHE = "tests/fixtures/extracted"
GOLDEN = "tests/fixtures/golden.json"
SHEET_ERRORS = "tests/fixtures/sheet_errors.json"


def _is_absent(value: str) -> bool:
    """The lab printed "N/A" / "Not Done": no result, so nothing to disagree with."""
    import re

    return re.sub(r"[\s_]+", " ", str(value).lower()).strip() in ABSENT


def _adjudicated() -> dict[tuple[str, str, str], dict]:
    """Disagreements where the SHEET is wrong, proven against the PDF's own text.

    Excluded from the failure list but still printed, so they stay visible facts
    rather than quietly-tolerated folklore.
    """
    data = json.load(open(SHEET_ERRORS, encoding="utf-8"))
    return {(e["person"], e["date"], e["analyte"]): e for e in data["errors"]}


def _cached() -> list[dict]:
    files = sorted(glob.glob(os.path.join(CACHE, "*.json")))
    if not files:
        pytest.skip("No cached extractions. Run: python -m tools.extract_golden")
    return [json.load(open(f, encoding="utf-8")) for f in files]


def _hand_typed() -> dict[tuple[str, str], dict[str, str]]:
    """{(person, sheet date): {analyte: printed value}} from the master sheet."""
    by_doc: dict[tuple[str, str], dict[str, str]] = {}
    for g in json.load(open(GOLDEN, encoding="utf-8")):
        by_doc.setdefault((g["person"], g["date"]), {})[g["analyte"]] = g["text"]
    return by_doc


def compare() -> tuple[list[str], list[str], list[str], int]:
    """Diff every cached extraction against the hand-typed sheet.

    The sheet's numbers are in the codebook's units, so an extracted value is
    converted into those units before it is compared. Comparing raw printed
    values would flag `215 x10^3/uL` against `215000` as a disagreement when the
    two are the same platelet count.

    Returns (mismatches, uncovered, sheet_errors_hit, agreed).
    """
    codebook = load_codebook()
    units = load_units()
    hand = _hand_typed()
    adjudicated = _adjudicated()
    mismatches: list[str] = []
    uncovered: list[str] = []
    sheet_errors: list[str] = []
    agreed = 0

    for doc in _cached():
        key = (doc["person"], doc["sheet_date"])
        expected = hand.get(key, {})
        if not expected:
            continue

        resolved, _ = resolve(doc["passed"], codebook)
        got = {r["analyte"]: r for r in resolved if r["analyte"]}

        for analyte, hand_value in expected.items():
            r = got.get(analyte)
            if r is None:
                uncovered.append(f"{doc['person']} {doc['sheet_date']} {analyte}")
                continue

            value = r["value"]
            number, qual = parse_value(value)

            if number is None and qual is None and not str(value).strip("- "):
                uncovered.append(
                    f"{doc['person']} {doc['sheet_date']} {analyte} (lab printed no value)"
                )
                continue
            if number is None and qual is None and _is_absent(value):
                # The lab printed "N/A" / "Not Done". That is the absence of a
                # result, not a result that contradicts the human's.
                uncovered.append(
                    f"{doc['person']} {doc['sheet_date']} {analyte} " f"(lab printed {value!r})"
                )
                continue

            if number is not None:
                converted, canonical, reason = convert(analyte, number, r.get("unit", ""), units)
                if reason:
                    # An untrusted unit is a review item, not a disagreement.
                    uncovered.append(f"{doc['person']} {doc['sheet_date']} {analyte} ({reason})")
                    continue
                value = f"{converted}"

            # Only like can be diffed against like. The lab printed HBsAg as
            # "Negative" while the sheet records the numeric titre "0.24"; the
            # Pap Smear report says "Received 2 unstained smears" where the sheet
            # says "Normal". Those are different REPRESENTATIONS of the same
            # finding, not contradictions, and asserting on them would only teach
            # us to ignore the assertion.
            hand_num, hand_qual = parse_value(hand_value)
            got_num, got_qual = parse_value(value)
            comparable = (got_num is not None and hand_num is not None) or (
                got_qual is not None and hand_qual is not None
            )
            if not comparable:
                uncovered.append(
                    f"{doc['person']} {doc['sheet_date']} {analyte} "
                    f"(not comparable: document {value!r} vs sheet {hand_value!r})"
                )
                continue

            if values_agree(value, hand_value):
                agreed += 1
                continue

            key = (doc["person"], doc["sheet_date"], analyte)
            if key in adjudicated:
                sheet_errors.append(
                    f"{doc['person']} {doc['sheet_date']} {analyte}: sheet says "
                    f"{hand_value!r}, document says {adjudicated[key]['extracted']!r}"
                )
                continue

            mismatches.append(
                f"{doc['person']} {doc['sheet_date']} {analyte}: extracted "
                f"{r['value']!r} {r.get('unit', '')!r} (= {value}) but "
                f"hand-typed {hand_value!r}"
            )

    return mismatches, uncovered, sheet_errors, agreed


def test_no_value_disagrees_with_the_human() -> None:
    """A value we disagree with is a wrong number in a medical record."""
    mismatches, uncovered, sheet_errors, agreed = compare()
    total = agreed + len(mismatches) + len(uncovered) + len(sheet_errors)
    print(
        f"\nagreed {agreed}/{total} | mismatched {len(mismatches)} | "
        f"uncovered {len(uncovered)} | known sheet errors {len(sheet_errors)}"
    )
    for e in sheet_errors:
        print(f"  sheet error (adjudicated, extractor is right): {e}")
    for u in uncovered[:25]:
        print(f"  uncovered: {u}")
    assert not mismatches, "\n".join(["Extractor disagrees with the human:", *mismatches])


def test_no_document_fails_its_patient_check() -> None:
    """A report whose printed patient contradicts its folder is a misfiled scan."""
    bad = [f"{d['source']}: {d['doc_hard']}" for d in _cached() if not d["doc_ok"]]
    assert not bad, "\n".join(["Documents failed the patient check:", *bad])

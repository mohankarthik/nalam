"""Who the patients are, and which reference range applies to each of them.

Every personal fact lives in `data/people.json` (gitignored). This module knows
only the SHAPE of a person, never a particular one -- relationships in
particular are config, not code: every family names itself differently, and
"dad" is not a concept the software should hold an opinion about.

Two rules it does hold:

* **Sex picks the fallback range.** Male and female normals genuinely differ
  (creatinine, uric acid, HDL, haemoglobin).

* **Children never get an adult range.** Paediatric normals differ hugely by
  age, so an adult band is confidently wrong in both directions: it marks healthy
  values abnormal and abnormal ones fine. A child is flagged against the range the
  LAB printed on the report, which is age-appropriate -- or against nothing at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from src.constants import PEOPLE_CONFIG


@dataclass(frozen=True)
class Person:
    folder: str
    correspondent: str
    sex: Optional[str] = None
    child: bool = False
    aliases: tuple[str, ...] = field(default_factory=tuple)


def load_people() -> dict[str, Person]:
    """{correspondent -> Person}."""
    with open(PEOPLE_CONFIG, encoding="utf-8") as f:
        raw = json.load(f)["people"]
    return {
        entry["correspondent"]: Person(
            folder=folder,
            correspondent=entry["correspondent"],
            sex=entry.get("sex"),
            child=bool(entry.get("child")),
            aliases=tuple(entry.get("aliases") or ()),
        )
        for folder, entry in raw.items()
        if not folder.startswith("_")
    }


def resolve(who: str) -> Optional[Person]:
    """Find a person by correspondent, folder, or one of THEIR OWN aliases.

    The aliases come from the user's config -- their words for their own family.
    The code does not know what a "dad" is, and should not.
    """
    want = (who or "").strip().lower()
    if not want:
        return None
    for person in load_people().values():
        if want in {
            person.correspondent.lower(),
            person.folder.lower(),
            *(a.lower() for a in person.aliases),
        }:
            return person
    return None


def reference_range(
    person: Person,
    analyte: str,
    codebook: dict[str, dict],
    lab_low: Optional[float] = None,
    lab_high: Optional[float] = None,
) -> tuple[Optional[float], Optional[float], str]:
    """The range a value should be flagged against. Returns (low, high, source).

    THE LAB'S PRINTED RANGE WINS. Ranges are not fixed facts: labs revise them,
    they are specific to the assay method actually used, and they are
    age-appropriate for children. A range frozen into a spreadsheet years ago is
    none of those things.

    The curated per-sex range is only the fallback, for results where the lab
    printed no usable range.

    Note the units. `lab_low`/`lab_high` are in the units the LAB printed, so
    they may ONLY be compared against the raw printed value -- never against a
    unit-converted one. A T3 of 1.73 nmol/L converts to 1.13 ng/mL, and checking
    1.13 against the lab's 1.30-3.10 nmol/L band would call a normal thyroid low.
    See `flag_observation`.

    No range -> no flag. An absent flag is honest; a wrong one is dangerous.
    """
    # A band whose ends are equal is not a range. It comes from labs that print an
    # interpretive TABLE instead of a band -- "Desirable <200 / Borderline 200-239
    # / High >240" for cholesterol, the diabetic tiers for HbA1c -- and the parser
    # picked two numbers out of it. Fall back rather than flag against nonsense.
    degenerate = lab_low is not None and lab_high is not None and lab_low == lab_high
    if (lab_low is not None or lab_high is not None) and not degenerate:
        return lab_low, lab_high, "lab"

    if person.child:
        # Adult ranges are wrong for children and we have no lab range to fall
        # back on. Say nothing rather than say something false.
        return None, None, "none"

    entry = codebook.get(analyte)
    if not entry or not person.sex:
        return None, None, "none"

    band = (entry.get("ranges") or {}).get(person.sex)
    if not band:
        return None, None, "none"
    return band.get("low"), band.get("high"), f"codebook ({person.sex})"


def flag_observation(
    person: Person,
    analyte: str,
    raw_value: str,
    value_num: Optional[float],
    lab_low: Optional[float],
    lab_high: Optional[float],
    codebook: dict[str, dict],
) -> tuple[str, str]:
    """Flag one observation. Returns (flag, which range was used).

    Compares like with like: the lab's range against the RAW printed value (same
    units by construction), and the codebook's range against the unit-converted
    value (which is in codebook units). Mixing the two is how a normal result
    gets called abnormal.
    """
    from src.normalize import parse_value

    low, high, source = reference_range(person, analyte, codebook, lab_low, lab_high)
    if low is None and high is None:
        return "", "none"

    if source == "lab":
        printed, _qual = parse_value(raw_value)
        return flag(printed, low, high), source
    return flag(value_num, low, high), source


def flag(value: Optional[float], low: Optional[float], high: Optional[float]) -> str:
    """'high' | 'low' | 'normal' | '' (no range, so no opinion)."""
    if value is None or (low is None and high is None):
        return ""
    if high is not None and value > high:
        return "high"
    if low is not None and value < low:
        return "low"
    return "normal"


def shared_name_tokens() -> set[str]:
    """Name tokens that more than one family member has: the surnames.

    A family is precisely the setting where everyone is called the same thing.
    Matching a document to a person on a surname alone would let one relative's
    records be filed against another -- so `validator.patient_matches` requires a
    match on a token that DISTINGUISHES the person.
    """
    from src.validator import _name_tokens

    counts: dict[str, int] = {}
    for person in load_people().values():
        for token in _name_tokens(person.correspondent):
            counts[token] = counts.get(token, 0) + 1
    return {token for token, n in counts.items() if n > 1}

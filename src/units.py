"""Convert a lab's printed unit into the codebook's unit. Never assume.

A 2021 report printed Vitamin D as "31.29 nmol/L"; the master sheet keeps
Vitamin D in ng/mL with a 30-80 range. Stored unconverted, 31.29 sits in the
middle of a range it does not belong to, and every future trend is nonsense.

The rule is conservative on purpose:

    unit matches the canonical  -> store as-is
    unit has a known factor     -> convert, keep the raw text
    unit unknown, or absent on a numeric result
                                -> DO NOT GUESS. Flag for review.

Guessing here is how a lab value ends up an order of magnitude wrong while
looking perfectly plausible.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

UNITS = "data/units.json"


def _fold(unit: str) -> str:
    """Normalise a unit string for comparison.

    Labs write the same unit a dozen ways, and the golden test turned up most of
    them in the wild: "mg/dl", "mg / dL", "MG/DL"; "mill/mm3", "million/cu.mm";
    "thou/mm3", "x10^3/uL"; "mic g/dl" for ug/dL; "/1sthour" for ESR's mm/hr.
    A cubic millimetre IS a microlitre, so they all fold onto one spelling.
    """
    u = (unit or "").strip().lower()
    u = u.replace("µ", "u").replace("μ", "u")
    # PDF font mangling turns "uL" into Greek lookalikes: "10^3 / μι" is 10^3/uL.
    u = u.replace("ι", "l").replace("Ι", "l").replace("ⅼ", "l")
    u = u.replace("**", "^").replace("*", "^")
    u = u.replace(".", "")
    u = re.sub(r"\bmic\s*g\b", "ug", u)  # "mic g/dl" -> "ug/dl"
    u = re.sub(r"\bmicg\b", "ug", u)
    u = re.sub(r"\bmcg\b", "ug", u)
    u = re.sub(r"\s+", "", u)

    # 1 mm^3 == 1 uL. Fold every spelling of the volume onto "/ul".
    u = re.sub(r"(cumm|cmm|mm3|mm\^3|cubicmm)", "ul", u)
    u = re.sub(r"(?<![a-z0-9])(million|mill|mil)(?=/)", "mill", u)
    u = re.sub(r"(?<![a-z0-9])(thousand|thou)(?=/)", "10^3", u)
    u = re.sub(r"^x", "", u)

    # ESR is printed as "mm/hr", "mm/1st hour", "/1sthour".
    u = re.sub(r"^/?(mm)?/?1sthour$", "mm/hr", u)
    u = re.sub(r"^mm/1sthr$", "mm/hr", u)
    return u


def load_units() -> dict[str, dict]:
    with open(UNITS, encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def convert(
    analyte: str,
    value: float,
    printed_unit: str,
    table: Optional[dict[str, dict]] = None,
) -> tuple[Optional[float], str, Optional[str]]:
    """Convert a value into the analyte's canonical unit.

    Returns (converted value, canonical unit, reason it could not be trusted).
    A non-None reason means the caller must route the result to review rather
    than commit it.
    """
    table = table if table is not None else load_units()
    entry = table.get(analyte)
    if entry is None:
        # No unit is defined for this analyte (ratios, indices, imaging
        # measurements). Nothing to convert, nothing to get wrong.
        return value, printed_unit, None

    canonical = entry["canonical"]
    # The model may emit `"unit": null`, which reaches here as None (not ""). Treat
    # a missing unit as absent rather than dereferencing it -- an unlabelled numeric
    # result is the review case this branch exists to flag, not a crash.
    if not (printed_unit or "").strip():
        return None, canonical, f"{analyte}: numeric result with no unit printed"

    if _fold(printed_unit) == _fold(canonical):
        return value, canonical, None

    for unit, factor in entry.get("convert", {}).items():
        if _fold(unit) == _fold(printed_unit):
            return value * float(factor), canonical, None

    return (
        None,
        canonical,
        f"{analyte}: unknown unit {printed_unit!r} (codebook uses {canonical!r})",
    )

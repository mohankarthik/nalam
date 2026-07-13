"""Indian brand name -> molecule. Display keeps the brand.

Three questions that a brand list alone cannot answer, and this can:

  "Is this patient on metformin?"  Possibly -- as TENEPRIDE M, GALVUS MET or
                               GLUCONORM G2. Three brands, one molecule.
  "Is anything duplicated?"    GLUCONORM G2 and GEMER are both
                               glimepiride+metformin. Two doctors, one drug,
                               twice the dose.
  "What class is this?"        CLOPILET is an antiplatelet. It matters whether
                               someone is on one after a stroke.

Display is "Metformin + Teneligliptin (TENEPRIDE M)": the generic first because
that is the drug, the brand kept because that is what is printed on the strip in
the cupboard and what the doctor wrote.

An UNCONFIRMED mapping is never used. Guessing a molecule is the most dangerous
thing this system could do, so an unknown brand keeps its brand name and goes to
review rather than acquiring a plausible-looking generic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

DRUGS = "data/drugs.json"

# Dosage-form prefixes labs and doctors prepend. Not part of the brand.
_FORM = re.compile(
    r"^(tab|tabs|tablet|cap|caps|capsule|inj|injection|syp|syrup|susp|oint|"
    r"cream|drops|neb|nebulization|nebulisation)\.?\s+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Drug:
    brand: str
    generic: list[str]
    drug_class: str
    confirmed: bool
    device: bool

    @property
    def display(self) -> str:
        """'Metformin + Teneligliptin (TENEPRIDE M)'. Brand always kept."""
        if self.device:
            return f"{self.brand} [device, not a drug]"
        if not self.generic or not self.confirmed:
            return self.brand
        return f"{' + '.join(self.generic)} ({self.brand})"


def load_drugs() -> dict[str, dict]:
    with open(DRUGS, encoding="utf-8") as f:
        return {
            k: v
            for k, v in json.load(f)["drugs"].items()
            if not k.startswith("_")
        }


def _key(printed: str) -> str:
    """Fold a printed drug name to a lookup key.

    'Tab. Pan - DSR' and 'TAB. PAN - DSR' and 'PAN' are not all the same drug:
    the DSR is a different formulation. So the form prefix is stripped but the
    rest of the name is preserved, and matching is longest-first.
    """
    name = (printed or "").strip()
    name = _FORM.sub("", name)
    name = re.sub(r"\s+", " ", name)
    return name.upper().strip(" .-")


def lookup(printed: str, table: Optional[dict[str, dict]] = None) -> Optional[Drug]:
    """Map a printed drug name to its molecule. None = not in the table.

    Longest match first, so 'ECOSPRIN AV' (aspirin + atorvastatin) is not
    swallowed by 'ECOSPIRIN' (aspirin alone), and 'TAB. PAN - DSR' is not
    reduced to plain 'PAN'.
    """
    table = table if table is not None else load_drugs()
    key = _key(printed)
    if not key:
        return None

    candidates = [
        (k, v) for k, v in table.items() if key == _key(k) or key.startswith(_key(k) + " ")
    ]
    if not candidates:
        # A brand the strength got glued onto: 'CAP. REALCEF 200MG'.
        candidates = [
            (k, v) for k, v in table.items() if key.startswith(_key(k))
        ]
    if not candidates:
        return None

    brand, entry = max(candidates, key=lambda kv: len(_key(kv[0])))
    return Drug(
        brand=printed.strip(),
        generic=list(entry.get("generic") or []),
        drug_class=str(entry.get("class") or ""),
        confirmed=bool(entry.get("confirmed")),
        device=bool(entry.get("device")),
    )


def molecules(printed: str, table: Optional[dict[str, dict]] = None) -> list[str]:
    """The molecules in a printed drug, or [] if unknown/unconfirmed."""
    d = lookup(printed, table)
    if d is None or not d.confirmed or d.device:
        return []
    return d.generic

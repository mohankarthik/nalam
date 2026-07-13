"""Deterministic guards against LLM hallucination. No LLM calls in here.

Lifted from gajana's StatementValidator, which exists because a vision model
reading a table can put the right number in the wrong column, or invent one
outright. The insight transfers exactly:

    Vision owns STRUCTURE (which test, which column).
    The PDF text layer owns TOKENS (never mangled).
    This module crosses them.

A value the model claims to have read, but which does not literally appear in
the PDF's own text layer, did not come from the document. It is quarantined --
never committed, never silently dropped.

Two hard checks, both quarantine:
  * token check    -- the value must appear in the text layer
  * patient check  -- the name printed ON the report must match the folder it
                      came from. This catches a misfiled scan, which is the
                      single most dangerous error this system can make.

Soft checks are logged and attached to the row, but do not block it.
"""

from __future__ import annotations

import datetime
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Verdict:
    """Why a result was quarantined, or why it passed."""

    ok: bool
    hard: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)


def normalise(text: str) -> str:
    """Fold a string for comparison: lowercase, collapse whitespace, strip punctuation."""
    text = unicodedata.normalize("NFKD", text or "")
    text = text.lower()
    text = re.sub(r"[^a-z0-9.<>%/-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def value_in_text(value: str, text_layer: str) -> bool:
    """Is this value literally present in the PDF's text layer?

    Numbers are compared on their digits so that "5.20" in the model's output
    still matches "5.20" in the text layer, while tolerating the thousands
    separators labs sprinkle into cell counts ("4,10,000").
    """
    value = (value or "").strip()
    if not value:
        return False

    haystack = normalise(text_layer)
    if normalise(value) and normalise(value) in haystack:
        return True

    # Numeric: strip separators from both sides and look for the bare digits.
    bare = re.sub(r"[,\s]", "", value)
    if re.fullmatch(r"[<>]?=?-?\d+(\.\d+)?", bare):
        digits = bare.lstrip("<>=-")
        stripped_haystack = re.sub(r"[,\s]", "", text_layer)
        if digits and digits in stripped_haystack:
            return True

    return False


def _name_tokens(name: str) -> set[str]:
    """Meaningful name parts, minus the honorifics labs prepend."""
    drop = {"mr", "mrs", "ms", "dr", "master", "miss", "baby", "b", "of", "smt", "sri"}
    return {t for t in normalise(name).split() if t and t not in drop and len(t) > 1}


# A neonatal record is labelled by the MOTHER's name: "B/O Alice Doe",
# "BABY OF ...", "Baby of ...". OCR mangles the slash, so "B/O" arrives as "Blo",
# "BIO", "B10".
#
# This nearly caused the worst error the system can make. The rule "the document
# wins over the folder" -- which correctly re-files a mother's surgery from a
# child's folder -- read "B/O ALICE DOE" on a premature baby's retinopathy
# report and moved the BABY's records into the MOTHER's. The folder was right;
# the document names the parent only to identify the child.
_NEONATAL = re.compile(
    r"^\s*(b\s*[/\\|1l]\s*o|b[il1]o|baby\s+of|baby|newborn|nb)\b",
    re.IGNORECASE,
)

# "1 Month 14 Days", "3 Days", "6 Weeks" -- nobody's mother is six weeks old.
_INFANT_AGE = re.compile(
    r"^\s*\d+\s*(day|days|week|weeks|month|months)\b", re.IGNORECASE
)


def names_a_baby(printed_name: str, printed_age: str = "") -> bool:
    """Is this a NEONATAL record, labelled with the parent's name?

    True means: the patient is this person's baby, NOT this person. The folder
    knows which child it is; the document does not.
    """
    if _NEONATAL.match((printed_name or "").strip()):
        return True
    return bool(_INFANT_AGE.match((printed_age or "").strip()))


def patient_matches(
    printed: str, expected: str, shared: Optional[set[str]] = None
) -> bool:
    """Does the name printed on the report plausibly refer to the expected patient?

    Lenient on FORM (initials, honorifics, reordered names, an abbreviated given
    name). Strict on IDENTITY -- and in a FAMILY, identity is not the surname.

    `shared` names the tokens that more than one family member has: the surnames.
    A match on those alone is not a match at all. Without this, a document for one
    child matched a parent because they share a surname, and the "re-file to the
    person the document names" rule could move records to the wrong relative --
    the worst error the system can make, in the one setting where it is most
    likely: everybody is called the same thing.

    An empty printed name is not a match; it is an unknown, and the caller decides.
    """
    printed_t, expected_t = _name_tokens(printed), _name_tokens(expected)
    if not printed_t or not expected_t:
        return False

    common = printed_t & expected_t
    if not common:
        return False
    if shared:
        # At least one token they share must be one that DISTINGUISHES this person
        # from the rest of the family.
        return bool(common - shared)
    return True


def check_result(
    result: dict[str, str],
    text_layer: str,
) -> Verdict:
    """Validate one extracted lab result against the document's own text."""
    hard: list[str] = []
    soft: list[str] = []

    name = (result.get("name") or "").strip()
    value = (result.get("value") or "").strip()

    if not name:
        hard.append("no test name")
    if not value:
        hard.append("no value")

    if value and text_layer.strip() and not value_in_text(value, text_layer):
        hard.append(f"value {value!r} not present in the PDF text layer")

    if name and text_layer.strip() and not value_in_text(name, text_layer):
        # The model may reasonably tidy a test name across a line break, so this
        # is a smell, not a lie.
        soft.append(f"test name {name!r} not found verbatim in text layer")

    if not result.get("unit") and not re.fullmatch(
        r"[a-z ]+", normalise(value) or "x"
    ):
        soft.append("numeric result with no unit printed")

    return Verdict(ok=not hard, hard=hard, soft=soft)


def check_document(
    patient: dict[str, str],
    expected_person: str,
    expected_date: Optional[datetime.date],
    text_layer: str,
    max_drift_days: int = 15,
) -> Verdict:
    """Validate the document as a whole: is it really this patient's, on this date?"""
    hard: list[str] = []
    soft: list[str] = []

    printed_name = (patient.get("name") or "").strip()
    if not printed_name:
        soft.append("report prints no patient name; relying on folder alone")
    elif not patient_matches(printed_name, expected_person):
        hard.append(
            f"patient on report ({printed_name!r}) does not match the folder "
            f"({expected_person!r}) -- possible misfiled scan"
        )

    collected = (patient.get("collected_at") or "").strip()
    if collected and expected_date:
        parsed = _parse_date(collected)
        if parsed is None:
            soft.append(f"unparseable collection date {collected!r}")
        elif abs((parsed - expected_date).days) > max_drift_days:
            soft.append(
                f"collection date {parsed} is {abs((parsed - expected_date).days)}d "
                f"from the filename date {expected_date}"
            )

    if not text_layer.strip():
        soft.append("no text layer: scanned or handwritten, token check unavailable")

    return Verdict(ok=not hard, hard=hard, soft=soft)


_DATE_FORMATS = (
    "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y",
    "%d %b %Y", "%d-%b-%Y", "%d/%b/%Y", "%b %d, %Y",
    "%d %B %Y", "%Y/%m/%d",
)


def _parse_date(text: str) -> Optional[datetime.date]:
    text = re.sub(r"\s+", " ", text.strip())
    text = re.sub(r"\s*\d{1,2}:\d{2}(:\d{2})?\s*(am|pm)?$", "", text, flags=re.I)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None

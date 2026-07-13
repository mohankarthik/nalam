"""The live medicine list, and the reconciliation that keeps it honest.

A prescription records what was STARTED. Nothing, anywhere, records what was
STOPPED. A discharge summary lists the drugs a patient leaves on; no later document
says whether they are still on any of them. So "what is this person taking right now"
cannot be derived from documents. It is not a parsing problem, it is a missing
fact, and no amount of LLM will conjure it.

So the medicine list is an EVENT LOG plus a human decision:

    prescribed | continued | changed | stopped

and `current()` is a view over it. When a new prescription or discharge arrives,
`reconcile()` diffs it against what we believe is current and produces cards:

    "new: Rosuvastatin. missing: Atorvastatin. Switched? Both? Ignore?"

That tap IS the state. There is no way around it, and pretending otherwise --
inferring that an absent drug was stopped -- would quietly delete drugs a person
is still taking. The system proposes; a human decides; `entered_by` records which.
"""

from __future__ import annotations

import datetime
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from src.drugs import lookup


@dataclass
class Med:
    drug: str
    generic: Optional[str]
    strength: Optional[str]
    frequency: Optional[str]
    duration: Optional[str]
    effective: Optional[str]
    event: str
    status: str

    @property
    def key(self) -> str:
        """What makes two prescriptions 'the same drug'.

        The molecule, when we know it -- so GLUCONORM G2 and GEMER are one drug
        (both glimepiride+metformin), and LOSAR and LOSARTAN are one drug. Where
        the molecule is unknown we fall back to the brand, which is the honest
        answer: we cannot tell.
        """
        return (self.generic or self.drug).strip().lower()

    @property
    def display(self) -> str:
        if self.generic:
            return f"{self.generic} ({self.drug})"
        return self.drug


@dataclass
class Reconciliation:
    subject: str
    as_of: Optional[str]
    started: list[Med] = field(default_factory=list)
    stopped: list[Med] = field(default_factory=list)   # PROPOSED, not decided
    continued: list[Med] = field(default_factory=list)
    changed: list[tuple[Med, Med]] = field(default_factory=list)  # (was, now)

    @property
    def needs_decision(self) -> bool:
        return bool(self.started or self.stopped or self.changed)


def _rows_to_meds(rows) -> list[Med]:
    return [
        Med(
            drug=r["drug"],
            generic=r["generic"],
            strength=r["strength"],
            frequency=r["frequency"],
            duration=r["duration"],
            effective=r["effective"],
            event=r["event"],
            status=r["status"],
        )
        for r in rows
    ]


# Doctors write durations by hand, and the real strings are messy:
#   "X 7. DAYS AF"   "X 10DAYS A"   "FOR 7 DAY BF"   "FOR 1 MONTH AP"
# so allow stray punctuation and a missing space between the number and the unit.
_DURATION = re.compile(
    r"(?:x\s*)?(\d+)\s*[.,]?\s*(day|days|week|weeks|month|months)\b", re.IGNORECASE
)
_INDEFINITE = re.compile(r"(continue|continous|continuous|lifelong|regular)", re.I)


def course_ends(med: Med) -> Optional[datetime.date]:
    """When a course explicitly ENDS, per the document. None = open-ended.

    This is not inferring that a drug stopped -- it is reading what the
    prescription actually says. An antibiotic prescribed 'X 7 DAYS' ended seven days after discharge, and
    treating a years-old antibiotic as a current medication is simply wrong. A drug with no stated duration stays open and needs a human.
    """
    if not med.effective or not med.duration:
        return None
    if _INDEFINITE.search(med.duration):
        return None

    m = _DURATION.search(med.duration)
    if not m:
        return None

    n, unit = int(m.group(1)), m.group(2).lower()
    days = n * {"day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30, "months": 30}[unit]
    try:
        start = datetime.date.fromisoformat(med.effective)
    except ValueError:
        return None
    return start + datetime.timedelta(days=days)


def current(
    con: sqlite3.Connection, subject: str, as_of: Optional[datetime.date] = None
) -> list[Med]:
    """What we believe the person is on now.

    The latest event per drug, unless that event was 'stopped' OR the document
    stated a duration that has since elapsed.

    Note the word BELIEVE. A drug with no stated duration, prescribed in 2023 and
    never reconciled, still shows as current -- because nothing has told us
    otherwise, and silently ageing it out would delete drugs a person is still
    taking. `stale` on those is the honest signal, not removal.
    """
    as_of = as_of or datetime.date.today()
    rows = con.execute(
        """SELECT * FROM medication_events
           WHERE subject = ?
           ORDER BY effective DESC, id DESC""",
        (subject,),
    ).fetchall()

    latest: dict[str, Med] = {}
    for med in _rows_to_meds(rows):
        latest.setdefault(med.key, med)

    active = []
    for m in latest.values():
        if m.event == "stopped":
            continue
        ends = course_ends(m)
        if ends and ends < as_of:
            continue  # the prescription said how long, and that time has passed
        active.append(m)
    return active


def from_document(con: sqlite3.Connection, document_id: int) -> list[Med]:
    return _rows_to_meds(
        con.execute(
            "SELECT * FROM medication_events WHERE document_id = ? ORDER BY id",
            (document_id,),
        ).fetchall()
    )


def reconcile(
    con: sqlite3.Connection, subject: str, document_id: int
) -> Reconciliation:
    """Diff a new medication list against what we believe is current.

    `stopped` is a PROPOSAL, never a conclusion. A drug missing from one
    discharge summary does not mean it was stopped -- the summary may simply not
    have listed the patient's long-term drugs. Only a human can say.
    """
    incoming = from_document(con, document_id)
    as_of = incoming[0].effective if incoming else None

    # What was current BEFORE this document arrived.
    previous = [
        m
        for m in current(con, subject)
        if m.effective is None or as_of is None or m.effective < as_of
    ]

    before = {m.key: m for m in previous}
    after = {m.key: m for m in incoming}

    rec = Reconciliation(subject=subject, as_of=as_of)

    for key, med in after.items():
        old = before.get(key)
        if old is None:
            rec.started.append(med)
        elif (old.strength or "") != (med.strength or "") or (
            old.frequency or ""
        ) != (med.frequency or ""):
            rec.changed.append((old, med))
        else:
            rec.continued.append(med)

    for key, med in before.items():
        if key not in after:
            rec.stopped.append(med)

    return rec


def record_decision(
    con: sqlite3.Connection,
    subject: str,
    drug: str,
    event: str,
    effective: Optional[str],
    document_id: Optional[int] = None,
) -> None:
    """Write a human's decision into the log. This is what makes it state."""
    if event not in ("prescribed", "continued", "changed", "stopped"):
        raise ValueError(f"not a medication event: {event!r}")

    d = lookup(drug)
    generic = " + ".join(d.generic) if d and d.confirmed and d.generic else None

    con.execute(
        """INSERT INTO medication_events
             (document_id, subject, drug, generic, event, effective, raw_text,
              entered_by, status)
           VALUES (?,?,?,?,?,?,?,'human','ok')""",
        (document_id, subject, drug, generic, event, effective, drug),
    )
    con.commit()

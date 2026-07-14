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
    # When this drug was FIRST prescribed, as opposed to `effective`, which is the
    # most recent event about it. They are different facts and the list needs both.
    #
    # Confirming "yes, he is still on it" writes a `continued` event dated today. If
    # that were the only date, the medicine list would say the drug STARTED today --
    # so reconciling a five-year-old statin would erase the five years. `effective`
    # answers "when did we last hear about this?" (and drives the stale flag, which
    # a confirmation must clear). `started` answers "since when has he been on it?".
    started: Optional[str] = None
    entered_by: str = "extractor"
    # The document behind the LATEST event about this drug -- None for a human
    # decision recorded with no source (record_decision's document_id is optional:
    # nobody wrote down "yes, still on it", which is why a human had to say so).
    document_id: Optional[int] = None

    @property
    def key(self) -> str:
        """What makes two prescriptions 'the same drug'.

        The molecule, when we know it -- so GLUCONORM G2 and GEMER are one drug
        (both glimepiride+metformin), and LOSAR and LOSARTAN are one drug.

        Where the molecule is unknown, fall back to the brand -- but NORMALISED.
        The raw string made "TAB. GALVUSMET", "AB. GALVUSMET" and "GALVUS MET"
        three different medicines in the live list, which is nonsense: they are
        one drug, a form prefix, and an OCR error.
        """
        if self.generic:
            # Order is presentation, not identity. "Methylcobalamin + Pregabalin"
            # and "Pregabalin + Methylcobalamin" are one drug written two ways,
            # and counting them twice put the same molecule on the live list
            # twice over.
            parts = sorted(x.strip().lower() for x in self.generic.split("+") if x.strip())
            return " + ".join(parts)

        from src.drugs import _key

        return _key(self.drug).lower() or self.drug.strip().lower()

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
    stopped: list[Med] = field(default_factory=list)  # PROPOSED, not decided
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
            entered_by=r["entered_by"],
            document_id=r["document_id"],
        )
        for r in rows
    ]


# Doctors write durations by hand, and the real strings are messy:
#   "X 7. DAYS AF"   "X 10DAYS A"   "FOR 7 DAY BF"   "FOR 1 MONTH AP"
# so allow stray punctuation and a missing space between the number and the unit.
# Doctors do not write "for 7 days". They write "x 3d", "x30d", "5 dy", "x 2wk".
# Spelling the units out in full parsed NOTHING on the real data -- every course in
# the database came back open-ended, so a THREE-DAY doxycycline course prescribed in
# 2022 was still a current medication three years later. The script had said when it
# ended; we could not read it.
_DURATION = re.compile(
    r"(?:x\s*)?(\d+)\s*[.,]?\s*" r"(days?|dys?|d|weeks?|wks?|w|months?|mons?|mths?|mo|m)\b",
    re.IGNORECASE,
)
_UNIT_DAYS = {
    "d": 1,
    "dy": 1,
    "dys": 1,
    "day": 1,
    "days": 1,
    "w": 7,
    "wk": 7,
    "wks": 7,
    "week": 7,
    "weeks": 7,
    "m": 30,
    "mo": 30,
    "mon": 30,
    "mons": 30,
    "mth": 30,
    "mths": 30,
    "month": 30,
    "months": 30,
}

# A bare number ("8", "10") is NOT parsed. It probably means days, and probably is
# not worth a wrong answer: guessing the unit on a duration silently expires a drug
# somebody is still taking. Same for OCR wreckage ("x 14clas", "x lodaf") -- the
# number is legible and the unit is not, and half a duration is not a duration.
# Both stay open and go to a human, which is what the review queue is for.
_INDEFINITE = re.compile(r"(continue|continous|continuous|lifelong|regular|sos|prn)", re.I)

# "5 day / month" is a RATE, not a duration: five days EVERY month, recurring --
# dermatology pulses itraconazole exactly like this. Read as a five-day course it
# expires on day five, and a drug the person is still cycling on vanishes from the
# list. A rate means the course has no stated end; a human decides.
_RATE = re.compile(r"(/|per\b)\s*(day|week|month|mth|mo|wk|m|w|d)\b", re.IGNORECASE)


def course_ends(med: Med) -> Optional[datetime.date]:
    """When a course explicitly ENDS, per the document. None = open-ended.

    This is not inferring that a drug stopped -- it is reading what the
    prescription actually says. An antibiotic prescribed 'X 7 DAYS' ended seven
    days after discharge, and treating a years-old antibiotic as a current
    medication is simply wrong. A drug with no stated duration stays open and
    needs a human.
    """
    if not med.effective or not med.duration:
        return None
    if _INDEFINITE.search(med.duration) or _RATE.search(med.duration):
        return None

    m = _DURATION.search(med.duration)
    if not m:
        return None

    n, unit = int(m.group(1)), m.group(2).lower()
    factor = _UNIT_DAYS.get(unit)
    if factor is None:
        return None
    days = n * factor
    try:
        start = datetime.date.fromisoformat(med.effective)
    except ValueError:
        return None
    return start + datetime.timedelta(days=days)


# "Stale" means nobody has said anything about a drug for years -- a question
# about the LAST event, not the first. Shared by run_meds.py's --list display
# and src/qa.py's Telegram answers so the two surfaces can't drift apart on
# what "stale" means.
STALE_BEFORE = "2024-01-01"


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

    all_meds = _rows_to_meds(rows)  # newest first

    # Med.key is the MOLECULE when we know it and the brand when we do not. So two
    # rows for one drug get two different keys the moment one of them learns its
    # molecule and the other has not -- and then "stop Brevoxyl wash" does not stop
    # Brevoxyl wash, it invents a second one beside it.
    #
    # A molecule known about a brand is known about every row of that brand. Share
    # it, so the key is stable whatever order the rows were written in.
    from src.drugs import _key as brand_key

    molecule: dict[str, str] = {}
    for m in all_meds:
        if m.generic:
            molecule.setdefault(brand_key(m.drug).lower(), m.generic)
    for m in all_meds:
        if not m.generic:
            m.generic = molecule.get(brand_key(m.drug).lower())

    # AS OF A DATE means as of a date. An event that had not happened yet cannot be
    # used to answer a question about the past: a drug stopped in 2026 was still
    # being taken in 2024, and "was she on aspirin when she had the stroke?" is
    # exactly the question this exists to answer. `as_of` used to filter course
    # expiry and nothing else, so it read the whole future of the log and gave the
    # 2026 answer to a 2020 question.
    #
    # An undated event is kept: it has no date to be after.
    cutoff = as_of.isoformat()
    visible = [m for m in all_meds if not m.effective or m.effective <= cutoff]

    latest: dict[str, Med] = {}
    for med in visible:
        latest.setdefault(med.key, med)

    # A human's "yes, still on it" is a `continued` event with no strength and no
    # frequency -- it confirms a fact, it does not restate the prescription. Taken
    # literally, reconciling a drug therefore BLANKS its dose and resets its start
    # date to today: the medicine list would say a five-year-old statin began this
    # morning, at an unknown dose. That is worse than the stale flag it replaced.
    #
    # So the confirmation decides the STATUS, and the prescriptions still supply the
    # facts.
    for key, m in latest.items():
        same = [x for x in visible if x.key == key]  # newest first

        # THE CURRENT EPISODE, not the whole history. A drug can be started,
        # stopped, and started again years later -- and then it has two separate
        # courses, not one long one. Walking back from the newest event and halting
        # at the most recent `stopped` gives the episode the person is in NOW; the
        # earlier course is history, and history is what --history is for.
        #
        # Without this, a drug stopped in 2016 and restarted in 2024 reports
        # "started 2015", which reads as an unbroken nine-year course that never
        # happened.
        episode = []
        for x in same:
            if x.event == "stopped":
                break
            episode.append(x)

        # A start date comes from a PRESCRIPTION, never from a confirmation. That is
        # a distinction about the EVENT, not about who recorded it: a `continued`
        # event is dated the day someone said "yes, still on it", and letting that be
        # the start date made a wash somebody has used for years say "started today".
        #
        # But a human saying "he started this in 2023" IS a start. Excluding humans
        # rather than confirmations threw that away too -- and a drug switched at a
        # clinic and never written down has no other source for its start date.
        #
        # No prescription in this episode, no start date. '?' is the honest answer.
        dated = [
            x.effective for x in episode if x.effective and x.event in ("prescribed", "changed")
        ]
        m.started = min(dated) if dated else None
        if m.strength is None:
            m.strength = next((x.strength for x in episode if x.strength), None)
        if m.frequency is None:
            m.frequency = next((x.frequency for x in episode if x.frequency), None)

    from src.drugs import load_drugs, lookup

    table = load_drugs()

    active = []
    for m in latest.values():
        if m.event == "stopped":
            continue

        # Neither a DEVICE nor a SINGLE DOSE is a medicine somebody is on.
        #
        # data/drugs.json knew both -- `device: true`, `single_dose: true` -- and the
        # medicine list never asked. So "what is he taking?" answered with DENTAL
        # FLOSS and a BiPAP machine; and an infant was listed as CURRENTLY TAKING
        # five vaccines, because nobody had written a stop date for her rotavirus
        # drops. You do not stop a vaccine. It was given, on a day, and that is the
        # whole of it.
        #
        # Both stay in medication_events: they happened, and --history must find
        # them. They are simply not medicines, and this is a list of medicines.
        d = lookup(m.drug, table)
        if d and (d.device or d.single_dose):
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


def is_long_term(med: Med) -> bool:
    """Is this a drug someone stays on, rather than a course that ends itself?

    A five-day antibiotic needs no decision from anyone: the prescription says
    how long, and `course_ends` retires it on that date. Asking about it is
    asking a question whose answer is already written down.

    Only open-ended drugs -- the statin, the metformin, the antiplatelet -- are
    genuinely ambiguous, and those are the ones worth a human's attention.
    """
    return course_ends(med) is None


def reconcile(
    con: sqlite3.Connection,
    subject: str,
    document_id: int,
    long_term_only: bool = True,
) -> Reconciliation:
    """Diff a new medication list against what we believe is current.

    Surfaces CHANGES only: a new drug, a changed dose, a drug that has vanished.
    A consultation that re-prescribes the same list produces no cards at all,
    because there is nothing to decide.

    That restraint is the point. Hundreds of prescriptions x half a dozen drugs
    is thousands of events; if every one became a card you would rubber-stamp
    them, and a rubber-stamped review is worse than no review -- it launders a
    guess into a decision. So: only changes, only open-ended drugs, and
    `long_term_only` skips the self-expiring courses entirely.

    `stopped` is a PROPOSAL, never a conclusion. A drug missing from one
    prescription does not mean it was stopped -- a consultation routinely omits a
    patient's other long-term medication. Only a human can say.
    """
    incoming = from_document(con, document_id)
    as_of = incoming[0].effective if incoming else None

    # What was current BEFORE this document arrived.
    previous = [
        m
        for m in current(con, subject)
        if m.effective is None or as_of is None or m.effective < as_of
    ]

    if long_term_only:
        incoming = [m for m in incoming if is_long_term(m)]
        previous = [m for m in previous if is_long_term(m)]

    before = {m.key: m for m in previous}
    after = {m.key: m for m in incoming}

    rec = Reconciliation(subject=subject, as_of=as_of)

    for key, med in after.items():
        old = before.get(key)
        if old is None:
            rec.started.append(med)
        elif (old.strength or "") != (med.strength or "") or (old.frequency or "") != (
            med.frequency or ""
        ):
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
    strength: Optional[str] = None,
    frequency: Optional[str] = None,
    raw_text: Optional[str] = None,
) -> None:
    """Write a human's decision into the log. This is what makes it state.

    `strength` and `frequency` are optional, and the difference matters. Confirming
    that a drug is still being taken says nothing about the dose, so a bare
    confirmation leaves both None and current() keeps whatever the last prescription
    stated -- which is right: the prescription is the evidence, and the confirmation
    is only about status.

    But when a person reads their own strip and says "Arbitel 40mg, 1-0-1", that IS
    the dose, and it is better evidence than a three-year-old script. Given here, it
    is recorded, and current() shows it.

    `raw_text` keeps whatever they actually said, verbatim -- "0-1-0, 6 days a week",
    "3 days a week, evening 5PM". The schema has no column for a regimen that
    complex, and inventing one to hold "6 days a week" would lose the sentence. The
    log keeps the sentence.
    """
    if event not in ("prescribed", "continued", "changed", "stopped"):
        raise ValueError(f"not a medication event: {event!r}")

    d = lookup(drug)
    generic = " + ".join(d.generic) if d and d.confirmed and d.generic else None

    con.execute(
        """INSERT INTO medication_events
             (document_id, subject, drug, generic, strength, frequency, event,
              effective, raw_text, entered_by, status)
           VALUES (?,?,?,?,?,?,?,?,?,'human','ok')""",
        (
            document_id,
            subject,
            drug,
            generic,
            strength,
            frequency,
            event,
            effective,
            raw_text or drug,
        ),
    )
    con.commit()


def history(
    con: sqlite3.Connection,
    subject: Optional[str] = None,
    drug: Optional[str] = None,
) -> list[dict]:
    """Every time a drug was prescribed. Nothing is filtered out.

    The live-list and reconciliation views deliberately hide things -- an expired
    antibiotic is not a current medication, and a five-day course needs no
    decision. But HIDDEN IS NOT DELETED. "When did we last give cetirizine?" and
    "what did she get for hand-foot-and-mouth?" are exactly the questions a family
    health record exists to answer, and they need the whole log: short courses,
    children, one-off prescriptions and all.

    `drug` matches the brand OR the molecule, so searching "cetirizine" finds it
    under whatever brand it was written as.
    """
    # LEFT JOIN, not JOIN. A human's decision -- "he stopped taking it" -- has no
    # document: nobody wrote it down, which is the entire reason a person had to say
    # so. An inner join therefore dropped every stop ever recorded, and "when did he
    # stop taking it?" could not be answered by the one view whose whole promise is
    # that it hides nothing.
    #
    # And `event` is selected now. A stop is not a prescription, and a log that
    # cannot tell them apart cannot show a drug that was taken, stopped, and started
    # again -- which is a thing that happens.
    sql = """
        SELECT m.subject, m.drug, m.generic, m.strength, m.frequency, m.duration,
               m.effective, m.status, m.event, m.entered_by,
               d.doc_type, d.source_path,
               e.diagnoses, e.reason
        FROM medication_events m
        LEFT JOIN documents d ON d.id = m.document_id
        LEFT JOIN encounters e ON e.document_id = m.document_id
        WHERE 1=1
    """
    params: list[str] = []
    if subject:
        sql += " AND m.subject = ?"
        params.append(subject)
    if drug:
        sql += " AND (m.drug LIKE ? OR IFNULL(m.generic,'') LIKE ?)"
        params += [f"%{drug}%", f"%{drug}%"]
    sql += " ORDER BY m.effective DESC, m.id"

    return [dict(r) for r in con.execute(sql, params).fetchall()]


def for_condition(
    con: sqlite3.Connection, condition: str, subject: Optional[str] = None
) -> list[dict]:
    """What was prescribed for a given diagnosis or complaint.

    "What did we give her the last time she had hand-foot-and-mouth?" -- the
    medicines are joined to the encounter that recorded WHY they were given.
    """
    sql = """
        SELECT e.subject, e.admitted AS date, e.diagnoses, e.reason,
               e.follow_up, d.source_path, d.id AS document_id
        FROM encounters e
        JOIN documents d ON d.id = e.document_id
        WHERE (LOWER(IFNULL(e.diagnoses,'')) LIKE LOWER(?)
            OR LOWER(IFNULL(e.reason,''))    LIKE LOWER(?))
    """
    params = [f"%{condition}%", f"%{condition}%"]
    if subject:
        sql += " AND e.subject = ?"
        params.append(subject)
    sql += " ORDER BY e.admitted DESC"

    episodes = []
    for r in con.execute(sql, params).fetchall():
        row = dict(r)
        row["medications"] = [
            dict(m)
            for m in con.execute(
                """SELECT drug, generic, strength, frequency, duration, status
                   FROM medication_events WHERE document_id = ? ORDER BY id""",
                (row["document_id"],),
            ).fetchall()
        ]
        episodes.append(row)
    return episodes

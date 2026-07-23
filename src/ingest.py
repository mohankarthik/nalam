"""Extract a document and commit what survives validation. Quarantine the rest.

The pipeline, in order, and every stage can send a result to review:

    extract   -> the model transcribes verbatim         (src/extractor)
    validate  -> the PDF's text layer must contain it   (src/validator)
    resolve   -> the printed name maps to the codebook  (src/normalize)
    convert   -> the unit maps to the codebook's unit   (src/units)
    commit    -> health.db                              (src/db)

Nothing is ever dropped. A result that fails any stage lands in `review_queue`
with the reason attached, because "we couldn't read it" and "it isn't there" are
different facts and a health record must not confuse them.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from typing import Optional

from src import db
from src.extractor import extract_lab
from src.normalize import is_ignored, load_codebook, load_ignored, parse_value, resolve
from src.people import source_path
from src.units import convert, load_units

logger = logging.getLogger(__name__)


def ingest_document(
    con,
    doc,
    ocr_text: Optional[str] = None,
    paperless_id: Optional[int] = None,
) -> dict:
    """Route one document to its extractor and ingest it. Returns a result dict.

    Same precedence run_extract.py's nightly batch passes use (is_radiology ->
    ingest_radiology, is_lab -> ingest_lab, is_discharge -> ingest_discharge,
    else classify() -> prescription/radiology -> the matching ingest_*), now
    shared with the on-demand queue (run_extract_queue.py) so nightly and
    on-demand routing can never drift apart.

    is_radiology() is checked FIRST, deliberately: imaging shares the
    Medical/Reports tag with lab reports, so is_lab() -- which routes on that tag
    -- would otherwise claim an echo or USG and explode it into junk analyte rows
    (the failure radiology_reports exists to prevent). classify() is the
    content-based backstop for an imaging report whose title names no study.

    ``ocr_text`` corroborates the prescription and radiology branches (labs and
    discharge summaries corroborate off the PDF's own text layer; see
    src/extractor.py).
    """
    from src.extractor import classify, is_discharge, is_encrypted, is_lab, is_radiology

    if doc.suffix != ".pdf":
        return {"doc_type": "unsupported", "note": "not a PDF"}

    doc_date: Optional[datetime.date] = None
    if doc.created:
        try:
            doc_date = datetime.date.fromisoformat(doc.created)
        except ValueError:
            doc_date = None

    if is_radiology(doc):
        return _radiology_result(con, doc, ocr_text, paperless_id, doc_date)

    if is_lab(doc):
        committed, queued = ingest_lab(
            con,
            rel_path=doc.rel,
            subject=doc.correspondent,
            doc_date=doc_date,
            paperless_id=paperless_id,
            abs_path=doc.path,
        )
        return {"doc_type": "lab", "committed": committed, "review": queued}

    if is_discharge(doc):
        meds, encs, misfiled = ingest_discharge(
            con,
            rel_path=doc.rel,
            subject=doc.correspondent,
            doc_date=doc_date,
            paperless_id=paperless_id,
            abs_path=doc.path,
        )
        return {
            "doc_type": "discharge",
            "medications": meds,
            "encounters": encs,
            "misfiled": misfiled,
        }

    pdf = open(doc.path, "rb").read()
    if is_encrypted(pdf):
        return {"doc_type": "encrypted", "note": "password-protected, cannot extract"}

    # classify() is the fallback source of truth for whatever is_lab()/
    # is_discharge() -- both free, title/tag-only heuristics -- miss. A
    # discharge summary titled by its admission ("Hyponatremia.pdf" under an
    # Admissions folder) doesn't match either heuristic but IS a document
    # classify() correctly reads from content, and it must route through the
    # SAME ingest_* the heuristic path would have used -- not a bare
    # "doc_type": kind with none of that extractor's keys. Returning a
    # doc_type string this module doesn't actually populate crashed every
    # on-demand tick that touched one (KeyError in the caller's result
    # formatting), popped nothing, and the item never advanced.
    kind = classify(pdf, source=doc.rel)["doc_type"]
    if kind == "prescription":
        meds, bad, misfiled = ingest_prescription(
            con,
            doc.rel,
            doc.correspondent,
            ocr_text=ocr_text,
            doc_date=doc_date,
            paperless_id=paperless_id,
            abs_path=doc.path,
        )
        return {
            "doc_type": "prescription",
            "medications": meds,
            "uncorroborated": bad,
            "misfiled": misfiled,
        }

    if kind == "discharge":
        meds, encs, misfiled = ingest_discharge(
            con,
            rel_path=doc.rel,
            subject=doc.correspondent,
            doc_date=doc_date,
            paperless_id=paperless_id,
            abs_path=doc.path,
        )
        return {
            "doc_type": "discharge",
            "medications": meds,
            "encounters": encs,
            "misfiled": misfiled,
        }

    if kind == "lab":
        committed, queued = ingest_lab(
            con,
            rel_path=doc.rel,
            subject=doc.correspondent,
            doc_date=doc_date,
            paperless_id=paperless_id,
            abs_path=doc.path,
        )
        return {"doc_type": "lab", "committed": committed, "review": queued}

    if kind == "radiology":
        return _radiology_result(con, doc, ocr_text, paperless_id, doc_date)

    return {"doc_type": kind, "note": "no extractor for this document type yet"}


def _radiology_result(con, doc, ocr_text, paperless_id, doc_date) -> dict:
    """Ingest an imaging report and shape the result dict the callers format.

    Shared by both radiology entry points in ingest_document() -- the is_radiology()
    title heuristic and the classify() content fallback -- so they can never
    return differently-shaped results (the KeyError-in-formatting trap that once
    stranded queue items; see run_extract_queue.py::_result_text)."""
    reports, unreadable, misfiled = ingest_radiology(
        con,
        doc.rel,
        doc.correspondent,
        ocr_text=ocr_text,
        doc_date=doc_date,
        paperless_id=paperless_id,
        abs_path=doc.path,
    )
    return {
        "doc_type": "radiology",
        "reports": reports,
        "unreadable": unreadable,
        "misfiled": misfiled,
    }


def ingest_lab(
    con,
    rel_path: str,
    subject: str,
    doc_date: Optional[datetime.date] = None,
    paperless_id: Optional[int] = None,
    abs_path: Optional[str] = None,
) -> tuple[int, int]:
    """Ingest one lab report. Returns (observations committed, items for review)."""
    path = abs_path or source_path(rel_path)
    pdf = open(path, "rb").read()

    if doc_date is None:
        head = os.path.basename(rel_path)[:10]
        try:
            doc_date = datetime.date.fromisoformat(head)
        except ValueError:
            doc_date = None

    extraction = extract_lab(pdf, subject, rel_path, doc_date)

    # Whose report is this REALLY? The single authoritative rule -- the same one
    # the prescription and radiology paths already use -- NOT check_document()/
    # `extraction.usable`, which only compares the printed name to the folder and
    # so refuses a newborn's own screen for being labelled "B/O <mother>". A lab
    # naming another family member re-files to them; a stranger's name blocks it.
    actual, misfiled_to, reconciled = resolve_patient(rel_path, subject, extraction.patient)

    document_id = db.upsert_document(
        con,
        paperless_id=paperless_id,
        subject=actual,
        source_path=rel_path,
        doc_type="lab",
        doc_date=doc_date.isoformat() if doc_date else None,
        lab=extraction.patient.get("lab"),
        model=extraction.model,
        text_layer=bool(extraction.passed or extraction.quarantined),
    )

    # Names someone we cannot identify as anyone in this family. Trust nothing on it.
    if not reconciled:
        db.queue_review(
            con,
            document_id,
            [
                {
                    "subject": subject,
                    "kind": "patient_mismatch",
                    "printed_name": extraction.patient.get("name"),
                    "raw_value": None,
                    "reasons": json.dumps(extraction.doc_verdict.hard),
                }
            ],
        )
        con.commit()
        logger.error(f"{rel_path}: {extraction.doc_verdict.hard}")
        return 0, 1

    codebook, units = load_codebook(), load_units()
    ignored = load_ignored()
    resolved, _unmatched = resolve(extraction.passed, codebook)

    effective = _collection_date(extraction.patient, doc_date)
    observations, review = [], []

    # Every extracted result becomes an observation -- named or not, verified or
    # not. `analyte IS NULL` means "the codebook has not met this test yet", and
    # `status='review'` means "do not trust this number". Neither is a reason to
    # discard the row: the unit and the date are exactly what make it redeemable
    # later, for free, by `--reclassify`.
    for r in resolved + [{**q, "unverifiable": True} for q in extraction.quarantined]:
        analyte = r.get("analyte")
        raw = r.get("value", "")
        printed = r.get("name", "")
        reasons: list[str] = []
        status = "ok"

        # A name that matched no live analyte but IS a deliberately-dropped test
        # (echo measurement, ratio, CPAP metric, qualitative urine) is discarded
        # silently -- not sent to review. A live match always wins first, and an
        # ambiguous name still goes to review, so this only catches the tests we
        # chose not to track. Without it, every re-extraction re-floods the queue.
        if (
            analyte is None
            and not r.get("ambiguous")
            and is_ignored(printed, r.get("section", ""), ignored)
        ):
            continue

        if r.get("unverifiable"):
            status = "review"
            reasons = list(r.get("reasons", []))
        elif not analyte:
            status = "review"
            reasons = (
                ["ambiguous: two results in this report claim this analyte"]
                if r.get("ambiguous")
                else ["no codebook entry for this test name"]
            )

        number, qualitative = parse_value(raw)
        value_num: Optional[float] = None
        value_text: Optional[str] = None

        if number is not None and analyte:
            converted, _canonical, reason = convert(analyte, number, r.get("unit", ""), units)
            if reason:
                status, reasons = "review", [reason]
            else:
                value_num = converted
        elif number is not None:
            # No codebook entry, so no canonical unit to convert into. Keep the
            # number as printed; --reclassify converts it once the analyte lands.
            value_num = number
        elif qualitative:
            value_text = qualitative
        elif raw:
            value_text = raw  # free text: a USG impression, a TMT conclusion

        low, high = _reference_range(r.get("reference_range", ""))
        observations.append(
            {
                "subject": actual,
                "segment": codebook[analyte].get("segment") if analyte else None,
                "analyte": analyte,
                "printed_name": printed,
                "section": r.get("section"),
                "effective": effective,
                "value_num": value_num,
                "value_text": value_text,
                "raw_value": raw,
                "unit": r.get("unit"),
                "ref_low": low,
                "ref_high": high,
                "source_quality": "image" if r.get("unverifiable") else "text",
                "status": status,
                "review_reason": json.dumps(reasons) if reasons else None,
            }
        )

    # Re-ingesting a lab must REPLACE its observations, not accumulate them. The
    # observations UNIQUE key includes `effective`, and trusted_date can now yield
    # a different effective for the same document than a prior run did (the filename
    # date supersedes a printed date it once tolerated), so INSERT OR IGNORE would
    # see a new key and add a SECOND row rather than dedupe -- one draw trending as
    # two points. Observations carry no human-entered rows (promotions live in the
    # codebook and re-resolve on re-ingest; corrections go through review), so a
    # clean delete-and-reinsert is safe -- the same rule radiology already uses.
    con.execute("DELETE FROM observations WHERE document_id = ?", (document_id,))
    committed = db.insert_observations(con, document_id, observations)
    queued = db.queue_review(con, document_id, review)
    con.commit()
    return committed, queued


MAX_DATE_DRIFT_DAYS = 15


def trusted_date(
    printed_text: str,
    filename_date: Optional[datetime.date],
    what: str = "",
    max_drift: int = MAX_DATE_DRIFT_DAYS,
) -> Optional[str]:
    """The date to record for a document.

    The uploader owns the filename. Every file arrives named `YYYY-MM-DD - ...`
    (or with that date in its Telegram caption), and the uploader guarantees that
    date is the event's REAL clinical date -- the day the sample was collected,
    the day of discharge -- not the day it happened to be scanned. So the filename
    date is authoritative and always wins when present; the date the model read
    off the page is only a fallback, used for a document that arrived without one.

    This deliberately reverses the older "printed date wins within tolerance"
    rule. The model misread dates in ways nothing caught: a prescription filed
    2024-06-14, its drugs marked "till delivery", recorded 2024-08-24 -- a month
    AFTER the delivery it preceded; an arterial Doppler that moved two months when
    04/02 was read as 02/04, the day/month swap being the commonest way a date goes
    wrong. The filename cannot be misread, and the uploader vouches for it, so it is
    trusted over the page unconditionally -- a wide disagreement is logged, not
    obeyed.
    """
    from src.validator import _parse_date

    printed = _parse_date(printed_text or "")
    if not filename_date:
        return printed.isoformat() if printed else None

    if printed and abs((printed - filename_date).days) > max_drift and what:
        logger.info(
            f"{what}: the model read {printed} off the page, "
            f"{abs((printed - filename_date).days)} days from the filename's "
            f"({filename_date}); using the filename (authoritative)."
        )
    return filename_date.isoformat()


def _collection_date(patient: dict, fallback: Optional[datetime.date]) -> Optional[str]:
    """The collection date printed on the report -- unless it cannot be true."""
    return trusted_date(patient.get("collected_at", "") or "", fallback)


def _reference_range(text: str) -> tuple[Optional[float], Optional[float]]:
    """Parse the lab's printed range, e.g. '0.6 - 1.2', '< 200', '> 40'.

    This is the range values are FLAGGED against: labs revise their ranges and
    their methods, so the printed one is current in a way a frozen sheet is not.

    The separator is a minus sign to a naive regex: '0.6 - 1.2' once parsed as
    (0.6, -1.2), which would have marked nearly every result abnormal. So the
    range dash is neutralised before any number is read.
    """
    import re

    cleaned = (text or "").replace(",", "")
    # A dash BETWEEN two numbers is a separator, not a sign.
    cleaned = re.sub(r"(?<=[\d.])\s*[-–—]\s*(?=[\d.])", " ", cleaned)

    nums = re.findall(r"-?\d+(?:\.\d+)?", cleaned)
    if len(nums) >= 2:
        low, high = float(nums[0]), float(nums[1])
        return (low, high) if low <= high else (high, low)
    if len(nums) == 1 and ("<" in cleaned or "upto" in cleaned.lower()):
        return None, float(nums[0])
    if len(nums) == 1 and ">" in cleaned:
        return float(nums[0]), None
    return None, None


def ingest_discharge(
    con,
    rel_path: str,
    subject: str,
    doc_date: Optional[datetime.date] = None,
    paperless_id: Optional[int] = None,
    abs_path: Optional[str] = None,
) -> tuple[int, int, Optional[str]]:
    """Ingest a discharge summary. Returns (medications, encounters, misfiled_to).

    A discharge summary is the highest-stakes document in the corpus: it carries
    the medication list. Filing it against the wrong person puts one person's
    drugs in another person's record.

    And some ARE misfiled: a mother's surgical and delivery summaries can sit in
    the CHILD's folder, because that folder is organised around the pregnancy.
    So when the document names a different patient than the folder, the DOCUMENT
    wins if we can identify who it actually names; otherwise nothing is committed
    and it goes to review. We never quietly accept the folder's word.

    Every medication lands with status='review'. A drug list is not a lab value:
    it is acted on, and a hallucinated or misread drug name is dangerous in a way
    a wrong cholesterol reading is not. A human confirms each one.
    """
    from src.extractor import extract_discharge

    path = abs_path or source_path(rel_path)
    pdf = open(path, "rb").read()

    if doc_date is None:
        head = os.path.basename(rel_path)[:10]
        try:
            doc_date = datetime.date.fromisoformat(head)
        except ValueError:
            doc_date = None

    d = extract_discharge(pdf, subject, rel_path, doc_date)

    # Whose document is this, really? The single authoritative rule -- shared with
    # the prescription and radiology paths -- NOT check_document()/`d.usable`. A
    # neonatal discharge is labelled "B/O <mother>" and would fail a name-vs-folder
    # check, yet it is perfectly reconciled: the folder says WHICH child, and the
    # mother is named only to identify him. resolve_patient() knows that; trusting
    # `d.usable` refused a premature infant's own NICU and admission summaries.
    printed = (d.patient.get("name") or "").strip()
    actual, misfiled_to, reconciled = resolve_patient(rel_path, subject, d.patient)

    document_id = db.upsert_document(
        con,
        paperless_id=paperless_id,
        subject=actual,
        source_path=rel_path,
        doc_type="discharge",
        doc_date=doc_date.isoformat() if doc_date else None,
        lab=d.encounter.get("hospital"),
        model=d.model,
        text_layer=d.text_layer,
    )

    # Names someone we cannot identify as anyone in this family. Commit nothing.
    if not reconciled:
        db.queue_review(
            con,
            document_id,
            [
                {
                    "subject": subject,
                    "kind": "patient_mismatch",
                    "printed_name": printed,
                    "raw_value": None,
                    "reasons": json.dumps(d.doc_verdict.hard),
                }
            ],
        )
        con.commit()
        logger.error(f"{rel_path}: names {printed!r}, filed under {subject!r} -- not committed")
        return 0, 0, None

    enc = d.encounter

    # The uploader names the file with the encounter's real date -- the discharge
    # date for an admission -- so the filename date is the authoritative anchor.
    # The model only fills what the filename cannot carry: for a MULTI-DAY stay the
    # filename is silent on the admit date, so a valid earlier admit date the model
    # read is kept; anything missing, unparseable, or later than discharge collapses
    # to the filename date (a same-day visit then shows one date, not a "?").
    discharged = (
        doc_date.isoformat()
        if doc_date
        else (_iso(enc.get("discharged")) or _iso(enc.get("admitted")))
    )
    admitted = _iso(enc.get("admitted"))
    if not admitted or (discharged and admitted > discharged):
        admitted = discharged

    n_enc = 0
    if enc.get("admitted") or enc.get("discharged") or enc.get("diagnoses"):
        con.execute(
            """INSERT OR IGNORE INTO encounters
                 (document_id, subject, hospital, admitted, discharged, reason,
                  diagnoses, procedures, follow_up, follow_up_date)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id,
                actual,
                enc.get("hospital"),
                admitted,
                discharged,
                enc.get("reason"),
                json.dumps(enc.get("diagnoses") or []),
                json.dumps(enc.get("procedures") or []),
                enc.get("follow_up"),
                _iso(enc.get("follow_up_date")),
            ),
        )
        n_enc = 1

    effective = discharged

    # Re-ingesting a document must REPLACE its medications, not add another copy.
    # Without this, every re-run duplicated them. Human corrections are kept: a
    # person's decision outranks a re-read, and re-extracting must never silently
    # discard it.
    reviewed = {
        r["raw_text"]
        for r in con.execute(
            "SELECT raw_text FROM medication_events "
            "WHERE document_id = ? AND entered_by = 'human'",
            (document_id,),
        )
    }
    con.execute(
        "DELETE FROM medication_events WHERE document_id = ? AND entered_by = 'extractor'",
        (document_id,),
    )

    n_med = 0
    for m in d.medications:
        if json.dumps(m, ensure_ascii=False) in reviewed:
            continue  # a human already ruled on this line
        drug = (m.get("drug") or "").strip()
        if not drug:
            continue
        con.execute(
            """INSERT INTO medication_events
                 (document_id, subject, drug, strength, form, dose, frequency,
                  duration, event, effective, raw_text, entered_by, status,
                  review_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id,
                actual,
                drug,
                m.get("strength"),
                m.get("form"),
                m.get("dose"),
                m.get("frequency"),
                m.get("duration"),
                "prescribed",
                effective,
                json.dumps(m, ensure_ascii=False),
                "extractor",
                "review",
                json.dumps(["a drug list is acted on; a human confirms each entry"]),
            ),
        )
        n_med += 1

    con.commit()
    return n_med, n_enc, misfiled_to


def _iso(text: Optional[str]) -> Optional[str]:
    from src.validator import _parse_date

    d = _parse_date((text or "").strip())
    return d.isoformat() if d else None


def resolve_patient(
    rel_path: str,
    subject: str,
    patient: dict,
) -> tuple[str, Optional[str], bool]:
    """Whose document is this REALLY? Returns (file_under, misfiled_to, reconciled).

    `reconciled` is False ONLY when the document names a person we cannot identify
    as anyone in this family. That is the one case where nothing may be committed:
    we do not know whose record this is, and guessing is how one person's medical
    history ends up in another's.

    It is NOT the same question as `validator.check_document().ok`, and conflating
    the two is a bug this function exists to prevent. check_document() compares the
    printed name against the folder and nothing else -- so a newborn's echo, printed
    "BABY OF <mother>" and correctly filed in the baby's folder, FAILS it. Trusting
    that verdict refused two of an infant's own scans. The document is perfectly
    well reconciled; it simply names the mother, because that is how neonatal charts
    are labelled.

    The single implementation of the rule that matters most in this system. It
    used to be copy-pasted per document type, and that is precisely how it
    accumulated three bugs, each one introduced by the fix for the last. There is
    one copy now, and every caller gets the same answer.

    The document usually wins over the folder: a mother's surgical discharge sits
    in her child's folder because that folder is organised around the pregnancy,
    and it belongs to her.

    EXCEPT when the document names a baby by its parent. A neonatal chart is
    labelled "B/O <mother>", and reading that as "this is the mother's document"
    moved a premature infant's retinopathy report into his mother's record. The
    folder knows WHICH child; the document names the parent only to identify it.

    And a folder is never overruled on a name alone when the document gives no age
    to argue with: handwriting turns "B/O Alice Doe" into "Rlo Alice Dohe", whose
    only legible token is the mother's given name. A wrongly re-filed child's
    record is worse than one left where it already was.
    """
    from src.people import load_people, shared_name_tokens
    from src.validator import names_a_baby, parse_age_years, patient_matches

    misfiled_to: Optional[str] = None
    printed = (patient.get("name") or "").strip()
    printed_age = (patient.get("age") or "").strip()
    people = load_people()
    shared = shared_name_tokens()

    folder_person = people.get(subject)
    printed_years = parse_age_years(printed_age)

    # No name printed at all: the folder is all we have, and it is authoritative.
    if not printed:
        return subject, None, True

    if names_a_baby(printed, printed_age):
        if not folder_person or not folder_person.child:
            logger.warning(
                f"{rel_path}: reads as a child's document ({printed!r}, age "
                f"{printed_age!r}) but the folder is not a child's ({subject!r}). "
                f"Filing per the folder; please check."
            )
        # Reconciled either way: we know WHICH child from the folder. The document
        # names the parent only to identify the child, and that is not a mismatch.
        return subject, None, True

    if folder_person and folder_person.child and printed_years is None:
        if not patient_matches(printed, subject, shared):
            logger.warning(
                f"{rel_path}: names {printed!r} in a child's folder but prints no "
                f"age. Keeping the folder's answer ({subject!r}); please check."
            )
        return subject, None, True

    if patient_matches(printed, subject, shared):
        return subject, None, True

    for candidate in people.values():
        if patient_matches(printed, candidate.correspondent, shared):
            misfiled_to = candidate.correspondent
            return misfiled_to, misfiled_to, True

    # Names someone who is not in this family. We do not know whose document this
    # is, so nothing on it may be committed to anyone.
    return subject, None, False


def ingest_prescription(
    con,
    rel_path: str,
    subject: str,
    ocr_text: Optional[str] = None,
    doc_date: Optional[datetime.date] = None,
    paperless_id: Optional[int] = None,
    abs_path: Optional[str] = None,
) -> tuple[int, int, Optional[str]]:
    """Ingest a consultation / prescription. Returns (meds, uncorroborated, misfiled_to).

    Consultations are where medicines CHANGE, so this is the document type the
    live medicine list actually depends on.

    A drug the model reported but the independent oracle never saw is committed
    with status='review'. It is not dropped -- the oracle may simply have failed
    to read a smudged line -- but it is never trusted. A wrong drug name is the
    most dangerous thing this system can emit.
    """
    from src.extractor import extract_prescription

    path = abs_path or source_path(rel_path)
    pdf = open(path, "rb").read()

    if doc_date is None:
        head = os.path.basename(rel_path)[:10]
        try:
            doc_date = datetime.date.fromisoformat(head)
        except ValueError:
            doc_date = None

    p = extract_prescription(pdf, subject, rel_path, ocr_text=ocr_text, expected_date=doc_date)

    printed = (p.patient.get("name") or "").strip()
    actual, misfiled_to, reconciled = resolve_patient(rel_path, subject, p.patient)

    cons = p.consultation
    document_id = db.upsert_document(
        con,
        paperless_id=paperless_id,
        subject=actual,
        source_path=rel_path,
        doc_type="prescription",
        doc_date=doc_date.isoformat() if doc_date else None,
        lab=cons.get("facility"),
        model=p.model,
        text_layer=p.oracle_source == "text_layer",
    )

    # Names someone we cannot identify as anyone in this family: commit nothing.
    # This asks resolve_patient(), NOT check_document(). check_document() compares
    # the printed name to the folder and knows nothing about how a newborn's chart
    # is labelled, so it fails an infant's own echo for naming its mother.
    if not reconciled:
        db.queue_review(
            con,
            document_id,
            [
                {
                    "subject": subject,
                    "kind": "patient_mismatch",
                    "printed_name": printed,
                    "raw_value": None,
                    "reasons": json.dumps(p.doc_verdict.hard),
                }
            ],
        )
        con.commit()
        logger.error(f"{rel_path}: names {printed!r}, filed under {subject!r} -- not committed")
        return 0, 0, None

    # NOT just `_iso(cons.get("date")) or doc_date`. The consultation date read off
    # the page was taken on trust, and a misread put a prescription's drugs two
    # months into the future -- past the delivery their duration said they ran until.
    effective = trusted_date(cons.get("date") or "", doc_date, what=rel_path)

    # The consultation itself: diagnosis, and the follow-up that becomes a reminder.
    if cons.get("diagnosis") or cons.get("follow_up") or cons.get("doctor"):
        con.execute(
            """INSERT OR IGNORE INTO encounters
                 (document_id, subject, hospital, admitted, discharged, reason,
                  diagnoses, procedures, follow_up, follow_up_date)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id,
                actual,
                cons.get("facility"),
                effective,
                effective,
                cons.get("complaints"),
                json.dumps(cons.get("diagnosis") or []),
                json.dumps(cons.get("investigations") or []),
                cons.get("follow_up"),
                _iso(cons.get("follow_up_date")),
            ),
        )

    # Re-ingesting a document must REPLACE its medications, not add another copy.
    # Without this, every re-run duplicated them. Human corrections are kept: a
    # person's decision outranks a re-read, and re-extracting must never silently
    # discard it.
    reviewed = {
        r["raw_text"]
        for r in con.execute(
            "SELECT raw_text FROM medication_events "
            "WHERE document_id = ? AND entered_by = 'human'",
            (document_id,),
        )
    }
    con.execute(
        "DELETE FROM medication_events WHERE document_id = ? AND entered_by = 'extractor'",
        (document_id,),
    )

    n_med = n_uncorroborated = 0
    for m in p.medications:
        if json.dumps(m, ensure_ascii=False) in reviewed:
            continue  # a human already ruled on this line
        drug = (m.get("drug") or "").strip()
        corroborated = bool(m.get("corroborated"))
        if not corroborated:
            n_uncorroborated += 1

        reason = None
        if not corroborated:
            reason = json.dumps(
                [
                    f"the drug name was not corroborated by the independent reading "
                    f"({p.oracle_source or 'no oracle available'})"
                ]
            )

        con.execute(
            """INSERT INTO medication_events
                 (document_id, subject, drug, strength, form, dose, frequency,
                  duration, event, effective, raw_text, entered_by, status,
                  review_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id,
                actual,
                drug,
                m.get("strength"),
                m.get("form"),
                m.get("dose"),
                m.get("frequency"),
                m.get("duration"),
                "prescribed",
                effective,
                json.dumps(m, ensure_ascii=False),
                "extractor",
                # Corroborated by an independent reading of the page -> trusted.
                # Not corroborated -> review. That distinction is the entire point
                # of having an oracle; marking everything 'review' throws it away.
                "ok" if corroborated else "review",
                reason,
            ),
        )
        n_med += 1

    con.commit()
    return n_med, n_uncorroborated, misfiled_to


def ingest_radiology(
    con,
    rel_path: str,
    subject: str,
    ocr_text: Optional[str] = None,
    doc_date: Optional[datetime.date] = None,
    paperless_id: Optional[int] = None,
    abs_path: Optional[str] = None,
) -> tuple[int, int, Optional[str]]:
    """Ingest an imaging report. Returns (reports_written, untrusted, misfiled_to).

    An imaging report is narrative, so it is stored as ONE verbatim text record in
    `radiology_reports`, not exploded into per-parameter `observations`. Nobody
    trends a single radiology number over years; they read the report -- and
    forcing the prose into an analyte-shaped table produced junk (see the
    radiology_reports schema comment). Paperless already OCRs and full-text-searches
    the PDF; what health.db adds is a browsable, bucketed, person-scoped record.

    Three fields matter and each keeps this system's guarantees:
      - the printed patient name still gates the whole document (resolve_patient) --
        the correspondent-is-the-patient rule is not relaxed for radiology;
      - `report_text` is the entire report, verbatim, from whichever independent
        reading is fuller (embedded text layer or Paperless OCR);
      - `impression` is the radiologist's own conclusion, word for word, or NULL --
        never a descriptive line promoted to a conclusion (see Radiology.impression).
    """
    from src.extractor import extract_radiology, text_layer
    from src.radiology import study_bucket

    # Does another extractor already own this document? Asked FIRST, before the file
    # is even opened: this walks an rclone mount that fetches the whole object on
    # open, so a skip must cost nothing.
    #
    # Two routers can claim one file and nothing arbitrates between them: is_lab()
    # calls EVERY document tagged Medical/Reports a lab, and the page-1 classifier
    # calls an echo in that same folder radiology. Both are "right". Meanwhile
    # upsert_document() conflicts on source_path and never updates doc_type, so the
    # first extractor to run owns the label for good -- and this function's
    # DELETE ... WHERE document_id = ? would then wipe THAT extractor's rows.
    #
    # It did: 448 lab observations across 20 documents, silently replaced.
    #
    # Trusting the classifier instead is not the fix. It called a health-checkup
    # panel (105 real lab values) "radiology". Neither source of truth is reliable
    # enough to overrule the other, so this refuses to choose. The document is left
    # exactly as it is and reported, and a human decides who owns it.
    owner = con.execute(
        "SELECT doc_type FROM documents WHERE source_path = ?", (rel_path,)
    ).fetchone()
    if owner and owner["doc_type"] != "radiology":
        logger.warning(
            f"{rel_path}: already ingested as {owner['doc_type']!r}. Skipped -- "
            f"extracting it as radiology would delete that extractor's observations."
        )
        return 0, 0, None

    path = abs_path or source_path(rel_path)
    pdf = open(path, "rb").read()

    if doc_date is None:
        head = os.path.basename(rel_path)[:10]
        try:
            doc_date = datetime.date.fromisoformat(head)
        except ValueError:
            doc_date = None

    r = extract_radiology(pdf, subject, rel_path, ocr_text=ocr_text, expected_date=doc_date)

    printed = (r.patient.get("name") or "").strip()
    actual, misfiled_to, reconciled = resolve_patient(rel_path, subject, r.patient)

    study = r.study
    document_id = db.upsert_document(
        con,
        paperless_id=paperless_id,
        subject=actual,
        source_path=rel_path,
        doc_type="radiology",
        doc_date=doc_date.isoformat() if doc_date else None,
        lab=r.patient.get("lab"),
        model=r.model,
        text_layer=r.oracle_source == "text_layer",
    )

    # Names someone we cannot identify as anyone in this family: commit nothing.
    # A newborn's echo is printed "BABY OF <mother>" and belongs in the baby's
    # folder. resolve_patient() knows that; check_document() does not, and asking
    # the latter refused two of an infant's own scans.
    if not reconciled:
        db.queue_review(
            con,
            document_id,
            [
                {
                    "subject": subject,
                    "kind": "patient_mismatch",
                    "printed_name": printed,
                    "raw_value": None,
                    "reasons": json.dumps(r.doc_verdict.hard),
                }
            ],
        )
        con.commit()
        logger.error(f"{rel_path}: names {printed!r}, filed under {subject!r} -- not committed")
        return 0, 0, None

    effective = _collection_date(r.patient, doc_date)

    # The entire report, verbatim, is what we keep. Prefer whichever independent
    # reading is fuller: a text-native PDF carries an embedded text layer, a scanned
    # one only Paperless's OCR.
    embedded = text_layer(pdf) or ""
    fuller = embedded if len(embedded.strip()) >= len((ocr_text or "").strip()) else ocr_text
    report_text = (fuller or "").strip() or None

    study_type = study_bucket(os.path.basename(rel_path), study)

    # Radiology no longer produces observations; clear any this document wrote
    # under the old per-parameter extractor before storing the single text record.
    con.execute("DELETE FROM observations WHERE document_id = ?", (document_id,))

    # A report we could not read at all (no text layer, no OCR) is not filed as an
    # empty record -- "we couldn't read it" goes to review, as everywhere else.
    if not report_text:
        db.queue_review(
            con,
            document_id,
            [
                {
                    "subject": actual,
                    "kind": "unreadable_radiology",
                    "printed_name": study_type,
                    "raw_value": None,
                    "reasons": json.dumps(["no text layer and no OCR to read the report from"]),
                }
            ],
        )
        con.commit()
        logger.warning(f"{rel_path}: no readable text -- sent to review")
        return 0, 1, misfiled_to

    db.upsert_radiology_report(
        con,
        document_id=document_id,
        subject=actual,
        study_type=study_type,
        effective=effective,
        impression=r.impression,
        report_text=report_text,
    )
    con.commit()
    return 1, 0, misfiled_to

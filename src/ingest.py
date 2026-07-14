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
from src.constants import MEDICAL_ROOT
from src.extractor import extract_lab
from src.normalize import load_codebook, parse_value, resolve
from src.units import convert, load_units

logger = logging.getLogger(__name__)


def ingest_document(
    con,
    doc,
    ocr_text: Optional[str] = None,
    paperless_id: Optional[int] = None,
) -> dict:
    """Route one document to its extractor and ingest it. Returns a result dict.

    Same precedence run_extract.py's nightly batch passes use (is_lab ->
    ingest_lab, is_discharge -> ingest_discharge, else classify() ->
    prescription -> ingest_prescription), now shared with the on-demand queue
    (run_extract_queue.py) so nightly and on-demand routing can never drift
    apart. Radiology is deliberately excluded: it has no trusted extractor yet
    (see CLAUDE.md), so it stays a manual, explicit `--radiology` pass.

    ``ocr_text`` is only useful to the prescription branch (labs and discharge
    summaries corroborate off the PDF's own text layer; see src/extractor.py).
    """
    from src.extractor import classify, is_discharge, is_encrypted, is_lab

    if doc.suffix != ".pdf":
        return {"doc_type": "unsupported", "note": "not a PDF"}

    doc_date: Optional[datetime.date] = None
    if doc.created:
        try:
            doc_date = datetime.date.fromisoformat(doc.created)
        except ValueError:
            doc_date = None

    if is_lab(doc):
        committed, queued = ingest_lab(
            con,
            rel_path=doc.rel,
            subject=doc.correspondent,
            doc_date=doc_date,
            paperless_id=paperless_id,
        )
        return {"doc_type": "lab", "committed": committed, "review": queued}

    if is_discharge(doc):
        meds, encs, misfiled = ingest_discharge(
            con,
            rel_path=doc.rel,
            subject=doc.correspondent,
            doc_date=doc_date,
            paperless_id=paperless_id,
        )
        return {
            "doc_type": "discharge",
            "medications": meds,
            "encounters": encs,
            "misfiled": misfiled,
        }

    path = os.path.join(MEDICAL_ROOT, doc.rel)
    pdf = open(path, "rb").read()
    if is_encrypted(pdf):
        return {"doc_type": "encrypted", "note": "password-protected, cannot extract"}

    kind = classify(pdf, source=doc.rel)["doc_type"]
    if kind == "prescription":
        meds, bad, misfiled = ingest_prescription(
            con,
            doc.rel,
            doc.correspondent,
            ocr_text=ocr_text,
            doc_date=doc_date,
            paperless_id=paperless_id,
        )
        return {
            "doc_type": "prescription",
            "medications": meds,
            "uncorroborated": bad,
            "misfiled": misfiled,
        }

    return {"doc_type": kind, "note": "no extractor for this document type yet"}


def ingest_lab(
    con,
    rel_path: str,
    subject: str,
    doc_date: Optional[datetime.date] = None,
    paperless_id: Optional[int] = None,
) -> tuple[int, int]:
    """Ingest one lab report. Returns (observations committed, items for review)."""
    path = os.path.join(MEDICAL_ROOT, rel_path)
    pdf = open(path, "rb").read()

    if doc_date is None:
        head = os.path.basename(rel_path)[:10]
        try:
            doc_date = datetime.date.fromisoformat(head)
        except ValueError:
            doc_date = None

    extraction = extract_lab(pdf, subject, rel_path, doc_date)

    document_id = db.upsert_document(
        con,
        paperless_id=paperless_id,
        subject=subject,
        source_path=rel_path,
        doc_type="lab",
        doc_date=doc_date.isoformat() if doc_date else None,
        lab=extraction.patient.get("lab"),
        model=extraction.model,
        text_layer=bool(extraction.passed or extraction.quarantined),
    )

    # The report says it belongs to someone else. Trust nothing on it.
    if not extraction.usable:
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
                "subject": subject,
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
    """The date to record, when the document and the filename disagree.

    The date printed on the page is normally the better one -- a lab report knows
    when the sample was collected, and the file may have been scanned weeks later.
    So it won.

    It won even when it was nonsense. A prescription filed 2024-06-14, whose drugs
    are marked "till delivery", was recorded as effective 2024-08-24 -- a month AFTER
    the delivery it was written for -- because the model misread the date off the
    page and nothing checked. An arterial Doppler moved two months when 04/02 was
    read as 02/04: the day/month swap, which is not an exotic failure, it is the
    single commonest way a date goes wrong.

    Filenames here begin with YYYY-MM-DD and are reliable. So: the printed date
    wins, UNLESS it disagrees with the filename by more than `max_drift` days -- at
    which point we do not know which is right, and this codebase does not guess. Take
    the filename, which is the one that cannot be misread, and say so out loud.
    """
    from src.validator import _parse_date

    printed = _parse_date(printed_text or "")
    if not printed:
        return filename_date.isoformat() if filename_date else None
    if not filename_date:
        return printed.isoformat()

    drift = abs((printed - filename_date).days)
    if drift > max_drift:
        logger.warning(
            f"{what}: the date read off the page ({printed}) is {drift} days from the "
            f"filename's ({filename_date}). Too far to be a scanning delay -- most "
            f"likely a misread, and a day/month swap is the usual one. Using the "
            f"filename."
        )
        return filename_date.isoformat()

    return printed.isoformat()


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
    from src.people import load_people, shared_name_tokens
    from src.validator import names_a_baby, parse_age_years, patient_matches

    path = os.path.join(MEDICAL_ROOT, rel_path)
    pdf = open(path, "rb").read()

    if doc_date is None:
        head = os.path.basename(rel_path)[:10]
        try:
            doc_date = datetime.date.fromisoformat(head)
        except ValueError:
            doc_date = None

    d = extract_discharge(pdf, subject, rel_path, doc_date)

    # Whose document is this, really? The document usually wins over the folder --
    # but NOT when it names a baby by its parent ("B/O <mother>"), which is how
    # neonatal records are labelled.
    misfiled_to: Optional[str] = None
    printed = (d.patient.get("name") or "").strip()
    printed_age = (d.patient.get("age") or "").strip()
    people = load_people()
    shared = shared_name_tokens()

    folder_person = people.get(subject)
    printed_years = parse_age_years(printed_age)

    if names_a_baby(printed, printed_age):
        if not folder_person or not folder_person.child:
            logger.warning(
                f"{rel_path}: reads as a child's document ({printed!r}, age "
                f"{printed_age!r}) but the folder is not a child's ({subject!r})."
            )
    elif (
        folder_person
        and folder_person.born
        and doc_date
        and doc_date.isoformat() < folder_person.born
    ):
        # The document predates the child's birth, so it cannot be the child's --
        # whatever the folder says. A pregnancy folder under a child's name holds
        # the MOTHER's consultations, and her progesterone and enoxaparin were
        # being recorded as her unborn son's medication.
        for candidate in people.values():
            if printed and patient_matches(printed, candidate.correspondent, shared):
                misfiled_to = candidate.correspondent
                break
        if not misfiled_to:
            logger.warning(
                f"{rel_path}: dated {doc_date} but {subject} was born "
                f"{folder_person.born}. Cannot be theirs, and the document names "
                f"{printed or 'nobody'}. Needs a human."
            )
    elif folder_person and folder_person.child and printed_years is None:
        if printed and not patient_matches(printed, subject, shared):
            logger.warning(
                f"{rel_path}: names {printed!r} in a child's folder but prints no "
                f"age. Keeping the folder's answer ({subject!r})."
            )
    elif printed and not patient_matches(printed, subject, shared):
        for candidate in people.values():
            if patient_matches(printed, candidate.correspondent, shared):
                misfiled_to = candidate.correspondent
                break

    actual = misfiled_to or subject

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

    # The document names someone we cannot identify. Commit nothing.
    if printed and not misfiled_to and not d.usable:
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
                _iso(enc.get("admitted")),
                _iso(enc.get("discharged")),
                enc.get("reason"),
                json.dumps(enc.get("diagnoses") or []),
                json.dumps(enc.get("procedures") or []),
                enc.get("follow_up"),
                _iso(enc.get("follow_up_date")),
            ),
        )
        n_enc = 1

    effective = _iso(enc.get("discharged")) or (doc_date.isoformat() if doc_date else None)

    # Re-ingesting a document must REPLACE its medications, not add another copy.
    # Without this, every re-run duplicated them -- one document was ingested six
    # times today (re-attribution, folder overrides) and its two drugs became
    # twelve rows. Human corrections are kept: a person's decision outranks a
    # re-read, and re-extracting must never silently discard it.
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

    path = os.path.join(MEDICAL_ROOT, rel_path)
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
    # Without this, every re-run duplicated them -- one document was ingested six
    # times today (re-attribution, folder overrides) and its two drugs became
    # twelve rows. Human corrections are kept: a person's decision outranks a
    # re-read, and re-extracting must never silently discard it.
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
) -> tuple[int, int, int, Optional[str]]:
    """Ingest an imaging report. Returns (measurements, findings, untrusted, misfiled_to).

    An imaging report holds two kinds of fact, and both become observations --
    because both are things that were true of a person on a date, which is what
    that table is.

    A MEASUREMENT lands as a number: value_num + unit. That is what makes it
    TRENDABLE -- "what has his ejection fraction done over five years" is a
    question a family health record should be able to answer, and it can only
    answer it if the 55 was stored as 55 and not buried inside a sentence.

    A FINDING lands as prose: value_text, verbatim. The impression is the single
    most important line in the document -- it is the radiologist's own conclusion,
    the thing a doctor reads first -- and it is stored word for word, never
    summarised. `printed_name` is 'Impression' for that row, so it can be found.

    `analyte` is left NULL for all of them: the codebook is a LAB codebook, and an
    ejection fraction is not in it. That is not a loss. `--reclassify` re-resolves
    NULL analytes for free, offline, whenever the codebook grows -- so adding
    "Ejection Fraction" to it later names every echo ever ingested, retroactively,
    at no cost.
    """
    from src.extractor import extract_radiology

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
        return 0, 0, 0, None

    path = os.path.join(MEDICAL_ROOT, rel_path)
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
        return 0, 0, 0, None

    effective = _collection_date(r.patient, doc_date)

    # The study title is the section a bare measurement belongs to. An echo that
    # prints "EF 55%" under no heading still belongs to "2D ECHOCARDIOGRAPHY", and
    # without that the row is an unattributed number.
    study_name = (study.get("name") or study.get("modality") or "").strip()

    # Re-ingesting must REPLACE this document's rows, not add a second copy. Every
    # observation is extractor-produced -- unlike medication_events, this table has
    # no entered_by column and carries no human rulings, so there is nothing here
    # to preserve. If observations ever become human-correctable, this delete has
    # to learn about it.
    con.execute("DELETE FROM observations WHERE document_id = ?", (document_id,))

    rows: list[dict] = []
    untrusted = 0
    n_meas = n_find = 0

    for m in r.measurements:
        raw = str(m.get("value") or "").strip()
        name = str(m.get("name") or "").strip()
        if not raw or not name:
            continue

        corroborated = bool(m.get("corroborated"))
        if not corroborated:
            untrusted += 1

        number, qualitative = parse_value(raw)
        low, high = _reference_range(str(m.get("reference_range") or ""))
        n_meas += 1

        rows.append(
            {
                "subject": actual,
                "segment": None,
                "analyte": None,
                "printed_name": name,
                "section": (m.get("section") or "").strip() or study_name,
                "effective": effective,
                # "1.2 x 0.8" is a dimension, not a number. It keeps its raw form
                # rather than being coerced into a float it is not.
                "value_num": number,
                "value_text": None if number is not None else (qualitative or raw),
                "raw_value": raw,
                "unit": (m.get("unit") or "").strip() or None,
                "ref_low": low,
                "ref_high": high,
                "source_quality": "text" if r.oracle_source == "text_layer" else "image",
                "status": "ok" if corroborated else "review",
                "review_reason": (
                    None
                    if corroborated
                    else json.dumps(
                        [
                            f"the value was not corroborated by the independent reading "
                            f"({r.oracle_source or 'no oracle available'})"
                        ]
                    )
                ),
            }
        )

    for f in r.findings:
        prose = str(f.get("text") or "").strip()
        if not prose:
            continue

        corroborated = bool(f.get("corroborated"))
        if not corroborated:
            untrusted += 1

        section = (f.get("section") or "").strip()
        is_impression = bool(f.get("is_impression"))
        n_find += 1

        rows.append(
            {
                "subject": actual,
                "segment": None,
                "analyte": None,
                # The impression is findable by name, from any report, in any
                # modality. Everything else is named for the structure it describes.
                "printed_name": "Impression" if is_impression else (section or "Finding"),
                "section": section or study_name,
                "effective": effective,
                "value_num": None,
                "value_text": prose,
                "raw_value": prose,
                "unit": None,
                "ref_low": None,
                "ref_high": None,
                "source_quality": "text" if r.oracle_source == "text_layer" else "image",
                "status": "ok" if corroborated else "review",
                "review_reason": (
                    None
                    if corroborated
                    else json.dumps(
                        [
                            f"only {f.get('coverage', 0.0):.0%} of the words in this finding "
                            f"appeared in the independent reading "
                            f"({r.oracle_source or 'no oracle available'})"
                        ]
                    )
                ),
            }
        )

    db.insert_observations(con, document_id, rows)
    con.commit()
    return n_meas, n_find, untrusted, misfiled_to

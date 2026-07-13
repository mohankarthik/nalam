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
            converted, _canonical, reason = convert(
                analyte, number, r.get("unit", ""), units
            )
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


def _collection_date(patient: dict, fallback: Optional[datetime.date]) -> Optional[str]:
    """Prefer the collection date printed on the report over the filename's."""
    from src.validator import _parse_date

    printed = _parse_date(patient.get("collected_at", "") or "")
    if printed:
        return printed.isoformat()
    return fallback.isoformat() if fallback else None


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
    from src.validator import names_a_baby, patient_matches

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

    if names_a_baby(printed, printed_age):
        if not people.get(subject, None) or not people[subject].child:
            logger.warning(
                f"{rel_path}: names a baby ({printed!r}) but the folder is not a "
                f"child's ({subject!r}). Filing per the folder; please check."
            )
    elif printed and not patient_matches(printed, subject, shared):
        for candidate in people.values():
            if patient_matches(printed, candidate.correspondent, shared):
                misfiled_to = candidate.correspondent
                break

    actual = misfiled_to or subject

    document_id = db.upsert_document(
        con,
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
            [{
                "subject": subject,
                "kind": "patient_mismatch",
                "printed_name": printed,
                "raw_value": None,
                "reasons": json.dumps(d.doc_verdict.hard),
            }],
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
    n_med = 0
    for m in d.medications:
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


def ingest_prescription(
    con,
    rel_path: str,
    subject: str,
    ocr_text: Optional[str] = None,
    doc_date: Optional[datetime.date] = None,
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
    from src.people import load_people, shared_name_tokens
    from src.validator import names_a_baby, patient_matches

    path = os.path.join(MEDICAL_ROOT, rel_path)
    pdf = open(path, "rb").read()

    if doc_date is None:
        head = os.path.basename(rel_path)[:10]
        try:
            doc_date = datetime.date.fromisoformat(head)
        except ValueError:
            doc_date = None

    p = extract_prescription(
        pdf, subject, rel_path, ocr_text=ocr_text, expected_date=doc_date
    )

    # Whose document is this, really? The document usually wins over the folder --
    # but NOT when it names a baby by its parent.
    misfiled_to: Optional[str] = None
    printed = (p.patient.get("name") or "").strip()
    printed_age = (p.patient.get("age") or "").strip()
    people = load_people()
    shared = shared_name_tokens()

    if names_a_baby(printed, printed_age):
        # "B/O Alice Doe", age "1 Month 14 Days": the patient is that person's
        # BABY, not that person. The folder knows which child; the document does
        # not. Re-filing here would move a premature infant's retinopathy notes
        # into his mother's record -- which is exactly what happened before this
        # check existed.
        if not people.get(subject, None) or not people[subject].child:
            logger.warning(
                f"{rel_path}: names a baby ({printed!r}) but the folder is not a "
                f"child's ({subject!r}). Filing per the folder; please check."
            )
    elif printed and not patient_matches(printed, subject, shared):
        for candidate in people.values():
            if patient_matches(printed, candidate.correspondent, shared):
                misfiled_to = candidate.correspondent
                break
    actual = misfiled_to or subject

    cons = p.consultation
    document_id = db.upsert_document(
        con,
        subject=actual,
        source_path=rel_path,
        doc_type="prescription",
        doc_date=doc_date.isoformat() if doc_date else None,
        lab=cons.get("facility"),
        model=p.model,
        text_layer=p.oracle_source == "text_layer",
    )

    # Names someone we cannot identify: commit nothing.
    if printed and not misfiled_to and not p.usable:
        db.queue_review(
            con,
            document_id,
            [{
                "subject": subject,
                "kind": "patient_mismatch",
                "printed_name": printed,
                "raw_value": None,
                "reasons": json.dumps(p.doc_verdict.hard),
            }],
        )
        con.commit()
        logger.error(f"{rel_path}: names {printed!r}, filed under {subject!r} -- not committed")
        return 0, 0, None

    effective = _iso(cons.get("date")) or (doc_date.isoformat() if doc_date else None)

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

    n_med = n_uncorroborated = 0
    for m in p.medications:
        drug = (m.get("drug") or "").strip()
        corroborated = bool(m.get("corroborated"))
        if not corroborated:
            n_uncorroborated += 1

        reason = None
        if not corroborated:
            reason = json.dumps([
                f"the drug name was not corroborated by the independent reading "
                f"({p.oracle_source or 'no oracle available'})"
            ])

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
                reason,
            ),
        )
        n_med += 1

    con.commit()
    return n_med, n_uncorroborated, misfiled_to

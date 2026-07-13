"""Extract structured observations from the lab reports. Phase 1 entry point.

Reads the source PDFs straight from the Drive mount, reusing the same walk the
Paperless sync uses (src/drive_sync.collect) -- so extraction does not wait on
Paperless' OCR queue, and the path to the file is never in doubt. Paperless owns
the scan and the full-text search; health.db owns the numbers.

Commits what survives validation to data/health.db. Everything else lands in the
review queue with a reason. Nothing is dropped.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import re

from src import cache, db
from src.drive_sync import collect
from src.extractor import (
    CLASSIFY_CONFIG,
    DISCHARGE_CONFIG,
    LAB_CONFIG,
    classify,
    is_encrypted,
)
from src.ingest import ingest_discharge, ingest_lab, ingest_prescription
from src.paperless import Paperless, ocr_for

logger = logging.getLogger(__name__)


def is_lab(doc) -> bool:
    """Is this a lab report?

    The folder says WHO the patient is (authoritative). It does not say what
    KIND of document this is: many lab reports sit outside a folder named
    'Reports' -- they land under a specialty or admission folder instead. Routing
    on the folder skipped every one of them.

    So: the Reports folder, OR a title that names a test. Keyword matching, not
    classification -- see the limits noted in data/configs/lab.json.
    """
    with open(LAB_CONFIG, encoding="utf-8") as f:
        routing = json.load(f)["routing"]

    if doc.tag in routing["tags"]:
        return True
    return any(
        re.search(p, doc.title, re.IGNORECASE) for p in routing["title_patterns"]
    )


def is_discharge(doc) -> bool:
    """Is this a discharge summary?

    Routed on the TITLE, not the folder: an Admissions folder groups an EPISODE, not a
    document type. Most of what is in it are the labs and scans from the stay;
    only a handful are actual summaries.
    """
    with open(DISCHARGE_CONFIG, encoding="utf-8") as f:
        routing = json.load(f)["routing"]
    return any(
        re.search(p, doc.title, re.IGNORECASE) for p in routing["title_patterns"]
    )


def run_discharge(con, limit: int = 0) -> None:
    docs, _skipped = collect()
    todo = [d for d in docs if d.suffix == ".pdf" and is_discharge(d)]
    done = {
        r["source_path"]
        for r in con.execute(
            "SELECT source_path FROM documents WHERE doc_type = 'discharge'"
        ).fetchall()
    }
    todo = [d for d in todo if d.rel not in done]
    if limit:
        todo = todo[:limit]

    logger.info(f"{len(todo)} discharge summaries to extract")
    for i, d in enumerate(todo, 1):
        doc_date = datetime.date.fromisoformat(d.created) if d.created else None
        try:
            meds, encs, misfiled = ingest_discharge(
                con, rel_path=d.rel, subject=d.correspondent, doc_date=doc_date
            )
        except Exception as e:
            logger.error(f"[{i}/{len(todo)}] {d.rel}: {e}")
            continue
        note = ""
        if misfiled:
            note = f"  ** MISFILED: folder says {d.correspondent}, document says {misfiled} -> filed under {misfiled}"
        logger.info(
            f"[{i}/{len(todo)}] {d.correspondent} | {d.title} -> "
            f"{encs} encounter, {meds} medications{note}"
        )


def classified_type(rel: str) -> str | None:
    """The cached classification for a document, if we have one. Free."""
    with open(CLASSIFY_CONFIG, encoding="utf-8") as f:
        prompt = json.load(f)["prompt"]
    hit = cache.load(rel, "classify", prompt)
    if not hit:
        return None
    return (hit.get("parsed") or {}).get("doc_type")


def run_classify(limit: int = 0) -> None:
    """Ask each document what it IS. Cached, so re-runs are free."""
    import collections
    import os

    from src.constants import MEDICAL_ROOT

    docs, _ = collect()
    pdfs = [d for d in docs if d.suffix == ".pdf"]
    todo = [d for d in pdfs if classified_type(d.rel) is None]
    cached = len(pdfs) - len(todo)
    if limit:
        todo = todo[:limit]

    logger.info(f"{len(pdfs)} PDFs: {cached} already classified, {len(todo)} to do")
    for i, d in enumerate(todo, 1):
        try:
            pdf = open(os.path.join(MEDICAL_ROOT, d.rel), "rb").read()
            if is_encrypted(pdf):
                # No model can read it, and no key is configured. Record the fact
                # rather than burning two API calls to be told so.
                cache.save(
                    source=d.rel, doc_type="classify",
                    prompt=open(CLASSIFY_CONFIG, encoding="utf-8").read(),
                    raw=json.dumps({"doc_type": "encrypted", "confidence": "high",
                                    "has_medications": False,
                                    "reason": "PDF is password protected"}),
                    parsed={"doc_type": "encrypted", "confidence": "high",
                            "has_medications": False,
                            "reason": "PDF is password protected"},
                    model="none",
                )
                logger.warning(f"[{i}/{len(todo)}] encrypted, skipped: {d.title[:40]}")
                continue
            c = classify(pdf, source=d.rel)
        except Exception as e:
            logger.error(f"[{i}/{len(todo)}] {d.rel}: {e}")
            continue
        if i % 25 == 0 or i == len(todo):
            logger.info(f"  [{i}/{len(todo)}] {c['doc_type']:<12} {d.title[:34]}")

    counts = collections.Counter(
        classified_type(d.rel) or "unclassified" for d in pdfs
    )
    logger.info("\nDocument types:")
    for t, n in counts.most_common():
        logger.info(f"  {n:>4}  {t}")


def run_prescriptions(con, limit: int = 0, person: str | None = None) -> None:
    """Ingest every document the classifier called a prescription."""
    docs, _ = collect()
    todo = [
        d for d in docs
        if d.suffix == ".pdf" and classified_type(d.rel) == "prescription"
    ]
    if person:
        todo = [d for d in todo if d.correspondent == person]

    done = {
        r["source_path"]
        for r in con.execute(
            "SELECT source_path FROM documents WHERE doc_type = 'prescription'"
        ).fetchall()
    }
    todo = [d for d in todo if d.rel not in done]
    if limit:
        todo = todo[:limit]

    logger.info(f"{len(todo)} prescriptions to extract")
    ocr = Paperless().ocr_index()
    misfiled = uncorroborated = 0

    for i, d in enumerate(todo, 1):
        try:
            n, bad, moved = ingest_prescription(
                con, d.rel, d.correspondent,
                ocr_text=ocr_for(ocr, d.correspondent, d.rel),
            )
        except Exception as e:
            logger.error(f"[{i}/{len(todo)}] {d.rel}: {e}")
            continue
        uncorroborated += bad
        if moved:
            misfiled += 1
            logger.warning(
                f"[{i}/{len(todo)}] MISFILED: {d.rel} names {moved}, not {d.correspondent}"
            )
        if i % 20 == 0 or i == len(todo):
            logger.info(f"  [{i}/{len(todo)}] {d.correspondent} | {d.title[:30]} -> {n} meds")

    logger.info(
        f"\ndone. {uncorroborated} drugs not corroborated by an independent reading "
        f"(-> review). {misfiled} documents were filed under the wrong person."
    )


def show_review(con) -> None:
    """What is in health.db but not trusted, and why."""
    total = con.execute("SELECT count(*) FROM observations").fetchone()[0]
    ok = con.execute(
        "SELECT count(*) FROM observations WHERE status = 'ok'"
    ).fetchone()[0]
    logger.info(f"{ok}/{total} observations trusted; {total - ok} need review")

    logger.info("\nUnnamed tests (add an alias, then --reclassify; no re-extraction):")
    rows = con.execute(
        """SELECT printed_name, section, count(*) n, count(DISTINCT subject) people
           FROM observations WHERE analyte IS NULL
           GROUP BY printed_name ORDER BY n DESC LIMIT 30"""
    ).fetchall()
    for r in rows:
        logger.info(
            f"  {r['n']:>3}x ({r['people']} people)  [{(r['section'] or '')[:18]:<18}] "
            f"{r['printed_name'][:44]}"
        )

    for r in con.execute(
        """SELECT count(*) n, review_reason FROM observations
           WHERE status='review' AND analyte IS NOT NULL
           GROUP BY review_reason ORDER BY n DESC LIMIT 10"""
    ):
        logger.info(f"  {r['n']:>3}x  {str(r['review_reason'])[:80]}")

    bad = con.execute(
        "SELECT count(*) FROM review_queue WHERE resolved = 0"
    ).fetchone()[0]
    if bad:
        logger.error(f"\n{bad} DOCUMENTS failed their patient check (possible misfile)")


def reclassify(con) -> None:
    """Re-resolve unnamed observations after the codebook or aliases changed."""
    from src.normalize import load_codebook, match

    codebook = load_codebook()

    def resolver(printed_name: str, section: str):
        analyte = match(printed_name, codebook, section)
        segment = codebook[analyte].get("segment") if analyte else None
        return analyte, segment

    before = con.execute(
        "SELECT count(*) FROM observations WHERE analyte IS NULL"
    ).fetchone()[0]
    fixed = db.reclassify(con, resolver)
    logger.info(f"named {fixed} of {before} previously-unnamed observations")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=0, help="Ingest at most N documents")
    p.add_argument("--person", help="Only this correspondent")
    p.add_argument("--discharge", action="store_true", help="Extract discharge summaries")
    p.add_argument("--classify", action="store_true", help="Ask each document what it IS (cached)")
    p.add_argument("--prescriptions", action="store_true", help="Extract consultations/prescriptions")
    p.add_argument("--review", action="store_true", help="Show what is not trusted, and why")
    p.add_argument("--reclassify", action="store_true",
                   help="Re-resolve unnamed observations after editing aliases (free, no LLM)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    con = db.connect()
    if args.review:
        show_review(con)
        return
    if args.reclassify:
        reclassify(con)
        return
    if args.classify:
        run_classify(limit=args.limit)
        return
    if args.discharge:
        run_discharge(con, limit=args.limit)
        return
    if args.prescriptions:
        run_prescriptions(con, limit=args.limit, person=args.person)
        return

    docs, _skipped = collect()
    labs = [d for d in docs if d.suffix == ".pdf" and is_lab(d)]
    if args.person:
        labs = [d for d in labs if d.correspondent == args.person]

    done = {
        r["source_path"]
        for r in con.execute("SELECT source_path FROM documents").fetchall()
    }
    todo = [d for d in labs if d.rel not in done]
    if args.limit:
        todo = todo[: args.limit]

    logger.info(f"{len(labs)} lab PDFs, {len(todo)} to extract")
    for i, d in enumerate(todo, 1):
        doc_date = datetime.date.fromisoformat(d.created) if d.created else None
        try:
            committed, queued = ingest_lab(
                con, rel_path=d.rel, subject=d.correspondent, doc_date=doc_date
            )
            logger.info(
                f"[{i}/{len(todo)}] {d.correspondent} | {d.title} -> "
                f"{committed} observations, {queued} to review"
            )
        except Exception as e:
            logger.error(f"[{i}/{len(todo)}] {d.rel}: {e}")


if __name__ == "__main__":
    main()

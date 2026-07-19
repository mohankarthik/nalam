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
import json
import logging

from src import cache, db
from src.drive_sync import collect
from src.extractor import (
    CLASSIFY_CONFIG,
    classify,
    is_discharge,
    is_encrypted,
    is_lab,
    is_radiology,
)
from src.ingest import ingest_document, ingest_radiology
from src.paperless import Paperless, id_for, ocr_for

logger = logging.getLogger(__name__)


def run_discharge(con, limit: int = 0) -> None:
    docs, _skipped = collect()
    # Title heuristic OR the page-1 classifier, like run_radiology(): a discharge
    # summary with an opaque title (an admission abbreviated to "NICU" / "UTI")
    # trips no title pattern, and without the classifier backstop it fell through
    # every pass and was silently never ingested. classify() read the page and
    # called it a discharge; honour that.
    todo = [
        d
        for d in docs
        if d.suffix == ".pdf" and (is_discharge(d) or classified_type(d.rel) == "discharge")
    ]
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
    paperless_ids = Paperless().document_id_index()
    for i, d in enumerate(todo, 1):
        try:
            result = ingest_document(
                con, d, paperless_id=id_for(paperless_ids, d.correspondent, d.rel)
            )
        except Exception as e:
            logger.error(f"[{i}/{len(todo)}] {d.rel}: {e}")
            continue
        note = ""
        if result.get("misfiled"):
            note = (
                f"  ** MISFILED: folder says {d.correspondent}, "
                f"document says {result['misfiled']} -> filed under {result['misfiled']}"
            )
        logger.info(
            f"[{i}/{len(todo)}] {d.correspondent} | {d.title} -> "
            f"{result.get('encounters', 0)} encounter, "
            f"{result.get('medications', 0)} medications{note}"
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
                    source=d.rel,
                    doc_type="classify",
                    prompt=open(CLASSIFY_CONFIG, encoding="utf-8").read(),
                    raw=json.dumps(
                        {
                            "doc_type": "encrypted",
                            "confidence": "high",
                            "has_medications": False,
                            "reason": "PDF is password protected",
                        }
                    ),
                    parsed={
                        "doc_type": "encrypted",
                        "confidence": "high",
                        "has_medications": False,
                        "reason": "PDF is password protected",
                    },
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

    counts = collections.Counter(classified_type(d.rel) or "unclassified" for d in pdfs)
    logger.info("\nDocument types:")
    for t, n in counts.most_common():
        logger.info(f"  {n:>4}  {t}")


def run_prescriptions(con, limit: int = 0, person: str | None = None) -> None:
    """Ingest every document the classifier called a prescription."""
    docs, _ = collect()
    todo = [d for d in docs if d.suffix == ".pdf" and classified_type(d.rel) == "prescription"]
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
    paperless = Paperless()
    ocr = paperless.ocr_index()
    paperless_ids = paperless.document_id_index()
    misfiled = uncorroborated = 0

    for i, d in enumerate(todo, 1):
        try:
            result = ingest_document(
                con,
                d,
                ocr_text=ocr_for(ocr, d.correspondent, d.rel),
                paperless_id=id_for(paperless_ids, d.correspondent, d.rel),
            )
        except Exception as e:
            logger.error(f"[{i}/{len(todo)}] {d.rel}: {e}")
            continue
        uncorroborated += result.get("uncorroborated", 0)
        if result.get("misfiled"):
            misfiled += 1
            logger.warning(
                f"[{i}/{len(todo)}] MISFILED: {d.rel} names {result['misfiled']}, "
                f"not {d.correspondent}"
            )
        if i % 20 == 0 or i == len(todo):
            logger.info(
                f"  [{i}/{len(todo)}] {d.correspondent} | {d.title[:30]} -> "
                f"{result.get('medications', 0)} meds"
            )

    logger.info(
        f"\ndone. {uncorroborated} drugs not corroborated by an independent reading "
        f"(-> review). {misfiled} documents were filed under the wrong person."
    )


def run_radiology(con, limit: int = 0, person: str | None = None) -> None:
    """Ingest every document the classifier called an imaging report.

    A deceased person's records are history, not something to maintain -- but they
    are still their records, and radiology is read-only. They are ingested like
    anyone else's; only the nagging (review, reconcile, reminders) skips them.
    """
    docs, _ = collect()
    # Either source of truth: the page-1 classifier called it imaging, OR its
    # title names a study (is_radiology). The title heuristic catches a scanned
    # report the classifier never saw; the classifier catches one whose title
    # names no study. ingest_radiology()'s owner guard makes a double-claim safe.
    todo = [
        d
        for d in docs
        if d.suffix == ".pdf" and (classified_type(d.rel) == "radiology" or is_radiology(d))
    ]
    if person:
        todo = [d for d in todo if d.correspondent == person]

    done = {
        r["source_path"]
        for r in con.execute(
            "SELECT source_path FROM documents WHERE doc_type = 'radiology'"
        ).fetchall()
    }
    todo = [d for d in todo if d.rel not in done]
    if limit:
        todo = todo[:limit]

    logger.info(f"{len(todo)} imaging reports to extract")
    paperless = Paperless()
    ocr = paperless.ocr_index()
    paperless_ids = paperless.document_id_index()
    misfiled = unreadable = reports = 0

    for i, d in enumerate(todo, 1):
        try:
            n_reports, bad, moved = ingest_radiology(
                con,
                d.rel,
                d.correspondent,
                ocr_text=ocr_for(ocr, d.correspondent, d.rel),
                paperless_id=id_for(paperless_ids, d.correspondent, d.rel),
            )
        except Exception as e:
            logger.error(f"[{i}/{len(todo)}] {d.rel}: {e}")
            continue

        reports += n_reports
        unreadable += bad
        if moved:
            misfiled += 1
            logger.warning(
                f"[{i}/{len(todo)}] MISFILED: {d.rel} names {moved}, not {d.correspondent}"
            )
        logger.info(
            f"  [{i}/{len(todo)}] {d.correspondent} | {d.title[:34]} -> "
            f"{'filed' if n_reports else 'review'}"
        )

    logger.info(
        f"\ndone. {reports} imaging reports filed. "
        f"{unreadable} had no readable text (-> review). "
        f"{misfiled} documents were filed under the wrong person."
    )


def show_review(con) -> None:
    """What is in health.db but not trusted, and why."""
    total = con.execute("SELECT count(*) FROM observations").fetchone()[0]
    ok = con.execute("SELECT count(*) FROM observations WHERE status = 'ok'").fetchone()[0]
    logger.info(f"{ok}/{total} observations trusted; {total - ok} need review")

    logger.info("\nUnnamed tests (add an alias, then --reclassify; no re-extraction):")
    rows = con.execute("""SELECT printed_name, section, count(*) n, count(DISTINCT subject) people
           FROM observations WHERE analyte IS NULL
           GROUP BY printed_name ORDER BY n DESC LIMIT 30""").fetchall()
    for r in rows:
        logger.info(
            f"  {r['n']:>3}x ({r['people']} people)  [{(r['section'] or '')[:18]:<18}] "
            f"{r['printed_name'][:44]}"
        )

    for r in con.execute("""SELECT count(*) n, review_reason FROM observations
           WHERE status='review' AND analyte IS NOT NULL
           GROUP BY review_reason ORDER BY n DESC LIMIT 10"""):
        logger.info(f"  {r['n']:>3}x  {str(r['review_reason'])[:80]}")

    bad = con.execute("SELECT count(*) FROM review_queue WHERE resolved = 0").fetchone()[0]
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

    before = con.execute("SELECT count(*) FROM observations WHERE analyte IS NULL").fetchone()[0]
    fixed = db.reclassify(con, resolver)
    logger.info(f"named {fixed} of {before} previously-unnamed observations")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=0, help="Ingest at most N documents")
    p.add_argument("--person", help="Only this correspondent")
    p.add_argument("--discharge", action="store_true", help="Extract discharge summaries")
    p.add_argument("--classify", action="store_true", help="Ask each document what it IS (cached)")
    p.add_argument(
        "--prescriptions", action="store_true", help="Extract consultations/prescriptions"
    )
    p.add_argument("--radiology", action="store_true", help="Extract imaging reports")
    p.add_argument("--review", action="store_true", help="Show what is not trusted, and why")
    p.add_argument(
        "--reclassify",
        action="store_true",
        help="Re-resolve unnamed observations after editing aliases (free, no LLM)",
    )
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
    if args.radiology:
        run_radiology(con, limit=args.limit, person=args.person)
        return

    docs, _skipped = collect()
    # is_radiology() first: imaging shares the Medical/Reports tag with labs, so
    # is_lab() would otherwise claim an echo/USG and ingest_document would route
    # it to ingest_radiology anyway -- but a radiology doc has no business padding
    # the "N lab PDFs" count or the labs todo. It is the --radiology pass's job.
    # is_lab() title/tag heuristic OR the page-1 classifier, like run_radiology()
    # and run_discharge(): a lab report whose title is an opaque abbreviation
    # ("TMS", "ABG") sits under no Reports tag and matches no title pattern, so
    # without the classifier backstop it was never ingested. is_radiology() still
    # wins first -- an echo/USG must not pad the lab count or explode into rows.
    labs = [
        d
        for d in docs
        if d.suffix == ".pdf"
        and (is_lab(d) or classified_type(d.rel) == "lab")
        and not is_radiology(d)
    ]
    if args.person:
        labs = [d for d in labs if d.correspondent == args.person]

    done = {r["source_path"] for r in con.execute("SELECT source_path FROM documents").fetchall()}
    todo = [d for d in labs if d.rel not in done]
    if args.limit:
        todo = todo[: args.limit]

    logger.info(f"{len(labs)} lab PDFs, {len(todo)} to extract")
    paperless_ids = Paperless().document_id_index()
    for i, d in enumerate(todo, 1):
        try:
            result = ingest_document(
                con, d, paperless_id=id_for(paperless_ids, d.correspondent, d.rel)
            )
            logger.info(
                f"[{i}/{len(todo)}] {d.correspondent} | {d.title} -> "
                f"{result.get('committed', 0)} observations, {result.get('review', 0)} to review"
            )
        except Exception as e:
            logger.error(f"[{i}/{len(todo)}] {d.rel}: {e}")


if __name__ == "__main__":
    main()

"""One-off: fold radiology observations into one text record per document.

Radiology used to explode into per-parameter `observations`; it now lives as one
verbatim record per study in `radiology_reports` (see the schema comment in
src/db.py). This backfills the existing radiology documents by FREE reconstruction
-- no LLM calls -- and then deletes the observations those documents produced.

For each document with doc_type='radiology':
  report_text  <- Paperless OCR (the entire report), falling back to the verbatim
                  text already extracted into this doc's observation rows;
  impression   <- the existing 'Impression' observation row, if any;
  study_type   <- src.radiology.study_bucket(title);
  subject/date <- the document row (effective from its observations if doc_date is
                  missing).

The patient identity was already checked when these documents were first ingested,
so nothing here re-runs that guard. Labs, discharges and prescriptions are left
untouched -- the scope is exactly doc_type='radiology'.

    ./venv/bin/python -m tools.migrate_radiology_to_text            # prod db, live
    ./venv/bin/python -m tools.migrate_radiology_to_text --db X     # a scratch copy
    ./venv/bin/python -m tools.migrate_radiology_to_text --dry-run  # report only
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import shutil
from collections import Counter

from src import db
from src.radiology import study_bucket

logger = logging.getLogger(__name__)

PROD_DB = "/root/docker-stacks/nalam/config/health.db"


def _ocr_index():
    """Paperless OCR keyed by (correspondent, rel_path), or {} if unreachable.

    The whole report text is the goal; Paperless holds it. If Paperless cannot be
    reached from where this runs, we fall back per document to the verbatim text
    already sitting in the observation rows -- less complete, but never a guess.
    """
    try:
        from src.paperless import Paperless

        return Paperless().ocr_index()
    except Exception as e:  # noqa: BLE001 -- any failure just means fall back
        logger.warning(f"Paperless OCR unavailable ({e}); reconstructing from observations")
        return {}


def _reconstruct_from_observations(con, document_id: int) -> str:
    """Rebuild readable report text from a document's own extracted rows.

    Findings are prose (value_text); measurements are 'Name: value unit'. Order by
    rowid so the text reads roughly as the report did.
    """
    parts = []
    for r in con.execute(
        "SELECT printed_name, value_text, raw_value, unit FROM observations "
        "WHERE document_id = ? ORDER BY id",
        (document_id,),
    ):
        if r["value_text"]:
            parts.append(r["value_text"].strip())
        elif r["raw_value"]:
            name = (r["printed_name"] or "").strip()
            unit = (r["unit"] or "").strip()
            parts.append(f"{name}: {r['raw_value']} {unit}".strip())
    return "\n".join(p for p in parts if p)


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--db", default=PROD_DB, help=f"health.db to migrate (default {PROD_DB})")
    p.add_argument("--dry-run", action="store_true", help="Report only; write nothing")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not args.dry_run:
        ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        backup = f"{args.db}.pre-radiology-{ts}"
        shutil.copy2(args.db, backup)
        logger.info(f"backed up {args.db} -> {backup}")

    con = db.connect(args.db)  # executescript(SCHEMA) creates radiology_reports if new

    docs = con.execute(
        "SELECT id, subject, source_path, doc_date FROM documents WHERE doc_type = 'radiology'"
    ).fetchall()
    logger.info(f"{len(docs)} radiology documents")

    rad_obs_before = con.execute(
        "SELECT count(*) FROM observations WHERE document_id IN "
        "(SELECT id FROM documents WHERE doc_type = 'radiology')"
    ).fetchone()[0]
    lab_obs_before = con.execute(
        "SELECT count(*) FROM observations WHERE document_id NOT IN "
        "(SELECT id FROM documents WHERE doc_type = 'radiology')"
    ).fetchone()[0]

    ocr = _ocr_index()
    from src.paperless import ocr_for

    buckets: Counter = Counter()
    no_text = 0
    for d in docs:
        rel, subject = d["source_path"], d["subject"]

        report_text = ocr_for(ocr, subject, rel) if ocr else None
        if not (report_text or "").strip():
            report_text = _reconstruct_from_observations(con, d["id"])
        report_text = (report_text or "").strip() or None
        if not report_text:
            no_text += 1

        imp = con.execute(
            "SELECT value_text FROM observations "
            "WHERE document_id = ? AND printed_name = 'Impression' AND value_text IS NOT NULL "
            "ORDER BY id LIMIT 1",
            (d["id"],),
        ).fetchone()
        impression = imp["value_text"] if imp else None

        effective = d["doc_date"]
        if not effective:
            eff = con.execute(
                "SELECT effective FROM observations WHERE document_id = ? "
                "AND effective IS NOT NULL ORDER BY effective DESC LIMIT 1",
                (d["id"],),
            ).fetchone()
            effective = eff["effective"] if eff else None

        study_type = study_bucket(os.path.basename(rel))
        buckets[study_type] += 1

        if not args.dry_run:
            db.upsert_radiology_report(
                con,
                document_id=d["id"],
                subject=subject,
                study_type=study_type,
                effective=effective,
                impression=impression,
                report_text=report_text,
            )

    if not args.dry_run:
        con.execute(
            "DELETE FROM observations WHERE document_id IN "
            "(SELECT id FROM documents WHERE doc_type = 'radiology')"
        )
        con.commit()

    lab_obs_after = con.execute(
        "SELECT count(*) FROM observations WHERE document_id NOT IN "
        "(SELECT id FROM documents WHERE doc_type = 'radiology')"
    ).fetchone()[0]
    rad_reports = con.execute("SELECT count(*) FROM radiology_reports").fetchone()[0]

    logger.info("\nstudy_type buckets:")
    for name, n in buckets.most_common():
        logger.info(f"  {n:3}  {name}")

    logger.info(
        f"\n{'DRY RUN -- nothing written' if args.dry_run else 'done'}. "
        f"radiology_reports: {rad_reports} | "
        f"radiology observations to drop: {rad_obs_before} | "
        f"{no_text} docs had no readable text | "
        f"lab observations {lab_obs_before} -> {lab_obs_after} "
        f"({'UNCHANGED' if lab_obs_before == lab_obs_after else 'CHANGED -- STOP'})"
    )


if __name__ == "__main__":
    main()

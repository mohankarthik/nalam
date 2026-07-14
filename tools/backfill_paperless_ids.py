"""One-time backfill: fill in documents.paperless_id for rows extracted before
run_extract.py started resolving it at ingest time.

Extraction reads PDFs straight from the Drive mount, never from Paperless, so
every `documents` row it ever wrote has `paperless_id = NULL` -- there was
nowhere in the pipeline that looked one up. That silently broke every citation
link src/qa.py's Telegram Q&A tries to attach to an answer (found while
debugging a "no trustworthy record" reply that turned out to have a real
document behind it, just no link to show for it).

Matches the exact same (correspondent, folded filename) join used by
tools/export_review.py and run_extract.py's ingest_* calls, via
Paperless.document_id_index(). Only fills NULLs -- never overwrites an id a
later run already resolved.

Run:  ./venv/bin/python -m tools.backfill_paperless_ids           # everything
      ./venv/bin/python -m tools.backfill_paperless_ids --limit 3  # try a few first
"""

from __future__ import annotations

import argparse
import logging

from src import db
from src.paperless import Paperless, id_for

logger = logging.getLogger(__name__)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=0, help="Only fill in the first N (0 = everything)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    con = db.connect()
    index = Paperless().document_id_index()

    rows = con.execute(
        "SELECT id, subject, source_path FROM documents WHERE paperless_id IS NULL"
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]

    # `paperless_id` is UNIQUE, and it is possible for TWO health.db rows to
    # resolve to the same one -- the same physical document filed under two
    # different Drive paths (the file-level version of the duplicate-folder
    # trap CLAUDE.md already warns about). Picking either one and keeping it
    # is a guess about which row "really" owns that document; the other rule
    # this project already lives by (CLAUDE.md trap #4: two results claiming
    # one identity are BOTH untrusted) applies here too. So a collision
    # leaves BOTH rows NULL and gets reported for a human to resolve.
    by_pid: dict[int, list] = {}
    for r in rows:
        pid = id_for(index, r["subject"], r["source_path"])
        if pid is not None:
            by_pid.setdefault(pid, []).append(dict(r))

    collisions = {pid: docs for pid, docs in by_pid.items() if len(docs) > 1}

    filled = 0
    for pid, docs in by_pid.items():
        if pid in collisions:
            continue
        r = docs[0]
        con.execute("UPDATE documents SET paperless_id = ? WHERE id = ?", (pid, r["id"]))
        filled += 1
        logger.info(f"  {r['subject']} | {r['source_path']} -> paperless_id={pid}")

    con.commit()
    logger.info(f"{filled}/{len(rows)} documents matched to a Paperless id")
    unmatched = len(rows) - sum(len(docs) for docs in by_pid.values())
    if unmatched:
        logger.info(
            f"{unmatched} had no match -- not yet uploaded to Paperless, or an "
            "ambiguous (person, filename) pair document_id_index() dropped rather "
            "than risk linking to the wrong page."
        )
    if collisions:
        logger.info(
            f"\n{len(collisions)} collision(s) -- two+ health.db rows resolved to the "
            "SAME Paperless document. Left ALL of them NULL; a human has to say which "
            "one (if any) actually owns that document id:"
        )
        for pid, docs in collisions.items():
            logger.info(f"  paperless_id={pid}:")
            for d in docs:
                logger.info(f"    id={d['id']}  {d['subject']}  {d['source_path']}")


if __name__ == "__main__":
    main()

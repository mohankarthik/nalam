"""Hand a document from one extractor to another, and let it be re-read.

    python -m tools.reown_document --to radiology --dry-run
    python -m tools.reown_document --to radiology "Dad/Reports/2026-04-03 - CT angio.pdf"
    python -m tools.reown_document --to radiology --all-classified

Two routers can claim the same file and nothing arbitrates between them. is_lab()
calls every document tagged Medical/Reports a lab; the page-1 classifier reads an
echo in that same folder and calls it radiology. Both are defensible, and
upsert_document() never changes doc_type -- so whichever extractor ran first owns
the document for good, and a CT angiogram sits in the database as a "lab" with a
handful of stray numbers scraped out of it by a lab prompt.

ingest_radiology() therefore REFUSES a document another extractor owns rather than
overwriting it. (It did overwrite once, and deleted 448 lab observations.) This is
how a human hands it over on purpose.

It is not automatic, and it must not be, because the classifier is not reliably
right: it called a health-checkup panel -- 105 real lab values -- "radiology".
Re-owning that one would have destroyed them. A machine cannot tell the difference
between "the classifier found an echo the lab router mislabelled" and "the
classifier misread a lab panel". A person can.

What it does: deletes the document row, which cascades its observations away. The
next `run_extract.py --<type>` re-reads it from the LLM cache -- free, offline, no
API call -- and files it under the right type. Nothing is lost that cannot be
rebuilt, because health.db is a derived view of the cache.

Refuses to touch a document carrying medications or encounters: those do not
rebuild from a radiology re-read, and losing them is not recoverable here.
"""

from __future__ import annotations

import argparse
import sys

from src import db


def contested(con) -> list[tuple[int, str, str, int]]:
    """Documents the classifier types differently from how they were ingested."""
    from run_extract import classified_type

    rows = []
    for r in con.execute("SELECT id, source_path, doc_type FROM documents ORDER BY source_path"):
        want = classified_type(r["source_path"])
        if want and want != r["doc_type"]:
            n = con.execute(
                "SELECT count(*) FROM observations WHERE document_id = ?", (r["id"],)
            ).fetchone()[0]
            rows.append((r["id"], r["source_path"], r["doc_type"], n, want))
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("paths", nargs="*", help="source_path of each document to hand over")
    p.add_argument("--to", required=True, help="the extractor that should own them")
    p.add_argument(
        "--all-classified",
        action="store_true",
        help="every document the classifier calls --to but that was ingested as something else",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    con = db.connect()

    if args.all_classified:
        targets = [(i, p_, t, n) for i, p_, t, n, want in contested(con) if want == args.to]
    else:
        targets = []
        for path in args.paths:
            r = con.execute(
                "SELECT id, source_path, doc_type FROM documents WHERE source_path = ?", (path,)
            ).fetchone()
            if not r:
                print(f"  not in the database: {path}", file=sys.stderr)
                continue
            n = con.execute(
                "SELECT count(*) FROM observations WHERE document_id = ?", (r["id"],)
            ).fetchone()[0]
            targets.append((r["id"], r["source_path"], r["doc_type"], n))

    if not targets:
        print("  nothing to do")
        return

    # Medications and encounters do not come back from a radiology re-read. A
    # document carrying them is not a candidate, whatever the classifier thinks.
    safe, refused = [], []
    for doc_id, path, was, n_obs in targets:
        meds = con.execute(
            "SELECT count(*) FROM medication_events WHERE document_id = ?", (doc_id,)
        ).fetchone()[0]
        encs = con.execute(
            "SELECT count(*) FROM encounters WHERE document_id = ?", (doc_id,)
        ).fetchone()[0]
        (refused if (meds or encs) else safe).append((doc_id, path, was, n_obs, meds, encs))

    for doc_id, path, was, n_obs, meds, encs in refused:
        print(
            f"  REFUSED  {path}\n"
            f"           carries {meds} medications and {encs} encounters, which a "
            f"{args.to} re-read does not rebuild.",
            file=sys.stderr,
        )

    for doc_id, path, was, n_obs, _m, _e in safe:
        print(f"  {was:>10} -> {args.to:<10} {n_obs:>4} observations dropped   {path}")

    if args.dry_run:
        print(f"\n  --dry-run: {len(safe)} document(s) would be handed over. Nothing written.")
        return

    for doc_id, *_ in safe:
        con.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    con.commit()

    print(f"\n  handed over {len(safe)} document(s).")
    print(f"  now run:  ./venv/bin/python run_extract.py --{args.to}")
    print("  it re-reads them from the LLM cache -- free, offline, no API call.")


if __name__ == "__main__":
    main()

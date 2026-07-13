"""Read the reviewed worksheet back in. The human's word is final.

    (blank)         the name as read is right   -> trust it
    Metformin 500   the correct name            -> replace it, and trust it
    -               not a drug at all           -> delete it
    ?               unreadable                  -> leave in review

A correction is recorded as `entered_by='human'`, so the log always says who
decided what. An extractor's reading and a person's reading are not the same kind
of fact and the database should never pretend otherwise.

Run:  python -m tools.import_review
"""

from __future__ import annotations

import os
import re

from src import db
from src.drugs import load_drugs, lookup

WORKSHEET = os.path.expanduser("~/nalam-drug-review.md")

# | 123 | `T. Minoz` | 100mg | 1-0-1 | guess | CORRECTION |
_ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*`([^`]*)`\s*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|")


def main() -> None:
    if not os.path.exists(WORKSHEET):
        raise SystemExit(f"{WORKSHEET} not found. Run: python -m tools.export_review")

    con = db.connect()
    table = load_drugs()

    accepted = corrected = deleted = left = 0

    for line in open(WORKSHEET, encoding="utf-8"):
        m = _ROW.match(line)
        if not m:
            continue
        med_id = int(m.group(1))
        as_read = m.group(2).strip()
        correction = m.group(6).strip()

        if correction in {"?", "??"}:
            left += 1
            continue

        if correction == "-":
            con.execute("DELETE FROM medication_events WHERE id = ?", (med_id,))
            deleted += 1
            continue

        if not correction:
            # The name as read is right. A human says so, which is a stronger fact
            # than an OCR match would have been.
            con.execute(
                """UPDATE medication_events
                   SET status='ok', review_reason=NULL, entered_by='human'
                   WHERE id = ?""",
                (med_id,),
            )
            accepted += 1
            continue

        # A corrected name. Re-map it to a molecule if we know one.
        d = lookup(correction, table)
        generic = " + ".join(d.generic) if d and d.confirmed and d.generic else None
        con.execute(
            """UPDATE medication_events
               SET drug=?, generic=?, status='ok', review_reason=NULL,
                   entered_by='human'
               WHERE id = ?""",
            (correction, generic, med_id),
        )
        corrected += 1

    con.commit()

    print(f"  {accepted:>4} accepted as read")
    print(f"  {corrected:>4} corrected")
    print(f"  {deleted:>4} deleted (not a drug)")
    print(f"  {left:>4} left in review (unreadable)")

    still = con.execute(
        "SELECT count(*) FROM medication_events WHERE status='review'"
    ).fetchone()[0]
    print(f"\n  {still} drugs still awaiting review")

    unmapped = con.execute(
        """SELECT count(DISTINCT drug) FROM medication_events
           WHERE generic IS NULL AND status='ok'"""
    ).fetchone()[0]
    if unmapped:
        print(
            f"  {unmapped} confirmed drug names have no molecule in data/drugs.json "
            f"-- add them there, then re-run to map them."
        )


if __name__ == "__main__":
    main()

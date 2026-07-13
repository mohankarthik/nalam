"""Fill in molecules for drugs whose brand was only added to the table later.

    python -m tools.reresolve_drugs --dry-run
    python -m tools.reresolve_drugs

`generic` is written once, when a drug is accepted -- so a brand confirmed in
data/drugs.json AFTER that leaves every existing row on generic=NULL. The name is
right, the molecule is simply missing, and "is he on montelukast?" quietly misses
it. This is the drug-table twin of `run_extract.py --reclassify`: free, offline,
and re-runnable.

It only ever fills a NULL. A generic already on the row was put there by a human
or by an earlier confirmed lookup, and a table read must never silently overwrite
either -- if the two disagree, that is a fact to surface, not to paper over. Rows
still in review are left alone: an unconfirmed NAME cannot yield a trusted
molecule, whatever the table says.
"""

from __future__ import annotations

import argparse

from src import db
from src.drugs import load_drugs, lookup


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="show what would change")
    args = p.parse_args()

    con = db.connect()
    table = load_drugs()

    rows = con.execute("""SELECT id, subject, drug, generic, status FROM medication_events
           WHERE status = 'ok' ORDER BY subject, drug""").fetchall()

    filled, conflicts = [], []
    for r in rows:
        d = lookup(r["drug"], table)
        if not d or not d.confirmed or not d.generic:
            continue
        found = " + ".join(d.generic)

        if not r["generic"]:
            filled.append((r["id"], r["subject"], r["drug"], found))
        elif r["generic"] != found:
            conflicts.append((r["id"], r["subject"], r["drug"], r["generic"], found))

    for _id, subject, drug, found in filled:
        print(f"  {_id:>4}  {subject[:16]:<16} {drug[:24]:<24} -> {found}")

    if conflicts:
        print(
            f"\n  {len(conflicts)} row(s) already carry a DIFFERENT molecule than the table."
            "\n  Not touched. One of the two is wrong and a human has to say which:"
        )
        for _id, subject, drug, was, now in conflicts:
            print(f"  {_id:>4}  {subject[:16]:<16} {drug[:24]:<24} has {was!r}, table says {now!r}")

    if args.dry_run:
        print(f"\n  --dry-run: {len(filled)} row(s) would be filled. Nothing written.")
        return

    for _id, _, _, found in filled:
        con.execute("UPDATE medication_events SET generic = ? WHERE id = ?", (found, _id))
    con.commit()
    print(f"\n  filled {len(filled)} row(s)")


if __name__ == "__main__":
    main()

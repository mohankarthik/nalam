"""Apply a human's decisions on reviewed drugs, one at a time.

    python -m tools.apply_review 260=ATORVA 261=LOSAR 262= 263=PAN 264=Alprax

    <id>=<name>       the correct name -> replace, trust, map to a molecule
    <id>=             the name as read is right -> trust it as-is
    <id>=-            not a drug -> delete it
    <id>=A||B         ONE entry that is really TWO drugs -> split it

A doctor writes "continue losar and galvus met" on one line, and the extractor
records a single drug called `losar and galvus met`. That is two medications.
Left merged, neither maps to a molecule and "is he on metformin?" misses it.

Everything applied here is recorded `entered_by='human'`. What a model read and
what a person confirmed are different kinds of fact, and the log must never blur
them.

The actual decision logic lives in src/meds.py:apply_drug_decision -- shared
with the web UI, so a correction typed here and one clicked there go through
the exact same code.
"""

from __future__ import annotations

import sys

from src import db
from src.drugs import load_drugs
from src.meds import apply_drug_decision


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)

    con = db.connect()
    table = load_drugs()

    for arg in sys.argv[1:]:
        if "=" not in arg:
            raise SystemExit(f"expected <id>=<name>, got {arg!r}")
        raw_id, correction = arg.split("=", 1)
        med_id = int(raw_id)

        try:
            summary = apply_drug_decision(con, table, med_id, correction)
        except KeyError:
            print(f"  {med_id}: no such entry")
            continue

        for line in summary.split("\n"):
            print(f"  {line}")

    con.commit()
    left = con.execute("SELECT count(*) FROM medication_events WHERE status='review'").fetchone()[0]
    print(f"\n  {left} drugs still in review")


if __name__ == "__main__":
    main()

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
"""

from __future__ import annotations

import re
import sys

from src import db
from src.drugs import load_drugs, lookup

# "Cilostazol 50mg" -> ("Cilostazol", "50mg"). The name belongs in `drug`, the
# strength in `strength` -- and the strength as READ is often wrong ("50g" for
# 50mg), so a correction that supplies one must replace it.
_STRENGTH = re.compile(
    r"\s+(\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|iu|units?|%)(?:\s*/\s*\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml))?)\s*$",
    re.IGNORECASE,
)


def split_strength(text: str) -> tuple[str, str | None]:
    m = _STRENGTH.search(text)
    if not m:
        return text.strip(), None
    return text[: m.start()].strip(), m.group(1).strip()


def _split(con, table, med_id: int, row, parts: list[str]) -> None:
    """One extracted entry is really several drugs. Make it several rows."""
    source = con.execute(
        "SELECT * FROM medication_events WHERE id = ?", (med_id,)
    ).fetchone()

    for i, part in enumerate(parts):
        name, strength = split_strength(part)
        d = lookup(name, table)
        generic = " + ".join(d.generic) if d and d.confirmed and d.generic else None
        shown = f"{generic} ({name})" if generic else name

        if i == 0:
            con.execute(
                """UPDATE medication_events
                   SET drug=?, generic=?, strength=COALESCE(?, strength),
                       status='ok', review_reason=NULL, entered_by='human'
                   WHERE id=?""",
                (name, generic, strength, med_id),
            )
            print(f"  {med_id}: {shown:<44} split from {row['drug']!r}")
        else:
            con.execute(
                """INSERT INTO medication_events
                     (document_id, subject, drug, generic, strength, form, dose,
                      frequency, duration, event, effective, raw_text, entered_by,
                      status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'human','ok')""",
                (
                    source["document_id"], source["subject"], name, generic,
                    strength or source["strength"], source["form"], source["dose"],
                    source["frequency"], source["duration"], source["event"],
                    source["effective"], source["raw_text"],
                ),
            )
            print(f"      + {shown:<42} (new row, split from the same line)")


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
        correction = correction.strip()

        row = con.execute(
            "SELECT drug, subject FROM medication_events WHERE id = ?", (med_id,)
        ).fetchone()
        if not row:
            print(f"  {med_id}: no such entry")
            continue

        if correction == "-":
            con.execute("DELETE FROM medication_events WHERE id = ?", (med_id,))
            print(f"  {med_id}: deleted  ({row['drug']!r} — not a drug)")
            continue

        if "||" in correction:
            parts = [x.strip() for x in correction.split("||") if x.strip()]
            _split(con, table, med_id, row, parts)
            continue

        name, strength = split_strength(correction or row["drug"])
        d = lookup(name, table)
        generic = " + ".join(d.generic) if d and d.confirmed and d.generic else None

        if strength:
            con.execute(
                """UPDATE medication_events
                   SET drug=?, generic=?, strength=?, status='ok',
                       review_reason=NULL, entered_by='human'
                   WHERE id=?""",
                (name, generic, strength, med_id),
            )
        else:
            con.execute(
                """UPDATE medication_events
                   SET drug=?, generic=?, status='ok', review_reason=NULL,
                       entered_by='human'
                   WHERE id=?""",
                (name, generic, med_id),
            )
        shown = f"{generic} ({name})" if generic else name
        how = "confirmed as read" if not correction else f"corrected from {row['drug']!r}"
        print(f"  {med_id}: {shown:<44} {how}")

    con.commit()
    left = con.execute(
        "SELECT count(*) FROM medication_events WHERE status='review'"
    ).fetchone()[0]
    print(f"\n  {left} drugs still in review")


if __name__ == "__main__":
    main()

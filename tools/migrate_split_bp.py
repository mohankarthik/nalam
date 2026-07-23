"""One-shot: split 'BP' observations into 'BP High' + 'BP Low'.

'BP' was a promoted analyte holding a combined "120/80" reading. Blood pressure is
two values, and the codebook already has BP High (systolic) and BP Low (diastolic).
This rewrites each 'BP' row as the two component rows and deletes the original, so
no reading is lost (the alternative -- purging BP with the other drops -- would have
thrown the numbers away).

    ./venv/bin/python -m tools.migrate_split_bp --db PATH --dry-run
    ./venv/bin/python -m tools.migrate_split_bp --db PATH

Back up the db first. Run BEFORE tools.migrate_drop_analytes (which would otherwise
purge the BP rows via the ignore-list).
"""

from __future__ import annotations

import re
import sqlite3
import sys

DB = "data/health.db"
_BP = re.compile(r"^\s*(\d{2,3})\s*/\s*(\d{2,3})\s*$")


def _db_path(argv: list[str]) -> str:
    return argv[argv.index("--db") + 1] if "--db" in argv else DB


def main() -> None:
    dry = "--dry-run" in sys.argv
    con = sqlite3.connect(_db_path(sys.argv))
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM observations WHERE analyte='BP'").fetchall()
    print(f"BP rows: {len(rows)}")

    made, skipped = 0, 0
    for r in rows:
        m = _BP.match(r["value_text"] or r["raw_value"] or "")
        if not m:
            print(f"  id={r['id']}: cannot parse {r['raw_value']!r} -- skipped")
            skipped += 1
            continue
        sys_v, dia_v = float(m.group(1)), float(m.group(2))
        print(
            f"  id={r['id']} {r['subject']} {r['effective']}: "
            f"{r['raw_value']} -> BP High {sys_v:g}, BP Low {dia_v:g}"
        )
        if dry:
            continue
        # Delete the combined row FIRST: the two new rows reuse its (subject,
        # section, effective) and would otherwise collide with it on the UNIQUE
        # key. The key also has no `analyte`, so the two components must differ on
        # printed_name (and they carry their own component value as raw_value) to
        # be distinct from each other.
        con.execute("DELETE FROM observations WHERE id=?", (r["id"],))
        for analyte, value, tag in (("BP High", sys_v, "systolic"), ("BP Low", dia_v, "diastolic")):
            con.execute(
                """INSERT OR IGNORE INTO observations
                     (document_id, subject, segment, analyte, printed_name, section,
                      effective, value_num, value_text, raw_value, unit, ref_low,
                      ref_high, source_quality, status, review_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    r["document_id"],
                    r["subject"],
                    "Phys",
                    analyte,
                    f"{r['printed_name']} ({tag})",
                    r["section"],
                    r["effective"],
                    value,
                    None,
                    f"{value:g}",
                    "mmHg",
                    None,
                    None,
                    r["source_quality"],
                    r["status"],
                    r["review_reason"],
                ),
            )
        made += 1

    if not dry:
        con.commit()
    verb = "would split" if dry else "split"
    print(f"\n{verb} {made} BP row(s) into 2 each; {skipped} unparseable left as-is.")


if __name__ == "__main__":
    main()

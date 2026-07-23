"""Purge observations for analytes dropped in the codebook consolidation.

The dropped analytes (echo -> radiology, ratios, CPAP device metrics, qualitative
urine, imaging buckets) are listed in data/ignored_analytes.json -- the single
source of truth. Their existing rows in `observations` are now orphaned: no
codebook entry resolves or range-flags them, and the ingest ignore-check keeps
new extractions from re-creating them. This deletes the orphans so the database
matches the codebook.

    ./venv/bin/python -m tools.migrate_drop_analytes --dry-run          # repo db, report
    ./venv/bin/python -m tools.migrate_drop_analytes --db PATH          # target a db
    ./venv/bin/python -m tools.migrate_drop_analytes --db PATH          # delete for real

Back up the target db before the real run -- it is gitignored and irreplaceable.
"""

from __future__ import annotations

import sqlite3
import sys

from src import config

DB = "data/health.db"
IGNORED = "data/ignored_analytes.json"


def dropped_names() -> list[str]:
    return sorted(config.load(IGNORED).get("ignored", {}))


def _db_path(argv: list[str]) -> str:
    if "--db" in argv:
        return argv[argv.index("--db") + 1]
    return DB


def main() -> None:
    dry = "--dry-run" in sys.argv
    names = dropped_names()
    con = sqlite3.connect(_db_path(sys.argv))
    placeholders = ",".join("?" * len(names))

    rows = con.execute(
        f"SELECT analyte, COUNT(*) FROM observations WHERE analyte IN ({placeholders}) "
        f"GROUP BY analyte ORDER BY COUNT(*) DESC",
        names,
    ).fetchall()
    total = sum(n for _, n in rows)

    print(f"{'analyte':32} rows")
    print("-" * 40)
    for analyte, n in rows:
        print(f"{analyte:32} {n}")
    print("-" * 40)
    print(f"{'TOTAL':32} {total}")
    print(f"\n{len(names)} dropped analytes; {len(rows)} of them have observations.")

    if dry:
        print("\n--dry-run: nothing deleted.")
        return

    con.execute(f"DELETE FROM observations WHERE analyte IN ({placeholders})", names)
    con.commit()
    print(f"\nDeleted {total} rows.")


if __name__ == "__main__":
    main()

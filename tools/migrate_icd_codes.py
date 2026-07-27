"""Add `encounters.icd_codes` and backfill it from the printed diagnoses.

Phase C (ICD-10). A discharge summary sometimes prints an ICD-10 code beside its
diagnosis ("... cataract, bilateral - H25.13"). We now capture those codes into a
dedicated column instead of leaving them buried in the verbatim `diagnoses` JSON.

This is a purely ADDITIVE column, so no table rebuild and no foreign-key surgery
(unlike tools/migrate_obs_section.py): `ALTER TABLE ADD COLUMN` is safe. The
backfill re-parses every encounter's existing diagnoses through the SAME code the
ingest path uses (src.ingest.icd_from_diagnoses), validating against the baked
ICD-10-CM category table so nutrition tokens like "B12" are never captured, and
surfacing any printed code that contradicts the bucket its diagnosis maps to.

Idempotent: safe to re-run. Backs the database up first.

Run against PROD (default is data/health.db, the repo's throwaway copy):
    ./venv/bin/python -m tools.migrate_icd_codes ~/docker-stacks/nalam/config/health.db
"""

from __future__ import annotations

import json
import os
import sys

from src import db
from src.ingest import icd_from_diagnoses


def has_column(con, table: str, col: str) -> bool:
    return any(r[1] == col for r in con.execute(f"PRAGMA table_info('{table}')"))


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else db.DB_PATH
    con = db.connect(path)

    backup = f"{path}.pre-icd.bak"
    if not os.path.exists(backup):
        con.execute("VACUUM INTO ?", (backup,))
        print(f"  backup:      {backup}")
    else:
        print(f"  backup:      {backup} (exists, kept)")

    if not has_column(con, "encounters", "icd_codes"):
        con.execute("ALTER TABLE encounters ADD COLUMN icd_codes TEXT")
        print("  column:      added encounters.icd_codes")
    else:
        print("  column:      encounters.icd_codes already present")

    rows = con.execute("SELECT id, diagnoses FROM encounters").fetchall()
    filled = 0
    coded_encounters = 0
    for r in rows:
        diagnoses = json.loads(r["diagnoses"] or "[]")
        codes = icd_from_diagnoses(diagnoses)  # logs any bucket/code conflicts
        con.execute(
            "UPDATE encounters SET icd_codes = ? WHERE id = ?",
            (json.dumps(codes), r["id"]),
        )
        filled += 1
        if codes:
            coded_encounters += 1
    con.commit()

    print(f"  encounters:  {filled} scanned, {coded_encounters} carry a printed ICD-10 code")
    print(f"  integrity:   {con.execute('PRAGMA integrity_check').fetchone()[0]}")
    # Show what we captured, for the human eyeballing the migration.
    for r in con.execute(
        "SELECT subject, discharged, icd_codes FROM encounters "
        "WHERE icd_codes IS NOT NULL AND icd_codes != '[]' ORDER BY discharged"
    ):
        print(f"    {r['discharged']}  {r['subject']:<12}  {r['icd_codes']}")


if __name__ == "__main__":
    main()

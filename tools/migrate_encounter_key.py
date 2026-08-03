"""Re-key `encounters` so two consultations on one day at one hospital both survive.

The old table had `UNIQUE (subject, admitted, hospital)` and every ingest path
inserts with `INSERT OR IGNORE`. That silently DROPPED the second of two same-day
same-hospital consultations -- e.g. a patient's urology and cardiology clinic
visits on one day at one hospital: the consult extracted first claimed the slot
and the second vanished, its meds ingested but no encounter.

This migration:
  - adds `doctor` and `speciality` columns (the prescription extractor already
    returns both; ingest now stores them), and
  - changes the uniqueness key to `(subject, admitted, hospital, document_id)`, so
    distinct documents coexist while re-ingesting the SAME document still dedups.

Changing a UNIQUE constraint needs a table REBUILD (SQLite can't ALTER it), unlike
the purely additive tools/migrate_icd_codes.py. No other table references
`encounters` by foreign key and it carries no indexes/triggers, so the rebuild is a
create-copy-drop-rename. Idempotent: safe to re-run. Backs the database up first.

Run against PROD (default is data/health.db, the repo's throwaway copy):
    ./venv/bin/python -m tools.migrate_encounter_key ~/docker-stacks/nalam/config/health.db
"""

from __future__ import annotations

import os
import sys

from src import db

NEW_UNIQUE = "subject, admitted, hospital, document_id"


def has_column(con, table: str, col: str) -> bool:
    return any(r[1] == col for r in con.execute(f"PRAGMA table_info('{table}')"))


def already_migrated(con) -> bool:
    sql = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='encounters'"
    ).fetchone()[0]
    return has_column(con, "encounters", "doctor") and NEW_UNIQUE in sql


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else db.DB_PATH
    con = db.connect(path)

    if already_migrated(con):
        print("  already migrated (doctor column + document_id in key present); nothing to do")
        return

    backup = f"{path}.pre-enckey.bak"
    if not os.path.exists(backup):
        con.execute("VACUUM INTO ?", (backup,))
        print(f"  backup:      {backup}")
    else:
        print(f"  backup:      {backup} (exists, kept)")

    before = con.execute("SELECT count(*) FROM encounters").fetchone()[0]

    # Rebuild. Old columns are copied verbatim; doctor/speciality land NULL for
    # every historical row (backfill them by re-ingesting the consultation docs).
    con.executescript("""
        PRAGMA foreign_keys = OFF;
        BEGIN;
        CREATE TABLE encounters_new (
            id             INTEGER PRIMARY KEY,
            document_id    INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            subject        TEXT COLLATE NOCASE NOT NULL,
            hospital       TEXT COLLATE NOCASE,
            doctor         TEXT COLLATE NOCASE,
            speciality     TEXT COLLATE NOCASE,
            admitted       TEXT,
            discharged     TEXT,
            reason         TEXT,
            diagnoses      TEXT,
            icd_codes      TEXT,
            procedures     TEXT,
            follow_up      TEXT,
            follow_up_date TEXT,
            UNIQUE (subject, admitted, hospital, document_id)
        );
        INSERT INTO encounters_new
            (id, document_id, subject, hospital, admitted, discharged, reason,
             diagnoses, icd_codes, procedures, follow_up, follow_up_date)
        SELECT
             id, document_id, subject, hospital, admitted, discharged, reason,
             diagnoses, icd_codes, procedures, follow_up, follow_up_date
        FROM encounters;
        DROP TABLE encounters;
        ALTER TABLE encounters_new RENAME TO encounters;
        COMMIT;
        PRAGMA foreign_keys = ON;
        """)

    after = con.execute("SELECT count(*) FROM encounters").fetchone()[0]
    fk_ok = not con.execute("PRAGMA foreign_key_check").fetchall()
    print("  columns:     added encounters.doctor, encounters.speciality")
    print(f"  key:         UNIQUE ({NEW_UNIQUE})")
    print(f"  rows:        {before} -> {after} (must be equal)")
    print(f"  integrity:   {con.execute('PRAGMA integrity_check').fetchone()[0]}")
    print(f"  fk_check:    {'ok' if fk_ok else 'FAILED'}")


if __name__ == "__main__":
    main()

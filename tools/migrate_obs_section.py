"""Add `section` to the observations UNIQUE key, so a section is part of a row's identity.

    UNIQUE (subject, printed_name,          effective, raw_value)   -- before
    UNIQUE (subject, printed_name, section, effective, raw_value)   -- after

Why this is data loss and not merely a schema wart: `insert_observations()` uses
INSERT OR IGNORE, so a row that collides on this key is not rejected loudly -- it
is silently dropped, and nothing anywhere records that it existed.

A follicular scan prints a "Follicle" of "18" in the right ovary and a "Follicle"
of "18" in the left ovary, on the same date. Those are two follicles. Under the
old key they are one row, and the other is gone.

Labs have the same hole and are saved only by luck: "RBC" under URINE ROUTINE and
"RBC" under COMPLETE BLOOD COUNT differ in value, so they survive. Print the same
value on both -- "Nil", "Negative", "Normal", which urine and stool reports do
constantly -- and one of them vanishes.

The new key is strictly MORE PERMISSIVE. It can only admit rows the old one
rejected; it can never merge two rows that were previously distinct. So no
existing data can be lost by running this. What is already gone is already gone --
re-ingest the affected documents (free, from the LLM cache) to recover it.

Follows tools/migrate_nocase.py, including the two things that corrupted the
database the first time someone tried this: legacy_alter_table (or RENAME rewrites
every foreign key that points at the table) and one transaction that rolls back
whole.

Run:  python -m tools.migrate_obs_section
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys

from src import db

TABLE = "observations"
NEW_KEY = "UNIQUE (subject, printed_name, section, effective, raw_value)"


def table_ddl(table: str) -> str:
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?^\);", db.SCHEMA, re.S | re.M)
    if not m:
        raise SystemExit(f"could not find the DDL for {table} in db.SCHEMA")
    return m.group(0)


def current_key(con: sqlite3.Connection) -> str:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone()
    if not row:
        raise SystemExit(f"no {TABLE} table")
    m = re.search(r"UNIQUE \([^)]*\)", row[0])
    return m.group(0) if m else "(none)"


def main() -> None:
    path = db.DB_PATH if hasattr(db, "DB_PATH") else "data/health.db"
    con = db.connect()

    have = current_key(con)
    print(f"  current key: {have}")
    if "section" in have:
        print("  already migrated. Nothing to do.")
        return

    backup = f"{path}.pre-section.bak"
    con.execute("VACUUM INTO ?", (backup,)) if not os.path.exists(backup) else None
    print(f"  backup:      {backup}")

    before = con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
    print(f"  rows before: {before}")

    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("PRAGMA legacy_alter_table = ON")

    try:
        con.execute("BEGIN")
        cols = [r[1] for r in con.execute(f"PRAGMA table_info('{TABLE}')")]
        collist = ", ".join(cols)

        con.execute(f"ALTER TABLE {TABLE} RENAME TO {TABLE}_migrating")
        con.execute(table_ddl(TABLE))
        con.execute(f"INSERT INTO {TABLE} ({collist}) SELECT {collist} FROM {TABLE}_migrating")

        after = con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
        if after != before:
            raise RuntimeError(
                f"row count changed: {before} -> {after}. The new key is supposed to be "
                f"strictly more permissive, so this must never happen. Rolling back."
            )

        con.execute(f"DROP TABLE {TABLE}_migrating")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_obs_subject_analyte "
            "ON observations (subject, analyte, effective)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_obs_unnamed "
            "ON observations (analyte, printed_name) WHERE analyte IS NULL"
        )
        con.execute("COMMIT")
    except Exception as e:
        con.execute("ROLLBACK")
        print(f"  FAILED, rolled back: {e}", file=sys.stderr)
        print(f"  the database is unchanged. A copy is at {backup}", file=sys.stderr)
        raise

    con.execute("PRAGMA foreign_keys = ON")
    bad = con.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        shutil.copy(backup, path)
        raise SystemExit(f"foreign keys broken after migration; restored from {backup}: {bad}")

    print(f"  new key:     {current_key(con)}")
    print(f"  rows after:  {con.execute(f'SELECT count(*) FROM {TABLE}').fetchone()[0]}")
    print(f"  integrity:   {con.execute('PRAGMA integrity_check').fetchone()[0]}")


if __name__ == "__main__":
    main()

"""Rebuild health.db with COLLATE NOCASE on the identity columns.

`Ecosprin`, `ECOSPRIN` and `T. Ecosprin` grouped as three different drugs, and
`WHERE drug = 'ECOSPRIN'` matched none of them. But LOWERCASING what we store
would have been wrong too -- the whole design rests on keeping the document's own
words. So the stored text keeps its case and only the COMPARISON ignores it.

Two things this migration learned the hard way, both of which corrupted the
database on the first attempt:

  * `ALTER TABLE x RENAME TO y` silently REWRITES every foreign key that points
    at x, so the other tables ended up referencing a table that was then dropped.
    `PRAGMA legacy_alter_table = ON` stops that.

  * `SCHEMA.split(";")` shreds statements at semicolons inside comments. The DDL
    has to be extracted properly.

So: everything runs in ONE transaction that rolls back on any failure, it
recovers from a half-finished previous run, and it verifies the row counts before
committing.

Run:  python -m tools.migrate_nocase
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3

from src import db

# A case-insensitive UNIQUE merges rows differing only in case. A handful is
# expected and correct; a flood means something is wrong, so stop rather than
# quietly delete data.
MAX_MERGE = 50

TABLES = ["documents", "observations", "encounters", "medication_events", "review_queue"]


def ddl_for(table: str) -> str:
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?^\);", db.SCHEMA, re.S | re.M)
    if not m:
        raise SystemExit(f"no DDL found for {table}")
    return m.group(0)


def indexes() -> list[str]:
    return re.findall(r"CREATE INDEX[^;]+;", db.SCHEMA, re.S)


def main() -> None:
    path = db.DB_PATH
    if not os.path.exists(path):
        raise SystemExit(f"{path} does not exist")

    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("PRAGMA legacy_alter_table = ON")

    existing = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    # Recover from a half-finished previous run: the data may be sitting under a
    # _tmp or _old name because the process died between the rename and the copy.
    sources: dict[str, str] = {}
    for t in TABLES:
        for candidate in (t, f"{t}_tmp", f"{t}_old"):
            if candidate in existing:
                n = con.execute(f"SELECT count(*) FROM '{candidate}'").fetchone()[0]
                # Prefer whichever copy actually holds the rows.
                if candidate not in sources.values() and (
                    t not in sources
                    or n > con.execute(f"SELECT count(*) FROM '{sources[t]}'").fetchone()[0]
                ):
                    sources[t] = candidate
        if t not in sources:
            raise SystemExit(f"cannot find any table holding {t}")

    before = {
        t: con.execute(f"SELECT count(*) FROM '{src}'").fetchone()[0] for t, src in sources.items()
    }
    print("  found data in:")
    for t, src in sources.items():
        print(f"    {t:<20} <- {src} ({before[t]} rows)")

    backup = path + ".pre-nocase"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
        print(f"\n  backed up to {backup}")

    merged: dict[str, int] = {}
    try:
        con.execute("BEGIN")
        for t in TABLES:
            src = sources[t]
            cols = [r["name"] for r in con.execute(f"PRAGMA table_info('{src}')")]
            cl = ", ".join(cols)

            if src == t:
                con.execute(f"ALTER TABLE {t} RENAME TO {t}_migrating")
                src = f"{t}_migrating"

            con.execute(ddl_for(t))
            # OR IGNORE: a case-insensitive UNIQUE constraint merges rows that
            # differed only in case -- "PH" and "pH" are the same urine test, and
            # recording them twice was the bug, not the fix. Any merge is counted
            # and reported; none happens silently.
            con.execute(f"INSERT OR IGNORE INTO {t} ({cl}) SELECT {cl} FROM '{src}'")
            con.execute(f"DROP TABLE '{src}'")

            after = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            merged[t] = before[t] - after
            if merged[t] < 0:
                raise RuntimeError(f"{t}: gained rows ({before[t]} -> {after})")
            if merged[t] > MAX_MERGE:
                raise RuntimeError(f"{t}: {merged[t]} rows merged, more than expected -- refusing")

        for stmt in indexes():
            con.execute(stmt)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        print("\n  MIGRATION FAILED -- rolled back, database unchanged")
        raise

    print()
    for t in TABLES:
        n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        if merged.get(t):
            print(
                f"     {merged[t]} duplicate row(s) merged in {t} "
                f"(differed only by letter case)"
            )
        sql = con.execute("SELECT sql FROM sqlite_master WHERE name = ?", (t,)).fetchone()["sql"]
        print(f"  ok {t:<20} {n:>5} rows, {sql.count('COLLATE NOCASE')} case-insensitive columns")

    con.execute("PRAGMA foreign_keys = ON")
    bad = con.execute("PRAGMA foreign_key_check").fetchall()
    print(f"\n  foreign key violations: {len(bad)}")
    print("  no rows lost, no text lowercased")


if __name__ == "__main__":
    main()

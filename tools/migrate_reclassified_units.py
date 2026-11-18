"""Convert unscaled `observations.value_num` left by pre-hardening `db.reclassify()`.

Earlier versions of `db.reclassify()` resolved `analyte` without converting
`value_num` from the printed unit into the codebook's canonical unit (for
example leaving Platelet Count at `215.0` with `unit='x10^3/uL'` instead of
`215000.0` in `/uL`).

Idempotent: safe to re-run. Backs the database up first (mode 0600).

Run against PROD (default is data/health.db, the repo's throwaway copy):
    ./venv/bin/python -m tools.migrate_reclassified_units ~/docker-stacks/nalam/config/health.db
"""

from __future__ import annotations

import argparse
from collections import Counter
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any

from src import db
from src.normalize import parse_value
from src.units import _fold, convert, load_units


def _has_non_unit_factor(entry: dict[str, Any], printed_unit: str) -> bool:
    folded = _fold(printed_unit)
    if not folded:
        return False
    for unit_key, factor in entry.get("convert", {}).items():
        if _fold(unit_key) == folded and float(factor) != 1.0:
            return True
    return False


def _scan_rows(
    con: sqlite3.Connection, units: dict[str, dict[str, Any]]
) -> tuple[int, Counter[tuple[str, str, str]], list[tuple[float, int]]]:
    rows = con.execute(
        "SELECT id, analyte, value_num, raw_value, unit "
        "FROM observations "
        "WHERE analyte IS NOT NULL AND value_num IS NOT NULL"
    ).fetchall()

    counts: Counter[tuple[str, str, str]] = Counter()
    updates: list[tuple[float, int]] = []

    for r in rows:
        analyte = r["analyte"]
        entry = units.get(analyte)
        if entry is None:
            continue
        printed_unit = r["unit"] or ""
        if not _has_non_unit_factor(entry, printed_unit):
            continue

        raw_num, _ = parse_value(r["raw_value"] or "")
        current_num = float(r["value_num"])
        if (
            raw_num is not None
            and raw_num != 0.0
            and abs(current_num - raw_num) <= 1e-9 * max(1.0, abs(raw_num))
        ):
            converted, canonical, reason = convert(analyte, raw_num, printed_unit, units)
            if reason is None and converted is not None and converted != current_num:
                counts[(analyte, printed_unit, canonical)] += 1
                updates.append((converted, r["id"]))

    return len(rows), counts, updates


def migrate(path: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Scan and convert unscaled numeric observations whose unit factor != 1.0."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"database not found: {path}")

    units = load_units()
    backup: str | None = None

    if dry_run:
        con = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    else:
        con = db.connect(path)

    try:
        if dry_run:
            scanned, counts, updates = _scan_rows(con, units)
        else:
            backup = f"{path}.pre-reclassify-units.bak"
            if not os.path.exists(backup):
                con.execute("VACUUM INTO ?", (backup,))
                os.chmod(backup, 0o600)
                print(f"  backup:      {backup}")
            else:
                print(f"  backup:      {backup} (exists, kept)")

            con.isolation_level = None
            con.execute("BEGIN IMMEDIATE")
            try:
                scanned, counts, updates = _scan_rows(con, units)
                if updates:
                    con.executemany("UPDATE observations SET value_num = ? WHERE id = ?", updates)
                con.execute("COMMIT")
            except Exception:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise

        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        action = "would convert" if dry_run else "converted"
        print(f"  observations: {scanned} scanned, {len(updates)} {action}")
        for (analyte, unit, canonical), count in sorted(counts.items()):
            print(f"    {analyte}: {unit} -> {canonical} ({count})")
        print(f"  integrity:   {integrity}")

        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity_check failed: {integrity}")

        return {
            "path": path,
            "dry_run": dry_run,
            "backup": backup,
            "scanned": scanned,
            "would_convert": len(updates),
            "converted": 0 if dry_run else len(updates),
            "by_unit": dict(counts),
            "integrity": integrity,
        }
    finally:
        con.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Convert unscaled observations left by pre-hardening db.reclassify()."
    )
    parser.add_argument(
        "db_path",
        nargs="?",
        default=db.DB_PATH,
        help=f"Path to health.db (default: {db.DB_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report rows that would be converted without modifying the DB or creating a backup.",
    )
    args = parser.parse_args(argv)
    try:
        migrate(args.db_path, dry_run=args.dry_run)
    except (FileNotFoundError, OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

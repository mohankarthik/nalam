"""One-time: re-home imaging narrative that leaked from bundled reports.

A Master Health Checkup is a lab report with a USG / echo / chest-radiograph /
ECG / eye exam stapled on. It is (correctly) classified `lab`, so its blood
panels land in `observations` -- but the lab extractor also pulls every imaging
section, and those rows sit in `observations` as unnamed junk (`SPLEEN`,
`Left kidney measures`, `Fundus Left Eye`). Nobody trends a spleen finding; an
imaging report is narrative, which is exactly why real radiology is stored one
verbatim record per study in `radiology_reports` (see CLAUDE.md).

This migration applies that same decision retroactively to the leaked rows:
for each document carrying imaging-narrative observations, it writes ONE
`radiology_reports` row (linked to the same document the blood panels stay
under) and deletes those observation rows. The structured, codebook-named echo
analytes (EF, LVIDD, Aorta, ...) are NOT touched -- they resolved to the
codebook and belong in `observations`.

Idempotent: a document that already has a radiology_report is left alone and
reported, never merged or double-deleted. Dry-run by default.

    python -m tools.migrate_mhc_imaging                 # plan, touches nothing
    python -m tools.migrate_mhc_imaging --apply         # do it
    python -m tools.migrate_mhc_imaging --db /path/to/health.db --apply
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import OrderedDict, defaultdict

from src import db
from src.normalize import domain_of

# Domains whose UNNAMED results are narrative, not analytes -- the imaging and
# bedside-exam sections a checkup bundles in. Blood/urine/sleep are deliberately
# excluded: an unnamed one of those may be a real analyte worth promoting.
NARRATIVE = {"usg", "xray", "echo", "ecg", "tmt", "opt"}
LABEL = {
    "usg": "USG",
    "xray": "X-Ray",
    "echo": "Echo",
    "ecg": "ECG",
    "opt": "Ophthalmology",
    "tmt": "TMT",
}


def _value(row: sqlite3.Row) -> str:
    """The finding as printed: the verbatim raw_value, else the parsed text/number."""
    val = row["raw_value"] or row["value_text"]
    if val is None and row["value_num"] is not None:
        val = str(row["value_num"])
    return (val or "").strip()


def build_report(rows: list[sqlite3.Row]) -> tuple[str, str | None, str]:
    """Reconstruct (study_type, impression, report_text) from a document's
    imaging-narrative observation rows, grouped by the section they sat under."""
    by_section: "OrderedDict[str, list[tuple[str, str]]]" = OrderedDict()
    labels: set[str] = set()
    impressions: list[str] = []

    for r in rows:
        domain = domain_of(r["section"] or "", r["printed_name"] or "")
        labels.add(LABEL[domain])
        section = r["section"] or "(no section)"
        name = (r["printed_name"] or "").strip()
        val = _value(r)
        by_section.setdefault(section, []).append((name, val))
        if "IMPRESSION" in f"{name} {section}".upper() and val:
            impressions.append(val)

    lines: list[str] = []
    for section, items in by_section.items():
        lines.append(section)
        for name, val in items:
            lines.append(f"  {name}: {val}" if name else f"  {val}")

    study_type = " · ".join(sorted(labels))
    impression = "\n".join(impressions) or None
    return study_type, impression, "\n".join(lines)


def migrate(con: sqlite3.Connection, apply: bool) -> None:
    per_doc: "defaultdict[int, list[sqlite3.Row]]" = defaultdict(list)
    for r in con.execute("SELECT * FROM observations WHERE analyte IS NULL"):
        if domain_of(r["section"] or "", r["printed_name"] or "") in NARRATIVE:
            per_doc[r["document_id"]].append(r)

    existing = {row[0] for row in con.execute("SELECT document_id FROM radiology_reports")}
    made = deleted = skipped = 0

    for doc_id, rows in sorted(per_doc.items()):
        if doc_id in existing:
            skipped += 1
            print(f"  SKIP doc #{doc_id}: already radiology ({len(rows)} rows left in place)")
            continue

        subject = rows[0]["subject"]
        effective = next((r["effective"] for r in rows if r["effective"]), None)
        study_type, impression, report_text = build_report(rows)
        print(f"  doc #{doc_id} [{subject}] {effective} -> {study_type} ({len(rows)} rows)")

        if apply:
            con.execute(
                """INSERT INTO radiology_reports
                       (document_id, subject, study_type, effective, impression, report_text)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (doc_id, subject, study_type, effective, impression, report_text),
            )
            con.executemany("DELETE FROM observations WHERE id = ?", [(r["id"],) for r in rows])
        made += 1
        deleted += len(rows)

    if apply:
        con.commit()

    verb = "migrated" if apply else "would migrate"
    print(
        f"\n{verb}: {made} document(s) -> radiology_reports, "
        f"{deleted} observation row(s) re-homed; {skipped} skipped (already radiology)."
    )
    if not apply:
        print("Dry run. Re-run with --apply to write.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=db.DB_PATH, help=f"health.db path (default {db.DB_PATH})")
    p.add_argument("--apply", action="store_true", help="Write; otherwise dry-run")
    args = p.parse_args()

    con = db.connect(args.db)
    try:
        migrate(con, args.apply)
    finally:
        con.close()


if __name__ == "__main__":
    main()

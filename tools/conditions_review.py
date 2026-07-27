"""Export the encounter diagnoses awaiting condition review as an editable worksheet.

A discharge summary's diagnoses are extracted verbatim. Many are legible and map
straight to a condition bucket; the rest -- garbled handwriting ("??rsi"), one-off
findings ("B/L flat foot"), or real conditions we simply have not bucketed ("gout")
-- fall through to raw text and show up unhelpfully in the encounters filter.

This lists exactly the UNDECIDED ones (map to no bucket, match no ignore term, not
yet dismissed), grouped by DOCUMENT so you open one scan and settle all of its
diagnoses at once. Each heading links to the scan in Paperless. You are looking at
the PDF anyway, so the worksheet lets you FIX the OCR right there -- a human reading
handwriting beats re-running the model on it.

Fill in the ACTION column, then:  python -m tools.conditions_review_apply

Run:  python -m tools.conditions_review [path/to/health.db]
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

from src import db
from src.conditions import _tokens, is_undecided, load_conditions
from src.constants import PAPERLESS_URL, SETTINGS

OUT = os.path.expanduser("~/nalam-conditions-review.md")

# The URL a HUMAN opens Paperless at (through the reverse proxy), not the API host.
VIEWER = str(SETTINGS.get("paperless_viewer_url") or PAPERLESS_URL).rstrip("/")


def hint(dx: str) -> str:
    """A cheap 'did you mean this bucket?' -- existing buckets whose key or an alias
    shares a whole word with the diagnosis (partial overlap; full containment already
    failed, that is why it is unmapped). Empty for garbled or genuinely-new terms."""
    dxt = _tokens(dx)
    if not dxt:
        return ""
    scored = []
    for key, entry in load_conditions().items():
        best = 0
        for term in (key, *entry["aliases"]):
            best = max(best, len(_tokens(term) & dxt))
        if best:
            scored.append((best, key))
    scored.sort(reverse=True)
    return ", ".join(k for _, k in scored[:3])


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else db.DB_PATH
    con = db.connect(path)

    rows = con.execute(
        """SELECT e.id, e.subject, e.diagnoses, d.doc_date, d.source_path, d.paperless_id
             FROM encounters e JOIN documents d ON d.id = e.document_id
            ORDER BY e.subject, d.doc_date DESC, e.id"""
    ).fetchall()

    # (subject, date, source, paperless_id) -> list[(enc_id, index, dx)]
    by_doc: dict[tuple, list] = defaultdict(list)
    n_items = 0
    for r in rows:
        diagnoses = json.loads(r["diagnoses"] or "[]")
        for i, dx in enumerate(diagnoses):
            if is_undecided(dx):
                key = (r["subject"], r["doc_date"] or "?", r["source_path"], r["paperless_id"])
                by_doc[key].append((r["id"], i, (dx or "").strip()))
                n_items += 1

    lines = [
        "# Diagnoses awaiting condition review",
        "",
        f"{n_items} undecided diagnoses across {len(by_doc)} documents.",
        "",
        "These map to no condition bucket and match no ignore term. Open the scan "
        "(each heading links to it) and settle each one.",
        "",
        "## How to fill this in — write in the **ACTION** column",
        "",
        "| You write | It means |",
        "|---|---|",
        "| *(blank)* | undecided — leave it, ask again next time |",
        "| `= Correct Text` | **FIX the OCR**: replace the stored diagnosis with this "
        "(it then re-maps automatically) |",
        "| `@bucket-key` | **MAP**: add this diagnosis to an existing bucket as an alias |",
        "| `+key : CODE` | **NEW bucket** `key` with ICD-10 `CODE`; this diagnosis becomes "
        "its alias |",
        "| `ignore: term` | add generic `term` to `conditions_ignore.json` (silence it "
        "everywhere) |",
        "| `-` | **DROP**: delete this diagnosis from the DB (unreadable OCR, no value) |",
        "",
        "Record-specific extras (`- 4 surgeries`, ` Mar 2023`) get **fixed** to a clean "
        "generic term. Unreadable handwriting with no clinical value gets **dropped** "
        "(`-`) rather than perpetually maintained in an ignore list.",
        "",
        "Then run: `./venv/bin/python -m tools.conditions_review_apply`",
        "",
        "---",
        "",
    ]

    current_person = None
    for (subject, date, source, pid), items in by_doc.items():
        if subject != current_person:
            lines += [f"# {subject}", ""]
            current_person = subject

        title = os.path.basename(source)
        if pid:
            heading = f"## [{date} — {title}]({VIEWER}/documents/{pid}/details)"
        else:
            heading = f"## {date} — {title}  *(not in Paperless)*"

        lines += [
            heading,
            "",
            "| id | AS READ | maybe | ACTION |",
            "|---|---|---|---|",
        ]
        for enc_id, idx, dx in items:
            lines.append(f"| {enc_id}:{idx} | `{dx}` | {hint(dx)} |  |")
        lines.append("")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"wrote {OUT}")
    print(f"  {n_items} undecided diagnoses across {len(by_doc)} documents")


if __name__ == "__main__":
    main()

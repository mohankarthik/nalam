"""Export the drugs awaiting review as an editable worksheet.

These are the drug names a vision model read off a scan that no independent
reading could confirm -- mostly handwriting, where Tesseract sees the letterhead
and nothing else. A wrong drug name is the most dangerous thing this system can
emit, so none of them are trusted until a human has looked.

Grouped by DOCUMENT, not by drug: you open one scan and settle all of its drugs
at once, rather than jumping between two hundred images. Each document links
straight to the scan in Paperless.

Fill in the CORRECTION column, then:  python -m tools.import_review

Run:  python -m tools.export_review
"""

from __future__ import annotations

import os
from collections import defaultdict

from src import db
from src.constants import PAPERLESS_URL, SETTINGS
from src.drugs import load_drugs, lookup
from src.paperless import Paperless, fold_filename

OUT = os.path.expanduser("~/nalam-drug-review.md")

# The URL a HUMAN opens Paperless at, which is not the URL the API is called on:
# the API talks to localhost, the browser goes through the reverse proxy. A link
# nobody can click is not a link.
VIEWER = str(SETTINGS.get("paperless_viewer_url") or PAPERLESS_URL).rstrip("/")


def main() -> None:
    con = db.connect()
    table = load_drugs()
    links = Paperless().document_id_index()

    rows = con.execute("""SELECT m.id, m.subject, m.drug, m.strength, m.frequency, m.duration,
                  m.review_reason, d.doc_date, d.source_path, d.doc_type
           FROM medication_events m JOIN documents d ON d.id = m.document_id
           WHERE m.status = 'review'
           ORDER BY m.subject, d.doc_date DESC, m.id""").fetchall()

    by_doc: dict[tuple[str, str, str], list] = defaultdict(list)
    for r in rows:
        by_doc[(r["subject"], r["doc_date"] or "?", r["source_path"])].append(r)

    lines = [
        "# Drugs awaiting review",
        "",
        f"{len(rows)} drug entries across {len(by_doc)} documents.",
        "",
        "A vision model read these off a scan. **No independent reading could confirm "
        "them** — mostly handwriting, where OCR sees the letterhead and little else. "
        "So none of them are trusted yet.",
        "",
        "## How to fill this in",
        "",
        "Open the scan (each heading links to it), then for each drug write in the "
        "**CORRECTION** column:",
        "",
        "| You write | It means |",
        "|---|---|",
        "| *(leave blank)* | the name is right — accept it |",
        "| `Metformin 500` | the correct name (and strength, if you like) |",
        "| `-` | **not a drug** — delete it (a device, a heading, OCR noise) |",
        "| `?` | you can't read it either — leave it in review |",
        "",
        "Then run: `./venv/bin/python -m tools.import_review`",
        "",
        "---",
        "",
    ]

    current_person = None
    for (subject, date, source), meds in by_doc.items():
        if subject != current_person:
            lines += [f"# {subject}", ""]
            current_person = subject

        doc_id = links.get((subject, fold_filename(os.path.basename(source))))
        title = os.path.basename(source)
        if doc_id:
            heading = f"## [{date} — {title}]({VIEWER}/documents/{doc_id}/details)"
        else:
            heading = f"## {date} — {title}  *(not found in Paperless)*"

        lines += [
            heading,
            "",
            "| id | AS READ | strength | freq | guess | CORRECTION |",
            "|---|---|---|---|---|---|",
        ]
        for m in meds:
            d = lookup(m["drug"], table)
            guess = ""
            if d and d.confirmed and d.generic:
                guess = " + ".join(d.generic)
            lines.append(
                f"| {m['id']} | `{m['drug']}` | {m['strength'] or ''} | "
                f"{m['frequency'] or ''} | {guess} |  |"
            )
        lines.append("")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"wrote {OUT}")
    print(f"  {len(rows)} drugs across {len(by_doc)} documents")
    print("  each document links to its scan in Paperless")


if __name__ == "__main__":
    main()

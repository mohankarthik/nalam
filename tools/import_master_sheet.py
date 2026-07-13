"""Import the hand-curated the master sheet.

Produces two artefacts, both of which the rest of nalam depends on:

  data/analytes.json         the codebook: 70+ analytes, their segment, and the
                             per-person reference range the user chose (NOT the
                             lab's -- labs disagree, and a trend against a moving
                             range is meaningless).

  tests/fixtures/golden.json every value the user typed by hand, 2010-2025.
                             This is the extractor's ground truth: run the
                             extractor over the source PDFs for these dates and
                             diff. Nothing else in this project can tell us
                             whether the LLM is quietly lying.

Run:  python -m tools.import_master_sheet
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from src.constants import SETTINGS

SPREADSHEET_ID = SETTINGS.get("master_sheet_id", "")

# Sheet tab -> {correspondent, sex}, from data/settings.json (gitignored).
#
# Ranges are keyed by SEX, not by person: male and female normals genuinely
# differ (creatinine, uric acid, HDL, haemoglobin). A hand-kept sheet usually
# holds one or two people; keying by sex lets everyone else inherit the ranges
# instead of having none at all.
TABS = SETTINGS.get("master_sheet_tabs", {})

NOT_A_VALUE = {"", "#N/A", "N/A", "-", "NA"}


def read_tab(tab: str) -> list[list[str]]:
    out = subprocess.run(
        [
            "gws", "sheets", "+read",
            "--spreadsheet", SPREADSHEET_ID,
            "--range", f"{tab}!A1:AZ200",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [list(map(str, row)) for row in json.loads(out.stdout).get("values", [])]


def find_header(rows: list[list[str]]) -> int:
    """The header row is the one whose first cell is 'Segment'."""
    for i, row in enumerate(rows):
        if row and row[0].strip().lower() == "segment":
            return i
    raise ValueError("No 'Segment' header row found")


def parse_tab(rows: list[list[str]]) -> tuple[list[str], list[dict[str, Any]]]:
    """Return (test dates, analyte rows) from one person's tab."""
    h = find_header(rows)
    header = rows[h]
    dates = [d.strip() for d in header[4:] if d.strip()]

    analytes: list[dict[str, Any]] = []
    segment = ""
    for row in rows[h + 1 :]:
        row = row + [""] * (4 + len(dates) - len(row))
        seg, name, low, high = (c.strip() for c in row[:4])
        if seg:
            segment = seg
        if not name:
            continue
        analytes.append(
            {
                "segment": segment,
                "name": name,
                "low": low,
                "high": high,
                "values": dict(zip(dates, (c.strip() for c in row[4 : 4 + len(dates)]))),
            }
        )
    return dates, analytes


def to_number(text: str) -> float | None:
    if text in NOT_A_VALUE:
        return None
    try:
        return float(text)
    except ValueError:
        return None  # free-text observation (e.g. TMT 'Final Diag')


def main() -> None:
    codebook: dict[str, dict[str, Any]] = {}
    golden: list[dict[str, Any]] = []

    for tab, who in TABS.items():
        person, sex = who["correspondent"], who["sex"]
        rows = read_tab(tab)
        dates, analytes = parse_tab(rows)
        print(f"{tab}: {len(analytes)} analytes, {len(dates)} test dates")

        for a in analytes:
            entry = codebook.setdefault(
                a["name"], {"segment": a["segment"], "aliases": [], "ranges": {}}
            )
            low, high = to_number(a["low"]), to_number(a["high"])
            if low is not None or high is not None:
                entry["ranges"][sex] = {"low": low, "high": high}

            for date, raw in a["values"].items():
                if raw in NOT_A_VALUE:
                    continue
                golden.append(
                    {
                        "person": person,
                        "segment": a["segment"],
                        "analyte": a["name"],
                        "date": date,
                        "value": to_number(raw),
                        "text": raw,
                    }
                )

    os.makedirs("tests/fixtures", exist_ok=True)
    with open("data/analytes.json", "w", encoding="utf-8") as f:
        json.dump(codebook, f, indent=2, ensure_ascii=False, sort_keys=True)
    with open("tests/fixtures/golden.json", "w", encoding="utf-8") as f:
        json.dump(golden, f, indent=2, ensure_ascii=False)

    numeric = sum(1 for g in golden if g["value"] is not None)
    print(f"\ndata/analytes.json:        {len(codebook)} analytes")
    print(f"tests/fixtures/golden.json: {len(golden)} hand-entered values "
          f"({numeric} numeric, {len(golden) - numeric} free text)")


if __name__ == "__main__":
    main()

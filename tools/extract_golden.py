"""Extract the golden-set PDFs once and cache the raw LLM output.

The golden test must be runnable offline, repeatedly, for free. Extraction is
slow (~100s/PDF) and costs money; normalisation is where the bugs live and needs
fast iteration. So the LLM runs once, here, and the cache is the fixture.

Re-run only when the prompt or the model changes.

Run:  python -m tools.extract_golden
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re

from src.extractor import extract_lab
from src.people import source_path

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

CACHE = "tests/fixtures/extracted"

GOLDEN_SET = "tests/fixtures/golden_set.json"


def golden_set() -> list[tuple[str, str, str]]:
    """Which source PDFs correspond to which hand-entered sheet columns.

    Lives in a gitignored fixture: it names real people and real file paths. The
    sheet date is the COLLECTION date the user typed; the filename date is when
    the scan was saved, and they drift.
    """
    if not os.path.exists(GOLDEN_SET):
        return []
    with open(GOLDEN_SET, encoding="utf-8") as f:
        return [(d["person"], d["sheet_date"], d["source"]) for d in json.load(f)["documents"]]


def slug(person: str, rel: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", f"{person}-{os.path.basename(rel)}".lower()).strip("-")


def main() -> None:
    os.makedirs(CACHE, exist_ok=True)
    if not golden_set():
        print(f"No {GOLDEN_SET}. Nothing to extract -- you have no ground truth.")
        return
    for person, sheet_date, rel in golden_set():
        out = os.path.join(CACHE, slug(person, rel) + ".json")
        if os.path.exists(out):
            print(f"cached  {rel}")
            continue

        path = source_path(rel)
        file_date = datetime.date.fromisoformat(os.path.basename(rel)[:10])
        try:
            ex = extract_lab(open(path, "rb").read(), person, rel, file_date)
        except Exception as e:
            print(f"FAILED  {rel}: {e}")
            continue

        with open(out, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "person": person,
                    "sheet_date": sheet_date,
                    "source": rel,
                    "model": ex.model,
                    "patient": ex.patient,
                    "doc_ok": ex.doc_verdict.ok,
                    "doc_hard": ex.doc_verdict.hard,
                    "doc_soft": ex.doc_verdict.soft,
                    "passed": ex.passed,
                    "quarantined": ex.quarantined,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        print(
            f"ok      {rel}  [{ex.model}]  passed={len(ex.passed)} "
            f"quarantined={len(ex.quarantined)} doc_ok={ex.doc_verdict.ok}"
        )


if __name__ == "__main__":
    main()

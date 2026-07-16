"""Imaging reports: the list, and the full text of one.

    python run_radiology_browse.py --list --person ila
    python run_radiology_browse.py --list --person ila --since 2020-01-01
    python run_radiology_browse.py --show 412 --person ila

Radiology is stored one verbatim record per study (see the radiology_reports
table), because an imaging report is read, not trended. --list shows the study
type, date and impression; --show prints the whole report text.

"ila" here is whatever alias you put in data/people.json.
"""

from __future__ import annotations

import argparse
import logging

from src import db
from src.people import load_people, resolve
from src.qa import doc_link

logger = logging.getLogger(__name__)


def resolve_person(who: str) -> str:
    person = resolve(who)
    if person is None:
        raise SystemExit(
            f"unknown person: {who!r} (use a name, a folder, or an alias from data/people.json)"
        )
    return person.correspondent


def show_list(con, who: str | None, since: str | None) -> None:
    subjects = [resolve_person(who)] if who else list(load_people())

    for subject in subjects:
        rows = db.radiology_for(con, subject, since)
        if not rows:
            continue

        print(f"\n{'=' * 78}")
        print(f"{subject} — {len(rows)} imaging report(s)")
        print("=" * 78)
        for r in rows:
            print(f"\n  {r['effective'] or '?'}  {r['study_type'] or 'Radiology'}")
            if r["impression"]:
                print(f"     {r['impression'][:70]}")
            link = doc_link(con, r["document_id"])
            print(f"     doc {r['document_id']}" + (f"  {link}" if link else ""))


def show_one(con, document_id: int, who: str | None) -> None:
    subjects = [resolve_person(who)] if who else list(load_people())
    for subject in subjects:
        r = db.radiology_report(con, document_id, subject)
        if not r:
            continue
        print(f"\n{'=' * 78}")
        print(f"{subject}  |  {r['study_type'] or 'Radiology'}  |  {r['effective'] or '?'}")
        print("=" * 78)
        if r["impression"]:
            print(f"\nIMPRESSION: {r['impression']}")
        print(f"\n{r['report_text'] or '(no report text on file)'}")
        link = doc_link(con, document_id)
        if link:
            print(f"\n{link}")
        return
    print(f"\nNo imaging report {document_id} for {who or 'anyone'}.")


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--list", action="store_true", help="Imaging reports, most recent first")
    p.add_argument("--show", type=int, metavar="DOC_ID", help="Print one report's full text")
    p.add_argument("--person", help="Name, folder, or alias from data/people.json")
    p.add_argument("--since", help="ISO date lower bound for --list")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    con = db.connect()
    if args.show:
        show_one(con, args.show, args.person)
        return
    show_list(con, args.person, args.since)


if __name__ == "__main__":
    main()

"""The live medicine list, and the reconciliation cards that maintain it.

    python run_meds.py --list                 what everyone is believed to be on
    python run_meds.py --list --person dad
    python run_meds.py --reconcile            diffs needing a human decision
    python run_meds.py --decide dad "ATORVA" stopped 2024-01-15

A prescription says what STARTED. Nothing says what STOPPED. So the list is only
as true as the last decision a human made -- see src/meds.py.

"dad" here is whatever alias you put in data/people.json. The code has no opinion
about family relationships; they are yours to name.
"""

from __future__ import annotations

import argparse
import logging

from src import db, meds
from src.people import load_people, resolve

logger = logging.getLogger(__name__)


def resolve_person(who: str) -> str:
    person = resolve(who)
    if person is None:
        raise SystemExit(
            f"unknown person: {who!r} "
            "(use a name, a folder, or an alias from data/people.json)"
        )
    return person.correspondent


def show_list(con, person: str | None) -> None:
    people = load_people()
    subjects = [resolve_person(person)] if person else list(people)

    for subject in subjects:
        active = meds.current(con, subject)
        if not active:
            continue

        print(f"\n{'=' * 78}")
        print(f"{subject} — believed current: {len(active)}")
        print("=" * 78)
        print(f"  {'MEDICINE':<44} {'STARTED':<12} {'DOSE':<9} FREQ")
        for m in sorted(active, key=lambda x: x.display.lower()):
            mark = "" if m.status == "ok" else "  [unconfirmed]"
            print(
                f"  {m.display[:44]:<44} {(m.effective or '?'):<12} "
                f"{(m.strength or '-'):<9} {m.frequency or '-'}{mark}"
            )

        stale = [m for m in active if m.effective and m.effective < "2024-01-01"]
        if stale:
            print(
                f"\n  ! {len(stale)} of these were last seen before 2024 and have never been"
                "\n    reconciled. Nothing says they stopped -- but nothing says they didn't."
            )


def show_reconcile(con, person: str | None) -> None:
    """Reconciliation cards, grouped by person, then by document."""
    want = resolve_person(person) if person else None

    rows = con.execute(
        """SELECT DISTINCT m.subject, m.document_id, d.doc_date, d.source_path
           FROM medication_events m JOIN documents d ON d.id = m.document_id
           WHERE m.entered_by = 'extractor'
           ORDER BY m.subject, d.doc_date"""
    ).fetchall()

    by_person: dict[str, list] = {}
    for r in rows:
        if want and r["subject"] != want:
            continue
        by_person.setdefault(r["subject"], []).append(r)

    for subject, docs in by_person.items():
        cards = [(r, meds.reconcile(con, subject, r["document_id"])) for r in docs]
        cards = [(r, rec) for r, rec in cards if rec.needs_decision]
        if not cards:
            continue

        print(f"\n{'=' * 78}")
        print(f"{subject}")
        print("=" * 78)

        for r, rec in cards:
            source = r["source_path"].split("/")[-1]
            print(f"\n  {r['doc_date']}  {source}")
            print(f"     {'MEDICINE':<42} {'STARTED':<12} {'DOSE':<9} FREQ")

            for m in rec.started:
                print(
                    f"  +  {m.display[:42]:<42} {(m.effective or '?'):<12} "
                    f"{(m.strength or '-'):<9} {m.frequency or '-'}"
                )
            for was, now in rec.changed:
                print(
                    f"  ~  {now.display[:42]:<42} {(now.effective or '?'):<12} "
                    f"{(now.strength or '-'):<9} {now.frequency or '-'}"
                )
                print(f"     {'':<42} was: {was.strength or '-'} {was.frequency or '-'}")
            for m in rec.stopped:
                print(
                    f"  ?  {m.display[:42]:<42} {(m.effective or '?'):<12} "
                    f"{(m.strength or '-'):<9} {m.frequency or '-'}   not listed here"
                )

            if rec.stopped:
                print(
                    "     'not listed' is NOT 'stopped' -- a summary often omits a patient's"
                    "\n     long-term drugs. Settle it with --decide."
                )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--list", action="store_true", help="Show the believed-current list")
    p.add_argument(
        "--reconcile", action="store_true", help="Show diffs needing a decision"
    )
    p.add_argument("--person", help="Name, folder, or alias from data/people.json")
    p.add_argument(
        "--decide",
        nargs=4,
        metavar=("PERSON", "DRUG", "EVENT", "DATE"),
        help="Record a human decision: prescribed | continued | changed | stopped",
    )
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    con = db.connect()
    if args.decide:
        who, drug, event, date = args.decide
        meds.record_decision(con, resolve_person(who), drug, event, date)
        print(f"recorded: {who} {drug} -> {event} on {date}")
        return
    if args.reconcile:
        show_reconcile(con, args.person)
        return
    show_list(con, args.person)


if __name__ == "__main__":
    main()

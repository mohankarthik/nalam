"""Hospital stays and discharge summaries: the list, what one was FOR, and
which follow-up instructions are still open.

    python run_encounters.py --list --person dad
    python run_encounters.py --list --person dad --since 2024-01-01

    python run_encounters.py --for "hand foot mouth"
    python run_encounters.py --for "hand foot mouth" --person dad

    python run_encounters.py --follow-up
    python run_encounters.py --follow-up --person dad

--follow-up is the nagging list: every encounter that recorded a follow-up
instruction, most overdue first. Nothing here marks one done -- that
requires a human decision the way a medication stop does (run_meds.py
--decide); CLAUDE.md lists Todoist reminders as not-yet-built, and this is
the manual stopgap until that lands.

"dad" here is whatever alias you put in data/people.json.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging

from src import db, meds
from src.people import load_people, resolve
from src.qa import doc_link

logger = logging.getLogger(__name__)

TODAY = datetime.date.today().isoformat()


def resolve_person(who: str) -> str:
    person = resolve(who)
    if person is None:
        raise SystemExit(
            f"unknown person: {who!r} " "(use a name, a folder, or an alias from data/people.json)"
        )
    return person.correspondent


def show_list(con, who: str | None, since: str | None) -> None:
    people = load_people()
    subjects = [resolve_person(who)] if who else list(people)

    for subject in subjects:
        sql = "SELECT * FROM encounters WHERE subject = ?"
        params: list[str] = [subject]
        if since:
            sql += " AND (admitted IS NULL OR admitted >= ?)"
            params.append(since)
        sql += " ORDER BY admitted DESC"
        rows = con.execute(sql, params).fetchall()
        if not rows:
            continue

        print(f"\n{'=' * 78}")
        print(f"{subject} — {len(rows)} encounter(s)")
        print("=" * 78)
        for r in rows:
            dx = "; ".join(json.loads(r["diagnoses"] or "[]")) or (r["reason"] or "?")
            span = r["admitted"] or "?"
            if r["discharged"] and r["discharged"] != r["admitted"]:
                span += f" -> {r['discharged']}"
            print(f"\n  {span}  {r['hospital'] or 'unknown hospital'}")
            print(f"     {dx[:70]}")
            procedures = json.loads(r["procedures"] or "[]")
            if procedures:
                print(f"     procedures: {'; '.join(procedures)[:70]}")
            if r["follow_up"]:
                due = f" (due {r['follow_up_date']})" if r["follow_up_date"] else ""
                print(f"     follow-up: {r['follow_up'][:70]}{due}")
            link = doc_link(con, r["document_id"])
            if link:
                print(f"     {link}")


def show_for_condition(con, condition: str, who: str | None) -> None:
    """What happened, and what was prescribed, for a given diagnosis or
    complaint. Reuses src.meds.for_condition -- it already joins the
    encounter to the medicines given during it, so this is the encounter
    side of the same lookup run_meds.py --for shows the medicine side of."""
    subject = resolve_person(who) if who else None
    episodes = meds.for_condition(con, condition, subject=subject)
    if not episodes:
        print(f"\nNothing recorded for {condition!r}.")
        return

    print(f"\n{condition!r} — {len(episodes)} episode(s), most recent first")
    for e in episodes:
        dx = "; ".join(json.loads(e["diagnoses"] or "[]")) or (e["reason"] or "?")
        print(f"\n  {e['date'] or '?'}  {e['subject']}  —  {dx[:56]}")
        if e["follow_up"]:
            print(f"     follow-up: {e['follow_up'][:56]}")
        if not e["medications"]:
            print("     (no medicines recorded)")
        for m in e["medications"]:
            name = f"{m['generic']} ({m['drug']})" if m["generic"] else m["drug"]
            mark = "" if m["status"] == "ok" else "  [unconfirmed]"
            freq = m["frequency"] or "-"
            print(f"     - {name[:38]:<38} {(m['strength'] or '-'):<9} {freq}{mark}")


def show_follow_up(con, who: str | None) -> None:
    people = load_people()
    want = resolve_person(who) if who else None

    sql = "SELECT * FROM encounters WHERE follow_up IS NOT NULL AND follow_up != ''"
    params: list[str] = []
    if want:
        sql += " AND subject = ?"
        params.append(want)
    sql += " ORDER BY IFNULL(follow_up_date, '9999-99-99'), admitted"

    rows = con.execute(sql, params).fetchall()
    # Deceased people generate no more follow-ups worth nagging about --
    # same exclusion run_meds.py --reconcile makes for the living-only queue.
    rows = [r for r in rows if not (people.get(r["subject"]) and people[r["subject"]].deceased)]
    if not rows:
        print(f"\nNo open follow-ups for {want or 'anyone'}.")
        return

    print(f"\n{len(rows)} follow-up instruction(s) on file, earliest due date first")
    for r in rows:
        overdue = r["follow_up_date"] and r["follow_up_date"] < TODAY
        mark = "  ! OVERDUE" if overdue else ""
        due = r["follow_up_date"] or "no date given"
        print(f"\n  {r['subject']}  —  due {due}{mark}")
        print(f"     {r['follow_up'][:78]}")
        print(f"     from: {r['admitted'] or '?'} {r['hospital'] or ''}".rstrip())
        link = doc_link(con, r["document_id"])
        if link:
            print(f"     {link}")


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--list", action="store_true", help="Encounters, most recent first")
    p.add_argument(
        "--follow-up", action="store_true", help="Open follow-up instructions, overdue first"
    )
    p.add_argument("--for", dest="condition", help="What was recorded for a diagnosis/complaint")
    p.add_argument("--person", help="Name, folder, or alias from data/people.json")
    p.add_argument("--since", help="ISO date lower bound for --list")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    con = db.connect()
    if args.condition:
        show_for_condition(con, args.condition, args.person)
        return
    if args.follow_up:
        show_follow_up(con, args.person)
        return
    show_list(con, args.person, args.since)


if __name__ == "__main__":
    main()

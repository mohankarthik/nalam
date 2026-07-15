"""The lab observation record: latest snapshot, a single analyte's trend, and
what still needs a human to look at it.

    python run_observations.py --list --person dad
    python run_observations.py --list --person dad --since 2024-01-01

    python run_observations.py --history --person dad --analyte HbA1c

    python run_observations.py --review
    python run_observations.py --review --person dad

--list shows the MOST RECENT value of every analyte on file, one row each --
a snapshot, not a log. --history is the log: every value ever recorded for
ONE named analyte, because a person's entire observation history (thousands
of rows across a decade) has no sane single table -- see run_meds.py, which
draws the same line for --history --drug.

Every value is flagged high/low/normal against the PERSON'S OWN range
(src.people.flag_observation) -- never the lab's printed range blindly, and
never at all for a child with no lab range printed (see CLAUDE.md and
src/people.py: guessing a child's range wrong is worse than no flag).

"dad" here is whatever alias you put in data/people.json.
"""

from __future__ import annotations

import argparse
import logging

from src import db
from src.normalize import load_codebook
from src.people import Person, flag_observation, load_people, resolve
from src.qa import doc_link

logger = logging.getLogger(__name__)


def resolve_person(who: str) -> Person:
    person = resolve(who)
    if person is None:
        raise SystemExit(
            f"unknown person: {who!r} " "(use a name, a folder, or an alias from data/people.json)"
        )
    return person


def _flag(con, person: Person, codebook: dict, row) -> str:
    if not row["analyte"]:
        return ""
    flag, _source = flag_observation(
        person,
        row["analyte"],
        row["raw_value"],
        row["value_num"],
        row["ref_low"],
        row["ref_high"],
        codebook,
    )
    return {"high": " H", "low": " L"}.get(flag, "")


def show_list(con, who: str, since: str | None) -> None:
    person = resolve_person(who)
    codebook = load_codebook()

    sql = "SELECT * FROM observations WHERE subject = ? AND effective IS NOT NULL"
    params = [person.correspondent]
    if since:
        sql += " AND effective >= ?"
        params.append(since)
    sql += " ORDER BY IFNULL(analyte, printed_name), effective DESC"

    # Latest row wins, per (analyte or printed_name) -- the ORDER BY above puts
    # it first, so a plain "seen it already" check is enough to dedupe.
    seen: set[str] = set()
    latest = []
    for r in con.execute(sql, params).fetchall():
        key = (r["analyte"] or r["printed_name"]).lower()
        if key in seen:
            continue
        seen.add(key)
        latest.append(r)

    if not latest:
        print(f"\nNo observations for {person.correspondent}.")
        return

    latest.sort(key=lambda r: (r["analyte"] or r["printed_name"]).lower())
    print(f"\n{person.correspondent} — {len(latest)} analytes, most recent value of each")
    print(f"  {'ANALYTE':<32} {'VALUE':<12} {'UNIT':<10} {'DATE':<12} FLAG")
    for r in latest:
        name = r["analyte"] or f"{r['printed_name']} (unnamed)"
        value = r["value_num"] if r["value_num"] is not None else r["value_text"]
        mark = "" if r["status"] == "ok" else "  [unconfirmed]"
        print(
            f"  {name[:32]:<32} {str(value)[:12]:<12} {(r['unit'] or '-'):<10} "
            f"{(r['effective'] or '?'):<12} {_flag(con, person, codebook, r)}{mark}"
        )


def show_history(con, who: str, analyte: str) -> None:
    person = resolve_person(who)
    codebook = load_codebook()

    rows = con.execute(
        """SELECT * FROM observations
           WHERE subject = ? AND effective IS NOT NULL
             AND (LOWER(IFNULL(analyte,'')) = LOWER(?) OR LOWER(printed_name) LIKE LOWER(?))
           ORDER BY effective DESC""",
        (person.correspondent, analyte, f"%{analyte}%"),
    ).fetchall()

    if not rows:
        print(f"\nNo record of {analyte!r} for {person.correspondent}.")
        return

    print(f"\n{analyte!r} — {person.correspondent} — {len(rows)} values, most recent first")
    print(f"  {'DATE':<12} {'VALUE':<12} {'UNIT':<10} {'FLAG':<6} SOURCE")
    for r in rows:
        value = r["value_num"] if r["value_num"] is not None else r["value_text"]
        mark = "" if r["status"] == "ok" else " [unconfirmed]"
        link = doc_link(con, r["document_id"]) or ""
        print(
            f"  {(r['effective'] or '?'):<12} {str(value)[:12]:<12} {(r['unit'] or '-'):<10} "
            f"{_flag(con, person, codebook, r):<6}{mark} {link}"
        )


def show_review(con, who: str | None) -> None:
    """Every observation status='review', browsable -- printed_name, raw value,
    section, and a link to the scan behind it, grouped by person then by
    reason. run_extract.py --review only counts these; this is the queue a
    human actually works from."""
    people = load_people()
    want = resolve_person(who).correspondent if who else None

    sql = """SELECT o.*, d.source_path FROM observations o
             JOIN documents d ON d.id = o.document_id
             WHERE o.status = 'review'"""
    params: list[str] = []
    if want:
        sql += " AND o.subject = ?"
        params.append(want)
    sql += " ORDER BY o.subject, o.review_reason, o.effective"

    by_person: dict[str, list] = {}
    for r in con.execute(sql, params).fetchall():
        by_person.setdefault(r["subject"], []).append(r)

    if not by_person:
        print(f"\nNothing in review for {want or 'anyone'}.")
        return

    for subject, rows in by_person.items():
        person = people.get(subject)
        deceased = person.deceased if person else False
        print(f"\n{'=' * 78}")
        print(f"{subject}{' (deceased)' if deceased else ''} — {len(rows)} in review")
        print("=" * 78)
        for r in rows:
            source = r["source_path"].split("/")[-1]
            link = doc_link(con, r["document_id"]) or ""
            print(
                f"\n  {(r['effective'] or '?'):<12} {r['printed_name'][:36]:<36} "
                f"raw={r['raw_value']!r} section={r['section'] or '-'}"
            )
            print(f"     {r['review_reason']}")
            print(f"     {source}  {link}")


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--list", action="store_true", help="Latest value of every analyte on file")
    p.add_argument(
        "--history", action="store_true", help="Every value ever recorded for one analyte"
    )
    p.add_argument("--review", action="store_true", help="Observations not yet trusted, and why")
    p.add_argument("--person", help="Name, folder, or alias from data/people.json")
    p.add_argument("--analyte", help="Test name -- canonical or as printed. Required for --history")
    p.add_argument("--since", help="ISO date lower bound for --list")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    con = db.connect()
    if args.history:
        if not args.analyte or not args.person:
            raise SystemExit("--history needs both --person and --analyte")
        show_history(con, args.person, args.analyte)
        return
    if args.review:
        show_review(con, args.person)
        return
    if not args.person:
        raise SystemExit("--list needs --person")
    show_list(con, args.person, args.since)


if __name__ == "__main__":
    main()

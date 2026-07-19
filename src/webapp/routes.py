"""Route handlers. Thin on purpose: person resolution + a call into
src/qa.py, src/meds.py, src/people.py, src/db.py, then a template render.
The two queries not already exposed elsewhere (the full-analyte snapshot,
which needs every analyte rather than qa.py's Q&A-sized limit, and the
review_queue resolve) are the only net-new SQL in this file."""

from __future__ import annotations

import datetime
import os
import sqlite3
from typing import Generator, List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from src import db, meds
from src.drugs import load_drugs
from src.normalize import load_codebook, match, promote
from src.people import Person, flag_observation, load_people
from src.qa import doc_link, get_observations
from src.webapp.charts import sparkline_svg

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def get_db() -> Generator[sqlite3.Connection, None, None]:
    con = db.connect()
    try:
        yield con
    finally:
        con.close()


def _default_person() -> str:
    people = sorted(load_people().values(), key=lambda p: (p.deceased, p.correspondent.lower()))
    if not people:
        raise HTTPException(500, "data/people.json has no one in it")
    return people[0].correspondent


def current_person(person: Optional[str] = None) -> Person:
    who = person or _default_person()
    resolved = None
    for p in load_people().values():
        if p.correspondent == who:
            resolved = p
            break
    if resolved is None:
        raise HTTPException(404, f"unknown person: {who!r}")
    return resolved


def nav_context(request: Request, person: Person) -> dict:
    people = sorted(load_people().values(), key=lambda p: p.correspondent.lower())
    return {"request": request, "people": people, "person": person}


@router.get("/")
def index(person: Optional[str] = None) -> RedirectResponse:
    who = person or _default_person()
    return RedirectResponse(url=f"/medications?person={who}")


# --- Medications --------------------------------------------------------


@router.get("/medications")
def medications_page(
    request: Request, person: Optional[str] = None, con: sqlite3.Connection = Depends(get_db)
):
    who = current_person(person)
    active = sorted(meds.current(con, who.correspondent), key=lambda m: m.display.lower())
    for m in active:
        # decorate each Med with a stale flag the template reads; STALE_BEFORE
        # means "not confirmed since" -- see src/meds.py.
        stale = bool(m.effective and m.effective < meds.STALE_BEFORE)
        m.stale = stale  # type: ignore[attr-defined]

    review_rows = con.execute(
        """SELECT m.id, m.drug, m.strength, m.frequency, m.review_reason,
                  d.doc_date, m.document_id
           FROM medication_events m JOIN documents d ON d.id = m.document_id
           WHERE m.status = 'review' AND m.subject = ?
           ORDER BY d.doc_date DESC, m.id""",
        (who.correspondent,),
    ).fetchall()
    review = [{**dict(r), "doc_link": doc_link(con, r["document_id"])} for r in review_rows]

    open_mismatches = con.execute(
        """SELECT id, printed_name, raw_value, reasons, created_at, document_id
           FROM review_queue WHERE subject = ? AND resolved = 0
           ORDER BY created_at DESC""",
        (who.correspondent,),
    ).fetchall()
    mismatches = [{**dict(r), "doc_link": doc_link(con, r["document_id"])} for r in open_mismatches]

    ctx = nav_context(request, who)
    ctx.update(active=active, review=review, mismatches=mismatches)
    return templates.TemplateResponse(request, "medications.html", ctx)


@router.post("/medications/confirm")
def confirm_medication(
    person: str = Form(...),
    drug: str = Form(...),
    event: str = Form(...),
    effective: str = Form(""),
    strength: str = Form(""),
    frequency: str = Form(""),
    con: sqlite3.Connection = Depends(get_db),
) -> RedirectResponse:
    who = current_person(person)
    # A confirmation with no explicit date is dated TODAY -- "still on it" or
    # "he stopped it" is being said right now, whatever the last document said.
    meds.record_decision(
        con,
        subject=who.correspondent,
        drug=drug,
        event=event,
        effective=effective or datetime.date.today().isoformat(),
        strength=strength or None,
        frequency=frequency or None,
    )
    return RedirectResponse(url=f"/medications?person={person}", status_code=303)


@router.post("/medications/review")
def review_medication(
    person: str = Form(...),
    med_id: int = Form(...),
    correction: str = Form(""),
    con: sqlite3.Connection = Depends(get_db),
) -> RedirectResponse:
    table = load_drugs()
    try:
        meds.apply_drug_decision(con, table, med_id, correction)
        con.commit()
    except KeyError:
        pass
    return RedirectResponse(url=f"/medications?person={person}", status_code=303)


# --- Observations --------------------------------------------------------


def _latest_per_analyte(con: sqlite3.Connection, subject: str) -> list[sqlite3.Row]:
    """Most recent value of every analyte on file -- a snapshot, not a log.
    Same query shape as run_observations.py's --list, kept separate from
    src.qa.get_observations because that helper caps rows for Q&A answers
    (OBSERVATION_LIMIT), not for a full per-person snapshot."""
    rows = con.execute(
        """SELECT * FROM observations
           WHERE subject = ? AND effective IS NOT NULL AND analyte IS NOT NULL
           ORDER BY analyte, effective DESC""",
        (subject,),
    ).fetchall()
    seen: set[str] = set()
    latest = []
    for r in rows:
        key = r["analyte"].lower()
        if key in seen:
            continue
        seen.add(key)
        latest.append(r)
    return latest


@router.get("/observations")
def observations_page(
    request: Request, person: Optional[str] = None, con: sqlite3.Connection = Depends(get_db)
):
    who = current_person(person)
    codebook = load_codebook()
    latest = _latest_per_analyte(con, who.correspondent)

    rows = []
    alerts = []
    for r in latest:
        name = r["analyte"]
        flag, _source = flag_observation(
            who, name, r["raw_value"], r["value_num"], r["ref_low"], r["ref_high"], codebook
        )
        entry = {
            "analyte": name,
            "value": r["value_num"] if r["value_num"] is not None else r["value_text"],
            "unit": r["unit"],
            "date": r["effective"],
            "flag": flag,
            "trusted": r["status"] == "ok",
        }
        rows.append(entry)
        if flag in ("high", "low"):
            alerts.append(entry)

    rows.sort(key=lambda e: e["analyte"].lower())

    ctx = nav_context(request, who)
    ctx.update(
        rows=rows,
        alerts=alerts,
        candidates=_promote_candidates(con, who.correspondent),
        # name -> segment for every codebook analyte; drives the promote-form
        # datalist and its segment lock (pick a known analyte -> its segment is
        # fixed; only a brand-new name leaves segment editable).
        analyte_segments={
            name: (entry.get("segment") or "") for name, entry in sorted(codebook.items())
        },
    )
    return templates.TemplateResponse(request, "observations.html", ctx)


def _promote_candidates(con: sqlite3.Connection, subject: str) -> list[dict]:
    """Distinct printed test names this person carries that the codebook can't name.

    These are the review surface for observations: each is either a real analyte
    worth promoting to the allowlist, or junk (a prose fragment, an antibiotic-
    panel line, blood-group antisera) worth rejecting. Only 'no codebook entry'
    rows -- not the ones held back over an OCR-corroboration failure, which are a
    trust problem, not a naming one."""
    rows = con.execute(
        """SELECT o.printed_name,
                  MAX(o.section)   AS section,
                  COUNT(*)         AS n,
                  MAX(o.effective) AS last_seen,
                  MAX(o.raw_value) AS sample,
                  (SELECT s.document_id
                     FROM observations s
                    WHERE s.subject = o.subject AND s.printed_name = o.printed_name
                          AND s.analyte IS NULL
                          AND s.review_reason LIKE '%no codebook entry%'
                    ORDER BY s.effective DESC LIMIT 1) AS document_id
           FROM observations o
           WHERE o.subject = ? AND o.analyte IS NULL
                 AND o.review_reason LIKE '%no codebook entry%'
           GROUP BY o.printed_name
           ORDER BY n DESC, o.printed_name""",
        (subject,),
    ).fetchall()
    return [{**dict(r), "doc_link": doc_link(con, r["document_id"])} for r in rows]


def _selected(selected: List[int], values: List[str]) -> list[str]:
    """The values of the checked rows. The three per-row arrays (printed_name,
    canonical, segment) submit for EVERY row in document order, so row i's data
    is at index i; ``selected`` carries only the checked indices. Guard the
    bounds so a malformed post can't index off the end."""
    return [values[i] for i in selected if 0 <= i < len(values)]


@router.post("/observations/promote")
def promote_observation(
    person: str = Form(...),
    selected: List[int] = Form(default=[]),
    printed_name: List[str] = Form(default=[]),
    canonical: List[str] = Form(default=[]),
    segment: List[str] = Form(default=[]),
    con: sqlite3.Connection = Depends(get_db),
) -> RedirectResponse:
    who = current_person(person)
    for i in selected:
        if 0 <= i < len(printed_name):
            promote(
                printed_name[i],
                canonical[i] if i < len(canonical) else "",
                segment[i] if i < len(segment) else "",
            )
    # One redemption pass after all promotions -- free and offline, and not
    # limited to this person: an alias helps the whole family.
    codebook = load_codebook()

    def resolver(pn: str, section: str):
        analyte = match(pn, codebook, section)
        return analyte, (codebook[analyte].get("segment") if analyte else None)

    db.reclassify(con, resolver)
    return RedirectResponse(url=f"/observations?person={who.correspondent}", status_code=303)


@router.post("/observations/reject")
def reject_observation(
    person: str = Form(...),
    selected: List[int] = Form(default=[]),
    printed_name: List[str] = Form(default=[]),
    con: sqlite3.Connection = Depends(get_db),
) -> RedirectResponse:
    who = current_person(person)
    for name in _selected(selected, printed_name):
        db.drop_unnamed(con, who.correspondent, name)
    return RedirectResponse(url=f"/observations?person={who.correspondent}", status_code=303)


@router.get("/observations/trend")
def observation_trend(
    request: Request,
    person: Optional[str] = None,
    analyte: str = "",
    con: sqlite3.Connection = Depends(get_db),
):
    who = current_person(person)
    values = get_observations(con, who.correspondent, analyte=analyte)
    values = list(reversed(values))  # chronological for the chart

    points = [(v["date"], v["value"]) for v in values if isinstance(v["value"], (int, float))]
    ref_low = next((v["ref_low"] for v in values if v["ref_low"] is not None), None)
    ref_high = next((v["ref_high"] for v in values if v["ref_high"] is not None), None)
    svg = sparkline_svg(points, ref_low, ref_high)

    history = [{**v, "doc_link": doc_link(con, v["document_id"])} for v in reversed(values)]

    ctx = nav_context(request, who)
    ctx.update(analyte=analyte, svg=svg, history=history)
    return templates.TemplateResponse(request, "observation_trend.html", ctx)


# --- Encounters ----------------------------------------------------------


@router.get("/encounters")
def encounters_page(
    request: Request, person: Optional[str] = None, con: sqlite3.Connection = Depends(get_db)
):
    who = current_person(person)
    rows = con.execute(
        "SELECT * FROM encounters WHERE subject = ? ORDER BY admitted DESC",
        (who.correspondent,),
    ).fetchall()

    import json

    encounters = []
    for r in rows:
        encounters.append(
            {
                "document_id": r["document_id"],
                "hospital": r["hospital"],
                "admitted": r["admitted"],
                "discharged": r["discharged"],
                "reason": r["reason"],
                "diagnoses": json.loads(r["diagnoses"] or "[]"),
                "follow_up": r["follow_up"],
                "follow_up_date": r["follow_up_date"],
            }
        )

    ctx = nav_context(request, who)
    ctx.update(encounters=encounters)
    return templates.TemplateResponse(request, "encounters.html", ctx)


@router.get("/radiology")
def radiology_page(
    request: Request, person: Optional[str] = None, con: sqlite3.Connection = Depends(get_db)
):
    who = current_person(person)
    rows = db.radiology_for(con, who.correspondent)
    reports = [
        {
            "document_id": r["document_id"],
            "study_type": r["study_type"],
            "effective": r["effective"],
            "impression": r["impression"],
        }
        for r in rows
    ]
    ctx = nav_context(request, who)
    ctx.update(reports=reports)
    return templates.TemplateResponse(request, "radiology.html", ctx)


@router.get("/radiology/{document_id}")
def radiology_detail(
    request: Request,
    document_id: int,
    person: Optional[str] = None,
    con: sqlite3.Connection = Depends(get_db),
):
    who = current_person(person)
    row = db.radiology_report(con, document_id, who.correspondent)
    if not row:
        raise HTTPException(404, "no such imaging report for this person")
    report = {
        "study_type": row["study_type"],
        "effective": row["effective"],
        "impression": row["impression"],
        "report_text": row["report_text"],
        "doc_link": doc_link(con, document_id),
    }
    ctx = nav_context(request, who)
    ctx.update(report=report)
    return templates.TemplateResponse(request, "radiology_detail.html", ctx)


@router.get("/encounters/{document_id}")
def encounter_detail(
    request: Request,
    document_id: int,
    person: Optional[str] = None,
    con: sqlite3.Connection = Depends(get_db),
):
    who = current_person(person)
    row = con.execute(
        "SELECT * FROM encounters WHERE document_id = ? AND subject = ?",
        (document_id, who.correspondent),
    ).fetchone()
    if not row:
        raise HTTPException(404, "no such encounter for this person")

    import json

    encounter = {
        "hospital": row["hospital"],
        "admitted": row["admitted"],
        "discharged": row["discharged"],
        "reason": row["reason"],
        "diagnoses": json.loads(row["diagnoses"] or "[]"),
        "procedures": json.loads(row["procedures"] or "[]"),
        "follow_up": row["follow_up"],
        "follow_up_date": row["follow_up_date"],
        "doc_link": doc_link(con, document_id),
    }
    medications = meds.from_document(con, document_id)

    ctx = nav_context(request, who)
    ctx.update(encounter=encounter, medications=medications)
    return templates.TemplateResponse(request, "encounter_detail.html", ctx)


# --- Review queue ----------------------------------------------------------


@router.post("/review-queue/{review_id}/resolve")
def resolve_review(
    review_id: int, person: str = Form(...), con: sqlite3.Connection = Depends(get_db)
) -> RedirectResponse:
    con.execute("UPDATE review_queue SET resolved = 1 WHERE id = ?", (review_id,))
    con.commit()
    return RedirectResponse(url=f"/medications?person={person}", status_code=303)

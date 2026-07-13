"""SQLite store. `health.db` is the source of truth; the Sheet is a view of it.

Identity columns are COLLATE NOCASE. `Ecosprin`, `ECOSPRIN` and `T. Ecosprin`
are the same drug, and grouping them as three was simply wrong -- but LOWERCASING
what we store would have been wrong too: the whole design rests on keeping the
document's own words. So the stored text keeps its case and only the COMPARISON
ignores it.

Column names follow FHIR (Observation.subject / .code / .effectiveDateTime /
.valueQuantity) even though this is plain SQLite. It costs nothing now and keeps
a future move to Postgres or Medplum cheap.

Two decisions worth knowing:

* An observation carries a number OR text, never both. The source data demands
  it: HbA1c is 5.2, but HBsAg is "Not Reactive" and an abdominal USG is "Grade I
  fatty liver. Cholelithiasis 1x12mm". FHIR models this the same way.

* Reference ranges are stored TWICE. `ref_low`/`ref_high` are what the lab
  printed (provenance -- labs disagree with each other). The range a value is
  actually FLAGGED against is the user's own, from the codebook, and it is
  per-person. A trend judged against a range that moves between labs is
  meaningless, which is why he chose fixed ones by hand.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

DB_PATH = os.path.join("data", "health.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY,
    paperless_id  INTEGER UNIQUE,          -- link back to the scan + viewer
    subject       TEXT COLLATE NOCASE    NOT NULL,        -- FHIR Patient: the correspondent
    source_path   TEXT    NOT NULL UNIQUE, -- path under the Drive Medical root
    doc_type      TEXT COLLATE NOCASE    NOT NULL,        -- lab | prescription | radiology | ...
    doc_date      TEXT,                    -- ISO; from the filename
    lab           TEXT COLLATE NOCASE,                    -- as printed on the document
    model         TEXT,                    -- which LLM read it
    text_layer    INTEGER NOT NULL DEFAULT 0,  -- 1 = had extractable text
    extracted_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- EVERY extracted result lands here, including the ones we cannot yet name.
--
-- `analyte` is NULL when the printed test name has no codebook entry. It is
-- deliberately NOT a reason to divert the row elsewhere: an earlier design sent
-- unknowns to a side-table that kept only (name, value) -- no unit, no date. So
-- 'R.D.W = 11.6' could never be redeemed by adding an alias later; it would have
-- to be re-extracted through the paid API. Keeping the full row here makes
-- adding an alias a free, offline re-resolution (`run_extract.py --reclassify`).
--
-- The codebook was built from two people's records. The other six have tests it
-- has never seen (urine microscopy, RDW/MPV, sleep-study AHI). Those are not
-- errors -- they are the codebook being incomplete, and they must not be lost
-- while it catches up.
CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY,
    document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    subject       TEXT COLLATE NOCASE    NOT NULL,   -- denormalised: every query filters on it
    segment       TEXT COLLATE NOCASE,               -- panel: Glucose, KFT, LFT, CBC, ...
    analyte       TEXT COLLATE NOCASE,               -- canonical name; NULL = not in codebook yet
    printed_name  TEXT COLLATE NOCASE    NOT NULL,   -- what the lab actually called it
    section       TEXT COLLATE NOCASE,               -- the report section it sat under
    effective     TEXT,               -- ISO collection date
    value_num     REAL,               -- exactly one of value_num / value_text
    value_text    TEXT,
    raw_value     TEXT    NOT NULL,   -- verbatim, as printed. Never lose this.
    unit          TEXT COLLATE NOCASE,
    ref_low       REAL,               -- the LAB's printed range (provenance only)
    ref_high      REAL,
    source_quality TEXT NOT NULL DEFAULT 'text',  -- text | image | handwritten
    status        TEXT COLLATE NOCASE NOT NULL DEFAULT 'ok',     -- ok | review
    review_reason TEXT,                           -- JSON array; NULL when ok
    -- SECTION IS PART OF THE IDENTITY, not decoration. Without it, two rows that
    -- differ only by the section they sat under collide, and insert_observations()
    -- uses INSERT OR IGNORE -- so the second one is not rejected, it is SILENTLY
    -- DROPPED.
    --
    -- A follicular scan lists a "Follicle" of "18" in the right ovary and a
    -- "Follicle" of "18" in the left ovary, on one date. Those are two follicles.
    -- Without `section` in this key they become one, and nothing says so.
    --
    -- The same hole exists for labs and is only hidden by luck: "RBC" under
    -- URINE ROUTINE and "RBC" under COMPLETE BLOOD COUNT survive today because
    -- their values happen to differ. Print the same value on both and one
    -- disappears. This is trap #2 in CLAUDE.md, in the schema rather than in
    -- normalize.py.
    UNIQUE (subject, printed_name, section, effective, raw_value)
);

CREATE INDEX IF NOT EXISTS idx_obs_subject_analyte
    ON observations (subject, analyte, effective);
CREATE INDEX IF NOT EXISTS idx_obs_unnamed
    ON observations (analyte, printed_name) WHERE analyte IS NULL;

-- A hospital stay. One row per discharge summary.
CREATE TABLE IF NOT EXISTS encounters (
    id             INTEGER PRIMARY KEY,
    document_id    INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    subject        TEXT COLLATE NOCASE NOT NULL,
    hospital       TEXT COLLATE NOCASE,
    admitted       TEXT,          -- ISO
    discharged     TEXT,          -- ISO
    reason         TEXT,          -- presenting complaint, as written
    diagnoses      TEXT,          -- JSON array, verbatim
    procedures     TEXT,          -- JSON array, verbatim
    follow_up      TEXT,          -- the instruction, verbatim
    follow_up_date TEXT,          -- ISO, when stated or derivable
    UNIQUE (subject, admitted, hospital)
);

-- Medication as an EVENT LOG, not a table of truth.
--
-- A prescription records what was STARTED. Nothing records what was STOPPED. So
-- "what is this person taking right now" cannot be derived from documents alone -- it
-- needs a human decision at each change, and that decision is the state. The
-- extractor proposes; `entered_by` records who actually decided.
CREATE TABLE IF NOT EXISTS medication_events (
    id           INTEGER PRIMARY KEY,
    document_id  INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    subject      TEXT COLLATE NOCASE NOT NULL,
    drug         TEXT COLLATE NOCASE NOT NULL,   -- as printed. Indian brand names are common.
    generic      TEXT COLLATE NOCASE,            -- molecule(s), once mapped. NULL = not yet.
    strength     TEXT,
    form         TEXT COLLATE NOCASE,            -- tablet / capsule / syrup / injection
    dose         TEXT,
    frequency    TEXT,            -- as written: "1-0-1", "BD", "once daily"
    duration     TEXT,
    event        TEXT COLLATE NOCASE NOT NULL,   -- prescribed | stopped | changed | continued
    effective    TEXT,            -- ISO
    raw_text     TEXT NOT NULL,   -- the line as printed. Never lose it.
    entered_by   TEXT COLLATE NOCASE NOT NULL DEFAULT 'extractor',  -- extractor | human
    status       TEXT COLLATE NOCASE NOT NULL DEFAULT 'review',     -- review | ok
    review_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_med_subject ON medication_events (subject, effective);

-- Document-level problems only: a report whose printed patient contradicts the
-- folder it came from. Result-level review lives on the observation itself.
CREATE TABLE IF NOT EXISTS review_queue (
    id            INTEGER PRIMARY KEY,
    document_id   INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    subject       TEXT COLLATE NOCASE    NOT NULL,
    kind          TEXT COLLATE NOCASE    NOT NULL,   -- patient_mismatch
    printed_name  TEXT COLLATE NOCASE,
    raw_value     TEXT,
    reasons       TEXT    NOT NULL,   -- JSON array of why
    resolved      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_review_open
    ON review_queue (resolved, subject);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def upsert_document(con: sqlite3.Connection, **fields: Any) -> int:
    """Insert a document, or return the id of the one already recorded.

    The upsert deliberately does NOT change `doc_type`, and that silence is a trap
    worth naming. Two routers can claim the same file -- is_lab() calls everything
    tagged Medical/Reports a lab, and the page-1 classifier calls an echo in that
    folder radiology -- so the second extractor to run gets back a row that is still
    labelled with the FIRST one's type. The database then quietly disagrees with
    itself about what the document is.

    Left silent, that let a radiology ingest write into rows labelled 'lab' and
    delete 448 lab observations. So a type change is now reported. It is still not
    APPLIED: which extractor owns a document is a human's call (the classifier is
    not reliably right either -- it called a health-checkup panel "radiology"), and
    the caller is the one that must refuse.
    """
    existing = con.execute(
        "SELECT doc_type FROM documents WHERE source_path = ?",
        (fields["source_path"],),
    ).fetchone()
    if existing and existing["doc_type"] != fields["doc_type"]:
        logger.warning(
            f"{fields['source_path']}: recorded as {existing['doc_type']!r}, now being "
            f"ingested as {fields['doc_type']!r}. The doc_type is NOT being changed. "
            f"Two extractors claim this document; a human has to say which owns it."
        )

    cur = con.execute(
        """INSERT INTO documents (paperless_id, subject, source_path, doc_type,
                                  doc_date, lab, model, text_layer)
           VALUES (:paperless_id, :subject, :source_path, :doc_type,
                   :doc_date, :lab, :model, :text_layer)
           ON CONFLICT(source_path) DO UPDATE SET
               model = excluded.model,
               extracted_at = datetime('now')
           RETURNING id""",
        {
            "paperless_id": fields.get("paperless_id"),
            "subject": fields["subject"],
            "source_path": fields["source_path"],
            "doc_type": fields["doc_type"],
            "doc_date": fields.get("doc_date"),
            "lab": fields.get("lab"),
            "model": fields.get("model"),
            "text_layer": int(bool(fields.get("text_layer"))),
        },
    )
    return int(cur.fetchone()["id"])


def insert_observations(
    con: sqlite3.Connection, document_id: int, rows: Iterable[dict[str, Any]]
) -> int:
    """Insert observations, ignoring exact re-inserts of the same value."""
    n = 0
    for r in rows:
        cur = con.execute(
            """INSERT OR IGNORE INTO observations
                 (document_id, subject, segment, analyte, printed_name, section,
                  effective, value_num, value_text, raw_value, unit,
                  ref_low, ref_high, source_quality, status, review_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id,
                r["subject"],
                r.get("segment"),
                r.get("analyte"),
                r["printed_name"],
                r.get("section"),
                r.get("effective"),
                r.get("value_num"),
                r.get("value_text"),
                r["raw_value"],
                r.get("unit"),
                r.get("ref_low"),
                r.get("ref_high"),
                r.get("source_quality", "text"),
                r.get("status", "ok"),
                r.get("review_reason"),
            ),
        )
        n += cur.rowcount
    return n


def reclassify(con: sqlite3.Connection, resolver) -> int:
    """Re-resolve the analyte for observations we could not name before.

    Free and offline: it re-runs the name matcher over `printed_name`, so adding
    an alias redeems every past value that alias covers without touching the LLM.
    This is the whole reason unnamed results are kept as observations rather than
    diverted to a lossy queue.
    """
    rows = con.execute(
        "SELECT id, printed_name, section FROM observations WHERE analyte IS NULL"
    ).fetchall()
    fixed = 0
    for r in rows:
        analyte, segment = resolver(r["printed_name"], r["section"] or "")
        if analyte:
            con.execute(
                """UPDATE observations
                   SET analyte = ?, segment = ?, status = 'ok', review_reason = NULL
                   WHERE id = ?""",
                (analyte, segment, r["id"]),
            )
            fixed += 1
    con.commit()
    return fixed


def queue_review(
    con: sqlite3.Connection, document_id: Optional[int], rows: Iterable[dict[str, Any]]
) -> int:
    n = 0
    for r in rows:
        con.execute(
            """INSERT INTO review_queue
                 (document_id, subject, kind, printed_name, raw_value, reasons)
               VALUES (?,?,?,?,?,?)""",
            (
                document_id,
                r["subject"],
                r["kind"],
                r.get("printed_name"),
                r.get("raw_value"),
                r["reasons"],
            ),
        )
        n += 1
    return n


def latest(con: sqlite3.Connection, subject: str, analyte: str) -> Optional[sqlite3.Row]:
    """The question this whole project exists to answer."""
    return con.execute(
        """SELECT * FROM observations
           WHERE subject = ? AND analyte = ? AND effective IS NOT NULL
           ORDER BY effective DESC LIMIT 1""",
        (subject, analyte),
    ).fetchone()

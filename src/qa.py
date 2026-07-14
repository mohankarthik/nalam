"""Read-only Q&A over health.db, for the Telegram bot (and, later, an MCP server).

See docs/telegram_qa.md for the design. Two rules carried over from the rest of
nalam, not new ones invented for this file:

* **The subject of a query is not guessed.** The person a question is about is
  resolved deterministically, by scanning the question for one of the aliases
  in data/people.json (src.people.resolve), BEFORE the LLM ever sees the
  question. An unresolved or ambiguous name gets a clarifying question back,
  never a guess -- the same rule that governs which patient a document is
  filed against.

* **Citations are computed by code, not asked of the model.** Every tool call
  below returns a `document_id` on each row it touches; `answer_question`
  collects all of them and appends the Paperless viewer link for each,
  deterministically, after the model's answer. The model is never trusted to
  remember to cite its source, the same way it is never trusted to convert a
  unit -- verbatim/deterministic code does the parts that must not be wrong.

Tool functions do no LLM calls and are pure lookups over health.db, so they are
unit-tested offline (tests/test_qa.py) the same way src/meds.py is.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import sqlite3
from typing import Any, Callable, Optional

import litellm

from src import db, meds
from src.constants import PAPERLESS_URL, SETTINGS, STATE_DIR
from src.llm import FALLBACK_MODEL, PRIMARY_MODEL, configure_api_keys
from src.people import Person, load_people

logger = logging.getLogger(__name__)

VIEWER_URL = str(SETTINGS.get("paperless_viewer_url") or PAPERLESS_URL).rstrip("/")

QA_LOG = os.path.join(STATE_DIR, "qa_log.jsonl")

MAX_TOOL_ROUNDS = 4
OBSERVATION_LIMIT = 10
ENCOUNTER_LIMIT = 5


def doc_link(con: sqlite3.Connection, document_id: Optional[int]) -> Optional[str]:
    """The URL a human opens to see the scan behind a value. None if we have no
    Paperless id for it (a document not yet uploaded, or a human decision with
    no source document at all)."""
    if document_id is None:
        return None
    row = con.execute("SELECT paperless_id FROM documents WHERE id = ?", (document_id,)).fetchone()
    if not row or row["paperless_id"] is None:
        return None
    return f"{VIEWER_URL}/documents/{row['paperless_id']}/details"


def extract_person(question: str) -> tuple[Optional[Person], str]:
    """Find the ONE person a question is about, by scanning for an alias.

    Deterministic word matching, not an LLM guess -- the same rule as document
    filing: an unresolved or ambiguous subject is refused, never inferred.
    Returns (person, "") on a clean match, or (None, message-to-send-back)
    otherwise.
    """
    # Letters only -- "dad's" must yield the token "dad", not "dad's", or a
    # possessive silently fails to match the alias it is possessive OF.
    tokens = set(re.findall(r"[a-z]+", question.lower()))
    people = load_people()
    matched: dict[str, Person] = {}
    for person in people.values():
        names = {
            person.correspondent.lower(),
            person.folder.lower(),
            *(a.lower() for a in person.aliases),
        }
        for name in names:
            if set(name.split()) <= tokens:
                matched[person.correspondent] = person
                break

    if len(matched) == 1:
        return next(iter(matched.values())), ""
    if not matched:
        everyone = sorted({p.correspondent for p in people.values()})
        return None, f"Who is this about? Say one of: {', '.join(everyone)}."
    return None, f"Which one -- {', '.join(sorted(matched))}?"


# --- tool functions: pure lookups, no LLM, always carry a document_id --------


def list_medications(con: sqlite3.Connection, subject: str) -> list[dict[str, Any]]:
    """The believed-current medicine list. Always names its own uncertainty:
    `confirmed` and `stale` are surfaced on every row, not just when asked --
    silence here would train a reader to trust a list this project documents
    as a belief, not a fact (src/meds.py)."""
    active = meds.current(con, subject)
    return [
        {
            "medicine": m.display,
            "started": m.started or "unknown",
            "strength": m.strength or "unknown",
            "frequency": m.frequency or "unknown",
            "confirmed": m.status == "ok",
            "stale": bool(m.effective and m.effective < meds.STALE_BEFORE),
            "document_id": m.document_id,
        }
        for m in sorted(active, key=lambda x: x.display.lower())
    ]


def get_observations(
    con: sqlite3.Connection,
    subject: str,
    analyte: Optional[str] = None,
    since: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Lab results, most recent first. `analyte` matches the canonical name or
    the name as printed on the report; `since` is an ISO date lower bound."""
    sql = "SELECT * FROM observations WHERE subject = ? AND effective IS NOT NULL"
    params: list[Any] = [subject]
    if analyte:
        sql += " AND (LOWER(IFNULL(analyte,'')) = LOWER(?) OR LOWER(printed_name) LIKE LOWER(?))"
        params += [analyte, f"%{analyte}%"]
    if since:
        sql += " AND effective >= ?"
        params.append(since)
    sql += " ORDER BY effective DESC LIMIT ?"
    params.append(OBSERVATION_LIMIT)

    rows = con.execute(sql, params).fetchall()
    return [
        {
            "analyte": r["analyte"] or r["printed_name"],
            "value": r["value_num"] if r["value_num"] is not None else r["value_text"],
            "unit": r["unit"],
            "date": r["effective"],
            "ref_low": r["ref_low"],
            "ref_high": r["ref_high"],
            "source_quality": r["source_quality"],
            "trusted": r["status"] == "ok",
            "document_id": r["document_id"],
        }
        for r in rows
    ]


def get_encounters(
    con: sqlite3.Connection, subject: str, since: Optional[str] = None
) -> list[dict[str, Any]]:
    """Hospital stays / discharges, most recent first."""
    sql = "SELECT * FROM encounters WHERE subject = ?"
    params: list[Any] = [subject]
    if since:
        sql += " AND (admitted IS NULL OR admitted >= ?)"
        params.append(since)
    sql += " ORDER BY admitted DESC LIMIT ?"
    params.append(ENCOUNTER_LIMIT)

    rows = con.execute(sql, params).fetchall()
    return [
        {
            "hospital": r["hospital"],
            "admitted": r["admitted"],
            "discharged": r["discharged"],
            "reason": r["reason"],
            "diagnoses": json.loads(r["diagnoses"] or "[]"),
            "follow_up": r["follow_up"],
            "follow_up_date": r["follow_up_date"],
            "document_id": r["document_id"],
        }
        for r in rows
    ]


def get_medication_history(
    con: sqlite3.Connection, subject: str, drug: Optional[str] = None
) -> list[dict[str, Any]]:
    """Every medication EVENT ever recorded, unfiltered -- unlike
    list_medications, this does not hide expired courses, children's
    prescriptions, or one-offs. This is the tool for "when did we last give
    her cetirizine" or "what was she on for her hand-foot-and-mouth" -- and,
    critically, for anything list_medications hid because it already ended
    (a short antibiotic course, a child's prescription): a "no current
    medication" answer from list_medications does NOT mean nothing was ever
    given, and this tool is how that distinction gets checked before saying
    "no record"."""
    rows = meds.history(con, subject=subject, drug=drug)
    return [
        {
            "medicine": meds.display_name(r["drug"], r["generic"]),
            "event": r["event"],
            "date": r["effective"],
            "confirmed": r["status"] == "ok",
            "diagnoses": r["diagnoses"],
            "reason": r["reason"],
            "document_id": r["document_id"],
        }
        for r in rows
    ]


def get_medications_for_condition(
    con: sqlite3.Connection, subject: str, condition: str
) -> list[dict[str, Any]]:
    """What was prescribed for a given diagnosis or complaint, e.g. "hand foot
    and mouth" or "cold" -- matches against the encounter's recorded diagnoses
    AND its free-text reason. The colloquial term you were asked is expanded
    against clinical shorthand too (data/conditions.json: "cold" also matches
    "URTI"/"AURTI").

    That mapping is not exhaustive. Rather than trust the model to remember a
    prompt rule and call get_medication_history itself when this comes back
    empty, this tool does it FOR the model: an empty condition match falls
    back to the full, unfiltered history here, so one tool call always has
    enough to reason over instead of depending on prompt compliance for the
    one thing this feature exists to fix ("no record" for a drug that was, in
    fact, given)."""
    episodes = meds.for_condition(con, condition, subject=subject)
    if not episodes:
        return get_medication_history(con, subject)
    return [
        {
            "date": e["date"],
            "diagnoses": json.loads(e["diagnoses"] or "[]"),
            "reason": e["reason"],
            "follow_up": e["follow_up"],
            "medications": [
                {
                    "medicine": meds.display_name(m["drug"], m["generic"]),
                    "strength": m["strength"],
                    "frequency": m["frequency"],
                    "duration": m["duration"],
                    "confirmed": m["status"] == "ok",
                }
                for m in e["medications"]
            ],
            "document_id": e["document_id"],
        }
        for e in episodes
    ]


# --- LLM orchestration -------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_medications",
            "description": "The medicines this person is believed to currently be on.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_observations",
            "description": "Lab results for this person, most recent first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "analyte": {
                        "type": "string",
                        "description": "Test name, e.g. 'HbA1c' or 'creatinine'. Omit for "
                        "everything.",
                    },
                    "since": {
                        "type": "string",
                        "description": "ISO date (YYYY-MM-DD) lower bound. Omit for no lower "
                        "bound.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_encounters",
            "description": "Hospital admissions / discharge summaries for this person.",
            "parameters": {
                "type": "object",
                "properties": {
                    "since": {
                        "type": "string",
                        "description": "ISO date (YYYY-MM-DD) lower bound. Omit for no lower "
                        "bound.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_medication_history",
            "description": "Every medication event ever recorded for this person, including "
            "expired courses, children's prescriptions and one-offs that "
            "list_medications hides because they already ended. ALWAYS call this "
            "before saying there is no record of a medicine, an antibiotic course, "
            "or anything similar -- 'not currently on it' and 'never had it' are "
            "different facts, and only this tool can tell them apart.",
            "parameters": {
                "type": "object",
                "properties": {
                    "drug": {
                        "type": "string",
                        "description": "Brand or molecule name to filter by, e.g. "
                        "'cetirizine'. Omit for the whole history.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_medications_for_condition",
            "description": "What was prescribed for a diagnosis or complaint, e.g. 'cold', "
            "'fever', 'hand foot and mouth'. Use this for any 'what did they get "
            "for X' question -- it searches the encounter's diagnoses AND its "
            "free-text reason, so an informal complaint that never got a named "
            "diagnosis (most colds) is still found.",
            "parameters": {
                "type": "object",
                "properties": {
                    "condition": {
                        "type": "string",
                        "description": "The diagnosis or complaint to search for.",
                    },
                },
                "required": ["condition"],
            },
        },
    },
]

SYSTEM_PROMPT = """You answer questions about {person}'s medical history, using \
ONLY the tools given to you -- never your own medical knowledge, and never a \
number or date you were not handed by a tool.

Rules:
- list_medications only shows what's believed CURRENT. Before telling anyone \
there is no record of a medicine, a course, or "what was given for X", you \
MUST also check get_medication_history -- an expired course or a child's \
prescription won't show up in list_medications, but it is still there.
- get_medications_for_condition expands common colloquial/clinical pairs \
(e.g. "cold" also matches "URTI"/"AURTI") and, if nothing matches even after \
that, falls back to the full history itself -- so an empty-looking result \
from it can still hand you real events. Read each one's own \
diagnoses/reason yourself: you ARE allowed to recognize that a coded \
diagnosis you were HANDED BY A TOOL (e.g. "AURTI") matches the \
plain-language condition asked about (e.g. "cold") -- that is reading \
what's on record, not guessing a value that isn't there.
- Only after checking get_medication_history (directly, or via \
get_medications_for_condition's fallback) and finding nothing relevant \
should you say there is no trustworthy record of it. Never fill a real gap \
with medical knowledge or a guess.
- Repeat every date and caveat a tool gives you VERBATIM. If a medicine is \
`"confirmed": false`, say it is unconfirmed. If `"stale": true`, say nobody has \
confirmed it recently and it may have stopped -- do not silently drop this.
- Do not mention document ids or write links yourself; those are appended \
automatically after your answer.
- Keep the answer short and in plain text (this is a Telegram message, no \
Markdown)."""


def _dispatch(con: sqlite3.Connection, subject: str) -> dict[str, Callable[..., Any]]:
    return {
        "list_medications": lambda: list_medications(con, subject),
        "get_observations": lambda analyte=None, since=None: get_observations(
            con, subject, analyte, since
        ),
        "get_encounters": lambda since=None: get_encounters(con, subject, since),
        "get_medication_history": lambda drug=None: get_medication_history(con, subject, drug),
        "get_medications_for_condition": lambda condition: get_medications_for_condition(
            con, subject, condition
        ),
    }


def _log_exchange(question: str, subject: str, document_ids: set[int], answer: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    entry = {
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "subject": subject,
        "question": question,
        "document_ids": sorted(document_ids),
        "answer": answer,
    }
    with open(QA_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _run_loop(
    model: str, messages: list[dict[str, Any]], dispatch: dict[str, Callable[..., Any]]
) -> str:
    for _ in range(MAX_TOOL_ROUNDS):
        resp = litellm.completion(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            # litellm.completion() unconditionally imports its MCP-gateway
            # handler whenever `tools` is set, and that import chain drags in
            # fastapi/orjson/etc -- proxy-only deps nalam never installs. We
            # use plain OpenAI-style function calling, never the MCP gateway,
            # so skip the import entirely rather than installing a proxy's
            # worth of dependencies to satisfy an import nothing here needs.
            _skip_mcp_handler=True,
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content or ""

        messages.append(msg.model_dump())
        for call in msg.tool_calls:
            fn = dispatch.get(call.function.name)
            args = json.loads(call.function.arguments or "{}")
            result = fn(**args) if fn else {"error": f"no such tool: {call.function.name}"}
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, default=str),
                }
            )
    return "That took too many steps to answer -- try asking something narrower."


def answer_question(question: str) -> str:
    """The whole Q&A path: resolve the subject, run the tool-calling loop,
    append citations, log, return plain text ready to send back."""
    person, clarify = extract_person(question)
    if person is None:
        return clarify

    con = db.connect()
    dispatch = _dispatch(con, person.correspondent)

    # Every tool call's result flows through the SAME wrapped functions, so this
    # closure sees every document_id the model actually looked at -- not what it
    # claims to have looked at.
    seen_docs: set[int] = set()
    wrapped = {name: _collecting(fn, seen_docs) for name, fn in dispatch.items()}

    configure_api_keys()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(person=person.correspondent)},
        {"role": "user", "content": question},
    ]

    try:
        answer = _run_loop(PRIMARY_MODEL, messages, wrapped)
    except Exception as e:
        logger.warning(f"{PRIMARY_MODEL} failed ({e}); falling back to {FALLBACK_MODEL}")
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT.format(person=person.correspondent)},
            {"role": "user", "content": question},
        ]
        answer = _run_loop(FALLBACK_MODEL, messages, wrapped)

    links = sorted({link for d in seen_docs if (link := doc_link(con, d))})
    missing = seen_docs - {d for d in seen_docs if doc_link(con, d)}
    if missing:
        logger.warning(f"qa: document(s) {sorted(missing)} have no paperless_id -- link omitted")
    if links:
        answer = answer.rstrip() + "\n\nSource(s):\n" + "\n".join(links)

    _log_exchange(question, person.correspondent, seen_docs, answer)
    return answer


def _collecting(fn: Callable[..., Any], seen_docs: set[int]) -> Callable[..., Any]:
    """Wrap a tool function so every document_id it returns is recorded, whether
    or not the model ends up mentioning it in the answer."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        rows = fn(*args, **kwargs)
        for row in rows:
            if row.get("document_id") is not None:
                seen_docs.add(row["document_id"])
        return rows

    return wrapper

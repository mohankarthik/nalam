# Telegram Q&A — HLD

Status: **shipped and deployed** (2026-07-14). Extends the existing ingest bot
(`plugins/telegram_bot/bot.py`) with a read-only question-answering path over `health.db`.

## Goal

A family member DMs the bot a question — "what's dad on for BP", "mom's latest HbA1c" — and
gets an answer grounded in `health.db`, with the source's Paperless link, never invented.

## Non-goals

- No MCP server here (that's Phase 3 in `health_records/PLAN.md`; see "Relationship to the MCP
  server" below).
- No document search / ingest changes — ingest path in `bot.py` is untouched.
- No rate limiting for now (explicit call — revisit if group-chat cost becomes real).

## Architecture

```
Telegram message (plain text, no attachment, not /start|/help)
        │
        ▼
plugins/telegram_bot/bot.py :: process_message()
        │  (existing allowed_chat_id / allowed_users check, unchanged)
        ▼
src/qa.py :: answer_question(question, user_id) -> str
        │
        ├─ 1. resolve person via src.people.resolve()
        │       not found / ambiguous → return a clarifying question, never guess
        │       (same rule as document filing: the subject of a query is not guessed)
        │
        ├─ 2. litellm tool-calling loop (src/llm.py, Gemini flash primary / Claude fallback)
        │       tools offered: list_medications, get_observations, get_encounters,
        │       get_medication_history, get_medications_for_condition
        │       system prompt: verbatim discipline — model may only state what a tool
        │       returned, must repeat dates/caveats verbatim, must say "no trustworthy
        │       value" if a tool returns nothing / quarantined data
        │
        ├─ 3. every tool result carries: value, date, confidence/trust flag, document_id
        │
        └─ 4. reply text always appends the Paperless viewer link(s) for every document
                cited — see "Citations" below
        ▼
send_message() (existing, plain text, unchanged)
```

## `src/qa.py` — tool functions

Deterministic, no LLM inside them, unit-testable offline against a fixture db (fits the
existing "pytest is free and offline" rule — LLM-in-the-loop stays out of the regression
suite, same treatment the golden test gives Gemini elsewhere).

- `list_medications(person, as_of=today)`
  Wraps `meds.current()`. **Always** surfaces the `[unconfirmed]` / `stale` caveat already
  printed by `run_meds.py` — proactively, on every answer, not just when asked. Silence here
  would train people to trust a list the project itself documents as unreliable ("a
  prescription says what STARTED, nothing says what STOPPED").

- `get_observations(person, analyte=None, since=None)`
  Latest value + trend from `observations`: `effective` date, `unit`, `ref_low/high`,
  `source_quality`, `document_id`.

- `get_encounters(person, since=None)`
  Discharge summaries: `diagnoses`, `follow_up`, `follow_up_date`, `document_id`.

- `get_medication_history(person, drug=None)`
  Wraps `meds.history()` — **every** medication event ever recorded, unfiltered. Unlike
  `list_medications`, this does not hide an expired 5-day course or a child's one-off
  prescription. The system prompt requires the model to check this (or the next tool)
  before ever telling someone there is "no record" of a medicine — `list_medications`
  saying nothing is current is not the same fact as nothing was ever given, and the
  first version of this feature conflated the two (a real bug, fixed after it shipped).

- `get_medications_for_condition(person, condition)`
  Wraps `meds.for_condition()` — what was prescribed for a diagnosis/complaint, matched
  against both the encounter's coded diagnoses and its free-text reason. The query is
  expanded through `data/conditions.json` first — see "Colloquial condition matching" below.

Every row returned includes `document_id` so the caller (the LLM loop, and the citation
step) always has a path back to source.

## Colloquial condition matching (`src/conditions.py`)

"What did she get for a cold" and a discharge summary that says `AURTI` are the same fact,
worded two different ways. `meds.for_condition()` expands the query through
`data/conditions.json` (hand-curated, generic medical terms only — same shape and rule as
`data/aliases.json`: no names, no dates, nothing from anyone's actual record) before
searching, so "cold" also matches "URTI"/"AURTI"/"common cold"/etc, in both directions.

Matching is **whole-word token containment**, not raw substring search. A raw substring
version shipped first and had a real bug, caught by its own test before it reached
production: a 2-letter clinical abbreviation like `RA` (rheumatoid arthritis) is a substring
of ordinary English words — `"ra"` is inside `"rare"`, `"library"` — so a literal `in` check
silently pulled in the wrong bucket for unrelated questions. `src/conditions.py::expand()`
tokenizes both sides and requires a whole term's tokens to be a subset of the question's
tokens, which fixes it.

The mapping is not exhaustive. `get_medications_for_condition`'s tool description and the
system prompt both tell the model that an empty result is *not* the answer — it must also
call `get_medication_history` and read the raw `diagnoses`/`reason` text itself, which is
where the model's own language understanding (not a static file) closes the rest of the gap.

## Citations — Drive/Paperless link on every response

Reuses the pattern already in `tools/export_review.py`:

```
{paperless_viewer_url}/documents/{paperless_id}/details
```

via the join `observations.document_id → documents.id → documents.paperless_id`
(`paperless_viewer_url` from `data/settings.json`, falling back to `paperless_url` like
`export_review.py` does).

Every reply that cites a value appends the link(s) for the document(s) it came from — this
is a hard requirement per response, not best-effort.

**Found in production, fixed the same day:** `documents.paperless_id` was NULL for all 359
rows in `health.db` — extraction reads PDFs straight from the Drive mount, never from
Paperless (see CLAUDE.md), so nothing in the pipeline had ever looked the id up. Fixed two
ways:
- `Paperless.document_id_index()` / `id_for()` (`src/paperless.py`) — the same
  `(correspondent, folded filename)` join `ocr_index()`/`ocr_for()` already used for OCR
  corroboration, now built once per `run_extract.py` pass and threaded through every
  `ingest_*()` call so new documents get a `paperless_id` at ingest time going forward.
  `db.upsert_document()`'s `ON CONFLICT` now `COALESCE`s it, so a re-run before Paperless has
  linked a document never clobbers an id a previous run already resolved.
- `tools/backfill_paperless_ids.py` — one-time backfill for the 359 pre-existing rows.
  Filled 343; left 16 `NULL`: 10 genuinely unmatched, and 6 from 3 real collisions (the same
  physical document filed under two different Drive paths — the file-level version of the
  duplicate-folder trap CLAUDE.md already warns about). A collision leaves **both** rows
  `NULL` rather than guessing which one owns the id — same rule as CLAUDE.md's trap #4 ("two
  results claiming one identity are both untrusted"). Backed up `health.db` before running;
  tested on a scratch copy and a 3-row live sample before running the rest.

## Guardrails carried over from the rest of the project

- Person not resolved → ask, don't guess ("the correspondent is the patient" extends to
  "the subject of a query is not guessed").
- Trust flags (`[unconfirmed]`, `stale`, `source_quality`) are repeated verbatim by the LLM,
  never smoothed into confident prose.
- Every exchange logged to `data/state/qa_log.jsonl` (question, tool calls, answer) — audit
  trail, matches the review-queue transparency the rest of the project already has, doubles
  as a future eval set.
- No per-user rate limit for now (explicit decision, revisit later).

## Two gaps found after shipping v1 (both fixed, both worth naming)

1. **litellm + `tools=` needs `_skip_mcp_handler=True`.** litellm 1.92's `completion()`
   unconditionally imports its MCP-gateway handler whenever `tools` is set, and that import
   chain needs `fastapi`/`orjson`/etc — proxy-only deps nalam never installs, for a gateway
   this code never uses. Passing litellm's own `_skip_mcp_handler=True` kwarg skips the
   import instead of installing a proxy's worth of dependencies to satisfy nothing.

2. **`list_medications` is not the whole medicine history.** The first version only exposed
   `list_medications`/`get_observations`/`get_encounters` as tools, so a question like "what
   was Bob Example given for a cold" got "no trustworthy record" — the drug WAS given, but it
   was a short course that `list_medications` correctly hides once it's no longer current,
   and there was no tool that could see anything `list_medications` hides. Fixed by adding
   `get_medication_history`/`get_medications_for_condition` (wrapping `meds.history()` /
   `meds.for_condition()`, which already existed for `run_meds.py --history`/`--for`) and a
   system-prompt rule that "not currently on it" and "never had it" are different facts, and
   the model must check history before ever saying the latter.

## Long polling (`plugins/telegram_bot/bot.py`)

`getUpdates` now passes Telegram's own `timeout=45` (`LONG_POLL_SECONDS`), so Telegram holds
the connection open server-side until a message arrives instead of returning empty
instantly — a message sent mid-tick gets answered in seconds rather than up to a minute
later on the next cron tick. No architecture change (still cron-polled, no daemon, no
exposed port) and no CPU cost while blocked (the process just parks on a socket read).

Since a tick can now legitimately run close to the full 60s interval (a long poll plus an
LLM round-trip), `run_telegram_bot.py` takes a non-blocking `flock` before running and skips
the tick outright if the previous one is still holding it, rather than risking two instances
racing on `telegram_bot_state.json`'s offset.

## Relationship to the MCP server (Phase 3, not this)

Same engine, different transport:

- **MCP server** (later): stdio/JSON-RPC process, driven by an MCP client (Claude Desktop,
  Claude Code) — a technical user, interactive multi-turn session, no polling latency,
  arbitrary tool chaining across a session.
- **Telegram bot** (this doc): cron-polled (`getUpdates`, ≤1 min latency), non-technical
  family members on a phone, one question → one answer, the bot owns the whole loop itself
  (person-resolution guard, caveat injection, chat auth) rather than handing tools to an
  external client.

`src/qa.py`'s tool functions are built standalone, not baked into `bot.py`, specifically so
the Phase 3 MCP server can re-expose the same functions as MCP tool definitions later with
zero duplicated logic — this work is the reusable core, MCP is a thin second frontend on
top of it.

## Testing

- `src/qa.py`/`src/conditions.py`/`src/meds.py` tool functions: normal offline pytest, fixture
  db, deterministic (`tests/test_qa.py`, `tests/test_conditions.py`, `tests/test_meds.py`).
- LLM tool-calling loop: manual smoke test only, not in the regression suite (flaky/costly,
  same as Gemini elsewhere).

## Deploying a new data file: don't forget the Dockerfile

`Dockerfile` copies medical-knowledge data files into the image by an **explicit list**
(`COPY data/aliases.json data/units.json ... ./data/`), not a directory copy — adding
`data/conditions.json` required a Dockerfile edit too, and the first redeploy after adding
it shipped without the file (silent until something called `load_conditions()`). Any new
committed `data/*.json` needs adding to that `COPY` line, or it never reaches the container.

## Open items

- Rate limiting: deferred, revisit if cost or spam becomes real.
- If `getFile` size caps or multi-document citations ever need more than a plain-text list of
  links, revisit reply formatting then — not needed for v1.
- 3 document-id collisions in `health.db` left `NULL` by the backfill (see "Citations" above)
  — a human decision, not urgent.

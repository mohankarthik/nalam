# Telegram Q&A — HLD

Status: proposed, not built. Extends the existing ingest bot (`plugins/telegram_bot/bot.py`)
with a read-only question-answering path over `health.db`. Reviewed 2026-07-14.

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
        │       tools offered: list_medications, get_observations, get_encounters
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

Every row returned includes `document_id` so the caller (the LLM loop, and the citation
step) always has a path back to source.

## Citations — Drive/Paperless link on every response

Reuses the pattern already in `tools/export_review.py:100`:

```
{paperless_viewer_url}/documents/{paperless_id}/details
```

via the join `observations.document_id → documents.id → documents.paperless_id`
(`paperless_viewer_url` from `data/settings.json`, falling back to `paperless_url` like
`export_review.py` does).

Every reply that cites a value appends the link(s) for the document(s) it came from — this
is a hard requirement per response, not best-effort. If a tool result has no `document_id`
(shouldn't happen — flag as a bug if it does), the reply says so rather than omitting the
link silently.

## Guardrails carried over from the rest of the project

- Person not resolved → ask, don't guess ("the correspondent is the patient" extends to
  "the subject of a query is not guessed").
- Trust flags (`[unconfirmed]`, `stale`, `source_quality`) are repeated verbatim by the LLM,
  never smoothed into confident prose.
- Every exchange logged to `data/state/qa_log.jsonl` (question, tool calls, answer) — audit
  trail, matches the review-queue transparency the rest of the project already has, doubles
  as a future eval set.
- No per-user rate limit for now (explicit decision, revisit later).

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
the Phase 3 MCP server can re-expose the same three functions as MCP tool definitions later
with zero duplicated logic — this work is the reusable core, MCP is a thin second frontend
on top of it.

## Testing

- `src/qa.py` tool functions: normal offline pytest, fixture db, deterministic.
- LLM tool-calling loop: manual smoke test only, not in the regression suite (flaky/costly,
  same as Gemini elsewhere).

## Open items

- Rate limiting: deferred, revisit if cost or spam becomes real.
- If `getFile` size caps or multi-document citations ever need more than a plain-text list of
  links, revisit reply formatting then — not needed for v1.

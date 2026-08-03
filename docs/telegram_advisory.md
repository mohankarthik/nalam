# Telegram Advisory / Appointment-Prep — HLD

Status: **built** (2026-08-03). Adds a second, clearly-labelled question-answering path to
the Telegram bot: grounded *synthesis* for appointment prep, alongside the existing
short-factual path (`docs/telegram_qa.md`), which is unchanged.

## Goal

A family member asks the bot to gather several record types for one person and help them
**prepare for a doctor's visit** — e.g. "fetch Ravi's HbA1c, CBC, MDS and urology
(ASU/SPC) records and formulate questions to ask the urologist about his treatment options
based on the recent ASU." The reply is a set of **questions to ask the doctor**, suggested
**reading / things to look up**, and brief **plain-language explanations** of findings —
all reasoned strictly over what the records contain, never invented, and clearly marked as
reasoning rather than a retrieved record.

## Why a separate path (not a relaxed factual one)

The factual path (`src/qa.py :: answer_question`) is deliberately forbidden from reasoning
beyond retrieved values; that grounding rule protects the golden-test guarantees and the
short-answer/citation contract. It stays **byte-for-byte unchanged**. Synthesis is a
different job with a different risk profile, so it gets its own prompt, its own budget, and
its own deterministic safety net — and it can never weaken the factual path.

## Non-goals

- No new data source. Advisory reasons over the **same** `get_*` tools; it is handed no
  parametric-knowledge values and no DB access the factual path doesn't already have.
- No MCP / Todoist / ingest changes. QA only.
- Not medical advice. The output is *appointment prep that defers to the clinician* —
  questions and framing, never a recommendation, dose, or decision.

## Routing — explicit `/advise` command

```
/advise <person> <what to prepare for>
    e.g.  /advise Ravi prepare for the cardiology follow-up
```

Deterministic and unambiguous: only this command reaches the advisory path; a bare text
message still goes to the factual path exactly as before. No LLM intent classifier (an
extra call and a new failure mode, against nalam's determinism ethos) and no phrase
heuristic (which would silently widen the factual path when it misfired). The person is
resolved from the remainder by the **existing** `extract_person()` — an unresolved or
ambiguous subject gets a clarifying question, never a guess (the correspondent-is-the-patient
rule). Handled in `plugins/telegram_bot/bot.py :: process_message` (the `@bot` suffix on the
command is tolerated, like the existing `/help`).

## The gather step — broader, but most-recent

`advise()` runs the same litellm tool-calling loop as the factual path, over the same tools,
but with a bigger budget so it can pull across topics before reasoning:

| | factual | advisory |
|---|---|---|
| tool rounds | `MAX_TOOL_ROUNDS = 4` | `ADVISORY_MAX_TOOL_ROUNDS = 8` |
| observations | `OBSERVATION_LIMIT = 10` | `ADVISORY_OBSERVATION_LIMIT = 60` |
| encounters | `ENCOUNTER_LIMIT = 5` | `ADVISORY_ENCOUNTER_LIMIT = 20` |
| radiology | `RADIOLOGY_LIMIT = 10` | `ADVISORY_RADIOLOGY_LIMIT = 20` |

Every underlying query already orders `... DESC` by date (`get_observations`,
`get_encounters`, `db.radiology_for`), so a larger cap means the **most-recent N** records,
never an arbitrary slice. `get_encounters`/`get_radiology` gained a backward-compatible
`limit=` kwarg (default = the factual constant), so the factual path is untouched;
`_advisory_dispatch()` passes the larger values. Person scoping is identical to `_dispatch`
— the subject is the correspondent, full stop.

## The synthesis prompt (`ADVISORY_SYSTEM_PROMPT`)

Distinct from `SYSTEM_PROMPT`. It tells the model to gather first, then reason ONLY over the
tool results, and to produce three short plain-text sections:

1. **Questions to ask the doctor** — each tied to a specific fetched finding.
2. **Suggested reading / things to look up** — a treatment option or test that a record
   *mentions*, framed as "read about …" / "ask whether …", never "do this".
3. **What this means** — a one-line, plain-language explanation of a finding or term that
   *appears* in the fetched records (e.g. what an ascending urethrogram is).

Hard rules in the prompt: every number, date, drug and diagnosis must come from a tool
result; no recommendations, doses, or decisions; no diagnosis stated as fact; caveats
(unconfirmed / stale meds) repeated verbatim; no links or document ids (appended by code).

## Grounding guard — the deterministic safety net

Prompt discipline is not trusted alone. Mirroring the extractor's text-layer oracle (a value
not literally in the source is quarantined), a deterministic post-check verifies the model's
output against the **fetched context**:

- Every tool payload the model was handed is captured as JSON into a *grounding corpus*
  (`_collecting_with_corpus`, a variant of the factual path's `_collecting`, which also
  keeps recording `document_id`s for the citation links).
- `_ground_check(answer, corpus)` scans the answer for **high-risk fact tokens** — ISO
  dates, 4-digit years, decimals, percentages — and redacts any whose text does not appear
  in the corpus, replacing it with `[unverified]` and reporting it. A percentage is grounded
  by its bare number (`7.1%` ↔ `7.1` in a payload).
- Bare prose integers ("3 questions", "2 tests") are deliberately left alone — redacting
  them would mangle ordinary sentences for no safety gain. Numbers and dates are fully
  checkable; prose grounding stays best-effort, on the prompt (as the task specifies).

If anything was redacted, a visible note is appended: `(!) N figure(s) removed — not found
in <person>'s records.`

## Labeling

The reply can never be mistaken for a retrieved record — the way the code already
distinguishes confirmed/unconfirmed meds and MISMATCH/UNCOVERED. It is prefixed:

```
⚕️ Appointment prep for <person> — questions, reading & plain-language notes.
Reasoning over records, not medical advice.
```

followed by the three sections, the redaction note (if any), and the same
`Source(s):` Paperless links the factual path appends (computed by code from the
`document_id`s actually fetched, never written by the model).

## Where it lives

- `src/qa.py` — `advise()`, `_advisory_dispatch()`, `ADVISORY_SYSTEM_PROMPT`,
  `_ground_check()`, `_collecting_with_corpus()`, and the advisory constants. The factual
  functions, `SYSTEM_PROMPT`, `TOOLS`, `MAX_TOOL_ROUNDS`, `_dispatch`, `_collecting` are
  unchanged; `_run_loop` and two tool functions only gained optional args with unchanged
  defaults.
- `plugins/telegram_bot/bot.py` — `/advise` routing in `process_message`, plus a `/help`
  line.
- `tests/test_qa_advisory.py` — offline, monkeypatched-LLM tests: the guard catching an
  injected ungrounded number/date, person scoping, `/advise` command routing, and proof the
  factual path is unchanged.

## Testing

`./venv/bin/python -m pytest tests/test_qa_advisory.py tests/test_qa.py` — offline and free
(the LLM loop is monkeypatched, same treatment Gemini gets in the golden test). A live smoke
needs `NALAM_GEMINI_MIN_INTERVAL=0` (billing is on).

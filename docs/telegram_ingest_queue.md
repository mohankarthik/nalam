# On-demand extraction + Paperless health monitoring — HLD

Status: **built, not yet deployed** (2026-07-14). Needs `NALAM_PAPERLESS_PUSH_URL` wired into the
container's env at deploy time, and its Uptime-Kuma monitor created.

## Goal

Today, a Telegram-filed document is filed to Drive + Paperless immediately, but extraction into
`health.db` waits for the nightly 04:30 IST cron (`run_extract.py`). This closes that gap: extract
right after filing, without blocking the bot's reply on a 10-30s+ LLM call, and without corrupting
data if Paperless is slow or down.

## Non-goals

- No change to filing itself (`bot.py`'s Drive-write + Paperless-upload path is untouched).
- No change to nightly `run_extract.py` batch logic beyond factoring out a shared per-doc function
  it now calls too — it stays the backstop for Drive-only uploads (`run_sync.py`) and anything
  on-demand missed or failed on.
- Radiology stays unextracted (no trusted extractor yet, per CLAUDE.md) — on-demand skips it same as
  nightly.

## Why not just extract inline in the Telegram tick

Extraction is an LLM call (classify + extract), 10-30s+, sometimes more under free-tier pacing.
Doing it inline would delay the bot's "✓ Filed" reply and risk the tick running past its 60s cron
interval. So: file immediately (fast), queue extraction, drain the queue on a separate cron tick.

## Architecture

```
Telegram message (PDF/photo + caption)
        │
        ▼
plugins/telegram_bot/bot.py :: process_message()   (unchanged: Drive write, Paperless upload)
        │
        │ upload succeeds
        ▼
src/extract_queue.py :: enqueue(rel, correspondent, tag, title, date, chat_id)
        │                                    data/state/pending_extract.json
        │
        │ reply "✓ Filed ... extraction queued"
        ▼
(next run_extract_queue.py cron tick, every 1 min, own non-blocking flock)
        │
        ├─ 1. check_paperless() health probe  ──▶ src/monitor.py ──▶ Uptime-Kuma push
        │       unreachable → push DOWN, leave entire queue untouched, return (retry next tick,
        │                      unlimited — an outage pages a human, never silently degrades)
        │       reachable   → push UP, continue
        │
        ├─ 2. per queued item: is this doc's OCR present in paperless.ocr_index()?
        │       not yet, queued < 30 min  → leave it, retry next tick
        │       not yet, queued >= 30 min → extract with text-layer-only oracle (Paperless
        │                                    confirmed reachable the whole wait — this is
        │                                    "Paperless stalled on this doc", not an outage).
        │                                    Log loudly. Uncorroborated results still go to
        │                                    review, never silently committed (existing rule).
        │       present                    → extract with full oracle (text layer + OCR)
        │
        ├─ 3. src/ingest.py :: ingest_document(con, doc, ocr_text, paperless_id)
        │       same routing nightly uses: is_lab() → ingest_lab
        │                                  is_discharge() → ingest_discharge
        │                                  else classify() → prescription → ingest_prescription
        │                                  else → "no extractor for this type"
        │
        ├─ 4. success → pop from queue, Telegram follow-up to chat_id with result
        │       ("2 observations committed, 0 to review" / "no extractor for insurance docs yet")
        │
        └─ 5. ingest_*() raises → retry, cap 3 attempts, then pop + Telegram-notify failure
                (never leaves a failure silently stuck forever)
        ▼
send_message() follow-up (existing, unchanged)
```

## Why the queue can never mark a document falsely "done"

Audited end to end, because a document silently skipped by both the queue AND the nightly backstop
would be the worst outcome here — worse than slow.

- **Filing time**: `bot.py` only calls `extract_queue.enqueue()` *after* `paperless.upload()`
  succeeds. If upload fails, the Drive copy stays, `sync_state` is not marked, and the existing
  6-hourly `run_sync.py` retries it by checksum — same as today, untouched by this change. No queue
  item is ever created for a document that isn't actually in Paperless yet.
- **Paperless unreachable during drain**: the whole tick's extraction pass is skipped, queue
  untouched — no item is popped, no retry budget is consumed. Retries indefinitely until Paperless
  comes back; an extended outage pages a human via the Uptime-Kuma DOWN heartbeat instead of the
  pipeline quietly degrading (extracting without an oracle, or dropping the item).
- **Paperless reachable but this doc's OCR is slow**: never a silent skip either way — under 30 min
  it just waits; at/after 30 min it extracts with the text-layer-only oracle, which is the same
  fallback `ingest_lab`/`ingest_prescription`/`ingest_discharge` already use for scanned image PDFs
  today. Uncorroborated values still land in `review`, never auto-committed — this is an existing
  trust rule (CLAUDE.md trap "nothing trusted without an independent reading"), not new behavior
  introduced by the queue.
- **`ingest_*()` itself throws** (Gemini error, bad response, etc — not a Paperless problem): retried
  up to 3 times across ticks, then popped *with* a Telegram failure notification, so the family
  member who sent the document sees it, rather than it rotting silently in a queue file.
- **Nightly `run_extract.py` stays the backstop** regardless of how any of the above resolves: it
  walks the Drive mount independently of the queue and skips only `source_path`s already present in
  `documents` — which only happens after a successful `ingest_*()` call, on-demand or nightly. A
  queue item that never got that far is still picked up by the nightly pass.

## Distinguishing "Paperless is down" from "this one document's OCR is slow"

The two failure modes look similar (both leave a document's OCR entry missing) but need opposite
handling, so they are checked separately, not conflated behind one timer:

|                                   | Paperless down | Paperless up, doc's OCR pending |
|-----------------------------------|-----------------|----------------------------------|
| Detection                        | `check_paperless()` probe fails (connection error / 5xx) | Probe succeeds; `ocr_index()` just has no entry for this doc yet |
| Response                         | Skip the **whole tick**, touch nothing | Wait, per-item, up to 30 min |
| Retry budget                     | Unlimited (outage isn't the item's fault) | Falls back to text-layer-only after the wait |
| Alerting                         | Uptime-Kuma heartbeat goes DOWN immediately | Logged when the 30-min fallback fires; not paged |

An earlier draft of this design used a single "queued ≥ 15 min → force-extract" timer that didn't
make this distinction — during a real Paperless outage it would have forced every queued document
through extraction without any independent oracle, which is precisely the "guess instead of refuse"
failure CLAUDE.md's four traps exist to prevent. Caught in review before being built.

## `src/monitor.py` — Paperless health heartbeat

New module, same shape as gajana's `src/monitor.py` (`push(url, is_up, msg)`, no-op if the env var
URL is unset, never raises — a monitoring failure must not fail the pipeline):

- `check_paperless() -> tuple[bool, str]` — a cheap reachability probe (e.g. `GET /api/` or the
  existing `ocr_index()` call wrapped in try/except), independent of whether the queue has anything
  in it.
- `push_paperless(is_up, msg)` → `NALAM_PAPERLESS_PUSH_URL` env var, its own Uptime-Kuma monitor ID
  (separate from any future "nalam pipeline" monitor), so "Paperless is down" alerts distinctly from
  "nalam's cron stopped running."
- Called every `run_extract_queue.py` tick (step 1 above), so the heartbeat is live even on ticks
  with an empty queue — Paperless going down gets caught within a minute, not only when someone
  happens to send a document.

## New files / changes

| File | Change |
|---|---|
| `src/extractor.py` | `is_lab()` / `is_discharge()` moved here from `run_extract.py` — free heuristics, no LLM |
| `src/ingest.py` | new `ingest_document(con, doc, ocr_text=None, paperless_id=None)` — shared routing (is_lab → ingest_lab, is_discharge → ingest_discharge, else classify → prescription → ingest_prescription), used by both nightly and on-demand |
| `src/extract_queue.py` | new — `load()` / `save()` / `enqueue()`, FIFO on `data/state/pending_extract.json` |
| `src/monitor.py` | new — Paperless health probe + Uptime-Kuma push, ported from gajana's `src/monitor.py` pattern |
| `run_extract_queue.py` | new cron entrypoint — health probe, drain queue, Telegram follow-ups, non-blocking flock (`extract_queue.lock`) |
| `plugins/telegram_bot/bot.py` | `process_message()` enqueues instead of "runs on next daily pass"; `send_message()` pulled out to a free function so the queue runner can reuse it without a `TelegramDocBot` instance |
| `run_extract.py` | batch loops call the shared `ingest_document()` instead of inlining routing |
| `crontab` | `* * * * * python run_extract_queue.py` |
| `Dockerfile` | add `run_extract_queue.py` to the `COPY` list |
| deploy env | `NALAM_PAPERLESS_PUSH_URL` (ansible/docker-compose.env, outside this repo) |

## Testing

- `src/extract_queue.py` (`tests/test_extract_queue.py`): load/save/enqueue roundtrip — offline,
  fixture state dir.
- `src/ingest.py::ingest_document` routing precedence (`tests/test_ingest_routing.py`):
  `is_lab`/`is_discharge`/classify dispatch — offline, fixture `Doc`s, monkeypatched `ingest_lab`/
  `ingest_discharge`/`ingest_prescription`/`classify`.
- `src/monitor.py` (`tests/test_monitor.py`): `check_paperless()` up/down branches — mocked
  `requests`, and `push()` itself (URL-merge behavior, no-op on unset URL), same shape as gajana's
  `test_monitor.py`.
- `run_extract_queue.run_once()` (`tests/test_extract_queue_runner.py`): all four rows of the
  Paperless-down-vs-slow-OCR table above, plus the ingest-failure retry-then-notify path — offline,
  Paperless/`ingest_document`/Telegram all faked, backdated `queued_at` timestamps instead of a real
  clock.
- `plugins/telegram_bot/bot.py::process_message` (`tests/test_telegram_bot.py`): filing a document
  enqueues rather than extracting inline, and the reply says "queued", not "next daily pass".
- LLM-in-the-loop (`ingest_*` actually calling Gemini): manual smoke test only, same convention as
  the rest of the project — not in the regression suite.

## Open items

- Deploy: set `NALAM_PAPERLESS_PUSH_URL` in the container env and create its Uptime-Kuma monitor.
  Until that's done, `push_paperless()` just logs and no-ops (safe, matches gajana's convention).
- Whether `run_extract_queue.py` needs its own Uptime-Kuma "pipeline alive" monitor in addition to
  the Paperless-specific one (mirrors gajana's daily-pipeline vs backup-specific push split) —
  deferred until this ships and the operational picture is clearer.

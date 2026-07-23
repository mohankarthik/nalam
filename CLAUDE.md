# CLAUDE.md

Guidance for Claude Code working in this repository.

## What nalam is

Family health record pipeline for 8 people. Scanned documents (labs, scans, consults,
prescriptions, discharge summaries, insurance) land in Google Drive; nalam files them into
Paperless-ngx, extracts structured observations into SQLite, answers questions over them, and pushes
follow-up reminders to Todoist.

Sibling service to **gajana** (personal finance, `~/gajana`). Same shape on purpose: supercronic in
a long-running container, `secrets/` bind-mount, config-driven parsing, Uptime-Kuma push, ansible
deploy. **When in doubt about how to do something here, look at how gajana does it.**

## Commands

**Always use the venv** (`./venv/bin/python`, or activate it). Like gajana, nalam has its own
`venv/`; do not `pip install` into the system interpreter.

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt   # first time

# --- Phase 0: Drive -> Paperless ---
./venv/bin/python run_sync.py --dry-run     # plan; uploads nothing
./venv/bin/python run_sync.py               # idempotent; safe to re-run
./venv/bin/python run_sync.py --report      # what Paperless refused to consume

# --- Phase 1: PDFs -> health.db ---
./venv/bin/python run_extract.py --limit 5  # extract 5 lab reports
./venv/bin/python run_extract.py            # all lab reports
./venv/bin/python run_extract.py --discharge  # discharge summaries
./venv/bin/python run_extract.py --review   # what is not trusted, and why
./venv/bin/python run_extract.py --reclassify  # re-resolve unnamed analytes (FREE, no LLM)

# --- Telegram on-demand extraction (docs/telegram_ingest_queue.md) ---
./venv/bin/python run_extract_queue.py  # drain the queue Telegram filing adds to; cron, 1 min

# --- Medicines ---
./venv/bin/python run_meds.py --list --person dad
./venv/bin/python run_meds.py --reconcile
./venv/bin/python run_meds.py --decide dad "FARONEM" stopped 2023-03-04

# --- Tests ---
./venv/bin/python -m pytest              # ~167 regressions, offline and free
./venv/bin/python -m tools.extract_golden      # regenerate golden cache (slow, costs money)
./venv/bin/python -m tools.import_master_sheet # DEPRECATED: seeded the codebook from the Sheet once; do not run (would clobber curated data/analytes.json)
./venv/bin/python -m tools.export_analytes     # codebook -> ~/nalam-analytes-review.md
```

**Gemini billing is enabled**, so set `NALAM_GEMINI_MIN_INTERVAL=0` for bulk runs — the default
7s pacing exists for the free tier (~20 req/day) and is pure dead time on a paid key. The key is
shared with gajana; a heavy nalam backfill used to starve gajana's 06:00 statement run.

**`pytest` is offline and costs nothing** — it reads cached extractions from
`tests/fixtures/extracted/`. Only regenerate that cache when the prompt or the model
changes, and when you do, **clear it first**: a cache half-built from two different
prompts is a silent correctness trap.

## The one rule that matters

**The correspondent is the patient.** A document filed against the wrong family member is the worst
failure this system can have — worse than not ingesting it at all. So:

- Each person in `data/people.json` names the **full path** to their own folder (`directory`) and a
  Paperless correspondent. The sync walks exactly those folders and nothing else — there is no shared
  root, none is assumed, and a folder belonging to no listed person is simply never looked at (not
  guessed, not aborted on). A document's identity (`documents.source_path`, the sync state key) is the
  person's people.json **key**, not the folder's on-disk name — so renaming a Drive folder never re-keys a document or re-triggers extraction. A new family member is added by editing `people.json`.
- `Paperless.correspondent_id()` defaults to `create=False`. An unexpected person name means the map
  is wrong, not that a new patient appeared.
- Nothing infers a patient from document *content* while the folder path is authoritative.
- `validator.check_document()` cross-checks the name **printed on the report** against the folder it
  came from. Mismatch → the whole document is quarantined. This catches a misfiled scan.

## The four traps (all of them cost real bugs; all have regression tests)

The golden test caught every one of these. **Gemini transcribed correctly every single time** — each
bug was in *our* post-processing. That is what the verbatim-copy prompt buys you: when something is
wrong, you know where to look.

1. **A qualifier is identity, not noise.** `serum`, `direct`, `total`, `fasting` must NEVER go in
   `normalize.NOISE`. Stripping `direct` once reduced `Direct Bilirubin` to `{bilirubin}`, which then
   also matched *Total* and *Indirect* Bilirubin — putting one test's value under another's name.

2. **An MHC report is five examinations stapled together**, not a lab report. Test names collide
   across sections and mean different things: `RBC` under `URINE ROUTINE` is a dipstick finding
   (`Negative`), under `COMPLETE BLOOD COUNT` it's a cell count (`5.83`). `Impression` exists in both
   the eye exam and the abdominal USG. The model reports the **section heading** and
   `normalize._domain_compatible()` refuses cross-section matches.

3. **Units are never assumed.** A 2021 report printed Vitamin D as `31.29 nmol/L`; the codebook keeps
   it in ng/mL with a 30–80 range. Unconverted, it looks normal against a scale it doesn't belong to.
   `src/units.py` converts to the codebook's unit, and an **unknown unit refuses** rather than
   guessing. Guessing is how a value ends up 10× wrong while looking plausible.

4. **Two results claiming one analyte are both untrusted.** If a report yields two `HbA1c` rows, one
   is silently overwriting the other and we can't know which is real. `normalize.resolve()` refuses
   both and sends them to review.

The through-line: **refusing is safe, guessing is not.** Every ambiguity in this codebase resolves to
the review queue, never to a coin flip.

## Ground truth (`tests/fixtures/golden.json`)

824 values the user typed by hand from the original reports, 2010–2025; 481 have a source PDF still
in Drive. This is the only thing that can tell us whether the LLM is lying, and most projects like
this have nothing equivalent.

`tests/test_golden.py` distinguishes two failures, and **they are not the same severity**:

- **MISMATCH** — we produced a different value than the human. A wrong number in a medical record.
  Fails the build.
- **UNCOVERED** — the human has a value we didn't produce. A missing alias, or a value quarantined
  for good reason. Reported, does not fail. *A gap is not a lie*, and conflating the two would train
  us to paper over real errors.

Adjudicated disagreements — cases where the DOCUMENT is right and the SHEET is wrong, each proven
against the PDF's own text layer — live in `tests/fixtures/sheet_errors.json` (gitignored: they
contain real values). The golden test excludes them but still prints them, so they stay visible
facts rather than folklore.

## Nothing personal in this repository

Real names, real paths, real values, real health records: **none of it is committed.** The code holds
the shape of the problem; the config holds the family.

| Committed (generic) | Gitignored (personal) |
|---|---|
| `data/settings.example.json`, `data/people.example.json` | `data/settings.json`, `data/people.json` |
| `data/aliases.json`, `data/units.json`, `data/drugs.json`, `data/analytes_extra.json` — medical knowledge, no identities | `data/analytes.json` — generated from the user's own sheet |
| `src/`, `tests/`, `tools/` | `data/health.db`, `data/state/`, `secrets/` |
| | `tests/fixtures/` — golden values, raw extractions, adjudicated errors |

**Relationships are config, not code.** `data/people.json` carries each person's own aliases
("dad", "appa"); `src/people.resolve()` just looks them up. Every family names itself differently and
the software should have no opinion about it.

If you add an example to a docstring, invent one. Do not paste a real name, a real value, or a real
file path into anything that gets committed.

## Data flow

```
{medical_root}/{Person}/{Specialty}/{YYYY-MM-DD} - {Title}.pdf
        │            │              │
        │            │              └─ title + document date
        │            └───────────────── tag   (data/specialties.json)
        └────────────────────────────── correspondent = patient (data/people.json)
        │
        ▼  src/drive_sync.py
Paperless-ngx  (OCR, dedupe, full-text search, viewer — pre-existing, not ours)
        │
        ▼
data/health.db  →  web UI / MCP server / Telegram bot / Todoist reminders
```

## Key facts about the source data

Established by survey; don't re-derive:

- **Extension is unreliable.** Some real PDFs have no extension at all. `sniff()` reads magic bytes,
  but only for the unlabelled ones — this walks an rclone mount that fetches the whole object on
  open, so opening every file to read 8 bytes downloads the entire folder.
- **Office files cannot be consumed.** Paperless without a Tika/Gotenberg container skips xlsx/docx.
  They are reported, never silently dropped.
- **Most filenames start with `YYYY-MM-DD`**, so the document date is usually free.
- Image files are mostly *not* handwriting — many are DICOM exports. Real handwriting lives inside
  scanned PDFs.

## Paperless conventions (pre-existing — follow them, don't invent)

Paperless already held 140 documents before nalam. Its taxonomy:

- **correspondent** = the person (`Patient A`, `Patient B`, `Family`, …)
- **document_type** = top-level category (`Medical`, `Work`, `Identity`, …)
- **tag** = `Category/Subcategory` (`Medical/Insurance`, `Work/Payslip`, …)

Insurance is filed against the `Family` correspondent, not a person. 11 insurance PDFs were already
imported by hand; Paperless rejects them on re-upload by checksum, which is the intended behaviour.

## Uploads go through the REST API, not the consume folder

The consume directory cannot set correspondent, tags or document date. `src/paperless.py` posts to
`/api/documents/post_document/` instead. Requires an API token in `secrets/paperless.json`
(`{"token": "..."}`) — mint it in the Paperless UI under the user menu → My Profile → API Auth Token.

## Planned (not yet built)

Phase 1+ is specified in `/root/health_records/PLAN.md`. The load-bearing decisions:

- **Verbatim extraction.** The LLM copies values exactly as printed and never reformats, converts, or
  computes. Deterministic code does all parsing. Copied from gajana's `_EXTRACTION_PROMPT`.
- **The PDF text layer is the token oracle.** A value not literally present in the text layer is
  quarantined, not committed. Copied from gajana's `StatementValidator`.
- **Documents check themselves.** Cross-check the patient name printed on the report against the
  folder it came from. Mismatch → quarantine. (gajana's `reconcile_summary` trick.)
- **`data/analytes.json` is the codebook**: 71–73 analytes, 20 segments, **per-person** reference
  ranges chosen by the user (not the lab's). It was seeded once from a hand-curated Google Sheet
  ("the master sheet"), which is now just a historical reference — **no longer authoritative and not
  maintained**. The codebook is edited directly (and via the web UI's promote/reject path); do not
  re-import from the Sheet. The Sheet's only lasting value was its hand-entered historical values,
  already ingested into `health.db` and preserved as the extractor's golden test.
- **`health.db` is the source of truth.** Opposite of gajana, where Sheets is primary. Corrections go
  through the Telegram review cards and the web review UI, not a spreadsheet.

## Where the work stands (2026-07-13)

`health.db`: 3,793 observations · 461 medication events · 103 encounters, 8 people, 2013–2026.
All 470 PDFs classified **by content**: 168 prescription · 157 lab · 61 radiology · 36 insurance ·
15 discharge · 12 vaccination. Labs, discharges and prescriptions are extracted.

**Radiology is stored as ONE verbatim text record per study, not per-parameter rows.** An imaging
report is narrative (MRI/USG/CT prose; the echo measurements are the exception), and forcing it into
the analyte-shaped `observations` table produced junk — one `IVS` analyte holding a septum finding,
a septum:wall ratio and a thickening percent at once. Nobody trends a single radiology number over
years; they read the report, and Paperless already full-text-searches the PDF. So `ingest_radiology`
now writes one row to `radiology_reports` (`study_type` bucket via `src/radiology.py:study_bucket`,
verbatim `report_text`, the radiologist's `impression`), keeping the patient-misfile guard. Browse
it with `run_radiology_browse.py`, the web UI's Radiology tab, or the bot's `get_radiology`. Labs
stay structured in `observations`; that split is the point.

A Telegram-filed document now extracts on-demand instead of waiting for the nightly pass — see
`docs/telegram_ingest_queue.md`. Filing enqueues (`src/extract_queue.py`); `run_extract_queue.py`
(new 1-min cron tick) drains it, watching Paperless's own health (`src/monitor.py` → Uptime-Kuma)
so an outage skips the tick entirely rather than extracting without a chance at independent
corroboration. Needs `NALAM_PAPERLESS_PUSH_URL` wired at deploy time.

### Medication review — resume here

Uncorroborated drug names are reviewed with the user, **one person at a time, batched**: propose
a reading for each, flag confidence, they correct what's wrong.

```bash
./venv/bin/python -m tools.export_review          # worksheet, links to each scan
./venv/bin/python -m tools.apply_review 47=Cilostazol 151='Telekast-F||Allegra' 461=-
#   id=Name  correct it     id=  accept as read     id=-  not a drug     id=A||B  split
```

| person | state |
|---|---|
| father, mother | done |
| wife | 6 left, honestly unknown |
| self | ~10 uncertain left |
| **mother-in-law** | **deceased — leave as-is, do not clean up** |
| son (b. 2024-07-23), daughter (b. 2025-11-13), father-in-law | 39 to do |

### Not started

MCP agent · Todoist reminders · Telegram bot · reconciling the live medicine list (people
still show 2023 antibiotics as "current" because nothing ever said stop).

(Deployment is done — live supercronic container on a shared `cron-base` image with gajana.
Radiology extraction is done — see the text-record note above.)

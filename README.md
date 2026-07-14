# nalam

A family health record pipeline. Scanned documents in, structured medical history out.

Ingests lab reports, prescriptions, consultations and discharge summaries from a
Drive folder; files them into Paperless-ngx; extracts structured observations,
encounters and medications into SQLite; and answers questions over them.

Sibling to [gajana](https://github.com/mohankarthik/gajana) (personal finance) and
built on the same principles.

## The rules it is built on

**The folder is the patient.** A document filed against the wrong family member is
the worst failure this system can have — worse than not ingesting it at all. An
unmapped folder aborts the sync; it is never guessed. And the patient name printed
*on the document* is cross-checked against the folder it came from, because folders
are organised by human convenience and sometimes lie.

**The model transcribes; it never interprets.** Every value is copied exactly as
printed — `5.20` stays `5.20`, `1-0-1` stays `1-0-1`. Deterministic code does all
parsing, unit conversion and normalisation. An LLM allowed to "tidy" a lab value is
an LLM that can quietly put a wrong number in a medical record.

**Nothing is trusted without an independent reading.** A value the model reports
must also appear in a reading of the document the model did not produce — the PDF's
own text layer, or Paperless's Tesseract OCR of the same pixels. No independent
reading means no auto-commit, ever.

**Refusing is safe; guessing is not.** Every ambiguity resolves to the review queue,
never to a coin flip. An unknown drug keeps its brand name rather than acquiring a
plausible-looking generic. A child is flagged against no range rather than an adult
one. An absent answer is honest; a confident wrong one is dangerous.

**The raw model output is the asset.** Responses are cached verbatim to `data/llm/`.
Parsing is free and will be got wrong several times; the LLM call is neither. The
database is a derived view and can be rebuilt from the cache offline, for nothing.

## Setup

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp data/settings.example.json data/settings.json   # your paths
cp data/people.example.json  data/people.json      # your family
# secrets/: paperless.json, gemini.json, anthropic.json
```

## Use

```bash
./venv/bin/python run_sync.py                 # Drive -> Paperless
./venv/bin/python run_extract.py              # PDFs  -> health.db
./venv/bin/python run_extract.py --review     # what is not trusted, and why
./venv/bin/python run_extract.py --reclassify # re-resolve analytes (free, offline)
./venv/bin/python run_extract_queue.py        # drain the Telegram on-demand queue (cron, 1 min)
./venv/bin/python run_meds.py --list --person dad
./venv/bin/python run_meds.py --reconcile
./venv/bin/python -m tools.reresolve_drugs    # backfill molecules (free, offline)
./venv/bin/python -m pytest                   # 167 regressions, offline
```

Nothing personal is committed. Real names, paths, values and records live in
gitignored config and data files; see `CLAUDE.md`.

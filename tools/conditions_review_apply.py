"""Apply a filled-in condition-review worksheet (see tools/conditions_review.py).

Reads ~/nalam-conditions-review.md and, per row, applies the ACTION you wrote:

  = Correct Text   FIX  the diagnosis text in the DB (old->new logged to stderr);
                        its icd_codes are recomputed.
  @bucket-key      MAP  add the (current) diagnosis to an existing bucket's aliases.
  +key : CODE      NEW  bucket `key` with ICD-10 category `CODE`; diagnosis -> alias.
  ignore: term     add generic `term` to data/conditions_ignore.json.

FIX edits the DB (default the repo throwaway; pass prod explicitly). MAP/NEW/ignore
edit the committed data/*.json in the working tree -- so after this you COMMIT and
REBUILD the image for those to take effect (conditions.json is baked, not mounted).

Run:  ./venv/bin/python -m tools.conditions_review_apply [path/to/health.db]
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys

from src import db
from src.conditions import _categories, _tokens, load_conditions
from src.constants import CONDITIONS_CONFIG, CONDITIONS_IGNORE_CONFIG
from src.ingest import icd_from_diagnoses

# Corrections/drops are audited to the log (stderr), not a DB table: the Paperless
# scan is the ground truth and prod is backed up, so a queryable in-DB history added
# nothing the log + backups don't already give.
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
logger = logging.getLogger("conditions_review")

WORKSHEET = os.path.expanduser("~/nalam-conditions-review.md")
ROW = re.compile(r"^\|\s*(\d+):(\d+)\s*\|\s*`(.*?)`\s*\|.*?\|\s*(.*?)\s*\|\s*$")


def maybe_alias(entry: dict, as_read: str, key: str, warn: list) -> None:
    """Add the raw diagnosis as an alias ONLY when it is both needed and generic.

    Not needed if the bucket key or an existing alias already whole-word matches it
    (the key does the work -- "cholecystitis" matches "Gangrenous cholecystitis ...").
    Not allowed if it carries a digit: a dated or quantified string ("... Mar 2023",
    "Low platelet - 78k") is record-specific and must be FIXED against the PDF, never
    committed to the generic codebook."""
    art = _tokens(as_read)
    for term in (key, *entry.get("aliases", ())):
        if _tokens(term) and _tokens(term) <= art:
            return
    if any(ch.isdigit() for ch in as_read):
        warn.append(f"{key}: '{as_read}' won't map but looks record-specific -- FIX its text")
        return
    entry.setdefault("aliases", []).append(as_read)


def load_raw(path: str) -> dict:
    """json.load WITHOUT config's comment-stripping, so the _comment survives a rewrite."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dump_raw(path: str, obj: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def main() -> None:
    dbpath = sys.argv[1] if len(sys.argv) > 1 else db.DB_PATH
    if not os.path.exists(WORKSHEET):
        raise SystemExit(f"no worksheet at {WORKSHEET}; run tools.conditions_review first")

    con = db.connect(dbpath)
    conditions = load_raw(CONDITIONS_CONFIG)
    ignore = load_raw(CONDITIONS_IGNORE_CONFIG)
    cats = _categories()
    buckets = load_conditions()  # for validating @bucket targets

    fixes = drops = maps = news = ignores = skipped = 0
    warn: list[str] = []

    for line in open(WORKSHEET, encoding="utf-8"):
        m = ROW.match(line.rstrip("\n"))
        if not m:
            continue
        enc_id, idx, as_read, action = int(m[1]), int(m[2]), m[3], m[4].strip()
        if not action:
            continue

        if action.startswith("="):  # FIX
            new_text = action[1:].strip()
            if not new_text:
                warn.append(f"{enc_id}:{idx} empty fix ignored")
                continue
            row = con.execute("SELECT diagnoses FROM encounters WHERE id=?", (enc_id,)).fetchone()
            dx = json.loads(row["diagnoses"] or "[]")
            if idx >= len(dx):
                warn.append(f"{enc_id}:{idx} out of range")
                continue
            old = dx[idx]
            dx[idx] = new_text
            con.execute(
                "UPDATE encounters SET diagnoses=?, icd_codes=? WHERE id=?",
                (json.dumps(dx), json.dumps(icd_from_diagnoses(dx)), enc_id),
            )
            logger.info("fix  enc %s: %r -> %r", enc_id, old, new_text)
            fixes += 1

        elif action == "-" or action.lower() == "drop":  # DROP garbage from the DB
            # Unreadable OCR with no clinical value: remove it from the encounter
            # rather than perpetually maintain it in an ignore list. Matched BY VALUE
            # (not index) so several drops in one encounter don't shift each other.
            # The Paperless scan stays the ground truth; old->'' is kept for provenance.
            row = con.execute("SELECT diagnoses FROM encounters WHERE id=?", (enc_id,)).fetchone()
            dx = json.loads(row["diagnoses"] or "[]")
            if as_read in dx:
                dx.remove(as_read)
                con.execute(
                    "UPDATE encounters SET diagnoses=?, icd_codes=? WHERE id=?",
                    (json.dumps(dx), json.dumps(icd_from_diagnoses(dx)), enc_id),
                )
                logger.info("drop enc %s: %r", enc_id, as_read)
                drops += 1
            else:
                warn.append(f"{enc_id}:{idx} drop: {as_read!r} not in the encounter")

        elif action.startswith("@"):  # MAP to existing bucket
            key = action[1:].strip()
            if key not in buckets:
                warn.append(f"{enc_id}:{idx} @{key}: no such bucket")
                continue
            maybe_alias(conditions[key], as_read, key, warn)
            maps += 1

        elif action.startswith("+"):  # NEW bucket:  +key : CODE [: alias1; alias2]
            parts = [p.strip() for p in action[1:].split(":")]
            if len(parts) < 2 or not parts[0] or not parts[1]:
                warn.append(f"{enc_id}:{idx} +new needs 'key : CODE'")
                continue
            key, code = parts[0], parts[1].upper()
            if code not in cats:
                warn.append(f"{enc_id}:{idx} +{key}: {code} is not an ICD-10-CM category")
                continue
            entry = conditions.setdefault(
                key, {"icd10": [code], "icd10_title": cats[code], "aliases": []}
            )
            if len(parts) >= 3 and parts[2]:  # explicit, human-chosen clean aliases
                for a in (x.strip() for x in re.split(r"[;,]", parts[2])):
                    if a and a not in entry["aliases"]:
                        entry["aliases"].append(a)
            else:  # otherwise add the raw diagnosis only if needed and generic
                maybe_alias(entry, as_read, key, warn)
            news += 1

        elif action.lower().startswith("ignore:"):  # generic ignore term
            term = action.split(":", 1)[1].strip()
            lst = ignore.setdefault("ignore", [])
            if term and term not in lst:
                lst.append(term)
            ignores += 1

        else:
            warn.append(f"{enc_id}:{idx} unrecognized action: {action!r}")
            skipped += 1

    con.commit()
    dump_raw(CONDITIONS_CONFIG, conditions)
    dump_raw(CONDITIONS_IGNORE_CONFIG, ignore)

    print(f"applied to {dbpath} + working-tree config:")
    print(f"  fixes:   {fixes}  (encounters.diagnoses corrected in DB)")
    print(f"  drops:   {drops}  (garbage removed from encounters.diagnoses)")
    print(f"  maps:    {maps}   (aliases added to existing buckets)")
    print(f"  new:     {news}   (new buckets created)")
    print(f"  ignores: {ignores} (terms added to conditions_ignore.json)")
    if skipped:
        print(f"  skipped: {skipped}")
    for w in warn:
        print(f"  ! {w}", file=sys.stderr)
    if maps or news or ignores:
        print("\n  conditions.json / conditions_ignore.json changed -> commit + rebuild the image.")


if __name__ == "__main__":
    main()

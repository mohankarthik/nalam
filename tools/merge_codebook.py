"""One-shot: consolidate the four codebook files into a single LOINC-keyed one.

Reads (all pre-consolidation):
    data/analytes.json         segment + per-sex ranges
    data/analytes_extra.json   segment-only analytes (no ranges)
    data/aliases.json          printed-name -> canonical
    data/loinc.json            loinc / official name / equivalents / ucum

Applies the human review captured in docs/loinc-coverage-review.md:
    DROPS      -> data/ignored_analytes.json (with their aliases, so re-extraction
                  drops the printed variants silently instead of flooding review)
    NEW CODES  -> ten analytes that were null get a real, table-verified LOINC

Writes:
    data/analytes.json         keyed by LOINC id: {name, official_name,
                               equivalents, aliases, segment, ucum, ranges, note?}
    data/ignored_analytes.json {name: [aliases]} for the dropped analytes

Value-preserving: every kept analyte's segment, ranges, aliases and code are
carried; the script asserts no kept analyte loses its code and no two collide.

Run:  ./venv/bin/python -m tools.merge_codebook          (writes)
      ./venv/bin/python -m tools.merge_codebook --check   (report only, no write)
"""

from __future__ import annotations

import json
import sys

ANALYTES = "data/analytes.json"
EXTRA = "data/analytes_extra.json"
ALIASES = "data/aliases.json"
LOINC = "data/loinc.json"
OUT_CODEBOOK = "data/analytes.json"
OUT_IGNORED = "data/ignored_analytes.json"

# --- The human decisions from the coverage review -------------------------------

# 52 analytes leaving the codebook. Echo is radiology (fully narrative, EF too);
# ratios are derived; sleep metrics are vendor CPAP fields with no LOINC; urine
# qualitative + imaging buckets carry no trend value.
DROP = {
    "Aorta",
    "Aortic Valve",
    "Final Diag",
    "IAS",
    "IVS",
    "IVSD",
    "LA",
    "LVIDD",
    "LVIDS",
    "LVPWD",
    "Left Atrium",
    "Left Ventricle",
    "Mitral Valve",
    "Pericardium",
    "Pulmonary Valve",
    "Right Atrium",
    "Right Ventricle",
    "Tricuspid Valve",
    "EF",
    "Platelet Morphology",
    "P Duration",
    "BUN/Creatinine Ratio",
    "Urea/Creatinine Ratio",
    "A:G Ratio",
    "SGOT/SGPT Ratio",
    "Cholesterol:HDL Ratio",
    "AHI",
    "Clear Airway Index",
    "Days with Device Usage",
    "Days without Device Usage",
    "EPAP",
    "Flow Limitation Index",
    "Hypopnea Index",
    "IPAP",
    "Large Leak Percent",
    "Obstructive Apnea Index",
    "Percent Days with Device Usage",
    "RERA Index",
    "Abdomen & Pelvis",
    "Breast",
    "Chest PA",
    "Urine Appearance",
    "Urine Bile Pigments",
    "Urine Bile Salt",
    "Urine Casts",
    "Urine Colour",
    "Urine Crystals",
    "Urine Epithelial Cells",
    "Urine Ketone Bodies",
    "Urine Leucocyte Esterase",
    "Urine Nitrite",
    "Urine SG",
    "Urine Volume",
}

# Ten previously-null analytes -> a table-verified ACTIVE LOINC. official_name and
# ucum are copied verbatim from data/loinc.csv (LONG_COMMON_NAME / EXAMPLE_UCUM).
# note is added where the code is qualitative/narrative and so carries no UCUM.
NEW_CODES = {
    "Plateletcrit": {
        "loinc": "51637-7",
        "official_name": "Plateletcrit [Volume Fraction] in Blood",
        "ucum": "%",
    },
    "Impression": {
        "loinc": "70936-0",
        "official_name": "Vision testing Narrative",
        "ucum": None,
        "note": "narrative optometry impression; no UCUM",
    },
    "Vision": {
        "loinc": "28711-0",
        "official_name": "Eye Visual acuity far.binocular by Phoropter",
        "ucum": "[ft_us]/[ft_us]",
    },
    "Near Vision": {
        "loinc": "28737-5",
        "official_name": "Eye Visual acuity N.binocular by Phoropter",
        "ucum": "[ft_us]/[ft_us]",
    },
    "Urine Protein": {
        "loinc": "2887-8",
        "official_name": "Protein [Presence] in Urine",
        "ucum": None,
        "note": "qualitative dipstick presence; no UCUM",
    },
    "Urine Glucose": {
        "loinc": "50555-2",
        "official_name": "Glucose [Presence] in Urine by Automated test strip",
        "ucum": None,
        "note": "qualitative dipstick presence; no UCUM",
    },
    "Urine Pus Cells": {
        "loinc": "5821-4",
        "official_name": "Leukocytes [#/area] in Urine sediment by Microscopy high power field",
        "ucum": "/[HPF]",
    },
    "Urine RBC": {
        "loinc": "30391-7",
        "official_name": "Erythrocytes [#/volume] in Urine",
        "ucum": "/uL",
    },
    "Urine pH": {
        "loinc": "50560-2",
        "official_name": "pH of Urine by Automated test strip",
        "ucum": "[pH]",
    },
    "Pap Smear": {
        "loinc": "18500-9",
        "official_name": "Microscopic observation [Identifier] in Cervix by Cyto stain.thin prep",
        "ucum": None,
        "note": "nominal cytology identifier; no UCUM",
    },
}


def _real(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def build() -> tuple[dict, dict]:
    analytes = json.load(open(ANALYTES, encoding="utf-8"))
    # Not idempotent: this reads analytes.json (name-keyed) and later overwrites it
    # (loinc-keyed). A second run would read its own output and mangle it. Refuse if
    # analytes.json already looks migrated (values carry a 'name' field).
    if analytes and all(isinstance(v, dict) and "name" in v for v in analytes.values()):
        raise SystemExit(
            "data/analytes.json is already LOINC-keyed (migrated). Refusing to re-run; "
            "restore the pre-migration file first if you really mean to."
        )
    extra = _real(json.load(open(EXTRA, encoding="utf-8")))
    aliases = _real(json.load(open(ALIASES, encoding="utf-8")))
    loinc = _real(json.load(open(LOINC, encoding="utf-8")))

    names = set(analytes) | set(extra)
    unknown_alias = set(aliases) - names
    if unknown_alias:
        raise SystemExit(f"aliases.json names not in codebook: {sorted(unknown_alias)}")
    missing_drop = DROP - names
    if missing_drop:
        raise SystemExit(f"DROP names not in codebook (typo?): {sorted(missing_drop)}")

    codebook: dict[str, dict] = {}  # loinc id -> entry
    ignored: dict[str, dict] = {}  # dropped canonical -> {segment, aliases}

    for name in sorted(names):
        base = analytes.get(name, {})
        segment = base.get("segment") or extra.get(name, {}).get("segment")

        if name in DROP:
            # Keep the segment so the ingest ignore-check can domain-gate exactly
            # as the live matcher did -- a urine alias only fires in a urine section.
            ignored[name] = {"segment": segment, "aliases": list(aliases.get(name, []))}
            continue

        ranges = base.get("ranges", {}) or {}
        row = loinc.get(name, {})

        if name in NEW_CODES:
            nc = NEW_CODES[name]
            code = nc["loinc"]
            official = nc["official_name"]
            ucum = nc["ucum"]
            note = nc.get("note")
        else:
            code = row.get("loinc")
            official = row.get("name")
            ucum = row.get("ucum")
            note = row.get("note")

        if not code:
            raise SystemExit(f"kept analyte {name!r} has no LOINC code")
        if code in codebook:
            raise SystemExit(f"LOINC collision {code}: {name!r} vs {codebook[code]['name']!r}")

        entry = {
            "name": name,
            "official_name": official,
            "equivalents": list(row.get("equivalents") or []),
            "aliases": list(aliases.get(name, [])),
            "segment": segment,
            "ucum": ucum,
            "ranges": ranges,
        }
        if note:
            entry["note"] = note
        codebook[code] = entry

    # invariants
    kept_names = [e["name"] for e in codebook.values()]
    assert len(kept_names) == len(set(kept_names)), "duplicate canonical name"
    assert len(names) == len(codebook) + len(ignored), "analyte accounting mismatch"
    return codebook, ignored


def main() -> None:
    codebook, ignored = build()
    print(f"kept: {len(codebook)} analytes (all coded, unique ids)", file=sys.stderr)
    print(f"dropped -> ignore-list: {len(ignored)}", file=sys.stderr)
    if "--check" in sys.argv:
        print("--check: no files written", file=sys.stderr)
        return

    with open(OUT_CODEBOOK, "w", encoding="utf-8") as f:
        json.dump(codebook, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    ignored_doc = {
        "_comment": "Analytes deliberately removed from the codebook (echo->radiology, "
        "ratios, CPAP device metrics, qualitative urine, imaging buckets). "
        "Their printed names + aliases are matched at ingest and dropped "
        "silently, so re-extraction never re-floods the review queue. "
        "See docs/loinc-coverage-review.md.",
        "ignored": {k: ignored[k] for k in sorted(ignored)},
    }
    with open(OUT_IGNORED, "w", encoding="utf-8") as f:
        json.dump(ignored_doc, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {OUT_CODEBOOK} and {OUT_IGNORED}", file=sys.stderr)


if __name__ == "__main__":
    main()

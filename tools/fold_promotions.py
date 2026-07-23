"""One-shot: graduate the prod UI-promotions into the consolidated codebook.

Promotions live in a name-keyed staging file (promoted.json) with no LOINC identity.
This bakes the reviewed ones into data/analytes.json with real, table-verified codes
(the same review pass tools/merge_codebook.py did for the original 10), graduates the
promoted aliases onto their existing analytes, and adds the two rejected promotions to
the ignore-list. After this, promoted.json is emptied by hand and the DB is migrated
(tools/migrate_split_bp.py + tools/migrate_drop_analytes.py).

Decisions (see the promotions review):
  fold  BMI, RBS, RA, Estradiol, AMH, FSH, LH   -> coded analytes
  drop  BP (redundant with BP High/Low; rows split, not purged)
  drop  LDL:HDL Ratio (a derived ratio, like the others we dropped)
  fold  10 promoted aliases -> existing analytes' aliases

Run:  ./venv/bin/python -m tools.fold_promotions [--check]
"""

from __future__ import annotations

import json
import sys

CODEBOOK = "data/analytes.json"
IGNORED = "data/ignored_analytes.json"

# name -> full codebook entry (verified against data/loinc.csv).
FOLD = {
    "BMI": {
        "loinc": "39156-5",
        "official_name": "Body mass index (BMI) [Ratio]",
        "ucum": "kg/m2",
        "segment": "Phys",
        "aliases": [],
    },
    "RBS": {
        "loinc": "2345-7",
        "official_name": "Glucose [Mass/volume] in Serum or Plasma",
        "ucum": "mg/dL",
        "segment": "Glucose",
        "aliases": [
            "Glucose - Random (RBS) (Plasma-R/ GOD- POD)",
            "GLUCOSE, RANDOM (R), PLASMA*",
            "RANDOM GLUCOSE LEVELS",
            "GRBS mg/dL",
        ],
    },
    "RA": {
        "loinc": "11572-5",
        "official_name": "Rheumatoid factor [Units/volume] in Serum or Plasma",
        "ucum": "[IU]/mL",
        "segment": "RHEUMATOID",
        "aliases": ["RHEUMATOID FACTOR", "RHEUMATOID FACTOR (RA), SERUM"],
    },
    "ESTRADIOL": {
        "loinc": "2243-4",
        "official_name": "Estradiol (E2) [Mass/volume] in Serum or Plasma",
        "ucum": "pg/mL",
        "segment": "Fertility",
        "aliases": ["ESTRADIOL(E2)"],
    },
    "ANTI-MULLERIAN HORMONE": {
        "loinc": "38476-8",
        "official_name": "Mullerian inhibiting substance [Mass/volume] in Serum or Plasma",
        "ucum": "ng/mL",
        "segment": "Fertility",
        "aliases": [
            'ANTI MULLERIAN HORMONE - Immunoenzymatic ("Sandwich") assay - Chemiluminescence'
        ],
    },
    "FOLLICLE STIMULATING HORMONE": {
        "loinc": "15067-2",
        "official_name": "Follitropin [Units/volume] in Serum or Plasma",
        "ucum": "m[IU]/mL",
        "segment": "Fertility",
        "aliases": ["FOLLICLE STIMULATING HORMONE(FSH)"],
    },
    "LUTEINIZING HORMONE": {
        "loinc": "10501-5",
        "official_name": "Lutropin [Units/volume] in Serum or Plasma",
        "ucum": "m[IU]/mL",
        "segment": "Fertility",
        "aliases": ["LUTEINIZING HORMONE(LH)"],
    },
}

# promoted aliases that belong on existing analytes: canonical -> [printed variants]
GRADUATE_ALIASES = {
    "Haemoglobin": ["CBC - Hb"],
    "Cholesterol": ["Lipid profile - TC", "TC"],
    "TGL": ["TG"],
    "Vitamin D": ["Vit D"],
    "HbA1c": ["Glycosylated Hb"],
    "HBsAg": ["Hbs Ag (Rapid Card Test)"],
    "PP Blood Sugar": ["GLUCOSE POSTPRANDIAL (GOD-POD)"],
    "TSH": ["hyroid Stimulating Hormone (CLIA)"],
    "C-Reactive Protein": ["C-REACTIVE PROTIEN"],
    "Non-HDL Cholesterol": ["Non HDL Cholesterolserum", "NON HDL CHOLESTROL"],
}

# promoted analytes we reject: name -> ignore-list entry
DROP = {
    "BP": {"segment": "Phys", "aliases": []},
    "LDL:HDL Ratio": {"segment": "Lipid", "aliases": ["LDL.CHOL/HDL.CHOL Ratio (Enzymatic)"]},
}


def main() -> None:
    codebook = json.load(open(CODEBOOK, encoding="utf-8"))
    ig = json.load(open(IGNORED, encoding="utf-8"))

    by_name = {e["name"]: (code, e) for code, e in codebook.items()}

    # 1. Fold the coded promotions.
    for name, spec in FOLD.items():
        code = spec["loinc"]
        if code in codebook:
            raise SystemExit(f"LOINC collision {code}: {name} vs {codebook[code]['name']}")
        if name in by_name:
            raise SystemExit(f"{name} already in codebook")
        codebook[code] = {
            "name": name,
            "official_name": spec["official_name"],
            "equivalents": [],
            "aliases": list(spec["aliases"]),
            "segment": spec["segment"],
            "ucum": spec["ucum"],
            "ranges": {},
        }

    # 2. Graduate aliases onto existing analytes (dedup, preserve order).
    for canonical, extra in GRADUATE_ALIASES.items():
        if canonical not in by_name:
            raise SystemExit(f"graduate-alias target {canonical!r} not in codebook")
        _, e = by_name[canonical]
        have = e["aliases"]
        e["aliases"] = have + [a for a in extra if a not in have]

    # 3. Reject the two into the ignore-list.
    ig.setdefault("ignored", {})
    for name, entry in DROP.items():
        ig["ignored"][name] = entry
    ig["ignored"] = {k: ig["ignored"][k] for k in sorted(ig["ignored"])}

    # invariants
    names = [e["name"] for e in codebook.values()]
    assert len(names) == len(set(names)), "duplicate canonical name"
    assert not (set(DROP) & set(names)), "a dropped name is also in the codebook"

    print(f"codebook: {len(codebook)} analytes (+{len(FOLD)} folded)", file=sys.stderr)
    print(f"aliases graduated onto: {len(GRADUATE_ALIASES)} analytes", file=sys.stderr)
    print(f"ignore-list: {len(ig['ignored'])} (+{len(DROP)})", file=sys.stderr)
    if "--check" in sys.argv:
        print("--check: no files written", file=sys.stderr)
        return

    with open(CODEBOOK, "w", encoding="utf-8") as f:
        json.dump(codebook, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    with open(IGNORED, "w", encoding="utf-8") as f:
        json.dump(ig, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {CODEBOOK} and {IGNORED}", file=sys.stderr)


if __name__ == "__main__":
    main()

"""Map a lab's printed test name onto the canonical analyte in the codebook.

This is where the project's value actually accrues, and it is deliberately NOT
an LLM's job to decide.

The lab prints "HbA1c (Glycosylated Hemoglobin)". The codebook (hand-curated over
years and imported from the master sheet) calls it "HbA1c". Get that
mapping wrong -- silently -- and a years-long HbA1c trend quietly becomes three
different tests. So:

    deterministic match  -> accept
    no confident match   -> propose, and hold for human approval

An LLM may PROPOSE an alias. It may never install one. Approved aliases are
written back into data/analytes.json and are then permanent and free.
"""

from __future__ import annotations

import json
import os
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

ANALYTES = "data/analytes.json"
ANALYTES_EXTRA = "data/analytes_extra.json"
ALIASES = "data/aliases.json"

# Words that genuinely carry no identity: assay method and filler.
#
# DANGER: do not add specimen ("serum", "urine", "plasma") or qualifiers
# ("total", "direct", "indirect", "fasting") here. They ARE the identity.
# Stripping "direct" once made "Direct Bilirubin" reduce to {bilirubin}, which
# then matched Total Bilirubin and Indirect Bilirubin -- writing the wrong
# number into a medical record. The golden test caught it; the next one might not.
NOISE = {
    "test",
    "level",
    "levels",
    "value",
    "method",
    "by",
    "in",
    "the",
    "of",
    "hexokinase",
    "westergren",
    "hplc",
    "calculated",
    "auto",
    "estimated",
    "s",
    "b",
}

# A Master Health Checkup is not a lab report -- it is five report types stapled
# together (blood panels, urine routine, 2D echo, ophthalmology, ultrasound).
# Test names collide ACROSS those sections and mean completely different things:
#
#   RBC        blood: 5.83 mill/uL      urine: "Negative" (dipstick)
#   Albumin    blood: 4.0 g/dL          urine: "Negative" (dipstick)
#   Impression eye:   "Normal Ocular"   USG:   "No evidence of cholecystitis"
#
# So the model reports which section heading each result sat under, and a result
# may only match an analyte from the same domain. Vision owns structure.
# Reports do NOT label sections with the word 'echo' or 'urine'. They use the
# sub-section heading: 'GREAT VESSELS', 'M-MODE MEASUREMENTS', 'VALVES', 'SEPT',
# 'MICROSCOPIC EXAMINATION'. Matching only on the domain word meant every echo
# measurement the user actually tracks (EF, LVIDD, Aorta, LA, IVSD) fell back to
# 'blood' and could never match its own analyte -- and, worse, a urine 'RBC'
# under 'Microscopy' was classified as blood and was one duplicate-guard away
# from being recorded as a blood cell count.
DOMAIN_KEYWORDS = {
    "urine": (
        "urine",
        "urinary",
        "urinalysis",
        "microscop",
        "deposit",
        "leucocyte esterase",
        "leukocyte esterase",
    ),
    "echo": (
        "echo",
        "echocardiog",
        "m-mode",
        "mmode",
        "doppler",
        "great vessel",
        "valve",
        "sept",
        "chamber",
        "wall motion",
        "pericardium",
        "ventricle",
        "atrium",
        "vegetation",
    ),
    "opt": ("ophthal", "optometry", "eye", "vision", "refraction", "fundus"),
    "usg": ("ultrasound", "usg", "sonograph", "sonolog", "ultrasonograph"),
    "xray": ("x-ray", "xray", "radiograph", "chest pa"),
    "tmt": ("tmt", "treadmill", "stress test", "exercise test"),
    "ecg": ("electrocardiog", "ecg", "ekg"),
    "sleep": (
        "sleep therapy",
        "bi-level",
        "bilevel",
        "cpap",
        "bipap",
        "apap",
        "compliance summary",
        "ahi",
        "apnea",
        "apnoea",
        "hypopnea",
        "periodic breathing",
        "large leak",
        "flow limitation",
    ),
    "phys": ("vitals", "physical exam", "anthropom", "general exam"),
}

# Which domain each codebook segment lives in. Anything unlisted is a blood
# panel (Glucose, KFT, LFT, Lipid, CBC, Thyroid, Iron, Vitamin, PSA, ...).
SEGMENT_DOMAIN = {
    "Urine": "urine",
    "2D Echo": "echo",
    "Opt": "opt",
    "USG": "usg",
    "X-Ray": "xray",
    "TMT": "tmt",
    "Phys": "phys",
    "ECG": "ecg",
    "Sleep": "sleep",
}
BLOOD = "blood"


# A printed name carrying one of these is a DIFFERENT test from the bare analyte,
# not a variant spelling of it:
#
#   "Absolute Neutrophil Count" (cells/uL)  is not  "Neutrophils" (a percentage)
#   "CHOL:HDL RATIO"                        is not  "HDL"
#   "Non HDL Cholesterol"                   is not  "HDL"
#   "WBC/Pus Cells" (urine microscopy)      is not  "WBC" (a blood count)
#
# Left unblocked, each of these also claims the real analyte, and the duplicate
# guard then refuses BOTH -- so the genuine value never lands.
DISQUALIFIERS = {
    "absolute",
    "abs",
    "ratio",
    "non",
    "index",
    "pus",
    "eag",
    "percentage",
}


# An all-caps acronym in parentheses is part of the test's NAME and must be kept:
# "PROSTATE SPECIFIC ANTIGEN (PSA)", "Iron (TPTZ)", "... Transpeptidase (GGT)".
# Anything else in parentheses is the assay method and must be dropped.
_ACRONYM = re.compile(r"^[A-Z0-9][A-Z0-9/\-]{1,7}$")


def _strip_methods(name: str) -> str:
    """Drop the assay method labs print in parentheses, but keep name acronyms.

    'SERUM SODIUM (Indirect ISE)' -> 'SERUM SODIUM'. Not cosmetic: the reagent in
    'Ketone Bodies (Strip, sodium prusside Reaction)' once made that result match
    the analyte Sodium. But '(PSA)' IS the name, so acronyms survive.
    """

    def keep(m: re.Match) -> str:
        inner = m.group(1).strip()
        return f" {inner} " if _ACRONYM.match(inner) else " "

    return re.sub(r"\(([^)]*)\)", keep, name or "")


def _tokens(name: str) -> set[str]:
    name = _strip_methods(name).lower()
    name = re.sub(r"[\(\)\[\],:;/\\-]+", " ", name)
    name = re.sub(r"[^a-z0-9 ]+", "", name)
    return {t for t in name.split() if t and t not in NOISE}


def domain_of(section: str, printed: str = "") -> str:
    """Which kind of report a result came from. Section heading wins; the test
    name is the fallback for reports that print no headings."""
    haystack = f"{section} {printed}".lower()
    for domain, keywords in DOMAIN_KEYWORDS.items():
        if any(k in haystack for k in keywords):
            return domain
    return BLOOD


def _domain_compatible(printed: str, section: str, entry: dict) -> bool:
    """A result may only match an analyte from its own kind of report."""
    return domain_of(section, printed) == SEGMENT_DOMAIN.get(entry.get("segment", ""), BLOOD)


def load_codebook() -> dict[str, dict]:
    """The codebook: three files, merged. Only the first is machine-generated.

      data/analytes.json        regenerated from the master sheet.
                                the analytes the sheet tracked -- typically for
                                only one or two people.
      data/analytes_extra.json  analytes that reports contain
                                but the sheet never did (RDW, MPV, urine
                                microscopy, sleep-study AHI). Hand-curated.
      data/aliases.json         what labs print -> the canonical name.
                                Hand-curated.

    Split so that re-importing the sheet can never clobber the curated files.
    """
    with open(ANALYTES, encoding="utf-8") as f:
        codebook = json.load(f)

    if os.path.exists(ANALYTES_EXTRA):
        with open(ANALYTES_EXTRA, encoding="utf-8") as f:
            for name, entry in json.load(f).items():
                if name.startswith("_"):
                    continue
                codebook.setdefault(
                    name, {"segment": entry.get("segment"), "aliases": [], "ranges": {}}
                )

    with open(ALIASES, encoding="utf-8") as f:
        aliases = json.load(f)
    for canonical, names in aliases.items():
        if canonical.startswith("_"):
            continue
        if canonical not in codebook:
            logger.warning(f"alias for unknown analyte {canonical!r}; ignoring")
            continue
        codebook[canonical]["aliases"] = list(names)

    return codebook


def match(printed: str, codebook: dict[str, dict], section: str = "") -> Optional[str]:
    """Return the canonical analyte for a printed test name, or None.

    Exact fold first, then approved aliases, then a strict token-containment
    rule: every token of the canonical name must appear in the printed name.
    "HbA1c" matches "HbA1c (Glycosylated Hemoglobin)". "Direct Bilirubin" does
    not match "Total Bilirubin", because "direct" is absent.

    Ambiguity is a failure, not a coin flip: if two canonical analytes both
    match, we return None and let a human decide.
    """
    p_tokens = _tokens(printed)
    if not p_tokens:
        return None

    # Each analyte is known by its canonical name plus any approved aliases.
    # A name matches when all of its tokens appear in the printed name, so
    # "HbA1c" matches "HbA1c (Glycosylated Hemoglobin)". Specificity = how many
    # tokens it pinned, so "PP Blood Sugar" beats a bare "Sugar".
    best_score = 0
    winners: list[str] = []
    for canonical, entry in codebook.items():
        if not _domain_compatible(printed, section, entry):
            continue
        for name in [canonical, *entry.get("aliases", [])]:
            tokens = _tokens(name)
            if not tokens or not tokens <= p_tokens:
                continue
            # Extra words that make it a different test, not a variant spelling.
            if (p_tokens - tokens) & DISQUALIFIERS:
                continue
            score = len(tokens)
            if score > best_score:
                best_score, winners = score, [canonical]
            elif score == best_score and canonical not in winners:
                winners.append(canonical)

    if len(winners) == 1:
        return winners[0]
    if len(winners) > 1:
        # Genuinely ambiguous. Refuse rather than guess -- a coin flip here puts
        # one test's value under another test's name, permanently.
        logger.debug(f"ambiguous, refusing: {printed!r} -> {winners}")
    return None


# A result the lab did not produce. Not a value, and not a disagreement with one.
ABSENT = {"n/a", "na", "not done", "nd", "-", "--", "not applicable", "pending"}

# Labs say the same thing many ways. These are equivalent, not different results.
QUALITATIVE = {
    "negative": "negative",
    "not reactive": "negative",
    "non reactive": "negative",
    "nonreactive": "negative",
    "non-reactive": "negative",
    "nil": "negative",
    "absent": "negative",
    "not detected": "negative",
    "positive": "positive",
    "reactive": "positive",
    "present": "positive",
    "detected": "positive",
}


def parse_value(raw: str) -> tuple[Optional[float], Optional[str]]:
    """Split a printed result into (number, qualitative). Exactly one is set.

    Handles the forms labs actually print: "5.20", "1,81,000", "< 0.5",
    "Not Reactive". A censored value ("< 0.5") keeps its bound as the number --
    the raw text is preserved separately, so nothing is lost.
    """
    text = (raw or "").strip()
    if not text:
        return None, None

    # PDF font mangling renders the "H"/"L" flag as Greek lookalikes: a value
    # arrives as "Η 3.35 ▲" (capital Eta, not H). Fold them back to ASCII.
    text = text.replace("Η", "H").replace("Ι", "I").replace("Ϊ", "I")

    # The flag can also LEAD the value: "H 3.35", "L 0.61".
    text = re.sub(r"^\s*[HL]\s+(?=[<>=~.\d])", "", text)

    # Labs mark abnormal results with trailing flags, and they stack:
    # "112 #", "206*", "15.4 H", "101.00 ▲", "103.00 ▲ (H)". The flag is
    # presentation, not data -- peel every one off and keep the number.
    for _ in range(4):
        stripped = re.sub(r"[\s]*[#*↑↓▲▼△▽]+\s*$", "", text)
        stripped = re.sub(r"\s*\(\s*(?:H|L|HH|LL|HIGH|LOW)\s*\)\s*$", "", stripped, flags=re.I)
        stripped = re.sub(r"\s+(?:H|L|HH|LL)$", "", stripped)
        stripped = stripped.strip()
        if stripped == text:
            break
        text = stripped

    folded = re.sub(r"[\s_]+", " ", text.lower()).strip()
    if folded in ABSENT:
        return None, None
    if folded in QUALITATIVE:
        return None, QUALITATIVE[folded]

    bare = re.sub(r"[,\s]", "", text).lstrip("<>=~")
    try:
        return float(bare), None
    except ValueError:
        return None, None


def values_agree(a: str, b: str, tolerance: float = 0.011) -> bool:
    """Do two printed results mean the same thing?

    Numeric comparison is relative, because labs print 18 and 18.00 and 18.0 for
    the same result. Qualitative comparison folds the synonyms.
    """
    a_num, a_qual = parse_value(a)
    b_num, b_qual = parse_value(b)

    if a_num is not None and b_num is not None:
        return abs(a_num - b_num) <= tolerance * max(abs(b_num), 1.0)
    if a_qual and b_qual:
        return a_qual == b_qual
    return (a or "").strip().lower() == (b or "").strip().lower()


def resolve(
    results: list[dict], codebook: Optional[dict[str, dict]] = None
) -> tuple[list[dict], list[str]]:
    """Attach a canonical analyte to each result. Returns (resolved, unmatched names).

    Unmatched results are kept -- they are not garbage, they are simply not yet
    in the codebook. They carry ``analyte=None`` and go to review.

    If two results in ONE report both claim the same analyte, neither is
    trusted: one is silently overwriting the other, and we cannot know which is
    real. Both are unresolved and go to review. (This is how a urine RBC came to
    sit where a blood RBC belonged.)
    """
    codebook = codebook if codebook is not None else load_codebook()
    resolved, unmatched = [], []
    for r in results:
        canonical = match(r.get("name", ""), codebook, r.get("section", ""))
        resolved.append({**r, "analyte": canonical})
        if canonical is None:
            unmatched.append(r.get("name", ""))

    claimed: dict[str, int] = {}
    for r in resolved:
        if r["analyte"]:
            claimed[r["analyte"]] = claimed.get(r["analyte"], 0) + 1

    for r in resolved:
        if r["analyte"] and claimed[r["analyte"]] > 1:
            logger.warning(
                f"{r['analyte']!r} claimed by {claimed[r['analyte']]} results in one "
                f"report ({r.get('name')!r}); refusing all of them"
            )
            unmatched.append(r.get("name", ""))
            r["analyte"] = None
            r["ambiguous"] = True

    return resolved, unmatched

"""Bucketing an imaging report into a study type.

Radiology reports are stored one-per-document (see `db.radiology_reports`); the
bucket is what makes them browsable -- "show me his echoes", "her abdominal
ultrasounds over the years". A bucket is deliberately coarse: modality plus body
region ("USG Abdomen", "MRI Brain", "X-Ray Chest"), not a per-parameter label.

Derivation is title-first, because most filenames already say it
(`2023-03-20 - Ila - MRI Brain.pdf`). The document date and the person's name are
noise here and are simply scanned past: a person's name almost never contains a
modality or region word, so keyword-scanning the whole descriptor is robust to
them without needing the people map. When no modality word is present at all
(`... - Vishwas Collection.pdf`, a lab's name), we fall back to the extractor's
own `study` object if one was passed, then to the raw descriptor. Nothing here
calls an LLM; it is pure string work so the backfill can run for free.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# modality keyword -> canonical label. Order matters only for readability; each is
# matched independently and the first hit wins.
_MODALITY = [
    (("echocardiogra", "2d echo", " echo", "echo ", "doppler echo"), "Echo"),
    (("mri", "magnetic resonance"), "MRI"),
    (("cect", "ct scan", " ct ", "computed tomog", "cbct"), "CT"),
    (("mammogra",), "Mammogram"),
    (("dexa", "bmd", "bone densit"), "DEXA"),
    (("pet ", "pet-ct", "pet scan"), "PET"),
    (("hysterosalping", "hsg"), "HSG"),
    (("tvs", "transvaginal"), "TVS"),
    (("doppler",), "Doppler"),
    (("x-ray", "xray", "x ray", "radiograph"), "X-Ray"),
    (("usg", "ultrasonogra", "ultrasound", "sonogra", "scan"), "USG"),
]

# region keyword -> canonical label.
_REGION = [
    (("brain", "head", "cranial"), "Brain"),
    (("chest", "thorax", "thoracic", "lung"), "Chest"),
    (("abdomen", "abdominal", "kub", "hepatobiliary"), "Abdomen"),
    (("pelvis", "pelvic"), "Pelvis"),
    (("whole abdomen",), "Abdomen"),
    (("spine", "spinal", "lumbar", "cervical spine", "ls spine"), "Spine"),
    (("neck",), "Neck"),
    (("breast",), "Breast"),
    (("knee",), "Knee"),
    (("shoulder",), "Shoulder"),
    (("lower limb", "lower limbs", "leg"), "Lower Limb"),
    (("upper limb", "arm"), "Upper Limb"),
    (("carotid",), "Carotid"),
    (("thyroid",), "Thyroid"),
]

_DATE_PREFIX = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*[-_ ]*")


def _descriptor(title: str) -> str:
    """The title with any leading ISO date and file extension stripped."""
    name = re.sub(r"\.[A-Za-z0-9]{1,4}$", "", title or "").strip()
    return _DATE_PREFIX.sub("", name).strip()


def _first_match(haystack: str, table: list) -> Optional[str]:
    for needles, label in table:
        if any(n in haystack for n in needles):
            return label
    return None


def study_bucket(title: str, study: Optional[dict[str, Any]] = None) -> str:
    """A coarse study-type label for an imaging report.

    `title` is the document title/filename (a leading date and the person's name
    are tolerated). `study` is the extractor's optional study object
    ({"name": ..., "modality": ...}), consulted only when the title carries no
    modality word of its own.
    """
    desc = _descriptor(title)
    hay = f" {desc.lower()} "

    modality = _first_match(hay, _MODALITY)
    region = _first_match(hay, _REGION)

    if modality and region:
        return f"{modality} {region}"
    if modality:
        return modality

    # No modality word in the title. Try the extractor's own reading of the study.
    if study:
        s = str(study.get("name") or study.get("modality") or "").strip()
        if s:
            sub = study_bucket(s)
            if sub != "Radiology":
                return sub

    # Nothing structured to go on: keep the human-written descriptor verbatim
    # (title-cased if it is a plain phrase), rather than invent a category. When the
    # descriptor is "Person - Something", the last segment is the study, not the
    # name -- take it so an unbucketable file reads "Vishwas Collection", not
    # "Ila - Vishwas Collection".
    tail = desc.split(" - ")[-1].strip() if " - " in desc else desc
    if tail:
        return tail if any(c.isupper() for c in tail) else tail.title()
    return "Radiology"

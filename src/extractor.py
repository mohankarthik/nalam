"""Extract structured lab results from a PDF, and refuse to trust the model.

Flow per document:

  1. pull the PDF's text layer            -- the token oracle
  2. ask the model to TRANSCRIBE results  -- verbatim, never interpret
  3. validate against the text layer      -- hallucinated values quarantine
  4. validate the patient on the report    -- misfiled scans quarantine

Nothing here writes to the database. It returns what passed and what did not,
and the caller decides. A quarantined result is never dropped.
"""

from __future__ import annotations

import datetime
import io
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from pypdf import PdfReader, PdfWriter

from src import validator
from src.llm import call_with_pdf

logger = logging.getLogger(__name__)

LAB_CONFIG = "data/configs/lab.json"
DISCHARGE_CONFIG = "data/configs/discharge.json"


def is_lab(doc: Any) -> bool:
    """Is this a lab report?

    The folder says WHO the patient is (authoritative). It does not say what
    KIND of document this is: many lab reports sit outside a folder named
    'Reports' -- they land under a specialty or admission folder instead. Routing
    on the folder skipped every one of them.

    So: the Reports folder, OR a title that names a test. Keyword matching, not
    classification -- see the limits noted in data/configs/lab.json.
    """
    import re

    with open(LAB_CONFIG, encoding="utf-8") as f:
        routing = json.load(f)["routing"]

    if doc.tag in routing["tags"]:
        return True
    return any(re.search(p, doc.title, re.IGNORECASE) for p in routing["title_patterns"])


def is_discharge(doc: Any) -> bool:
    """Is this a discharge summary?

    Routed on the TITLE, not the folder: an Admissions folder groups an EPISODE, not a
    document type. Most of what is in it are the labs and scans from the stay;
    only a handful are actual summaries.
    """
    import re

    with open(DISCHARGE_CONFIG, encoding="utf-8") as f:
        routing = json.load(f)["routing"]
    return any(re.search(p, doc.title, re.IGNORECASE) for p in routing["title_patterns"])


def is_radiology(doc: Any) -> bool:
    """Is this an imaging report (X-ray, USG/Doppler, CT, MRI, echo, mammogram)?

    Routed on the TITLE, like is_discharge() -- and this check has to run BEFORE
    is_lab(): imaging shares the Medical/Reports tag with lab reports, so the tag
    cannot tell them apart. is_lab() calls every Medical/Reports document a lab,
    and a "2D Echo" or "USG Abdomen" filed under Reports was grabbed by it and
    exploded into junk analyte rows -- the exact failure radiology_reports exists
    to prevent. A title that names an imaging study routes to ingest_radiology
    first. Keyword matching, not classification -- classify() reads the page and
    is the content-based backstop for whatever these title patterns miss.
    """
    import re

    with open(RADIOLOGY_CONFIG, encoding="utf-8") as f:
        routing = json.load(f).get("routing", {})
    if doc.tag in routing.get("tags", []):
        return True
    return any(re.search(p, doc.title, re.IGNORECASE) for p in routing.get("title_patterns", []))


@dataclass
class Extraction:
    person: str
    source: str
    model: str
    patient: dict[str, str]
    passed: list[dict[str, Any]] = field(default_factory=list)
    quarantined: list[dict[str, Any]] = field(default_factory=list)
    doc_verdict: Optional[validator.Verdict] = None

    @property
    def usable(self) -> bool:
        """A document that fails its own patient check yields nothing."""
        return bool(self.doc_verdict and self.doc_verdict.ok)


def text_layer(pdf_bytes: bytes) -> str:
    """Extract the PDF's embedded text. Empty for a pure scan -- that's a signal."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:
        logger.warning(f"Could not read text layer: {e}")
        return ""


def extract_lab(
    pdf_bytes: bytes,
    person: str,
    source: str,
    expected_date: Optional[datetime.date] = None,
    models: Optional[list[str]] = None,
) -> Extraction:
    with open(LAB_CONFIG, encoding="utf-8") as f:
        config = json.load(f)

    text = text_layer(pdf_bytes)
    data, model = call_with_pdf(
        config["prompt"], pdf_bytes, models=models, source=source, doc_type="lab"
    )

    patient = data.get("patient") or {}
    results = data.get("results") or []

    doc_verdict = validator.check_document(
        patient=patient,
        expected_person=person,
        expected_date=expected_date,
        text_layer=text,
        max_drift_days=config["validation"]["collection_date_max_drift_days"],
    )

    extraction = Extraction(
        person=person,
        source=source,
        model=model,
        patient=patient,
        doc_verdict=doc_verdict,
    )

    if not doc_verdict.ok:
        # The document is not who it claims to be. Quarantine everything on it.
        for r in results:
            extraction.quarantined.append({**r, "reasons": doc_verdict.hard})
        return extraction

    for r in results:
        verdict = validator.check_result(r, text)
        row = {**r, "soft": verdict.soft}
        if verdict.ok:
            extraction.passed.append(row)
        else:
            extraction.quarantined.append({**row, "reasons": verdict.hard})

    return extraction


@dataclass
class Discharge:
    person: str
    source: str
    model: str
    patient: dict[str, str]
    encounter: dict[str, Any]
    medications: list[dict[str, Any]]
    doc_verdict: validator.Verdict
    text_layer: bool

    @property
    def usable(self) -> bool:
        return self.doc_verdict.ok


def extract_discharge(
    pdf_bytes: bytes,
    person: str,
    source: str,
    expected_date: Optional[datetime.date] = None,
    models: Optional[list[str]] = None,
) -> Discharge:
    """Extract a discharge summary.

    The patient check matters more here than anywhere else in the system. Some
    documents are filed under the wrong person: a mother's surgical and delivery
    summaries can sit in the CHILD's folder, because that folder is organised
    around the pregnancy rather than around the patient. A discharge summary
    carries the medication list, so filing it against the wrong person puts one
    person's drugs in another person's record.
    """
    with open(DISCHARGE_CONFIG, encoding="utf-8") as f:
        config = json.load(f)

    text = text_layer(pdf_bytes)
    data, model = call_with_pdf(
        config["prompt"], pdf_bytes, models=models, source=source, doc_type="discharge"
    )

    patient = data.get("patient") or {}
    verdict = validator.check_document(
        patient=patient,
        expected_person=person,
        expected_date=expected_date,
        text_layer=text,
        max_drift_days=config["validation"]["collection_date_max_drift_days"],
    )

    return Discharge(
        person=person,
        source=source,
        model=model,
        patient=patient,
        encounter=data.get("encounter") or {},
        medications=data.get("medications") or [],
        doc_verdict=verdict,
        text_layer=bool(text.strip()),
    )


CLASSIFY_CONFIG = "data/configs/classify.json"
PRESCRIPTION_CONFIG = "data/configs/prescription.json"
RADIOLOGY_CONFIG = "data/configs/radiology.json"


def is_encrypted(pdf_bytes: bytes) -> bool:
    """Is this PDF password-protected?

    Some insurance policies are. Sending one to a model wastes two API calls and
    returns an error, so check first. gajana keeps statement passwords in
    secrets/passwords.json and decrypts; nothing here needs that yet -- these are
    policy documents, not medical records.
    """
    try:
        return bool(PdfReader(io.BytesIO(pdf_bytes)).is_encrypted)
    except Exception:
        return False


def first_page(pdf_bytes: bytes) -> bytes:
    """Just page 1. Classification does not need the whole document, and a 6 MB
    scan costs real money and real seconds to send."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if len(reader.pages) <= 1:
            return pdf_bytes
        writer = PdfWriter()
        writer.add_page(reader.pages[0])
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()
    except Exception:
        return pdf_bytes


def classify(
    pdf_bytes: bytes, source: str = "", models: Optional[list[str]] = None
) -> dict[str, Any]:
    """What KIND of document is this? Read the page; don't guess from the name.

    The folder says WHO the patient is. It does not say what the document IS: a
    lab report sits under a specialty folder, a prescription is titled with the
    doctor's name. Routing on the filename silently skipped hundreds of them.
    """
    with open(CLASSIFY_CONFIG, encoding="utf-8") as f:
        prompt = json.load(f)["prompt"]

    data, model = call_with_pdf(
        prompt,
        first_page(pdf_bytes),
        models=models,
        source=source or None,
        doc_type="classify",
    )
    return {
        "doc_type": (data.get("doc_type") or "other").strip().lower(),
        "confidence": (data.get("confidence") or "low").strip().lower(),
        "has_medications": bool(data.get("has_medications")),
        "reason": (data.get("reason") or "").strip(),
        "model": model,
    }


@dataclass
class Prescription:
    person: str
    source: str
    model: str
    patient: dict[str, str]
    consultation: dict[str, Any]
    medications: list[dict[str, Any]]
    doc_verdict: validator.Verdict
    oracle_source: str

    @property
    def usable(self) -> bool:
        return self.doc_verdict.ok


def extract_prescription(
    pdf_bytes: bytes,
    person: str,
    source: str,
    ocr_text: Optional[str] = None,
    expected_date: Optional[datetime.date] = None,
    models: Optional[list[str]] = None,
) -> Prescription:
    """Extract a consultation / prescription.

    Consultations are where medicines CHANGE, and most of them are scans with no
    text layer -- so the model's output is checked against Paperless' Tesseract
    OCR instead (`ocr_text`), an independent engine reading the same pixels. A
    drug the model reports but the oracle never saw does not get committed.
    """
    from src import oracle as oracle_mod

    with open(PRESCRIPTION_CONFIG, encoding="utf-8") as f:
        config = json.load(f)

    text = text_layer(pdf_bytes)
    oracle = oracle_mod.build(text, ocr_text)
    data, model = call_with_pdf(
        config["prompt"], pdf_bytes, models=models, source=source, doc_type="prescription"
    )

    patient = data.get("patient") or {}
    verdict = validator.check_document(
        patient=patient,
        expected_person=person,
        expected_date=expected_date,
        text_layer=oracle.text,
        max_drift_days=config["validation"]["collection_date_max_drift_days"],
    )

    # Corroborate every drug NAME against the independent reading.
    meds = []
    for m in data.get("medications") or []:
        drug = (m.get("drug") or "").strip()
        if not drug:
            continue
        meds.append({**m, "corroborated": oracle.corroborates(drug)})

    return Prescription(
        person=person,
        source=source,
        model=model,
        patient=patient,
        consultation=data.get("consultation") or {},
        medications=meds,
        doc_verdict=verdict,
        oracle_source=oracle.source,
    )


@dataclass
class Radiology:
    person: str
    source: str
    model: str
    patient: dict[str, str]
    study: dict[str, str]
    measurements: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    doc_verdict: validator.Verdict
    oracle_source: str

    @property
    def usable(self) -> bool:
        return self.doc_verdict.ok

    @property
    def impression(self) -> Optional[str]:
        """The radiologist's own conclusion, if the report printed one.

        Deliberately returns None rather than falling back to the last finding.
        A descriptive line is not a conclusion, and promoting one to an
        impression would put words in the radiologist's mouth.
        """
        for f in self.findings:
            if f.get("is_impression") and (f.get("text") or "").strip():
                return str(f["text"]).strip()
        return None


def _merge_studies(data: Any) -> dict[str, Any]:
    """One PDF can hold SEVERAL studies. Fold them into one result.

    A health-checkup pack is an echo AND an abdominal ultrasound AND a chest film,
    stapled together. An endoscopy report is an OGD AND a colonoscopy. Asked to
    describe "the study", the model correctly returns a LIST -- one object per
    study -- because that is what is on the page. Assuming a single object crashed
    on exactly those documents.

    Merging must not lose which study a row came from: an "AO" of 24mm under the
    echo and a "diameter" under the ultrasound are not in the same examination, and
    a section is part of a row's identity (see the observations UNIQUE key). So each
    row that carries no section of its own inherits its OWN study's name -- not the
    first study's, which would file every finding under the wrong examination.
    """
    if isinstance(data, dict):
        return data
    if not isinstance(data, list):
        return {}

    studies = [s for s in data if isinstance(s, dict)]
    if not studies:
        return {}

    merged: dict[str, Any] = {
        "patient": next((s.get("patient") for s in studies if s.get("patient")), {}),
        "study": studies[0].get("study") or {},
        "measurements": [],
        "findings": [],
    }

    for s in studies:
        st = s.get("study") or {}
        name = str(st.get("name") or st.get("modality") or "").strip()
        for key in ("measurements", "findings"):
            for row in s.get(key) or []:
                if not isinstance(row, dict):
                    continue
                if not (row.get("section") or "").strip():
                    row = {**row, "section": name}
                merged[key].append(row)

    return merged


def extract_radiology(
    pdf_bytes: bytes,
    person: str,
    source: str,
    ocr_text: Optional[str] = None,
    expected_date: Optional[datetime.date] = None,
    models: Optional[list[str]] = None,
) -> Radiology:
    """Extract an imaging report: X-ray, USG, Doppler, CT, MRI, echo, TVS.

    An imaging report carries two different kinds of fact and they are checked
    two different ways.

    A MEASUREMENT is a number, and a number is corroborated the way every other
    number in this system is: it must appear, exactly, in an independent reading
    of the same page.

    A FINDING is prose, and prose cannot survive that test. One garbled character
    in a 40-word paragraph breaks an exact match, and Tesseract garbles something
    in nearly every paragraph -- so demanding one would quarantine every report,
    which is indistinguishable from never reading them. Prose is corroborated by
    word coverage instead (see oracle.coverage): a transcribed impression scores
    near 1.0 even through bad OCR, an invented one scores near 0.0.

    Neither check runs at all without an oracle. No independent reading, no
    trust -- the whole document goes to review, as everywhere else.
    """
    from src import oracle as oracle_mod

    with open(RADIOLOGY_CONFIG, encoding="utf-8") as f:
        config = json.load(f)

    text = text_layer(pdf_bytes)
    oracle = oracle_mod.build(text, ocr_text)
    data, model = call_with_pdf(
        config["prompt"], pdf_bytes, models=models, source=source, doc_type="radiology"
    )

    data = _merge_studies(data)

    patient = data.get("patient") or {}
    verdict = validator.check_document(
        patient=patient,
        expected_person=person,
        expected_date=expected_date,
        text_layer=oracle.text,
        max_drift_days=config["validation"]["collection_date_max_drift_days"],
    )

    measurements = []
    for m in data.get("measurements") or []:
        value = str(m.get("value") or "").strip()
        name = str(m.get("name") or "").strip()
        if not value or not name:
            continue
        # Name AND value, together and adjacent -- see Oracle.corroborates_measurement.
        # Checking the bare value would mark every two-digit number on an echo
        # unverifiable, which is most of them.
        measurements.append({**m, "corroborated": oracle.corroborates_measurement(name, value)})

    threshold = float(config["validation"]["prose_min_coverage"])
    findings = []
    for f in data.get("findings") or []:
        prose = str(f.get("text") or "").strip()
        if not prose:
            continue
        score = oracle.coverage(prose)
        findings.append({**f, "coverage": score, "corroborated": score >= threshold})

    return Radiology(
        person=person,
        source=source,
        model=model,
        patient=patient,
        study=data.get("study") or {},
        measurements=measurements,
        findings=findings,
        doc_verdict=verdict,
        oracle_source=oracle.source,
    )

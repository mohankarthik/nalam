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

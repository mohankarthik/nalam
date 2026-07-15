"""ingest_document() routing precedence -- offline, no LLM, no DB writes.

This is the single dispatch nightly (run_extract.py) and on-demand
(run_extract_queue.py) both call, so a routing bug here is a bug in both
places at once -- see docs/telegram_ingest_queue.md. Every ingest_* / classify
call is monkeypatched: this test is about WHICH branch fires, not what any
extractor does once it fires (that's covered elsewhere, e.g. test_radiology.py,
test_golden.py).
"""

from __future__ import annotations

from typing import Any

import pytest

from src import ingest
from src.drive_sync import Doc


def _doc(*, tag: str = "Medical/General", title: str = "Consultation", suffix: str = ".pdf") -> Doc:
    return Doc(
        path=f"/tmp/{title}{suffix}",
        rel=f"Alice/{title}{suffix}",
        person="Alice",
        correspondent="Alice Example",
        tag=tag,
        title=title,
        created="2026-01-01",
        suffix=suffix,
        key="k",
    )


@pytest.fixture(autouse=True)
def stub_extractors(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {}

    def fake_ingest_lab(con, rel_path, subject, doc_date=None, paperless_id=None):
        calls["branch"] = "lab"
        return 3, 1

    def fake_ingest_discharge(con, rel_path, subject, doc_date=None, paperless_id=None):
        calls["branch"] = "discharge"
        return 2, 1, None

    def fake_ingest_prescription(
        con, rel_path, subject, ocr_text=None, doc_date=None, paperless_id=None
    ):
        calls["branch"] = "prescription"
        return 4, 0, None

    def fake_classify(pdf_bytes, source="", models=None):
        calls["branch"] = "classify"
        return {"doc_type": calls.get("classify_as", "radiology")}

    monkeypatch.setattr(ingest, "ingest_lab", fake_ingest_lab)
    monkeypatch.setattr(ingest, "ingest_discharge", fake_ingest_discharge)
    monkeypatch.setattr(ingest, "ingest_prescription", fake_ingest_prescription)
    monkeypatch.setattr("src.extractor.classify", fake_classify)
    monkeypatch.setattr("src.extractor.is_encrypted", lambda pdf: False)

    # Only fake the PDF read (ingest_document's own `open(path, "rb")`) --
    # is_lab()/is_discharge() do their OWN real `open()` on the routing JSON
    # configs, and a blanket patch would feed them a fake PDF byte string
    # instead of JSON and break routing itself, not just the extraction.
    real_open = open

    def fake_open(path: Any, mode: str = "r", *a: Any, **k: Any) -> Any:
        if "b" in mode:
            return _FakeFile()
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr("builtins.open", fake_open)
    return calls


class _FakeFile:
    def __enter__(self) -> "_FakeFile":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def read(self) -> bytes:
        return b"%PDF-fake"


class TestRouting:
    def test_lab_wins_on_tag(self, stub_extractors: dict[str, Any]) -> None:
        doc = _doc(tag="Medical/Reports", title="Random Visit")
        result = ingest.ingest_document(None, doc)
        assert stub_extractors["branch"] == "lab"
        assert result == {"doc_type": "lab", "committed": 3, "review": 1}

    def test_lab_wins_on_title_keyword(self, stub_extractors: dict[str, Any]) -> None:
        doc = _doc(tag="Medical/General", title="2026-01-01 - CBC Report")
        result = ingest.ingest_document(None, doc)
        assert stub_extractors["branch"] == "lab"
        assert result["doc_type"] == "lab"

    def test_discharge_wins_on_title_when_not_a_lab(self, stub_extractors: dict[str, Any]) -> None:
        doc = _doc(tag="Medical/General", title="Discharge Summary")
        result = ingest.ingest_document(None, doc)
        assert stub_extractors["branch"] == "discharge"
        assert result == {
            "doc_type": "discharge",
            "medications": 2,
            "encounters": 1,
            "misfiled": None,
        }

    def test_falls_through_to_classify_then_prescription(
        self, stub_extractors: dict[str, Any]
    ) -> None:
        stub_extractors["classify_as"] = "prescription"
        doc = _doc(tag="Medical/General", title="Dr Smith Visit")
        result = ingest.ingest_document(None, doc)
        assert stub_extractors["branch"] == "prescription"
        assert result == {
            "doc_type": "prescription",
            "medications": 4,
            "uncorroborated": 0,
            "misfiled": None,
        }

    def test_falls_through_to_classify_then_discharge(
        self, stub_extractors: dict[str, Any]
    ) -> None:
        """A discharge summary titled by its admission, not the word 'discharge'
        (e.g. 'Hyponatremia.pdf' under an Admissions folder), misses the free
        is_discharge() title heuristic and must still route through
        ingest_discharge() via classify() -- not fall into the generic
        "no extractor" bucket, which crashed run_extract_queue.py in production
        (KeyError formatting a result with no 'medications' key)."""
        stub_extractors["classify_as"] = "discharge"
        doc = _doc(tag="Medical/Admissions", title="Hyponatremia")
        result = ingest.ingest_document(None, doc)
        assert stub_extractors["branch"] == "discharge"
        assert result == {
            "doc_type": "discharge",
            "medications": 2,
            "encounters": 1,
            "misfiled": None,
        }

    def test_falls_through_to_classify_then_lab(self, stub_extractors: dict[str, Any]) -> None:
        stub_extractors["classify_as"] = "lab"
        doc = _doc(tag="Medical/Admissions", title="Sodium Levels")
        result = ingest.ingest_document(None, doc)
        assert stub_extractors["branch"] == "lab"
        assert result == {"doc_type": "lab", "committed": 3, "review": 1}

    def test_classified_as_something_unsupported_is_reported_not_dropped(
        self, stub_extractors: dict[str, Any]
    ) -> None:
        stub_extractors["classify_as"] = "insurance"
        doc = _doc(tag="Medical/General", title="Dr Smith Visit")
        result = ingest.ingest_document(None, doc)
        assert result["doc_type"] == "insurance"
        assert "no extractor" in result["note"]

    def test_non_pdf_is_reported_not_dropped(self, stub_extractors: dict[str, Any]) -> None:
        doc = _doc(suffix=".jpg", title="photo")
        result = ingest.ingest_document(None, doc)
        assert result == {"doc_type": "unsupported", "note": "not a PDF"}
        assert "branch" not in stub_extractors  # never even tried to extract

    def test_encrypted_pdf_is_reported_not_dropped(
        self, stub_extractors: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("src.extractor.is_encrypted", lambda pdf: True)
        doc = _doc(tag="Medical/General", title="Policy Document")
        result = ingest.ingest_document(None, doc)
        assert result["doc_type"] == "encrypted"
        assert "branch" not in stub_extractors

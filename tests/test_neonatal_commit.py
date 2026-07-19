"""A newborn's own lab and discharge must COMMIT, not go to review.

The regression this locks down: ingest_lab() and ingest_discharge() used to gate
the commit on check_document()/`usable`, which only compares the printed name to
the folder. A neonatal chart is labelled "B/O <mother>", so that check fails --
and a premature infant's entire NICU and admission record (his own hospitalizations)
was refused and committed to nobody, while the same documents routed through the
prescription and radiology paths committed fine. Both paths now ask the one
authoritative rule, resolve_patient(), exactly as those two already did.

Offline: extract_* is faked, so this is about the commit gate, not extraction.
"""

from __future__ import annotations

import pytest

from src import db, ingest
from src.extractor import Discharge, Extraction
from src.people import Person
from src.validator import Verdict


@pytest.fixture
def con():
    return db.connect(":memory:")


@pytest.fixture(autouse=True)
def family(monkeypatch: pytest.MonkeyPatch):
    """An invented family with a child. The real one is in gitignored people.json."""
    people = {
        "Alice Doe": Person(folder="Alice", correspondent="Alice Doe", sex="female"),
        "Bob Example": Person(folder="Bob", correspondent="Bob Example", sex="male"),
        "Baby Doe": Person(folder="Baby", correspondent="Baby Doe", child=True, born="2024-07-23"),
    }
    monkeypatch.setattr("src.people.load_people", lambda: people)
    monkeypatch.setattr("src.people.shared_name_tokens", lambda: {"doe"})
    # ingest_*/resolve_patient still open() the real PDF before extract_* is faked.
    real_open = open

    def fake_open(path, mode="r", *a, **k):
        if "b" in mode:
            import io

            return io.BytesIO(b"%PDF-fake")
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr("builtins.open", fake_open)


# The name-vs-folder check FAILS (the baby is named for his mother) -- exactly the
# verdict that used to veto the commit.
_NEONATAL_FAIL = Verdict(
    ok=False,
    hard=["patient on report ('B/O ALICE DOE') does not match the folder ('Baby Doe')"],
    soft=[],
)


def test_a_newborns_lab_commits_under_the_baby(con, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        ingest,
        "extract_lab",
        lambda *a, **k: Extraction(
            person="Baby Doe",
            source="Baby/screen.pdf",
            model="test",
            patient={"name": "B/O ALICE DOE", "age": "3 Days"},
            passed=[],
            quarantined=[],
            doc_verdict=_NEONATAL_FAIL,
        ),
    )
    committed, queued = ingest.ingest_lab(con, "Baby/screen.pdf", "Baby Doe")
    assert queued == 0, "a newborn's own lab was refused for being labelled 'B/O <mother>'"
    row = con.execute("SELECT subject FROM documents").fetchone()
    assert row["subject"] == "Baby Doe"
    assert con.execute("SELECT count(*) FROM review_queue").fetchone()[0] == 0


def test_a_newborns_discharge_commits_the_encounter_under_the_baby(
    con, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "src.extractor.extract_discharge",
        lambda *a, **k: Discharge(
            person="Baby Doe",
            source="Baby/nicu.pdf",
            model="test",
            patient={"name": "B/O ALICE DOE", "age": "3 Days"},
            encounter={
                "hospital": "Miracle Children's",
                "admitted": "2024-09-06",
                "discharged": "2024-09-20",
                "diagnoses": ["Prematurity"],
            },
            medications=[],
            doc_verdict=_NEONATAL_FAIL,
            text_layer=True,
        ),
    )
    meds, encs, misfiled = ingest.ingest_discharge(con, "Baby/nicu.pdf", "Baby Doe")
    assert encs == 1, "a newborn's own NICU discharge produced no encounter"
    assert misfiled is None
    row = con.execute("SELECT subject FROM encounters").fetchone()
    assert row["subject"] == "Baby Doe"
    assert con.execute("SELECT count(*) FROM review_queue").fetchone()[0] == 0


def test_a_strangers_discharge_still_commits_nothing(con, monkeypatch: pytest.MonkeyPatch):
    """The one case that must still block: a name we cannot place in this family."""
    monkeypatch.setattr(
        "src.extractor.extract_discharge",
        lambda *a, **k: Discharge(
            person="Baby Doe",
            source="Baby/stranger.pdf",
            model="test",
            patient={"name": "Someone Unrelated", "age": "45 Yr(s)"},
            encounter={"admitted": "2024-09-06", "diagnoses": ["X"]},
            medications=[],
            doc_verdict=Verdict(ok=False, hard=["stranger"], soft=[]),
            text_layer=True,
        ),
    )
    meds, encs, misfiled = ingest.ingest_discharge(con, "Baby/stranger.pdf", "Baby Doe")
    assert encs == 0
    assert con.execute("SELECT count(*) FROM encounters").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM review_queue").fetchone()[0] == 1

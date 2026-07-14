"""Tool functions behind Telegram Q&A (src/qa.py).

No LLM calls here -- these are pure lookups over health.db, tested the same
offline way src/meds.py is. The tool-calling loop itself (answer_question) is
smoke-tested manually, not in the regression suite, same treatment Gemini gets
elsewhere (see docs/telegram_qa.md).
"""

from __future__ import annotations

import sqlite3

import pytest

from src import db, meds, qa
from src.people import Person


@pytest.fixture()
def con() -> sqlite3.Connection:
    return db.connect(":memory:")


def _doc(con, subject="Alice Example", paperless_id=None, doc_type="lab", **kw) -> int:
    return db.upsert_document(
        con,
        subject=subject,
        source_path=kw.pop("source_path", f"{subject}-{paperless_id}.pdf"),
        doc_type=doc_type,
        doc_date=kw.pop("doc_date", "2025-01-01"),
        model="test",
        text_layer=True,
        paperless_id=paperless_id,
        **kw,
    )


class TestDocLink:
    def test_links_to_the_viewer(self, con) -> None:
        doc_id = _doc(con, paperless_id=42)
        link = qa.doc_link(con, doc_id)
        assert link is not None
        assert link.endswith("/documents/42/details")

    def test_no_paperless_id_yields_no_link(self, con) -> None:
        doc_id = _doc(con, paperless_id=None)
        assert qa.doc_link(con, doc_id) is None

    def test_no_document_yields_no_link(self, con) -> None:
        assert qa.doc_link(con, None) is None
        assert qa.doc_link(con, 999999) is None


class TestExtractPerson:
    @pytest.fixture(autouse=True)
    def people(self, monkeypatch: pytest.MonkeyPatch) -> None:
        directory = {
            "Alice Example": Person(
                "Alice", "Alice Example", "female", False, aliases=("wife", "mom")
            ),
            "Bob Example": Person("Bob", "Bob Example", "male", False, aliases=("dad",)),
        }
        monkeypatch.setattr(qa, "load_people", lambda: directory)

    def test_resolves_by_alias(self) -> None:
        person, msg = qa.extract_person("what's dad on for BP?")
        assert msg == ""
        assert person.correspondent == "Bob Example"

    def test_resolves_by_correspondent_name(self) -> None:
        person, msg = qa.extract_person("Alice Example's latest HbA1c")
        assert person.correspondent == "Alice Example"

    def test_no_name_asks_rather_than_guesses(self) -> None:
        person, msg = qa.extract_person("what's the latest HbA1c?")
        assert person is None
        assert "Who is this about" in msg

    def test_two_names_asks_which_one(self) -> None:
        person, msg = qa.extract_person("compare mom and dad's HbA1c")
        assert person is None
        assert "Which one" in msg


class TestListMedications:
    def test_surfaces_confirmed_and_stale_on_every_row(self, con) -> None:
        doc_id = _doc(con, doc_type="prescription", paperless_id=7)
        meds.record_decision(
            con,
            subject="Alice Example",
            drug="Atorvastatin",
            event="prescribed",
            effective="2022-01-01",
            document_id=doc_id,
            strength="10mg",
            frequency="0-0-1",
        )
        rows = qa.list_medications(con, "Alice Example")
        assert len(rows) == 1
        row = rows[0]
        assert row["medicine"].startswith("Atorvastatin")
        assert row["stale"] is True, "last heard about in 2022, well before the 2024 cutoff"
        assert row["document_id"] == doc_id

    def test_confirmed_ok_row_is_not_flagged_stale(self, con) -> None:
        doc_id = _doc(con, doc_type="prescription", paperless_id=8)
        meds.record_decision(
            con,
            subject="Alice Example",
            drug="Metformin",
            event="prescribed",
            effective="2025-06-01",
            document_id=doc_id,
            strength="500mg",
            frequency="1-0-1",
        )
        row = qa.list_medications(con, "Alice Example")[0]
        assert row["confirmed"] is True
        assert row["stale"] is False


class TestGetObservations:
    def _insert(self, con, doc_id, **kw) -> None:
        db.insert_observations(
            con,
            doc_id,
            [
                {
                    "subject": "Alice Example",
                    "printed_name": kw.get("printed_name", "HbA1c"),
                    "analyte": kw.get("analyte", "HbA1c"),
                    "effective": kw["effective"],
                    "value_num": kw.get("value_num", 5.6),
                    "raw_value": str(kw.get("value_num", 5.6)),
                    "unit": "%",
                }
            ],
        )

    def test_filters_by_analyte_and_orders_latest_first(self, con) -> None:
        doc1 = _doc(con, paperless_id=1, source_path="a.pdf", doc_date="2023-01-01")
        doc2 = _doc(con, paperless_id=2, source_path="b.pdf", doc_date="2024-01-01")
        self._insert(con, doc1, effective="2023-01-01", value_num=5.4)
        self._insert(con, doc2, effective="2024-06-01", value_num=6.1)

        rows = qa.get_observations(con, "Alice Example", analyte="HbA1c")
        assert [r["date"] for r in rows] == ["2024-06-01", "2023-01-01"]
        assert rows[0]["document_id"] == doc2

    def test_since_filters_out_older_values(self, con) -> None:
        doc1 = _doc(con, paperless_id=3, source_path="c.pdf", doc_date="2020-01-01")
        self._insert(con, doc1, effective="2020-01-01")
        rows = qa.get_observations(con, "Alice Example", analyte="HbA1c", since="2024-01-01")
        assert rows == []


class TestGetEncounters:
    def test_returns_document_id_for_citation(self, con) -> None:
        doc_id = _doc(con, doc_type="discharge", paperless_id=9, source_path="d.pdf")
        con.execute(
            """INSERT INTO encounters
                 (document_id, subject, hospital, admitted, discharged, reason,
                  diagnoses, follow_up, follow_up_date)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                doc_id,
                "Alice Example",
                "Some Hospital",
                "2025-03-01",
                "2025-03-03",
                "fever",
                '["dengue"]',
                "review in 1 week",
                "2025-03-10",
            ),
        )
        con.commit()

        rows = qa.get_encounters(con, "Alice Example")
        assert len(rows) == 1
        assert rows[0]["diagnoses"] == ["dengue"]
        assert rows[0]["document_id"] == doc_id

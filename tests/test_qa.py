"""Tool functions behind Telegram Q&A (src/qa.py).

No LLM calls here -- these are pure lookups over health.db, tested the same
offline way src/meds.py is. The tool-calling loop itself (answer_question) is
smoke-tested manually, not in the regression suite, same treatment Gemini gets
elsewhere (see docs/telegram_qa.md).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

import pytest

from src import db, meds, qa, units
from src.people import Person
from tools import migrate_reclassified_units


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
        assert person is not None
        assert person.correspondent == "Bob Example"

    def test_resolves_by_correspondent_name(self) -> None:
        person, msg = qa.extract_person("Alice Example's latest HbA1c")
        assert person is not None
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

    def test_converted_row_displays_canonical_unit_and_scaled_range(self, con) -> None:
        doc1 = _doc(con, paperless_id=4, source_path="d-plt.pdf", doc_date="2026-04-01")
        db.insert_observations(
            con,
            doc1,
            [
                {
                    "subject": "Alice Example",
                    "printed_name": "Platelet Count",
                    "analyte": "Platelet Count",
                    "effective": "2026-04-01",
                    "value_num": 215000.0,
                    "raw_value": "215",
                    "unit": "x10^3/uL",
                    "ref_low": 150.0,
                    "ref_high": 400.0,
                }
            ],
        )
        rows = qa.get_observations(con, "Alice Example", analyte="Platelet Count")
        assert len(rows) == 1
        assert rows[0]["value"] == pytest.approx(215000.0)
        assert rows[0]["unit"] == "/uL"
        assert rows[0]["ref_low"] == pytest.approx(150000.0)
        assert rows[0]["ref_high"] == pytest.approx(400000.0)

    def test_reclassify_converts_numeric_units_and_quarantines_unknown_unit(self, con) -> None:
        doc1 = _doc(con, paperless_id=5, source_path="reclass.pdf", doc_date="2026-04-01")
        db.insert_observations(
            con,
            doc1,
            [
                {
                    "subject": "Alice Example",
                    "printed_name": "PLT",
                    "analyte": None,
                    "effective": "2026-04-01",
                    "value_num": 215.0,
                    "raw_value": "215",
                    "unit": "x10^3/uL",
                    "ref_low": 150.0,
                    "ref_high": 400.0,
                    "status": "review",
                    "review_reason": '["no codebook entry"]',
                },
                {
                    "subject": "Alice Example",
                    "printed_name": "PLT_BAD_UNIT",
                    "analyte": None,
                    "effective": "2026-04-02",
                    "value_num": 215.0,
                    "raw_value": "215",
                    "unit": "IU/mL",
                    "ref_low": 150.0,
                    "ref_high": 400.0,
                    "status": "review",
                    "review_reason": '["no codebook entry"]',
                },
            ],
        )

        def resolver(printed_name: str, _section: str) -> tuple[str | None, str | None]:
            if printed_name in ("PLT", "PLT_BAD_UNIT"):
                return "Platelet Count", "CBC"
            return None, None

        fixed = db.reclassify(con, resolver)
        assert fixed == 1

        good = con.execute(
            "SELECT analyte, value_num, status, review_reason FROM observations "
            "WHERE printed_name = 'PLT'"
        ).fetchone()
        assert good["analyte"] == "Platelet Count"
        assert good["value_num"] == pytest.approx(215000.0)
        assert good["status"] == "ok"
        assert good["review_reason"] is None

        bad = con.execute(
            "SELECT analyte, value_num, status, review_reason FROM observations "
            "WHERE printed_name = 'PLT_BAD_UNIT'"
        ).fetchone()
        assert bad["analyte"] == "Platelet Count"
        assert bad["value_num"] is None
        assert bad["status"] == "review"
        assert "unknown unit" in bad["review_reason"]

        rows = qa.get_observations(con, "Alice Example", analyte="Platelet Count")
        by_date = {r["date"]: r for r in rows}
        assert by_date["2026-04-01"]["value"] == pytest.approx(215000.0)
        assert by_date["2026-04-01"]["unit"] == "/uL"
        assert by_date["2026-04-01"]["ref_low"] == pytest.approx(150000.0)
        assert by_date["2026-04-01"]["ref_high"] == pytest.approx(400000.0)
        assert by_date["2026-04-02"]["value"] is None
        assert by_date["2026-04-02"]["trusted"] is False


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


class TestGetMedicationHistory:
    """list_medications hides an expired 5-day course; history must not --
    this is exactly the gap that made the bot say 'no record' for a course
    that had already ended (see the docstring on get_medication_history)."""

    def test_finds_an_expired_course_hidden_from_list_medications(self, con) -> None:
        doc_id = _doc(con, subject="A Child", doc_type="prescription", paperless_id=11)
        con.execute(
            """INSERT INTO medication_events
                 (document_id, subject, drug, strength, frequency, duration,
                  event, effective, raw_text)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                doc_id,
                "A Child",
                "CETZINE",
                "5mg",
                "0-0-1",
                "5 days",
                "prescribed",
                "2024-03-01",
                "Cetzine 5mg x 5 days",
            ),
        )
        con.commit()

        assert qa.list_medications(con, "A Child") == [], "expired course, correctly hidden"

        rows = qa.get_medication_history(con, "A Child")
        assert len(rows) == 1
        assert rows[0]["medicine"] == "CETZINE"
        assert rows[0]["document_id"] == doc_id

    def test_filters_by_drug(self, con) -> None:
        doc_id = _doc(con, subject="A Child", doc_type="prescription", paperless_id=12)
        meds.record_decision(
            con,
            subject="A Child",
            drug="CETZINE",
            event="prescribed",
            effective="2024-03-01",
            document_id=doc_id,
        )
        assert (
            len(qa.get_medication_history(con, "A Child", drug="cetirizine")) == 0
        ), "record_decision doesn't map a molecule -- brand search still works"
        assert len(qa.get_medication_history(con, "A Child", drug="CETZINE")) == 1


class TestGetMedicationsForCondition:
    def test_finds_medicines_by_free_text_reason(self, con) -> None:
        doc_id = _doc(con, subject="A Child", doc_type="prescription", paperless_id=13)
        con.execute(
            """INSERT INTO encounters (document_id, subject, admitted, reason, diagnoses)
               VALUES (?,?,?,?,?)""",
            (doc_id, "A Child", "2024-03-01", "cold and cough", "[]"),
        )
        con.execute(
            """INSERT INTO medication_events
                 (document_id, subject, drug, event, effective, raw_text)
               VALUES (?,?,?,?,?,?)""",
            (doc_id, "A Child", "Cetzine", "prescribed", "2024-03-01", "Cetzine"),
        )
        con.commit()

        episodes = qa.get_medications_for_condition(con, "A Child", "cold")
        assert len(episodes) == 1
        assert episodes[0]["medications"][0]["medicine"] == "Cetzine"
        assert episodes[0]["document_id"] == doc_id


class TestRunLoopAndFailover:
    def test_run_loop_executes_tools_and_returns_final_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        turns = [
            {
                "ok": True,
                "turn": {
                    "type": "tool_calls",
                    "calls": [{"id": "c1", "name": "list_medications", "arguments": {}}],
                },
                "meta": {"model": "gemini-flash"},
            },
            {
                "ok": True,
                "turn": {"type": "final", "content": "Alice Example takes Metformin."},
                "meta": {"model": "gemini-flash"},
            },
        ]
        seen_families: list[str | None] = []

        def _fake_complete(self, **kwargs):
            seen_families.append(kwargs.get("family"))
            return turns.pop(0)

        monkeypatch.setattr(qa.LLMClient, "complete", _fake_complete)

        called = []
        dispatch = {"list_medications": lambda: called.append(True) or [{"medicine": "Metformin"}]}
        messages = [{"role": "user", "content": "What is Alice Example taking?"}]

        answer = qa._run_loop("gemini", messages, dispatch)
        assert answer == "Alice Example takes Metformin."
        assert called == [True]
        assert seen_families == ["gemini", "gemini"]

    def test_run_loop_catches_tool_loop_error_and_returns_narrower_prompt_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _exhaust(self, *args, **kwargs):
            raise qa.LLMToolLoopError("exceeded max_rounds (4)")

        monkeypatch.setattr(qa.LLMClient, "run_tool_loop", _exhaust)

        out = qa._run_loop("gemini", [{"role": "user", "content": "q"}], {})
        assert out == "That took too many steps to answer -- try asking something narrower."

    def test_answer_question_failover_clears_seen_docs_from_failed_primary_attempt(
        self, con, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        doc_stale = _doc(con, subject="Alice Example", paperless_id=101, source_path="s.pdf")
        doc_good = _doc(con, subject="Alice Example", paperless_id=202, source_path="g.pdf")
        directory = {
            "Alice Example": Person("Alice", "Alice Example", "female", False, aliases=("mom",)),
        }
        monkeypatch.setattr(qa, "load_people", lambda: directory)
        monkeypatch.setattr(qa.db, "connect", lambda *a, **k: con)
        monkeypatch.setattr(qa, "_log_exchange", lambda *a, **k: None)

        def _fake_dispatch(_con, _subject):
            return {
                "stale_tool": lambda: [{"document_id": doc_stale}],
                "good_tool": lambda: [{"document_id": doc_good}],
            }

        monkeypatch.setattr(qa, "_dispatch", _fake_dispatch)

        models_tried: list[str] = []

        def _fake_run_loop(model, messages, dispatch, max_rounds=qa.MAX_TOOL_ROUNDS):
            models_tried.append(model)
            if model == qa.PRIMARY_MODEL:
                dispatch["stale_tool"]()
                raise RuntimeError("primary failed after partial tool call")
            dispatch["good_tool"]()
            return "Fallback answer."

        monkeypatch.setattr(qa, "_run_loop", _fake_run_loop)

        reply = qa.answer_question("what are mom's results?")
        assert models_tried == [qa.PRIMARY_MODEL, qa.FALLBACK_MODEL]
        assert "Fallback answer." in reply
        assert "/documents/202/details" in reply
        assert "/documents/101/details" not in reply

    def test_advise_failover_clears_seen_docs_and_corpus_from_failed_primary_attempt(
        self, con, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        doc_stale = _doc(con, subject="Alice Example", paperless_id=101, source_path="s.pdf")
        doc_good = _doc(con, subject="Alice Example", paperless_id=202, source_path="g.pdf")
        directory = {
            "Alice Example": Person("Alice", "Alice Example", "female", False, aliases=("mom",)),
        }
        monkeypatch.setattr(qa, "load_people", lambda: directory)
        monkeypatch.setattr(qa.db, "connect", lambda *a, **k: con)
        monkeypatch.setattr(qa, "_log_exchange", lambda *a, **k: None)

        def _fake_advisory_dispatch(_con, _subject):
            return {
                "stale_tool": lambda: [{"document_id": doc_stale, "value": 9.4}],
                "good_tool": lambda: [{"document_id": doc_good, "value": 5.8}],
            }

        monkeypatch.setattr(qa, "_advisory_dispatch", _fake_advisory_dispatch)

        def _fake_run_loop(model, messages, dispatch, max_rounds=qa.MAX_TOOL_ROUNDS):
            if model == qa.PRIMARY_MODEL:
                dispatch["stale_tool"]()
                raise RuntimeError("primary failed halfway")
            dispatch["good_tool"]()
            return "Ask about 5.8 and 9.4."

        monkeypatch.setattr(qa, "_run_loop", _fake_run_loop)

        reply = qa.advise("mom prep for visit")
        assert "5.8" in reply
        assert "9.4" not in reply
        assert "[unverified]" in reply
        assert "/documents/202/details" in reply
        assert "/documents/101/details" not in reply

    def test_collecting_wrappers_log_exception_and_reraise(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _boom() -> None:
            raise RuntimeError("broken sqlite query")

        seen: set[int] = set()
        corpus: list[str] = []
        wrapped_basic = qa._collecting(_boom, seen)
        wrapped_corpus = qa._collecting_with_corpus(_boom, seen, corpus)

        with pytest.raises(RuntimeError, match="broken sqlite query"):
            wrapped_basic()
        assert "qa tool failed" in caplog.text

        caplog.clear()
        with pytest.raises(RuntimeError, match="broken sqlite query"):
            wrapped_corpus()
        assert "qa tool failed" in caplog.text


class TestReclassifyHardening:
    def test_reclassify_leaves_ambiguous_and_image_rows_in_review(self, con) -> None:
        doc1 = _doc(con, paperless_id=301, source_path="ambig-img.pdf", doc_date="2026-04-01")
        db.insert_observations(
            con,
            doc1,
            [
                {
                    "subject": "Alice Example",
                    "printed_name": "PLT_AMBIG",
                    "analyte": None,
                    "effective": "2026-04-01",
                    "value_num": 215.0,
                    "raw_value": "215",
                    "unit": "x10^3/uL",
                    "source_quality": "text",
                    "status": "review",
                    "review_reason": '["ambiguous: two results in this report claim this analyte"]',
                },
                {
                    "subject": "Alice Example",
                    "printed_name": "PLT_IMG",
                    "analyte": None,
                    "effective": "2026-04-02",
                    "value_num": 220.0,
                    "raw_value": "220",
                    "unit": "x10^3/uL",
                    "source_quality": "image",
                    "status": "review",
                    "review_reason": '["no codebook entry"]',
                },
                {
                    "subject": "Alice Example",
                    "printed_name": "PLT_IMG_UNIT",
                    "analyte": "Platelet Count",
                    "segment": "CBC",
                    "effective": "2026-04-03",
                    "value_num": None,
                    "raw_value": "230",
                    "unit": "x10^3/uL",
                    "source_quality": "image",
                    "status": "review",
                    "review_reason": "[\"Platelet Count: unknown unit 'foo'\"]",
                },
            ],
        )

        def resolver(printed_name: str, _section: str) -> tuple[str | None, str | None]:
            if printed_name in ("PLT_AMBIG", "PLT_IMG"):
                return "Platelet Count", "CBC"
            return None, None

        fixed = db.reclassify(con, resolver)
        assert fixed == 0

        rows = {
            r["printed_name"]: dict(r)
            for r in con.execute(
                "SELECT printed_name, analyte, value_num, status, review_reason FROM observations"
            ).fetchall()
        }
        assert rows["PLT_AMBIG"]["analyte"] is None
        assert rows["PLT_AMBIG"]["status"] == "review"
        assert "ambiguous" in rows["PLT_AMBIG"]["review_reason"]

        assert rows["PLT_IMG"]["analyte"] is None
        assert rows["PLT_IMG"]["status"] == "review"
        assert "no codebook entry" in rows["PLT_IMG"]["review_reason"]

        assert rows["PLT_IMG_UNIT"]["analyte"] == "Platelet Count"
        assert rows["PLT_IMG_UNIT"]["value_num"] is None
        assert rows["PLT_IMG_UNIT"]["status"] == "review"

    def test_reclassify_retries_and_redeems_unit_refused_row(
        self, con, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        doc1 = _doc(con, paperless_id=302, source_path="unit-retry.pdf", doc_date="2026-04-01")
        db.insert_observations(
            con,
            doc1,
            [
                {
                    "subject": "Alice Example",
                    "printed_name": "Platelet Count",
                    "analyte": "Platelet Count",
                    "segment": "CBC",
                    "effective": "2026-04-01",
                    "value_num": None,
                    "raw_value": "215",
                    "unit": "custom_k/uL",
                    "source_quality": "text",
                    "status": "review",
                    "review_reason": (
                        "[\"Platelet Count: unknown unit 'custom_k/uL' " "(codebook uses '/uL')\"]"
                    ),
                },
                {
                    "subject": "Alice Example",
                    "printed_name": "Platelet Count",
                    "analyte": "Platelet Count",
                    "segment": "CBC",
                    "effective": "2026-04-02",
                    "value_num": None,
                    "raw_value": "210",
                    "unit": "",
                    "source_quality": "text",
                    "status": "review",
                    "review_reason": '["Platelet Count: numeric result with no unit printed"]',
                },
            ],
        )

        extended_units = units.load_units()
        extended_units["Platelet Count"] = {
            "canonical": "/uL",
            "convert": {
                **extended_units["Platelet Count"]["convert"],
                "custom_k/uL": 1000,
            },
        }
        monkeypatch.setattr(units, "load_units", lambda: extended_units)

        fixed = db.reclassify(con, lambda *_a: (None, None))
        assert fixed == 1

        redeemed = con.execute(
            "SELECT analyte, segment, value_num, status, review_reason "
            "FROM observations WHERE effective = '2026-04-01'"
        ).fetchone()
        assert redeemed["analyte"] == "Platelet Count"
        assert redeemed["segment"] == "CBC"
        assert redeemed["value_num"] == pytest.approx(215000.0)
        assert redeemed["status"] == "ok"
        assert redeemed["review_reason"] is None

        still_missing = con.execute(
            "SELECT value_num, status, review_reason "
            "FROM observations WHERE effective = '2026-04-02'"
        ).fetchone()
        assert still_missing["value_num"] is None
        assert still_missing["status"] == "review"
        assert "no unit printed" in still_missing["review_reason"]

    def test_migrate_reclassified_units_dry_run_apply_and_idempotency(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "health.db"
        con = db.connect(str(db_path))
        try:
            doc1 = _doc(
                con,
                subject="Alice Example",
                paperless_id=303,
                source_path="mig.pdf",
                doc_date="2026-04-01",
            )
            db.insert_observations(
                con,
                doc1,
                [
                    {
                        "subject": "Alice Example",
                        "printed_name": "PLT",
                        "analyte": "Platelet Count",
                        "segment": "CBC",
                        "effective": "2026-04-01",
                        "value_num": 215.0,
                        "raw_value": "215",
                        "unit": "x10^3/uL",
                        "status": "ok",
                    },
                    {
                        "subject": "Alice Example",
                        "printed_name": "Platelet Count",
                        "analyte": "Platelet Count",
                        "segment": "CBC",
                        "effective": "2026-04-02",
                        "value_num": 215000.0,
                        "raw_value": "215",
                        "unit": "x10^3/uL",
                        "status": "ok",
                    },
                ],
            )
            con.commit()
        finally:
            con.close()

        migrate_reclassified_units.main([str(db_path), "--dry-run"])
        dry_out = capsys.readouterr().out
        assert "Alice Example" not in dry_out
        assert "1 would convert" in dry_out
        assert "Platelet Count: x10^3/uL -> /uL (1)" in dry_out
        backup_path = tmp_path / "health.db.pre-reclassify-units.bak"
        assert not backup_path.exists()

        con = db.connect(str(db_path))
        try:
            val = con.execute(
                "SELECT value_num FROM observations WHERE effective = '2026-04-01'"
            ).fetchone()["value_num"]
            assert val == pytest.approx(215.0)
        finally:
            con.close()

        migrate_reclassified_units.main([str(db_path)])
        apply_out = capsys.readouterr().out
        assert "Alice Example" not in apply_out
        assert "1 converted" in apply_out
        assert "integrity:   ok" in apply_out
        assert backup_path.exists()
        assert stat.S_IMODE(os.stat(backup_path).st_mode) == 0o600

        con = db.connect(str(db_path))
        try:
            rows = {
                r["effective"]: r["value_num"]
                for r in con.execute("SELECT effective, value_num FROM observations").fetchall()
            }
            assert rows["2026-04-01"] == pytest.approx(215000.0)
            assert rows["2026-04-02"] == pytest.approx(215000.0)
        finally:
            con.close()

        second = migrate_reclassified_units.migrate(str(db_path), dry_run=False)
        second_out = capsys.readouterr().out
        assert "Alice Example" not in second_out
        assert second["converted"] == 0
        assert second["would_convert"] == 0

    def test_nalam_settings_file_override_and_detached_e2e_import(self, tmp_path: Path) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        custom_settings = tmp_path / "settings.json"
        custom_settings.write_text(
            json.dumps(
                {
                    "paperless_url": "http://127.0.0.1:9999",
                    "document_type": "CustomMedical",
                }
            ),
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["NALAM_SETTINGS_FILE"] = str(custom_settings)
        res = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json, src.constants as c; "
                "print(json.dumps({'cfg': c.SETTINGS_CONFIG, 'url': c.PAPERLESS_URL, "
                "'dt': c.DOCUMENT_TYPE}))",
            ],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(res.stdout)
        assert data["cfg"] == str(custom_settings)
        assert data["url"] == "http://127.0.0.1:9999"
        assert data["dt"] == "CustomMedical"

        detached_env = os.environ.copy()
        detached_env.pop("NALAM_SETTINGS_FILE", None)
        detached_env.pop("NALAM_PEOPLE_FILE", None)
        res_e2e = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os, tests.test_e2e_pipeline, src.constants as c; "
                "print(os.environ['NALAM_SETTINGS_FILE'] + '|' + c.SETTINGS_CONFIG)",
            ],
            cwd=repo_root,
            env=detached_env,
            capture_output=True,
            text=True,
            check=True,
        )
        env_path, const_path = res_e2e.stdout.strip().split("|", 1)
        assert env_path.endswith("data/settings.example.json")
        assert const_path.endswith("data/settings.example.json")

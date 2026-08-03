"""The grounded advisory / appointment-prep path (src/qa.advise) and its routing.

Offline and free like the rest of the suite: the LLM tool-loop (_run_loop) is
monkeypatched everywhere, so nothing here makes a network call. What IS exercised for
real is the deterministic machinery -- the grounding guard, person scoping, and the
/advise command routing -- because that is the part that must not be wrong. The factual
path (answer_question) is asserted untouched; its own regressions stay in test_qa.py.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from src import db, qa
from src.people import Person

# --- The grounding guard: a figure the records didn't give us never reaches the reader.


class TestGroundCheck:
    def test_ungrounded_number_is_redacted_and_reported(self) -> None:
        corpus = '[{"analyte": "HbA1c", "value": 7.1, "date": "2025-06-01"}]'
        answer = "Ask about the HbA1c of 8.2% seen on 2019-05-01."
        redacted, ungrounded = qa._ground_check(answer, corpus)
        assert "8.2%" not in redacted
        assert "2019-05-01" not in redacted
        assert redacted.count("[unverified]") == 2
        assert set(ungrounded) == {"8.2%", "2019-05-01"}

    def test_grounded_number_and_date_pass_untouched(self) -> None:
        corpus = '[{"analyte": "HbA1c", "value": 7.1, "date": "2025-06-01"}]'
        answer = "Ask why the HbA1c of 7.1% on 2025-06-01 hasn't improved."
        redacted, ungrounded = qa._ground_check(answer, corpus)
        assert redacted == answer
        assert ungrounded == []

    def test_percentage_is_grounded_by_the_bare_number_in_the_payload(self) -> None:
        # The payload stores 7.1 (a number); the model wrote "7.1%". Same figure.
        corpus = '[{"value": 7.1}]'
        redacted, ungrounded = qa._ground_check("HbA1c is 7.1% now.", corpus)
        assert ungrounded == []
        assert "[unverified]" not in redacted

    def test_bare_prose_integers_are_left_alone(self) -> None:
        # "3 questions" / "2 tests" are prose, not medical facts -- redacting them would
        # mangle ordinary sentences. Only decimals, percentages, years and ISO dates
        # are fenced.
        corpus = "[]"
        answer = "Here are 3 questions and 2 things to read about."
        redacted, ungrounded = qa._ground_check(answer, corpus)
        assert redacted == answer
        assert ungrounded == []

    def test_ungrounded_year_is_caught(self) -> None:
        corpus = '[{"date": "2025-06-01"}]'
        redacted, ungrounded = qa._ground_check("Back in 2011 the scan showed...", corpus)
        assert "2011" not in redacted
        assert ungrounded == ["2011"]


# --- advise(): person scoping is absolute (the correspondent-is-the-patient rule).


@pytest.fixture()
def people(monkeypatch: pytest.MonkeyPatch) -> None:
    directory = {
        "Alice Example": Person("Alice", "Alice Example", "female", False, aliases=("mom",)),
        "Bob Example": Person("Bob", "Bob Example", "male", False, aliases=("dad",)),
    }
    monkeypatch.setattr(qa, "load_people", lambda: directory)


def _seed_two_people(con: sqlite3.Connection) -> None:
    """One HbA1c for Alice, one for Bob, so a scoped fetch can prove it never crosses."""
    for subject, analyte, val in (("Alice Example", "HbA1c", 7.1), ("Bob Example", "CBC", 5.0)):
        doc_id = db.upsert_document(
            con,
            subject=subject,
            source_path=f"{subject}.pdf",
            doc_type="lab",
            doc_date="2025-01-01",
            model="test",
            text_layer=True,
            paperless_id=None,
        )
        db.insert_observations(
            con,
            doc_id,
            [
                {
                    "subject": subject,
                    "printed_name": analyte,
                    "analyte": analyte,
                    "effective": "2025-01-01",
                    "value_num": val,
                    "raw_value": str(val),
                    "unit": "%",
                }
            ],
        )
    con.commit()


class TestAdvisePersonScoping:
    def test_unresolved_subject_asks_rather_than_guessing(
        self, people: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*a: Any, **k: Any) -> str:
            raise AssertionError("the model loop must not run without a resolved subject")

        monkeypatch.setattr(qa, "_run_loop", _boom)
        out = qa.advise("what should I ask the doctor?")
        assert "Who is this about" in out

    def test_dispatch_is_bound_to_the_named_person_only(
        self, people: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        con = db.connect(":memory:")
        _seed_two_people(con)
        monkeypatch.setattr(qa.db, "connect", lambda *a, **k: con)
        monkeypatch.setattr(qa, "configure_api_keys", lambda: None)

        captured: dict[str, Any] = {}

        def fake_loop(model, messages, dispatch, max_rounds=qa.MAX_TOOL_ROUNDS):
            # A real advisory run gathers labs; do the same so we can prove the fetch
            # is scoped to Bob and never returns Alice's row.
            captured["rows"] = dispatch["get_observations"]()
            captured["system"] = messages[0]["content"]
            captured["max_rounds"] = max_rounds
            return "Questions to ask: why is the CBC where it is?"

        monkeypatch.setattr(qa, "_run_loop", fake_loop)

        out = qa.advise("dad, prepare for the review")
        analytes = {r["analyte"] for r in captured["rows"]}
        assert analytes == {"CBC"}, "Bob's fetch must not include Alice's HbA1c"
        assert "Bob Example" in captured["system"]
        assert captured["max_rounds"] == qa.ADVISORY_MAX_TOOL_ROUNDS
        assert out.startswith("⚕️ Appointment prep for Bob Example")
        assert "not medical advice" in out

    def test_output_redacts_a_hallucinated_figure_end_to_end(
        self, people: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        con = db.connect(":memory:")
        _seed_two_people(con)
        monkeypatch.setattr(qa.db, "connect", lambda *a, **k: con)
        monkeypatch.setattr(qa, "configure_api_keys", lambda: None)

        def fake_loop(model, messages, dispatch, max_rounds=qa.MAX_TOOL_ROUNDS):
            dispatch["get_observations"]()  # populate the grounding corpus
            # 7.1 is real (in the corpus); 9.9% is invented and must be redacted.
            return "Ask about the HbA1c of 7.1 and the alarming 9.9% figure."

        monkeypatch.setattr(qa, "_run_loop", fake_loop)

        out = qa.advise("mom, prep for endocrinology")
        assert "7.1" in out
        assert "9.9%" not in out
        assert "[unverified]" in out
        assert "1 figure removed" in out


# --- The factual path is not weakened by any of the above.


class TestFactualPathUnchanged:
    def test_constants_and_prompt_are_untouched(self) -> None:
        assert qa.MAX_TOOL_ROUNDS == 4
        assert qa.OBSERVATION_LIMIT == 10
        assert "never your own medical knowledge" in qa.SYSTEM_PROMPT
        assert len(qa.TOOLS) == 6

    def test_run_loop_still_defaults_to_the_factual_round_budget(self) -> None:
        import inspect

        default = inspect.signature(qa._run_loop).parameters["max_rounds"].default
        assert default == qa.MAX_TOOL_ROUNDS


# --- Long replies are chunked under Telegram's 4096-char limit, not silently dropped.


class TestMessageChunking:
    def test_long_reply_is_split_under_the_limit(self) -> None:
        from plugins.telegram_bot import bot as tgbot

        text = "\n".join(f"line {i} " + "x" * 50 for i in range(400))  # well over 4096
        pieces = tgbot._chunk(text)
        assert len(pieces) > 1
        assert all(len(p) <= tgbot.TELEGRAM_MAX_CHARS for p in pieces)
        # No content lost and line boundaries preserved (rejoining restores the original).
        assert "\n".join(pieces) == text

    def test_short_reply_stays_one_message(self) -> None:
        from plugins.telegram_bot import bot as tgbot

        assert tgbot._chunk("just a short answer") == ["just a short answer"]

    def test_a_single_overlong_line_is_hard_split(self) -> None:
        from plugins.telegram_bot import bot as tgbot

        line = "y" * (tgbot.TELEGRAM_MAX_CHARS * 2 + 5)
        pieces = tgbot._chunk(line)
        assert all(len(p) <= tgbot.TELEGRAM_MAX_CHARS for p in pieces)
        assert "".join(pieces) == line

    def test_send_message_posts_each_chunk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from plugins.telegram_bot import bot as tgbot

        posted: list[str] = []
        monkeypatch.setattr(
            tgbot, "_post_message", lambda token, chat_id, text: posted.append(text)
        )
        long_text = "\n".join("z" * 100 for _ in range(200))
        tgbot.send_message("tok", 1, long_text)
        assert len(posted) > 1
        assert all(len(p) <= tgbot.TELEGRAM_MAX_CHARS for p in posted)


# --- /advise command routing (plugins/telegram_bot/bot.py).


class TestBotRouting:
    @pytest.fixture()
    def bot_and_sent(self, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[str]]:
        from plugins.telegram_bot import bot as tgbot

        instance = object.__new__(tgbot.TelegramDocBot)  # skip __init__ (Paperless/network)
        sent: list[str] = []
        monkeypatch.setattr(instance, "send_message", lambda chat_id, text: sent.append(text))
        return instance, sent

    def test_advise_command_routes_to_advise(
        self, bot_and_sent: tuple[Any, list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from plugins.telegram_bot import bot as tgbot

        seen: dict[str, str] = {}
        monkeypatch.setattr(
            tgbot.qa, "advise", lambda text: seen.__setitem__("advise", text) or "OK"
        )
        monkeypatch.setattr(
            tgbot.qa, "answer_question", lambda text: seen.__setitem__("factual", text) or "NO"
        )
        instance, sent = bot_and_sent

        instance.process_message({"chat": {"id": 1}, "text": "/advise dad prep for cardiology"})
        assert seen.get("advise") == "dad prep for cardiology"
        assert "factual" not in seen
        assert sent == ["OK"]

    def test_advise_with_bot_suffix_still_routes(
        self, bot_and_sent: tuple[Any, list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from plugins.telegram_bot import bot as tgbot

        seen: dict[str, str] = {}
        monkeypatch.setattr(
            tgbot.qa, "advise", lambda text: seen.__setitem__("advise", text) or "OK"
        )
        instance, sent = bot_and_sent

        instance.process_message({"chat": {"id": 1}, "text": "/advise@nalambot mom labs"})
        assert seen.get("advise") == "mom labs"

    def test_bare_question_routes_to_factual(
        self, bot_and_sent: tuple[Any, list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from plugins.telegram_bot import bot as tgbot

        seen: dict[str, str] = {}
        monkeypatch.setattr(
            tgbot.qa, "advise", lambda text: seen.__setitem__("advise", text) or "OK"
        )
        monkeypatch.setattr(
            tgbot.qa, "answer_question", lambda text: seen.__setitem__("factual", text) or "ANS"
        )
        instance, sent = bot_and_sent

        instance.process_message({"chat": {"id": 1}, "text": "what is dad's latest HbA1c?"})
        assert seen.get("factual") == "what is dad's latest HbA1c?"
        assert "advise" not in seen
        assert sent == ["ANS"]

    def test_advise_without_a_question_sends_usage_and_calls_nothing(
        self, bot_and_sent: tuple[Any, list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from plugins.telegram_bot import bot as tgbot

        called: list[str] = []
        monkeypatch.setattr(tgbot.qa, "advise", lambda text: called.append("advise") or "OK")
        monkeypatch.setattr(
            tgbot.qa, "answer_question", lambda text: called.append("factual") or "ANS"
        )
        instance, sent = bot_and_sent

        instance.process_message({"chat": {"id": 1}, "text": "/advise"})
        assert called == []
        assert len(sent) == 1 and "Usage:" in sent[0]

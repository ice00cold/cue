"""The shared /decisions parser: listing, show, answer (numeric + free text),
withdraw, and the resume prompts adapters enqueue."""

import pytest

from hermes_cli.decision_command import (
    answer_decision,
    dispatch_decision_command,
    withdraw_decision,
)
from tools.decisions_store import DecisionsStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = DecisionsStore(tmp_path)
    store.add_decision(
        question="Which database for the job queue?",
        context="Backs the cron jobs refactor.",
        options=[{"label": "Postgres", "impact": "known ops"},
                 {"label": "SQLite", "impact": "zero ops"}],
        recommendation="Postgres", impact="blocks the refactor")
    return store


def _dispatch(store, arg=""):
    return dispatch_decision_command(store, arg)


class TestListing:
    def test_bare_lists_open_with_answer_hint(self, store):
        result = _dispatch(store)
        assert not result.error
        assert "[d1] Which database for the job queue?" in result.output
        assert "recommends: Postgres" in result.output
        assert "/decisions answer" in result.output
        assert result.resume_prompt is None

    def test_empty_queue_says_so(self, tmp_path):
        assert _dispatch(DecisionsStore(tmp_path)).output == "No pending decisions."

    def test_all_appends_closed_history(self, store):
        _dispatch(store, "answer d1 1 cheaper ops")
        result = _dispatch(store, "all")
        assert "No pending decisions." in result.output
        assert "Closed:" in result.output
        assert "d1] Which database for the job queue? → Postgres" in result.output


class TestShow:
    def test_show_prints_numbered_options(self, store):
        output = _dispatch(store, "show d1").output
        assert "1. Postgres — known ops" in output
        assert "2. SQLite — zero ops" in output
        assert "Recommendation: Postgres" in output

    def test_show_unknown_id_errors(self, store):
        result = _dispatch(store, "show d9")
        assert result.error and "No decision with id 'd9'." in result.output


class TestAnswer:
    def test_numeric_answer_resolves_to_the_option_label(self, store):
        result = _dispatch(store, "answer d1 2 cheaper to run")
        assert not result.error
        assert "resolved → SQLite" in result.output
        assert store.get("d1")["status"] == "resolved"

    def test_free_text_answer_stays_verbatim(self, store):
        result = _dispatch(store, "answer d1 neither")
        assert "resolved → neither" in result.output

    def test_rationale_is_everything_after_the_choice(self, store):
        _dispatch(store, "answer d1 1 the team already runs it and backups exist")
        assert store.get("d1")["rationale"] == "the team already runs it and backups exist"

    def test_resume_prompt_carries_question_answer_and_rationale(self, store):
        result = _dispatch(store, "answer d1 2 cheaper to run")
        prompt = result.resume_prompt
        assert "[Decision d1 resolved by the user]" in prompt
        assert "Which database for the job queue?" in prompt
        assert "Answer: SQLite" in prompt
        assert "Rationale: cheaper to run" in prompt
        assert "resume the work" in prompt

    def test_answer_missing_parts_is_usage(self, store):
        for arg in ("answer", "answer d1"):
            result = _dispatch(store, arg)
            assert result.error and "Usage:" in result.output

    def test_answering_twice_errors(self, store):
        _dispatch(store, "answer d1 1")
        result = _dispatch(store, "answer d1 2")
        assert result.error and "already resolved" in result.output


class TestWithdraw:
    def test_withdraw_closes_without_resuming(self, store):
        result = _dispatch(store, "withdraw d1 the queue moved off SQL")
        assert not result.error
        assert "withdrawn (the queue moved off SQL)" in result.output
        assert store.get("d1")["status"] == "withdrawn"
        assert "[Decision d1 withdrawn by the user]" in result.resume_prompt
        assert "cancelled" in result.resume_prompt


class TestUnknown:
    def test_unknown_verb_is_usage(self, store):
        result = _dispatch(store, "ponder d1")
        assert result.error and "Usage:" in result.output


class TestStructuredEntries:
    """The dashboard's decision cards resolve through the same semantics the
    command path applies (answer_decision / withdraw_decision) — pinned here so
    a surface can never grow its own parser drift."""

    def test_structured_answer_matches_the_command_path(self, store, tmp_path):
        twin = DecisionsStore(tmp_path / "twin")
        twin.add_decision(
            question="Which database for the job queue?", context="Backs the cron jobs refactor.",
            options=[{"label": "Postgres", "impact": "known ops"},
                     {"label": "SQLite", "impact": "zero ops"}],
            recommendation="Postgres", impact="blocks the refactor")
        via_command = dispatch_decision_command(twin, "answer d1 2 cheaper to run")
        structured = answer_decision(store, "d1", "2", "cheaper to run")
        assert structured.output == via_command.output
        assert structured.resume_prompt == via_command.resume_prompt
        assert structured.error == via_command.error
        assert store.get("d1")["status"] == "resolved"
        assert store.get("d1")["resolution"] == "SQLite"
        assert store.get("d1")["rationale"] == "cheaper to run"

    def test_structured_free_text_answer_with_spaces_stays_verbatim(self, store):
        # The one place the structured entry deliberately exceeds the command
        # grammar: a card submits already-parsed fields, so a multi-word free
        # choice is the choice, not choice + mangled rationale.
        result = answer_decision(store, "d1", "do both in stages", "safest")
        assert not result.error
        assert store.get("d1")["resolution"] == "do both in stages"
        assert store.get("d1")["rationale"] == "safest"

    def test_structured_answer_unknown_id_raises_key_error(self, store):
        with pytest.raises(KeyError):
            answer_decision(store, "d9", "1")

    def test_structured_answer_twice_raises_value_error(self, store):
        answer_decision(store, "d1", "1")
        with pytest.raises(ValueError):
            answer_decision(store, "d1", "2")

    def test_structured_answer_empty_choice_is_usage(self, store):
        result = answer_decision(store, "d1", "  ")
        assert result.error and "Usage:" in result.output

    def test_structured_withdraw_matches_the_command_path(self, store):
        result = withdraw_decision(store, "d1", "the queue moved off SQL")
        assert not result.error
        assert store.get("d1")["status"] == "withdrawn"
        assert "[Decision d1 withdrawn by the user]" in result.resume_prompt

    def test_structured_withdraw_twice_is_the_closed_error(self, store):
        withdraw_decision(store, "d1")
        result = withdraw_decision(store, "d1")
        assert result.error and "already withdrawn" in result.output

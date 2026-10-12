"""Goals drift detection: the policy as data, plus the turn-end hook against a
real store. Drift must be conservative — quick non-goal questions that merely
touch a terminal are not drift; unlinked mutating work is."""

import time
from types import SimpleNamespace

import pytest

from agent import goals_drift
from agent.goals_drift import (
    assess_turn_drift, mutating_tools_used, record_turn_drift, text_matches_goal,
    turn_user_text,
)
from tools.goals_store import GoalsStore


def _goal(statement="Ship the mars rover launch system", next_action="Write the test plan",
          status="active"):
    return {"id": "g1", "statement": statement, "why": "", "next_action": next_action,
            "status": status}


def _turn_rows(user_text, tool_names):
    rows = [{"role": "user", "content": user_text}]
    if tool_names:
        rows.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"call_{i}", "type": "function",
             "function": {"name": name, "arguments": "{}"}}
            for i, name in enumerate(tool_names)
        ]})
        rows.extend({"role": "tool", "tool_call_id": f"call_{i}", "content": "ok"}
                    for i in range(len(tool_names)))
    rows.append({"role": "assistant", "content": "done"})
    return rows


class TestPolicy:
    def test_dormant_without_active_goals(self):
        assert assess_turn_drift([_goal(status="paused")], user_text="anything",
                                 mutating=["terminal"], progress_logged=False) is None
        assert assess_turn_drift([], user_text="anything", mutating=["terminal"],
                                 progress_logged=False) is None

    def test_no_mutating_tools_is_not_drift(self):
        assert assess_turn_drift([_goal()], user_text="what is 2+2",
                                 mutating=[], progress_logged=False) is None

    def test_progress_logged_links_the_turn(self):
        assert assess_turn_drift([_goal()], user_text="tidy the garage",
                                 mutating=["write_file"], progress_logged=True) is None

    def test_keyword_overlap_links_the_turn(self):
        assert text_matches_goal("prepare the mars rover test plan", _goal())
        assert assess_turn_drift([_goal()], user_text="prepare the mars rover test plan",
                                 mutating=["terminal"], progress_logged=False) is None

    def test_unlinked_mutating_work_is_drift(self):
        event = assess_turn_drift([_goal()], user_text="reorganize my photo library",
                                  mutating=["terminal", "write_file"], progress_logged=False)
        assert event == {"summary": "reorganize my photo library",
                         "tools": ["terminal", "write_file"]}

    def test_boundary_scopes_tool_scan_to_this_turn(self):
        rows = _turn_rows("earlier ask", ["terminal"]) + _turn_rows("current ask", [])
        # The whole list contains a terminal call, but only from the first turn.
        assert mutating_tools_used(rows, start=4) == []

    def test_multimodal_user_text_is_extracted(self):
        rows = [{"role": "user", "content": [
            {"type": "text", "text": "sort the mars rover launch checklist"},
        ]}]
        assert "mars rover" in turn_user_text(rows, 0, None)


class TestHook:
    @pytest.fixture
    def agent(self):
        return SimpleNamespace(valid_tool_names={"goals_read", "terminal", "write_file"})

    @pytest.fixture
    def with_goal(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        GoalsStore().add_goal(statement="Ship the mars rover launch system",
                              why="", next_action="Write the test plan")
        return GoalsStore()

    def _turn_started(self, minutes_ago=1):
        return time.time() - minutes_ago * 60

    def test_records_drift_for_unlinked_work(self, agent, with_goal):
        record_turn_drift(agent, _turn_rows("tidy the garage", ["terminal"]),
                          user_message="tidy the garage", current_turn_user_idx=0,
                          turn_started=self._turn_started())
        events = with_goal.load()["drift"]
        assert [e["tools"] for e in events] == [["terminal"]]

    def test_quiet_when_progress_logged_during_turn(self, agent, with_goal):
        with_goal.log_progress("g1", "test plan drafted")  # now, i.e. during the turn
        record_turn_drift(agent, _turn_rows("anything", ["write_file"]),
                          user_message="anything", current_turn_user_idx=0,
                          turn_started=self._turn_started(minutes_ago=5))
        assert with_goal.load()["drift"] == []

    def test_quiet_for_goal_matching_work(self, agent, with_goal):
        record_turn_drift(agent, _turn_rows("update the mars rover test plan", ["write_file"]),
                          user_message="update the mars rover test plan",
                          current_turn_user_idx=0, turn_started=self._turn_started())
        assert with_goal.load()["drift"] == []

    def test_quiet_without_goals_tools(self, agent, with_goal):
        agent.valid_tool_names = {"terminal"}
        record_turn_drift(agent, _turn_rows("tidy the garage", ["terminal"]),
                          user_message="tidy the garage", current_turn_user_idx=0,
                          turn_started=self._turn_started())
        assert with_goal.load()["drift"] == []

    def test_no_proven_turn_clock_under_reports(self, agent, with_goal):
        # turn_started None (marker already cleared): linkage check treats the
        # turn as linked rather than manufacturing drift noise.
        record_turn_drift(agent, _turn_rows("tidy the garage", ["terminal"]),
                          user_message="tidy the garage", current_turn_user_idx=0,
                          turn_started=None)
        assert with_goal.load()["drift"] == []

    def test_mutation_free_turn_never_touches_the_store(self, agent, with_goal):
        record_turn_drift(agent, _turn_rows("what is 2+2", []),
                          user_message="what is 2+2", current_turn_user_idx=0,
                          turn_started=self._turn_started())
        assert with_goal.load()["drift"] == []

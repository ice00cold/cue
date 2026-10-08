"""Goals block in the system prompt: placement, gating and the caching contract.

The block must be a pure function of ``<home>/goals.yaml`` rendered only at
prompt-build time — an unchanged file renders byte-identical bytes, and a
goals_update mid-conversation changes nothing until the next build (new session
or the sanctioned compression rebuild)."""

from types import SimpleNamespace

import pytest

from agent.system_prompt import _goals_parts
from tools.goals_store import GOALS_BLOCK_HEADER, GoalsStore


def _agent(valid_tools=True, skip_context_files=False):
    names = {"goals_read", "goals_update", "goals_review"} if valid_tools else set()
    return SimpleNamespace(valid_tool_names=names, skip_context_files=skip_context_files)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


class TestGating:
    def test_no_goals_no_block(self, home):
        assert _goals_parts(_agent()) == []

    def test_goals_render_block(self, home):
        GoalsStore().add_goal(statement="Run a marathon", why="Health",
                              next_action="Sunday long run")
        parts = _goals_parts(_agent())
        assert len(parts) == 1
        assert parts[0].startswith(GOALS_BLOCK_HEADER)
        assert "Run a marathon" in parts[0]

    def test_absent_goals_tools_no_block(self, home):
        GoalsStore().add_goal(statement="Run a marathon", why="", next_action="")
        assert _goals_parts(_agent(valid_tools=False)) == []

    def test_context_file_free_forks_no_block(self, home):
        GoalsStore().add_goal(statement="Run a marathon", why="", next_action="")
        assert _goals_parts(_agent(skip_context_files=True)) == []


class TestCachingContract:
    def test_pure_function_of_the_file(self, home):
        store = GoalsStore()
        store.add_goal(statement="Run a marathon", why="", next_action="Sunday long run")
        first = _goals_parts(_agent())
        # A later build over an unchanged file renders the same bytes.
        assert _goals_parts(_agent()) == first

    def test_change_only_reaches_the_next_build(self, home):
        store = GoalsStore()
        store.add_goal(statement="Run a marathon", why="", next_action="Sunday long run")
        built = _goals_parts(_agent())
        # A goals_update AFTER the prompt was built: the already-rendered block is
        # what the cached conversation keeps sending...
        store.update_goal("g1", {"next_action": "Register for the race"})
        assert built is not None and "Register for the race" not in built
        # ...and the next build boundary (new session / compression rebuild) sees it.
        rebuilt = _goals_parts(_agent())
        assert rebuilt != built and "Register for the race" in rebuilt[0]

    def test_block_carries_no_timestamps(self, home):
        store = GoalsStore()
        store.add_goal(statement="Run a marathon", why="", next_action="Sunday long run")
        store.log_progress("g1", "progress never rides the prompt block")
        block = _goals_parts(_agent())[0]
        import re

        # No ISO timestamp can appear, and progress entries never ride the block:
        # both would churn bytes that the cached prefix must keep stable.
        assert not re.search(r"\d{4}-\d{2}-\d{2}", block)
        assert "progress never rides the prompt block" not in block

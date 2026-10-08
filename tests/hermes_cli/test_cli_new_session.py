"""Cue CLI session-shape commands: /new rotates the topic (same session), new_session resets
in-memory context (toolset changes) without minting a session id."""

from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB
from tools.todo_tool import TodoStore


class _FakeCompressor:
    """Minimal stand-in for ContextCompressor."""

    def __init__(self):
        self.last_prompt_tokens = 500
        self.last_completion_tokens = 200
        self.last_total_tokens = 700
        self.compression_count = 3
        self._context_probed = True


class _FakeAgent:
    def __init__(self, session_id: str, session_start):
        self.session_id = session_id
        self.session_start = session_start
        self.model = "anthropic/claude-opus-4.6"
        self._last_flushed_db_idx = 7
        self._todo_store = TodoStore()
        self._todo_store.write(
            [{"id": "t1", "content": "unfinished task", "status": "in_progress"}]
        )
        self.commit_memory_session = MagicMock()
        self._invalidate_system_prompt = MagicMock()

        # Token counters (non-zero to verify reset)
        self.session_total_tokens = 1000
        self.session_input_tokens = 600
        self.session_output_tokens = 400
        self.session_prompt_tokens = 550
        self.session_completion_tokens = 350
        self.session_cache_read_tokens = 100
        self.session_cache_write_tokens = 50
        self.session_reasoning_tokens = 80
        self.session_api_calls = 5
        self.session_estimated_cost_usd = 0.42
        self.session_cost_status = "estimated"
        self.session_cost_source = "openrouter"
        self.context_compressor = _FakeCompressor()

    def reset_session_state(self):
        """Mirror the real AIAgent.reset_session_state()."""
        self.session_total_tokens = 0
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_api_calls = 0
        self.session_estimated_cost_usd = 0.0
        self.session_cost_status = "unknown"
        self.session_cost_source = "none"
        if hasattr(self, "context_compressor") and self.context_compressor:
            self.context_compressor.last_prompt_tokens = 0
            self.context_compressor.last_completion_tokens = 0
            self.context_compressor.last_total_tokens = 0
            self.context_compressor.compression_count = 0
            self.context_compressor._context_probed = False


def _make_cli(env_overrides=None, config_overrides=None, **kwargs):
    """Create a HermesCLI instance with minimal mocking."""
    _clean_config = {
        "model": {
            "default": "anthropic/claude-opus-4.6",
            "base_url": "https://openrouter.ai/v1",
            "provider": "auto",
        },
        "display": {"compact": False, "tool_progress": "all"},
        "agent": {},
        "terminal": {"env_type": "local"},
    }
    if config_overrides:
        _clean_config.update(config_overrides)
    clean_env = {"LLM_MODEL": "", "HERMES_MAX_ITERATIONS": ""}
    if env_overrides:
        clean_env.update(env_overrides)
    prompt_toolkit_stubs = {
        "prompt_toolkit": MagicMock(),
        "prompt_toolkit.history": MagicMock(),
        "prompt_toolkit.styles": MagicMock(),
        "prompt_toolkit.patch_stdout": MagicMock(),
        "prompt_toolkit.application": MagicMock(),
        "prompt_toolkit.layout": MagicMock(),
        "prompt_toolkit.layout.processors": MagicMock(),
        "prompt_toolkit.filters": MagicMock(),
        "prompt_toolkit.layout.dimension": MagicMock(),
        "prompt_toolkit.layout.menus": MagicMock(),
        "prompt_toolkit.widgets": MagicMock(),
        "prompt_toolkit.key_binding": MagicMock(),
        "prompt_toolkit.completion": MagicMock(),
        "prompt_toolkit.formatted_text": MagicMock(),
        "prompt_toolkit.auto_suggest": MagicMock(),
    }
    with patch.dict(sys.modules, prompt_toolkit_stubs), patch.dict(
        "os.environ", clean_env, clear=False
    ):
        import cli as _cli_mod

        _cli_mod = importlib.reload(_cli_mod)
        with patch.object(_cli_mod, "get_tool_definitions", return_value=[]), patch.dict(
            _cli_mod.__dict__, {"CLI_CONFIG": _clean_config}
        ):
            return _cli_mod.HermesCLI(**kwargs)


def _prepare_cli_with_active_session(tmp_path):
    cli = _make_cli()
    cli._session_db = SessionDB(db_path=tmp_path / "state.db")
    cli._session_db.create_session(session_id=cli.session_id, source="cli", model=cli.model)

    cli.agent = _FakeAgent(cli.session_id, cli.session_start)
    cli.conversation_history = [{"role": "user", "content": "hello"}] * 8

    old_session_start = cli.session_start - timedelta(seconds=1)
    cli.session_start = old_session_start
    cli.agent.session_start = old_session_start
    return cli


@pytest.fixture(autouse=True)
def _reset_session_id_context():
    from gateway.session_context import _UNSET, _VAR_MAP

    yield
    os.environ.pop("HERMES_SESSION_ID", None)
    _VAR_MAP["HERMES_SESSION_ID"].set(_UNSET)


def test_new_command_rotates_the_topic_in_the_same_session(tmp_path, monkeypatch):
    """/new runs the topic rotation and installs its compacted history; the session id NEVER
changes and the session row is never ended."""
    from agent.context_rotation import RotationResult

    cli = _prepare_cli_with_active_session(tmp_path)
    old_session_id = cli.session_id
    rotated = RotationResult("rotated", list(cli.conversation_history),
                             [{"role": "user", "content": "summary + tail"}], title="T")

    def _fake_rotate(agent, history, *, title=None, task_id="default"):
        assert title == "T"
        return rotated

    import agent.context_rotation as _rot
    monkeypatch.setattr(_rot, "rotate_topic", _fake_rotate)
    monkeypatch.setattr(
        "hermes_cli.cli_session_mixin.rotate_topic", _fake_rotate, raising=False)

    cli.process_command("/new T")

    assert cli.session_id == old_session_id  # one main thread: never a fresh id
    assert cli.conversation_history == rotated.after_messages
    session = cli._session_db.get_session(old_session_id)
    assert session is not None and session["end_reason"] is None  # conversation continues


def test_new_command_reports_a_short_conversation(tmp_path, capsys):
    cli = _prepare_cli_with_active_session(tmp_path)
    cli.conversation_history = [{"role": "user", "content": "hi"}]
    cli.process_command("/new")
    assert "Nothing to rotate" in capsys.readouterr().out


def test_new_session_resets_context_without_minting_an_id(tmp_path):
    """new_session (toolset toggle / browser-use / voice wake) resets in-memory state and
    rebuilds the agent — under the one-main-thread model it KEEPS the session id and never
    ends the row."""
    cli = _prepare_cli_with_active_session(tmp_path)
    old_session_id = cli.session_id
    old_session_start = cli.session_start

    cli.new_session()

    assert cli.session_id == old_session_id
    session = cli._session_db.get_session(old_session_id)
    assert session is not None and session["end_reason"] is None
    assert cli.conversation_history == []
    assert cli.agent.session_id == cli.session_id
    assert cli.agent._last_flushed_db_idx == 0
    assert cli.agent._todo_store.read() == []
    assert cli.session_start > old_session_start
    assert cli.agent.session_start == cli.session_start
    cli.agent._invalidate_system_prompt.assert_called_once()


def test_new_session_delivers_context_engine_boundary_synchronously(tmp_path):
    """The context-engine on_session_end must fire during the reset itself.

    It is cheap local state work and ordering-sensitive: it must land before
    reset_session_state() rebinds the engine. The LLM-bound provider extraction is
    what gets deferred, not this."""
    cli = _prepare_cli_with_active_session(tmp_path)
    old_session_id = cli.session_id

    engine_calls = []
    cli.agent.context_compressor.on_session_end = (
        lambda sid, msgs: engine_calls.append((sid, list(msgs)))
    )

    cli.new_session()

    assert engine_calls == [(old_session_id, [{"role": "user", "content": "hello"}] * 8)]


def test_run_cleanup_flushes_pending_memory_manager_work(tmp_path):
    """A 'reset then quit' must not drop the queued old-context extraction.

    _run_cleanup gives the manager's serialized worker a bounded drain via
    flush_pending() before shutdown_all()'s short-fuse drain runs."""
    import cli as _cli_mod

    agent = MagicMock()
    mm = MagicMock()
    mm.flush_pending.return_value = True
    agent._memory_manager = mm
    agent._session_messages = []

    old_ref = _cli_mod._active_agent_ref
    _cli_mod._active_agent_ref = agent
    _cli_mod._cleanup_done = False
    try:
        _cli_mod._run_cleanup(notify_session_finalize=False)
    finally:
        _cli_mod._cleanup_done = True
        _cli_mod._active_agent_ref = old_ref

    mm.flush_pending.assert_called_once_with(timeout=10)


def test_clear_command_only_clears_the_screen(tmp_path):
    """/clear is visual: no session change, no history loss."""
    cli = _prepare_cli_with_active_session(tmp_path)
    cli.console = MagicMock()
    cli.show_banner = MagicMock()

    old_session_id = cli.session_id
    history = list(cli.conversation_history)
    cli.process_command("/clear")

    assert cli.session_id == old_session_id
    assert cli.conversation_history == history
    cli.console.clear.assert_called_once()
    cli.show_banner.assert_called_once()


def test_new_session_resets_token_counters(tmp_path):
    """Regression test for #2099: a context reset must zero all token counters.

    Drives the real ``AIAgent.reset_session_state`` (and the real context-engine
    ``on_session_reset``) on the fake agent's attribute bag, so this guards both the
    CLI wiring (the reset must call the reset) and the reset itself.
    """
    import types

    from agent.context_engine import ContextEngine
    from run_agent import AIAgent

    cli = _prepare_cli_with_active_session(tmp_path)
    agent = cli.agent
    agent.reset_session_state = types.MethodType(AIAgent.reset_session_state, agent)
    agent._transition_context_engine_session = types.MethodType(
        AIAgent._transition_context_engine_session, agent
    )
    comp = agent.context_compressor
    comp.on_session_reset = types.MethodType(ContextEngine.on_session_reset, comp)

    assert agent.session_total_tokens > 0
    assert agent.session_api_calls > 0
    assert comp.compression_count > 0

    cli.new_session()

    assert agent.session_total_tokens == 0
    assert agent.session_input_tokens == 0
    assert agent.session_output_tokens == 0
    assert agent.session_prompt_tokens == 0
    assert agent.session_completion_tokens == 0
    assert agent.session_cache_read_tokens == 0
    assert agent.session_cache_write_tokens == 0
    assert agent.session_reasoning_tokens == 0
    assert agent.session_api_calls == 0
    assert agent.session_estimated_cost_usd == 0.0
    assert agent.session_cost_status == "unknown"
    assert agent.session_cost_source == "none"

    assert comp.last_prompt_tokens == 0
    assert comp.last_completion_tokens == 0
    assert comp.last_total_tokens == 0
    assert comp.compression_count == 0

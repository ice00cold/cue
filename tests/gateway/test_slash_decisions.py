"""The gateway /decisions handler: dispatch through the runner, resolution enqueues the
resume turn through the adapter FIFO (the one main thread), and the command is reachable
on the mid-run (busy) surface."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource


def _source() -> SessionSource:
    return SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="chat-1", chat_type="dm")


def _event(args: str = "") -> MessageEvent:
    return MessageEvent(
        text=f"/decisions {args}".strip(), message_type=MessageType.TEXT,
        source=_source(), message_id="m1")


def _runner(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = object.__new__(GatewayRunner)
    # The executor hop stays real code shape but runs inline: ContextVars still cross it.
    runner._run_in_executor_with_context = AsyncMock(side_effect=lambda func: func())
    runner._enqueue_goal_turn = MagicMock()
    runner._telegramized_command_reply = lambda event, text: text
    return runner


_OPTIONS = [{"label": "a"}, {"label": "b"}]


async def _dispatch(tmp_path, monkeypatch, args: str = ""):
    runner = _runner(tmp_path, monkeypatch)
    return await runner._handle_decisions_command(_event(args)), runner


@pytest.mark.asyncio
async def test_bare_command_lists_pending(tmp_path, monkeypatch):
    from tools.decisions_store import DecisionsStore

    DecisionsStore(tmp_path).add_decision(question="Q?", options=_OPTIONS)
    output, runner = await _dispatch(tmp_path, monkeypatch)
    assert "[d1] Q?" in output
    runner._enqueue_goal_turn.assert_not_called()


@pytest.mark.asyncio
async def test_answer_resolves_and_enqueues_the_resume_turn(tmp_path, monkeypatch):
    from tools.decisions_store import DecisionsStore

    DecisionsStore(tmp_path).add_decision(
        question="Q?", options=_OPTIONS, recommendation="a")
    output, runner = await _dispatch(tmp_path, monkeypatch, "answer d1 2 cheaper to run")
    assert "resolved → b" in output
    runner._enqueue_goal_turn.assert_called_once()
    (event, prompt), kwargs = runner._enqueue_goal_turn.call_args
    assert "[Decision d1 resolved by the user]" in prompt
    assert "Answer: b" in prompt
    assert kwargs["kickoff"] is False


@pytest.mark.asyncio
async def test_unknown_id_reports_the_error(tmp_path, monkeypatch):
    output, runner = await _dispatch(tmp_path, monkeypatch, "answer d9 1")
    assert "No decision with id 'd9'." in output
    runner._enqueue_goal_turn.assert_not_called()


def test_command_is_dispatchable_while_busy():
    """Answering a decision must not wait for an idle agent: the command rides the
    mid-run (plain) dispatch surface and resolves the store under the event's scope."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    table = runner._gateway_plain_command_handlers()
    assert callable(table.get("decisions"))

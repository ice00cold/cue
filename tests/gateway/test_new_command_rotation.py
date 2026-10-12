"""Gateway /new rotates the topic: same session, conversation-scoped state cleared, live work stopped.

Phase-1 invariants (PLAN.md § One main thread): /new is context rotation, never a fresh session.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="c1",
                         user_name="tester", chat_type="dm")


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._background_tasks = set()

    session_key = build_session_key(_make_source())
    entry = SessionEntry(session_key=session_key, session_id="sess-1",
                         created_at=datetime.now(), updated_at=datetime.now(),
                         platform=Platform.TELEGRAM, chat_type="dm")
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store._entries = {session_key: entry}
    runner.session_store._generate_session_key.return_value = session_key
    runner.session_store.reset_session = AsyncMock(return_value=entry)
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._agent_cache_lock = None
    runner._is_user_authorized = lambda _source: True
    runner._resolve_session_agent_runtime = lambda **_kw: ("", {"api_key": None})
    runner._resolve_session_reasoning_config = lambda **_kw: None
    return runner, entry


@pytest.mark.asyncio
async def test_new_rotates_without_resetting_the_session():
    runner, entry = _make_runner()
    runner._session_model_overrides[entry.session_key] = {"model": "gpt-4o"}

    await runner._handle_new_command(_make_event("/new Deploy review"))

    # Rotation NEVER mints a session id: no reset, same entry.
    runner.session_store.reset_session.assert_not_awaited()
    assert entry.session_id == "sess-1"
    # The finished topic's conversation-scoped state (overrides) is cleared.
    assert entry.session_key not in runner._session_model_overrides


@pytest.mark.asyncio
async def test_new_passes_the_title_to_the_rotation():
    runner, _ = _make_runner()
    seen = {}

    async def _fake_rotation(source, title_arg):
        seen["title"] = title_arg
        return "Rotated topic: x"

    runner._run_topic_rotation = _fake_rotation
    reply = await runner._handle_new_command(_make_event("/new Deploy review"))
    assert seen["title"] == "Deploy review"
    assert reply == "Rotated topic: x"


@pytest.mark.asyncio
async def test_busy_new_interrupts_then_rotates():
    """/new mid-run interrupts the running agent first (interrupt_then_dispatch), then rotates."""
    from gateway.run import _INTERRUPT_REASON_RESET

    runner, entry = _make_runner()
    interrupted = {}

    async def _fake_interrupt(key, source, interrupt_reason=None, invalidation_reason=None):
        interrupted.update(key=key, reason=interrupt_reason)

    runner._interrupt_and_clear_session = _fake_interrupt

    async def _fake_rotation(source, title_arg):
        return "rotated"

    runner._run_topic_rotation = _fake_rotation
    reply = await runner._busy_new_command(_make_event("/new"), entry.session_key, _make_source())
    assert interrupted["reason"] == _INTERRUPT_REASON_RESET
    assert reply == "rotated"

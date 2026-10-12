"""Cue /stop contract: ONE main-thread key per profile.

Every chat window of a profile derives the same session key, so the exact-key path is the whole
stop surface: a /stop from any window interrupts the profile's running turn. Another profile's
runs are never touched, a pending sentinel is force-cleaned, and background delegations of the
session still count as active work. (Upstream's tiered chat-scope fallback matched per-chat key
shapes that no longer exist under the one-main-thread model.)
"""

from __future__ import annotations

import pytest

from agent.i18n import t
from gateway.run import GatewayRunner, _AGENT_PENDING_SENTINEL
from gateway.session import SessionSource, build_session_key
from gateway.platforms.base import Platform
from gateway.platforms.event import MessageEvent, MessageType


class _FakeAgent:
    pass


class _StoreEntry:
    def __init__(self, session_key):
        self.session_key = session_key


class _FakeStore:
    def __init__(self, session_key):
        self._key = session_key

    def get_or_create_session(self, source):
        return _StoreEntry(self._key)


def _source(platform=Platform.TELEGRAM, chat_id="111", chat_type="dm", **kw):
    return SessionSource(platform=platform, chat_id=chat_id, chat_type=chat_type, user_id="u1", **kw)


def _runner_with_runs(running, own_key):
    runner = object.__new__(GatewayRunner)
    runner._running_agents = dict(running)
    runner.session_store = _FakeStore(own_key)
    runner._is_user_authorized_for_source = lambda source, **kw: True
    runner.adapters = {}
    interrupted = []

    async def _fake_interrupt(session_key, source, *, interrupt_reason, invalidation_reason):
        interrupted.append((session_key, invalidation_reason))

    runner._interrupt_and_clear_session = _fake_interrupt
    return runner, interrupted


def _text(result) -> str:
    """The handler returns EphemeralReply or plain text; normalize for assertions."""
    return getattr(result, "value", result)


@pytest.mark.asyncio
async def test_stop_from_any_window_interrupts_the_main_threads_run():
    """A Telegram DM window stops a run that a Discord group window started: one thread, one key."""
    own_key = build_session_key(_source())  # the stopper's key
    runner, interrupted = _runner_with_runs({own_key: _FakeAgent()}, own_key)
    stopper = _source(platform=Platform.DISCORD, chat_id="222", chat_type="group")
    assert build_session_key(stopper) == own_key  # same conversation

    result = await runner._handle_stop_command(
        MessageEvent(text="/stop", message_type=MessageType.TEXT, source=stopper))

    assert interrupted == [(own_key, "stop_command_handler")]
    assert _text(result) == t("gateway.stop.stopped")


@pytest.mark.asyncio
async def test_stop_never_touches_another_profile():
    own_key = build_session_key(_source())
    other_profile_key = build_session_key(_source(), profile="work")
    runner, interrupted = _runner_with_runs({other_profile_key: _FakeAgent()}, own_key)

    result = await runner._handle_stop_command(
        MessageEvent(text="/stop", message_type=MessageType.TEXT, source=_source()))

    assert interrupted == []
    assert _text(result) == t("gateway.stop.no_active")


@pytest.mark.asyncio
async def test_pending_sentinel_is_force_cleaned():
    """A session still being set up has no agent turn to interrupt, but the sentinel holding the
    slot is cleared so the session is not left locked."""
    own_key = build_session_key(_source())
    runner, interrupted = _runner_with_runs({own_key: _AGENT_PENDING_SENTINEL}, own_key)

    result = await runner._handle_stop_command(
        MessageEvent(text="/stop", message_type=MessageType.TEXT, source=_source()))

    assert interrupted == [(own_key, "stop_command_pending")]
    assert _text(result) == t("gateway.stop.stopped_pending")

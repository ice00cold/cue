"""Cue's one-main-thread session model: every chat/platform maps to a single session per profile.

Phase-1 invariants (PLAN.md § One main thread):
- `build_session_key` collapses platform/chat/thread/user distinctions to the profile's one key,
  on BOTH sides of the ingress seam (adapter `_source_session_key` == runner `_session_key_for_source`).
- The routing entry's origin follows the most recent ACTIVE source, so restored-row deliveries
  (heartbeats, background completions) reach the chat that was used last — never just the seeding chat.
- Internal events do not move the origin (same clock as the activity touch).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.config import load_gateway_config
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import (
    Platform,
    SessionSource,
    SessionStore,
    build_session_key,
    main_thread_session_key,
)


def _source(platform: Platform, chat_id: str, *, chat_type: str = "dm", user_id: str = "u1",
            thread_id: str | None = None) -> SessionSource:
    return SessionSource(
        platform=platform, chat_id=chat_id, chat_type=chat_type, user_id=user_id, thread_id=thread_id,
    )


@pytest.mark.parametrize(
    "a,b",
    [
        (_source(Platform.TELEGRAM, "111"), _source(Platform.TELEGRAM, "999")),
        (_source(Platform.TELEGRAM, "111"), _source(Platform.DISCORD, "222", chat_type="group")),
        (_source(Platform.SLACK, "C1", chat_type="channel", user_id="U1"),
         _source(Platform.SLACK, "C2", chat_type="channel", user_id="U2")),
        (_source(Platform.TELEGRAM, "111", thread_id="77"), _source(Platform.TELEGRAM, "111")),
        (_source(Platform.TELEGRAM, "111"), _source(Platform.WHATSAPP, "1234567890123@s.whatsapp.net")),
    ],
)
def test_all_chats_map_to_one_main_thread_key(a, b):
    assert build_session_key(a) == build_session_key(b)
    assert build_session_key(a) == main_thread_session_key(None)


def test_main_thread_key_is_per_profile():
    assert build_session_key(_source(Platform.TELEGRAM, "1"), profile="ops") \
        == main_thread_session_key("ops") \
        != main_thread_session_key(None)
    # The namespace prefix stays parseable so per-profile store resolution keeps working.
    assert main_thread_session_key(None).startswith("agent:main:")
    assert main_thread_session_key("ops").startswith("agent:ops:")


def test_runner_derives_the_main_key_for_every_platform(tmp_path: Path):
    """The runner-side ingress seam derives the one main-thread key for a Telegram chat, a Discord
    group and a Slack channel alike."""
    from gateway.run import GatewayRunner

    config = load_gateway_config()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = SessionStore(tmp_path / "sessions", config)
    keys = {
        runner._session_key_for_source(_source(Platform.TELEGRAM, "111")),
        runner._session_key_for_source(_source(Platform.DISCORD, "222", chat_type="group")),
        runner._session_key_for_source(_source(Platform.SLACK, "C1", chat_type="channel")),
    }
    assert keys == {main_thread_session_key(None)}


def test_origin_follows_most_recent_active_source(tmp_path: Path):
    config = load_gateway_config()
    store = SessionStore(tmp_path / "sessions", config)
    tg = _source(Platform.TELEGRAM, "111", user_id="owner")
    entry = store.get_or_create_session(tg)
    assert entry.origin is not None and entry.origin.platform == Platform.TELEGRAM

    # A different platform's activity on the same thread refreshes the origin + transport hint.
    dc = _source(Platform.DISCORD, "222", chat_type="group", user_id="owner")
    entry = store.get_or_create_session(dc)
    assert entry.origin is not None and entry.origin.platform == Platform.DISCORD

    # Internal events (touch_activity=False) never move it.
    entry = store.get_or_create_session(tg, touch_activity=False)
    assert entry.origin is not None and entry.origin.platform == Platform.DISCORD


def test_message_event_source_key_agrees_with_builder():
    event = MessageEvent(
        text="hi", message_type=MessageType.TEXT,
        source=_source(Platform.TELEGRAM, "31337", user_id="42"))
    assert build_session_key(event.source) == main_thread_session_key(None)


def test_main_thread_recovers_across_platforms_after_a_store_reset(tmp_path: Path):
    """The main key's durable row is recovered whatever platform minted it: a source-filtered
    peer lookup would split the one thread per platform once the in-memory index is gone."""
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    key = main_thread_session_key(None)
    db.create_session("sid-main", "telegram", session_key=key, chat_id="111", chat_type="dm")
    db.append_message("sid-main", "user", "hello from telegram")

    config = load_gateway_config()
    store = SessionStore(tmp_path / "sessions", config)
    store._db = db  # pin: an unpinned store resolves the ambient home's state.db, not tmp_path
    # A Discord source (a different window) with an EMPTY in-memory index recovers the same row.
    entry = store.get_or_create_session(_source(Platform.DISCORD, "222", chat_type="group"))
    assert entry.session_id == "sid-main"

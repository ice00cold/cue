"""Cue: cron fires run in the profile's ONE persistent main thread (PLAN.md § One main thread).

Invariants under test, against real SessionDB/SessionStore imports on a temp home:
- ``ensure_main_thread_session`` resolves the existing row under the main key — whatever
  platform's ``source`` column minted it — and mints + creates the row on a cold store;
- the gateway's recovery adopts that row source-agnostically (no per-platform split of the
  main thread after a store reset);
- ``_origin_from_env`` never captures a thread lane (origin is an egress address).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from cron.main_thread import (
    ensure_main_thread_session, load_main_thread_history, main_thread_session_key_for_current_profile)
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore


def _source(platform: Platform, chat_id: str, *, chat_type: str = "dm") -> SessionSource:
    return SessionSource(platform=platform, chat_id=chat_id, chat_type=chat_type, user_id="u1")


def _store(tmp_path):
    return SessionStore(tmp_path / "sessions", GatewayConfig())


def test_ensure_resolves_the_existing_main_row_whatever_source_minted_it(tmp_path, monkeypatch):
    from hermes_state import SessionDB

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    db = SessionDB(db_path=tmp_path / "home" / "state.db")
    try:
        key = main_thread_session_key_for_current_profile()
        db.create_session(session_id="20260101_000000_deadbeef", source="telegram", session_key=key)

        session_id, created = ensure_main_thread_session(db, datetime.now())
        assert (session_id, created) == ("20260101_000000_deadbeef", False)
    finally:
        db.close()


def test_ensure_mints_and_creates_the_row_on_a_cold_store(tmp_path, monkeypatch):
    from hermes_state import SessionDB

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    db = SessionDB(db_path=tmp_path / "home" / "state.db")
    try:
        session_id, created = ensure_main_thread_session(db, datetime.now())
        assert created is True
        row = db.find_latest_gateway_session_for_main_key(
            session_key=main_thread_session_key_for_current_profile())
        assert row and row["id"] == session_id

        # Idempotent: the next fire resolves the row it just created.
        assert ensure_main_thread_session(db, datetime.now()) == (session_id, False)

        history = load_main_thread_history(db, session_id)
        assert history == []
    finally:
        db.close()


def test_gateway_recovery_adopts_a_cron_minted_main_row_any_platform(tmp_path, monkeypatch):
    """The whole point of the by-key lookup: a cron-only install's main row (source='cron') is
    the SAME conversation the gateway's first message — from ANY platform — must continue, and a
    store reset must not split the thread per platform."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    store = _store(tmp_path)
    # The cron side writes through the SAME durable store the gateway reads (a cron-only
    # install mints the row before any gateway message exists).
    db = store._db_for_key(main_thread_session_key_for_current_profile())
    main_id, _ = ensure_main_thread_session(db, datetime.now())
    db.append_message(session_id=main_id, role="user", content="cron says hi")

    telegram_entry = store.get_or_create_session(_source(Platform.TELEGRAM, "111"))
    assert telegram_entry.session_id == main_id
    # Simulate the store losing its entries (restart with a pruned sessions.json): a Discord
    # source must still recover the SAME conversation — never mint a per-platform sibling.
    store._entries.clear()
    store._loaded = False
    discord_entry = store.get_or_create_session(_source(Platform.DISCORD, "222", chat_type="group"))
    assert discord_entry.session_id == main_id


def test_origin_capture_never_carries_a_thread_lane():
    from gateway import session_context as sc
    from tools.cronjob_job_args import _origin_from_env

    tokens = sc.set_session_vars(
        platform="telegram", chat_id="-1001", thread_id="17585", message_id="m1", user_id="u1")
    try:
        origin = _origin_from_env("every 5m")
    finally:
        sc.clear_session_vars(tokens)
    assert origin == {"platform": "telegram", "chat_id": "-1001", "chat_name": None,
                      "user_id": "u1", "scope_id": None}
    assert "thread_id" not in origin

"""The CLI attaches the profile's ONE main thread (Cue's session model).

Phase-1 invariants (PLAN.md § One main thread): a plain CLI launch resolves the main-thread
session id through the durable routing index (creating it on first contact) instead of minting
a per-launch session; an explicit --resume keeps its own session (kanban workers, imports); and
closing the CLI never ends the main-thread row (the conversation outlives every window).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from hermes_cli.main_thread import resolve_main_thread_session


def _db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def test_creates_and_then_reuses_the_main_thread_row(tmp_path):
    db = _db(tmp_path)
    first = resolve_main_thread_session(db)
    assert first
    entry = db.get_session(first)
    assert entry is not None and entry["session_key"] == "agent:main:main-thread"

    # Second contact (next CLI run / the gateway) adopts the SAME conversation.
    assert resolve_main_thread_session(db) == first


def test_without_a_session_db_it_yields_none(tmp_path):
    assert resolve_main_thread_session(None) is None


def test_profile_namespace_matches_the_gateway(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "ops"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from gateway.session import active_profile_main_thread_session_key
    assert active_profile_main_thread_session_key() == "agent:ops:main-thread"
    from hermes_state import SessionDB
    profile_db = SessionDB(home / "state.db")
    sid = resolve_main_thread_session(profile_db)
    entry = profile_db.get_session(sid)
    assert entry["session_key"] == "agent:ops:main-thread"


def test_startup_resolution_prefers_explicit_resume(tmp_path, monkeypatch):
    """An explicit --resume target keeps its own session; a bare launch attaches the main thread."""
    from hermes_cli.main_thread import resolve_main_thread_session

    cli = SimpleNamespace(
        _session_db=_db(tmp_path),
        session_start=__import__("datetime").datetime.now(),
        _resolve_startup_session_id=None,  # bound below
    )
    from hermes_state_ids import new_session_id

    resumed = new_session_id(cli.session_start)
    sid, on_main = _resolve(cli, resumed)
    assert (sid, on_main) == (resumed, False)

    sid, on_main = _resolve(cli, None)
    assert on_main is True
    assert sid == resolve_main_thread_session(cli._session_db)


def _resolve(cli, resume):
    from hermes_cli.cli_init_mixin import CLIInitMixin

    method = CLIInitMixin._resolve_startup_session_id.__get__(cli)
    return method(resume)

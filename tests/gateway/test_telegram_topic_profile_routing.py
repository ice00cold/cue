"""Issue #76423 — Gateway routes source.profile into telegram topic state."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from hermes_state import SessionDB
from gateway.config import Platform
from gateway.session import SessionSource


CHAT = "208214988"


def _source(profile=None, thread_id="42"):
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id=CHAT,
        chat_id=CHAT,
        user_name="tester",
        chat_type="dm",
        thread_id=thread_id,
        profile=profile,
    )


def test_telegram_topic_mode_is_permanently_off_whatever_the_profile(tmp_path: Path):
    """Cue: topic lanes were upstream's per-topic session UX; with one main thread there is
    nothing to bind, so the mode stays off even for chats that enabled it before the fork."""
    from gateway.run import GatewayRunner

    assert GatewayRunner._telegram_topic_profile_name(_source("coder")) == "coder"
    assert GatewayRunner._telegram_topic_profile_name(_source(None)) == "default"

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="sess-coder", source="telegram", user_id=CHAT, profile_name="coder")
    db.enable_telegram_topic_mode(chat_id=CHAT, user_id=CHAT, profile_name="coder")

    runner = object.__new__(GatewayRunner)
    runner._session_db = db
    for profile in ("coder", "other", None):
        assert runner._telegram_topic_mode_enabled(_source(profile)) is False
    db.close()


def test_routed_profile_flows_into_prune_via_send_metadata(tmp_path: Path):
    """profile_routes: the transport adapter may be the primary (default) bot
    while the turn is routed to another profile — the outbound metadata built
    by the gateway carries the routed profile, and prune uses it over the
    adapter's own stamp (#76423)."""
    from gateway.run import GatewayRunner
    from plugins.platforms.telegram.adapter import TelegramAdapter

    runner = object.__new__(GatewayRunner)
    runner._thread_metadata_for_target = lambda *a, **k: {"thread_id": "99"}
    meta = runner._thread_metadata_for_source(_source("coder", "99"))
    assert meta["hermes_profile"] == "coder"
    assert "hermes_profile" not in runner._thread_metadata_for_source(_source(None, "99"))

    # Cooldowns are keyed (profile, chat): alpha's reminder must not gag beta.
    assert runner._should_send_telegram_lobby_reminder(_source("alpha")) is True
    assert runner._should_send_telegram_lobby_reminder(_source("beta")) is True
    assert runner._should_send_telegram_lobby_reminder(_source("alpha")) is False

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="sess-default", source="telegram", user_id=CHAT)
    db.create_session(session_id="sess-coder", source="telegram", user_id=CHAT, profile_name="coder")
    for prof, sid in (("default", "sess-default"), ("coder", "sess-coder")):
        db.bind_telegram_topic(
            chat_id=CHAT, thread_id="99", user_id=CHAT,
            session_key=f"k-{prof}", session_id=sid, profile_name=prof,
        )

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter._session_store = SimpleNamespace(_db=db)
    adapter._hermes_profile_name = "default"  # transport = primary bot
    adapter._prune_stale_dm_topic_binding(CHAT, "99", metadata=meta)

    assert db.get_telegram_topic_binding(chat_id=CHAT, thread_id="99", profile_name="coder") is None
    assert db.get_telegram_topic_binding(chat_id=CHAT, thread_id="99", profile_name="default") is not None
    db.close()

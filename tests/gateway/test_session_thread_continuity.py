"""Thread continuity under Cue's one-main-thread session model.

Upstream isolated thread sessions (a thread started EMPTY — no parent seeding) because a
per-thread session was a separate conversation. Cue has ONE conversation per profile: a thread
message is just another turn of the main thread, so it continues the same session and sees the
conversation's transcript. Thread context beyond that (fetching thread replies) stays a platform
adapter concern.

Covers:
- DM and group threads continue the main session (same id, transcript visible)
- Multiple threads of one chat are the same conversation
- Consistent across platforms (Slack, Telegram, Discord)
"""

import pytest

from gateway.config import Platform, GatewayConfig
from gateway.session import SessionSource, SessionStore


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """SessionStore with SQLite — load_transcript reads from DB only.

    Pin DEFAULT_DB_PATH to tmp_path so SessionDB() can't write to the real
    ~/.cuehome/state.db. (DEFAULT_DB_PATH is a module-level constant computed
    at hermes_state import time, before pytest's HERMES_HOME monkeypatch
    fires — the autouse fixture's HERMES_HOME override doesn't help here.)
    """
    import hermes_state
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    config = GatewayConfig()
    s = SessionStore(sessions_dir=tmp_path, config=config)
    return s


def _dm_source(platform=Platform.SLACK, chat_id="D123", thread_id=None, user_id="U1"):
    return SessionSource(
        platform=platform,
        chat_id=chat_id,
        chat_type="dm",
        user_id=user_id,
        thread_id=thread_id,
    )


def _group_source(platform=Platform.SLACK, chat_id="C456", thread_id=None, user_id="U1"):
    return SessionSource(
        platform=platform,
        chat_id=chat_id,
        chat_type="group",
        user_id=user_id,
        thread_id=thread_id,
    )


PARENT_HISTORY = [
    {"role": "user", "content": "What's the weather?"},
    {"role": "assistant", "content": "It's sunny and 72°F."},
]


class TestThreadContinuityEdgeCases:
    """Threads are turns of the ONE conversation, not fresh sessions."""

    def test_group_thread_continues_the_main_session(self, store):
        parent_source = _group_source()
        parent_entry = store.get_or_create_session(parent_source)
        for msg in PARENT_HISTORY:
            store.append_to_transcript(parent_entry.session_id, msg)

        thread_source = _group_source(thread_id="1234567890.000001")
        thread_entry = store.get_or_create_session(thread_source)

        assert thread_entry.session_id == parent_entry.session_id
        thread_transcript = store.load_transcript(thread_entry.session_id)
        assert [m["content"] for m in thread_transcript] == [m["content"] for m in PARENT_HISTORY]

    def test_multiple_threads_of_one_chat_are_one_conversation(self, store):
        first = store.get_or_create_session(_dm_source(thread_id="t1"))
        second = store.get_or_create_session(_dm_source(thread_id="t2"))
        assert first.session_id == second.session_id


class TestThreadContinuityCrossPlatform:
    """Verify thread continuity is consistent across all platforms."""

    @pytest.mark.parametrize("platform", [Platform.SLACK, Platform.TELEGRAM, Platform.DISCORD])
    def test_thread_continues_the_main_session_across_platforms(self, store, platform):
        parent_source = _dm_source(platform=platform)
        parent_entry = store.get_or_create_session(parent_source)
        for msg in PARENT_HISTORY:
            store.append_to_transcript(parent_entry.session_id, msg)

        thread_source = _dm_source(platform=platform, thread_id="thread_123")
        thread_entry = store.get_or_create_session(thread_source)

        assert thread_entry.session_id == parent_entry.session_id
        assert len(store.load_transcript(thread_entry.session_id)) == len(PARENT_HISTORY)

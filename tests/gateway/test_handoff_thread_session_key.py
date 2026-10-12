"""Handoff destinations land in the ONE main thread (Cue's session model).

Upstream's CLI→messaging handoff had to shape its destination SessionSource so its
``build_session_key`` matched the platform adapter's organic in-thread key (Discord keyed
threads on the thread's own id while Slack/Telegram used the parent channel — one wrong shape
spawned a fresh session instead of continuing). Under the one-main-thread model every source
derives the same key, so the parity is structural: pin that the handoff source, an organic
thread reply and a plain DM all map to the same conversation.
"""

from __future__ import annotations

from gateway.session import Platform, SessionSource, build_session_key, main_thread_session_key


def test_handoff_destination_matches_organic_in_thread_key():
    handoff_dest = SessionSource(
        platform=Platform.DISCORD, chat_id="123", chat_type="thread",
        thread_id="456", user_id="u1")
    organic_reply = SessionSource(
        platform=Platform.DISCORD, chat_id="456", chat_type="thread",
        thread_id="456", user_id="u1", parent_chat_id="123")
    dm = SessionSource(platform=Platform.DISCORD, chat_id="u1", chat_type="dm", user_id="u1")

    keys = {build_session_key(s) for s in (handoff_dest, organic_reply, dm)}
    assert keys == {main_thread_session_key(None)}


def test_platform_shaping_no_longer_affects_the_key():
    """Slack's parent-channel shape and Telegram's forum-topic shape derive the same key as the
    thread's own-id shape: nothing a handoff or adapter can do splits the conversation."""
    slack = SessionSource(platform=Platform.SLACK, chat_id="C1", chat_type="thread",
                          thread_id="170.1", user_id="U1", scope_id="T1")
    telegram = SessionSource(platform=Platform.TELEGRAM, chat_id="-100123", chat_type="group",
                             thread_id="77", user_id="u1")
    matrix = SessionSource(platform=Platform.MATRIX, chat_id="!room:example.org",
                           chat_type="thread", thread_id="$t1", user_id="@a:example.org")

    keys = {build_session_key(s) for s in (slack, telegram, matrix)}
    assert keys == {main_thread_session_key(None)}

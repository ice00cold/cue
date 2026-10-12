"""The QQ update-prompt authz key comes from ``build_session_key`` (profile-namespaced), not a
hard-coded ``agent:main:`` literal. Under Cue's one-main-thread model every chat derives the same
main-thread key, so interaction authz is the DM intake allowlist, not per-chat key equality."""

from __future__ import annotations

from types import SimpleNamespace

from gateway.config import Platform
from gateway.platforms.qqbot.adapter import QQAdapter
from gateway.platforms.qqbot.keyboards import InteractionEvent
from gateway.session import SessionSource, build_session_key


def _adapter(owner_profile=None):
    adapter = QQAdapter.__new__(QQAdapter)
    adapter.config = SimpleNamespace(extra={})
    adapter.platform = Platform.QQBOT
    if owner_profile:
        adapter._owner_profile = owner_profile
    return adapter


def test_update_prompt_key_is_the_canonical_main_thread_key_per_profile():
    event = InteractionEvent(scene="c2c", user_openid="U1")
    for profile in (None, "ops"):
        adapter = _adapter(profile)
        key = adapter._update_prompt_session_key(event, "U1")
        source = SessionSource(platform=Platform.QQBOT, chat_id="U1", chat_type="c2c", profile=profile)
        assert key == build_session_key(source, profile=profile)
    assert _adapter()._update_prompt_session_key(event, "U1") == "agent:main:main-thread"


def test_main_thread_interaction_authorizes_via_intake_allowlist(monkeypatch):
    adapter = _adapter()
    allowed = []
    monkeypatch.setattr(adapter, "_is_dm_intake_allowed", lambda uid: uid in allowed)
    event = InteractionEvent(scene="c2c", user_openid="U1")
    key = adapter._update_prompt_session_key(event, "U1")

    allowed.append("U1")
    assert adapter._is_authorized_interaction_for_session(event, key)
    assert adapter._is_authorized_interaction_for_session(event, "agent:main:main-thread")

    allowed.clear()
    assert not adapter._is_authorized_interaction_for_session(event, key)

"""Cue: cron runs land in the profile's ONE main thread (PLAN.md § One main thread).

Upstream Hermes gave every fire its own ``cron_<job>_<ts>`` session and mirrored/seeded per-chat
continuation sessions around the delivered report. Cue has one persistent conversation per
profile: the fire's prompt and response append to the main-thread transcript (``agent:<ns>:
main-thread``) under the durable cross-process turn lease, the report egresses to the origin
chat, and no session is spawned, seeded, titled or ended. The main-thread row is created here
only when nothing exists yet (a cron-only install whose gateway has never chatted); the gateway
adopts that row through the source-agnostic by-key recovery lookup.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

logger = logging.getLogger("cron.scheduler")  # log-record parity with cron/scheduler.py


def main_thread_session_key_for_current_profile() -> str:
    """The main-thread key of the profile the caller is scoped to (cron binds each tick's
    scope); see ``gateway.session.active_profile_main_thread_session_key`` for the namespace
    parity rules."""
    from gateway.session import active_profile_main_thread_session_key

    return active_profile_main_thread_session_key()


def resolve_main_thread_session(session_db) -> Optional[str]:
    """The profile's live main-thread session id, or None when no row exists yet.

    ``sessions.json`` (the gateway's routing index) is deliberately NOT consulted: the durable
    row under the key is authoritative, works standalone, and the by-key query already ranks to
    the most recent recoverable row."""
    finder = getattr(session_db, "find_latest_gateway_session_for_main_key", None)
    if not callable(finder):
        return None
    key = main_thread_session_key_for_current_profile()
    try:
        row = finder(session_key=key)
        # isinstance guard: a test double's finder returns a MagicMock, never a row.
        return str(row.get("id")) or None if isinstance(row, dict) else None
    except Exception as exc:
        logger.debug("main-thread session lookup failed for %s: %s", key, exc)
        return None


def load_main_thread_history(session_db, session_id: str):
    """The persisted main-thread transcript for a fresh cron agent (None when unreadable).

    Same loader as the durable lease's contended reload (``get_messages_as_conversation`` with
    alternation repair) so a fire continues the conversation exactly as the next gateway turn
    would replay it. The class-attribute check keeps MagicMock-style shims out."""
    if not callable(getattr(type(session_db), "get_messages_as_conversation", None)) or not session_id:
        return None
    try:
        return session_db.get_messages_as_conversation(
            session_id, repair_alternation=True, include_row_ids=True)
    except Exception as exc:
        logger.warning("main-thread history load failed for %s: %s", session_id, exc)
        return None


def ensure_main_thread_session(session_db, now) -> Tuple[str, bool]:
    """``(session_id, created)``: the profile's main-thread session, minting the row when this is
    the first ever fire (cron-only installs). The row carries the main key and ``source="cron"``
    so the gateway's first message recovers the same conversation instead of minting a sibling."""
    session_id = resolve_main_thread_session(session_db)
    if session_id:
        return session_id, False
    from hermes_state_ids import new_session_id

    key = main_thread_session_key_for_current_profile()
    session_id = new_session_id(now, hex_len=8)
    try:
        session_db.create_session(session_id=session_id, source="cron", session_key=key)
    except Exception as exc:
        # A failed CREATE still returns the minted id: the agent persists under it, and the row
        # appears on the next append — better than silently falling back to a throwaway id.
        logger.warning("main-thread session row create failed for %s: %s", key, exc)
    return session_id, True

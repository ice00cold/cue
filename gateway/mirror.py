"""Session mirroring for cross-platform message delivery.

When a message is sent to a platform (send_message or a webhook deliver_only route), append a
"delivery-mirror" record to the transcript so the conversation knows what was sent.  Standalone:
works from CLI and gateway contexts.

Cue: every chat IS the profile's one main thread, so a mirror always targets that session — the
old per-(platform, chat, thread) origin scan cannot resolve anything else.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from hermes_cli.config import get_hermes_home

logger = logging.getLogger(__name__)

_SESSIONS_DIR = get_hermes_home() / "sessions"
_SESSIONS_INDEX = _SESSIONS_DIR / "sessions.json"
_SESSIONS_INDEX_AT_IMPORT = _SESSIONS_INDEX


def _resolve_sessions_index() -> Path:
    """Active profile's ``sessions.json`` at call time: the patched ``_SESSIONS_INDEX`` when a test
    changed it, else live profile-scoped HERMES_HOME — under the multiplexed gateway one process
    serves every profile, so the import-time constant would resolve every profile's pre-migration
    session lookup against the launch profile's index."""
    return (_SESSIONS_INDEX if _SESSIONS_INDEX != _SESSIONS_INDEX_AT_IMPORT
            else get_hermes_home() / "sessions" / "sessions.json")


def _main_thread_session_id() -> Optional[str]:
    """The active profile's main-thread session id: the gateway routing index (``sessions.json``)
    first — it is fresh through unpublished rotations — then the durable state.db row under the
    main key."""
    from gateway.session import main_thread_session_key
    from hermes_cli.profiles import current_profile_name

    key = main_thread_session_key(current_profile_name() or None)
    sessions_index = _resolve_sessions_index()
    if sessions_index.exists():
        try:
            data = json.loads(sessions_index.read_text(encoding="utf-8-sig"))
        except Exception:
            data = None
        entry = (data or {}).get(key) if isinstance(data, dict) else None
        session_id = entry.get("session_id") if isinstance(entry, dict) else None
        if session_id:
            return str(session_id)
    try:
        from hermes_state_registry import acquire, release_or_close
        db = acquire()
        try:
            finder = getattr(db, "find_latest_gateway_session_for_main_key", None)
            row = finder(session_key=key) if callable(finder) else None
            return str(row.get("id")) if isinstance(row, dict) and row.get("id") else None
        finally:
            release_or_close(db)
    except Exception as e:
        logger.debug("Mirror: main-thread session lookup failed: %s", e)
        return None


def mirror_to_session(
    message_text: str, source_label: str = "cli", role: str = "assistant",
    session_id: Optional[str] = None,
) -> bool:
    """Append a delivery-mirror message to the main thread's SQLite transcript.

    Pass ``session_id`` when the caller already holds the exact session to skip the lookup.
    Text that is NOT the agent speaking (e.g. a webhook payload) must pass ``role="user"``:
    ``mirror`` metadata is dropped at the SQLite boundary, so an assistant-role mirror replays
    as a real turn and yields assistant→assistant pairs that break strict-alternation providers,
    while a user-role mirror collapses safely via the consecutive-user merge.
    Returns True if mirrored, False if no session or error. Never raises.

    ``role`` defaults to ``"assistant"`` — correct for the interactive ``send_message`` mirror, where the
    mirrored text is the agent's own outgoing reply (a genuine assistant turn). See #2221.
    """
    try:
        if not session_id:
            session_id = _main_thread_session_id()
        if not session_id:
            logger.warning(
                "Mirror: no main-thread session found for %s (explicit_id=none)", source_label)
            return False
        _append_to_sqlite(session_id, {
            "role": role, "content": message_text, "timestamp": datetime.now().isoformat(),
            "mirror": True, "mirror_source": source_label,
        })
        logger.debug("Mirror: wrote to session %s (from %s)", session_id, source_label)
        return True
    except Exception as e:
        # WARNING, not debug: a silent mirror drop loses the delivery from the conversation.
        logger.warning("Mirror failed for %s session=%s: %s", source_label, session_id, e)
        return False


def _append_to_sqlite(session_id: str, message: dict) -> None:
    """Append a message to the SQLite session database.

    Raises on failure: ``mirror_to_session`` reports ``False`` (and warns) only when the
    exception reaches it — swallowing it here made every failed write look mirrored (#10130).
    """
    from hermes_state_registry import acquire, release_or_close

    db = acquire()
    try:
        db.append_message(session_id=session_id, role=message.get("role", "assistant"), content=message.get("content"))
    finally:
        release_or_close(db)

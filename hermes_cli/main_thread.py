"""CLI-side resolution of the profile's ONE main thread (Cue's session model).

The CLI is a window into the same persistent conversation the gateway serves: at startup it
adopts the main-thread session id from the durable routing index (creating the session + routing
row on first contact), instead of minting a fresh per-launch session. The gateway's SessionStore
reads the same table, so CLI turns and gateway turns converge on one transcript.

Namespace derivation is shared with the cron/mirror paths
(``gateway.session.active_profile_main_thread_session_key``): a named profile home keys under
its namespace, every other home under the default one.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def resolve_main_thread_session(session_db) -> Optional[str]:
    """The profile's main-thread session id, get-or-created through the routing index.

    Returns None when the session DB is unavailable (callers fall back to a fresh per-launch
    id — the store is broken, not the model). Creation races with a live gateway at most once:
    the gateway's healthy saves are single-row upserts, so the CLI row survives and both
    processes converge on the same conversation.
    """
    from gateway.session import SessionEntry, active_profile_main_thread_session_key
    from gateway.session_lifecycle import _now, _new_session_id

    if session_db is None:
        return None
    key = active_profile_main_thread_session_key()
    try:
        rows = session_db.load_gateway_routing_entries()
        entry_json = rows.get(key)
        if entry_json:
            import json
            existing = (json.loads(entry_json) or {}).get("session_id") or ""
            if existing and session_db.get_session(existing):
                return existing
        session_id = _new_session_id(_now())
        session_db.ensure_session(session_id, source="cli", session_key=key)
        from gateway.config import Platform
        import json as _json
        candidate = SessionEntry(
            session_key=key, session_id=session_id,
            created_at=_now(), updated_at=_now(),
            platform=Platform.LOCAL, chat_type="dm")
        session_db.save_gateway_routing_entry(key, _json.dumps(candidate.to_dict()))
        return session_id
    except Exception as exc:
        logger.warning("Main-thread session resolution failed (%s); using a fresh session id", exc,
                    exc_info=True)
        return None

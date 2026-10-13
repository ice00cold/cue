"""Main-thread home routes (Cue's ONE-session model — PLAN.md Phase 6 M1).

``GET /api/main_thread`` resolves the profile's live main-thread session: the
same ``agent:<ns>:main-thread`` routing key every platform collapses to
(``gateway.session`` key-collapse helpers), read from the durable
``gateway_routing`` index so the Thread surface never guesses a session id
across compression rotations. Read-only by design — the first window that
chats (the embedded TUI) creates the routing row; ``sessions.changed`` then
tells the surface to re-resolve.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from fastapi import APIRouter

from hermes_cli.web_deps import late
from hermes_cli.web_routers._common import log as _log

router = APIRouter()

_open_session_db_for_profile = late("_open_session_db_for_profile", "hermes_cli.web_server_sessions")


def _main_thread_key(profile: Optional[str]) -> str:
    """Routing key for the store *profile* names.

    A named profile keys under its own namespace; an omitted profile keys under
    the active-scope derivation shared with the CLI/cron/mirror paths — namespace
    parity with the gateway's key collapse
    (``gateway.session.active_profile_main_thread_session_key``).
    """
    from gateway.session import active_profile_main_thread_session_key, main_thread_session_key

    named = (profile or "").strip()
    if not named:
        return active_profile_main_thread_session_key()
    return main_thread_session_key(named)


def _resolve_main_thread(db, key: str):
    """(routing session_id, live tip id, tip summary row) for *key*.

    A routing entry whose target no longer exists resolves as "no thread yet"
    (the CLI resolver's contract) — the next chatting window recreates it, so
    the honest answer is ``None`` rather than a 404. The live tip follows the
    compression chain to where messages actually land, mirroring what the
    messages endpoint does with either id.
    """
    entry_json = db.load_gateway_routing_entries().get(key)
    session_id: Optional[str] = None
    if entry_json:
        try:
            session_id = (json.loads(entry_json) or {}).get("session_id") or None
        except ValueError:
            _log.warning("malformed gateway_routing entry for %s", key)
            session_id = None
    if session_id and not db.get_session(session_id):
        session_id = None
    live_id = db.resolve_resume_session_id(session_id) if session_id else None
    summary = db.get_session(live_id) if live_id else None
    return session_id, live_id, summary


@router.get("/api/main_thread")
async def get_main_thread(profile: Optional[str] = None):
    """The profile's main thread: routing id, compression-aware live id, and the
    tip's summary row — the Thread surface's header/cards data in one call."""
    from hermes_cli.web_routers.sessions import _is_active, _serving_profile, _with_db

    key = _main_thread_key(profile)
    session_id, live_id, summary = await asyncio.to_thread(
        _with_db, profile, lambda db: _resolve_main_thread(db, key), read_only=True)
    return {
        "profile": _serving_profile(profile),
        "session_key": key,
        "session_id": session_id,
        "live_session_id": live_id,
        "exists": bool(session_id),
        "is_active": bool(summary and _is_active(summary, time.time())),
        "session": summary,
    }

"""Live SSE session tail (Cue Phase 6 M3 — R3).

``GET /api/sessions/{session_id}/live`` streams a read-only view of ONE
session while it runs: transcript rows as they land (keyset over the
``messages`` table's monotone AUTOINCREMENT row ids), the live compression
tip as it rotates, and the session's activity stamps when they move — all
through the same display projection the messages endpoint serves
(``_project_for_display``), so a row streamed here is the row the paged
transcript would show.

This is the one contained seam M3 adds (PHASE6-ARCHITECTURE.md §3/§6), NOT a
second event transport: interaction and token-level streaming for
dashboard-driven turns stay with the embedded TUI (PTY + ``/api/ws``), and
the cross-process ``sessions.changed`` push stays on the PTY child's
``/api/events`` channel — this endpoint only lets any surface (Thread home,
drill-down, any device) WATCH a worker session or the main thread run.
Turn-level persistence is the substrate: gateway/cron/worker turns are
written per round, so the tail streams round-granular rows; sub-turn
token deltas for foreign-origin turns are the explicitly out-of-scope
follow-up (gateway stream-tap), not this file.

Auth is the ordinary REST middleware path (session-token header / gated
cookie) — the SPA consumes this with a streaming ``fetch``, which carries
both, never ``EventSource`` (which can send neither), so no second scheme.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from hermes_cli.web_deps import late
from hermes_cli.web_routers._common import log as _log
from hermes_state_errors import StateDbReplacedError

router = APIRouter()

_open_session_db_for_profile = late("_open_session_db_for_profile", "hermes_cli.web_server_sessions")

_NOT_FOUND = "Session not found"

# Tail cadence: same watch rhythm as tui_gateway's change watcher. Each poll
# is a keyset read + one session row on the stream's read-only connection.
_POLL_INTERVAL_S = 0.5
# Comment frame so idle proxies keep the connection open; any emitted data
# frame also refreshes the deadline.
_HEARTBEAT_INTERVAL_S = 15.0
# Rows per poll, matching the messages endpoint's page cap: a burst larger
# than one page is caught up over successive polls (keyset keeps the anchor).
_MAX_PAGE = 500
# The activity window the sessions list uses for ``is_active``.
_ACTIVE_WINDOW_S = 300


def _sse_frame(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _resolve_target(db, session_id: str) -> Optional[str]:
    """Canonical id for *session_id*, or None when the store does not know it.
    Reuses the messages endpoint's resolver, so prefix ids resolve and a
    corrupt store maps to the same 503."""
    from hermes_cli.web_routers.sessions import _resolve_session_id

    return _resolve_session_id(db, session_id)


def _tail_rows(db, tip: str, last_row_id: int) -> tuple[List[Dict[str, Any]], int]:
    """Keyset page of new display rows past *last_row_id* on *tip*, and the
    next anchor (never below the one asked for).

    A row without an id cannot be tailed past (no keyset address) — the
    caller treats that as a terminal store-shape error, exactly like the
    export endpoint's "cannot keyset without row ids" stop.
    """
    rows = db.get_messages(tip, after_id=last_row_id, limit=_MAX_PAGE)
    if any(row.get("id") is None for row in rows):
        raise _UnkeysetableRows()
    return rows, max((row["id"] for row in rows), default=last_row_id)


class _UnkeysetableRows(Exception):
    """The store served rows the tail cannot address — end the stream rather
    than re-emitting the same page forever on reconnect."""


def _activity_snapshot(row: Dict[str, Any], now: float) -> Dict[str, Any]:
    """Bounded activity stamp: the fields the live surfaces show, plus the
    list-consistent ``is_active`` flag."""
    last = row.get("last_activity_at") or row.get("started_at") or 0
    return {
        "last_activity_at": last,
        "last_activity_description": row.get("last_activity_description"),
        "ended_at": row.get("ended_at"),
        "end_reason": row.get("end_reason"),
        "message_count": row.get("message_count"),
        "tool_call_count": row.get("tool_call_count"),
        "is_active": row.get("ended_at") is None and (now - (last or 0)) < _ACTIVE_WINDOW_S,
    }


def _poll_once(db, resolved: str, last_row_id: int, home) -> Dict[str, Any]:
    """One synchronous poll pass (runs on a worker thread).

    Returns the frame inputs: re-resolved tip (rotation follows the chain
    from the ORIGINAL id, matching what the messages endpoint serves), the
    display-projected keyset page, the next anchor, and the activity
    snapshot — or ``deleted`` when the session row has vanished.
    """
    from hermes_cli.web_routers.sessions import _project_for_display

    tip = db.resolve_resume_session_id(resolved)
    row = db.get_session(tip)
    if row is None:
        return {"tip": tip, "messages": [], "last_row_id": last_row_id,
                "activity": None, "deleted": True}
    messages, next_row_id = _tail_rows(db, tip, last_row_id)
    return {
        "tip": tip,
        "messages": _project_for_display(messages, home=home),
        "last_row_id": next_row_id,
        "activity": _activity_snapshot(row, time.time()),
        "deleted": False,
    }


def _is_transient_store_error(exc: BaseException) -> bool:
    """A busy/locked store this poll must survive (live view keeps streaming;
    the next pass re-reads) — as opposed to a malformed/replaced image."""
    from hermes_state import is_malformed_db_error

    if isinstance(exc, StateDbReplacedError) or is_malformed_db_error(exc):
        return False
    return isinstance(exc, sqlite3.OperationalError)


async def _live_frames(
    request: Request, db, profile: Optional[str], resolved: str, after_row_id: int, home,
):
    """The SSE body: hello snapshot, then per-poll messages/rotated/activity
    frames until the client disconnects or the session is deleted.

    The stream owns *db* from first frame to teardown: a cancelled response
    (client disconnect) unwinds through the ``finally`` and closes it — one
    read-only connection for the stream's whole lifetime, never one per poll.
    """
    from hermes_cli.web_routers.sessions import _serving_profile

    tip: Optional[str] = None
    last_row_id = after_row_id
    last_activity: Optional[Dict[str, Any]] = None
    hello_sent = False
    last_emit = time.monotonic()
    try:
        while True:
            if await request.is_disconnected():
                return
            try:
                poll = await asyncio.to_thread(_poll_once, db, resolved, last_row_id, home)
            except _UnkeysetableRows:
                yield _sse_frame("error", {
                    "session_id": resolved, "error": "rows_without_ids",
                    "message": "This store served transcript rows without durable ids; the live tail cannot follow it.",
                })
                return
            except (sqlite3.DatabaseError, StateDbReplacedError) as exc:
                if not _is_transient_store_error(exc):
                    _log.warning("live tail for %s stopped: store unreadable: %s", resolved, exc)
                    yield _sse_frame("error", {
                        "session_id": resolved, "error": "state_db_unreadable",
                        "message": "The session store became unreadable; run `hermes doctor`.",
                    })
                    return
                _log.debug("live tail for %s skipped a busy store poll: %s", resolved, exc)
                poll = None
            if poll is not None:
                if not hello_sent:
                    hello_sent = True
                    yield _sse_frame("hello", {
                        "session_id": resolved,
                        "live_session_id": poll["tip"],
                        "profile": _serving_profile(profile),
                        "after_row_id": after_row_id,
                        "activity": poll["activity"],
                    })
                    # hello already carried this poll's stamps; only MOVES
                    # after it become activity frames.
                    last_activity = poll["activity"]
                    last_emit = time.monotonic()
                if poll["deleted"]:
                    yield _sse_frame("deleted", {"session_id": resolved})
                    return
                if tip is not None and poll["tip"] != tip:
                    # Emit before the rows that landed on the new tip, so the
                    # client re-keys its pane before appending.
                    yield _sse_frame("rotated", {"session_id": poll["tip"], "previous": tip})
                tip = poll["tip"]
                if poll["messages"]:
                    yield _sse_frame("messages", {
                        "session_id": tip,
                        "messages": poll["messages"],
                        "last_row_id": poll["last_row_id"],
                    })
                    last_row_id = poll["last_row_id"]
                    last_emit = time.monotonic()
                if poll["activity"] != last_activity:
                    yield _sse_frame("activity", {"session_id": tip, **poll["activity"]})
                    last_activity = poll["activity"]
                    last_emit = time.monotonic()
            if time.monotonic() - last_emit >= _HEARTBEAT_INTERVAL_S:
                yield ": keepalive\n\n"
                last_emit = time.monotonic()
            await asyncio.sleep(_POLL_INTERVAL_S)
    finally:
        # Inline close on the cancellation path: releasing one read-only
        # connection is microseconds; scheduling a thread for it would race
        # the very teardown that owns it.
        db.close()


@router.get("/api/sessions/{session_id}/live")
async def get_session_live(
    session_id: str, request: Request, profile: Optional[str] = None,
    after_row_id: int = Query(0, ge=0),
):
    """Read-only SSE tail of one session's transcript + activity stamps.

    ``after_row_id`` anchors the keyset (the newest row id the caller already
    shows — 0 replays from the session's start). Frames: ``hello`` (resolved
    ids + initial activity), ``messages`` (new display-projected rows, same
    projection as the messages endpoint), ``rotated`` (the live compression
    tip moved; new rows follow it — row ids are globally monotone, so the
    anchor survives rotation), ``activity`` (when the stamps move), and the
    terminal ``deleted`` / ``error``. Idle stretches get ``: keepalive``
    comments. The stream ends on client disconnect or terminal frame; the
    client reconnects with its newest row id.
    """
    from hermes_cli.web_routers.sessions import _history_profile_home

    db = await asyncio.to_thread(_open_session_db_for_profile, profile, read_only=True)
    try:
        target = await asyncio.to_thread(_resolve_target, db, session_id)
    except BaseException:
        await asyncio.to_thread(db.close)
        raise
    if target is None:
        await asyncio.to_thread(db.close)
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    resolved = target
    home = _history_profile_home(profile)
    return StreamingResponse(
        _live_frames(request, db, profile, resolved, after_row_id, home),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

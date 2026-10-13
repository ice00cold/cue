"""Delegation ledger routes (Cue Phase 6 M2 — R2 drill-down).

``GET /api/delegations`` surfaces the durable ``async_delegations`` rows —
the delegation half of the drill-down, complementing the lineage tree's
child-session half. A delegation record carries state/goal/result; its
conversation is the ORIGIN session (the main thread, or a worker when work
was delegated from one), reachable through the existing sessions/messages
endpoints — never re-rendered here. Reads go through the profile's SessionDB
connection, so a ``?profile=`` request reads that profile's ledger, not the
dashboard process's launch home.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException

from hermes_cli.web_deps import late
from hermes_cli.web_routers._common import http_failure

router = APIRouter()

_open_session_db_for_profile = late("_open_session_db_for_profile", "hermes_cli.web_server_sessions")

_NOT_FOUND = "Delegation not found"


def _chain_scoped_ids(db, session_id: str):
    """The session's whole compression lineage — delegation rows point at the
    member that was live at dispatch, so a rotated thread keeps its history.

    An unknown session resolves as "no delegations" (the M1 resolver's honest
    no-thread-yet contract), never a 404 the drill-down can't render.
    """
    from hermes_cli.web_routers.sessions import _resolve_session_id

    sid = _resolve_session_id(db, session_id)
    if not sid:
        return []
    return db.get_compression_lineage(sid) or [sid]


def _list_delegations_sync(session_id: Optional[str], profile: Optional[str], limit: int):
    from hermes_cli.web_routers.sessions import _serving_profile

    db = _open_session_db_for_profile(profile, read_only=True)
    try:
        ids = _chain_scoped_ids(db, session_id) if session_id else None
        rows = db.list_async_delegations(limit=limit, session_ids=ids)
        return {
            "delegations": rows,
            "count": len(rows),
            "limit": limit,
            "profile": _serving_profile(profile),
        }
    finally:
        db.close()


def _get_delegation_sync(delegation_id: str, profile: Optional[str]):
    from hermes_cli.web_routers.sessions import _serving_profile

    db = _open_session_db_for_profile(profile, read_only=True)
    try:
        row = db.get_async_delegation(delegation_id)
        if not row:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        return {**row, "profile": _serving_profile(profile)}
    finally:
        db.close()


@router.get("/api/delegations")
async def list_delegations(
    session_id: Optional[str] = None, profile: Optional[str] = None, limit: int = 50,
):
    """Delegation records, newest dispatched first. ``session_id`` scopes to one
    conversation (compression-chain aware) — the Thread drill-down's filter."""
    limit_n = max(1, min(int(limit), 200))
    with http_failure("GET /api/delegations failed", 500, detail="Delegation lookup failed"):
        return await asyncio.to_thread(_list_delegations_sync, session_id, profile, limit_n)


@router.get("/api/delegations/{delegation_id}")
async def get_delegation(delegation_id: str, profile: Optional[str] = None):
    """One delegation record with its task payload and bounded result summary."""
    with http_failure(
        f"GET /api/delegations/{delegation_id} failed", 500, detail="Delegation lookup failed"):
        return await asyncio.to_thread(_get_delegation_sync, delegation_id, profile)

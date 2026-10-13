"""Session lineage routes (Cue Phase 6 M2 — R2 drill-down).

``GET /api/sessions/{id}/lineage`` answers the Thread surface's drill-down
question — "which child sessions hang off this conversation?" — over the
``parent_session_id`` graph: worker/subagent, delegation, branch and cron
children, as a bounded tree. The requested conversation's own compression
chain is returned as ids (the caller already collapses it via
``/api/main_thread``); a child that itself compressed projects to its live
tip, so opening a node's transcript (the existing messages endpoint) shows the
whole child conversation. Transcript paging, live tails and PTY hosting are
NOT rebuilt here — this surface only maps the graph.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter

from hermes_cli.web_deps import late
from hermes_cli.web_routers._common import http_failure, log as _log

router = APIRouter()

_open_session_db_for_profile = late("_open_session_db_for_profile", "hermes_cli.web_server_sessions")

_NOT_FOUND = "Session not found"

# Bounds for one tree response: delegation depth past a handful of levels and
# fan-outs past a few hundred nodes are runaway graphs (or a store with cyclic
# parentage); the surface truncates and says so instead of walking forever.
_MAX_DEPTH = 4
_MAX_NODES = 200


def _child_chain_ids(db, session_id: str) -> List[str]:
    """The child's own compression lineage (root through live tip), or
    ``[session_id]`` when unreadable."""
    try:
        return db.get_compression_lineage(session_id) or [session_id]
    except Exception:
        _log.debug("lineage: compression chain walk failed for %s", session_id, exc_info=True)
        return [session_id]


def _lineage_subtree(
    db, parent_ids: List[str], visited: Set[str], *, depth: int, budget: Dict[str, int], now: float,
) -> List[Dict[str, Any]]:
    """One level of children for *parent_ids*, each projected to its chain tip.

    Compression continuations never appear as their own nodes: a child node
    absorbs its chain (``session_id`` = live tip) and recursion continues from
    every chain member, so a worker's own workers are found wherever they
    attached along the worker's rotated ids. ``budget['nodes']`` counts every
    appended node across the whole tree; exhausting it flags ``truncated``.
    """
    nodes: List[Dict[str, Any]] = []
    for row in db.list_child_sessions(parent_ids):
        if budget["nodes"] <= 0:
            budget["truncated"] = True
            break
        row_id = row.get("id")
        if not row_id or row_id in visited:
            continue
        # chain[0] is row_id, so every chain member here is fresh (row_id was
        # not visited); a second parent edge onto a visited worker is skipped.
        chain = _child_chain_ids(db, row_id)
        visited.update(chain)
        node = {
            **row,
            "session_id": chain[-1] if chain else row_id,
            "chain": chain,
            "is_active": (
                row.get("ended_at") is None
                and (now - (row.get("last_activity_at") or row.get("started_at") or 0)) < 300),
            "child_count": 0,
            "children": [],
        }
        budget["nodes"] -= 1
        nodes.append(node)
        if depth < _MAX_DEPTH and budget["nodes"] > 0:
            node["children"] = _lineage_subtree(
                db, chain, visited, depth=depth + 1, budget=budget, now=now)
            node["child_count"] = len(node["children"])
    return nodes


def _lineage_sync(session_id: str, profile: Optional[str]):
    from fastapi import HTTPException

    from hermes_cli.web_routers.sessions import _resolve_session_id, _serving_profile

    def _read(db):
        sid = _resolve_session_id(db, session_id)
        if not sid:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        chain = db.get_compression_lineage(sid) or [sid]
        budget = {"nodes": _MAX_NODES, "truncated": False}
        children = _lineage_subtree(
            db, chain, set(chain), depth=1, budget=budget, now=time.time())
        return {
            "session_id": sid,
            "chain": chain,
            "live_session_id": chain[-1],
            "children": children,
            "truncated": budget["truncated"],
            "profile": _serving_profile(profile),
        }

    db = _open_session_db_for_profile(profile, read_only=True)
    try:
        return _read(db)
    finally:
        db.close()


@router.get("/api/sessions/{session_id}/lineage")
async def get_session_lineage(session_id: str, profile: Optional[str] = None):
    """The session's child-session tree for the drill-down (workers, branches,
    cron children), chain-collapsed per node and bounded; read-only."""
    with http_failure(f"GET /api/sessions/{session_id}/lineage failed", 500,
                      detail="Lineage lookup failed"):
        return await asyncio.to_thread(_lineage_sync, session_id, profile)

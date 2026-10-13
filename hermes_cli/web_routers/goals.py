"""Goal-card routes (Cue Phase 6 M4 / R4 — interaction from the surface).

The main thread's goal state (``/goal``) as a card: status read over
``GET /api/goal``, control verbs (pause / resume / clear / unwait) over
``POST /api/goal`` — through the SHARED dispatcher
(:func:`hermes_cli.goal_command.dispatch_goal_command`), never a
surface-specific parser. Setting goal text is deliberately NOT offered: a new
goal is a composer action (the embedded TUI's ``/goal``), the card only
controls the goal the thread already runs.

State lives in the profile's state.db under the goal meta key of the main
thread's LIVE compression tip (compression migrates it forward), so the
manager is built under the profile home's scope on a worker thread. A resumed
goal's continuation prompt is delivered into the ONE main thread through the
M4 send seam, exactly like a resolved decision's resume turn.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

#: Control verbs the card offers. Goal-setting and gate management stay with
#: the composer (``/goal``): a card that replaces goal text would be a second
#: chat surface (web/AGENTS.md), and gates run shell commands.
CONTROL_ACTIONS = ("status", "pause", "resume", "clear", "unwait")

_GATE_DENIED = "Gate management is not available from the dashboard cards — use /goal in the chat composer."


def _home_for(profile: Optional[str]):
    from hermes_cli.web_server_cron import _cron_profile_home

    return _cron_profile_home(profile)[1]


def _live_thread_id(profile: Optional[str]) -> Optional[str]:
    """The main thread's live compression tip, or None while it does not exist."""
    from hermes_cli.web_routers.main_thread import _main_thread_key, _resolve_main_thread
    from hermes_cli.web_routers.sessions import _with_db

    key = _main_thread_key(profile)
    _routing, live_id, _summary = _with_db(
        profile, lambda db: _resolve_main_thread(db, key), read_only=True)
    return live_id


def _goal_snapshot(home, session_id: str) -> Dict[str, Any]:
    """Read the goal under the owning profile's home scope (worker thread):
    the manager's own DB resolution keys on ``get_hermes_home()``."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.goals import GoalManager

    token = set_hermes_home_override(str(home))
    try:
        mgr = GoalManager(session_id=session_id)
        state = mgr.state
        return {
            "exists": state is not None and state.status != "cleared",
            "status_line": mgr.status_line(),
            "state": json.loads(state.to_json()) if state is not None else None,
        }
    finally:
        reset_hermes_home_override(token)


def _goal_action(home, session_id: str, action: str):
    """Apply one control verb under the profile's home scope (worker thread).

    Returns the dispatcher's ``GoalCommandResult``; the shared module owns
    every semantic (pause clears wait barriers, resume resets the budget,
    clear is final), so this side never re-implements one.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.goal_command import dispatch_goal_command
    from hermes_cli.goals import GoalManager

    token = set_hermes_home_override(str(home))
    try:
        mgr = GoalManager(session_id=session_id)
        return dispatch_goal_command(mgr, action, authorize_gate=lambda: _GATE_DENIED)
    finally:
        reset_hermes_home_override(token)


@router.get("/api/goal")
async def get_goal(profile: Optional[str] = None):
    """The main thread's goal: status line (the exact ``/goal status`` text)
    plus the structured state the card renders — turns budget, verdict,
    wait barriers — or the honest no-goal answer."""
    from hermes_cli.web_routers.sessions import _serving_profile

    live_id = await asyncio.to_thread(_live_thread_id, profile)
    response: Dict[str, Any] = {
        "profile": _serving_profile(profile),
        "session_id": live_id,
    }
    if not live_id:
        response.update({"exists": False,
                         "status_line": "No active goal. Set one with /goal <text>.",
                         "state": None})
        return response
    response.update(await asyncio.to_thread(_goal_snapshot, _home_for(profile), live_id))
    return response


class GoalActionBody(BaseModel):
    action: str


@router.post("/api/goal")
async def post_goal_action(body: GoalActionBody, profile: Optional[str] = None):
    """Apply one control verb to the main thread's goal.

    ``resume`` also schedules the goal's continuation turn into the main
    thread (the same delivery the decision cards use); the state change
    applies first and the delivery report is honest about a thread that
    cannot take the turn.
    """
    from hermes_cli.web_routers.main_thread import (
        deliver_main_thread_prompt, main_thread_delivery_status)
    from hermes_cli.web_routers.sessions import _serving_profile

    action = (body.action or "").strip().lower()
    if action not in CONTROL_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"action must be one of: {', '.join(CONTROL_ACTIONS)} (goal text is set from the chat composer)")
    live_id = await asyncio.to_thread(_live_thread_id, profile)
    if not live_id:
        raise HTTPException(status_code=409, detail={
            "error": "no_thread",
            "message": "The main thread does not exist yet — its first message creates it."})

    result = await asyncio.to_thread(_goal_action, _home_for(profile), live_id, action)
    if result.error:
        raise HTTPException(status_code=409, detail=result.output)
    response: Dict[str, Any] = {
        "profile": _serving_profile(profile),
        "output": result.output,
        "delivered": "none",
    }
    if result.prompt:
        can_deliver, reason = await main_thread_delivery_status(profile)
        if can_deliver:
            deliver_main_thread_prompt(profile, result.prompt, label="goal continuation")
            response["delivered"] = "queued"
        else:
            response["delivered"] = "unavailable"
            response["delivery_note"] = reason
    response.update(await asyncio.to_thread(_goal_snapshot, _home_for(profile), live_id))
    return response

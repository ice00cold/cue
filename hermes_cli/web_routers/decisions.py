"""Decision-queue routes (Cue Phase 6 M4 / R4 — interaction from the surface).

``GET /api/decisions`` and the resolve/withdraw actions the Thread home's
decision cards drive. The queue is the profile's ``decisions.yaml``
(:class:`tools.decisions_store.DecisionsStore`) — the same store every other
surface reads — and the actions go through the SHARED dispatcher semantics
(:func:`hermes_cli.decision_command.answer_decision` /
:func:`withdraw_decision`), never a surface-specific parser.

Resolving is two steps, in order: the record's state change persists FIRST
(a resolved fork stays answered even when delivery fails), then the resume
prompt is delivered into the ONE main thread as an ordinary turn through the
gateway's api_server ingress (:func:`.main_thread.deliver_main_thread_prompt`)
— the same headless path ``POST /api/main_thread/send`` serves, so the
blocked work resumes wherever the thread runs and the reply surfaces through
the transcript / live tail.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hermes_cli.web_routers._common import log as _log

router = APIRouter()


def _store(profile: Optional[str]):
    """The serving profile's decision queue (its own home, its own file)."""
    from hermes_cli.web_server_cron import _cron_profile_home
    from tools.decisions_store import DecisionsStore

    return DecisionsStore(_cron_profile_home(profile)[1])


def _card_view(decision: Dict[str, Any]) -> Dict[str, Any]:
    """The card-rendering projection: every field the pending view shows, with
    option indexes (the dispatcher's numeric answer vocabulary) attached."""
    return {
        "id": decision["id"],
        "question": decision["question"],
        "context": decision["context"],
        "options": [
            {"index": number, "label": option["label"], "impact": option["impact"]}
            for number, option in enumerate(decision["options"], 1)
        ],
        "recommendation": decision["recommendation"],
        "impact": decision["impact"],
        "status": decision["status"],
        "created_at": decision["created_at"],
    }


@router.get("/api/decisions")
async def list_decisions(profile: Optional[str] = None):
    """Pending decisions for the profile, oldest first (queue order).

    Only the pending half: the card surface answers forks, it is not the
    history browser — ``/decisions all`` (CLI/gateway) owns the closed tail.
    """
    from hermes_cli.web_routers.sessions import _serving_profile

    pending: List[Dict[str, Any]] = await asyncio.to_thread(
        lambda: _store(profile).open_decisions())
    return {
        "profile": _serving_profile(profile),
        "pending": [_card_view(decision) for decision in pending],
        "pending_count": len(pending),
    }


class DecisionResolveBody(BaseModel):
    """One answer: an option's ``index`` (the numbered buttons), or free-form
    ``choice`` text; optional rationale is recorded with the resolution."""
    choice: str
    rationale: str = ""


class DecisionWithdrawBody(BaseModel):
    reason: str = ""


def _dispatch_error(exc: Exception) -> HTTPException:
    """Map the dispatcher's shared error contract onto REST statuses."""
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc.args[0]))
    return HTTPException(status_code=409, detail=f"/decisions: {exc}")


async def _apply_and_deliver(
    profile: Optional[str], action, label: str,
) -> Dict[str, Any]:
    """Run one dispatcher action off-loop, then schedule the resume turn.

    The dispatch resolves under the serving profile's home; a KeyError (unknown
    id) maps to 404 and a ValueError (fork already closed) to 409 — the same
    answers ``/decisions`` gives. ``asyncio.to_thread`` keeps the YAML store's
    flock I/O off the request loop. The state change persists FIRST; the
    delivery report says ``queued`` (scheduled into the main thread) or
    ``unavailable`` with the honest reason — a resume turn that cannot land
    never refuses the resolution itself.
    """
    from hermes_cli.decision_command import DecisionCommandResult
    from hermes_cli.web_routers.main_thread import (
        deliver_main_thread_prompt, main_thread_delivery_status)
    from hermes_cli.web_routers.sessions import _serving_profile

    def run() -> DecisionCommandResult:
        try:
            return action()
        except KeyError as exc:
            raise _dispatch_error(exc) from exc
        except ValueError as exc:
            raise _dispatch_error(exc) from exc

    result = await asyncio.to_thread(run)
    if result.error:
        # Usage-shaped errors (empty choice): the same text the command prints.
        raise HTTPException(status_code=400, detail=result.output)
    response: Dict[str, Any] = {
        "profile": _serving_profile(profile),
        "output": result.output,
        "delivered": "none",
    }
    if result.resume_prompt:
        can_deliver, reason = await main_thread_delivery_status(profile)
        if can_deliver:
            deliver_main_thread_prompt(profile, result.resume_prompt, label=label)
            response["delivered"] = "queued"
        else:
            response["delivered"] = "unavailable"
            response["delivery_note"] = reason
    return response


@router.post("/api/decisions/{decision_id}/resolve")
async def resolve_decision(decision_id: str, body: DecisionResolveBody,
                           profile: Optional[str] = None):
    """Answer one pending decision; the blocked work resumes in the main thread."""
    from hermes_cli.decision_command import answer_decision

    store = _store(profile)
    decision_id = decision_id.strip()
    choice = (body.choice or "").strip()
    # A numeric choice is the option's INDEX (the dispatcher's vocabulary);
    # anything else is free-form answer text, first-class like the CLI's.
    return await _apply_and_deliver(
        profile,
        lambda: answer_decision(store, decision_id, choice, (body.rationale or "").strip()),
        label=f"decision {decision_id} resume")


@router.post("/api/decisions/{decision_id}/withdraw")
async def withdraw_decision(decision_id: str, body: DecisionWithdrawBody,
                            profile: Optional[str] = None):
    """Close one pending decision as moot; the parked work is cancelled in the
    main thread unless the user asks otherwise."""
    from hermes_cli.decision_command import withdraw_decision as withdraw

    store = _store(profile)
    decision_id = decision_id.strip()
    return await _apply_and_deliver(
        profile,
        lambda: withdraw(store, decision_id, (body.reason or "").strip()),
        label=f"decision {decision_id} withdraw")

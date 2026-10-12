"""Decisions tools — ``decisions_log`` / ``decisions_list`` / ``decisions_resolve`` over
``DecisionsStore``.

Plain registry tools: decision state is durable per-profile file state, so handlers resolve
``<home>/decisions.yaml`` through ``get_hermes_home()`` at call time (each served profile
gets its own queue). Logging a decision also notifies the user per the Phase 4 escalation
rules (``deliver_decision_ping``): wake-tier impact pings the profile's Telegram home
channel immediately, everything else queues into the proactive outbox for the next batched
report — a stalled fork never waits for the user to notice either way.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from tools.decisions_store import DecisionsStore, validate_decision_fields
from tools.registry import registry, tool_error

logger = logging.getLogger("tools.decisions_tool")

DECISIONS_TOOLSET = "decisions"
PING_PLATFORM = "telegram"


def _store() -> DecisionsStore:
    return DecisionsStore()


def _clean(value: Optional[str]) -> str:
    return str(value).strip() if value is not None else ""


def _clean_options(options: Any) -> List[Dict[str, str]]:
    """Options as ``{label, impact}`` dicts; a bare string option carries a label only."""
    if not isinstance(options, (list, tuple)):
        return []
    cleaned = []
    for option in options:
        if isinstance(option, dict):
            cleaned.append({"label": _clean(str(option.get("label") or "")),
                            "impact": _clean(str(option.get("impact") or ""))})
        elif str(option or "").strip():
            cleaned.append({"label": _clean(str(option)), "impact": ""})
    return cleaned


def decisions_log(question: str, context: Optional[str] = None, options: Any = None,
                  recommendation: Optional[str] = None, impact: Optional[str] = None) -> str:
    """Queue a fork-in-the-road decision for the user and notify them per its urgency."""
    question, context = _clean(question), _clean(context)
    recommendation, impact = _clean(recommendation), _clean(impact)
    cleaned_options = _clean_options(options)
    error = validate_decision_fields(
        question=question, context=context, options=cleaned_options,
        recommendation=recommendation, impact=impact,
        require_question=True, require_options=True)
    if error:
        return tool_error(error, success=False)
    try:
        record = _store().add_decision(
            question=question, context=context, options=cleaned_options,
            recommendation=recommendation, impact=impact)
    except ValueError as exc:
        return tool_error(str(exc), success=False)
    ping = deliver_decision_ping(record)
    if ping.get("sent"):
        message = f"Decision logged and pinged the user on {PING_PLATFORM}."
    elif ping.get("queued"):
        message = ("Decision logged and queued for the next batched proactive report "
                   "(quiet hours respected) — it is also listed by /decisions right away.")
    else:
        message = (f"Decision logged. Ping not delivered ({ping.get('error', 'unknown')}) — "
                   "surface the decision and the answer options in your reply.")
    return json.dumps({"success": True, "decision": record, "pinged": bool(ping.get("sent")),
                       "queued": bool(ping.get("queued")), "message": message}, ensure_ascii=False)


def decisions_list(include_resolved: bool = False) -> str:
    """Pending decisions (or the whole history); the calling model renders the prose."""
    store = _store()
    decisions = store.read() if include_resolved else store.open_decisions()
    return json.dumps({
        "success": True,
        "decisions": decisions,
        "open_count": store.pending_count(),
        "listing": DecisionsStore.format_open_list(store.open_decisions()),
    }, ensure_ascii=False)


def decisions_resolve(decision_id: str, choice: str, rationale: Optional[str] = None) -> str:
    """Record the user's answer to a pending decision."""
    decision_id, choice, rationale = _clean(decision_id), _clean(choice), _clean(rationale)
    if not decision_id:
        return tool_error("decision_id is required.", success=False)
    if not choice:
        return tool_error("choice is required — the option number the user picked, or their words.", success=False)
    store = _store()
    record = store.get(decision_id)
    if record is None:
        return tool_error(f"No decision with id '{decision_id}'.", success=False)
    resolution = DecisionsStore.choice_text(record, choice)
    error = validate_decision_fields(resolution=resolution, rationale=rationale)
    if error:
        return tool_error(error, success=False)
    try:
        resolved = store.resolve_decision(decision_id, resolution, rationale)
    except ValueError as exc:
        return tool_error(str(exc), success=False)
    return json.dumps({"success": True, "decision": resolved,
                       "message": "Decision recorded — resume the work that was blocked on it."},
                      ensure_ascii=False)


def notify_pending_decision(record: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort immediate ping that a decision awaits the user (PLAN.md § Decision surfacing).

    Delivery follows the Phase 1 one-main-thread model: the profile's Telegram home
    channel (``send_message`` resolves it; a thread id is never part of the target) and
    the send mirrors into the profile's ONE main-thread transcript. Failure is reported,
    never raised — a ping problem must not fail the decision the agent just logged.
    """
    from tools.send_message_tool import send_message_tool

    try:
        raw = send_message_tool({"action": "send", "target": PING_PLATFORM,
                                 "message": DecisionsStore.format_ping(record)})
        result = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception as exc:
        logger.warning("decision ping failed for %s", record.get("id"), exc_info=True)
        return {"sent": False, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(result, dict):
        return {"sent": False, "error": "unexpected send result"}
    if result.get("success"):
        return {"sent": True}
    return {"sent": False, "error": str(result.get("error") or "send failed")}


def deliver_decision_ping(record: Dict[str, Any]) -> Dict[str, Any]:
    """Escalation-aware notification for a fresh decision (PLAN.md § Phase 4).

    The decision escalates on its ``impact`` field: WAKE tier (impact matches a configured
    ``proactive.wake_patterns`` regex) sends the ping now — quiet hours or not — and WAIT
    tier queues a one-line notice into the proactive outbox, flushed with the next batched
    report. Either way the decision itself is logged and listed by ``/decisions``
    immediately; a notification problem never fails the log.
    """
    from tools.proactive_store import (
        WAKE_TIER, ProactiveStore, escalation_tier, proactive_settings,
    )

    tier = escalation_tier(record.get("impact", ""), proactive_settings().get("wake_patterns"))
    if tier != WAKE_TIER:
        try:
            ProactiveStore().enqueue_notice("decision", DecisionsStore.format_queue_notice(record))
            return {"queued": True}
        except Exception:
            logger.warning("decision notice queueing failed for %s", record.get("id"), exc_info=True)
    return notify_pending_decision(record)


def check_decisions_requirements() -> bool:
    """Decisions tools have no external requirements — always available."""
    return True


DECISIONS_LOG_SCHEMA = {
    "name": "decisions_log",
    "description": (
        "Log a fork-in-the-road decision that needs the user's call: the question, the "
        "realistic options (label + impact each, 2-6), your recommendation, and the overall "
        "impact (what stays blocked while it waits). Use it whenever the next step needs a "
        "human choice — never silently stall and never guess on the user's behalf: log the "
        "decision, tell the user you are moving on, then keep working on whatever else is "
        "actionable. The user is pinged and answers with /decisions or in chat; check "
        "decisions_list to see what is still open before parking work."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The fork, one sentence the user can answer cold."},
            "context": {"type": "string", "description": "Background the user needs to answer."},
            "options": {
                "type": "array",
                "description": "The realistic paths (2-6), each label + its impact.",
                "items": {"type": "object", "properties": {
                    "label": {"type": "string", "description": "The option in a few words."},
                    "impact": {"type": "string", "description": "What choosing it means — cost, risk, reversibility."},
                }},
            },
            "recommendation": {"type": "string", "description": "Which option you recommend and why, briefly."},
            "impact": {"type": "string", "description": "What this decision affects / what stays blocked meanwhile."},
        },
        "required": ["question", "options"],
    },
}

DECISIONS_LIST_SCHEMA = {
    "name": "decisions_list",
    "description": (
        "List pending decisions (id, question, options, recommendation, impact), or the whole "
        "history with resolutions and rationales (include_resolved=true). Check it before "
        "parking work or when the user asks what is waiting on them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "include_resolved": {"type": "boolean", "description": "Include resolved/withdrawn history (default false)."},
        },
        "required": [],
    },
}

DECISIONS_RESOLVE_SCHEMA = {
    "name": "decisions_resolve",
    "description": (
        "Record the user's answer to a pending decision: decision_id, their choice (an "
        "option's 1-based number or their own words) and the rationale they gave, if any. "
        "Call it as soon as the user answers in conversation, then resume the blocked work."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "decision_id": {"type": "string", "description": "Decision id (e.g. d1)."},
            "choice": {"type": "string", "description": "The user's answer — an option's number or free text."},
            "rationale": {"type": "string", "description": "Why they chose it, when they said so."},
        },
        "required": ["decision_id", "choice"],
    },
}

registry.register(
    name="decisions_log", toolset=DECISIONS_TOOLSET, schema=DECISIONS_LOG_SCHEMA,
    handler=lambda args, **kw: decisions_log(
        question=args.get("question") or "", context=args.get("context"),
        options=args.get("options"), recommendation=args.get("recommendation"),
        impact=args.get("impact")),
    check_fn=check_decisions_requirements, emoji="⚖️")

registry.register(
    name="decisions_list", toolset=DECISIONS_TOOLSET, schema=DECISIONS_LIST_SCHEMA,
    handler=lambda args, **kw: decisions_list(include_resolved=bool(args.get("include_resolved", False))),
    check_fn=check_decisions_requirements, emoji="⚖️")

registry.register(
    name="decisions_resolve", toolset=DECISIONS_TOOLSET, schema=DECISIONS_RESOLVE_SCHEMA,
    handler=lambda args, **kw: decisions_resolve(
        decision_id=args.get("decision_id") or "", choice=args.get("choice") or "",
        rationale=args.get("rationale")),
    check_fn=check_decisions_requirements, emoji="⚖️")

"""The CLI's /decisions semantics, shared by every command surface.

Adapters (CLI, messaging gateway) resolve the store under the caller's profile scope and
schedule the returned resume prompt as the next turn of the profile's ONE main thread;
this module alone parses subcommands and mutates decision state (the queue lives in
``<home>/decisions.yaml``). It never changes conversation history.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from tools.decisions_store import PENDING_STATUS, DecisionsStore

logger = logging.getLogger(__name__)

_ANSWER_VERBS = frozenset({"answer", "resolve", "pick", "choose"})
_USAGE = (
    "Usage: /decisions [list | show <id> | answer <id> <choice> [rationale] | "
    "withdraw <id> [reason] | all]"
)
_CLOSED_TAIL = 10  # history lines the 'all' listing shows


@dataclass(frozen=True)
class DecisionCommandResult:
    output: str
    resume_prompt: Optional[str] = None
    error: bool = False


def dispatch_decision_command(store: DecisionsStore, arg: str) -> DecisionCommandResult:
    """Apply one ``/decisions`` command. ``store`` must be the caller's profile store."""
    tokens = (arg or "").strip().split(None, 1)
    verb = tokens[0].lower() if tokens else ""
    rest = tokens[1].strip() if len(tokens) > 1 else ""
    try:
        if not verb or verb in {"list", "open"}:
            return _list(store)
        if verb in {"all", "history"}:
            return _list(store, include_resolved=True)
        if verb == "show":
            return _show(store, rest)
        if verb in _ANSWER_VERBS:
            return _answer(store, rest)
        if verb == "withdraw":
            return _withdraw(store, rest)
    except KeyError as exc:
        return DecisionCommandResult(str(exc.args[0]), error=True)
    except ValueError as exc:
        return DecisionCommandResult(f"/decisions: {exc}", error=True)
    return DecisionCommandResult(_USAGE, error=True)


def _require(store: DecisionsStore, decision_id: str) -> dict:
    decision_id = (decision_id or "").strip()
    if not decision_id:
        raise KeyError("A decision id is required (e.g. d1).")
    record = store.get(decision_id)
    if record is None:
        raise KeyError(f"No decision with id '{decision_id}'.")
    return record


def _list(store: DecisionsStore, *, include_resolved: bool = False) -> DecisionCommandResult:
    records = store.read() if include_resolved else store.open_decisions()
    if not include_resolved:
        return DecisionCommandResult(DecisionsStore.format_open_list(records))
    if not records:
        return DecisionCommandResult("No decisions logged yet.")
    pending = [d for d in records if d["status"] == PENDING_STATUS]
    closed = [d for d in records if d["status"] != PENDING_STATUS]
    output = DecisionsStore.format_open_list(pending)
    if closed:
        lines = [f"[{d['id']}] {d['question']} → {d['resolution'] or d['status']}" for d in closed[-_CLOSED_TAIL:]]
        output += "\nClosed:\n" + "\n".join(lines)
    return DecisionCommandResult(output)


def _show(store: DecisionsStore, arg: str) -> DecisionCommandResult:
    record = _require(store, arg)
    return DecisionCommandResult(DecisionsStore.format_detail(record))


def _answer(store: DecisionsStore, rest: str) -> DecisionCommandResult:
    parts = rest.split(None, 2)
    if len(parts) < 2:
        return DecisionCommandResult("Usage: /decisions answer <id> <choice> [rationale]", error=True)
    decision_id, choice = parts[0], parts[1]
    rationale = parts[2].strip() if len(parts) > 2 else ""
    record = _require(store, decision_id)
    resolution = DecisionsStore.choice_text(record, choice)
    resolved = store.resolve_decision(decision_id, resolution, rationale)
    output = f"✓ [{resolved['id']}] resolved → {resolved['resolution']}"
    if resolved["rationale"]:
        output += f" ({resolved['rationale']})"
    output += "\nResuming the blocked work in the main thread."
    return DecisionCommandResult(output, resume_prompt=_resume_prompt(resolved))


def _withdraw(store: DecisionsStore, rest: str) -> DecisionCommandResult:
    parts = rest.split(None, 1)
    if not parts or not parts[0].strip():
        return DecisionCommandResult("Usage: /decisions withdraw <id> [reason]", error=True)
    decision_id = parts[0]
    reason = parts[1].strip() if len(parts) > 1 else ""
    record = _require(store, decision_id)
    if record["status"] != PENDING_STATUS:
        return DecisionCommandResult(
            f"/decisions: Decision {record['id']} is already {record['status']}.", error=True)
    withdrawn = store.withdraw_decision(decision_id, reason)
    output = f"✓ [{withdrawn['id']}] withdrawn" + (f" ({withdrawn['rationale']})" if withdrawn["rationale"] else "")
    return DecisionCommandResult(output, resume_prompt=_withdraw_prompt(withdrawn))


def _resume_prompt(decision: dict) -> str:
    """The turn that tells the agent the fork is answered and work resumes."""
    lines = [
        f"[Decision {decision['id']} resolved by the user]",
        decision["question"],
        f"Answer: {decision['resolution']}",
    ]
    if decision["rationale"]:
        lines.append(f"Rationale: {decision['rationale']}")
    lines.append("The choice above is final — resume the work that was blocked on this decision.")
    return "\n".join(lines)


def _withdraw_prompt(decision: dict) -> str:
    """The turn that tells the agent the fork closed without a choice."""
    lines = [
        f"[Decision {decision['id']} withdrawn by the user]",
        decision["question"],
    ]
    if decision["rationale"]:
        lines.append(f"Reason: {decision['rationale']}")
    lines.append(
        "The fork is closed without a choice — treat the parked work as cancelled unless the user asks otherwise.")
    return "\n".join(lines)

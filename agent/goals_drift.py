"""Goals drift detection — flag substantive turn work that serves no active goal.

Cue pillar 2: the agent orients every piece of work toward goals. This module
runs at turn end (:func:`record_turn_drift`, called from
``agent/turn_finalizer.finalize_turn`) and answers one question deterministically,
without an extra model call: *did this turn do world-mutating work without any
link to an active goal?*

A turn is LINKED (no drift) when any of:
  1. no active goals exist (the subsystem is dormant — nothing to drift from);
  2. a progress entry was logged during the turn (``goals_update action=log``
     timestamps the store — the explicit linkage signal);
  3. the turn's user text shares keywords with an active goal's text (the cheap
     semantic proxy that keeps quick questions that legitimately need a
     terminal/file call from being flagged).

Unlinked mutating work is appended to the store's drift ledger — it never
interrupts the conversation (prompt caching and role alternation are sacred);
it surfaces through ``goals_read``, the daily/weekly reviews, and the log.
"""

import logging
import re
from datetime import datetime
from typing import Any, Iterable, List, Optional

logger = logging.getLogger("agent.goals_drift")

# Tools that change the world: turns that call one of these are "substantive
# work" worth orienting toward a goal. Read-only research is not.
MUTATING_TOOLS = frozenset({
    "write_file", "patch", "terminal", "execute_code", "process_manage",
    "browser_navigate", "browser_click", "browser_type", "browser_press",
    "browser_exec", "skill_manage", "image_generate", "text_to_speech",
})

_WORD_RE = re.compile(r"[a-z0-9]{4,}")
# Glue words that match everything; dropping them keeps the overlap test meaningful.
_STOPWORDS = frozenset({
    "this", "that", "with", "from", "what", "when", "where", "which", "your",
    "then", "them", "there", "here", "have", "into", "just", "like", "some",
    "more", "want", "need", "make", "made", "does", "done", "over", "under",
    "about", "after", "before", "please", "using", "user", "work", "them",
})
# Distinctive-enough tokens: a single shared long token counts as overlap.
_LONG_TOKEN = 8


def _tokens(text: str) -> set:
    return {t for t in _WORD_RE.findall((text or "").lower()) if t not in _STOPWORDS}


def text_matches_goal(turn_text: str, goal: dict) -> bool:
    """Keyword overlap between the turn's user text and one goal's text fields."""
    goal_tokens = set()
    for field in ("statement", "why", "next_action"):
        goal_tokens |= _tokens(str(goal.get(field) or ""))
    turn_tokens = _tokens(turn_text)
    shared = turn_tokens & goal_tokens
    return len(shared) >= 2 or any(len(t) >= _LONG_TOKEN for t in shared)


def _message_text(message: Any) -> str:
    """Best-effort text of one message row (str content or multimodal parts list)."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts)
    return ""


def turn_user_text(messages: List[Any], start: Optional[int], user_message: Any) -> str:
    """Concatenated user-authored text of this turn: the turn's user rows plus the
    raw ``user_message`` the loop was entered with (identical in the common case)."""
    rows = messages[start:] if isinstance(start, int) and 0 <= start < len(messages) else messages
    texts = [
        _message_text(row) for row in rows
        if isinstance(row, dict) and row.get("role") == "user"
    ]
    texts.append(_message_text({"content": user_message}))
    return "\n".join(t for t in texts if t)


def mutating_tools_used(messages: List[Any], start: Optional[int]) -> List[str]:
    """Names of mutating tools this turn's assistant rows called (boundary-scoped)."""
    rows = messages[start:] if isinstance(start, int) and 0 <= start < len(messages) else messages
    used = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("role") != "assistant":
            continue
        calls = row.get("tool_calls") or []
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            name = str(function.get("name") or "") if isinstance(function, dict) else ""
            if name in MUTATING_TOOLS:
                used.add(name)
    return sorted(used)


def _progress_logged_since(store, turn_started: Optional[float]) -> bool:
    """True when a progress entry landed at/after the turn's start clock."""
    if turn_started is None or turn_started <= 0:
        return True  # no proven clock: under-report drift rather than raise noise
    latest = store.latest_progress_at()
    if latest is None:
        return False
    return latest >= datetime.fromtimestamp(turn_started).astimezone()


def assess_turn_drift(goals: Iterable[dict], *, user_text: str, mutating: List[str],
                      progress_logged: bool) -> Optional[dict]:
    """Pure decision: drift event payload, or None when the turn is linked/dormant.

    Kept separate from I/O so the policy is testable as data.
    """
    active = [g for g in goals if g.get("status") == "active"]
    if not active or not mutating or progress_logged:
        return None
    if any(text_matches_goal(user_text, goal) for goal in active):
        return None
    summary = " ".join(user_text.split())[:200] or "(no user text)"
    return {"summary": summary, "tools": mutating}


def record_turn_drift(agent, messages: List[Any], *, user_message: Any,
                      current_turn_user_idx: Optional[int],
                      turn_started: Optional[float]) -> None:
    """Turn-end hook: append a drift event when this turn's work served no goal.

    Fully fail-open by contract — ``finalize_turn`` calls it bare, so a store or
    I/O failure can never take the turn result down with it.
    """
    try:
        _record_turn_drift(agent, messages, user_message=user_message,
                           current_turn_user_idx=current_turn_user_idx,
                           turn_started=turn_started)
    except Exception:
        logger.debug("goals drift detection failed", exc_info=True)


def _record_turn_drift(agent, messages: List[Any], *, user_message: Any,
                       current_turn_user_idx: Optional[int],
                       turn_started: Optional[float]) -> None:
    if "goals_read" not in (getattr(agent, "valid_tool_names", None) or set()):
        return
    from tools.goals_store import GoalsStore

    store = GoalsStore()
    goals = store.read()
    event = assess_turn_drift(
        goals,
        user_text=turn_user_text(messages, current_turn_user_idx, user_message),
        mutating=mutating_tools_used(messages, current_turn_user_idx),
        progress_logged=_progress_logged_since(store, turn_started),
    )
    if event is None:
        return
    store.record_drift(event["summary"], event["tools"])
    logger.info(
        "goals drift: turn did mutating work (%s) linked to no active goal: %s",
        ", ".join(event["tools"]), event["summary"],
    )

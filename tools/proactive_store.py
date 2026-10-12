"""ProactiveStore — the proactive loop's per-profile state and policy (``<home>/proactive.yaml``).

Cue's thesis (PLAN.md § Proactive loop): between conversations the agent works on its goals
instead of idling. This module owns the durable state that loop needs — the tick anchor
(when autonomous goal work last fired; missed ticks coalesce like heartbeats), the pick
ledger (least-recently-picked goal first, so one stuck next_action cannot monopolise the
loop) and the outbox of queued user notices — plus the policies that keep it quiet:

- **Batching**: per-tick summaries and non-urgent decision pings queue in the outbox and
  flush as ONE report per ``report_interval_minutes`` — never per event.
- **Quiet hours**: a report window that lands inside ``quiet_hours`` holds until the window
  ends (local clock via :mod:`hermes_time`, wrap-around windows supported).
- **Escalation**: a notice whose impact text matches a wake pattern sends immediately,
  quiet hours or not — "what wakes the user vs what waits". Blocked decisions escalate on
  their ``impact`` field (what stays blocked carries the urgency signal).

No scheduler lives here: the gateway watcher (``gateway/run_proactive.py``) drives ticks and
flushes while it runs; calendar cadences stay ``hermes cron`` (the goals reviews). Escalation
patterns and all knobs are ``config.yaml`` § ``proactive`` (canonical defaults in
``hermes_cli/config_defaults.py``); :func:`proactive_settings` is the one reader.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from hermes_time import now as _hermes_now
from tools.yaml_state_store import LockedYamlStore

logger = logging.getLogger("tools.proactive_store")

PROACTIVE_FILE_NAME = "proactive.yaml"
WAKE_TIER = "wake"
WAIT_TIER = "wait"
REPORT_HEADER = "📊 Cue proactive report"

# Caps keep one queued notice — and the report that renders the queue — bounded.
MAX_OUTBOX = 30
MAX_NOTICE_CHARS = 1200
REPORT_MAX_CHARS = 3500  # under Telegram's 4096 hard limit, headroom for platform overhead
MAX_PICKED_ENTRIES = 100  # pick ledger tail; older entries age out

_QUIET_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})\s*$")
_ID_RE = re.compile(r"^p(\d+)$")
_TRUNC = "…"

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _cap(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - len(_TRUNC)] + _TRUNC


def _aware(value: datetime) -> datetime:
    """Neutralise naive stamps (hand-edited files) so ordering comparisons never raise."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def parse_ts(value: Any) -> datetime:
    """A stored timestamp as an aware datetime; the epoch for anything unparseable."""
    try:
        return _aware(datetime.fromisoformat(str(value or "")))
    except ValueError:
        return _EPOCH


# ---- quiet hours -----------------------------------------------------------


def parse_quiet_hours(spec: Any) -> Optional[Tuple[dt_time, dt_time]]:
    """``"23:00-07:00"`` → ``(23:00, 07:00)`` (start may exceed end: a wrap-around window).

    None when disabled (empty) or unparseable — a bad window must never wedge reporting,
    so it degrades to "no quiet hours" rather than raising.
    """
    match = _QUIET_RE.match(str(spec or ""))
    if not match:
        return None
    start_h, start_m, end_h, end_m = (int(group) for group in match.groups())
    if max(start_h, end_h) > 23 or max(start_m, end_m) > 59:
        return None
    start, end = dt_time(start_h, start_m), dt_time(end_h, end_m)
    return None if start == end else (start, end)


def in_quiet_hours(spec: Any, now: Optional[datetime] = None) -> bool:
    """True inside the quiet window on the local clock (wrap-around aware)."""
    window = parse_quiet_hours(spec)
    if window is None:
        return False
    start, end = window
    clock = (now or _hermes_now()).time()
    if start > end:  # e.g. 23:00-07:00 wraps midnight
        return clock >= start or clock < end
    return start <= clock < end


# ---- escalation ------------------------------------------------------------

_pattern_cache: Dict[Tuple[str, ...], Tuple[re.Pattern, ...]] = {}


def _compiled_patterns(patterns: Any) -> Tuple[re.Pattern, ...]:
    raw = (patterns,) if isinstance(patterns, str) else tuple(
        str(p) for p in (patterns or ()) if str(p or "").strip())
    compiled = _pattern_cache.get(raw)
    if compiled is None:
        valid: List[re.Pattern] = []
        for pattern in raw:
            try:
                valid.append(re.compile(pattern, re.IGNORECASE))
            except re.error:
                logger.warning("proactive wake pattern %r is invalid; skipped", pattern)
        compiled = tuple(valid)
        _pattern_cache[raw] = compiled
    return compiled


def escalation_tier(impact_text: str, patterns: Any = ()) -> str:
    """:data:`WAKE_TIER` when *impact_text* matches a wake pattern, else :data:`WAIT_TIER`.

    A blocked decision escalates on its impact field — the free-text "what stays blocked"
    the agent wrote when logging the fork. Empty impact never wakes.
    """
    text = str(impact_text or "").strip()
    if not text:
        return WAIT_TIER
    return WAKE_TIER if any(pattern.search(text) for pattern in _compiled_patterns(patterns)) else WAIT_TIER


# ---- tick selection --------------------------------------------------------


def select_goal_actions(goals: List[Dict[str, Any]], picked: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    """The goals one tick works on: active goals carrying a next action, least-recently-picked
    first (a stuck next_action rotates to the back instead of monopolising the loop), then
    least-recently-updated; capped at *limit*."""
    candidates = [
        goal for goal in goals
        if isinstance(goal, dict) and goal.get("status") == "active"
        and str(goal.get("next_action") or "").strip()
    ]

    def rank(goal: Dict[str, Any]) -> Tuple[datetime, datetime, str]:
        return (parse_ts((picked or {}).get(goal["id"])), parse_ts(goal.get("updated_at")),
                str(goal.get("id") or ""))

    return sorted(candidates, key=rank)[: max(0, int(limit))]


# ---- report composition ----------------------------------------------------


def compose_report(notices: List[Dict[str, Any]]) -> Optional[str]:
    """One batched message for the queued notices; None when there is nothing to say (the
    report equivalent of cron's ``[SILENT]`` — no message, not an empty one)."""
    lines = [REPORT_HEADER]
    for notice in notices:
        text = str(notice.get("text") or "").strip()
        if not text:
            continue
        body = text.splitlines()
        lines.append("• " + body[0])
        lines.extend("  " + continuation for continuation in body[1:])
    if len(lines) == 1:
        return None
    report = "\n".join(lines)
    return _cap(report, REPORT_MAX_CHARS)


# ---- settings --------------------------------------------------------------


def proactive_settings() -> Dict[str, Any]:
    """Effective ``config.yaml`` § ``proactive`` for the ACTIVE profile (scope-aware through
    ``get_hermes_home()``; the gateway watcher runs under each served profile's scope).

    Scalar fallbacks only cover an unreadable config; the canonical defaults — including the
    wake patterns — live in ``DEFAULT_CONFIG`` so ``hermes config`` shows and migrates them.
    """
    from hermes_cli.config import load_config

    settings: Dict[str, Any] = {
        "enabled": True,
        "interval_minutes": 30,
        "max_goals_per_tick": 2,
        "report_interval_minutes": 240,
        "quiet_hours": "23:00-07:00",
        "wake_patterns": [],
    }
    try:
        section = (load_config() or {}).get("proactive")
    except Exception:
        logger.warning("proactive config unreadable; using fallback defaults", exc_info=True)
        return settings
    if isinstance(section, dict):
        settings.update({key: section[key] for key in settings if key in section})
    return settings


class ProactiveStore(LockedYamlStore):
    """Proactive-loop state for one profile home (``get_hermes_home()`` by default).

    All mutations are read-modify-write cycles under the shared flock, so the gateway
    watcher, an agent turn and a cron tick never interleave writes.
    """

    file_name = PROACTIVE_FILE_NAME

    # ---- raw state ------------------------------------------------------

    @staticmethod
    def _empty_state() -> Dict[str, Any]:
        return {"outbox": [], "last_tick_at": "", "last_report_at": "", "picked": {}}

    @staticmethod
    def _normalize_state(state: Dict[str, Any]) -> Dict[str, Any]:
        state.setdefault("outbox", [])
        state.setdefault("last_tick_at", "")
        state.setdefault("last_report_at", "")
        state.setdefault("picked", {})
        if isinstance(state["outbox"], list):
            state["outbox"] = [n for n in state["outbox"] if isinstance(n, dict)][-MAX_OUTBOX:]
        else:
            state["outbox"] = []
        if not isinstance(state["picked"], dict):
            state["picked"] = {}
        return state

    # ---- tick anchor ------------------------------------------------------

    def should_tick(self, interval_minutes: int, now: Optional[datetime] = None) -> bool:
        """True when the tick anchor aged past *interval_minutes* (missed ticks coalesce:
        the anchor resets to the last fire, never to the theoretical schedule)."""
        interval = timedelta(minutes=max(1, int(interval_minutes)))
        return (now or _hermes_now()) - parse_ts(self.load()["last_tick_at"]) >= interval

    def record_tick(self, now: Optional[datetime] = None) -> Tuple[str, str]:
        """Consume the due tick. Returns ``(written, previous)`` anchors so a caller whose
        injection failed can :meth:`refund_tick` and leave the tick due."""
        timestamp = (now or _hermes_now()).isoformat()

        def _record(state: Dict[str, Any]) -> Tuple[str, str]:
            previous = str(state.get("last_tick_at") or "")
            state["last_tick_at"] = timestamp
            return timestamp, previous

        return self._mutate(_record)

    def refund_tick(self, written: str, previous: str) -> None:
        """Restore the anchor only when it is still the one this caller wrote — a tick that
        landed in between (another process) wins."""

        def _refund(state: Dict[str, Any]) -> Dict[str, Any]:
            if str(state.get("last_tick_at") or "") == written:
                state["last_tick_at"] = str(previous or "")
            return {}

        self._mutate(_refund)

    def record_picked(self, goal_ids: List[str], now: Optional[datetime] = None) -> None:
        """Stamp the pick ledger (LRU input for :func:`select_goal_actions`)."""
        timestamp = (now or _hermes_now()).isoformat()

        def _record(state: Dict[str, Any]) -> Dict[str, Any]:
            for goal_id in goal_ids:
                if str(goal_id).strip():
                    state["picked"][str(goal_id)] = timestamp
            if len(state["picked"]) > MAX_PICKED_ENTRIES:
                state["picked"] = dict(
                    sorted(state["picked"].items(), key=lambda kv: kv[1], reverse=True)[:MAX_PICKED_ENTRIES])
            return {}

        self._mutate(_record)

    # ---- outbox -----------------------------------------------------------

    @staticmethod
    def _next_id(notices: List[Any]) -> str:
        highest = 0
        for notice in notices:
            match = _ID_RE.match(str(notice.get("id", "")) if isinstance(notice, dict) else "")
            if match:
                highest = max(highest, int(match.group(1)))
        return f"p{highest + 1}"

    def enqueue_notice(self, kind: str, text: str, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Queue one wait-tier notice for the next batched report (wake-tier notices send
        immediately and never pass through here). The oldest notice is dropped when full —
        a bounded report beats an unbounded one."""
        notice = {
            "id": self._next_id(self.load()["outbox"]),
            "kind": str(kind or "notice"),
            "text": _cap(str(text or ""), MAX_NOTICE_CHARS),
            "created_at": (now or _hermes_now()).isoformat(),
        }

        def _enqueue(state: Dict[str, Any]) -> Dict[str, Any]:
            state["outbox"].append(notice)
            if len(state["outbox"]) > MAX_OUTBOX:
                dropped = state["outbox"][:-MAX_OUTBOX]
                state["outbox"] = state["outbox"][-MAX_OUTBOX:]
                logger.warning("proactive outbox full; dropped oldest notice(s): %s",
                               ", ".join(str(n.get("id")) for n in dropped))
            return notice

        return self._mutate(_enqueue)

    def due_notices(self, report_interval_minutes: int, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """The whole outbox once the report cadence elapsed, else ``[]`` (quiet-hours and
        route checks belong to the caller — this answers cadence only)."""
        moment = now or _hermes_now()
        state = self.load()
        if not state["outbox"]:
            return []
        interval = timedelta(minutes=max(1, int(report_interval_minutes)))
        return list(state["outbox"]) if moment - parse_ts(state["last_report_at"]) >= interval else []

    def clear_notices(self, notice_ids: List[str], now: Optional[datetime] = None) -> None:
        """Remove flushed notices and stamp the report anchor (the cadence clock)."""
        wanted = {str(n) for n in notice_ids}
        timestamp = (now or _hermes_now()).isoformat()

        def _clear(state: Dict[str, Any]) -> Dict[str, Any]:
            state["outbox"] = [n for n in state["outbox"] if str(n.get("id")) not in wanted]
            state["last_report_at"] = timestamp
            return {}

        self._mutate(_clear)

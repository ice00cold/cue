"""GoalsStore — durable, first-class goal state (``<home>/goals.yaml``).

Cue's thesis: goals are state the agent orients every piece of work around, not
chat content. One YAML file per profile home holds the goal records plus two
ledgers the goals loop feeds: ``reviews`` (kind, at) and ``drift`` (work that
served no active goal — appended by agent/goals_drift.py).

Caching contract: :meth:`format_for_system_prompt` is a pure function of the
file with no timestamps, so the block is byte-stable for a given goals state.
It is rendered only where the system prompt is built (session start, or the
sanctioned compression rebuild) — NEVER per turn and never invalidated by a
goals_update call; mid-conversation changes reach the model through the tool
result and the next session's prompt.
"""

import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_time import now as _hermes_now
from utils import atomic_write_text

logger = logging.getLogger("tools.goals_store")

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt  # noqa: F401
except ImportError:
    msvcrt = None

GOALS_FILE_NAME = "goals.yaml"
VALID_GOAL_STATUSES = ("active", "paused", "done", "dropped")
ACTIVE_STATUS = "active"
REVIEW_CADENCES = ("daily", "weekly")
REVIEW_KINDS = REVIEW_CADENCES  # a review run matches a cadence

# Caps keep the store and its prompt block bounded (goal text rides the system
# prompt, so unbounded content would tax every cached prefix it lands in).
MAX_GOALS = 64
MAX_STATEMENT_CHARS = 500
MAX_WHY_CHARS = 1000
MAX_NEXT_ACTION_CHARS = 500
MAX_PROGRESS_NOTE_CHARS = 2000
MAX_PROGRESS_ENTRIES_PER_GOAL = 200  # keep the tail; older entries age out
MAX_DRIFT_EVENTS = 200
GOALS_BLOCK_MAX_CHARS = 2400

GOALS_BLOCK_HEADER = "GOALS (first-class state — orient work toward these; goals_update to change)"
_TRUNC = "…"

_ID_RE = re.compile(r"^g(\d+)$")


def _now_iso() -> str:
    return _hermes_now().isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse a store timestamp; None for anything unparseable (treated as 'old')."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _cap(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - len(_TRUNC)] + _TRUNC


def _scan_goal_text(text: str) -> Optional[str]:
    """Threat-scan one text field (goal text enters the system prompt)."""
    from tools.threat_patterns import first_threat_message

    return first_threat_message(text, scope="strict")


def _scan_field(label: str, text: str) -> Optional[str]:
    """Error string when *text* matches injection/exfil patterns, else None."""
    finding = _scan_goal_text(text)
    return f"{label}: {finding}" if finding else None


def _normalize_cadence(value: Any) -> str:
    cadence = str(value or "").strip().lower()
    return cadence if cadence in REVIEW_CADENCES else REVIEW_CADENCES[0]


def _normalize_status(value: Any, default: str = ACTIVE_STATUS) -> str:
    status = str(value or "").strip().lower()
    return status if status in VALID_GOAL_STATUSES else default


class GoalsStore:
    """YAML-backed goal state for one profile home (``get_hermes_home()`` by default).

    All mutations are read-modify-write cycles under an flock on a separate
    ``.lock`` file, so an agent turn and a cron review never interleave writes.
    """

    def __init__(self, home: Optional[Path] = None):
        if home is None:
            from hermes_constants import get_hermes_home

            home = get_hermes_home()
        self._home = Path(home)

    # ---- paths & locking ------------------------------------------------

    @property
    def path(self) -> Path:
        return self._home / GOALS_FILE_NAME

    @contextmanager
    def _file_lock(self):
        """Exclusive lock on a separate .lock file (the store itself is atomically replaced)."""
        from hermes_constants import mkdir_under_hermes_home

        lock_path = self.path.with_suffix(".yaml.lock")
        if fcntl is None and msvcrt is None:
            yield
            return
        mkdir_under_hermes_home(lock_path.parent)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        raw_fd = os.open(lock_path, flags, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(raw_fd, 0o600)
            fd = os.fdopen(raw_fd, "r+", encoding="utf-8")
        except Exception:
            os.close(raw_fd)
            raise
        with fd:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                try:
                    if fcntl:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    else:
                        fd.seek(0)
                        msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass

    # ---- raw state ------------------------------------------------------

    def load(self) -> Dict[str, Any]:
        """Whole store state; missing file → empty, unreadable file → backed up + empty."""
        from hermes_yaml import YAMLError, safe_load

        try:
            raw = self.path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return self._empty_state()
        except OSError:
            logger.warning("goals store unreadable (%s); treating as empty", self.path, exc_info=True)
            return self._empty_state()
        if not raw.strip():
            return self._empty_state()
        try:
            state = safe_load(raw)
        except YAMLError:
            backup = self.path.with_suffix(".yaml.corrupt")
            try:
                os.replace(self.path, backup)
                logger.warning(
                    "goals store was not valid YAML; moved to %s and started fresh", backup,
                    exc_info=True)
            except OSError:
                logger.warning("goals store was not valid YAML and could not be backed up", exc_info=True)
            return self._empty_state()
        if not isinstance(state, dict):
            return self._empty_state()
        state.setdefault("goals", [])
        state.setdefault("reviews", [])
        state.setdefault("drift", [])
        return state

    @staticmethod
    def _empty_state() -> Dict[str, Any]:
        return {"goals": [], "reviews": [], "drift": []}

    def save(self, state: Dict[str, Any]) -> None:
        from hermes_constants import mkdir_under_hermes_home
        from hermes_yaml import safe_dump

        mkdir_under_hermes_home(self.path.parent)
        atomic_write_text(self.path, safe_dump(state, sort_keys=False), create_mode=0o600)

    def _mutate(self, mutate) -> Dict[str, Any]:
        """Run ``mutate(state) -> result`` under the lock on a fresh read."""
        with self._file_lock():
            state = self.load()
            result = mutate(state)
            self.save(state)
            return result

    # ---- reads -----------------------------------------------------------

    def read(self) -> List[Dict[str, Any]]:
        """All goal records (normalized), in creation order."""
        goals = [g for g in self.load()["goals"] if isinstance(g, dict)]
        return [self._normalize_goal(g) for g in goals]

    def active_goals(self) -> List[Dict[str, Any]]:
        return [g for g in self.read() if g["status"] == ACTIVE_STATUS]

    @staticmethod
    def _next_id(goals: List[Any]) -> str:
        highest = 0
        for goal in goals:
            match = _ID_RE.match(str(goal.get("id", "")) if isinstance(goal, dict) else "")
            if match:
                highest = max(highest, int(match.group(1)))
        return f"g{highest + 1}"

    @staticmethod
    def _normalize_goal(goal: Dict[str, Any]) -> Dict[str, Any]:
        """One record with every field present and capped (hand-edited files stay renderable)."""
        raw_progress = goal.get("progress")
        progress = [p for p in raw_progress if isinstance(p, dict)] if isinstance(raw_progress, list) else []
        return {
            "id": str(goal.get("id") or "").strip() or "?",
            "statement": _cap(str(goal.get("statement") or ""), MAX_STATEMENT_CHARS),
            "why": _cap(str(goal.get("why") or ""), MAX_WHY_CHARS),
            "status": _normalize_status(goal.get("status")),
            "next_action": _cap(str(goal.get("next_action") or ""), MAX_NEXT_ACTION_CHARS),
            "review_cadence": _normalize_cadence(goal.get("review_cadence")),
            "created_at": str(goal.get("created_at") or ""),
            "updated_at": str(goal.get("updated_at") or ""),
            "progress": [
                {"at": str(p.get("at") or ""), "note": _cap(str(p.get("note") or ""), MAX_PROGRESS_NOTE_CHARS)}
                for p in progress[-MAX_PROGRESS_ENTRIES_PER_GOAL:]
            ],
        }

    # ---- writes ----------------------------------------------------------

    def add_goal(self, *, statement: str, why: str = "", next_action: str = "",
                 status: str = ACTIVE_STATUS, review_cadence: str = REVIEW_CADENCES[0]) -> Dict[str, Any]:
        """Create a goal; returns the stored record. Raises ValueError on invalid input."""

        def _create(state: Dict[str, Any]) -> Dict[str, Any]:
            if len([g for g in state["goals"] if isinstance(g, dict)]) >= MAX_GOALS:
                raise ValueError(f"Goal limit reached ({MAX_GOALS}). Finish or drop a goal before adding another.")
            record = self._normalize_goal({
                "id": self._next_id(state["goals"]),
                "statement": statement,
                "why": why,
                "status": status,
                "next_action": next_action,
                "review_cadence": review_cadence,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "progress": [],
            })
            state["goals"].append(record)
            return record

        return self._mutate(_create)

    def update_goal(self, goal_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Patch one goal's fields; returns the updated record. Raises KeyError/ValueError."""

        def _update(state: Dict[str, Any]) -> Dict[str, Any]:
            goal = self._find_goal(state, goal_id)
            for field in ("statement", "why", "status", "next_action", "review_cadence"):
                if field in updates and updates[field] is not None:
                    goal[field] = updates[field]
            goal["updated_at"] = _now_iso()
            return self._normalize_goal(goal)

        return self._mutate(_update)

    def log_progress(self, goal_id: str, note: str) -> Dict[str, Any]:
        """Append one progress entry; returns the updated record."""

        def _log(state: Dict[str, Any]) -> Dict[str, Any]:
            goal = self._find_goal(state, goal_id)
            entries = goal["progress"] if isinstance(goal.get("progress"), list) else []
            entries.append({"at": _now_iso(), "note": _cap(note, MAX_PROGRESS_NOTE_CHARS)})
            goal["progress"] = entries[-MAX_PROGRESS_ENTRIES_PER_GOAL:]
            goal["updated_at"] = _now_iso()
            return self._normalize_goal(goal)

        return self._mutate(_log)

    def remove_goal(self, goal_id: str) -> Dict[str, Any]:
        """Hard-delete a goal (prefer ``status=dropped`` to keep the history)."""

        def _remove(state: Dict[str, Any]) -> Dict[str, Any]:
            goal = self._find_goal(state, goal_id)
            state["goals"] = [g for g in state["goals"] if g is not goal]
            return goal

        return self._mutate(_remove)

    @staticmethod
    def _find_goal(state: Dict[str, Any], goal_id: str) -> Dict[str, Any]:
        wanted = str(goal_id or "").strip().lower()
        for goal in state["goals"]:
            if isinstance(goal, dict) and str(goal.get("id", "")).strip().lower() == wanted:
                return goal
        raise KeyError(f"No goal with id '{goal_id}'.")

    # ---- ledgers ---------------------------------------------------------

    def record_drift(self, summary: str, tools: List[str]) -> None:
        """Append a drift event (substantive turn work that served no active goal)."""

        def _record(state: Dict[str, Any]) -> Dict[str, Any]:
            state["drift"].append({
                "at": _now_iso(),
                "summary": _cap(summary, 240),
                "tools": sorted(set(str(t) for t in tools))[:12],
            })
            state["drift"] = state["drift"][-MAX_DRIFT_EVENTS:]
            return {}

        self._mutate(_record)

    def record_review(self, kind: str) -> None:
        if kind not in REVIEW_KINDS:
            return

        def _record(state: Dict[str, Any]) -> Dict[str, Any]:
            state["reviews"].append({"kind": kind, "at": _now_iso()})
            state["reviews"] = state["reviews"][-50:]
            return {}

        self._mutate(_record)

    def last_review_at(self, kind: str) -> Optional[datetime]:
        for review in reversed(self.load()["reviews"]):
            if isinstance(review, dict) and review.get("kind") == kind:
                stamp = _parse_iso(review.get("at"))
                if stamp is not None:
                    return stamp
        return None

    def latest_progress_at(self) -> Optional[datetime]:
        """Newest progress timestamp across all goals (the drift linkage signal)."""
        latest: Optional[datetime] = None
        for goal in self.load()["goals"]:
            for entry in (goal.get("progress") or []) if isinstance(goal, dict) else []:
                stamp = _parse_iso(entry.get("at") if isinstance(entry, dict) else None)
                if stamp is not None and (latest is None or stamp > latest):
                    latest = stamp
        return latest

    # ---- system prompt block ----------------------------------------------

    def format_for_system_prompt(self) -> Optional[str]:
        """Compact active-goal block, or None with no active goals (empty state → no
        prompt change at all). Pure function of the file: no timestamps, capped fields."""
        goals = self.active_goals()
        if not goals:
            return None
        lines = [GOALS_BLOCK_HEADER]
        for goal in goals:
            line = f"- [{goal['id']}] {goal['statement']}"
            if goal["next_action"]:
                line += f" — next: {goal['next_action']}"
            lines.append(_cap(line, 160))
        counts = self.status_counts()
        tail = [f"{count} {status}" for status, count in counts.items() if status != ACTIVE_STATUS and count]
        if tail:
            lines.append("(" + ", ".join(tail) + ")")
        lines.append("Log progress via goals_update (action=log) as goal work completes.")
        block = "\n".join(lines)
        return block[:GOALS_BLOCK_MAX_CHARS] if len(block) > GOALS_BLOCK_MAX_CHARS else block or None

    def status_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for goal in self.read():
            counts[goal["status"]] = counts.get(goal["status"], 0) + 1
        return counts

    # ---- review assembly ---------------------------------------------------

    def review_payload(self, kind: str, *, record: bool = True) -> Dict[str, Any]:
        """Structured data for a daily/weekly review; the calling model writes the prose."""
        kind = _normalize_cadence(kind)
        since = self.last_review_at(kind)
        now = _hermes_now()
        goals_out = []
        for goal in self.read():
            recent = [
                entry for entry in goal["progress"]
                if (stamp := _parse_iso(entry.get("at"))) is not None
                and (since is None or stamp >= since)
            ]
            goals_out.append({
                **{k: goal[k] for k in ("id", "statement", "why", "status", "next_action", "review_cadence", "updated_at")},
                "progress_since_last_review": recent,
                "days_since_update": self._days_between(_parse_iso(goal.get("updated_at")), now),
            })
        drift = [
            event for event in self.load()["drift"]
            if (stamp := _parse_iso(event.get("at"))) is not None
            and (since is None or stamp >= since)
        ]
        payload = {
            "kind": kind,
            "since_last_review": since.isoformat() if since else None,
            "goals": goals_out,
            "active": sum(1 for g in goals_out if g["status"] == ACTIVE_STATUS),
            "drift_since_last_review": drift,
            "review_focus": (
                "Weekly deep review: challenge each active goal — is the why still true, is "
                "next_action still the right move, should anything be paused, merged or dropped?"
                if kind == "weekly" else
                "Daily summary: one line per active goal — what moved, what is next."
            ),
        }
        if record:
            self.record_review(kind)
        return payload

    @staticmethod
    def _days_between(then: Optional[datetime], now: datetime) -> Optional[int]:
        if then is None:
            return None
        return max(0, round((now - then).total_seconds() / 86400))


def validate_goal_fields(*, statement: str = "", why: str = "", next_action: str = "",
                         status: str = "", review_cadence: str = "", note: str = "",
                         require_statement: bool = False) -> Optional[str]:
    """Error string for invalid goal input, or None when valid (used by the tool layer)."""
    if require_statement and not (statement or "").strip():
        return "statement is required."
    if require_statement and len(statement) > MAX_STATEMENT_CHARS:
        return f"statement is too long (max {MAX_STATEMENT_CHARS} chars)."
    for label, text, limit in (
        ("why", why, MAX_WHY_CHARS), ("next_action", next_action, MAX_NEXT_ACTION_CHARS),
        ("note", note, MAX_PROGRESS_NOTE_CHARS),
    ):
        if len(text or "") > limit:
            return f"{label} is too long (max {limit} chars)."
    if status and status not in VALID_GOAL_STATUSES:
        return f"status must be one of {', '.join(VALID_GOAL_STATUSES)}."
    if review_cadence and review_cadence not in REVIEW_CADENCES:
        return f"review_cadence must be one of {', '.join(REVIEW_CADENCES)}."
    for label, text in (
        ("statement", statement), ("why", why), ("next_action", next_action), ("note", note),
    ):
        if text:
            finding = _scan_field(label, text)
            if finding:
                return finding
    return None

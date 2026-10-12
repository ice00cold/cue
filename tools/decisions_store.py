"""DecisionsStore — the pending-decision queue (``<home>/decisions.yaml``).

Cue's thesis (PLAN.md § Decision surfacing): the agent never silently stalls at a
fork in the road. When work needs a human call, the agent logs the fork here — the
question, the realistic options with each option's impact, its recommendation, and
what stays blocked while the decision waits — pings the user, then moves to other
work. Answers arrive through ``/decisions`` (or chat) and are recorded with a
rationale, so the file is also the durable record of *why* the road taken was taken.

No system-prompt surface: decisions reach the model through the decisions tools,
the ping and the command — never a prompt block, so nothing here is prompt-cached.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from hermes_time import now as _hermes_now
from tools.yaml_state_store import LockedYamlStore

logger = logging.getLogger("tools.decisions_store")

DECISIONS_FILE_NAME = "decisions.yaml"
VALID_DECISION_STATUSES = ("pending", "resolved", "withdrawn")
PENDING_STATUS = "pending"
RESOLVED_STATUS = "resolved"
WITHDRAWN_STATUS = "withdrawn"

# Caps keep one record — and the ping + /decisions listing that render it — bounded.
MAX_DECISIONS = 200  # total records kept (pending + history)
MAX_OPTIONS = 6
MAX_QUESTION_CHARS = 500
MAX_CONTEXT_CHARS = 1500
MAX_OPTION_LABEL_CHARS = 160
MAX_OPTION_IMPACT_CHARS = 500
MAX_RECOMMENDATION_CHARS = 800
MAX_IMPACT_CHARS = 800
MAX_RESOLUTION_CHARS = 500
MAX_RATIONALE_CHARS = 1500
PING_MAX_CHARS = 3500  # under Telegram's 4096 hard limit, headroom for platform overhead
MAX_QUEUE_NOTICE_CHARS = 400  # the batched report renders the queue; one headline per decision

_ID_RE = re.compile(r"^d(\d+)$")


def _now_iso() -> str:
    return _hermes_now().isoformat()


def _cap(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _scan_field(label: str, text: str) -> Optional[str]:
    """Error string when *text* matches injection/exfil patterns, else None."""
    from tools.threat_patterns import first_threat_message

    finding = first_threat_message(text, scope="strict")
    return f"{label}: {finding}" if finding else None


def _normalize_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    return status if status in VALID_DECISION_STATUSES else PENDING_STATUS


class DecisionsStore(LockedYamlStore):
    """YAML-backed pending-decision queue for one profile home (``get_hermes_home()`` by default)."""

    file_name = DECISIONS_FILE_NAME

    # ---- raw state ------------------------------------------------------

    @staticmethod
    def _empty_state() -> Dict[str, Any]:
        return {"decisions": []}

    @staticmethod
    def _normalize_state(state: Dict[str, Any]) -> Dict[str, Any]:
        state.setdefault("decisions", [])
        return state

    # ---- reads -----------------------------------------------------------

    def read(self) -> List[Dict[str, Any]]:
        """All decision records (normalized), in creation order."""
        records = [d for d in self.load()["decisions"] if isinstance(d, dict)]
        return [self._normalize_decision(d) for d in records]

    def open_decisions(self) -> List[Dict[str, Any]]:
        return [d for d in self.read() if d["status"] == PENDING_STATUS]

    def pending_count(self) -> int:
        return len(self.open_decisions())

    def get(self, decision_id: str) -> Optional[Dict[str, Any]]:
        wanted = str(decision_id or "").strip().lower()
        for decision in self.read():
            if decision["id"].lower() == wanted:
                return decision
        return None

    @staticmethod
    def _normalize_decision(decision: Dict[str, Any]) -> Dict[str, Any]:
        """One record with every field present and capped (hand-edited files stay renderable)."""
        raw_options = decision.get("options")
        options = [
            {"label": _cap(str(o.get("label") or ""), MAX_OPTION_LABEL_CHARS),
             "impact": _cap(str(o.get("impact") or ""), MAX_OPTION_IMPACT_CHARS)}
            for o in raw_options if isinstance(o, dict)
        ] if isinstance(raw_options, list) else []
        return {
            "id": str(decision.get("id") or "").strip() or "?",
            "question": _cap(str(decision.get("question") or ""), MAX_QUESTION_CHARS),
            "context": _cap(str(decision.get("context") or ""), MAX_CONTEXT_CHARS),
            "options": options[:MAX_OPTIONS],
            "recommendation": _cap(str(decision.get("recommendation") or ""), MAX_RECOMMENDATION_CHARS),
            "impact": _cap(str(decision.get("impact") or ""), MAX_IMPACT_CHARS),
            "status": _normalize_status(decision.get("status")),
            "created_at": str(decision.get("created_at") or ""),
            "resolved_at": str(decision.get("resolved_at") or ""),
            "resolution": _cap(str(decision.get("resolution") or ""), MAX_RESOLUTION_CHARS),
            "rationale": _cap(str(decision.get("rationale") or ""), MAX_RATIONALE_CHARS),
        }

    @staticmethod
    def _next_id(decisions: List[Any]) -> str:
        highest = 0
        for decision in decisions:
            match = _ID_RE.match(str(decision.get("id", "")) if isinstance(decision, dict) else "")
            if match:
                highest = max(highest, int(match.group(1)))
        return f"d{highest + 1}"

    @staticmethod
    def _find_decision(state: Dict[str, Any], decision_id: str) -> Dict[str, Any]:
        wanted = str(decision_id or "").strip().lower()
        for decision in state["decisions"]:
            if isinstance(decision, dict) and str(decision.get("id", "")).strip().lower() == wanted:
                return decision
        raise KeyError(f"No decision with id '{decision_id}'.")

    # ---- writes ----------------------------------------------------------

    def add_decision(self, *, question: str, context: str = "", options: Any = (),
                     recommendation: str = "", impact: str = "") -> Dict[str, Any]:
        """Queue a pending decision; returns the stored record.

        Raises ValueError when the question is empty, no option survives cleaning, or the
        record cap is reached — the fork is real and the caller must hear about it.
        """
        normalized_options = [
            {"label": _cap(str(o.get("label") or ""), MAX_OPTION_LABEL_CHARS),
             "impact": _cap(str(o.get("impact") or ""), MAX_OPTION_IMPACT_CHARS)}
            for o in options if isinstance(o, dict)
        ]
        if not str(question or "").strip():
            raise ValueError("question is required.")
        if not normalized_options:
            raise ValueError("at least one option is required (2-6 recommended).")

        def _create(state: Dict[str, Any]) -> Dict[str, Any]:
            if len([d for d in state["decisions"] if isinstance(d, dict)]) >= MAX_DECISIONS:
                raise ValueError(
                    f"Decision limit reached ({MAX_DECISIONS}). Resolve or withdraw old ones first.")
            record = self._normalize_decision({
                "id": self._next_id(state["decisions"]),
                "question": question,
                "context": context,
                "options": normalized_options,
                "recommendation": recommendation,
                "impact": impact,
                "status": PENDING_STATUS,
                "created_at": _now_iso(),
            })
            state["decisions"].append(record)
            return record

        return self._mutate(_create)

    def resolve_decision(self, decision_id: str, resolution: str, rationale: str = "") -> Dict[str, Any]:
        """Record the user's answer (choice + rationale); returns the updated record.

        Raises KeyError for an unknown id and ValueError when the decision is not pending
        (a resolved fork stays answered; re-asking is a new decision).
        """

        def _resolve(state: Dict[str, Any]) -> Dict[str, Any]:
            decision = self._find_decision(state, decision_id)
            if decision.get("status") != PENDING_STATUS:
                raise ValueError(f"Decision {decision.get('id')} is already {decision.get('status')}.")
            decision["status"] = RESOLVED_STATUS
            decision["resolution"] = _cap(resolution, MAX_RESOLUTION_CHARS)
            decision["rationale"] = _cap(rationale, MAX_RATIONALE_CHARS)
            decision["resolved_at"] = _now_iso()
            return self._normalize_decision(decision)

        return self._mutate(_resolve)

    def withdraw_decision(self, decision_id: str, reason: str = "") -> Dict[str, Any]:
        """Close a pending decision as moot without a choice (reason stored as the rationale)."""

        def _withdraw(state: Dict[str, Any]) -> Dict[str, Any]:
            decision = self._find_decision(state, decision_id)
            if decision.get("status") != PENDING_STATUS:
                raise ValueError(f"Decision {decision.get('id')} is already {decision.get('status')}.")
            decision["status"] = WITHDRAWN_STATUS
            decision["rationale"] = _cap(reason, MAX_RATIONALE_CHARS)
            decision["resolved_at"] = _now_iso()
            return self._normalize_decision(decision)

        return self._mutate(_withdraw)

    # ---- choice mapping ---------------------------------------------------

    @staticmethod
    def choice_text(record: Dict[str, Any], choice: Any) -> str:
        """The stored resolution for *choice*: an option's label when *choice* is that
        option's 1-based number, else the raw text (free-form answers are first-class)."""
        text = str(choice or "").strip()
        options = record.get("options") or []
        if text.isdigit():
            index = int(text)
            if 1 <= index <= len(options) and options[index - 1].get("label"):
                return options[index - 1]["label"]
        return text

    # ---- rendering --------------------------------------------------------

    @staticmethod
    def format_open_list(decisions: List[Dict[str, Any]]) -> str:
        """Compact listing for menus and replies; one line per decision plus the answer hint."""
        if not decisions:
            return "No pending decisions."
        lines: List[str] = []
        for decision in decisions:
            lines.append(f"[{decision['id']}] {decision['question']}")
            if decision["recommendation"]:
                lines.append(f"    ↳ recommends: {decision['recommendation']}")
        lines.append("Answer: /decisions answer <id> <option number|choice> [rationale] — detail: /decisions show <id>")
        return "\n".join(lines)

    @staticmethod
    def format_detail(decision: Dict[str, Any]) -> str:
        """Full record: question, context, numbered options, recommendation, impact, outcome."""
        lines = [f"[{decision['id']}] {decision['question']}"]
        if decision["context"]:
            lines.append(decision["context"])
        for index, option in enumerate(decision["options"], 1):
            line = f"{index}. {option['label']}"
            if option["impact"]:
                line += f" — {option['impact']}"
            lines.append(line)
        if decision["recommendation"]:
            lines.append(f"Recommendation: {decision['recommendation']}")
        if decision["impact"]:
            lines.append(f"Impact: {decision['impact']}")
        if decision["status"] == RESOLVED_STATUS:
            lines.append(f"Resolved → {decision['resolution']}")
            if decision["rationale"]:
                lines.append(f"Rationale: {decision['rationale']}")
        elif decision["status"] == WITHDRAWN_STATUS:
            suffix = f" ({decision['rationale']})" if decision["rationale"] else ""
            lines.append(f"Withdrawn{suffix}")
        return "\n".join(lines)

    @staticmethod
    def format_ping(decision: Dict[str, Any]) -> str:
        """Self-contained ping body for the profile's messaging home channel."""
        lines = [f"🔔 Decision needed — [{decision['id']}] {decision['question']}"]
        for index, option in enumerate(decision["options"], 1):
            line = f"{index}. {option['label']}"
            if option["impact"]:
                line += f" — {option['impact']}"
            lines.append(line)
        if decision["recommendation"]:
            lines.append(f"Recommendation: {decision['recommendation']}")
        if decision["impact"]:
            lines.append(f"Impact: {decision['impact']}")
        lines.append(f"Answer here or with /decisions answer {decision['id']} <number|choice> [rationale]")
        text = "\n".join(lines)
        return text if len(text) <= PING_MAX_CHARS else _cap(text, PING_MAX_CHARS)

    @staticmethod
    def format_queue_notice(decision: Dict[str, Any]) -> str:
        """One-line notice for the proactive outbox (the batched report renders the queue,
        so a decision there is a headline, not the full fork detail — ``/decisions`` has it)."""
        line = f"decision [{decision['id']}] {decision['question']}"
        if decision["recommendation"]:
            line += f" — recommends: {decision['recommendation']}"
        line += f" (/decisions show {decision['id']})"
        return _cap(line, MAX_QUEUE_NOTICE_CHARS)


def validate_decision_fields(*, question: str = "", context: str = "", options: Any = (),
                             recommendation: str = "", impact: str = "", resolution: str = "",
                             rationale: str = "", require_question: bool = False,
                             require_options: bool = False) -> Optional[str]:
    """Error string for invalid decision input, or None when valid (used by the tool layer)."""
    if require_question and not (question or "").strip():
        return "question is required."
    if require_question and len(question) > MAX_QUESTION_CHARS:
        return f"question is too long (max {MAX_QUESTION_CHARS} chars)."
    cleaned_options = [o for o in options if isinstance(o, dict)] if isinstance(options, (list, tuple)) else []
    if require_options:
        labels = [str(o.get("label") or "").strip() for o in cleaned_options]
        if not any(labels):
            return "at least one option is required (2-6 recommended)."
        if len(labels) > MAX_OPTIONS:
            return f"too many options (max {MAX_OPTIONS})."
    for label, text, limit in (
        ("question", question, MAX_QUESTION_CHARS), ("context", context, MAX_CONTEXT_CHARS),
        ("recommendation", recommendation, MAX_RECOMMENDATION_CHARS),
        ("impact", impact, MAX_IMPACT_CHARS), ("resolution", resolution, MAX_RESOLUTION_CHARS),
        ("rationale", rationale, MAX_RATIONALE_CHARS),
    ):
        if len(text or "") > limit:
            return f"{label} is too long (max {limit} chars)."
    for index, option in enumerate(cleaned_options, 1):
        if len(str(option.get("label") or "")) > MAX_OPTION_LABEL_CHARS:
            return f"option {index} label is too long (max {MAX_OPTION_LABEL_CHARS} chars)."
        if len(str(option.get("impact") or "")) > MAX_OPTION_IMPACT_CHARS:
            return f"option {index} impact is too long (max {MAX_OPTION_IMPACT_CHARS} chars)."
    scans = (
        ("question", question), ("context", context), ("recommendation", recommendation),
        ("impact", impact), ("resolution", resolution), ("rationale", rationale),
    )
    scans += tuple((f"option {index} label", str(o.get("label") or "")) for index, o in enumerate(cleaned_options, 1))
    scans += tuple((f"option {index} impact", str(o.get("impact") or "")) for index, o in enumerate(cleaned_options, 1))
    for label, text in scans:
        if text:
            finding = _scan_field(label, text)
            if finding:
                return finding
    return None

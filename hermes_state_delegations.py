"""Async-delegation ledger reads for SessionDB (Cue Phase 6 M2 — R2 drill-down).

The durable ``async_delegations`` table (state.db; written by
``tools/async_delegation.py`` in whichever process dispatched the work) is the
delegation drill-down's data. This mixin exposes bounded, display-shaped reads
through the profile's own SessionDB connection — the dashboard cannot use the
tool module's module-global connection because it binds the LAUNCH profile's
home, not the profile a request scopes to. Parsing tolerant: a malformed JSON
column degrades to ``None`` rather than failing the whole drill-down.
"""

import json
import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence

from hermes_state_common import _id_chunks

logger = logging.getLogger("hermes_state")

# Per-delegation task summaries in one list row: batches can carry hundreds of
# units (a 1,393-agent run exists); the drill-down needs state at a glance and
# counts, not every goal in the list response.
_MAX_TASK_SUMMARIES = 20

_DELEGATION_COLUMNS = (
    "delegation_id", "origin_session", "origin_ui_session_id", "origin_session_id",
    "parent_session_id", "state", "dispatched_at", "completed_at", "updated_at",
    "delivery_state", "task_json", "result_json",
)


def _parse_json_object(raw: Optional[str]) -> Dict[str, Any]:
    """Parsed JSON object, or ``{}`` for empty/malformed/non-object payloads."""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        logger.warning("malformed JSON column in async_delegations row", exc_info=True)
        return {}
    return value if isinstance(value, dict) else {}


def _summarize_result(result: Dict[str, Any], state: str, goals: Sequence[Any]) -> Dict[str, Any]:
    """Bounded display projection of ``result_json`` for one delegation row.

    Single tasks keep their summary/error/duration; batches collapse to one
    status line per task (capped) plus totals — the transcript-grade record
    stays in the origin conversation, reachable through the linked session.
    """
    tasks = result.get("results") if isinstance(result.get("results"), list) else None
    if tasks is None:
        return {
            "status": str(result.get("status") or state or ""),
            "summary": result.get("summary"),
            "error": result.get("error"),
            "api_calls": result.get("api_calls"),
            "duration_seconds": result.get("duration_seconds"),
            "exit_reason": result.get("exit_reason"),
        }
    goal_for = dict(zip(range(len(goals)), goals)) if goals else {}
    summaries: List[Dict[str, Any]] = []
    for index, entry in enumerate(tasks):
        if not isinstance(entry, dict):
            continue
        task_index = entry.get("task_index")
        goal = (
            entry.get("goal") or goal_for.get(
                task_index if isinstance(task_index, int) else index) or "")
        summaries.append({
            "task_index": task_index if isinstance(task_index, int) else index,
            "status": str(entry.get("status") or ""),
            "goal": str(goal or ""),
            "error": entry.get("error"),
        })
    return {
        "status": str(result.get("status") or state or ""),
        "error": result.get("error"),
        "task_count": len(tasks),
        "total_duration_seconds": result.get("total_duration_seconds"),
        "tasks": summaries[:_MAX_TASK_SUMMARIES],
        "truncated": len(summaries) > _MAX_TASK_SUMMARIES,
    }


def _delegation_row(row) -> Dict[str, Any]:
    record = dict(row)
    task = _parse_json_object(record.pop("task_json"))
    result = _parse_json_object(record.pop("result_json"))
    goals = task.get("goals") if isinstance(task.get("goals"), list) else []
    record["task"] = task or None
    record["result"] = _summarize_result(result, str(record.get("state") or ""), goals) if (
        result or record.get("completed_at")) else None
    return record


class SessionDelegationsMixin:
    """Read-only queries over the durable ``async_delegations`` ledger."""

    def list_async_delegations(
        self, *, limit: int = 50, session_ids: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Delegation rows newest-dispatched first, optionally scoped to a conversation.

        ``session_ids=None`` lists the store; any other iterable scopes to
        delegations whose origin or parent session is one of its ids (the
        caller passes the whole compression chain, so rotation keeps a
        thread's delegations attached to it). An empty scope matches nothing.
        """
        cols = ", ".join(_DELEGATION_COLUMNS)
        sql = f"SELECT {cols} FROM async_delegations"
        if session_ids is None:
            rows = self._read_all(f"{sql} ORDER BY dispatched_at DESC LIMIT ?", (limit,))
            return [_delegation_row(row) for row in rows]
        ids = [str(s) for s in session_ids if s]
        if not ids:
            return []
        rows: List[Any] = []
        for chunk in _id_chunks(ids):
            placeholders = ", ".join("?" for _ in chunk)
            sql_chunk = (
                f"{sql} WHERE origin_session_id IN ({placeholders}) "
                f"OR parent_session_id IN ({placeholders}) "
                "ORDER BY dispatched_at DESC LIMIT ?")
            rows.extend(self._read_all(sql_chunk, (*chunk, *chunk, limit)))
            if len(rows) >= limit:
                break
        return [_delegation_row(row) for row in rows[:limit]]

    def get_async_delegation(self, delegation_id: str) -> Optional[Dict[str, Any]]:
        """One delegation row by id, or ``None``."""
        cols = ", ".join(_DELEGATION_COLUMNS)
        row = self._read_one(
            f"SELECT {cols} FROM async_delegations WHERE delegation_id = ?", (delegation_id,))
        return _delegation_row(row) if row else None

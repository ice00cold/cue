"""Session lineage mixin for SessionDB (Cue Phase 6 M2 — R2 drill-down).

The Thread surface walks the session DAG: worker/subagent, branch and cron
children hang off a conversation's ``parent_session_id`` links. This mixin owns
the one bounded query that traversal needs — the direct children of a set of
parents — so the lineage router never reaches into SessionDB private helpers.
Classifying a child edge (compression continuation vs. real child) stays with
the chain walkers in :mod:`hermes_state_compression`; callers exclude chain
members themselves.
"""

from typing import Any, Dict, Iterable, List

from hermes_state_common import _id_chunks

# Display projection for drill-down nodes: the sessions-list shape minus
# prompt/model storage internals (the router stamps the computed fields).
_LINEAGE_CHILD_COLUMNS = (
    "id", "source", "session_key", "title", "parent_session_id", "model",
    "started_at", "ended_at", "end_reason", "message_count", "tool_call_count",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "estimated_cost_usd", "cwd", "git_branch", "last_activity_at",
    "last_activity_description", "archived", "pinned", "hidden",
)


class SessionLineageMixin:
    """Read-only child-session discovery over ``sessions.parent_session_id``."""

    def list_child_sessions(self, parent_ids: Iterable[str]) -> List[Dict[str, Any]]:
        """Direct child rows whose ``parent_session_id`` is in *parent_ids*, oldest first.

        Every edge class is returned (workers, branches, cron/tool children and
        compression continuations alike): the parent-indexed scan is the raw
        graph step, and callers decide which edges are the same-conversation
        chain. ``idx_sessions_parent`` makes this bounded by the parents asked
        about, never by total store size.
        """
        parents = [str(p) for p in parent_ids if p]
        if not parents:
            return []
        cols = ", ".join(_LINEAGE_CHILD_COLUMNS)
        rows: List[Dict[str, Any]] = []
        for chunk in _id_chunks(parents):
            placeholders = ", ".join("?" for _ in chunk)
            sql = (
                f"SELECT {cols} FROM sessions "
                f"WHERE parent_session_id IN ({placeholders}) "
                "ORDER BY started_at ASC, id ASC")
            rows.extend(dict(row) for row in self._read_all(sql, chunk))
        return rows

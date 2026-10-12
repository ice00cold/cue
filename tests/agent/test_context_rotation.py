"""Topic rotation: /new folds the finished topic into durable artifacts, same session.

Covers the Phase-1 invariants (PLAN.md § One main thread): rotation never changes the
session, archives exactly the rows that left live context, and writes a topic artifact
next to the rolling transcript.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from agent.context_rotation import (
    MIN_ROTATE_MESSAGES,
    append_archive_transcript,
    render_rotation_result,
    rotate_topic,
    write_topic_artifact,
)


def _history(n_pairs: int = 8, start_row: int = 1) -> List[Dict[str, Any]]:
    """Alternating user/assistant exchanges with persisted-row identities and timestamps."""
    messages: List[Dict[str, Any]] = []
    row = start_row
    for i in range(n_pairs):
        messages.append({"role": "user", "content": f"user turn {i}", "timestamp": f"2026-10-08T10:{i:02d}:00Z", "_row_id": row})
        row += 1
        messages.append({"role": "assistant", "content": f"assistant turn {i}", "timestamp": f"2026-10-08T10:{i:02d}:05Z", "_row_id": row})
        row += 1
    return messages


class _FakeAgent:
    """Minimal agent double: `_compress_context` folds the head into one summary row."""

    def __init__(self, home: Path, session_id: str = "20261008_main"):
        self.session_id = session_id
        self._session_db = SimpleNamespace(db_path=str(home / "state.db"))
        self.context_compressor = None
        self.compressed_heads: List[List[Dict[str, Any]]] = []
        self._compression_skipped_due_to_lock = None

    def _compress_context(self, head, _system_message=None, **_kw):  # noqa: ARG002
        self.compressed_heads.append(list(head))
        summary = {"role": "user", "content": "Topic summary: the earlier exchanges folded here.",
                   "_compressed_summary": True, "timestamp": head[-1].get("timestamp")}
        return [summary], None


def test_rotate_topic_compresses_archives_and_writes_artifact(tmp_path):
    agent = _FakeAgent(tmp_path)
    before = _history()
    result = rotate_topic(agent, before, title="Deploy pipeline")

    assert result.status == "rotated"
    # Same session: rotation never touches the session identity.
    assert agent.session_id == "20261008_main"
    # The compressor received the head (topic body), and the surface gets head-summary + tail.
    assert agent.compressed_heads and len(agent.compressed_heads[0]) < len(before)
    assert result.after_messages[0].get("_compressed_summary") is True
    assert len(result.after_messages) < len(before)

    # Archive: exactly the rows that left live context, one JSON line each, session stamped.
    assert result.archive_path is not None and result.archive_path.exists()
    lines = [json.loads(line) for line in result.archive_path.read_text(encoding="utf-8").splitlines() if line]
    assert lines and all(line["session"] == "20261008_main" for line in lines)
    assert all(line["topic"] == "Deploy pipeline" for line in lines)
    archived_contents = {line["content"] for line in lines}
    kept_contents = {str(m.get("content")) for m in result.after_messages}
    assert archived_contents == {str(m.get("content")) for m in before} - kept_contents

    # Artifact: markdown note under archive/topics with the summary and recall pointer.
    assert result.artifact_path is not None and result.artifact_path.exists()
    artifact = result.artifact_path.read_text(encoding="utf-8")
    assert "# Deploy pipeline" in artifact
    assert "Topic summary: the earlier exchanges folded here." in artifact
    assert "20261008_main" in artifact
    assert result.artifact_path.parent == tmp_path / "archive" / "topics"


def test_rotate_topic_short_history_is_a_no_op(tmp_path):
    agent = _FakeAgent(tmp_path)
    short = _history(1)[: MIN_ROTATE_MESSAGES - 1]
    result = rotate_topic(agent, short)
    assert result.status == "nothing_to_do"
    assert result.after_messages == short
    assert not agent.compressed_heads
    assert not (tmp_path / "archive").exists()


def test_rotate_topic_lock_skip_leaves_no_artifacts(tmp_path):
    agent = _FakeAgent(tmp_path)
    agent._compression_skipped_due_to_lock = True
    before = _history()
    result = rotate_topic(agent, before)
    assert result.status == "lock_skipped"
    assert result.after_messages == before
    assert not (tmp_path / "archive").exists()
    assert any("in progress" in line or "skipped" in line for line in render_rotation_result(result))


def test_rotate_topic_derives_title_from_first_user_message(tmp_path):
    agent = _FakeAgent(tmp_path)
    before = _history()
    result = rotate_topic(agent, before)
    assert result.title == "user turn 0"
    assert "Rotated topic: user turn 0" in render_rotation_result(result)[0]


def test_append_archive_transcript_rolls_per_month_and_appends(tmp_path):
    rows = _history(2)
    first = append_archive_transcript(tmp_path, rows, session_id="s1", topic="t1")
    second = append_archive_transcript(tmp_path, rows[:2], session_id="s1", topic="t2")
    assert first == second  # same month → same rolling file
    lines = [json.loads(line) for line in first.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == len(rows) + 2
    assert [line["topic"] for line in lines] == ["t1"] * len(rows) + ["t2", "t2"]


def test_write_topic_artifact_without_summary_still_records_the_topic(tmp_path):
    path = write_topic_artifact(
        tmp_path, title="Empty topic", session_id="s9", summary_text="",
        window=("2026-10-08T10:00:00Z", "2026-10-08T11:00:00Z"))
    text = path.read_text(encoding="utf-8")
    assert "# Empty topic" in text
    assert "rolling archive" in text
    assert "s9" in text

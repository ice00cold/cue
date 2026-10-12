"""Topic rotation — ``/new`` in Cue folds the finished topic away, it never starts over.

Cue has one continuous main thread per profile. When a topic ends, the raw transcript has
done its job: its conclusions survive in three durable places and the live context moves on
in the SAME session (the session id never changes, so the transcript, the routing entry and
the agent cache all keep their identity):

1. the rolling transcript archive — append-only JSONL under ``<home>/archive/main-thread/``,
   the belt-and-suspenders copy that outlives any state.db maintenance;
2. a topic artifact — a markdown note under ``<home>/archive/topics/`` recording the topic's
   summary and time window, readable by the user, the model (file tools) and session_search;
3. the conversation's own compaction summary — written by the regular compression pipeline
   (``compress_now``), which stays the ONE sanctioned cache break.

The sequence mirrors manual ``/compress``: archive the rows that leave live context, run an
aggressive partial compression, write the artifact, hand ``after_messages`` back for the
surface to install. History is never mutated here.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

try:  # cross-platform append lock (mirrors tools/memory_tool.py: patched there in tests)
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None

#: Recent exchanges kept verbatim after a rotation — a topic boundary, not a hard reset, so
#: the conversation keeps its last exchanges for immediate follow-ups.
ROTATION_KEEP_LAST = 4

#: Below this there is no topic body to fold (matches manual /compress).
MIN_ROTATE_MESSAGES = 4

#: Archive layout under the profile home (per-profile: the home IS the profile boundary).
ARCHIVE_DIR = "archive"
TOPICS_DIR = "topics"
TRANSCRIPT_DIR = "main-thread"

_TITLE_MAX = 70


@dataclass
class RotationResult:
    """Outcome of one rotation; the surface installs ``after_messages`` like /compress."""

    status: str  # "rotated" | "nothing_to_do" | "lock_skipped"
    before_messages: List[Dict[str, Any]]
    after_messages: List[Dict[str, Any]]
    title: str = ""
    artifact_path: Optional[Path] = None
    archive_path: Optional[Path] = None
    summary: Optional[Dict[str, Any]] = None
    lock_holder: Any = None

    @property
    def rotated(self) -> bool:
        return self.status == "rotated"


def rotate_topic(
    agent: Any, history: Sequence[Dict[str, Any]], *, focus_topic: Optional[str] = None,
    title: Optional[str] = None, task_id: str = "default", system_message: Any = None,
    home: Optional[Path] = None,
) -> RotationResult:
    """Rotate the current topic of ``history`` on ``agent``; the caller installs the new history.

    ``home`` defaults to the owning profile home derived from the agent's session DB — the
    ground truth for where archive artifacts belong (never ``os.environ``).
    """
    from agent.conversation_compression_manual import CompressRequest, compress_now

    before = list(history)
    if len(before) < MIN_ROTATE_MESSAGES:
        return RotationResult("nothing_to_do", before, before)
    resolved_home = home or _agent_home(agent)
    request = CompressRequest(partial=True, keep_last=ROTATION_KEEP_LAST, focus_topic=focus_topic)
    result = compress_now(agent, before, request, system_message=system_message, task_id=task_id)
    if result.status != "compressed":
        return RotationResult(result.status, before, before, lock_holder=result.lock_holder)

    archived = _rows_leaving_context(before, result.after_messages)
    archive_path = None
    artifact_path = None
    topic_title = _sanitize_title(title or focus_topic or _derive_title(archived or before))
    session_id = str(getattr(agent, "session_id", "") or "")
    if archived and resolved_home is not None:
        archive_path = append_archive_transcript(
            resolved_home, archived, session_id=session_id, topic=topic_title)
    if resolved_home is not None:
        artifact_path = write_topic_artifact(
            resolved_home, title=topic_title, session_id=session_id,
            summary_text=_summary_text(result.after_messages),
            window=_time_window(archived or before), detail=result.summary or {})
    return RotationResult(
        "rotated", before, result.after_messages, title=topic_title,
        artifact_path=artifact_path, archive_path=archive_path, summary=result.summary)


def _agent_home(agent: Any) -> Optional[Path]:
    """Owning profile home from the agent's session DB path (never the launch environment)."""
    db = getattr(agent, "_session_db", None)
    db_path = getattr(db, "db_path", None)
    return Path(db_path).parent if db_path else None


def _message_identity(message: Dict[str, Any]) -> str:
    """Stable identity for archive diffing: persisted row id when present, else a content hash."""
    row_id = message.get("_row_id")
    if isinstance(row_id, int) and not isinstance(row_id, bool):
        return f"row:{row_id}"
    payload = json.dumps(
        {k: message.get(k) for k in ("role", "content", "tool_call_id", "name")},
        sort_keys=True, default=str, ensure_ascii=False)
    return "hash:" + str(hash(payload))


def _rows_leaving_context(
    before: Sequence[Dict[str, Any]], after: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """The topic body: rows of ``before`` absent from ``after`` (the compaction replaced them)."""
    kept = {_message_identity(m) for m in after}
    return [m for m in before if _message_identity(m) not in kept]


def _summary_text(after_messages: Sequence[Dict[str, Any]]) -> str:
    """The compaction summary the pipeline generated (first summary row of the new head)."""
    from agent.context_compressor import is_compaction_summary_message

    for message in after_messages:
        if isinstance(message, dict) and is_compaction_summary_message(message):
            return str(message.get("content") or "")
    return ""


def _time_window(messages: Sequence[Dict[str, Any]]) -> tuple[Optional[str], Optional[str]]:
    def _ts(message: Dict[str, Any]) -> Optional[str]:
        ts = message.get("timestamp")
        return str(ts) if ts else None

    timestamps = [t for t in map(_ts, messages) if t]
    return (timestamps[0], timestamps[-1]) if timestamps else (None, None)


def _derive_title(messages: Sequence[Dict[str, Any]]) -> str:
    """First user message of the topic, trimmed — the cheapest stable topic label."""
    for message in messages:
        content = str(message.get("content") or "")
        if message.get("role") == "user" and content.strip():
            return content
    return ""


def _sanitize_title(raw: str) -> str:
    folded = " ".join(str(raw or "").split())
    folded = re.sub(r"[\r\n]+", " ", folded)
    return folded[:_TITLE_MAX].rstrip()


def _archive_dirs(home: Path) -> tuple[Path, Path]:
    return home / ARCHIVE_DIR / TOPICS_DIR, home / ARCHIVE_DIR / TRANSCRIPT_DIR


def append_archive_transcript(
    home: Path, messages: Sequence[Dict[str, Any]], *, session_id: str, topic: str = "",
) -> Optional[Path]:
    """Append the topic body to the rolling per-month JSONL transcript under ``home``.

    One line per message: ``ts/role/content/topic/session`` — enough for grep and for a future
    re-import, without duplicating state.db's rich rows. Appends are flock-serialized so a CLI
    rotation and a gateway rotation cannot interleave half-lines.
    """
    if not messages:
        return None
    _, transcript_dir = _archive_dirs(home)
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_dir / f"{datetime.now(timezone.utc):%Y-%m}.jsonl"
    lines = []
    for message in messages:
        lines.append(json.dumps({
            "ts": str(message.get("timestamp") or ""),
            "role": str(message.get("role") or ""),
            "content": str(message.get("content") or ""),
            "topic": topic,
            "session": session_id,
        }, ensure_ascii=False, default=str))
    payload = "\n".join(lines) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        _lock_append_handle(fh)
        try:
            fh.write(payload)
            fh.flush()
        finally:
            _unlock_append_handle(fh)
    return path


def _lock_append_handle(fh: Any) -> None:
    if fcntl is not None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    elif msvcrt is not None:  # pragma: no cover - Windows
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)


def _unlock_append_handle(fh: Any) -> None:
    if fcntl is not None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - Windows
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


def write_topic_artifact(
    home: Path, *, title: str, session_id: str, summary_text: str,
    window: tuple[Optional[str], Optional[str]], detail: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write one markdown topic artifact under ``<home>/archive/topics/`` and return its path.

    Artifacts are the topic-end memory of the main thread: durable, greppable and surfaced by
    session_search alongside the transcript itself.
    """
    from utils import atomic_write_text

    topics_dir, _ = _archive_dirs(home)
    topics_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "topic").lower()).strip("-")[:40] or "topic"
    path = topics_dir / f"{stamp}-{slug}.md"
    lines = [
        f"# {title or 'Topic'}",
        "",
        f"- session: `{session_id or 'unknown'}`",
    ]
    if window[0] or window[1]:
        lines.append(f"- window: {window[0] or '…'} → {window[1] or '…'}")
    if detail and detail.get("token_line"):
        lines.append(f"- {detail['token_line']}")
    lines += ["", "## Summary", ""]
    lines.append(summary_text.strip() or "_No summary was generated; the raw transcript stays in the rolling archive._")
    lines += [
        "",
        "## Recall",
        "",
        "Raw turns for this topic remain in the rolling archive and state.db —",
        "`session_search(query=..., session_id=\"" + session_id + "\")` recalls them.",
        "",
    ]
    atomic_write_text(path, "\n".join(lines))
    return path


def render_rotation_result(result: RotationResult, *, prefix: str = "") -> List[str]:
    """Surface-neutral text lines for a rotation outcome."""
    if result.status == "lock_skipped":
        from agent.manual_compression_feedback import describe_compression_lock_skip
        return [f"{prefix}{describe_compression_lock_skip(result.lock_holder or True)}"]
    if result.status == "nothing_to_do":
        return [f"{prefix}Nothing to rotate yet — the conversation is still short."]
    lines = [f"{prefix}Rotated topic: {result.title or 'untitled'}"]
    summary = result.summary or {}
    for key in ("headline", "token_line"):
        if summary.get(key):
            lines.append(f"{prefix}{summary[key]}")
    if result.artifact_path is not None:
        lines.append(f"{prefix}Artifact: {result.artifact_path}")
    lines.append(f"{prefix}The conversation continues in the same session — nothing was reset.")
    return lines

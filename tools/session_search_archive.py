"""Archive search for ``session_search`` — the rolling transcript + topic artifacts.

Cue rotates finished topics out of live context into durable artifacts (``agent/
context_rotation.py``): an append-only JSONL transcript per month under
``<home>/archive/main-thread/`` and one markdown note per topic under ``<home>/archive/topics/``.
state.db still holds the rows (in-place compaction keeps them searchable), but the archive
outlives any database maintenance — so recall falls back to it when FTS finds nothing, and
topic summaries surface as first-class hits even when their raw rows are long gone.

Pure-function module: no DB, no registry — ``session_search_tool`` shapes the hits.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("tools.session_search")

#: Scan caps: the archive is unbounded by design, a query is not. Newest files first, so the
#: caps bound OLD history, never recent topics.
_MAX_TRANSCRIPT_FILES = 36  # three years of monthly files
_MAX_LINES_PER_FILE = 200_000
_SNIPPET_CHARS = 240
_TOPIC_HEADING = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _terms(query: str) -> List[str]:
    """Case-insensitive search terms; an empty list matches nothing (never everything)."""
    return [t.lower() for t in (query or "").split() if t.strip()]


def _matches(content: str, terms: List[str]) -> bool:
    lowered = content.lower()
    return all(t in lowered for t in terms)


def _snippet(content: str, terms: List[str]) -> str:
    """Excerpt centred on the first matching term, term-cased markers preserved."""
    text = content or ""
    lowered = text.lower()
    pos = min((lowered.find(t) for t in terms if t in lowered), default=-1)
    if pos < 0:
        return text[:_SNIPPET_CHARS].strip()
    start = max(0, pos - _SNIPPET_CHARS // 3)
    return text[start:start + _SNIPPET_CHARS].strip()


def _archive_home(home: Optional[Path]) -> Path:
    from hermes_constants import get_hermes_home

    return Path(home) if home is not None else get_hermes_home()


def _transcript_files(root: Path) -> List[Path]:
    """Monthly JSONL files, newest first (name format ``YYYY-MM`` sorts lexically)."""
    directory = root / "archive" / "main-thread"
    try:
        return sorted(directory.glob("*.jsonl"), reverse=True)[:_MAX_TRANSCRIPT_FILES]
    except OSError:
        return []


def _topic_files(root: Path) -> List[Path]:
    directory = root / "archive" / "topics"
    try:
        return sorted(directory.glob("*.md"), reverse=True)
    except OSError:
        return []


def search_transcript_archive(
    query: str, *, home: Optional[Path] = None, limit: int = 3,
) -> List[Dict[str, Any]]:
    """Newest-first hits from the rolling JSONL transcript; one entry per matching line."""
    terms = _terms(query)
    if not terms or limit <= 0:
        return []
    hits: List[Dict[str, Any]] = []
    for path in _transcript_files(_archive_home(home)):
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError as exc:
            logger.debug("archive transcript unreadable: %s (%s)", path, exc)
            continue
        for line in reversed(lines[-_MAX_LINES_PER_FILE:]):  # newest line first within a month
            try:
                row = json.loads(line)
            except ValueError:
                continue
            content = str(row.get("content") or "")
            if not content or not _matches(content, terms):
                continue
            hits.append({
                "source": "archive",
                "session_id": str(row.get("session") or "") or None,
                "topic": str(row.get("topic") or "") or None,
                "when": str(row.get("ts") or "") or None,
                "matched_role": str(row.get("role") or "") or None,
                "snippet": _snippet(content, terms),
                "archive": str(path),
            })
            if len(hits) >= limit:
                return hits
    return hits


def search_topic_artifacts(
    query: str, *, home: Optional[Path] = None, limit: int = 3,
) -> List[Dict[str, Any]]:
    """Newest-first hits from the topic artifact notes (rotation summaries)."""
    terms = _terms(query)
    if not terms or limit <= 0:
        return []
    hits: List[Dict[str, Any]] = []
    for path in _topic_files(_archive_home(home)):
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            logger.debug("topic artifact unreadable: %s (%s)", path, exc)
            continue
        if not _matches(text, terms):
            continue
        heading = _TOPIC_HEADING.search(text)
        # First matching line keeps the hit anchored in the note's own words.
        anchor = next((l.strip() for l in text.splitlines() if _matches(l, terms)), "")
        hits.append({
            "source": "topic_artifact",
            "session_id": None,
            "title": heading.group(1).strip() if heading else path.stem,
            "when": None,
            "matched_role": "topic_summary",
            "snippet": _snippet(anchor or text, terms),
            "archive": str(path),
        })
        if len(hits) >= limit:
            return hits
    return hits


def search_archive(query: str, *, home: Optional[Path] = None, limit: int = 3) -> List[Dict[str, Any]]:
    """Rolling-transcript + topic-artifact hits, newest class first (transcript, then topics).

    Entries are discovery-shaped (``source`` distinguishes them from state.db FTS hits); the
    caller owns link/rank integration and truncation."""
    return [*search_transcript_archive(query, home=home, limit=limit),
            *search_topic_artifacts(query, home=home, limit=limit)][:max(0, limit)]

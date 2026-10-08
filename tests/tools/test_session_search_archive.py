"""Archive search: the rolling transcript + topic artifacts answer when FTS has no match.

Cue rotates finished topics out of live context into ``<home>/archive/main-thread/*.jsonl``
(rolling per-month transcript) and ``<home>/archive/topics/*.md`` (topic notes).
``session_search`` falls back to them so rotated-out content stays recallable — PLAN.md § One
main thread ("session_search becomes archive search over the rolling transcript + artifacts").
"""

from __future__ import annotations

import json

from tools.session_search_archive import search_archive, search_topic_artifacts, search_transcript_archive


def _write_month(home, month: str, rows):
    directory = home / "archive" / "main-thread"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{month}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def _write_topic(home, name: str, text: str):
    directory = home / "archive" / "topics"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(text, encoding="utf-8")


def test_transcript_hits_are_newest_first_and_carry_provenance(tmp_path):
    _write_month(tmp_path, "2026-08", [
        {"ts": "2026-08-01T00:00:00", "role": "user", "content": "kubernetes upgrade plan",
         "topic": "k8s", "session": "s_old"},
    ])
    _write_month(tmp_path, "2026-09", [
        {"ts": "2026-09-01T00:00:00", "role": "user", "content": "unrelated chatter", "session": "s_x"},
        {"ts": "2026-09-02T00:00:00", "role": "assistant", "content": "the kubernetes upgrade went fine",
         "topic": "k8s", "session": "s_new"},
    ])

    hits = search_transcript_archive("kubernetes", home=tmp_path)
    assert len(hits) == 2
    assert hits[0]["session_id"] == "s_new"  # newest month, newest line within it
    assert hits[0]["topic"] == "k8s"
    assert hits[0]["source"] == "archive"
    assert "kubernetes" in hits[0]["snippet"].lower()
    assert hits[1]["session_id"] == "s_old"


def test_all_terms_must_match_and_limit_is_respected(tmp_path):
    _write_month(tmp_path, "2026-09", [
        {"ts": "2026-09-01", "role": "user", "content": "alpha only", "session": "s1"},
        {"ts": "2026-09-02", "role": "user", "content": "alpha beta", "session": "s2"},
        {"ts": "2026-09-03", "role": "user", "content": "alpha beta gamma", "session": "s3"},
    ])

    assert [h["session_id"] for h in search_transcript_archive("alpha beta", home=tmp_path)] == ["s3", "s2"]
    assert search_transcript_archive("alpha zeta", home=tmp_path) == []
    assert len(search_transcript_archive("alpha", home=tmp_path, limit=1)) == 1


def test_topic_artifacts_match_and_surface_their_title(tmp_path):
    _write_topic(tmp_path, "20260901-120000-k8s-upgrade.md",
                 "# Kubernetes upgrade\n\n- session: `s1`\n\n## Summary\n\nRolled the control plane to 1.31.\n")
    _write_topic(tmp_path, "20260902-120000-garden.md",
                 "# Garden plan\n\n## Summary\n\nTomatoes again.\n")

    hits = search_topic_artifacts("kubernetes control plane", home=tmp_path)
    assert len(hits) == 1
    assert hits[0]["title"] == "Kubernetes upgrade"
    assert hits[0]["source"] == "topic_artifact"
    assert "control plane" in hits[0]["snippet"].lower()


def test_merged_search_prefers_transcript_then_artifacts(tmp_path):
    _write_month(tmp_path, "2026-09", [
        {"ts": "2026-09-02", "role": "user", "content": "terraform module registry", "session": "s1"},
    ])
    _write_topic(tmp_path, "20260901-120000-tf.md", "# Terraform audit\n\nmodule registry drift\n")

    hits = search_archive("terraform", home=tmp_path, limit=3)
    assert [h["source"] for h in hits] == ["archive", "topic_artifact"]
    assert len(search_archive("terraform", home=tmp_path, limit=1)) == 1


def test_empty_query_matches_nothing(tmp_path):
    _write_month(tmp_path, "2026-09", [
        {"ts": "2026-09-01", "role": "user", "content": "anything", "session": "s1"},
    ])
    assert search_archive("", home=tmp_path) == []
    assert search_archive("   ", home=tmp_path) == []


def test_discovery_falls_back_to_the_archive_when_fts_has_no_match(tmp_path, monkeypatch):
    """End to end through session_search: an empty state.db plus a matching rolling archive
    answers with archive entries instead of "No matching sessions found"."""
    from hermes_state import SessionDB
    from tools.session_search_tool import session_search

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    _write_month(tmp_path / "home", "2026-09", [
        {"ts": "2026-09-02", "role": "user", "content": "the observability retrofit plan",
         "topic": "observability", "session": "s_rotated"},
    ])
    db = SessionDB(tmp_path / "state.db")
    try:
        payload = json.loads(session_search("observability retrofit", db=db))
    finally:
        db.close()

    assert payload["mode"] == "discover"
    assert payload["count"] == 1
    hit = payload["results"][0]
    assert hit["source"] == "archive"
    assert hit["session_id"] == "s_rotated"
    assert hit["archive"].endswith(".jsonl")
    assert "rotated-out topics" in payload["message"]


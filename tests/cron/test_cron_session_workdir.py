"""Cue: cron fires run in the profile's ONE persistent main thread (cron/main_thread.py).

The workdir stays per-FIRE scope (tools + context files for that run): the shared main-session
row is never retitled, re-cwd'd or ended, and a cold store (no gateway has ever chatted) mints
the main-thread row itself so every later fire — and the gateway's first message — lands in the
same conversation.
"""

from __future__ import annotations

from unittest.mock import patch

from cron.scheduler import run_job
from hermes_state import SessionDB

_RUNTIME = {
    "api_key": "test-key",
    "base_url": "https://example.invalid/v1",
    "provider": "openrouter",
    "api_mode": "chat_completions",
}


class _FakeCronAgent:
    """Stand-in for the cron AIAgent: creates its session row on the first turn exactly
    like AIAgent's lazy create (source 'cron', no cwd — _launch_cwd_for_session records
    none for cron source)."""

    def __init__(self, *args, session_id=None, session_db=None, **kwargs):
        self.session_id = session_id
        self.session_db = session_db

    def run_conversation(self, user_message, conversation_history=None, task_id=None):
        if self.session_db is not None:
            self.session_db.create_session(self.session_id, source="cron")
        return {"final_response": "ok"}


def _run_job_with_real_db(job, db, tmp_path):
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state_registry.acquire", return_value=db), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=_RUNTIME), \
         patch("run_agent.AIAgent", _FakeCronAgent):
        return run_job(job)


def _main_rows(db):
    """Rows under the profile's main-thread key (Cue: fires run in the persistent main thread)."""
    return [dict(r) for r in db._read_all(
        "SELECT * FROM sessions WHERE session_key LIKE 'agent:%:main-thread'")]


def test_run_job_never_stamps_workdir_on_the_persistent_main_row(tmp_path):
    """A workdir is per-FIRE scope (tools + context files for that run); stamping it on the
    shared main-session row would churn the whole conversation's cwd — it must stay unset."""
    db = SessionDB(db_path=tmp_path / "state.db")
    repo = tmp_path / "repo"
    repo.mkdir()
    job = {"id": "workdir-job", "name": "workdir job", "prompt": "hello", "workdir": str(repo)}

    try:
        success, _output, _final, error = _run_job_with_real_db(job, db, tmp_path)

        assert success is True, error
        rows = _main_rows(db)
        assert len(rows) == 1
        assert rows[0]["cwd"] is None
    finally:
        db.close()


def test_run_job_creates_the_main_thread_row_under_the_main_key(tmp_path):
    """A fire on a cold store (no gateway has ever chatted) mints the main-thread row itself so
    every later fire — and the gateway's first message — lands in the same conversation."""
    from cron.main_thread import main_thread_session_key_for_current_profile

    db = SessionDB(db_path=tmp_path / "state.db")
    job = {"id": "cold-start-job", "name": "cold start", "prompt": "hello"}

    try:
        success, _output, _final, error = _run_job_with_real_db(job, db, tmp_path)

        assert success is True, error
        rows = _main_rows(db)
        assert len(rows) == 1
        assert rows[0]["session_key"] == main_thread_session_key_for_current_profile()
        assert rows[0]["cwd"] is None
    finally:
        db.close()

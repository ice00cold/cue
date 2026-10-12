"""Regression tests for bounded cron post-run cleanup.

A cron worker must release its in-memory dispatch guard even when an agent
resource finalizer stops returning after the model turn has ended. (The
session-finalization hang class is gone with Cue: a fire appends to the
persistent main thread and the finalize path performs no session DB ops.)
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

from cron.scheduler import _teardown_cron_agent, run_job
from cron.scheduler_detached_worker import defer_teardown_to_running_worker


_RUNTIME = {
    "api_key": "test-key",
    "base_url": "https://example.invalid/v1",
    "provider": "openrouter",
    "api_mode": "chat_completions",
}


class HangingAgent:
    def __init__(self, release: threading.Event):
        self.release = release
        self.entered = threading.Event()

    def close(self):
        self.entered.set()
        self.release.wait()


def test_agent_teardown_is_bounded():
    release = threading.Event()
    agent = HangingAgent(release)

    try:
        started = time.monotonic()
        _teardown_cron_agent(agent, "cleanup-agent-hang", timeout_seconds=0.02)
        elapsed = time.monotonic() - started

        assert agent.entered.wait(timeout=2.0)
        assert elapsed < 5.0
    finally:
        release.set()


def test_detached_worker_teardown_waits_for_future():
    """A timed-out worker keeps its agent and SessionDB until its Future completes."""
    future = Future()
    fake_db = MagicMock()
    agent = MagicMock()

    with patch("cron.scheduler._finalize_cron_session") as finalize, \
         patch("cron.scheduler._teardown_cron_agent") as teardown_agent:
        assert defer_teardown_to_running_worker(
            future, fake_db, agent, "detached-worker", "detached worker", "cron_detached-worker") is True
        finalize.assert_not_called()
        teardown_agent.assert_not_called()

        future.set_result({"final_response": "late"})

        finalize.assert_called_once_with(fake_db, agent, "detached-worker", "detached worker",
                                         "cron_detached-worker", workdir=None, persistent=False)
        teardown_agent.assert_called_once_with(agent, "detached-worker")
    assert defer_teardown_to_running_worker(
        future, fake_db, agent, "detached-worker", "detached worker", "cron_detached-worker") is False

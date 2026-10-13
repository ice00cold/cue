"""``GET /api/delegations`` + ``/api/delegations/{id}`` — the delegation half
of the Thread surface's drill-down (PLAN.md Phase 6 M2 / R2).

The durable ``async_delegations`` ledger is the only place a finished
delegation still exists (the live roster forgets it). The router reads it
through the profile's SessionDB with bounded, display-shaped rows: state,
goal and a per-task result summary, never the raw result blob. Pinned here:
the projection (single vs batch), newest-first ordering, the
compression-chain-aware session filter (a rotated thread keeps its
delegations), the honest empty answers, profile isolation and malformed-JSON
tolerance.
"""

import json
import time

import pytest


class _DelegationEndpoints:
    """Shared harness: authenticated TestClient against the isolated home's store.

    Ledger rows are seeded through the delegation tool's OWN persistence calls
    (the real writer shapes), not hand-written SQL.
    """

    @pytest.fixture(autouse=True)
    def _setup_test_client(self, monkeypatch, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_constants import (
            get_hermes_home, reset_hermes_home_override, set_hermes_home_override)
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
        from tools import async_delegation as ad

        self.home = get_hermes_home()
        monkeypatch.setattr(
            hermes_state, "DEFAULT_DB_PATH", self.home / "state.db"
        )
        self._home_token = set_hermes_home_override(str(self.home))
        ad._reset_for_tests()
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
        yield
        ad._reset_for_tests()
        reset_hermes_home_override(self._home_token)

    def _seed_session(self, session_id="main-1", *, parent=None, end_reason=None):
        from hermes_state import SessionDB

        db = SessionDB(self.home / "state.db")
        try:
            db.create_session(session_id=session_id, source="cli",
                              parent_session_id=parent)
            if end_reason:
                db.end_session(session_id, end_reason=end_reason)
            return db
        finally:
            db.close()

    def _seed_delegation(self, delegation_id, *, status="completed", result=None,
                         origin_session_id="main-1", parent_session_id="main-1",
                         dispatched_at=None, **task):
        from tools import async_delegation as ad

        ad._persist_dispatch({
            "delegation_id": delegation_id, "session_key": "agent:main:main-thread",
            "origin_ui_session_id": "", "origin_session_id": origin_session_id,
            "parent_session_id": parent_session_id,
            "dispatched_at": dispatched_at or (time.time() - 60), **task})
        if result is not None:
            ad._persist_completion(
                {"delegation_id": delegation_id, "status": status,
                 "completed_at": time.time() - 30},
                result)


class TestListDelegations(_DelegationEndpoints):
    def test_an_empty_ledger_is_an_honest_empty_list(self):
        body = self.client.get("/api/delegations").json()
        assert body["delegations"] == []
        assert body["count"] == 0

    def test_a_single_delegation_carries_goal_and_result_summary(self):
        self._seed_delegation(
            "d-single", goal="ship M2",
            result={"status": "completed", "summary": "drilled down", "api_calls": 7,
                    "duration_seconds": 12.5, "exit_reason": "stop"})

        row = self.client.get("/api/delegations").json()["delegations"][0]
        assert row["delegation_id"] == "d-single"
        assert row["state"] == "completed"
        assert row["task"]["goal"] == "ship M2"
        assert row["result"]["status"] == "completed"
        assert row["result"]["summary"] == "drilled down"
        assert row["result"]["api_calls"] == 7
        assert row["result"]["exit_reason"] == "stop"
        # Raw JSON columns never leave the store: the projection replaced them.
        assert "result_json" not in row and "task_json" not in row

    def test_a_running_delegation_has_no_result_projection(self):
        self._seed_delegation("d-running", goal="still going")

        row = self.client.get("/api/delegations").json()["delegations"][0]
        assert row["state"] == "running"
        assert row["task"]["goal"] == "still going"
        assert row["result"] is None

    def test_a_batch_collapses_to_per_task_status_lines_with_totals(self):
        self._seed_delegation(
            "d-batch", is_batch=True, goals=["scan api", "scan web", "scan db"],
            result={"results": [
                {"task_index": 0, "status": "success", "goal": "scan api"},
                {"task_index": 1, "status": "error", "goal": "scan web", "error": "401"},
                {"task_index": 2, "status": "success", "goal": "scan db"},
            ], "total_duration_seconds": 40.0})

        result = self.client.get("/api/delegations").json()["delegations"][0]["result"]
        assert result["task_count"] == 3
        assert [t["status"] for t in result["tasks"]] == ["success", "error", "success"]
        assert result["tasks"][1]["error"] == "401"
        assert result["truncated"] is False

    def test_a_huge_batch_truncates_its_task_lines_but_keeps_the_count(self):
        tasks = [{"task_index": i, "status": "success", "goal": f"g{i}"} for i in range(60)]
        self._seed_delegation("d-big", is_batch=True, result={"results": tasks})

        result = self.client.get("/api/delegations").json()["delegations"][0]["result"]
        assert result["task_count"] == 60
        assert len(result["tasks"]) == 20
        assert result["truncated"] is True

    def test_rows_list_newest_dispatch_first(self):
        now = time.time()
        self._seed_delegation("d-old", goal="first", dispatched_at=now - 300,
                              result={"status": "completed"})
        self._seed_delegation("d-new", goal="second", dispatched_at=now - 10,
                              result={"status": "completed"})

        ids = [row["delegation_id"] for row in
               self.client.get("/api/delegations").json()["delegations"]]
        assert ids == ["d-new", "d-old"]

    def test_session_filter_keeps_a_rotated_threads_delegations(self):
        """The ledger points at the chain member that was live at dispatch;
        the filter is chain-aware, so rotation does not orphan the history."""
        db = self._seed_session("main-1")
        try:
            db.end_session("main-1", end_reason="compression")
            db.create_session(session_id="main-2", source="cli", parent_session_id="main-1")
        finally:
            db.close()
        self._seed_delegation("d-before-rotation", goal="early",
                              origin_session_id="main-1", parent_session_id="main-1",
                              result={"status": "completed"})
        self._seed_delegation("d-after-rotation", goal="late",
                              origin_session_id="main-2", parent_session_id="main-2",
                              result={"status": "completed"})
        self._seed_delegation("d-foreign", goal="elsewhere",
                              origin_session_id="other-sess", parent_session_id="other-sess",
                              result={"status": "completed"})

        body = self.client.get(
            "/api/delegations", params={"session_id": "main-2"}).json()
        ids = {row["delegation_id"] for row in body["delegations"]}
        assert ids == {"d-before-rotation", "d-after-rotation"}

    def test_an_unknown_session_filters_to_nothing_not_an_error(self):
        """The Thread surface filters by a routing id that may not exist yet
        (no thread); that is an answer, not a 404 (the M1 resolver contract)."""
        body = self.client.get(
            "/api/delegations", params={"session_id": "never-created"}).json()
        assert body["delegations"] == []
        assert body["count"] == 0

    def test_named_profile_reads_its_own_ledger(self, tmp_path):
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from tools import async_delegation as ad

        profile_home = self.home / "profiles" / "ops"
        profile_home.mkdir(parents=True)
        token = set_hermes_home_override(str(profile_home))
        try:
            self._seed_delegation("d-ops", goal="ops work")
        finally:
            reset_hermes_home_override(token)
        self._seed_delegation("d-default", goal="default work")

        body = self.client.get(
            "/api/delegations", params={"profile": "ops"}).json()
        assert body["profile"] == "ops"
        assert [row["delegation_id"] for row in body["delegations"]] == ["d-ops"]

        default_body = self.client.get("/api/delegations").json()
        assert [row["delegation_id"] for row in default_body["delegations"]] == ["d-default"]

    def test_a_malformed_result_column_degrades_to_null_result(self):
        from hermes_state import SessionDB

        self._seed_delegation("d-fine", goal="ok", result={"status": "completed"})
        db = SessionDB(self.home / "state.db")

        def _corrupt(conn):
            conn.execute(
                "UPDATE async_delegations SET result_json = '{not json' "
                "WHERE delegation_id = 'd-fine'")

        try:
            db._execute_write(_corrupt)
        finally:
            db.close()

        row = self.client.get("/api/delegations").json()["delegations"][0]
        assert row["delegation_id"] == "d-fine"
        assert row["result"] is None or "status" in row["result"]


class TestGetDelegation(_DelegationEndpoints):
    def test_detail_returns_the_full_record(self):
        self._seed_delegation(
            "d-1", goal="one", context="the thread asked",
            result={"status": "completed", "summary": "done"})

        body = self.client.get("/api/delegations/d-1").json()
        assert body["delegation_id"] == "d-1"
        assert body["task"]["goal"] == "one"
        assert body["task"]["context"] == "the thread asked"
        assert body["origin_session_id"] == "main-1"
        assert body["profile"] == "default"

    def test_unknown_delegation_is_a_404(self):
        assert self.client.get("/api/delegations/nope").status_code == 404

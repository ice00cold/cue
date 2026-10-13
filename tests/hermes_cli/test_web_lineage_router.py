"""``GET /api/sessions/{id}/lineage`` — the Thread surface's drill-down tree
(PLAN.md Phase 6 M2 / R2).

The one-conversation model still spawns child sessions (workers, cron runs,
branches); the drill-down must walk them over ``parent_session_id`` while
collapsing each conversation's own compression chain — a rotated worker is one
node whose transcript opens at the live tip. Pinned here: the tree shape, the
chain collapse (both the focus session's and each child's), the honest 404 for
an unknown session, profile isolation, and the runaway-graph truncation flag.
"""

import pytest


class _LineageEndpoints:
    """Shared harness: authenticated TestClient against the isolated home's store."""

    @pytest.fixture(autouse=True)
    def _setup_test_client(self, monkeypatch, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_constants import get_hermes_home
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        self.home = get_hermes_home()
        monkeypatch.setattr(
            hermes_state, "DEFAULT_DB_PATH", self.home / "state.db"
        )
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def _db(self, home=None):
        from hermes_state import SessionDB

        return SessionDB((home or self.home) / "state.db")


class TestGetSessionLineage(_LineageEndpoints):
    def test_a_session_without_children_is_an_honest_empty_tree(self):
        db = self._db()
        try:
            db.create_session(session_id="main-1", source="cli")
            db.append_message("main-1", role="user", content="hello")
        finally:
            db.close()

        body = self.client.get("/api/sessions/main-1/lineage").json()
        assert body["session_id"] == "main-1"
        assert body["chain"] == ["main-1"]
        assert body["live_session_id"] == "main-1"
        assert body["children"] == []
        assert body["truncated"] is False

    def test_worker_and_cron_children_form_the_tree_with_grandchildren_nested(self):
        db = self._db()
        try:
            db.create_session(session_id="main-1", source="cli")
            db.create_session(session_id="worker-1", source="subagent",
                              parent_session_id="main-1")
            db.append_message("worker-1", role="assistant", content="scouting")
            db.set_session_title("worker-1", "Scout worker")
            db.create_session(session_id="worker-1a", source="subagent",
                              parent_session_id="worker-1")
            db.create_session(session_id="cron_job1_20260101_000000", source="cron",
                              parent_session_id="main-1")
        finally:
            db.close()

        body = self.client.get("/api/sessions/main-1/lineage").json()
        ids = [node["id"] for node in body["children"]]
        assert ids == ["worker-1", "cron_job1_20260101_000000"]

        worker = body["children"][0]
        assert worker["source"] == "subagent"
        assert worker["title"] == "Scout worker"
        assert worker["child_count"] == 1
        assert [child["id"] for child in worker["children"]] == ["worker-1a"]

        cron = body["children"][1]
        assert cron["source"] == "cron"
        assert cron["child_count"] == 0

    def test_the_focus_sessions_own_rotation_is_not_a_child(self):
        """A compression continuation of the REQUESTED session is the same
        conversation: it lands in ``chain``/``live_session_id``, never as a
        drill-down node."""
        db = self._db()
        try:
            db.create_session(session_id="main-1", source="cli")
            db.append_message("main-1", role="user", content="before")
            db.end_session("main-1", end_reason="compression")
            db.create_session(session_id="main-2", source="cli", parent_session_id="main-1")
            db.append_message("main-2", role="user", content="after")
        finally:
            db.close()

        # Asking from either end of the chain yields the same tree answer.
        for sid in ("main-1", "main-2"):
            body = self.client.get(f"/api/sessions/{sid}/lineage").json()
            assert body["chain"] == ["main-1", "main-2"]
            assert body["live_session_id"] == "main-2"
            assert body["children"] == []

    def test_a_rotated_worker_projects_to_its_live_tip(self):
        """A worker that compressed is ONE node addressed by its tip: opening
        the node's transcript (the existing messages endpoint) shows the whole
        worker conversation."""
        db = self._db()
        try:
            db.create_session(session_id="main-1", source="cli")
            db.create_session(session_id="worker-1", source="subagent",
                              parent_session_id="main-1")
            db.append_message("worker-1", role="assistant", content="part one")
            db.end_session("worker-1", end_reason="compression")
            db.create_session(session_id="worker-2", source="subagent",
                              parent_session_id="worker-1")
            db.append_message("worker-2", role="assistant", content="part two")
        finally:
            db.close()

        body = self.client.get("/api/sessions/main-1/lineage").json()
        assert len(body["children"]) == 1
        worker = body["children"][0]
        assert worker["id"] == "worker-1"
        assert worker["session_id"] == "worker-2"
        assert worker["chain"] == ["worker-1", "worker-2"]
        # The continuation is absorbed by the node, not listed again.
        assert worker["child_count"] == 0

    def test_active_worker_is_stamped_active(self):
        db = self._db()
        try:
            db.create_session(session_id="main-1", source="cli")
            db.create_session(session_id="worker-live", source="subagent",
                              parent_session_id="main-1")
            db.create_session(session_id="worker-old", source="subagent",
                              parent_session_id="main-1")
            db.end_session("worker-old", end_reason="done")
        finally:
            db.close()

        body = self.client.get("/api/sessions/main-1/lineage").json()
        stamps = {node["id"]: node["is_active"] for node in body["children"]}
        assert stamps == {"worker-live": True, "worker-old": False}

    def test_unknown_session_is_a_404(self):
        assert self.client.get("/api/sessions/does-not-exist/lineage").status_code == 404

    def test_session_id_prefix_resolves_like_the_messages_endpoint(self):
        db = self._db()
        try:
            db.create_session(session_id="main-1", source="cli")
            db.create_session(session_id="worker-1", source="subagent",
                              parent_session_id="main-1")
        finally:
            db.close()

        body = self.client.get("/api/sessions/main/lineage").json()
        assert body["session_id"] == "main-1"
        assert [node["id"] for node in body["children"]] == ["worker-1"]

    def test_named_profile_reads_its_own_store(self):
        profile_home = self.home / "profiles" / "ops"
        profile_home.mkdir(parents=True)
        db = self._db(profile_home)
        try:
            db.create_session(session_id="ops-main", source="cli")
            db.create_session(session_id="ops-worker", source="subagent",
                              parent_session_id="ops-main")
        finally:
            db.close()
        db = self._db()
        try:
            db.create_session(session_id="main-1", source="cli")
            db.create_session(session_id="main-worker", source="subagent",
                              parent_session_id="main-1")
        finally:
            db.close()

        body = self.client.get(
            "/api/sessions/ops-main/lineage", params={"profile": "ops"}).json()
        assert [node["id"] for node in body["children"]] == ["ops-worker"]
        assert body["profile"] == "ops"

        # The default store's tree is untouched by the profile's rows.
        default_body = self.client.get("/api/sessions/main-1/lineage").json()
        assert [node["id"] for node in default_body["children"]] == ["main-worker"]

    def test_a_runaway_graph_truncates_instead_of_walking_forever(self, monkeypatch):
        from hermes_cli.web_routers import lineage as lineage_router

        db = self._db()
        try:
            db.create_session(session_id="main-1", source="cli")
            for i in range(5):
                db.create_session(
                    session_id=f"w-{i}", source="subagent", parent_session_id="main-1")
        finally:
            db.close()

        monkeypatch.setattr(lineage_router, "_MAX_NODES", 2)
        body = self.client.get("/api/sessions/main-1/lineage").json()
        assert len(body["children"]) == 2
        assert body["truncated"] is True

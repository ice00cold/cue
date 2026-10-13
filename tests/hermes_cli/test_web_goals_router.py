"""``GET /api/goal`` + ``POST /api/goal`` — the goal card's surface (PLAN.md
Phase 6 M4 / R4).

The card controls the goal the MAIN THREAD runs: state lives in the profile's
state.db under the goal meta key of the live compression tip, actions ride the
SHARED dispatcher (``goal_command.dispatch_goal_command``) — no surface
specific parser — and a resumed goal's continuation turn is scheduled into the
one main thread through the M4 send seam. Pinned here: the snapshot shape,
pause/resume/clear semantics (wait barriers clear, budget resets), the
continuation delivery report, goal-setting refusal (composer-owned), the
no-thread and no-goal answers, and the session-token middleware.
"""

import json

import pytest


class _GoalEndpoints:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_constants import get_hermes_home
        from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app

        self.home = get_hermes_home()
        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", self.home / "state.db")
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

        self.deliveries = []
        from hermes_cli.web_routers import main_thread as main_thread_router

        # Patch where production reads: goals.py late-imports the delivery seam
        # from main_thread inside the handler (root AGENTS patching rule).
        monkeypatch.setattr(
            main_thread_router, "deliver_main_thread_prompt",
            lambda profile, prompt, *, label: self.deliveries.append(
                {"profile": profile, "prompt": prompt, "label": label}))

    def _seed_thread(self, session_id="sess-main"):
        from hermes_state import SessionDB

        db = SessionDB(self.home / "state.db")
        try:
            db.create_session(session_id=session_id, source="telegram")
            db.save_gateway_routing_entry(
                "agent:main:main-thread", json.dumps({"session_id": session_id}))
        finally:
            db.close()

    def _set_goal(self, goal="ship the M4 cards", *, turns=12):
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id="sess-main", default_max_turns=turns)
        mgr.set(goal)
        return mgr

    def _stored_goal(self, session_id="sess-main"):
        from hermes_cli.goals import GoalManager

        return GoalManager(session_id=session_id).state


class TestGoalSnapshot(_GoalEndpoints):
    def test_the_card_reads_the_main_threads_goal(self):
        self._seed_thread()
        self._set_goal()
        resp = self.client.get("/api/goal")
        assert resp.status_code == 200
        body = resp.json()
        assert body["exists"] is True
        assert body["session_id"] == "sess-main"
        assert "ship the M4 cards" in body["status_line"]
        assert body["state"]["goal"] == "ship the M4 cards"
        assert body["state"]["status"] == "active"
        assert body["state"]["max_turns"] == 12

    def test_no_goal_is_the_honest_answer_not_an_error(self):
        self._seed_thread()
        resp = self.client.get("/api/goal")
        assert resp.status_code == 200
        body = resp.json()
        assert body["exists"] is False
        assert body["state"] is None
        assert "No active goal" in body["status_line"]

    def test_no_thread_yet_reports_no_goal_without_inventing_one(self):
        resp = self.client.get("/api/goal")
        assert resp.status_code == 200
        assert resp.json()["exists"] is False
        assert resp.json()["session_id"] is None


class TestGoalActions(_GoalEndpoints):
    def test_pause_parks_the_goal(self):
        self._seed_thread()
        self._set_goal()
        resp = self.client.post("/api/goal", json={"action": "pause"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "paused" in body["output"]
        assert body["state"]["status"] == "paused"
        assert body["delivered"] == "none"

    def test_resume_resets_the_budget_and_queues_the_continuation(self):
        self._seed_thread()
        mgr = self._set_goal()
        mgr.pause()
        resp = self.client.post("/api/goal", json={"action": "resume"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["state"]["status"] == "active"
        assert body["state"]["turns_used"] == 0
        assert body["delivered"] == "queued"
        assert len(self.deliveries) == 1
        assert "ship the M4 cards" in self.deliveries[0]["prompt"]

    def test_clear_ends_the_goal(self):
        self._seed_thread()
        self._set_goal()
        resp = self.client.post("/api/goal", json={"action": "clear"})
        assert resp.status_code == 200
        assert resp.json()["exists"] is False
        assert self._stored_goal() is None or self._stored_goal().status == "cleared"

    def test_setting_goal_text_is_refused_the_composer_owns_it(self):
        self._seed_thread()
        resp = self.client.post("/api/goal", json={"action": "ship it"})
        assert resp.status_code == 400
        assert "chat composer" in resp.json()["detail"]
        # Nothing was mutated by the refused action.
        assert self._stored_goal() is None

    def test_gate_management_is_not_reachable_from_the_card(self):
        self._seed_thread()
        self._set_goal()
        resp = self.client.post("/api/goal", json={"action": "gate"})
        assert resp.status_code == 400

    def test_resume_without_a_goal_is_the_dispatcher_answer(self):
        self._seed_thread()
        resp = self.client.post("/api/goal", json={"action": "resume"})
        assert resp.status_code == 200
        assert "No goal to resume" in resp.json()["output"]
        assert self.deliveries == []

    def test_actions_need_the_thread_to_exist(self):
        resp = self.client.post("/api/goal", json={"action": "pause"})
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "no_thread"



class TestAccess:
    def test_the_surface_sits_behind_the_session_token_middleware(self, monkeypatch, _isolate_hermes_home):
        import hermes_state
        from hermes_constants import get_hermes_home
        from starlette.testclient import TestClient
        from hermes_cli.web_server import app

        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")
        assert TestClient(app).get("/api/goal").status_code in (401, 403)

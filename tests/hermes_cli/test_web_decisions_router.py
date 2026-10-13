"""``GET /api/decisions`` + resolve/withdraw — the decision cards' surface
(PLAN.md Phase 6 M4 / R4).

The pending queue is the profile's ``decisions.yaml``, the same store every
other surface reads, and the actions ride the SHARED dispatcher semantics
(``decision_command.answer_decision`` / ``withdraw_decision``) — never a
surface-specific parser. Pinned here: the card projection (options carry the
dispatcher's numeric answer vocabulary), resolve-with-index and free-form
answers, the resume turn actually being scheduled into the main thread, the
ordering (state change persists even when delivery is impossible), the error
statuses (404 unknown, 409 already closed, 400 empty choice), profile
isolation, and the session-token middleware.
"""

import json

import pytest


class _DecisionEndpoints:
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

        # Patch where production reads: decisions.py late-imports the delivery
        # seam from main_thread inside the handler, so the defining module is
        # the seam (root AGENTS patching rule).
        monkeypatch.setattr(
            main_thread_router, "deliver_main_thread_prompt",
            lambda profile, prompt, *, label: self.deliveries.append(
                {"profile": profile, "prompt": prompt, "label": label}))

    def _store(self, home=None):
        from tools.decisions_store import DecisionsStore

        return DecisionsStore(home or self.home)

    def _seed_thread(self, session_id="sess-main"):
        """The profile's main thread: session row + routing entry, so resume
        deliveries have somewhere to land (cards schedule into it)."""
        from hermes_state import SessionDB

        db = SessionDB(self.home / "state.db")
        try:
            db.create_session(session_id=session_id, source="telegram")
            db.save_gateway_routing_entry(
                "agent:main:main-thread", json.dumps({"session_id": session_id}))
        finally:
            db.close()

    def _seed_decision(self, home=None, **kwargs):
        store = self._store(home)
        return store.add_decision(**kwargs or {
            "question": "Which database for the job queue?",
            "context": "Backs the cron jobs refactor.",
            "options": [{"label": "Postgres", "impact": "known ops"},
                        {"label": "SQLite", "impact": "zero ops"}],
            "recommendation": "Postgres", "impact": "blocks the refactor"})


class TestDecisionListing(_DecisionEndpoints):
    def test_pending_queue_renders_as_cards_with_option_indexes(self):
        self._seed_decision()
        resp = self.client.get("/api/decisions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pending_count"] == 1
        card = body["pending"][0]
        assert card["id"] == "d1"
        assert card["question"] == "Which database for the job queue?"
        assert card["recommendation"] == "Postgres"
        assert card["options"] == [
            {"index": 1, "label": "Postgres", "impact": "known ops"},
            {"index": 2, "label": "SQLite", "impact": "zero ops"},
        ]
        assert card["status"] == "pending"

    def test_empty_queue_is_an_honest_empty_list(self):
        resp = self.client.get("/api/decisions")
        assert resp.status_code == 200
        assert resp.json() == {"profile": "default", "pending": [], "pending_count": 0}

    def test_resolved_decisions_leave_the_pending_view(self):
        self._seed_decision()
        resp = self.client.post("/api/decisions/d1/resolve", json={"choice": "1"})
        assert resp.status_code == 200
        assert self.client.get("/api/decisions").json()["pending_count"] == 0

    def test_named_profile_reads_its_own_queue(self):
        profile_home = self.home / "profiles" / "ops"
        profile_home.mkdir(parents=True)
        (profile_home / ".env").write_text("")  # identity marker: a live profile dir
        self._seed_decision(home=profile_home,
                            question="ops fork?", options=[{"label": "a"}, {"label": "b"}])
        assert self.client.get("/api/decisions").json()["pending_count"] == 0
        body = self.client.get("/api/decisions", params={"profile": "ops"}).json()
        assert body["pending_count"] == 1
        assert body["pending"][0]["question"] == "ops fork?"


class TestDecisionResolve(_DecisionEndpoints):
    def test_option_index_resolves_to_the_option_label_and_queues_the_resume(self):
        self._seed_thread()
        self._seed_decision()
        resp = self.client.post("/api/decisions/d1/resolve",
                                json={"choice": "2", "rationale": "cheaper to run"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["delivered"] == "queued"
        assert "resolved → SQLite" in body["output"]
        record = self._store().get("d1")
        assert record["status"] == "resolved"
        assert record["resolution"] == "SQLite"
        assert record["rationale"] == "cheaper to run"

        assert len(self.deliveries) == 1
        delivery = self.deliveries[0]
        assert "[Decision d1 resolved by the user]" in delivery["prompt"]
        assert "Answer: SQLite" in delivery["prompt"]
        assert "Rationale: cheaper to run" in delivery["prompt"]

    def test_free_text_answer_stays_verbatim(self):
        self._seed_decision()
        resp = self.client.post("/api/decisions/d1/resolve",
                                json={"choice": "do both in stages"})
        assert resp.status_code == 200
        assert self._store().get("d1")["resolution"] == "do both in stages"

    def test_unknown_id_is_404(self):
        resp = self.client.post("/api/decisions/d9/resolve", json={"choice": "1"})
        assert resp.status_code == 404
        assert "No decision with id 'd9'" in resp.json()["detail"]

    def test_resolving_twice_is_409(self):
        self._seed_decision()
        self.client.post("/api/decisions/d1/resolve", json={"choice": "1"})
        resp = self.client.post("/api/decisions/d1/resolve", json={"choice": "2"})
        assert resp.status_code == 409
        assert "already resolved" in resp.json()["detail"]

    def test_empty_choice_is_400(self):
        self._seed_decision()
        resp = self.client.post("/api/decisions/d1/resolve", json={"choice": "  "})
        assert resp.status_code == 400
        assert "Usage" in resp.json()["detail"]

    def test_the_state_change_persists_even_when_the_resume_cannot_land(self):
        # No main thread yet (a decisions.yaml can outlive the store): the
        # record still resolves, the response reports the undelivered resume
        # with the honest reason instead of refusing the answer.
        self._seed_decision()
        resp = self.client.post("/api/decisions/d1/resolve", json={"choice": "1"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["delivered"] == "unavailable"
        assert "main thread does not exist" in body["delivery_note"]
        assert self._store().get("d1")["status"] == "resolved"
        assert self.deliveries == []


class TestDecisionWithdraw(_DecisionEndpoints):
    def test_withdraw_closes_the_fork_and_queues_the_cancellation(self):
        self._seed_thread()
        self._seed_decision()
        resp = self.client.post("/api/decisions/d1/withdraw",
                                json={"reason": "the queue moved off SQL"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["delivered"] == "queued"
        record = self._store().get("d1")
        assert record["status"] == "withdrawn"
        assert record["rationale"] == "the queue moved off SQL"
        assert "[Decision d1 withdrawn by the user]" in self.deliveries[0]["prompt"]

    def test_withdraw_unknown_id_is_404(self):
        assert self.client.post("/api/decisions/d9/withdraw", json={}).status_code == 404


class TestAccess:
    def test_the_surface_sits_behind_the_session_token_middleware(self, monkeypatch, _isolate_hermes_home):
        import hermes_state
        from hermes_constants import get_hermes_home
        from starlette.testclient import TestClient
        from hermes_cli.web_server import app

        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")
        assert TestClient(app).get("/api/decisions").status_code in (401, 403)

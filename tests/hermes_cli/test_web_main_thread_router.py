"""``GET /api/main_thread`` — the Thread surface's resolver (PLAN.md Phase 6 M1).

Cue collapses every chat into one main thread per profile; the dashboard's
Thread home must resolve the profile's LIVE session id through the same
``gateway_routing`` index the gateway/CLI key into — never a guess that breaks
on compression rotation. Pinned here: the key-collapse parity (default vs named
profile), rotation to the live tip, and the honest "no thread yet" answers
(fresh home, dead routing target) that let the SPA render an empty state
instead of a 404.
"""

import json

import pytest


class _MainThreadEndpoints:
    """Shared harness: authenticated TestClient against the isolated home's store.

    The isolated HERMES_HOME doubles as the profiles root (a custom root IS the
    default root, ``hermes_constants.get_default_hermes_root``), so a named
    profile created under ``<home>/profiles/<name>`` resolves through the real
    profile helpers — no patching.
    """

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

    def _seed_main_thread(self, home=None, *, key="agent:main:main-thread",
                          session_id="main-1"):
        """Create the session + routing row the gateway/CLI would have written."""
        from hermes_state import SessionDB

        db = SessionDB((home or self.home) / "state.db")
        try:
            db.create_session(session_id=session_id, source="cli", session_key=key)
            db.save_gateway_routing_entry(key, json.dumps(
                {"session_key": key, "session_id": session_id}))
            return db
        except Exception:
            db.close()
            raise


class TestGetMainThread(_MainThreadEndpoints):
    def test_fresh_home_reports_no_thread_yet(self):
        """A store with no routing row is an answer, not an error: the first
        chatting window creates the thread, and ``sessions.changed`` re-resolves."""
        body = self.client.get("/api/main_thread").json()
        assert body["exists"] is False
        assert body["session_id"] is None
        assert body["live_session_id"] is None
        assert body["session"] is None
        assert body["session_key"] == "agent:main:main-thread"

    def test_resolves_the_routing_row_and_its_summary(self):
        db = self._seed_main_thread()
        try:
            db.append_message("main-1", role="user", content="hello")
        finally:
            db.close()

        body = self.client.get("/api/main_thread").json()
        assert body["exists"] is True
        assert body["session_id"] == "main-1"
        assert body["live_session_id"] == "main-1"
        assert body["session"]["id"] == "main-1"
        assert body["session"]["message_count"] >= 1

    def test_rotation_resolves_to_the_live_tip(self):
        """A compression rotation ends the routing row's session and forks a
        child; the resolver must hand the SPA the tip where messages land (the
        messages endpoint follows the chain anyway, but the header/cards row
        must be the live one)."""
        db = self._seed_main_thread()
        try:
            db.append_message("main-1", role="user", content="before rotation")
            db.end_session("main-1", end_reason="compression")
            db.create_session("main-2", source="cli", parent_session_id="main-1")
            db.append_message("main-2", role="user", content="after rotation")
        finally:
            db.close()

        body = self.client.get("/api/main_thread").json()
        assert body["session_id"] == "main-1"
        assert body["live_session_id"] == "main-2"
        assert body["session"]["id"] == "main-2"

    def test_dead_routing_target_is_reported_as_no_thread(self):
        """A routing row pointing at a deleted session resolves as "no thread
        yet" (the CLI resolver's contract), never as a dangling id."""
        db = self._seed_main_thread(session_id="gone-soon")
        try:
            db.append_message("gone-soon", role="user", content="bye")
            db.delete_session("gone-soon", sessions_dir=None)
        finally:
            db.close()

        body = self.client.get("/api/main_thread").json()
        assert body["exists"] is False
        assert body["session_id"] is None
        assert body["live_session_id"] is None

    def test_named_profile_keys_under_its_namespace(self):
        """``?profile=ops`` reads the profile's own store under its ``agent:ops:``
        key — the namespace the gateway's key collapse derives for that profile,
        with the default profile's store untouched."""
        profile_home = self.home / "profiles" / "ops"
        profile_home.mkdir(parents=True)
        db = self._seed_main_thread(
            profile_home, key="agent:ops:main-thread", session_id="ops-main")
        try:
            db.append_message("ops-main", role="user", content="ops hello")
        finally:
            db.close()

        body = self.client.get("/api/main_thread", params={"profile": "ops"}).json()
        assert body["session_key"] == "agent:ops:main-thread"
        assert body["session_id"] == "ops-main"
        assert body["live_session_id"] == "ops-main"
        assert body["session"]["id"] == "ops-main"

        # The default home's store never learned about the profile's thread.
        default_body = self.client.get("/api/main_thread").json()
        assert default_body["exists"] is False

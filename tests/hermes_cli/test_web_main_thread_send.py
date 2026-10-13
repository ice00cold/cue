"""``POST /api/main_thread/send`` — the M4 headless send path (PLAN.md Phase 6
M4 / R4).

One turn forwarded to the gateway's api_server session-chat ingress on
loopback: the turn runs in the GATEWAY process with full main-thread
semantics, the interactive composer stays the embedded TUI. Pinned here: the
ingress URL/key resolution (config, .env, the multiplex ``/p/<profile>/``
listener and the routed profile's OWN key), the rotation-pinning headers, the
response mapping, and the honest failure statuses — 409 while the thread does
not exist, 503 when the gateway is unreachable, never a hang or a lie.
"""

import json

import pytest


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeHttpx:
    """Replaces ``httpx.AsyncClient`` for one test: captures the request,
    answers the scripted response (or raises, for transport failures)."""

    requests: list = []
    response = None
    error: Exception | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeHttpx.requests.append({"url": url, "json": json, "headers": dict(headers or {})})
        if _FakeHttpx.error is not None:
            raise _FakeHttpx.error
        return _FakeHttpx.response


class _SendEndpoints:
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
        self.app = app
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def _seed_thread(self, session_id="sess-main"):
        from hermes_state import SessionDB

        db = SessionDB(self.home / "state.db")
        try:
            db.create_session(session_id=session_id, source="telegram")
            db.save_gateway_routing_entry(
                "agent:main:main-thread", json.dumps({"session_id": session_id}))
        finally:
            db.close()

    def _install_httpx(self, monkeypatch, response=None, error=None):
        import httpx

        _FakeHttpx.requests = []
        _FakeHttpx.response = response or _FakeResponse(200, {
            "session_id": "sess-main", "message": {"role": "assistant", "content": "done"}})
        _FakeHttpx.error = error
        monkeypatch.setattr(httpx, "AsyncClient", _FakeHttpx)


class TestMainThreadSend(_SendEndpoints):
    def test_forwards_the_turn_with_rotation_pinning_and_maps_the_reply(self, monkeypatch):
        self._seed_thread()
        monkeypatch.setenv("API_SERVER_KEY", "t" * 24)
        self._install_httpx(monkeypatch)

        resp = self.client.post("/api/main_thread/send", json={"message": "run the sweep"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["delivered"] is True
        assert body["reply"] == "done"
        assert body["session_id"] == "sess-main"

        request = _FakeHttpx.requests[0]
        assert request["url"] == "http://127.0.0.1:8642/api/sessions/sess-main/chat"
        assert request["json"] == {"message": "run the sweep"}
        assert request["headers"]["Authorization"] == f"Bearer {'t' * 24}"
        # The pin api_server exists for: the conversation key survives a
        # mid-turn compression rotation, so the turn never lands on the stale id.
        assert request["headers"]["X-Hermes-Session-Key"] == "agent:main:main-thread"

    def test_no_key_sends_no_auth_headers(self, monkeypatch):
        self._seed_thread()
        monkeypatch.delenv("API_SERVER_KEY", raising=False)
        self._install_httpx(monkeypatch)

        resp = self.client.post("/api/main_thread/send", json={"message": "hi"})
        assert resp.status_code == 200
        assert _FakeHttpx.requests[0]["headers"] == {}

    def test_unreachable_gateway_is_503_with_the_remedy(self, monkeypatch):
        import httpx

        self._seed_thread()
        self._install_httpx(monkeypatch, error=httpx.ConnectError("refused"))
        resp = self.client.post("/api/main_thread/send", json={"message": "hi"})
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert detail["error"] == "gateway_unreachable"
        assert "gateway" in detail["message"]

    def test_no_thread_yet_is_409_not_a_forward(self, monkeypatch):
        self._install_httpx(monkeypatch)
        resp = self.client.post("/api/main_thread/send", json={"message": "hi"})
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "no_thread"
        assert _FakeHttpx.requests == []

    def test_empty_message_is_400(self, monkeypatch):
        self._seed_thread()
        self._install_httpx(monkeypatch)
        resp = self.client.post("/api/main_thread/send", json={"message": "   "})
        assert resp.status_code == 400
        assert _FakeHttpx.requests == []

    def test_rejected_key_maps_the_api_server_error_body(self, monkeypatch):
        self._seed_thread()
        monkeypatch.setenv("API_SERVER_KEY", "t" * 24)
        self._install_httpx(monkeypatch, _FakeResponse(401, {
            "error": {"message": "Invalid gateway API key", "type": "gateway_auth_error"}})) 
        resp = self.client.post("/api/main_thread/send", json={"message": "hi"})
        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert detail["error"] == "http_401"
        assert "Invalid gateway API key" in detail["message"]

    def test_requires_the_session_token_like_every_rest_surface(self):
        from starlette.testclient import TestClient

        self._seed_thread()
        resp = TestClient(self.app).post("/api/main_thread/send", json={"message": "hi"})
        assert resp.status_code in (401, 403)


class TestIngressResolution(_SendEndpoints):
    """The URL/key ladder: config wins, .env is the fallback, and the multiplex
    listener swap (default binds, routed profile authenticates with its OWN
    key) must match what api_server actually checks."""

    def _ingress(self, profile=None):
        from hermes_cli.web_routers.main_thread import _api_server_ingress

        return _api_server_ingress(profile)

    def test_config_port_and_key_win(self, monkeypatch):
        (self.home / "config.yaml").write_text(
            "platforms:\n  api_server:\n    port: 9001\n    key: cfg-key-0123456789\n")
        monkeypatch.delenv("API_SERVER_PORT", raising=False)
        base, key = self._ingress()
        assert base == "http://127.0.0.1:9001"
        assert key == "cfg-key-0123456789"

    def test_env_port_and_key_are_the_fallback(self, monkeypatch):
        monkeypatch.setenv("API_SERVER_PORT", "9002")
        monkeypatch.setenv("API_SERVER_KEY", "env-key-0123456789")
        base, key = self._ingress()
        assert base == "http://127.0.0.1:9002"
        assert key == "env-key-0123456789"

    def test_multiplexed_named_profile_rides_the_default_listener_with_its_own_key(self, monkeypatch):
        import hermes_cli.gateway_multiplex_mode as mux

        profiles = self.home / "profiles"
        satellite = profiles / "satellite"
        satellite.mkdir(parents=True)
        (satellite / ".env").write_text("API_SERVER_KEY=satellite-key-0123456789\n")
        (self.home / ".env").write_text("API_SERVER_KEY=default-key-0123456789\n")
        monkeypatch.setattr(mux, "default_gateway_multiplexes", lambda: True)
        monkeypatch.delenv("API_SERVER_PORT", raising=False)

        base, key = self._ingress("satellite")
        assert base == "http://127.0.0.1:8642/p/satellite"
        # /p/<profile>/ auth checks the ROUTED profile's key, not the listener's.
        assert key == "satellite-key-0123456789"

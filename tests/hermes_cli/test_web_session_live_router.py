"""``GET /api/sessions/{id}/live`` — the read-only SSE session tail (PLAN.md
Phase 6 M3 / R3).

The live view behind the Thread home and the drill-down: while a worker (or
the main thread) runs, new transcript rows must stream to the viewer — same
display projection as the messages endpoint, anchored by a keyset row id,
following the compression tip across rotation, with activity stamps when
they move and honest terminal frames. Pinned here: the anchor contract
(``after_row_id``), mid-stream appends arriving as frames, rotation
following, deletion ending the stream, the 404 for unknown sessions, profile
isolation, projection parity with the messages endpoint, and that the route
sits behind the session-token middleware like every other REST surface.
"""

import asyncio
import json
import queue
import threading
import time
from urllib.parse import urlencode

import pytest


class _SseStream:
    """Incremental ASGI driver for one live-tail request.

    starlette's TestClient and httpx's ASGITransport both buffer the WHOLE
    response body before any of it is readable, and an endless SSE stream
    never completes — so this drives the app the way a real server does: the
    app runs as a task on its own loop/thread, body chunks surface on a
    queue as the app sends them, and ``close()`` is the client hanging up
    (the generator's next ``is_disconnected`` peek sees ``http.disconnect``
    and unwinds through its ``finally``).
    """

    def __init__(self, app, path, params=None, headers=None):
        self._app = app
        self._path = path
        self._query = urlencode(params or {})
        self._headers = headers or {}
        self._chunks: "queue.Queue[bytes]" = queue.Queue()
        self._loop = None
        self._disconnect = None
        self._done = threading.Event()
        self.status = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        async def main() -> None:
            self._loop = asyncio.get_running_loop()
            self._disconnect = asyncio.Event()
            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "GET", "scheme": "http", "path": self._path,
                "raw_path": self._path.encode(), "query_string": self._query.encode(),
                "root_path": "", "server": ("testserver", 80),
                "client": ("127.0.0.1", 1234),
                "headers": [(k.lower().encode(), v.encode())
                            for k, v in {"host": "testserver", **self._headers}.items()],
            }

            async def receive():
                # No await before the branch: a set event answers the
                # pre-cancelled is_disconnected peek synchronously; an unset
                # one parks at a checkpoint, which that same scope cancels.
                if self._disconnect.is_set():
                    return {"type": "http.disconnect"}
                await self._disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.start":
                    self.status = message["status"]
                elif message["type"] == "http.response.body":
                    if message.get("body"):
                        self._chunks.put(message["body"])
                    if not message.get("more_body", False):
                        self._chunks.put(b"")

            try:
                await self._app(scope, receive, send)
            finally:
                self._chunks.put(b"")

        try:
            asyncio.run(main())
        finally:
            self._done.set()

    def _next_chunk(self, timeout: float) -> bytes:
        try:
            return self._chunks.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(
                f"timed out after {timeout}s waiting for SSE bytes (status={self.status})") from None

    def _next_frame(self, timeout: float, *, comments=False):
        """``(event, data)`` of the next data frame (or ``(": …", None)`` for a
        keepalive when *comments*). ``("eof", None)`` once the body ended."""
        buffer = b""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("timed out waiting for a complete SSE frame")
            chunk = self._next_chunk(remaining)
            if not chunk:
                return ("eof", None)
            buffer += chunk
            while b"\n\n" in buffer:
                raw, buffer = buffer.split(b"\n\n", 1)
                event, data_lines, saw_data = "message", [], False
                for line in raw.decode("utf-8", "replace").split("\n"):
                    if line.startswith(":"):
                        event = line
                    elif line.startswith("event:"):
                        event = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        saw_data = True
                        data_lines.append(line[len("data:"):].strip())
                if saw_data:
                    return event, json.loads("\n".join(data_lines))
                if comments and event.startswith(":"):
                    return event, None

    def next_frame(self, timeout: float = 10.0):
        return self._next_frame(timeout)[1]

    def expect_end(self, timeout: float = 5.0) -> None:
        """The server ended the body after a terminal frame."""
        assert self._next_frame(timeout)[0] == "eof"

    def close(self) -> None:
        if self._disconnect is not None and not self._done.is_set():
            try:
                self._loop.call_soon_threadsafe(self._disconnect.set)
            except RuntimeError:
                pass  # loop already finished racing the app's own teardown
        self._thread.join(timeout=5.0)


class _LiveEndpoints:
    """Shared harness: authenticated app against the isolated home's store,
    with the tail's poll cadence tightened so tests stream promptly."""

    POLL_S = 0.05

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, _isolate_hermes_home):
        import hermes_state
        from hermes_constants import get_hermes_home
        from hermes_cli.web_routers import session_live

        self.home = get_hermes_home()
        monkeypatch.setattr(
            hermes_state, "DEFAULT_DB_PATH", self.home / "state.db"
        )
        monkeypatch.setattr(session_live, "_POLL_INTERVAL_S", self.POLL_S)

        from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app

        self.app = app
        self.auth_headers = {_SESSION_HEADER_NAME: _SESSION_TOKEN}

    def _db(self, home=None):
        from hermes_state import SessionDB

        return SessionDB((home or self.home) / "state.db")

    def _seed(self, session_id, *, source="cli", parent=None, home=None):
        db = self._db(home)
        try:
            db.create_session(session_id=session_id, source=source,
                              parent_session_id=parent)
            return db
        except Exception:
            db.close()
            raise

    def _stream(self, path, params=None):
        return _SseStream(self.app, path, params=params, headers=self.auth_headers)

    def _row_ids(self, session_id):
        """Row ids of every stored message, in order (the keyset address space)."""
        db = self._db()
        try:
            return [m["id"] for m in db.get_messages(session_id, include_inactive=True)]
        finally:
            db.close()


class TestSessionLiveTail(_LiveEndpoints):
    def test_hello_carries_the_anchor_contract_and_only_new_rows_stream(self):
        db = self._seed("main-1")
        try:
            db.append_message("main-1", role="user", content="one")
            db.append_message("main-1", role="assistant", content="two")
        finally:
            db.close()
        first_id = self._row_ids("main-1")[0]

        stream = self._stream("/api/sessions/main-1/live", {"after_row_id": first_id})
        try:
            event, hello = stream._next_frame(10)
            assert event == "hello"
            assert hello["session_id"] == "main-1"
            assert hello["live_session_id"] == "main-1"
            assert hello["after_row_id"] == first_id
            assert hello["activity"]["message_count"] == 2

            event, messages = stream._next_frame(10)
            assert event == "messages"
            assert [m["content"] for m in messages["messages"]] == ["two"]
            assert messages["last_row_id"] == self._row_ids("main-1")[-1]
        finally:
            stream.close()

    def test_rows_stream_as_they_land_with_activity_stamps(self):
        db = self._seed("main-1")
        try:
            db.append_message("main-1", role="user", content="seed")
        finally:
            db.close()
        anchor = self._row_ids("main-1")[-1]

        stream = self._stream("/api/sessions/main-1/live", {"after_row_id": anchor})
        try:
            assert stream._next_frame(10)[0] == "hello"

            # The writer is another process in production (gateway, worker);
            # here a second connection on the test thread plays it.
            db = self._db()
            try:
                db.append_message("main-1", role="assistant", content="streamed row")
            finally:
                db.close()

            event, messages = stream._next_frame(10)
            assert event == "messages"
            assert [m["content"] for m in messages["messages"]] == ["streamed row"]
            assert messages["last_row_id"] > anchor

            event, activity = stream._next_frame(10)
            assert event == "activity"
            assert activity["message_count"] == 2
            assert activity["is_active"] is True

            # The anchor advanced: the streamed page must not re-emit on
            # later polls (a tail that repeats its last page forever is the
            # one failure mode this surface must never have).
            quiet_until = time.monotonic() + 0.6  # ~12 polls at the test cadence
            while time.monotonic() < quiet_until:
                try:
                    chunk = stream._chunks.get(timeout=max(0.05, quiet_until - time.monotonic()))
                except queue.Empty:
                    continue
                assert b"event: messages" not in chunk, f"re-emitted a page: {chunk[:120]!r}"
        finally:
            stream.close()

    def test_rotation_follows_the_compression_tip(self):
        db = self._seed("main-1")
        try:
            db.append_message("main-1", role="user", content="before")
        finally:
            db.close()
        anchor = self._row_ids("main-1")[-1]

        stream = self._stream("/api/sessions/main-1/live", {"after_row_id": anchor})
        try:
            assert stream._next_frame(10)[0] == "hello"
            db = self._db()
            try:
                db.end_session("main-1", end_reason="compression")
            finally:
                db.close()
            db = self._seed("main-2", parent="main-1")
            try:
                db.append_message("main-2", role="user", content="after rotation")
            finally:
                db.close()

            event, rotated = stream._next_frame(10)
            assert event == "rotated"
            assert rotated["previous"] == "main-1"
            assert rotated["session_id"] == "main-2"

            event, messages = stream._next_frame(10)
            assert event == "messages"
            assert messages["session_id"] == "main-2"
            assert [m["content"] for m in messages["messages"]] == ["after rotation"]
        finally:
            stream.close()

    def test_deleting_the_session_is_a_terminal_frame_that_ends_the_stream(self):
        db = self._seed("main-1")
        try:
            db.append_message("main-1", role="user", content="seed")
        finally:
            db.close()
        anchor = self._row_ids("main-1")[-1]

        stream = self._stream("/api/sessions/main-1/live", {"after_row_id": anchor})
        try:
            assert stream._next_frame(10)[0] == "hello"
            db = self._db()
            try:
                db.delete_session("main-1")
            finally:
                db.close()

            event, deleted = stream._next_frame(10)
            assert event == "deleted"
            assert deleted["session_id"] == "main-1"
            stream.expect_end()
        finally:
            stream.close()

    def test_unknown_session_is_a_plain_404(self):
        stream = self._stream("/api/sessions/does-not-exist/live")
        try:
            deadline = time.monotonic() + 10
            while stream.status is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert stream.status == 404
            stream.expect_end()
        finally:
            stream.close()

    def test_session_id_prefix_resolves_like_the_messages_endpoint(self):
        db = self._seed("main-1")
        try:
            db.append_message("main-1", role="user", content="one")
        finally:
            db.close()

        stream = self._stream("/api/sessions/main/live")
        try:
            event, hello = stream._next_frame(10)
            assert event == "hello"
            assert hello["session_id"] == "main-1"
        finally:
            stream.close()

    def test_named_profile_reads_its_own_store(self):
        profile_home = self.home / "profiles" / "ops"
        profile_home.mkdir(parents=True)
        db = self._db(profile_home)
        try:
            db.create_session(session_id="ops-main", source="cli")
            db.append_message("ops-main", role="user", content="ops row")
        finally:
            db.close()
        db = self._seed("main-1")
        db.close()

        stream = self._stream("/api/sessions/ops-main/live", {"profile": "ops"})
        try:
            event, hello = stream._next_frame(10)
            assert event == "hello"
            assert hello["profile"] == "ops"
            assert hello["session_id"] == "ops-main"
            event, messages = stream._next_frame(10)
            assert [m["content"] for m in messages["messages"]] == ["ops row"]
        finally:
            stream.close()
        # The default store's session is not addressable through the profile's tail.
        stream = self._stream("/api/sessions/main-1/live", {"profile": "ops"})
        try:
            deadline = time.monotonic() + 10
            while stream.status is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert stream.status == 404
        finally:
            stream.close()

    def test_streamed_rows_match_the_messages_endpoint_projection(self):
        db = self._seed("main-1")
        try:
            db.append_message("main-1", role="user", content="question")
            db.append_message("main-1", role="assistant", content="answer")
        finally:
            db.close()

        stream = self._stream("/api/sessions/main-1/live", {"after_row_id": 0})
        try:
            event, messages = stream._next_frame(10)
            if event == "hello":
                event, messages = stream._next_frame(10)
            assert event == "messages"
            expected = self._db().get_messages("main-1")
            assert [m["id"] for m in messages["messages"]] == [m["id"] for m in expected]
            assert [m["role"] for m in messages["messages"]] == [m["role"] for m in expected]
            assert [m["content"] for m in messages["messages"]] == [
                m["content"] for m in expected]
        finally:
            stream.close()

    def test_idle_stretch_heartbeats_comment_frames(self, monkeypatch):
        from hermes_cli.web_routers import session_live

        monkeypatch.setattr(session_live, "_HEARTBEAT_INTERVAL_S", 0.2)
        db = self._seed("main-1")
        try:
            db.append_message("main-1", role="user", content="seed")
        finally:
            db.close()

        stream = self._stream("/api/sessions/main-1/live", {"after_row_id": 0})
        try:
            deadline = time.monotonic() + 10
            while True:
                event, _ = stream._next_frame(max(1.0, deadline - time.monotonic()),
                                              comments=True)
                if event.startswith(":"):
                    break  # the keepalive this test pins
                assert event != "eof", "stream ended before any keepalive comment"
                assert time.monotonic() < deadline, "no keepalive comment within the window"
        finally:
            stream.close()

    def test_the_tail_sits_behind_the_session_token_middleware(self):
        stream = _SseStream(self.app, "/api/sessions/main-1/live")
        try:
            deadline = time.monotonic() + 10
            while stream.status is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert stream.status == 401
        finally:
            stream.close()

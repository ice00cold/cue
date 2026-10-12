"""Tests for gateway/mirror.py — delivery mirroring into the one main thread."""

import importlib
import json
from unittest.mock import patch, MagicMock

import gateway.mirror as mirror_mod
from gateway.mirror import (
    mirror_to_session,
    _main_thread_session_id,
)

MAIN_KEY = "agent:main:main-thread"


def _setup_sessions(tmp_path, sessions_data):
    """Helper to write a fake sessions.json and patch module-level paths."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    index_file = sessions_dir / "sessions.json"
    index_file.write_text(json.dumps(sessions_data), encoding="utf-8")
    return sessions_dir, index_file


class TestMainThreadSessionId:
    def test_resolves_the_main_thread_entry_from_the_routing_index(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            MAIN_KEY: {"session_id": "sess_main", "updated_at": "2026-01-01T00:00:00"},
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            assert _main_thread_session_id() == "sess_main"

    def test_no_main_entry_and_no_durable_row_means_none(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "agent:main:telegram:dm:123": {"session_id": "sess_legacy"},  # pre-fork row
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file), \
             patch("hermes_state_registry.acquire", side_effect=OSError("no store")):
            assert _main_thread_session_id() is None

    def test_durable_row_answers_when_the_index_is_empty(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {})
        db = MagicMock()
        db.find_latest_gateway_session_for_main_key.return_value = {"id": "sess_durable"}

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file), \
             patch("hermes_state_registry.acquire", return_value=db), \
             patch("hermes_state_registry.release_or_close"):
            assert _main_thread_session_id() == "sess_durable"


class TestMirrorToSession:
    def test_mirror_appends_to_the_resolved_main_thread(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            MAIN_KEY: {"session_id": "sess_main", "updated_at": "2026-01-01T00:00:00"},
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file), \
             patch("gateway.mirror._append_to_sqlite") as mock_sqlite:
            result = mirror_to_session("Hello group!", source_label="cli")

        assert result is True
        mock_sqlite.assert_called_once()
        assert mock_sqlite.call_args[0][0] == "sess_main"

    def test_no_matching_session(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {})

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file), \
             patch("hermes_state_registry.acquire", side_effect=OSError("no store")):
            result = mirror_to_session("Hello!")

        assert result is False

    def test_failed_sqlite_write_reports_false(self, tmp_path):
        """A mirror whose transcript write raises must not report success (#10130)."""
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            MAIN_KEY: {"session_id": "sess_main", "updated_at": "2026-01-01T00:00:00"},
        })
        broken_db = MagicMock()
        broken_db.append_message.side_effect = OSError("disk full")

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file), \
             patch("hermes_state_registry.acquire", return_value=broken_db), \
             patch("hermes_state_registry.release_or_close"):
            result = mirror_to_session("Hello!")

        assert result is False
        assert broken_db.append_message.called


class TestAppendToSqlite:
    def test_connection_is_released_after_use(self, tmp_path):
        """Verify _append_to_sqlite returns the shared SessionDB reference."""
        from gateway.mirror import _append_to_sqlite
        mock_db = MagicMock()
        released = []

        with patch("hermes_state_registry.acquire", return_value=mock_db), \
             patch(
                 "hermes_state_registry.release_or_close",
                 side_effect=lambda db: released.append(db),
             ):
            _append_to_sqlite("sess_1", {"role": "assistant", "content": "hello"})

        mock_db.append_message.assert_called_once()
        assert released == [mock_db], (
            "the shared handle must be released exactly once after use"
        )


class TestSessionsIndexProfileScoping:
    """#112844: the fallback index must follow the active profile, not the launch one."""

    @staticmethod
    def _write_index(home, session_id):
        d = home / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        (d / "sessions.json").write_text(json.dumps({
            MAIN_KEY: {"session_id": session_id, "updated_at": "2026-01-01T00:00:00"},
        }), encoding="utf-8")

    def test_fallback_follows_active_profile_home(self, tmp_path, monkeypatch):
        """A profile switched in after import must be read, not the launch profile's index.

        The module captures ``sessions.json`` under the home that was live at import. Under the
        multiplexed gateway one process serves every profile, so a lookup for another profile
        must not resolve against the launch profile's index and return its session id.
        """
        launch_home, active_home = tmp_path / "launch", tmp_path / "active"
        self._write_index(launch_home, "sess_launch")
        self._write_index(active_home, "sess_active")

        # Re-import the module with the launch home live: this is the import-time capture.
        monkeypatch.setenv("HERMES_HOME", str(launch_home))
        importlib.reload(mirror_mod)
        try:
            # A request for a different profile is now served by the same process.
            monkeypatch.setenv("HERMES_HOME", str(active_home))
            assert mirror_mod._main_thread_session_id() == "sess_active"
        finally:
            monkeypatch.undo()
            importlib.reload(mirror_mod)

    def test_patched_constant_still_wins(self, tmp_path, monkeypatch):
        """Tests that patch ``_SESSIONS_INDEX`` keep overriding the live home."""
        patched = tmp_path / "patched"
        self._write_index(patched, "sess_patched")

        monkeypatch.setattr(mirror_mod, "_SESSIONS_INDEX", patched / "sessions" / "sessions.json")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "elsewhere"))

        assert mirror_mod._main_thread_session_id() == "sess_patched"

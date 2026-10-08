"""CLI ``/new`` end-to-end contract (Cue): rotation, never a destructive modal.

Drives ``HermesCLI.process_command("/new ...")`` against a minimal stand-in and verifies the
topic-rotation path fires with the title parsed from the command — including the legacy
``--yes`` skip token, which is now a no-op (rotation keeps the session; nothing destructive
remains to confirm) — and that no confirmation modal is ever consulted.
"""

from __future__ import annotations

from types import SimpleNamespace

from cli import HermesCLI


def _make_cli_stub():
    """Build a minimal HermesCLI-shaped object that can run ``process_command`` for ``/new``
    without spinning up a real TUI."""
    rotate_calls = []

    self_ = SimpleNamespace(
        _app=None,
        _prompt_text_input_modal=lambda **_kw: (_ for _ in ()).throw(
            AssertionError("/new must never consult a modal — rotation is not destructive")
        ),
        _rotate_topic=lambda title=None: rotate_calls.append({"title": title}),
        console=SimpleNamespace(clear=lambda: None),
        compact=False,
        model="stub-model",
        session_id="stub-session",
        enabled_toolsets=[],
        _pending_title=None,
        _session_db=None,
    )
    self_._split_destructive_skip = HermesCLI._split_destructive_skip
    self_._confirm_destructive_slash = HermesCLI._confirm_destructive_slash.__get__(
        self_, type(self_)
    )
    self_._slash_handler = HermesCLI._slash_handler
    self_._slash_metrics_surface = HermesCLI._slash_metrics_surface
    self_._cmd_new = HermesCLI._cmd_new.__get__(self_, type(self_))
    self_.process_command = HermesCLI.process_command.__get__(self_, type(self_))
    return self_, rotate_calls


def test_new_invokes_rotation_without_modal():
    self_, rotate_calls = _make_cli_stub()
    self_.process_command("/new")
    assert rotate_calls == [{"title": None}], "rotation must run with no title"


def test_new_title_reaches_the_topic_artifact():
    self_, rotate_calls = _make_cli_stub()
    self_.process_command("/new My Session")
    assert rotate_calls == [{"title": "My Session"}]


def test_legacy_skip_token_is_a_no_op():
    """``--yes``/``--now`` tokens from the reset era are dropped, not treated as a title."""
    self_, rotate_calls = _make_cli_stub()
    self_.process_command("/new --yes My Session")
    assert rotate_calls == [{"title": "My Session"}]

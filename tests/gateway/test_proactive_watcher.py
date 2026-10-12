"""The proactive watcher: due ticks inject selected goal work into the IDLE main thread
(deterministic selection, muted reply, refund on failure) and flushes batched reports
(quiet hours hold, one message, outbox survives a failed send)."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.response_filters import INTERNAL_NOTIFICATION_DISPLAY_KIND, display_kind_for_event
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_time import now as hermes_now
from tools.goals_store import GoalsStore
from tools.proactive_store import ProactiveStore


class _WireAdapter(BasePlatformAdapter):
    """Real adapter lifecycle with an in-memory wire transport, no bot/model."""

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="wire-1")


def _write_proactive_config(home: Path, **settings):
    """Write config.yaml § proactive through the real file the loader reads."""
    lines = ["proactive:"] + [f"  {key}: {json.dumps(value)}" for key, value in settings.items()]
    (home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _clock(moment) -> str:
    return moment.strftime("%H:%M")


def _window_containing(moment) -> str:
    """A local window that provably contains *moment* for the next ~2 minutes."""
    return f"{_clock(moment)}-{_clock(moment + timedelta(minutes=2))}"


def _window_excluding(moment) -> str:
    """A local window that provably excludes *moment* (strictly in the future)."""
    return f"{_clock(moment + timedelta(minutes=30))}-{_clock(moment + timedelta(minutes=90))}"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A profile home with one actionable goal and quiet hours disabled by default."""
    home = tmp_path
    monkeypatch.setenv("HERMES_HOME", str(home))
    GoalsStore(home).add_goal(
        statement="Ship Cue phase 4", why="proactive loop",
        next_action="Write the watcher tests")
    _write_proactive_config(home, enabled=True, quiet_hours="")
    yield home


@pytest.fixture
def runner(env):
    """A GatewayRunner stand-in: real mixin methods, stubbed routing surface."""
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="42", chat_type="dm")
    key = "agent:main:main-thread"
    adapter = _WireAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    adapter.set_message_handler(lambda event: asyncio.sleep(0, result=None))

    runner = object.__new__(GatewayRunner)
    runner.session_store = SimpleNamespace(lookup_by_session_key=lambda _key: SimpleNamespace(origin=object()))
    runner._restored_source = lambda _entry: source
    runner._delivery_adapter_for = lambda _source: adapter
    runner._build_process_event_source = lambda _evt: source  # home-channel fallback seam
    runner._adapters_for_profile = lambda _profile: {Platform.TELEGRAM: adapter}
    runner._is_session_running = lambda _key: False
    runner._queue_depth = lambda _key, adapter=None: 0
    runner._run_in_executor_with_context = asyncio.to_thread
    runner.config = None
    runner._thread_metadata_for_source = lambda _source: {}
    yield SimpleNamespace(runner=runner, adapter=adapter, source=source, key=key, home=env)


def _drain(adapter):
    return asyncio.gather(*adapter._background_tasks)


def _capture_events(adapter, received):
    async def handler(event):
        received.append(event)
        return None

    adapter.set_message_handler(handler)


class TestTick:
    @pytest.mark.asyncio
    async def test_due_tick_injects_selected_goal_into_idle_main_thread(self, runner):
        received = []
        _capture_events(runner.adapter, received)
        await runner.runner._proactive_pass(None)
        await _drain(runner.adapter)

        (event,) = received
        assert "[g1] Ship Cue phase 4" in event.text and "next: Write the watcher tests" in event.text
        assert not event.internal  # authorization and the emergency stop still apply
        assert event._proactive_tick and event._notification_reply_muted  # machinery + muted reply
        assert event.metadata["gateway_session_key"] == runner.key
        assert display_kind_for_event(event) == INTERNAL_NOTIFICATION_DISPLAY_KIND

        store = ProactiveStore(runner.home)
        assert not store.should_tick(30)  # anchor consumed at-most-once
        assert "g1" in store.load()["picked"]

    @pytest.mark.asyncio
    async def test_busy_main_thread_leaves_the_tick_due(self, runner):
        received = []
        _capture_events(runner.adapter, received)
        runner.runner._is_session_running = lambda _key: True
        await runner.runner._proactive_pass(None)
        await _drain(runner.adapter)
        assert received == []
        assert ProactiveStore(runner.home).should_tick(30)  # between-conversations only

    @pytest.mark.asyncio
    async def test_no_actionable_goals_no_tick(self, runner):
        GoalsStore(runner.home).update_goal("g1", {"next_action": "", "status": "paused"})
        received = []
        _capture_events(runner.adapter, received)
        await runner.runner._proactive_pass(None)
        await _drain(runner.adapter)
        assert received == []
        assert ProactiveStore(runner.home).should_tick(30)

    @pytest.mark.asyncio
    async def test_injection_raise_is_refunded_not_raised(self, runner, monkeypatch):
        async def boom(event):
            raise RuntimeError("adapter lost the wire")

        monkeypatch.setattr(runner.adapter, "handle_message", boom)
        await runner.runner._proactive_tick(None)  # the watcher must not blow up on this
        assert ProactiveStore(runner.home).should_tick(30)

    @pytest.mark.asyncio
    async def test_dropped_tick_is_refunded_and_stays_due(self, runner):
        # No message handler installed: handle_message drops the event at the gate, and the
        # tick must not be consumed by a turn that never started.
        runner.adapter._message_handler = None
        await runner.runner._proactive_tick(None)
        await _drain(runner.adapter)
        assert ProactiveStore(runner.home).should_tick(30)

    @pytest.mark.asyncio
    async def test_disabled_config_gates_the_tick(self, runner):
        _write_proactive_config(runner.home, enabled=False, quiet_hours="")
        received = []
        _capture_events(runner.adapter, received)
        await runner.runner._proactive_pass(None)
        await _drain(runner.adapter)
        assert received == []

    @pytest.mark.asyncio
    async def test_emergency_stop_holds_the_tick_due(self, runner):
        from agent import estop

        received = []
        _capture_events(runner.adapter, received)
        estop.engage(reason="maintenance")
        try:
            await runner.runner._proactive_pass(None)
            await _drain(runner.adapter)
        finally:
            estop.disengage()
        assert received == []
        assert ProactiveStore(runner.home).should_tick(30)


class TestFlush:
    @pytest.mark.asyncio
    async def test_due_outbox_flushes_as_one_report_and_clears(self, runner, monkeypatch):
        store = ProactiveStore(runner.home)
        store.enqueue_notice("decision", "decision [d1] Which DB? (/decisions show d1)")
        store.enqueue_notice("goal_report", "worked on g1: drafted the plan")
        mirrored = []
        monkeypatch.setattr("gateway.mirror.mirror_to_session", lambda *a, **kw: mirrored.append(a) or True)
        sent = []
        monkeypatch.setattr(runner.adapter, "send", _send_capture(sent))
        await runner.runner._proactive_flush(None)

        assert len(sent) == 1, "the batch is ONE message, not one per notice"
        report = sent[0][1]
        assert "decision [d1]" in report and "worked on g1" in report
        assert store.load()["outbox"] == [] and store.load()["last_report_at"]
        assert mirrored, "the sent report mirrors into the main thread"

    @pytest.mark.asyncio
    async def test_quiet_hours_hold_the_batch(self, runner, monkeypatch):
        store = ProactiveStore(runner.home)
        store.enqueue_notice("decision", "decision [d2] Quiet fork")
        _write_proactive_config(runner.home, enabled=True, quiet_hours=_window_containing(hermes_now()))
        sent = []
        monkeypatch.setattr(runner.adapter, "send", _send_capture(sent))
        await runner.runner._proactive_flush(None)
        assert sent == []
        assert [n["id"] for n in store.load()["outbox"]] == ["p1"], "held, not dropped"

    @pytest.mark.asyncio
    async def test_report_cadence_gates_the_flush(self, runner, monkeypatch):
        store = ProactiveStore(runner.home)
        store.clear_notices([], hermes_now())  # stamp a fresh report anchor
        store.enqueue_notice("decision", "decision [d5] Not yet")
        sent = []
        monkeypatch.setattr(runner.adapter, "send", _send_capture(sent))
        await runner.runner._proactive_flush(None)
        assert sent == []
        assert len(store.load()["outbox"]) == 1

    @pytest.mark.asyncio
    async def test_failed_send_keeps_the_notices_queued(self, runner, monkeypatch):
        store = ProactiveStore(runner.home)
        store.enqueue_notice("decision", "decision [d3] Retry me")

        async def send(chat_id, content, reply_to=None, metadata=None):
            return SendResult(success=False, error="flood control")

        monkeypatch.setattr(runner.adapter, "send", send)
        await runner.runner._proactive_flush(None)
        assert [n["id"] for n in store.load()["outbox"]] == ["p1"]
        assert store.load()["last_report_at"] == ""

    @pytest.mark.asyncio
    async def test_no_route_holds_the_batch_without_consuming_the_window(self, runner):
        store = ProactiveStore(runner.home)
        store.enqueue_notice("decision", "decision [d4] No window")
        runner.runner._delivery_adapter_for = lambda _source: None
        await runner.runner._proactive_flush(None)
        assert [n["id"] for n in store.load()["outbox"]] == ["p1"]
        assert store.load()["last_report_at"] == ""


def _send_capture(sent):
    async def send(chat_id, content, reply_to=None, metadata=None):
        sent.append((chat_id, content))
        return SendResult(success=True, message_id="wire-1")

    return send


class TestPostTurnSummary:
    @pytest.mark.asyncio
    async def test_muted_tick_reply_queues_as_a_batched_notice(self, runner):
        store = ProactiveStore(runner.home)
        await runner.runner._post_turn_proactive_summary(
            session_entry=None, source=runner.source, final_response="g1 advanced: tests written")
        (notice,) = store.load()["outbox"]
        assert notice["kind"] == "goal_report" and "tests written" in notice["text"]

    @pytest.mark.asyncio
    async def test_empty_reply_queues_nothing(self, runner):
        await runner.runner._post_turn_proactive_summary(
            session_entry=None, source=runner.source, final_response="  ")
        assert ProactiveStore(runner.home).load()["outbox"] == []

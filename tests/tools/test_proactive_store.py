"""ProactiveStore behaviour: tick-anchor coalescing, LRU goal selection, outbox batching
caps, quiet-hours windows, escalation tiers per a decision's impact field, and the
config→reader plumbing for the ``proactive`` section. Contracts, not snapshots."""

from datetime import datetime, time, timedelta, timezone

import pytest

from hermes_time import now as hermes_now
from tools import proactive_store
from tools.proactive_store import (
    ProactiveStore,
    compose_report,
    escalation_tier,
    in_quiet_hours,
    parse_quiet_hours,
    proactive_settings,
    select_goal_actions,
)


@pytest.fixture
def store(tmp_path):
    return ProactiveStore(tmp_path)


def _goal(goal_id, *, status="active", next_action="Do the thing", updated_at="", statement="Goal"):
    return {"id": goal_id, "status": status, "next_action": next_action,
            "updated_at": updated_at, "statement": statement}


class TestTickAnchor:
    def test_first_tick_is_due_and_records_anchor(self, store):
        now = hermes_now()
        assert store.should_tick(30, now)
        written, previous = store.record_tick(now)
        assert written == now.isoformat() and previous == ""
        assert not store.should_tick(30, now + timedelta(minutes=29))
        assert store.should_tick(30, now + timedelta(minutes=30))

    def test_missed_ticks_coalesce_on_the_last_fire_not_a_schedule(self, store):
        now = hermes_now()
        store.record_tick(now)
        # A gateway down for 3 intervals still fires exactly one tick after restart.
        later = now + timedelta(minutes=90)
        assert store.should_tick(30, later)
        store.record_tick(later)
        assert not store.should_tick(30, later + timedelta(minutes=1))

    def test_refund_restores_only_the_callers_own_anchor(self, store):
        now = hermes_now()
        written, previous = store.record_tick(now)
        store.refund_tick(written, previous)
        assert store.should_tick(30, now)

        # An anchor that moved on (another process fired in between) wins over the refund.
        written2, _previous2 = store.record_tick(now + timedelta(minutes=5))
        store.refund_tick(written, previous)  # stale claim
        assert not store.should_tick(30, now + timedelta(minutes=5))


class TestSelection:
    def test_active_goals_with_next_action_only(self):
        goals = [
            _goal("g1", status="paused"),
            _goal("g2", next_action="   "),
            _goal("g3"),
            _goal("g4", status="done"),
        ]
        assert [g["id"] for g in select_goal_actions(goals, {}, 5)] == ["g3"]

    def test_least_recently_picked_first_and_capped(self):
        goals = [_goal("g1", updated_at="2026-01-01T00:00:00+00:00"),
                 _goal("g2", updated_at="2026-01-02T00:00:00+00:00"),
                 _goal("g3", updated_at="2026-01-03T00:00:00+00:00")]
        picked = {"g2": "2026-01-01T00:00:00+00:00"}  # g2 was picked most recently → last
        assert [g["id"] for g in select_goal_actions(goals, picked, 2)] == ["g1", "g3"]

    def test_never_picked_orders_by_least_recently_updated(self):
        goals = [_goal("g1", updated_at="2026-01-03T00:00:00+00:00"),
                 _goal("g2", updated_at="2026-01-01T00:00:00+00:00")]
        assert [g["id"] for g in select_goal_actions(goals, {}, 5)] == ["g2", "g1"]

    def test_unparseable_stamps_sort_as_oldest(self):
        goals = [_goal("g1", updated_at="not-a-date"), _goal("g2", updated_at="2026-01-01T00:00:00+00:00")]
        assert [g["id"] for g in select_goal_actions(goals, {"g1": "garbage"}, 5)] == ["g1", "g2"]

    def test_pick_ledger_persists(self, store):
        now = hermes_now()
        store.record_picked(["g1", "g2"], now)
        assert store.load()["picked"] == {"g1": now.isoformat(), "g2": now.isoformat()}


class TestOutbox:
    def test_notices_flush_only_on_cadence_then_anchor_resets(self, store):
        now = hermes_now()
        store.clear_notices([], now)  # stamp a fresh report anchor: the cadence clock
        store.enqueue_notice("decision", "d1 queued", now)
        assert store.due_notices(240, now) == []  # queued ≠ due: cadence owns the flush
        later = now + timedelta(minutes=240)
        due = store.due_notices(240, later)
        assert [n["id"] for n in due] == ["p1"]
        store.clear_notices([n["id"] for n in due], later)
        store.enqueue_notice("goal_report", "worked on g1", later)
        assert store.due_notices(240, later + timedelta(minutes=239)) == []
        assert len(store.due_notices(240, later + timedelta(minutes=240))) == 1

    def test_first_notice_after_a_long_gap_flushes_on_the_next_pass(self, store):
        # last_report_at is ancient/empty: cadence is a MAX rate, not a wait-after-queue.
        store.enqueue_notice("decision", "d1 queued")
        assert len(store.due_notices(240, hermes_now())) == 1

    def test_notice_text_capped_and_ids_increment(self, store):
        first = store.enqueue_notice("decision", "x" * 5000)
        second = store.enqueue_notice("goal_report", "short")
        assert first["id"] == "p1" and second["id"] == "p2"
        assert len(first["text"]) == proactive_store.MAX_NOTICE_CHARS

    def test_outbox_bounded_oldest_dropped(self, store):
        for index in range(proactive_store.MAX_OUTBOX + 3):
            store.enqueue_notice("decision", f"notice {index}")
        outbox = store.load()["outbox"]
        assert len(outbox) == proactive_store.MAX_OUTBOX
        assert outbox[0]["text"] == "notice 3"  # the oldest three were dropped


class TestReportComposition:
    def test_empty_and_blank_input_compose_nothing(self):
        assert compose_report([]) is None
        assert compose_report([{"text": "  "}]) is None

    def test_multiline_notice_indents_under_its_bullet(self):
        report = compose_report([
            {"text": "decision [d1] Which DB?\nrecommends: Postgres\n(/decisions show d1)"},
            {"text": "worked on g1: drafted the plan"},
        ])
        lines = report.splitlines()
        assert lines[0] == proactive_store.REPORT_HEADER
        assert lines[1] == "• decision [d1] Which DB?"
        assert lines[2] == "  recommends: Postgres"
        assert lines[4] == "• worked on g1: drafted the plan"

    def test_report_capped(self):
        report = compose_report([{"text": "y" * proactive_store.REPORT_MAX_CHARS * 2}])
        assert len(report) == proactive_store.REPORT_MAX_CHARS
        assert report.endswith("…")


class TestQuietHours:
    def test_parse_window_shapes(self):
        assert parse_quiet_hours("23:00-07:00") == (time(23, 0), time(7, 0))
        assert parse_quiet_hours("9:30 - 17:00") == (time(9, 30), time(17, 0))
        assert parse_quiet_hours("") is None
        assert parse_quiet_hours(None) is None
        assert parse_quiet_hours("whenever") is None
        assert parse_quiet_hours("24:00-07:00") is None  # out of range degrades to disabled
        assert parse_quiet_hours("23:00-23:00") is None  # zero-length window is disabled

    def test_wraparound_window(self):
        late = datetime(2026, 10, 12, 23, 30, tzinfo=timezone.utc)
        night = datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc)
        morning = datetime(2026, 10, 13, 7, 0, tzinfo=timezone.utc)
        day = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        spec = "23:00-07:00"
        assert in_quiet_hours(spec, late)
        assert in_quiet_hours(spec, night)
        assert not in_quiet_hours(spec, morning)  # end is exclusive
        assert not in_quiet_hours(spec, day)

    def test_same_day_window_and_disabled(self):
        spec = "09:00-17:00"
        assert in_quiet_hours(spec, datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc))
        assert in_quiet_hours(spec, datetime(2026, 10, 12, 16, 59, tzinfo=timezone.utc))
        assert not in_quiet_hours(spec, datetime(2026, 10, 12, 17, 0, tzinfo=timezone.utc))
        assert not in_quiet_hours("bogus", datetime(2026, 10, 12, 3, 0, tzinfo=timezone.utc))


class TestEscalation:
    def test_impact_field_decides_the_tier(self):
        wake_patterns = (r"\b(urgent|outage)\b",)
        assert escalation_tier("urgent: the deploy pipeline is blocked", wake_patterns) == "wake"
        assert escalation_tier("service outage until answered", wake_patterns) == "wake"
        assert escalation_tier("blocks the queue refactor", wake_patterns) == "wait"
        assert escalation_tier("", wake_patterns) == "wait"

    def test_patterns_match_case_insensitively_and_invalid_skip(self):
        assert escalation_tier("URGENT fix needed", (r"\burgent\b",)) == "wake"
        assert escalation_tier("anything", (r"[unclosed",)) == "wait"

    def test_default_patterns_configured_on_the_proactive_section(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        # The canonical defaults live in DEFAULT_CONFIG (the config is the user-facing doc);
        # the policy itself starts pattern-less so there is exactly one source.
        assert escalation_tier("urgent: prod is down") == "wait"
        patterns = DEFAULT_CONFIG["proactive"]["wake_patterns"]
        assert patterns, "wake patterns must ship enabled by default"
        assert escalation_tier("urgent: prod is down", patterns) == "wake"
        assert escalation_tier("picks the library for the retry wrapper", patterns) == "wait"


class TestSettings:
    def test_reader_reflects_a_written_config_yaml(self, tmp_path, monkeypatch):
        (tmp_path / "config.yaml").write_text(
            "proactive:\n"
            "  enabled: false\n"
            "  interval_minutes: 90\n"
            "  max_goals_per_tick: 3\n"
            "  report_interval_minutes: 60\n"
            "  quiet_hours: \"01:00-05:00\"\n"
            "  wake_patterns: ['\\bship\\b']\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        settings = proactive_settings()
        assert settings["enabled"] is False
        assert settings["interval_minutes"] == 90
        assert settings["max_goals_per_tick"] == 3
        assert settings["report_interval_minutes"] == 60
        assert settings["quiet_hours"] == "01:00-05:00"
        assert settings["wake_patterns"] == [r"\bship\b"]

    def test_defaults_fill_an_empty_config(self, tmp_path, monkeypatch):
        (tmp_path / "config.yaml").write_text("model: x\n", encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        settings = proactive_settings()
        assert settings["enabled"] is True
        assert settings["interval_minutes"] == 30
        assert settings["report_interval_minutes"] == 240
        assert parse_quiet_hours(settings["quiet_hours"]) == (time(23, 0), time(7, 0))

    def test_unknown_subkeys_do_not_leak_into_settings(self, tmp_path, monkeypatch):
        (tmp_path / "config.yaml").write_text("proactive:\n  bogus: 1\n", encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert "bogus" not in proactive_settings()


class TestStoreShape:
    def test_missing_file_is_empty_and_hand_edited_states_normalize(self, store, tmp_path):
        assert store.load() == {"outbox": [], "last_tick_at": "", "last_report_at": "", "picked": {}}
        (tmp_path / "proactive.yaml").write_text(
            "outbox:\n  - {id: p1, kind: decision, text: queued, created_at: '2026-01-01T00:00:00+00:00'}\n"
            "  - not-a-mapping\n"
            "picked: {g1: '2026-01-01T00:00:00+00:00'}\n",
            encoding="utf-8",
        )
        state = store.load()
        assert [n["id"] for n in state["outbox"]] == ["p1"]
        assert state["picked"] == {"g1": "2026-01-01T00:00:00+00:00"}

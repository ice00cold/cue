"""GoalsStore behaviour: CRUD roundtrips, persistence shape, prompt-block purity,
review windows and the drift ledger. Contracts, not snapshots."""

import json
from datetime import timedelta

import pytest

from hermes_time import now as hermes_now
from tools import goals_store
from tools.goals_store import GoalsStore, GOALS_BLOCK_HEADER


@pytest.fixture
def store(tmp_path):
    return GoalsStore(tmp_path)


def _create(store, **fields):
    base = {"statement": "Ship Cue phase 2", "why": "Goals make the agent goal-oriented",
            "next_action": "Write the drift tests"}
    base.update(fields)
    return store.add_goal(**base)


class TestCrud:
    def test_create_persists_yaml_with_every_field(self, store, tmp_path):
        goal = _create(store, review_cadence="weekly")
        raw = (tmp_path / "goals.yaml").read_text(encoding="utf-8")
        assert "Ship Cue phase 2" in raw
        assert goal["id"] == "g1"
        assert goal["status"] == "active"
        assert goal["review_cadence"] == "weekly"
        assert goal["created_at"] and goal["updated_at"]
        assert goal["progress"] == []

    def test_ids_increment_and_are_not_reused_after_remove(self, store):
        _create(store)
        second = _create(store, statement="Second goal")
        store.remove_goal("g1")
        third = store.add_goal(statement="Third goal", why="", next_action="")
        assert [second["id"], third["id"]] == ["g2", "g3"]

    def test_update_patches_only_given_fields(self, store):
        _create(store)
        before = store.read()[0]["created_at"]
        goal = store.update_goal("g1", {"status": "paused"})
        assert goal["status"] == "paused"
        assert goal["statement"] == "Ship Cue phase 2"
        assert goal["created_at"] == before
        assert goal["updated_at"] >= before

    def test_log_appends_progress_and_touches_updated_at(self, store):
        _create(store)
        goal = store.log_progress("g1", "Store tests written")
        assert [e["note"] for e in goal["progress"]] == ["Store tests written"]
        assert goal["progress"][0]["at"]
        goal = store.log_progress("g1", "Roundtrip verified")
        assert len(store.read()[0]["progress"]) == 2

    def test_unknown_goal_id_is_keyerror(self, store):
        with pytest.raises(KeyError):
            store.update_goal("g9", {"status": "paused"})

    def test_goal_limit_enforced(self, store):
        for i in range(goals_store.MAX_GOALS):
            store.add_goal(statement=f"goal {i}", why="", next_action="")
        with pytest.raises(ValueError):
            store.add_goal(statement="one too many", why="", next_action="")


class TestPersistence:
    def test_missing_file_reads_empty(self, store):
        assert store.read() == []
        assert store.format_for_system_prompt() is None

    def test_corrupt_file_is_backed_up_not_lost(self, store, tmp_path):
        (tmp_path / "goals.yaml").write_text("{not: valid: yaml:", encoding="utf-8")
        assert store.read() == []
        backups = list(tmp_path.glob("goals.yaml.corrupt"))
        assert backups and "not: valid" in backups[0].read_text(encoding="utf-8")

    def test_hand_edited_file_normalizes(self, store, tmp_path):
        (tmp_path / "goals.yaml").write_text(json.dumps({
            "goals": [{"id": "g7", "statement": "Hand written", "status": "weird"}],
        }), encoding="utf-8")
        goal = store.read()[0]
        assert goal["id"] == "g7"
        assert goal["status"] == "active"  # unknown status falls back, renderable
        assert goal["progress"] == []
        assert store.add_goal(statement="Next", why="", next_action="")["id"] == "g8"

    def test_two_store_instances_see_each_others_writes(self, store, tmp_path):
        _create(store)
        assert GoalsStore(tmp_path).read()[0]["statement"] == "Ship Cue phase 2"


class TestPromptBlock:
    def test_no_active_goals_means_no_block(self, store):
        _create(store, status="done")
        assert store.format_for_system_prompt() is None

    def test_block_lists_active_goals_with_next_action(self, store):
        _create(store)
        block = store.format_for_system_prompt()
        assert block.startswith(GOALS_BLOCK_HEADER)
        assert "[g1] Ship Cue phase 2 — next: Write the drift tests" in block

    def test_block_counts_but_hides_inactive_goals(self, store):
        _create(store, statement="Visible goal", next_action="")
        _create(store, statement="Hidden goal", status="paused")
        block = store.format_for_system_prompt()
        assert "Hidden goal" not in block
        assert "1 paused" in block

    def test_block_is_pure_of_time(self, store):
        _create(store)
        first = store.format_for_system_prompt()
        _create(store, statement="Another goal")
        second = store.format_for_system_prompt()
        assert first != second  # content change is the only way the bytes change
        assert "updated_at" not in first.lower() and "created_at" not in first.lower()


class TestReviewsAndLedgers:
    def test_review_window_covers_only_new_material(self, store):
        _create(store)
        store.log_progress("g1", "first entry")
        payload = store.review_payload("daily")
        assert payload["goals"][0]["progress_since_last_review"]
        assert payload["drift_since_last_review"] == []
        # review recorded → a follow-up (record=False) shows an empty window
        payload = store.review_payload("daily", record=False)
        assert payload["since_last_review"]
        assert payload["goals"][0]["progress_since_last_review"] == []

    def test_daily_and_weekly_windows_are_independent(self, store):
        _create(store)
        store.review_payload("daily")
        payload = store.review_payload("weekly")
        assert payload["since_last_review"] is None  # no weekly review recorded yet
        assert payload["review_focus"].startswith("Weekly deep review")

    def test_drift_events_recorded_and_reported(self, store):
        _create(store)
        store.record_drift("reorganized ~/Downloads", ["terminal", "write_file"])
        payload = store.review_payload("daily")
        event = payload["drift_since_last_review"][0]
        assert event["tools"] == ["terminal", "write_file"]
        assert event["summary"] == "reorganized ~/Downloads"

    def test_progress_logged_during_window_links_the_turn(self, store):
        _create(store)
        entry_at = hermes_now() - timedelta(minutes=5)
        # A progress entry written five minutes ago is after any turn older than that.
        state = store.load()
        state["goals"][0]["progress"] = [{"at": entry_at.isoformat(), "note": "moved"}]
        store.save(state)
        latest = store.latest_progress_at()
        assert abs((latest - entry_at).total_seconds()) < 1

    def test_unknown_review_kind_does_not_stamp(self, store):
        _create(store)
        store.record_review("monthly")
        assert store.last_review_at("monthly") is None

"""Fire-time guards: stale Slack creation-thread routing + relay-fronted preflight.

Two related defects on relay-fronted Slack deployments:

1. Jobs persisted before the synthetic-thread capture fix carry the creation
   message's own id as ``origin.thread_id``. At fire time ``deliver=origin``
   replayed it unconditionally, and the Slack origin-affinity re-attach put it
   back even on explicit ``slack:<chat_id>`` targets. Guard: when the resolved
   Slack chat IS the configured home chat, the origin thread is a stale
   per-message artifact — deliver top-level (home thread config still wins).

2. ``_preflight_check_delivery`` validated the ``slack:`` prefix against
   natively-configured platforms only; in relay-only topology that set is
   ``{relay}`` and the job was refused with "no gateway credentials configured"
   although fire-time routing (resolve_delivery_transport + fronts_platform)
   would have delivered it. Preflight must consult the relay's fronted set.
"""

from unittest.mock import MagicMock, patch

import pytest

from cron import scheduler as sched
from cron import scheduler_delivery as sched_delivery
from cron.scheduler_preflight import _preflight_check_delivery
from cron.scheduler_delivery import _resolve_single_delivery_target, cron_delivery_targets


def _slack_home(monkeypatch, chat_id="D0BJTDCSR7C", thread_id=None):
    monkeypatch.setattr(sched_delivery, "_get_home_target_chat_id",
                        lambda p: chat_id if p == "slack" else None)
    monkeypatch.setattr(sched_delivery, "_get_home_target_thread_id",
                        lambda p: thread_id if p == "slack" else None)


SYNTH = "1755043010.123456"


class TestOriginThreadAlwaysFlat:
    """Cue: origin is an egress address (platform + chat) — a stored thread_id is dropped at
    read time whatever the platform, home config or staleness heuristic."""

    def test_origin_thread_dropped_regardless_of_home_or_platform(self, monkeypatch):
        _slack_home(monkeypatch)
        for origin in (
            {"platform": "slack", "chat_id": "D0BJTDCSR7C", "thread_id": SYNTH},
            {"platform": "slack", "chat_id": "C0AGENERAL", "thread_id": "1755040000.000100"},
            {"platform": "telegram", "chat_id": "-1003941067111", "thread_id": "2203"},
        ):
            target = _resolve_single_delivery_target({"origin": origin}, "origin")
            assert target == {"platform": origin["platform"], "chat_id": origin["chat_id"],
                              "_resolved_from": "origin"}

    def test_explicit_target_never_inherits_the_origin_thread(self, monkeypatch):
        _slack_home(monkeypatch, chat_id="D_OTHER_HOME")
        monkeypatch.setattr(
            "tools.send_message_tool.prepare_send_message_platforms", lambda: None)
        monkeypatch.setattr(
            "tools.send_message_tool.resolve_send_target",
            lambda platform, rest, **kw: (rest, None, None))
        job = {"origin": {"platform": "slack", "chat_id": "C0AGENERAL",
                          "thread_id": "1755040000.000100"}}
        target = _resolve_single_delivery_target(job, "slack:C0AGENERAL")
        assert target["thread_id"] is None


def _gateway_config(connected_values):
    config = MagicMock()
    config.get_connected_platforms.return_value = [
        MagicMock(value=v) for v in connected_values
    ]
    return config


class TestPreflightRelayFronted:
    def test_relay_fronted_slack_accepted(self, monkeypatch):
        """Relay-only topology fronting slack: slack:CHAT passes preflight."""
        monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "slack")
        with patch("gateway.config.load_gateway_config",
                   return_value=_gateway_config({"relay"})):
            assert _preflight_check_delivery(
                {"deliver": "slack:D0BJTDCSR7C"}) is None

    def test_unfronted_platform_still_rejected(self, monkeypatch):
        """The relay fronting slack does not whitelist other platforms."""
        monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "slack")
        with patch("gateway.config.load_gateway_config",
                   return_value=_gateway_config({"relay"})):
            reason = _preflight_check_delivery({"deliver": "discord:12345"})
            assert reason is not None
            assert "discord" in reason

    def test_native_strictness_without_relay(self, monkeypatch):
        """No relay configured: the native credential check is unchanged."""
        monkeypatch.delenv("GATEWAY_RELAY_PLATFORMS", raising=False)
        with patch("gateway.config.load_gateway_config",
                   return_value=_gateway_config({"telegram"})):
            reason = _preflight_check_delivery(
                {"deliver": "slack:D0BJTDCSR7C"})
            assert reason is not None
            assert "slack" in reason

    def test_delivery_targets_include_relay_fronted(self, monkeypatch):
        """The UI dropdown source offers relay-fronted platforms."""
        monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "slack")
        _slack_home(monkeypatch)
        monkeypatch.setattr(sched_delivery, "_iter_home_target_platforms",
                            lambda: ["slack", "telegram"])
        with patch("gateway.config.load_gateway_config",
                   return_value=_gateway_config({"relay"})):
            ids = {t["id"] for t in cron_delivery_targets()}
        assert "slack" in ids
        assert "telegram" not in ids

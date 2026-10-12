"""Webhook deliveries land in the profile's ONE main-thread session and never end it.

Cue (PLAN.md § One main thread): a webhook run is a turn in the main conversation, not a
one-shot per-delivery session. Reply egress still rides the per-delivery routing info.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.webhook import WebhookAdapter


def _adapter() -> WebhookAdapter:
    adapter = WebhookAdapter.__new__(WebhookAdapter)
    adapter.config = PlatformConfig(enabled=True, extra={})
    adapter.platform = Platform.WEBHOOK
    return adapter


def test_webhook_adapter_does_not_close_sessions_after_a_run():
    """The per-delivery session-close override is gone: the run lands in the shared main
    session, which must keep living (the base ``on_processing_complete`` default is a no-op
    reaction hook, not a session close)."""
    adapter = _adapter()
    assert type(adapter).on_processing_complete is WebhookAdapter.__mro__[1].on_processing_complete
    assert not hasattr(adapter, "_end_webhook_session")


def test_webhook_run_routes_into_the_main_thread_key():
    """The spawned run's source derives the profile's main-thread session key, whatever the
    per-delivery chat id is."""
    adapter = _adapter()
    source = adapter.build_source(chat_id="webhook:v2:abc", chat_type="webhook",
                                  user_id="webhook:route", user_name="route")
    assert adapter._source_session_key(source) == "agent:main:main-thread"


def test_spawn_agent_run_delivery_info_keys_reply_routing_not_sessions():
    """The per-delivery chat id still keys ``_delivery_info`` (reply egress), and the spawned
    event carries the unique delivery identity on its source."""
    import asyncio

    adapter = _adapter()
    adapter._delivery_info = {}
    adapter._delivery_info_created = {}
    adapter._delivery_info_order = []
    adapter._background_tasks = set()
    adapter._idempotency_ttl = 600.0
    captured = {}

    async def _capture(event):
        captured["source"] = event.source

    adapter.handle_message = _capture

    async def _run():
        task = adapter._spawn_agent_run(
            {"k": 1}, "prompt text", "delivery-1", 1.0,
            route_config={"deliver": "log"}, route_name="gh", profile=None, event_type="push")
        await task

    asyncio.run(_run())
    chat_id = captured["source"].chat_id
    assert chat_id in adapter._delivery_info
    assert adapter._delivery_info[chat_id]["deliver"] == "log"

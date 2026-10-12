"""Cue: the proactive loop — autonomous goal work between conversations (PLAN.md § Phase 4).

One supervised gateway watcher (every served profile, one pass per slow poll) does two
things, both in the profile's scope:

1. **The tick** — when the tick anchor is due (``tools/proactive_store.py``), the profile's
   ONE main thread is idle and ``hermes pause`` is not engaged, pick next actions off the
   active goals (deterministic selection: least-recently-picked first) and inject them as
   one ordinary user turn. No prompt surgery, no toolset swap — prompt caching intact. The
   turn's reply is muted (``_notification_reply_muted``, the trusted diagnostic-wake seam)
   and queued by the post-turn hook as a batched notice instead of pinging per tick.
2. **The flush** — drain the outbox of wait-tier notices into ONE report per report window,
   held through quiet hours, then mirrored into the main thread so the conversation knows
   what was sent. Wake-tier notices never queue: the decisions ping sends them immediately.

Deferral rules mirror the heartbeat poller (``run_goals.py``): a busy session or an engaged
emergency stop leaves the tick due — never parked, never dropped. Gateway-scoped like the
heartbeat and loop watchers; the durable cross-process scheduler stays ``hermes cron``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.run import GatewayRunner  # noqa: F401

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")

PROACTIVE_POLL_SECONDS = 60.0
_TICK_PROMPT_HEAD = "[Proactive goal tick — autonomous background work, fired between conversations]"


def render_tick_prompt(selection: List[dict]) -> str:
    """The injected turn: the picked goals verbatim plus the working rules for the tick."""
    lines = [_TICK_PROMPT_HEAD, "", "Advance these goals now (picked automatically):"]
    for goal in selection:
        lines.append(f"- [{goal['id']}] {goal['statement']} — next: {goal['next_action']}")
    lines += [
        "",
        "Execute the next actions with your normal tools. After each one: goals_update",
        "action=log the outcome; update next_action when the step changes; status=done when",
        "the goal is finished. A fork that needs the user's call: decisions_log it and move",
        "on — never wait, never guess for them. If an action truly needs the user present,",
        "skip it and say so briefly.",
        "",
        "End with a short summary (≤8 lines): what moved, what is blocked on what. The",
        "summary is queued for the next batched proactive report — it is not sent now.",
    ]
    return "\n".join(lines)


class GatewayProactiveMixin:
    """The proactive watcher: autonomous goal ticks + batched report flushes for GatewayRunner."""

    # ── watcher loop ────────────────────────────────────────────────────

    async def _proactive_watcher(self, interval: float = PROACTIVE_POLL_SECONDS) -> None:
        """Supervised watcher (``_POST_RECONNECT_WATCHERS``): one pass per served profile."""
        from gateway.run import _async_profile_runtime_scope, _resolve_handoff_watch_scopes
        from gateway.run_idle_gates import profile_has_proactive_work
        from tui_gateway.launch_profile_policy import async_launch_profile_scope_if_multiplexed

        async def _scope(profile_home):
            # profile_home None = the launch profile's own store; once the process multiplexes it
            # binds its own scope instead of running on ambient env (same shape as the loop watcher).
            if profile_home is not None:
                return _async_profile_runtime_scope(profile_home)
            return async_launch_profile_scope_if_multiplexed()

        await asyncio.sleep(15)  # let platforms finish connecting before the first pass
        while self._running:
            try:
                for profile_name, profile_home in await _resolve_handoff_watch_scopes(self):
                    # Cheap gate (run_idle_gates): skip the scope entry (a config + .env parse)
                    # when the profile holds no actionable goal and nothing queued.
                    if profile_home is not None and not await self._run_in_executor_with_context(
                            lambda home=profile_home: profile_has_proactive_work(home)):
                        continue
                    async with _scope(profile_home):
                        await self._proactive_pass(profile_name)
            except Exception:
                logger.debug("proactive watcher pass failed", exc_info=True)
            await asyncio.sleep(interval)

    async def _proactive_pass(self, profile_name: Optional[str]) -> None:
        """One profile's poll: flush due reports first (cheap, user-facing), then a due tick.
        A paused gateway stays silent — no turns and no outbound reports until `hermes resume`."""
        from agent.estop import check_paused

        if check_paused("proactive", logger):
            return  # the tick stays due and the outbox stays queued
        try:
            await self._proactive_flush(profile_name)
        except Exception:
            logger.debug("proactive flush failed for %s", profile_name, exc_info=True)
        try:
            await self._proactive_tick(profile_name)
        except Exception:
            logger.debug("proactive tick failed for %s", profile_name, exc_info=True)

    # ── routing ─────────────────────────────────────────────────────────

    def _proactive_route(self, profile_name: Optional[str]):
        """``(source, adapter, quick_key)`` for the profile's main thread, or None.

        The turn runs in the profile's ONE main thread (Phase 1); the source only picks the
        window: the main-thread entry's persisted origin (the chat that owns it), with the
        profile's home channel as the fallback for cron-minted rows. Never a thread lane.
        """
        from gateway.session import active_profile_main_thread_session_key

        try:
            quick_key = active_profile_main_thread_session_key()
            source = None
            entry = self.session_store.lookup_by_session_key(quick_key)
            if entry is not None and getattr(entry, "origin", None):
                source = self._restored_source(entry)
            if source is None:
                source = self._proactive_home_channel_source(profile_name)
            if source is None:
                return None
            adapter = self._delivery_adapter_for(source)
            if adapter is None or not adapter._message_handler:
                return None
            return source, adapter, quick_key
        except Exception:
            logger.debug("proactive route resolution failed for %s", profile_name, exc_info=True)
            return None

    def _proactive_home_channel_source(self, profile_name: Optional[str]):
        """Synthetic source for the profile's first home channel with a live adapter."""
        config = self.config if profile_name is None else (getattr(self, "_profile_configs", None) or {}).get(profile_name)
        if config is None:
            return None
        adapters = self._adapters_for_profile(profile_name)
        for platform, platform_config in config.platforms.items():
            home = platform_config.home_channel
            if not home or not home.chat_id:
                continue
            if not any(p.value == platform.value for p, _adapter in adapters.items()):
                continue
            return self._build_process_event_source({
                "platform": platform.value,
                "chat_type": "dm",
                "chat_id": home.chat_id,
                "thread_id": home.thread_id,
                "user_id": home.user_id,
            })
        return None

    async def _proactive_send(self, adapter, source, message: str) -> bool:
        """Best-effort one-message send to the user's window; failures keep the outbox intact."""
        metadata = None
        with suppress(Exception):
            metadata = self._thread_metadata_for_source(source)
        result = await adapter.send(source.chat_id, message, metadata=metadata)
        return result is None or bool(getattr(result, "success", True))

    # ── the tick ─────────────────────────────────────────────────────────

    async def _proactive_tick(self, profile_name: Optional[str]) -> None:
        """Fire one autonomous goal tick when due, idle and unpaused (else leave it due).
        The estop gate lives in :meth:`_proactive_pass` (one seam for flush + tick)."""
        from hermes_time import now as hermes_now
        from tools.goals_store import GoalsStore
        from tools.proactive_store import ProactiveStore, proactive_settings, select_goal_actions

        settings = proactive_settings()
        if not settings.get("enabled", True):
            return
        interval, limit = int(settings["interval_minutes"]), int(settings["max_goals_per_tick"])

        def _select():
            now = hermes_now()
            store = ProactiveStore()
            if not store.should_tick(interval, now):
                return None
            selection = select_goal_actions(
                GoalsStore().active_goals(), store.load().get("picked") or {}, limit)
            return (store, now, selection) if selection else None

        batch = await self._run_in_executor_with_context(_select)
        if batch is None:
            return
        store, now, selection = batch
        route = self._proactive_route(profile_name)
        if route is None:
            return  # no window yet: the anchor stays unconsumed and retries next pass
        source, adapter, quick_key = route
        if (self._is_session_running(quick_key)
                or quick_key in adapter._active_sessions
                or self._queue_depth(quick_key, adapter=adapter) > 0):
            return  # busy: between-conversations only — the tick stays due
        written, previous = store.record_tick(now)
        store.record_picked([goal["id"] for goal in selection], now)
        event = self._synthetic_prompt_event(source, render_tick_prompt(selection))
        # Gateway-stamped provenance, never inferred from text: a machinery turn (quiet
        # surfaces) whose reply is deterministically muted — the summary queues as the next
        # batched notice instead of pinging the chat per tick.
        event._proactive_tick = True
        event._notification_reply_muted = True
        event.metadata["gateway_session_key"] = quick_key
        try:
            await adapter.handle_message(event)
            logger.info("proactive tick fired for %s: %s", quick_key,
                        ", ".join(goal["id"] for goal in selection))
        except Exception:
            def _refund():
                store.refund_tick(written, previous)
            with suppress(Exception):
                await self._run_in_executor_with_context(_refund)
            logger.warning("proactive tick injection failed for %s", quick_key, exc_info=True)
            return
        # Acceptance accounting, same shape as the heartbeat poller: a tick whose turn
        # never started (dropped at the adapter gate — no handler, unresolved identity,
        # key mismatch) is refunded and stays due. A turn that started owns the tick,
        # whatever its outcome; the next one is a full interval away by design.
        task = getattr(adapter, "_session_tasks", {}).get(quick_key)
        if task is not None:
            def _settle(_done, _event=event, _store=store, _claim=(written, previous)):
                if not getattr(_event, "_gateway_accepted", False):
                    with suppress(Exception):
                        _store.refund_tick(*_claim)
            task.add_done_callback(_settle)
        elif quick_key not in adapter._active_sessions:
            def _refund_unaccepted():
                store.refund_tick(written, previous)
            with suppress(Exception):
                await self._run_in_executor_with_context(_refund_unaccepted)

    # ── the flush ────────────────────────────────────────────────────────

    async def _proactive_flush(self, profile_name: Optional[str]) -> None:
        """Send ONE batched report when the cadence elapsed and quiet hours allow it."""
        from hermes_time import now as hermes_now
        from tools.proactive_store import ProactiveStore, compose_report, in_quiet_hours, proactive_settings

        settings = proactive_settings()
        if in_quiet_hours(settings["quiet_hours"]):
            return  # holds the whole batch; wake-tier notices never queue

        def _due():
            now = hermes_now()
            store = ProactiveStore()
            return store, now, store.due_notices(int(settings["report_interval_minutes"]), now)

        store, now, notices = await self._run_in_executor_with_context(_due)
        if not notices:
            return
        report = compose_report(notices)
        notice_ids = [notice["id"] for notice in notices]

        def _clear():
            store.clear_notices(notice_ids, now)

        if not report:  # nothing renderable queued — consume the window, don't re-loop on it
            await self._run_in_executor_with_context(_clear)
            return
        route = self._proactive_route(profile_name)
        if route is None:
            logger.debug("proactive report held: no route for profile %s", profile_name)
            return
        source, adapter, _quick_key = route
        if not await self._proactive_send(adapter, source, report):
            logger.warning("proactive report send failed for profile %s; notices stay queued", profile_name)
            return
        await self._run_in_executor_with_context(_clear)

        def _mirror():
            from gateway.mirror import mirror_to_session

            # The report is the agent's own outgoing message → assistant role, same as the
            # send_message mirror, so the main thread knows what was sent.
            mirror_to_session(report, source_label="proactive-report")

        await self._run_in_executor_with_context(_mirror)

    # ── post-turn capture ────────────────────────────────────────────────

    async def _post_turn_proactive_summary(
        self, *, session_entry: Any, source: Any, final_response: str,
    ) -> None:
        """Queue a fired tick's muted summary as the next batched notice (no empty notices —
        a nothing-to-do tick must not wake a report window)."""
        text = (final_response or "").strip()
        if not text:
            return
        from tools.proactive_store import ProactiveStore

        await self._run_in_executor_with_context(
            lambda: ProactiveStore().enqueue_notice("goal_report", text))

"""Decision-queue gateway command: /decisions. Bound onto ``GatewayRunner`` through
``GatewaySlashCommandsMixin``."""

from __future__ import annotations

import logging

from gateway.platforms.event import MessageEvent

# Log-record parity with gateway/run.py and the origin module.
logger = logging.getLogger("gateway.run")


class GatewayDecisionCommandsMixin:
    """/decisions — the profile's pending-decision queue, resolved in chat."""

    async def _handle_decisions_command(self, event: MessageEvent) -> str:
        """List pending decisions; ``answer`` resolves one and resumes the blocked work.

        The store is per-profile (``<home>/decisions.yaml``), so the dispatch resolves under
        the event's scope in the executor (I/O off the messaging loop, ContextVars kept);
        the resume prompt re-enters through the adapter FIFO like any user turn, which lands
        in the profile's ONE main thread.
        """
        from hermes_cli.decision_command import dispatch_decision_command
        from tools.decisions_store import DecisionsStore

        arg = event.get_command_args() or ""

        def dispatch():
            return dispatch_decision_command(DecisionsStore(), arg)

        result = await self._run_in_executor_with_context(dispatch)
        if result.resume_prompt:
            # Same FIFO enqueue the goal loop uses: a plain user turn, never history surgery.
            self._enqueue_goal_turn(event, result.resume_prompt, label="decision resume", kickoff=False)
        return self._telegramized_command_reply(event, result.output)

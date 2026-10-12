"""Gateway slash commands that rotate or rewrite the session transcript: /new (topic rotation),
/title, /save, /undo, /retry, /compress. Split out of ``gateway/slash_commands.py``; bound onto
``GatewayRunner`` through ``GatewaySlashCommandsMixin``. Origin internals are imported lazily
inside the bodies to avoid the import cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Optional, Union

from agent.i18n import t
from gateway.config import Platform
from gateway.platforms.base import EphemeralReply
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session_transcript import TranscriptReadError
from gateway.slash_commands_status import history_unreadable

logger = logging.getLogger("gateway.run")  # log-record parity with gateway/run.py

def _manual_compression_reply_lines(summary: dict, compressor, focus_topic) -> list[str]:
    """Manual /compress confirmation lines, surfacing summariser/aux-model failures.
    ``_last_compress_aborted`` = no usable summary, messages unchanged.  Provider exception text is
    force-redacted at this UI boundary even when global redaction is off; an aux model recovered
    via main is an info note so the user can fix their config."""
    lines = [f"🗜️ {summary['headline']}"]
    if focus_topic:
        lines.append(t("gateway.compress.focus_line", topic=focus_topic))
    lines.append(summary["token_line"])
    if summary["note"]:
        lines.append(summary["note"])
    summary_err = getattr(compressor, "_last_summary_error", None)
    if summary_err:
        from agent.redact import redact_sensitive_text
        summary_err = redact_sensitive_text(summary_err, force=True)
    aux_fail_model = getattr(compressor, "_last_aux_model_failure_model", None)
    if getattr(compressor, "_last_compress_aborted", False):
        lines.append(t("gateway.compress.aborted", error=(summary_err or t("gateway.shared.unknown_error"))))
    elif aux_fail_model:
        aux_err = getattr(compressor, "_last_aux_model_failure_error", None) or t("gateway.shared.unknown_error")
        lines.append(t("gateway.compress.aux_failed", model=aux_fail_model, error=aux_err))
    return lines


def _compress_preview_reply(history, partial: bool, keep_last, focus_topic, agg_note: str) -> str:
    """``/compress --preview``: report what WOULD be compressed — no agent, no writes."""
    from agent.model_metadata import estimate_request_tokens_rough
    from hermes_cli.partial_compress import summarize_compress_preview

    pv_msgs = [{"role": m.get("role"), "content": m.get("content")} for m in history
               if m.get("role") in {"user", "assistant"} and m.get("content")]
    report = summarize_compress_preview(pv_msgs, partial, keep_last, focus_topic,
                                        estimate_request_tokens_rough(pv_msgs))
    lines = [f"🗜️ {line}" for line in report["lines"]]
    if agg_note:
        lines.append(agg_note)
    return "\n".join(lines)


def _reset_process_scoped_tool_state() -> None:
    """Drop env-passthrough and credential-file state at a conversation boundary (best-effort)."""
    with contextlib.suppress(Exception):
        from tools.env_passthrough import clear_env_passthrough
        clear_env_passthrough()
    with contextlib.suppress(Exception):
        from tools.credential_files import clear_credential_files
        clear_credential_files()


class GatewaySessionCommandsMixin:
    """Session-transcript slash commands (/new, /title, /save, /undo, /retry, /compress).

    Cue: /new rotates the current topic (archive + compress into durable artifacts, same
    session). The session-per-chat commands of upstream Hermes (/resume, /sessions, /branch,
    /topic) are gone — there is exactly one persistent main thread per profile.
    """

    # ------------------------------------------------------------------ /new, /reset

    async def _handle_new_command(self, event: MessageEvent) -> Union[str, EphemeralReply]:
        """/new (and /reset): rotate the current topic. The finished topic is archived and folded
        into durable artifacts; the conversation continues in the SAME session — nothing resets."""
        source = event.source
        title_arg = event.get_command_args().strip() or None
        session_key = self._session_key_for_source(source)
        # A topic rotation is a boundary for live work, not for the conversation: stop runs and
        # delegations dispatched under the finished topic and clear conversation-scoped state
        # (approvals, queue, one-turn restores) so none of it leaks into the next topic.
        self._invalidate_session_run_generation(session_key, reason="topic_rotation")
        self._release_running_agent_state(session_key)
        with contextlib.suppress(Exception):
            from tools.async_delegation import interrupt_for_session
            interrupt_for_session(session_key=session_key, reason="topic_rotation")
        self._clear_conversation_scope(session_key, reason="topic_rotation")
        _reset_process_scoped_tool_state()
        if not getattr(getattr(self, "config", None), "multiplex_profiles", False):
            return await self._run_topic_rotation(source, title_arg)
        from gateway.run import _profile_runtime_scope
        with _profile_runtime_scope(self._resolve_profile_home_for_source(source)):
            return await self._run_topic_rotation(source, title_arg)

    async def _run_topic_rotation(self, source, title_arg: Optional[str]) -> str:
        """Run one topic rotation on the main thread's transcript and describe the outcome."""
        from agent.context_rotation import render_rotation_result, rotate_topic
        from agent.conversation_compression import finalize_context_engine_compression_notification
        from gateway.run import _platform_config_key

        session_entry = await self.async_session_store.get_or_create_session(source)
        try:
            history = await self.async_session_store.load_transcript(session_entry.session_id)
        except TranscriptReadError:
            return history_unreadable()
        session_key = self._session_key_for_source(source)
        model, runtime_kwargs = self._resolve_session_agent_runtime(source=source, session_key=session_key)
        if str(runtime_kwargs.get("api_mode") or "").lower() == "codex_app_server":
            # The context lives in the server-side thread of the LIVE agent; a temporary agent
            # cannot compress it. Rotate what we can: force a native thread compaction.
            return await self._compress_codex_app_server_session(session_key, session_entry.session_id)
        if not runtime_kwargs.get("api_key"):
            return t("gateway.compress.no_provider")
        # FULL transcript (tool results included), like manual /compress.
        msgs = [m for m in history if m.get("role") in {"user", "assistant", "tool"}]
        platform_key = _platform_config_key(source.platform) if source.platform else None
        if platform_key is not None:
            runtime_kwargs["platform"] = platform_key
        runtime_kwargs["gateway_session_key"] = session_key
        runtime_kwargs["reasoning_config"] = self._resolve_session_reasoning_config(source=source, model=model)

        tmp_agent = await self._build_manual_compression_agent(session_entry.session_id, model, runtime_kwargs)
        try:
            # Not a bare run_in_executor: the profile secret scope is a contextvar the default
            # executor hop would drop, failing aux-client credential resolution closed.
            result = await self._run_in_executor_with_context(
                lambda: rotate_topic(tmp_agent, msgs, title=title_arg,
                                     task_id=session_entry.session_id or "default"))
            if result.status == "rotated":
                await self._persist_manual_compression(tmp_agent, session_entry, source, result.after_messages)
                finalize_context_engine_compression_notification(tmp_agent, committed=True)
        finally:
            finalize_context_engine_compression_notification(tmp_agent, committed=False)
            self._evict_cached_agent(session_key)  # next turn rebuilds from the compacted transcript
            await self._cleanup_agent_resources_off_loop(tmp_agent, context="topic rotation")
        return "\n".join(render_rotation_result(result))

    # ------------------------------------------------------------------ /retry, /undo

    async def _handle_retry_command(self, event: MessageEvent) -> str:
        """Handle /retry command - re-send the last user message."""
        # The canonical projection skips bookkeeping rows (role=user + display_kind) and pure
        # handoffs while still recognizing a real ask embedded in a compaction carrier.
        from agent.context_compressor import (
            history_before_user_originated_turn, retryable_user_text, split_user_originated_turn,
            user_originated_turn_view)

        source = event.source
        session_entry = await self.async_session_store.get_or_create_session(source)
        try:
            history = await self.async_session_store.load_transcript(session_entry.session_id)
        except TranscriptReadError:
            return history_unreadable()
        last_user_idx = next((i for i in range(len(history) - 1, -1, -1)
                              if user_originated_turn_view(history[i]) is not None), None)
        if last_user_idx is None:
            return t("gateway.retry.no_previous")
        # Resolve text + scaffold-preserving prefix BEFORE any write; messaging retries cannot
        # reconstruct attachments, so media/unknown content is rejected with the session untouched.
        try:
            truncated, live_view = history_before_user_originated_turn(history, last_user_idx)
            last_user_msg = retryable_user_text(live_view.get("content"))
            handoff, _ = split_user_originated_turn(history[last_user_idx])
        except ValueError as exc:
            return t("gateway.retry.unsafe", error=exc)

        if handoff is not None:
            # Composite carrier (one row = retained summary + live ask): the carrier-aware rewind
            # archives it and inserts the pure scaffold atomically, reselecting the latest carrier
            # on the same snapshot so a concurrent newer turn is never removed for stale text.
            try:
                # Plain turns keep the existing rewrite path below; #84078 owns its separate
                # archive_dropped/prefix-CAS semantics.
                rewind_result = await self.async_session_store.rewind_session(
                    session_entry.session_id, 1, require_retryable_composite=True)
            except ValueError as exc:
                return t("gateway.retry.unsafe", error=exc)
            if rewind_result is None:
                return t("gateway.retry.failed_unchanged")
            last_user_msg = rewind_result["target_text"]
        # active_only preserves the active=0/compacted=1 archive left by in-place compaction.
        elif not await self.async_session_store.rewrite_transcript(
            session_entry.session_id, truncated, active_only=True, reject_active_turn_lease=True):
            return t("gateway.retry.failed_unchanged")
        session_entry.last_prompt_tokens = 0  # transcript was truncated
        self._record_model_friction("retry", source, session_entry.session_id)
        return await self._handle_message(MessageEvent(
            text=last_user_msg, message_type=MessageType.TEXT, source=source,
            raw_message=event.raw_message, channel_prompt=event.channel_prompt))

    def _record_model_friction(self, signal: str, source, session_id: str, turns: int = 1) -> None:
        """Slash dispatch does not install the routed profile's scope, so a multiplexed runner
        names the owning home explicitly."""
        from hermes_cli.observability.shared_metrics_model import record_model_friction
        home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            with contextlib.suppress(Exception):
                home = self._resolve_profile_home_for_source(source)
        record_model_friction(signal, session_id=session_id, hermes_home=home, turns=turns)

    async def _handle_undo_command(self, event: MessageEvent) -> str:
        """Handle /undo [N] — back up N user turns (default 1), soft-deleting the truncated rows and
        echoing the backed-up text; evicts the cached agent so the next turn rebuilds from the
        active-only transcript."""
        source = event.source
        n = 1
        raw_args = event.get_command_args().strip()
        if raw_args:
            try:
                n = max(1, int(raw_args.split()[0]))
            except ValueError:
                return t("gateway.undo.invalid_count", arg=raw_args.split()[0])
        session_entry = await self.async_session_store.get_or_create_session(source)
        result = await self.async_session_store.rewind_session(session_entry.session_id, n)
        if result is None:
            return t("gateway.undo.nothing")
        session_entry.last_prompt_tokens = 0  # transcript was truncated
        self._record_model_friction("undo", source, session_entry.session_id, result.get("turns_undone") or 1)
        try:
            # The cache is keyed by the profile-namespaced key; a bare build_session_key(source)
            # yields ``agent:main:…`` and misses for every secondary profile.
            self._evict_cached_agent(self._session_key_for_source(source))
        except Exception as e:
            logger.debug("undo: cached-agent eviction skipped: %s", e)
        target_text = result["target_text"]
        preview = target_text[:200] + "..." if len(target_text) > 200 else target_text
        return t("gateway.undo.removed", turns=result["turns_undone"],
                 count=result["rewound_count"], preview=preview)

    # --------------------------------------------------------------------- /compress

    async def _handle_compress_command(self, event: MessageEvent) -> str:
        """Profile-scoping wrapper around manual /compress: multiplexed gateways resolve credentials
        through the fail-closed per-profile secret scope, which slash dispatch (unlike ``_run_agent``)
        does not install — unscoped, /compress would raise ``UnscopedSecretError``."""
        if not getattr(getattr(self, "config", None), "multiplex_profiles", False):
            return await self._handle_compress_command_inner(event)
        from gateway.run import _profile_runtime_scope
        with _profile_runtime_scope(self._resolve_profile_home_for_source(event.source)):
            return await self._handle_compress_command_inner(event)

    async def _compress_codex_app_server_session(self, session_key: str, session_id: str) -> str:
        """Manual /compress for codex_app_server sessions: compacts the LIVE cached agent's
        app-server thread (``force=True`` bypasses the ``codex_app_server_auto`` gate) and keeps it
        cached. A temporary agent or a mirror rewrite cannot shrink the server-side thread.

        See #73503.
        """
        from gateway.run import _AGENT_PENDING_SENTINEL

        agent = self._cached_agent_for(session_key, lockless_fallback=True)
        if agent is None or agent is _AGENT_PENDING_SENTINEL or getattr(agent, "_codex_session", None) is None:
            return t("gateway.compress.codex_nothing")
        compressor = getattr(agent, "context_compressor", None)
        count_before = getattr(compressor, "compression_count", 0)
        try:
            await self._run_in_executor_with_context(
                lambda: agent._compress_context([], "", force=True, task_id=session_id or "default"))
        except Exception as exc:
            return t("gateway.compress.failed", error=exc)
        if getattr(compressor, "compression_count", 0) > count_before:
            return t("gateway.compress.codex_compacted")
        return t("gateway.compress.codex_incomplete")

    async def _handle_compress_command_inner(self, event: MessageEvent) -> str:
        """Handle /compress -- manually compress conversation context; ``/compress <focus>`` tells
        the summariser what to preserve. Flags/positional forms are parsed by the shared core."""
        from agent.conversation_compression_manual import MIN_MESSAGES, parse_compress_args

        source = event.source
        session_entry = await self.async_session_store.get_or_create_session(source)
        try:
            history = await self.async_session_store.load_transcript(session_entry.session_id)
        except TranscriptReadError:
            return history_unreadable()
        if not history or len(history) < MIN_MESSAGES:
            return t("gateway.compress.not_enough")
        request = parse_compress_args(event.get_command_args() or "")
        _agg_note = t("gateway.compress.aggressive_unsupported") if request.aggressive else ""
        if request.aggressive and not request.preview:
            return _agg_note
        if request.preview:
            return _compress_preview_reply(history, request.partial, request.keep_last, request.focus_topic, _agg_note)
        try:
            return await self._run_manual_compression(source, session_entry, history, request)
        except Exception as e:
            logger.warning("Manual compress failed: %s", e)
            return t("gateway.compress.failed", error=e)

    async def _run_manual_compression(self, source, session_entry, history: list, request) -> str:
        """Build a temporary agent, run the shared compress core, persist, and describe the outcome."""
        from agent.conversation_compression import finalize_context_engine_compression_notification
        from agent.conversation_compression_manual import compress_now, render_compress_result
        from gateway.run import _platform_config_key

        session_key = self._session_key_for_source(source)
        # Platform + stable gateway session key bind this agent (for external context engines) to
        # the original conversation, not a default "cli" host.
        platform_key = _platform_config_key(source.platform) if source.platform else None
        model, runtime_kwargs = self._resolve_session_agent_runtime(source=source, session_key=session_key)
        if str(runtime_kwargs.get("api_mode") or "").lower() == "codex_app_server":
            # Context lives in the server-side thread of the LIVE cached agent; a temporary agent
            # has none (and finally-eviction would destroy the real context).
            return await self._compress_codex_app_server_session(session_key, session_entry.session_id)
        if not runtime_kwargs.get("api_key"):
            return t("gateway.compress.no_provider")
        # FULL transcript (tool results included), like auto-compress: user/assistant-only starves
        # tool-result pruning and can trip the protect-first/last early-return.
        msgs = [m for m in history if m.get("role") in {"user", "assistant", "tool"}]
        # Assign, not setdefault (a resolver value would be a stale placeholder); platform only when
        # known so None -> "cli" holds.
        if platform_key is not None:
            runtime_kwargs["platform"] = platform_key
        runtime_kwargs["gateway_session_key"] = session_key
        # Same reasoning setting as a live turn (session ``/reasoning`` > per-model > global): without it
        # the transport applies its default effort — a 400 on non-reasoning models.
        runtime_kwargs["reasoning_config"] = self._resolve_session_reasoning_config(source=source, model=model)

        tmp_agent = await self._build_manual_compression_agent(session_entry.session_id, model, runtime_kwargs)
        try:
            # Not a bare run_in_executor: the profile secret scope is a contextvar the default
            # executor hop would drop, failing aux-client credential resolution closed.
            result = await self._run_in_executor_with_context(
                lambda: compress_now(tmp_agent, msgs, request, system_message="", skip_without_window=True,
                                     task_id=session_entry.session_id or "default"))
            if result.status == "nothing_to_do":
                return t("gateway.compress.nothing_to_do")
            if result.status != "compressed":
                return "\n".join(render_compress_result(result))
            await self._persist_manual_compression(tmp_agent, session_entry, source, result.after_messages)
            finalize_context_engine_compression_notification(tmp_agent, committed=True)
            compressor = tmp_agent.context_compressor
            summary = result.summary
        finally:
            finalize_context_engine_compression_notification(tmp_agent, committed=False)
            self._evict_cached_agent(session_key)  # next turn rebuilds the prompt from current files
            # Off-loop + bounded: teardown can block on subprocess/network/SQLite.
            await self._cleanup_agent_resources_off_loop(tmp_agent, context="manual compression")
        return "\n".join(_manual_compression_reply_lines(summary, compressor, request.focus_topic))

    async def _build_manual_compression_agent(self, session_id: str, model, runtime_kwargs: dict):
        """Build the throwaway AIAgent that performs a manual /compress rewrite of *session_id*."""
        from run_agent import AIAgent
        from gateway.run import _GATEWAY_HYGIENE_PLATFORM, _seed_hygiene_system_prompt
        from hermes_cli.config import load_config as _load_cfg
        from utils import is_truthy_value as _is_truthy

        # _compress_context may persist its cached system prompt, and this agent runs outside the
        # live session's prompt environment — restore the exact live prompt so provider blocks stay.
        session_row = None
        get_session = getattr(self._session_db, "get_session", None)
        if callable(get_session):
            try:
                session_row = await get_session(session_id)
            except Exception as exc:
                logger.warning(
                    "Manual compression could not restore the system prompt for session %s: %s. "
                    "Preserving an empty prompt so the live turn rebuilds it with its configured "
                    "providers.", session_id, exc, exc_info=True)

        # compression.checkpoint_required needs the memory provider loaded so _compress_context()
        # can write the pre-compression checkpoint; otherwise keep the fast path (no provider init).
        _checkpoint_required = _is_truthy(
            ((_load_cfg() or {}).get("compression") or {}).get("checkpoint_required"),
            default=False)
        tmp_agent = AIAgent(**runtime_kwargs, model=model, max_iterations=4, quiet_mode=True,
                            skip_memory=not _checkpoint_required, enabled_toolsets=["memory"],
                            session_id=session_id,
                            session_db=getattr(self._session_db, "_db", self._session_db))
        _seed_hygiene_system_prompt(tmp_agent, session_row)
        # Real platform during construction (context engines bind correctly); the stamp afterwards
        # only marks this agent as no real surface. Since #104414 Platform is not a restore-identity
        # field, so it no longer forces the next live turn to rebuild; the seed's retain flag is what
        # keeps the reduced-toolset build out of the session row (#122822).
        tmp_agent.platform = _GATEWAY_HYGIENE_PLATFORM
        tmp_agent._print_fn = lambda *a, **kw: None
        # close() must not end the rotated session the gateway entry now points at.
        tmp_agent._end_session_on_close = False
        return tmp_agent

    async def _persist_manual_compression(self, tmp_agent, session_entry, source, compressed) -> None:
        """Commit a manual /compress result to the session store.  Rotation (new continuation id)
        makes the NEW session durable (already published, else rewritten) so the original stays searchable;
        persist BEFORE repointing so a failed write is fatal and old history stays reachable.
        In-place compaction already archived + inserted rows, and a rewrite would DELETE the
        archive; an unchanged id without in-place means rotation FAILED."""
        new_session_id = tmp_agent.session_id
        if new_session_id != session_entry.session_id:
            # Published child is already durable; a rewrite would drop rows cloned at publish.
            if not await self.async_session_store.persist_rotated_compression_child(
                    session_entry.session_id, new_session_id, compressed):
                raise RuntimeError(f"failed to persist compressed transcript for session {new_session_id}")
            session_entry.session_id = new_session_id
            await self.async_session_store._save()
            await asyncio.to_thread(self._sync_telegram_topic_binding, source, session_entry,
                                    reason="compress-command")
        elif not getattr(tmp_agent, "_last_compaction_in_place", False):
            logger.warning(
                "Manual /compress: session rotation did not occur (session_id unchanged) and in-place "
                "mode is off — preserving original transcript instead of overwriting it (#44794).")
        await self.async_session_store.update_session(session_entry.session_key, last_prompt_tokens=0)

    # ------------------------------------------------------------------ /save, /title

    async def _handle_save_command(self, event: MessageEvent) -> str:
        """Handle /save — export the current session and send it as a document."""
        import tempfile
        from hermes_cli.session_export import (
            SAVE_USAGE, default_save_filename, load_save_snapshot, normalize_save_format, render_session_for_save)

        parts = event.get_command_args().split()
        redact = bool(parts) and parts[-1].lower() in ("redact", "--redact")
        if redact:
            parts = parts[:-1]
        if not parts:
            return SAVE_USAGE
        try:
            fmt = normalize_save_format(parts[0])
        except ValueError as e:
            return f"{e}\n\n{SAVE_USAGE}"

        source = event.source
        session_entry = await self.async_session_store.get_or_create_session(source)
        session_id = session_entry.session_id
        if not self._session_db:
            return t("gateway.shared.session_db_unavailable")
        # Never trust path separators from chat input; the filename is only echoed to the platform.
        filename = parts[1] if len(parts) > 1 else default_save_filename(session_id, fmt)
        filename = os.path.basename(filename) or default_save_filename(session_id, fmt)
        from hermes_state import SessionExportTooLargeError
        try:
            # One off-loop hop for the cap check + read; the helper is shared with the CLI and TUI /save.
            export_data = await asyncio.to_thread(load_save_snapshot, self._session_db._db, session_id, fmt)
        except SessionExportTooLargeError as e:
            return str(e)
        if not export_data:
            return t("gateway.save.no_messages", session_id=session_id)
        if redact:
            from hermes_cli.session_export_md import redact_session_data
            export_data = redact_session_data(export_data)
        temp_dir = tempfile.mkdtemp(prefix="hermes_save_")
        temp_path = os.path.join(temp_dir, filename)
        try:
            # Off-loop: render + write scale with transcript size (multi-MB) and would stall the loop.
            def _render_and_write() -> None:
                rendered = render_session_for_save(export_data, fmt)
                with open(temp_path, "w", encoding="utf-8") as f:
                    f.write(rendered)

            await asyncio.to_thread(_render_and_write)
            # Profile-aware: under multiplex the requester's bot lives in _profile_adapters, not self.adapters.
            adapter = self._delivery_adapter_for(source)
            if not adapter:
                return t("gateway.save.no_adapter")
            await adapter.send_document(chat_id=source.chat_id, file_path=temp_path,
                                        caption=t("gateway.save.caption", filename=filename), file_name=filename)
            return t("gateway.save.complete")
        except Exception as e:
            logger.warning("Session /save failed: %s", e)
            return t("gateway.save.failed", error=e)
        finally:
            with contextlib.suppress(Exception):
                os.remove(temp_path)
                os.rmdir(temp_dir)

    async def _handle_title_command(self, event: MessageEvent) -> str:
        """Handle /title command — set or show the current session's title."""
        source = event.source
        session_entry = await self.async_session_store.get_or_create_session(source)
        session_id = session_entry.session_id
        if not self._session_db:
            return self._session_db_unavailable_reply()

        # The session may only exist in session_store so far (first command in a new session).
        # The messaging origin is persisted so a later /resume of this titled-but-inactive session
        # can prove it belongs to the caller's chat/thread (IDOR scoping).
        if await self._session_db.get_session_title(session_id) is None:
            with contextlib.suppress(Exception):  # may already exist
                await self._session_db.create_session(
                    session_id=session_id,
                    source=source.platform.value if source.platform else "unknown",
                    user_id=source.user_id, chat_id=source.chat_id, chat_type=source.chat_type,
                    thread_id=source.thread_id)
        title_arg = event.get_command_args().strip()
        if not title_arg:
            title = await self._session_db.get_session_title(session_id)
            if title:
                return t("gateway.title.current_with_title", session_id=session_id, title=title)
            return t("gateway.title.current_no_title", session_id=session_id)
        try:
            from hermes_state import SessionDB
            sanitized = SessionDB.sanitize_title(title_arg)
        except ValueError as e:
            return t("gateway.shared.warn_passthrough", error=e)
        if not sanitized:
            return t("gateway.title.empty_after_clean")
        try:
            if not await self._session_db.set_session_title(session_id, sanitized):
                return t("gateway.title.not_found")
        except ValueError as e:
            return t("gateway.shared.warn_passthrough", error=e)
        # Mirror the title onto the Telegram forum topic name (auto titles already do this).
        try:
            await asyncio.to_thread(self._schedule_telegram_topic_title_rename, source, session_id, sanitized)
        except Exception:
            logger.debug("Failed to rename Telegram topic from /title", exc_info=True)
        return t("gateway.title.set_to", title=sanitized)

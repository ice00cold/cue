/**
 * useSessionLiveTail (Cue Phase 6 M3 / R3) — the live view behind the
 * transcript panes.
 *
 * Subscribes one read-only SSE tail (`/api/sessions/{id}/live`) per pane,
 * anchored at the newest row the pane already renders, and exposes the rows
 * that stream in while the session runs plus the latest activity stamp. The
 * cursor only ever advances (rotation keeps row ids monotone), so reconnects
 * after drops — the same backoff ladder the sessions.changed feed uses —
 * never re-deliver a page. Terminal answers (`deleted`, `error`, HTTP 404)
 * stop the ladder honestly instead of hammering.
 *
 * This is the watch-only half of R3: interaction and token-level streaming
 * for dashboard-driven turns remain the embedded TUI's; this hook never
 * sends anything.
 */

import { useEffect, useRef, useState } from "react";
import type { SessionMessage } from "@/lib/api";
import { LiveTailHttpError, streamSessionLiveTail } from "@/lib/liveTailClient";
import type { LiveTailActivity, LiveTailEvent } from "@/lib/live-tail";
import { mergeThreadPages } from "@/lib/thread-home";
import {
  EVENTS_MAX_RECONNECT_ATTEMPTS,
  eventsReconnectDelayMs,
} from "@/lib/events-reconnect";

/** An open stream that survives this long refills the reconnect ladder. */
const HEALTHY_OPEN_GRACE_MS = 5_000;
/** Live row buffer cap; `sessions.changed`-driven refreshes fold these rows
 * into the paged window anyway, so the cap only bounds a never-refreshed pane. */
const MAX_BUFFERED_ROWS = 1000;

export type LiveTailState =
  | "idle"
  | "connecting"
  | "open"
  | "reconnecting"
  | "stopped"
  | "error";

/** HTTP answers retrying cannot fix: the session is gone or auth failed. */
function isTerminalHttpError(err: unknown): boolean {
  return (
    err instanceof LiveTailHttpError &&
    (err.status === 404 || err.status === 401 || err.status === 403)
  );
}

export function useSessionLiveTail({
  sessionId,
  profile,
  afterRowId,
  enabled,
}: {
  sessionId: string | null;
  profile: string | undefined;
  /** Newest row id the caller already renders; read once at connect. */
  afterRowId: number;
  enabled: boolean;
}) {
  const [rawState, setRawState] = useState<LiveTailState>("idle");
  const [messages, setMessages] = useState<SessionMessage[]>([]);
  const [activity, setActivity] = useState<LiveTailActivity | null>(null);
  const cursorRef = useRef(afterRowId);
  // The anchor rides a ref: the connect effect reads it at (re)mount only,
  // so a window refresh moving `afterRowId` never re-opens the stream.
  const anchorRef = useRef(afterRowId);
  useEffect(() => {
    anchorRef.current = afterRowId;
  }, [afterRowId]);

  useEffect(() => {
    if (!enabled || !sessionId) return;
    const controller = new AbortController();
    let unmounting = false;
    let terminal = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let healthyTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    // First connect anchors where the caller's window ended; the cursor then
    // only advances, so a later anchor (window refresh) can never drag the
    // tail backwards.
    cursorRef.current = anchorRef.current;
    const setState = setRawState;
    // Reset the buffer for THIS session out-of-band (the loader-callback
    // idiom): an unkeyed caller switching ids must not keep the previous
    // session's streamed rows on screen.
    queueMicrotask(() => {
      if (!unmounting) {
        setMessages([]);
        setActivity(null);
      }
    });

    const onEvent = (event: LiveTailEvent) => {
      if (unmounting) return;
      switch (event.type) {
        case "hello":
          setActivity(event.activity);
          setState("open");
          if (healthyTimer) clearTimeout(healthyTimer);
          healthyTimer = setTimeout(() => {
            healthyTimer = null;
            attempt = 0;
          }, HEALTHY_OPEN_GRACE_MS);
          break;
        case "messages":
          setMessages((prev) =>
            mergeThreadPages([prev, event.messages]).slice(-MAX_BUFFERED_ROWS),
          );
          cursorRef.current = Math.max(cursorRef.current, event.last_row_id);
          break;
        case "activity":
          setActivity(event);
          break;
        case "rotated":
          // Row ids are monotone across the compression chain, so appended
          // rows keep streaming in order; nothing to re-key here.
          break;
        case "deleted":
        case "error":
          terminal = true;
          setState("stopped");
          break;
      }
    };

    const scheduleReconnect = () => {
      if (unmounting || reconnectTimer || terminal) return;
      if (healthyTimer) {
        clearTimeout(healthyTimer);
        healthyTimer = null;
      }
      if (attempt >= EVENTS_MAX_RECONNECT_ATTEMPTS) {
        console.warn("[live-tail] gave up after max reconnect attempts");
        setState("error");
        return;
      }
      const delay = eventsReconnectDelayMs(attempt);
      attempt += 1;
      setState("reconnecting");
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        void connect();
      }, delay);
    };

    const connect = async () => {
      if (unmounting) return;
      setState((prev) => (prev === "open" ? prev : attempt === 0 ? "connecting" : "reconnecting"));
      try {
        const end = await streamSessionLiveTail({
          sessionId,
          profile,
          afterRowId: cursorRef.current,
          signal: controller.signal,
          onEvent,
        });
        if (unmounting || terminal) return;
        if (end === "ended") {
          // The server closed without a terminal frame (restart, proxy
          // timeout) — the stream is resumable at the cursor.
          scheduleReconnect();
        }
      } catch (err) {
        if (unmounting) return;
        if (isTerminalHttpError(err)) {
          terminal = true;
          setState("error");
          return;
        }
        console.warn("[live-tail] stream failed:", err);
        scheduleReconnect();
      }
    };

    void connect();

    return () => {
      unmounting = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (healthyTimer) clearTimeout(healthyTimer);
      controller.abort();
    };
  }, [sessionId, profile, enabled]);

  // A disabled pane reports idle with nothing buffered — the derived outputs
  // make the guard free of effect-body state flips.
  const active = enabled && sessionId !== null;
  return {
    state: active ? rawState : "idle",
    messages: active ? messages : [],
    activity: active ? activity : null,
  };
}

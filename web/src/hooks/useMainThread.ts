/**
 * Main-thread data hook (Cue Phase 6 M1).
 *
 * `useMainThread` resolves the profile's ONE conversation through
 * `/api/main_thread` and refetches on demand. `useSessionsChangedRefetch`
 * wires the cross-process push: the embedded TUI's PTY child watches
 * state.db and publishes `sessions.changed` into `/api/pub`, the server fans
 * it out on the child's channel, and this hook subscribes to that channel
 * (shared via the live-chat-channel store) — no polling loop as the primary
 * mechanism. Best-effort by design: drops reconnect with backoff and the
 * surface simply stays on its last fetch until the next signal.
 */

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { api } from "@/lib/api";
import type { MainThreadResponse } from "@/lib/api";
import { EventsFeedClient } from "@/lib/eventsFeedClient";
import {
  EVENTS_MAX_RECONNECT_ATTEMPTS,
  eventsReconnectDelayMs,
  shouldRetryEventsClose,
} from "@/lib/events-reconnect";
import {
  getLiveChatChannel,
  subscribeLiveChatChannel,
} from "@/lib/chat-channel-store";

/** Coalesce a burst of watcher signals (0.5 s debounce upstream) into one refetch. */
const REFETCH_DEBOUNCE_MS = 400;
/** An open feed that survives this long refills the reconnect ladder. */
const HEALTHY_OPEN_GRACE_MS = 5_000;

export function useMainThread(profile: string | undefined) {
  const [thread, setThread] = useState<MainThreadResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshToken, setRefreshToken] = useState(0);
  const scopedProfile = profile ?? "";

  // Loader-callback idiom (SessionsPage's loadSessions): the state flips live
  // inside the callback, not synchronously in an effect body. The sequence
  // guard drops a late resolve from a superseded load (profile switch, refresh).
  const requestSeq = useRef(0);
  const load = useCallback(() => {
    const seq = ++requestSeq.current;
    setLoading(true);
    api
      .getMainThread(scopedProfile)
      .then((res) => {
        if (seq !== requestSeq.current) return;
        setThread(res);
        setError(null);
      })
      .catch((err: unknown) => {
        if (seq !== requestSeq.current) return;
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (seq !== requestSeq.current) return;
        setLoading(false);
      });
  }, [scopedProfile]);

  useEffect(() => {
    let cancelled = false;
    // queueMicrotask keeps the loader's synchronous state flips out of the
    // effect body (react-hooks/set-state-in-effect; SessionsPage idiom).
    queueMicrotask(() => {
      if (!cancelled) load();
    });
    return () => {
      cancelled = true;
    };
  }, [load, refreshToken]);

  const refresh = useCallback(() => setRefreshToken((n) => n + 1), []);
  return { thread, error, loading, refresh, refreshToken };
}

export function useSessionsChangedRefetch(onChanged: () => void) {
  const channel = useSyncExternalStore(subscribeLiveChatChannel, getLiveChatChannel);
  // Latest callback without re-subscribing the socket on every render.
  const onChangedRef = useRef(onChanged);
  useEffect(() => {
    onChangedRef.current = onChanged;
  }, [onChanged]);

  useEffect(() => {
    if (!channel) return;
    const feed = new EventsFeedClient();
    let unmounting = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let healthyTimer: ReturnType<typeof setTimeout> | null = null;
    let debounceTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    const notify = () => {
      if (unmounting || debounceTimer) return;
      debounceTimer = setTimeout(() => {
        debounceTimer = null;
        onChangedRef.current();
      }, REFETCH_DEBOUNCE_MS);
    };

    const scheduleReconnect = () => {
      if (unmounting || reconnectTimer) return;
      if (healthyTimer) {
        clearTimeout(healthyTimer);
        healthyTimer = null;
      }
      if (attempt >= EVENTS_MAX_RECONNECT_ATTEMPTS) {
        console.warn("[thread] sessions.changed feed gave up after max reconnect attempts");
        return;
      }
      const delay = eventsReconnectDelayMs(attempt);
      attempt += 1;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        void connect();
      }, delay);
    };

    const connect = async () => {
      if (unmounting) return;
      try {
        await feed.connect(channel);
      } catch {
        // Pre-socket failures have no close event; the ladder recovers them here.
        if (!unmounting && feed.lastCloseCode === null) {
          scheduleReconnect();
        }
      }
    };

    const offEvent = feed.on("sessions.changed", notify);
    const offClose = feed.onClose((code) => {
      if (unmounting) return;
      console.warn(`[thread] sessions.changed feed closed code=${code ?? "none"}`);
      if (shouldRetryEventsClose(code)) scheduleReconnect();
    });
    const offState = feed.onState((state) => {
      if (state === "open") {
        if (healthyTimer) clearTimeout(healthyTimer);
        healthyTimer = setTimeout(() => {
          healthyTimer = null;
          attempt = 0;
        }, HEALTHY_OPEN_GRACE_MS);
      }
    });

    void connect();

    return () => {
      unmounting = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (healthyTimer) clearTimeout(healthyTimer);
      if (debounceTimer) clearTimeout(debounceTimer);
      offEvent();
      offClose();
      offState();
      feed.close();
    };
  }, [channel]);
}

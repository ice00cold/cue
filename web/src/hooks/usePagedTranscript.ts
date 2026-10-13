/**
 * usePagedTranscript (Cue Phase 6 M2) — the paged read-only transcript loader
 * behind every REST-transcript pane: pages backwards over the EXISTING
 * messages endpoint (compression lineage included), merges pages oldest-first,
 * supports "load older" and windowed refreshes (``sessions.changed``-driven
 * callers bump ``refreshToken``). Extracted from the M1 ThreadTranscript so
 * the drill-down's child-session panes share one reader — not a second one.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { SessionMessage } from "@/lib/api";
import {
  THREAD_TRANSCRIPT_PAGE_SIZE,
  hasOlderThreadPage,
  mergeThreadPages,
  threadPageOffset,
} from "@/lib/thread-home";

/** Distance from the bottom (px) that still counts as "following". */
export const FOLLOW_THRESHOLD_PX = 48;

export function usePagedTranscript({
  sessionId,
  profile,
  refreshToken,
}: {
  sessionId: string | null;
  profile: string | undefined;
  refreshToken: number;
}) {
  const [pages, setPages] = useState<SessionMessage[][]>([]);
  const [loading, setLoading] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pinnedRef = useRef(true);
  const scopedProfile = profile ?? "";
  const requestSeq = useRef(0);
  // The refresh loader re-fetches every loaded page in one window pass; it
  // reads the depth through a ref so the callback stays identity-stable.
  const pagesLenRef = useRef(0);
  useEffect(() => {
    pagesLenRef.current = pages.length;
  }, [pages.length]);

  const messages = useMemo(() => mergeThreadPages(pages), [pages]);
  const oldestPage = pages.length > 0 ? pages[0] : [];
  const canLoadOlder = sessionId !== null && hasOlderThreadPage(oldestPage);

  // Initial load and refreshes: pages backwards from the newest over the
  // EXISTING messages endpoint (compression lineage included), one page per
  // loaded step. Loader-callback idiom — state flips inside the callback,
  // never synchronously in the effect body.
  const loadWindow = useCallback(() => {
    if (!sessionId) return;
    const seq = ++requestSeq.current;
    setLoading(true);
    const pageCount = Math.max(pagesLenRef.current, 1);
    Promise.all(
      Array.from({ length: pageCount }, (_, i) =>
        api
          .getSessionMessagesPage(sessionId, {
            offset: threadPageOffset(i),
            limit: THREAD_TRANSCRIPT_PAGE_SIZE,
          }, scopedProfile)
          .then((res) => res.messages),
      ),
    )
      .then((nextPages) => {
        if (seq !== requestSeq.current) return;
        setPages(nextPages);
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
  }, [sessionId, profile]);

  useEffect(() => {
    let cancelled = false;
    // queueMicrotask keeps the loader's synchronous state flips out of the
    // effect body (react-hooks/set-set-in-effect; SessionsPage idiom).
    queueMicrotask(() => {
      if (!cancelled) loadWindow();
    });
    return () => {
      cancelled = true;
    };
  }, [loadWindow, refreshToken]);

  const loadOlder = useCallback(() => {
    if (!sessionId || loadingOlder) return;
    const seq = ++requestSeq.current;
    setLoadingOlder(true);
    api
      .getSessionMessagesPage(
        sessionId,
        { offset: threadPageOffset(pagesLenRef.current), limit: THREAD_TRANSCRIPT_PAGE_SIZE },
        scopedProfile,
      )
      .then((res) => {
        if (seq !== requestSeq.current) return;
        setPages((prev) => [res.messages, ...prev]);
        setError(null);
      })
      .catch((err: unknown) => {
        if (seq !== requestSeq.current) return;
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (seq !== requestSeq.current) return;
        setLoadingOlder(false);
      });
  }, [sessionId, scopedProfile, loadingOlder]);

  // Follow the newest turn: a refreshed transcript scrolls to the bottom for
  // a viewer who was already there.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !pinnedRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [messages]);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    pinnedRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < FOLLOW_THRESHOLD_PX;
  }, []);

  return {
    messages,
    loading,
    loadingOlder,
    error,
    canLoadOlder,
    loadOlder,
    scrollRef,
    onScroll,
  };
}

/**
 * usePagedTranscript (Cue Phase 6 M2) — the paged read-only transcript loader
 * behind every REST-transcript pane: pages backwards over the EXISTING
 * messages endpoint (compression lineage included), merges pages oldest-first,
 * supports "load older" and windowed refreshes (``sessions.changed``-driven
 * callers bump ``refreshToken``). Extracted from the M1 ThreadTranscript so
 * the drill-down's child-session panes share one reader — not a second one.
 *
 * M3 (R3) adds the live half: with ``live`` set (the Thread home always, a
 * drill-down child while it runs), the loaded window's newest row id anchors
 * a read-only SSE tail and streamed rows append to the merge — follow-scroll
 * keeps a pinned viewer at the bottom as the session runs. The merge dedupes
 * by row id, so rows the next window refresh also delivers collapse to one
 * copy. Live rows are display-only: interaction stays with the embedded TUI.
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
import { newestRowId } from "@/lib/live-tail";
import { useSessionLiveTail } from "@/hooks/useSessionLiveTail";

/** Distance from the bottom (px) that still counts as "following". */
export const FOLLOW_THRESHOLD_PX = 48;

export function usePagedTranscript({
  sessionId,
  profile,
  refreshToken,
  live = false,
}: {
  sessionId: string | null;
  profile: string | undefined;
  refreshToken: number;
  /** Subscribe the loaded window to the read-only live tail (M3/R3). */
  live?: boolean;
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

  // The live tail anchors at the newest LOADED row, so it only starts once a
  // window has landed (an empty transcript anchors at 0 — the first message
  // then streams in). Pages are chronological oldest-first with the newest
  // page last, so the anchor is that page's highest row id.
  const tail = useSessionLiveTail({
    sessionId,
    profile,
    afterRowId: newestRowId(pages.length > 0 ? pages[pages.length - 1] : []),
    enabled: live && pages.length > 0,
  });

  const messages = useMemo(
    () => mergeThreadPages(tail.messages.length > 0 ? [...pages, tail.messages] : pages),
    [pages, tail.messages],
  );
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
    /** M3 live tail: connection state + latest activity stamp (null when not live). */
    liveState: live ? tail.state : "idle",
    liveActivity: live ? tail.activity : null,
  };
}

/**
 * ThreadTranscript — the paged read-only transcript of the main thread.
 *
 * Pages backwards over the EXISTING messages endpoint (compression lineage
 * included) so the continuous conversation is readable on any device without
 * the TUI having streamed it. Not a chat surface: no composer, no send path —
 * interaction belongs to the embedded TUI below this pane (web/AGENTS.md).
 * Refresh signal comes from the page (sessions.changed-driven); pinned-to-
 * bottom scroll keeps live refetches following the newest turn.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronUp, History } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { MessageList } from "@/components/SessionMessageList";
import { api } from "@/lib/api";
import type { SessionMessage } from "@/lib/api";
import {
  THREAD_TRANSCRIPT_PAGE_SIZE,
  hasOlderThreadPage,
  mergeThreadPages,
  threadPageOffset,
} from "@/lib/thread-home";

const COLLAPSED_KEY = "cue-thread-transcript-collapsed";
/** Distance from the bottom (px) that still counts as "following". */
const FOLLOW_THRESHOLD_PX = 48;

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === "true";
  } catch {
    return false;
  }
}

export function ThreadTranscript({
  sessionId,
  profile,
  refreshToken,
}: {
  sessionId: string | null;
  profile: string | undefined;
  refreshToken: number;
}) {
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [pages, setPages] = useState<SessionMessage[][]>([]);
  const [loading, setLoading] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pinnedRef = useRef(true);
  const requestSeq = useRef(0);
  // The refresh loader re-fetches every loaded page in one window pass; it
  // reads the depth through a ref so the callback stays identity-stable.
  const pagesLenRef = useRef(0);
  useEffect(() => {
    pagesLenRef.current = pages.length;
  }, [pages.length]);

  const toggleCollapsed = () =>
    setCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(COLLAPSED_KEY, String(next));
      } catch {
        /* localStorage may be unavailable in private browsing */
      }
      return next;
    });

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
          }, profile)
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
    // effect body (react-hooks/set-state-in-effect; SessionsPage idiom).
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
        profile,
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
  }, [sessionId, profile, loadingOlder]);

  // Follow the newest turn: a refreshed transcript scrolls to the bottom for
  // a viewer who was already there.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || collapsed || !pinnedRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [messages, collapsed]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    pinnedRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < FOLLOW_THRESHOLD_PX;
  };

  return (
    <div className="flex min-w-0 flex-col border border-current/15">
      <div className="flex items-center gap-2 px-3 py-1.5">
        <History className="h-3.5 w-3.5 shrink-0 text-text-tertiary" />
        <span className="font-sans text-display text-xs tracking-wider text-text-tertiary">
          Transcript
        </span>
        <span className="text-xs text-text-tertiary tabular-nums">
          {sessionId ? `${messages.length} shown` : ""}
        </span>
        {(loading || loadingOlder) && <Spinner className="h-3.5 w-3.5" />}
        {error && (
          <span className="ml-auto max-w-[20rem] truncate text-xs text-destructive" title={error}>
            {error}
          </span>
        )}
        <Button
          ghost
          size="icon"
          onClick={toggleCollapsed}
          aria-expanded={!collapsed}
          aria-label={collapsed ? "Expand transcript" : "Collapse transcript"}
          className="ml-auto shrink-0 text-text-secondary hover:text-midground"
        >
          {collapsed ? <ChevronDown className="h-4 w-4" /> : <ChevronUp className="h-4 w-4" />}
        </Button>
      </div>
      {!collapsed && (
        <div className="px-3 pb-3">
          {!sessionId ? (
            <p className="py-4 text-center text-sm text-text-tertiary">
              The main thread starts with the first message — type in the
              terminal below.
            </p>
          ) : messages.length === 0 && loading ? (
            <p className="py-4 text-center text-sm text-text-tertiary">
              Loading transcript…
            </p>
          ) : messages.length === 0 ? (
            <p className="py-4 text-center text-sm text-text-tertiary">
              No messages yet — the terminal below continues this thread.
            </p>
          ) : (
            <>
              <MessageList
                messages={messages}
                className="flex max-h-[45vh] min-h-[8rem] flex-col gap-3 overflow-y-auto pr-2"
                containerRef={scrollRef}
                onScroll={onScroll}
              />
              {canLoadOlder && (
                <div className="mt-2 flex justify-center">
                  <Button
                    outlined
                    size="sm"
                    onClick={loadOlder}
                    disabled={loadingOlder}
                    className="text-xs"
                  >
                    {loadingOlder ? "Loading…" : "Load older"}
                  </Button>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

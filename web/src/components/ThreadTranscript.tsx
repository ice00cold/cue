/**
 * ThreadTranscript — the paged read-only transcript of the main thread.
 *
 * The paging itself lives in `usePagedTranscript` (shared with the M2
 * drill-down's child-session panes); this component is the Thread surface's
 * chrome: collapse toggle, count, follow-scroll and the honest empty states.
 * Not a chat surface: no composer, no send path — interaction belongs to the
 * embedded TUI below this pane (web/AGENTS.md). Refresh signal comes from the
 * page (sessions.changed-driven). M3 (R3): the pane is live — streamed rows
 * append as the thread runs anywhere (gateway, cron, proactive), with the
 * tail's activity stamp surfacing what it is doing right now.
 */

import { useState } from "react";
import { ChevronDown, ChevronUp, History } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { MessageList } from "@/components/SessionMessageList";
import { usePagedTranscript } from "@/hooks/usePagedTranscript";
import type { LiveTailActivity } from "@/lib/live-tail";

const COLLAPSED_KEY = "cue-thread-transcript-collapsed";

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === "true";
  } catch {
    return false;
  }
}

/** Live-tail status chip: connected = the pane is streaming this session. */
export function LiveTailBadge({
  state,
  activity,
}: {
  state: string;
  activity: LiveTailActivity | null;
}) {
  if (state !== "open") return null;
  const description = activity?.last_activity_description ?? null;
  return (
    <span
      data-testid="transcript-live"
      className="flex shrink-0 items-center gap-1 text-xs text-text-tertiary"
      title={
        description
          ? `Live — ${description}`
          : "Live: new turns stream in as they land"
      }
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${activity?.is_active ? "animate-pulse bg-success" : "bg-success/60"}`}
        aria-hidden="true"
      />
      live
    </span>
  );
}

export function ThreadTranscript({
  sessionId,
  profile,
  refreshToken,
  live = false,
}: {
  sessionId: string | null;
  profile: string | undefined;
  refreshToken: number;
  /** Watch the thread live (M3/R3) — the Thread home passes true. */
  live?: boolean;
}) {
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const {
    messages, loading, loadingOlder, error, canLoadOlder, loadOlder, scrollRef, onScroll,
    liveState, liveActivity,
  } = usePagedTranscript({ sessionId, profile, refreshToken, live });

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

  return (
    <div className="flex min-w-0 flex-col border border-current/15">
      <div className="flex items-center gap-2 px-3 py-1.5">
        <History className="h-3.5 w-3.5 shrink-0 text-text-tertiary" />
        <span className="font-sans text-display text-xs tracking-wider text-text-tertiary">
          Transcript
        </span>
        {live && (
          <LiveTailBadge state={liveState} activity={liveActivity} />
        )}
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

/**
 * DrilldownTranscript — the inline read-only transcript of a drill-down child
 * session (worker / cron run). Same shared paged reader as the Thread
 * transcript (`usePagedTranscript`), addressed at the node's live tip; the
 * messages endpoint includes the child's compression ancestors, so the whole
 * child conversation pages. Live PTY interaction stays with the embedded TUI
 * via `/chat?resume=` (web/AGENTS.md — no second chat surface here).
 *
 * M3 (R3): a child that is RUNNING (`live`, set from the lineage node's
 * `is_active`) streams its transcript as the worker works — the OpenChamber
 * -class "watch the worker go" view, without a second chat surface.
 */

import { Terminal } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { LiveTailBadge } from "@/components/ThreadTranscript";
import { MessageList } from "@/components/SessionMessageList";
import { usePagedTranscript } from "@/hooks/usePagedTranscript";

export function DrilldownTranscript({
  sessionId,
  title,
  profile,
  onOpenInTerminal,
  live = false,
}: {
  sessionId: string;
  title: string | null;
  profile: string | undefined;
  onOpenInTerminal: () => void;
  /** Stream while this child runs (driven by the lineage node's is_active). */
  live?: boolean;
}) {
  const {
    messages, loading, loadingOlder, error, canLoadOlder, loadOlder, scrollRef, onScroll,
    liveState, liveActivity,
  } = usePagedTranscript({ sessionId, profile, refreshToken: 0, live });

  return (
    <div className="mt-1 ml-5 border border-current/15 bg-background-base/40">
      <div className="flex min-w-0 flex-wrap items-center gap-2 px-3 py-1.5">
        <span className="font-sans text-display text-xs tracking-wider text-text-tertiary">
          {title || "Child session"} · transcript
        </span>
        {live && <LiveTailBadge state={liveState} activity={liveActivity} />}
        <span className="text-xs text-text-tertiary tabular-nums">
          {messages.length} shown
        </span>
        {(loading || loadingOlder) && <Spinner className="h-3.5 w-3.5" />}
        {error && (
          <span className="max-w-[16rem] truncate text-xs text-destructive" title={error}>
            {error}
          </span>
        )}
        <Button
          ghost
          size="sm"
          onClick={onOpenInTerminal}
          className="ml-auto shrink-0 text-xs text-text-secondary hover:text-midground"
        >
          <Terminal className="h-3.5 w-3.5" />
          Open in terminal
        </Button>
      </div>
      {messages.length === 0 && loading ? (
        <p className="px-3 pb-3 py-2 text-center text-xs text-text-tertiary">
          Loading transcript…
        </p>
      ) : messages.length === 0 ? (
        <p className="px-3 pb-3 py-2 text-center text-xs text-text-tertiary">
          No messages in this child session.
        </p>
      ) : (
        <div className="px-3 pb-3">
          <MessageList
            messages={messages}
            className="flex max-h-[40vh] min-h-[6rem] flex-col gap-3 overflow-y-auto pr-2"
            containerRef={scrollRef}
            onScroll={onScroll}
          />
          {canLoadOlder && (
            <div className="mt-2 flex justify-center">
              <Button outlined size="sm" onClick={loadOlder} disabled={loadingOlder} className="text-xs">
                {loadingOlder ? "Loading…" : "Load older"}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

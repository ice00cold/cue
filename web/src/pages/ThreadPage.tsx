/**
 * ThreadPage — the dashboard's home for the profile's ONE conversation
 * (Cue Phase 6 M1 / R1).
 *
 * A thin route root: the structured half of the Thread surface (header,
 * summary cards, paged transcript, and the M2 drill-down — workers,
 * delegations, cron cross-links) sits above the persistently-hosted
 * embedded TUI (App.tsx renders ChatPage below this page), which stays the
 * interaction pane — send, slash commands, approvals and token-level
 * streaming all belong to it (web/AGENTS.md). Data comes from
 * `/api/main_thread` + the existing sessions endpoints; `sessions.changed`
 * (cross-process push via the PTY child's watcher) drives refetches.
 */

import { ThreadDrilldown } from "@/components/ThreadDrilldown";
import { ThreadHeader } from "@/components/ThreadHeader";
import { ThreadSummaryCards } from "@/components/ThreadSummaryCards";
import { ThreadTranscript } from "@/components/ThreadTranscript";
import { useProfileScope } from "@/contexts/useProfileScope";
import {
  useMainThread,
  useSessionsChangedRefetch,
} from "@/hooks/useMainThread";

export default function ThreadPage() {
  const { profile } = useProfileScope();
  const { thread, error, loading, refresh, refreshToken } = useMainThread(profile);
  useSessionsChangedRefetch(refresh);

  return (
    <div className="flex w-full min-w-0 shrink-0 flex-col gap-3 pb-2">
      <ThreadHeader
        thread={thread}
        loading={loading}
        error={error}
        onRefresh={refresh}
      />
      <ThreadSummaryCards thread={thread} />
      {/* Keyed by the live tip: rotation/creation/profile-switch remounts the
          pane, so its paging state always belongs to the transcript shown. */}
      <ThreadTranscript
        key={thread?.live_session_id ?? "none"}
        sessionId={thread?.live_session_id ?? null}
        profile={profile}
        refreshToken={refreshToken}
      />
      {/* M2 drill-down (R2): workers, delegations and cron cross-links below
          the transcript, riding the same refresh signal. */}
      <ThreadDrilldown
        thread={thread}
        profile={profile}
        refreshToken={refreshToken}
      />
    </div>
  );
}

/**
 * ThreadDrilldown — the M2 (R2) surface under the Thread home: drill into the
 * profile's non-main sessions — worker/cron children (lineage tree over
 * `/api/sessions/{id}/lineage`), the durable delegation ledger
 * (`/api/delegations`), and cron-run cross-links over the existing cron
 * endpoints. Transcripts render through the SAME paged reader as the thread
 * (existing messages endpoint); live interaction opens the embedded TUI via
 * `/chat?resume=` — this panel never becomes a second chat surface
 * (web/AGENTS.md). Data refetches ride the page's `sessions.changed` signal.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Bot, Clock, GitBranch } from "lucide-react";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { CronRunsSection } from "@/components/CronRunsSection";
import { DelegationList } from "@/components/DelegationList";
import { LineageTree } from "@/components/LineageTree";
import type {
  CronJob,
  DelegationRecord,
  MainThreadResponse,
  SessionLineageNode,
} from "@/lib/api";
import { api } from "@/lib/api";
import { lineageNodeCount } from "@/lib/thread-drilldown";

function SectionHeader({
  icon: Icon,
  title,
  count,
  error,
  loading,
}: {
  icon: typeof Bot;
  title: string;
  count: number | null;
  error: string | null;
  loading: boolean;
}) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2 px-3 py-1.5">
      <Icon className="h-3.5 w-3.5 shrink-0 text-text-tertiary" />
      <span className="font-sans text-display text-xs tracking-wider text-text-tertiary">
        {title}
      </span>
      {count !== null && (
        <span className="text-xs text-text-tertiary tabular-nums">{count}</span>
      )}
      {loading && <Spinner className="h-3.5 w-3.5" />}
      {error && (
        <span className="ml-auto max-w-[20rem] truncate text-xs text-destructive" title={error}>
          {error}
        </span>
      )}
    </div>
  );
}

export function ThreadDrilldown({
  thread,
  profile,
  refreshToken,
}: {
  thread: MainThreadResponse | null;
  profile: string | undefined;
  refreshToken: number;
}) {
  const [children, setChildren] = useState<SessionLineageNode[] | null>(null);
  const [lineageError, setLineageError] = useState<string | null>(null);
  const [lineageLoading, setLineageLoading] = useState(true);
  const [delegations, setDelegations] = useState<DelegationRecord[] | null>(null);
  const [delegationsError, setDelegationsError] = useState<string | null>(null);
  const [delegationsLoading, setDelegationsLoading] = useState(true);
  const [jobs, setJobs] = useState<CronJob[] | null>(null);
  const [jobsError, setJobsError] = useState<string | null>(null);
  const [jobsLoading, setJobsLoading] = useState(true);
  // The lineage root: the routing-row id survives rotation (the endpoint is
  // chain-aware); the live tip is the fallback for a thread that predates it.
  const lineageRoot = thread?.session_id ?? thread?.live_session_id ?? null;
  // Selection is remembered with the root it was made under, so a rotation
  // that changes the root silently drops it (derived, no reset effect).
  const [selection, setSelection] = useState<{ root: string; sessionId: string } | null>(
    null,
  );
  const openSessionId =
    selection && selection.root === lineageRoot ? selection.sessionId : null;
  const scopedProfile = profile ?? "";

  // Loader-callback idiom (SessionsPage/useMainThread): state flips live
  // inside the callback; the sequence guard drops superseded loads.
  const requestSeq = useRef(0);
  const load = useCallback(() => {
    const seq = ++requestSeq.current;
    setLineageLoading(true);
    setDelegationsLoading(true);
    setJobsLoading(true);
    if (lineageRoot) {
      api
        .getSessionLineage(lineageRoot, scopedProfile)
        .then((res) => {
          if (seq !== requestSeq.current) return;
          setChildren(res.children);
          setLineageError(null);
        })
        .catch((err: unknown) => {
          if (seq !== requestSeq.current) return;
          setLineageError(err instanceof Error ? err.message : String(err));
        })
        .finally(() => {
          if (seq !== requestSeq.current) return;
          setLineageLoading(false);
        });
      api
        .getDelegations({ sessionId: lineageRoot }, scopedProfile)
        .then((res) => {
          if (seq !== requestSeq.current) return;
          setDelegations(res.delegations);
          setDelegationsError(null);
        })
        .catch((err: unknown) => {
          if (seq !== requestSeq.current) return;
          setDelegationsError(err instanceof Error ? err.message : String(err));
        })
        .finally(() => {
          if (seq !== requestSeq.current) return;
          setDelegationsLoading(false);
        });
    } else {
      setChildren([]);
      setLineageLoading(false);
      setDelegations([]);
      setDelegationsLoading(false);
    }
    api
      .getCronJobs(scopedProfile || "all")
      .then((res) => {
        if (seq !== requestSeq.current) return;
        setJobs(res);
        setJobsError(null);
      })
      .catch((err: unknown) => {
        if (seq !== requestSeq.current) return;
        setJobsError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (seq !== requestSeq.current) return;
        setJobsLoading(false);
      });
  }, [lineageRoot, scopedProfile]);

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

  const childCount = children === null ? null : lineageNodeCount(children);

  return (
    <div className="flex min-w-0 flex-col gap-2" data-testid="thread-drilldown">
      <div className="flex min-w-0 flex-col border border-current/15">
        <SectionHeader
          icon={GitBranch}
          title="Workers & child sessions"
          count={childCount}
          error={lineageError}
          loading={lineageLoading}
        />
        <div className="px-2 pb-2">
          {!lineageRoot ? (
            <p className="px-2 py-2 text-xs text-text-tertiary">
              Child sessions (workers, cron runs) appear here once the thread exists.
            </p>
          ) : children !== null && children.length === 0 ? (
            <p className="px-2 py-2 text-xs text-text-tertiary">
              No child sessions — workers and delegations spawned from this
              thread show up here.
            </p>
          ) : (
            children !== null && (
              <LineageTree
                nodes={children}
                profile={profile}
                openSessionId={openSessionId}
                onSelect={(sessionId) =>
                  setSelection(
                    sessionId === null || !lineageRoot
                      ? null
                      : { root: lineageRoot, sessionId },
                  )}
              />
            )
          )}
        </div>
      </div>

      <div className="flex min-w-0 flex-col border border-current/15">
        <SectionHeader
          icon={Bot}
          title="Delegations"
          count={delegations === null ? null : delegations.length}
          error={delegationsError}
          loading={delegationsLoading}
        />
        <div className="px-2 pb-2">
          {delegations !== null && delegations.length === 0 ? (
            <p className="px-2 py-2 text-xs text-text-tertiary">
              No delegations recorded for this thread.
            </p>
          ) : (
            delegations !== null && <DelegationList records={delegations} />
          )}
        </div>
      </div>

      <div className="flex min-w-0 flex-col border border-current/15">
        <SectionHeader
          icon={Clock}
          title="Cron"
          count={jobs === null ? null : jobs.length}
          error={jobsError}
          loading={jobsLoading}
        />
        <div className="px-2 pb-2">
          {jobs !== null && <CronRunsSection jobs={jobs} profile={profile} />}
        </div>
      </div>
    </div>
  );
}

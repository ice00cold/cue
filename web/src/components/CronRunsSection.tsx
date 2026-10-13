/**
 * CronRunsSection — the cron half of the M2 drill-down, composed entirely from
 * EXISTING endpoints (`/api/cron/jobs`, `/api/cron/jobs/{id}/runs`): under
 * Cue's one-thread model cron fires land IN the main thread, so this surface
 * is a cross-link — which jobs exist, when they last ran, and each job's run
 * history. A run that is an agent session opens its transcript inline or live
 * in the terminal; script-only output rows show their preview (no session
 * behind them).
 */

import { useState } from "react";
import { useNavigate } from "react-router";
import { ChevronDown, ChevronRight, Clock, Terminal } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { ListItem } from "@nous-research/ui/ui/components/list-item";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { api } from "@/lib/api";
import type { CronJob, CronRunRow } from "@/lib/api";
import { timeAgo } from "@/lib/utils";
import { cronRunSessionId } from "@/lib/thread-drilldown";

function jobStatusLabel(job: CronJob): { label: string; tone: "success" | "warning" | "destructive" | "secondary" } {
  if (!job.enabled) return { label: "Paused", tone: "secondary" };
  const status = (job.last_status ?? "").toLowerCase();
  if (status === "failed") return { label: "Failing", tone: "destructive" };
  if (status === "running" || job.state === "running") return { label: "Running", tone: "warning" };
  return { label: "Scheduled", tone: "success" };
}

function epochOf(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? null : ms / 1000;
}

function RunRow({
  run,
  onOpen,
}: {
  run: CronRunRow;
  onOpen: (sessionId: string) => void;
}) {
  const sessionId = cronRunSessionId(run);
  return (
    <li className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 py-1 text-xs">
      <span className="shrink-0 text-text-tertiary tabular-nums">
        {timeAgo(run.started_at)}
      </span>
      <span
        className="min-w-0 flex-1 truncate text-text-secondary"
        title={run.preview ?? run.title ?? run.id}
      >
        {run.title ?? run.id}
      </span>
      {run.is_active && (
        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-success" aria-label="active" />
      )}
      {sessionId ? (
        <button
          type="button"
          onClick={() => onOpen(sessionId)}
          aria-label={`Open run ${sessionId} live in the terminal`}
          title="Open this run live in the embedded terminal (resumed)"
          className="flex shrink-0 items-center gap-1 text-text-secondary hover:text-midground"
        >
          <Terminal className="h-3.5 w-3.5" />
        </button>
      ) : (
        <span className="shrink-0 text-text-tertiary" title="Script-only run — output doc, no session">
          script
        </span>
      )}
    </li>
  );
}

function JobRow({ job, profile }: { job: CronJob; profile: string | undefined }) {
  const [open, setOpen] = useState(false);
  const [runs, setRuns] = useState<CronRunRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();
  const status = jobStatusLabel(job);
  const label = job.name || job.id;
  const lastRun = epochOf(job.last_run_at);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && runs === null && !error) {
      api
        .getCronJobRuns(job.id, 5, profile)
        .then((res) => setRuns(res.runs))
        .catch((err: unknown) =>
          setError(err instanceof Error ? err.message : String(err)));
    }
  };

  return (
    <li className="border-b border-current/10 last:border-b-0">
      <ListItem onClick={toggle} aria-expanded={open} className="min-w-0 items-start px-2 py-1.5 text-sm">
        {open ? <ChevronDown className="h-3.5 w-3.5 shrink-0" /> : <ChevronRight className="h-3.5 w-3.5 shrink-0" />}
        <Badge tone={status.tone} className="shrink-0 text-xs py-0 px-1.5">
          {status.label}
        </Badge>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-text-primary">{label}</span>
          <span className="block truncate text-xs text-text-tertiary">
            {job.schedule_display ?? job.schedule?.display ?? ""}
            {lastRun ? ` · last run ${timeAgo(lastRun)}` : " · never run"}
          </span>
        </span>
      </ListItem>
      {open && (
        <div className="px-3 pb-2 pl-8">
          {error ? (
            <p className="text-xs text-destructive" title={error}>
              Runs unavailable: {error}
            </p>
          ) : runs === null ? (
            <p className="flex items-center gap-1 text-xs text-text-tertiary">
              <Spinner className="h-3 w-3" /> Loading runs…
            </p>
          ) : runs.length === 0 ? (
            <p className="text-xs text-text-tertiary">No runs yet.</p>
          ) : (
            <ul className="flex min-w-0 flex-col">
              {runs.map((run) => (
                <RunRow
                  key={run.id}
                  run={run}
                  onOpen={(sid) => navigate(`/chat?resume=${encodeURIComponent(sid)}`)}
                />
              ))}
            </ul>
          )}
          <Button
            ghost
            size="sm"
            onClick={() => navigate("/cron")}
            className="mt-1 text-xs text-text-secondary hover:text-midground"
          >
            <Clock className="h-3.5 w-3.5" />
            Manage jobs on the Cron page
          </Button>
        </div>
      )}
    </li>
  );
}

export function CronRunsSection({
  jobs,
  profile,
}: {
  jobs: CronJob[];
  profile: string | undefined;
}) {
  if (jobs.length === 0) {
    return (
      <p className="px-2 py-2 text-xs text-text-tertiary">
        No cron jobs — schedule work from the thread and it lands here.
      </p>
    );
  }
  return (
    <ul className="flex min-w-0 flex-col">
      {jobs.map((job) => (
        <JobRow key={`${job.profile ?? ""}:${job.id}`} job={job} profile={profile} />
      ))}
    </ul>
  );
}

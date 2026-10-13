/**
 * LineageTree — the worker/cron child-session tree of the M2 drill-down.
 *
 * One row per node (flattened document order, indent = nesting depth): kind,
 * title/goal, activity, volume; a row expands its inline transcript (the
 * shared paged reader over the existing messages endpoint) or opens the
 * session in the embedded TUI via `/chat?resume=`. A RUNNING child's inline
 * transcript is live (M3/R3): streamed rows append while the worker works.
 * Sessions whose cwd is an OpenChamber-managed opencode worktree get an
 * external OC link — OC renders those natively and Cue does not re-render
 * them (PHASE6-ARCHITECTURE.md).
 */

import { ChevronDown, ChevronRight, ExternalLink, Terminal } from "lucide-react";
import { useNavigate } from "react-router";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { DrilldownTranscript } from "@/components/DrilldownTranscript";
import type { SessionLineageNode } from "@/lib/api";
import { sourceLabel } from "@/pages/SessionsPage_sources";
import { timeAgo } from "@/lib/utils";
import {
  flattenLineage,
  lineageKind,
  openchamberUrl,
} from "@/lib/thread-drilldown";

const KIND_TONE: Record<string, "success" | "warning" | "secondary"> = {
  worker: "warning",
  cron: "secondary",
  session: "success",
};

function LineageRow({
  node,
  depth,
  profile,
  openSessionId,
  onToggleSelect,
  onOpenInTerminal,
}: {
  node: SessionLineageNode;
  depth: number;
  profile: string | undefined;
  openSessionId: string | null;
  onToggleSelect: (sessionId: string) => void;
  onOpenInTerminal: (sessionId: string) => void;
}) {
  const kind = lineageKind(node);
  const selected = openSessionId === node.session_id;
  const ocUrl = openchamberUrl(node.cwd);
  const label = node.title?.trim() || sourceLabel(node.source ?? "") || "Child session";

  return (
    <li>
      <div
        className={`flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 py-1 pr-1 ${selected ? "bg-midground/5" : ""}`}
        style={{ paddingLeft: `${depth * 1.25}rem` }}
      >
        <Button
          ghost
          size="icon"
          onClick={() => onToggleSelect(node.session_id)}
          aria-expanded={selected}
          aria-label={`${selected ? "Collapse" : "Open"} transcript of ${label}`}
          className="h-5 w-5 shrink-0 text-text-secondary hover:text-midground"
        >
          {selected ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        </Button>
        <Badge tone={KIND_TONE[kind]} className="shrink-0 text-xs py-0 px-1.5">
          {kind}
        </Badge>
        <button
          type="button"
          onClick={() => onToggleSelect(node.session_id)}
          className="min-w-0 max-w-full truncate text-left text-sm text-text-primary hover:text-midground"
          title={`${node.session_id}${node.cwd ? ` · ${node.cwd}` : ""}`}
        >
          {label}
        </button>
        {node.is_active && (
          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-success" aria-label="active" />
        )}
        <span className="shrink-0 text-xs text-text-tertiary tabular-nums">
          {node.message_count} msg
        </span>
        <span className="shrink-0 text-xs text-text-tertiary">
          {timeAgo(node.started_at)}
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-1">
          {ocUrl && (
            <a
              href={ocUrl}
              target="_blank"
              rel="noreferrer"
              className="flex items-center gap-1 text-xs text-text-secondary hover:text-midground"
              title="This child ran in an OpenChamber-managed opencode worktree — open it in OpenChamber"
            >
              <ExternalLink className="h-3.5 w-3.5" />
              OC
            </a>
          )}
          <button
            type="button"
            onClick={() => onOpenInTerminal(node.session_id)}
            aria-label={`Open ${label} live in the terminal`}
            title="Open this session live in the embedded terminal (resumed)"
            className="flex items-center gap-1 text-xs text-text-secondary hover:text-midground"
          >
            <Terminal className="h-3.5 w-3.5" />
          </button>
        </span>
      </div>
      {selected && (
        <DrilldownTranscript
          sessionId={node.session_id}
          title={node.title}
          profile={profile}
          onOpenInTerminal={() => onOpenInTerminal(node.session_id)}
          live={node.is_active}
        />
      )}
    </li>
  );
}

export function LineageTree({
  nodes,
  profile,
  openSessionId,
  onSelect,
}: {
  nodes: SessionLineageNode[];
  profile: string | undefined;
  openSessionId: string | null;
  onSelect: (sessionId: string | null) => void;
}) {
  const navigate = useNavigate();
  const rows = flattenLineage(nodes);
  return (
    <ul className="flex min-w-0 flex-col">
      {rows.map(({ node, depth }) => (
        <LineageRow
          key={node.id}
          node={node}
          depth={depth}
          profile={profile}
          openSessionId={openSessionId}
          onToggleSelect={(sid) => onSelect(openSessionId === sid ? null : sid)}
          onOpenInTerminal={(sid) =>
            navigate(`/chat?resume=${encodeURIComponent(sid)}`)}
        />
      ))}
    </ul>
  );
}

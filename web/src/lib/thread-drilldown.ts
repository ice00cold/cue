/**
 * Thread-drilldown helpers (Cue Phase 6 M2) — the pure half of the R2
 * surface: lineage-tree flattening, delegation badge/summary derivations, the
 * cron-run→session cross-link rule and the OpenChamber deep-link rule.
 * Effects and fetching live in the components; everything here is
 * data-in/data-out for vitest.
 */

import type {
  CronRunRow,
  DelegationRecord,
  SessionLineageNode,
} from "@/lib/api";

/** Script-only run rows the runs endpoint mints: their ids carry this prefix
 * and have NO session behind them (never a resume target). */
export const CRON_OUTPUT_ROW_PREFIX = "cron_output:";

export interface FlattenedLineageRow {
  node: SessionLineageNode;
  depth: number;
}

/** Depth-first flatten of the lineage tree (document order), keeping each
 * node's nesting depth for indentation. */
export function flattenLineage(
  nodes: SessionLineageNode[],
  depth = 0,
  out: FlattenedLineageRow[] = [],
): FlattenedLineageRow[] {
  for (const node of nodes) {
    out.push({ node, depth });
    flattenLineage(node.children, depth + 1, out);
  }
  return out;
}

/** Total nodes in the tree, nested included. */
export function lineageNodeCount(nodes: SessionLineageNode[]): number {
  return flattenLineage(nodes).length;
}

/** Drill-down kind for a child session, from its source. */
export function lineageKind(
  node: Pick<SessionLineageNode, "source">,
): "worker" | "cron" | "session" {
  const source = node.source ?? "";
  if (source === "cron") return "cron";
  if (source === "subagent" || source === "tool" || source === "vulcan_delegate") {
    return "worker";
  }
  return "session";
}

/** Badge tone for a delegation's ledger state. */
export function delegationBadge(
  state: string,
): { label: string; tone: "success" | "warning" | "destructive" | "secondary" } {
  const normalized = (state || "").toLowerCase();
  if (normalized === "running" || normalized === "finalizing") {
    return { label: "Running", tone: "warning" };
  }
  if (normalized === "completed" || normalized === "success") {
    return { label: "Done", tone: "success" };
  }
  if (
    normalized === "error" || normalized === "failed" || normalized === "interrupted" ||
    normalized === "stalled" || normalized === "unknown"
  ) {
    return { label: normalized === "unknown" ? "Unknown" : "Failed", tone: "destructive" };
  }
  return { label: state || "—", tone: "secondary" };
}

/** One-line result summary for a delegation row ("" when there is nothing to say). */
export function delegationResultLine(record: DelegationRecord): string {
  const result = record.result;
  if (!result) {
    return record.state === "running" ? "in progress…" : "";
  }
  const parts: string[] = [];
  if (typeof result.task_count === "number") {
    const failed = (result.tasks ?? []).filter(
      (t) => !["success", "completed"].includes((t.status || "").toLowerCase()),
    ).length;
    parts.push(`${result.task_count} tasks${failed ? ` · ${failed} failed` : ""}`);
    if (result.truncated) parts.push("more…");
  } else if (result.summary) {
    parts.push(result.summary);
  }
  if (result.error) parts.push(String(result.error));
  return parts.join(" · ");
}

/** The delegation's display goal: a batch's joined goals, else its goal. */
export function delegationGoal(record: DelegationRecord): string {
  const task = record.task ?? {};
  if (Array.isArray(task.goals) && task.goals.length > 0) {
    return task.goals.join(", ");
  }
  return task.goal ?? "";
}

/**
 * Session id a cron-run row can be opened with, or null for script-only
 * output rows (`cron_output:` prefix — output doc metadata, no session).
 */
export function cronRunSessionId(run: Pick<CronRunRow, "id">): string | null {
  return run.id.startsWith(CRON_OUTPUT_ROW_PREFIX) ? null : run.id;
}

/** Verified 2026-10-12 (PHASE6-ARCHITECTURE.md §7): the OpenChamber web UI
 * carries NO per-session URL routing (its bundle has no router/pushState), so
 * the deep link for opencode-worktree sessions is the app itself — the
 * documented fallback. Override the base for a remote OC instance. */
export const DEFAULT_OPENCHAMBER_URL = "http://127.0.0.1:3000";

const OPENCODE_WORKTREE_SEGMENT = "/.local/share/opencode/worktree/";

/** Whether a session's cwd is an OpenChamber-managed opencode worktree. */
export function isOpencodeWorktreeCwd(cwd: string | null | undefined): boolean {
  return typeof cwd === "string" && cwd.includes(OPENCODE_WORKTREE_SEGMENT);
}

/** Deep link into OpenChamber for an opencode-worktree session, else null. */
export function openchamberUrl(
  cwd: string | null | undefined,
  base = DEFAULT_OPENCHAMBER_URL,
): string | null {
  return isOpencodeWorktreeCwd(cwd) ? base : null;
}

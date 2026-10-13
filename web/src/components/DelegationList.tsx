/**
 * DelegationList — the durable delegation ledger rows of the M2 drill-down.
 *
 * One row per `async_delegations` record: state badge, goal (batch goals
 * joined), timing and a one-line result summary; expanding shows the bounded
 * per-task status lines the endpoint already projected (a batch caps its task
 * list — counts carry the truth). A delegation's conversation is its ORIGIN
 * session — this list links, it never re-renders transcripts.
 */

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { ListItem } from "@nous-research/ui/ui/components/list-item";
import type { DelegationRecord } from "@/lib/api";
import { timeAgo } from "@/lib/utils";
import {
  delegationBadge,
  delegationGoal,
  delegationResultLine,
} from "@/lib/thread-drilldown";

function DelegationRow({ record }: { record: DelegationRecord }) {
  const [open, setOpen] = useState(false);
  const badge = delegationBadge(record.state);
  const goal = delegationGoal(record) || "(no goal recorded)";
  const summary = delegationResultLine(record);
  const tasks = record.result?.tasks ?? [];

  return (
    <li className="border-b border-current/10 last:border-b-0">
      <ListItem
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="min-w-0 items-start px-2 py-1.5 text-sm"
      >
        {open ? <ChevronDown className="h-3.5 w-3.5 shrink-0" /> : <ChevronRight className="h-3.5 w-3.5 shrink-0" />}
        <Badge tone={badge.tone} className="shrink-0 text-xs py-0 px-1.5">
          {badge.label}
        </Badge>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-text-primary" title={goal}>
            {goal}
          </span>
          <span className="block truncate text-xs text-text-tertiary">
            dispatched {timeAgo(record.dispatched_at)}
            {record.completed_at ? ` · finished ${timeAgo(record.completed_at)}` : ""}
            {summary ? ` · ${summary}` : ""}
          </span>
        </span>
      </ListItem>
      {open && (
        <div className="px-3 pb-2 pl-8">
          {record.task?.model && (
            <p className="text-xs text-text-tertiary">model: {record.task.model}</p>
          )}
          {record.task?.owner_cwd && (
            <p className="truncate text-xs text-text-tertiary" title={record.task.owner_cwd}>
              cwd: {record.task.owner_cwd}
            </p>
          )}
          {tasks.length > 0 ? (
            <ul className="mt-1 flex flex-col gap-0.5">
              {tasks.map((task) => (
                <li key={task.task_index} className="flex min-w-0 items-center gap-2 text-xs">
                  <span className="tabular-nums text-text-tertiary">#{task.task_index}</span>
                  <span className="min-w-0 flex-1 truncate text-text-secondary" title={task.goal}>
                    {task.goal || "(no goal)"}
                  </span>
                  <span
                    className={
                      ["success", "completed"].includes(task.status.toLowerCase())
                        ? "text-success"
                        : "text-destructive"
                    }
                  >
                    {task.status}
                  </span>
                  {task.error && (
                    <span className="max-w-[12rem] truncate text-destructive" title={task.error}>
                      {task.error}
                    </span>
                  )}
                </li>
              ))}
              {record.result?.truncated && (
                <li className="text-xs text-text-tertiary">
                  … {record.result.task_count! - tasks.length} more tasks
                </li>
              )}
            </ul>
          ) : (
            <p className="mt-1 text-xs text-text-tertiary">
              {record.result?.summary || record.result?.error || "No result detail recorded."}
            </p>
          )}
        </div>
      )}
    </li>
  );
}

export function DelegationList({ records }: { records: DelegationRecord[] }) {
  return (
    <ul className="flex min-w-0 flex-col">
      {records.map((record) => (
        <DelegationRow key={record.delegation_id} record={record} />
      ))}
    </ul>
  );
}

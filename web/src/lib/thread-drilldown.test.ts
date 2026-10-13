// Pure helpers of the M2 drill-down surface (thread-drilldown.ts): tree
// flattening, kind/badge derivations, the cron-run cross-link rule and the
// OpenChamber deep-link rule (verified fallback: the OC web UI has no
// per-session URL routing).
import { describe, expect, it } from "vitest";
import type {
  DelegationRecord,
  SessionLineageNode,
} from "@/lib/api";
import {
  CRON_OUTPUT_ROW_PREFIX,
  cronRunSessionId,
  DEFAULT_OPENCHAMBER_URL,
  delegationBadge,
  delegationGoal,
  delegationResultLine,
  flattenLineage,
  isOpencodeWorktreeCwd,
  lineageKind,
  lineageNodeCount,
  openchamberUrl,
} from "./thread-drilldown";

function node(
  id: string,
  children: SessionLineageNode[] = [],
  source = "subagent",
): SessionLineageNode {
  return {
    id,
    session_id: id,
    chain: [id],
    source,
    title: null,
    started_at: 1,
    ended_at: null,
    message_count: 1,
    tool_call_count: 0,
    input_tokens: 0,
    output_tokens: 0,
    is_active: false,
    child_count: children.length,
    children,
  };
}

describe("flattenLineage / lineageNodeCount", () => {
  it("flattens depth-first with nesting depths", () => {
    const tree = [
      node("w-1", [node("w-1a"), node("w-1b", [node("w-1b-i")])]),
      node("cron-1", [], "cron"),
    ];
    expect(flattenLineage(tree)).toEqual([
      { node: tree[0], depth: 0 },
      { node: tree[0].children[0], depth: 1 },
      { node: tree[0].children[1], depth: 1 },
      { node: tree[0].children[1].children[0], depth: 2 },
      { node: tree[1], depth: 0 },
    ]);
    expect(lineageNodeCount(tree)).toBe(5);
  });

  it("counts an empty tree as zero without inventing rows", () => {
    expect(lineageNodeCount([])).toBe(0);
    expect(flattenLineage([])).toEqual([]);
  });
});

describe("lineageKind", () => {
  it("classifies worker, cron and plain session sources", () => {
    expect(lineageKind({ source: "subagent" })).toBe("worker");
    expect(lineageKind({ source: "tool" })).toBe("worker");
    expect(lineageKind({ source: null })).toBe("session");
    expect(lineageKind({ source: "cron" })).toBe("cron");
    expect(lineageKind({ source: "telegram" })).toBe("session");
  });
});

describe("delegationBadge", () => {
  it("running states are warnings, success is green, failures red", () => {
    expect(delegationBadge("running")).toEqual({ label: "Running", tone: "warning" });
    expect(delegationBadge("finalizing")).toEqual({ label: "Running", tone: "warning" });
    expect(delegationBadge("completed")).toEqual({ label: "Done", tone: "success" });
    expect(delegationBadge("error")).toEqual({ label: "Failed", tone: "destructive" });
    expect(delegationBadge("interrupted")).toMatchObject({ tone: "destructive" });
    expect(delegationBadge("weird-state")).toEqual({
      label: "weird-state",
      tone: "secondary",
    });
  });
});

describe("delegationResultLine / delegationGoal", () => {
  const base = {
    delegation_id: "d",
    origin_session: "",
    origin_ui_session_id: "",
    origin_session_id: "main",
    parent_session_id: "main",
    state: "completed",
    dispatched_at: 1,
    completed_at: 2,
    updated_at: 2,
    delivery_state: "delivered",
    task: null,
    result: null,
  } satisfies DelegationRecord as DelegationRecord;

  it("a running delegation without result says so", () => {
    const record = { ...base, state: "running" };
    expect(delegationResultLine(record)).toBe("in progress…");
    expect(delegationGoal(record)).toBe("");
  });

  it("a single delegation summarizes its summary and error", () => {
    const record: DelegationRecord = {
      ...base,
      task: { goal: "ship M2" },
      result: { status: "completed", summary: "shipped", error: null },
    };
    expect(delegationResultLine(record)).toBe("shipped");
    expect(delegationGoal(record)).toBe("ship M2");
  });

  it("a batch reports task totals with failure count, capped marker and error", () => {
    const record: DelegationRecord = {
      ...base,
      task: { is_batch: true, goals: ["a", "b", "c"] },
      result: {
        status: "completed",
        error: "one child died",
        task_count: 3,
        tasks: [
          { task_index: 0, status: "success", goal: "a", error: null },
          { task_index: 1, status: "error", goal: "b", error: "401" },
        ],
      },
    };
    expect(delegationResultLine(record)).toBe("3 tasks · 1 failed · one child died");
    expect(delegationGoal(record)).toBe("a, b, c");
  });

  it("a batch over the cap notes truncation instead of lying by omission", () => {
    const record: DelegationRecord = {
      ...base,
      result: {
        status: "completed",
        task_count: 60,
        truncated: true,
        tasks: [],
      },
    };
    expect(delegationResultLine(record)).toBe("60 tasks · more…");
  });
});

describe("cronRunSessionId", () => {
  it("agent run rows open as sessions; script-only output rows do not", () => {
    expect(cronRunSessionId({ id: "cron_job1_20260101_000000" })).toBe(
      "cron_job1_20260101_000000",
    );
    expect(
      cronRunSessionId({ id: `${CRON_OUTPUT_ROW_PREFIX}job1:2026-01-01_00-00-00` }),
    ).toBeNull();
  });
});

describe("openchamberUrl", () => {
  const worktreeCwd =
    "/home/af/.local/share/opencode/worktree/0ddfe25bfeb2/cleanup";

  it("links opencode-worktree sessions to the OpenChamber app", () => {
    expect(openchamberUrl(worktreeCwd)).toBe(DEFAULT_OPENCHAMBER_URL);
    expect(openchamberUrl(worktreeCwd, "http://oc.lan:3000")).toBe(
      "http://oc.lan:3000",
    );
  });

  it("leaves ordinary cwd and missing cwd alone", () => {
    expect(openchamberUrl("/home/af/cue")).toBeNull();
    expect(openchamberUrl(null)).toBeNull();
    expect(isOpencodeWorktreeCwd("/home/af/cue")).toBe(false);
  });
});

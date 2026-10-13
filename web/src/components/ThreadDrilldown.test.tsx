// @vitest-environment jsdom
// ThreadDrilldown (Cue Phase 6 M2 / R2): the drill-down surface under the
// Thread home — lineage tree over the new lineage endpoint, delegation rows
// over the new delegations endpoint, cron cross-links over the EXISTING cron
// endpoints. Pinned here: the three sections render, a child row opens the
// inline paged transcript at the node's live tip (same messages endpoint),
// the terminal affordance routes to /chat?resume=, OC-worktree children get
// the OpenChamber link (and ordinary cwd does not), cron jobs expand to runs
// with script-only rows marked unopenable, and the no-thread state stays
// honest without fetching session-scoped data.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { useEffect } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  getSessionLineage: vi.fn(),
  getDelegations: vi.fn(),
  getCronJobs: vi.fn(),
  getCronJobRuns: vi.fn(),
  getSessionMessagesPage: vi.fn(),
}));

// M3 live tail: the streaming client is the seam the pane tests drive — the
// mock captures the last `onEvent` and stays "open" forever so unmount paths
// never race a resolution.
const liveTailMock = vi.hoisted(() => {
  const state = {
    onEvent: null as ((event: unknown) => void) | null,
    calls: [] as Array<Record<string, unknown>>,
  };
  return {
    state,
    streamSessionLiveTail: vi.fn(
      async (opts: { onEvent: (event: unknown) => void }) => {
        state.onEvent = opts.onEvent;
        state.calls.push(opts);
        return new Promise<string>(() => {});
      },
    ),
  };
});

vi.mock("@/lib/liveTailClient", () => ({
  streamSessionLiveTail: liveTailMock.streamSessionLiveTail,
}));

vi.mock("@/lib/api", () => ({
  api: apiMocks,
}));import type { MainThreadResponse } from "@/lib/api";

const transcriptMock = vi.hoisted(() => ({ rendered: [] as string[] }));
vi.mock("@/components/SessionMessageList", () => ({
  MessageList: ({ messages }: { messages: Array<{ content: string | null }> }) => {
    transcriptMock.rendered = messages.map((m) => m.content ?? "");
    return <div data-testid="transcript">{transcriptMock.rendered.join("|")}</div>;
  },
}));

/** Location probe: records the last routed search string (nav assertions). */
const navMock = vi.hoisted(() => ({ search: "" as string }));
function ChatProbe() {
  const location = useLocation();
  useEffect(() => {
    navMock.search = location.search;
  }, [location.search]);
  return <div data-testid="chat-probe" />;
}

let container: HTMLDivElement;
let root: Root;
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

/** Try-style waitFor: `cond` asserts (throws while not yet true). */
async function waitFor(cond: () => void, timeoutMs = 5000) {
  const start = Date.now();
  for (;;) {
    try {
      cond();
      return;
    } catch {
      /* not yet */
    }
    if (Date.now() - start > timeoutMs) {
      throw new Error("waitFor: condition never became true");
    }
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
  }
}

async function click(el: Element | null | undefined) {
  if (!el) throw new Error("element not rendered");
  // Async act: the click's fetch kicks off state updates that land on the
  // microtask queue (mocked promises); flushing inside act keeps them
  // observable rather than warned.
  await act(async () => {
    el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

const OC_WORKTREE_CWD =
  "/home/af/.local/share/opencode/worktree/0ddfe25bfeb2/cleanup";

const THREAD: MainThreadResponse = {
  profile: "default",
  session_key: "agent:main:main-thread",
  session_id: "sess-root",
  live_session_id: "sess-tip",
  exists: true,
  is_active: false,
  session: null,
};

const LINEAGE = {
  session_id: "sess-root",
  chain: ["sess-root", "sess-tip"],
  live_session_id: "sess-tip",
  truncated: false,
  profile: "default",
  children: [
    {
      id: "worker-1",
      session_id: "worker-2", // rotated worker: node addresses the tip
      chain: ["worker-1", "worker-2"],
      source: "subagent",
      title: "Scout worker",
      started_at: Date.now() / 1000 - 90,
      ended_at: null,
      message_count: 12,
      tool_call_count: 3,
      input_tokens: 100,
      output_tokens: 50,
      is_active: true,
      cwd: "/home/af/cue",
      child_count: 1,
      children: [
        {
          id: "worker-1a", session_id: "worker-1a", chain: ["worker-1a"],
          source: "subagent", title: null, started_at: Date.now() / 1000 - 60,
          ended_at: null, message_count: 2, tool_call_count: 0,
          input_tokens: 0, output_tokens: 0, is_active: false,
          cwd: OC_WORKTREE_CWD, child_count: 0, children: [],
        },
      ],
    },
  ],
};

const DELEGATIONS = {
  delegations: [
    {
      delegation_id: "d-1", origin_session: "agent:main:main-thread",
      origin_ui_session_id: "", origin_session_id: "sess-root",
      parent_session_id: "sess-root", state: "completed",
      dispatched_at: Date.now() / 1000 - 300, completed_at: Date.now() / 1000 - 240,
      updated_at: Date.now() / 1000 - 240, delivery_state: "delivered",
      task: { goal: "ship the drill-down" },
      result: { status: "completed", summary: "shipped", error: null },
    },
  ],
  count: 1, limit: 50, profile: "default",
};

const JOBS = [
  {
    id: "nightly-review", name: "nightly-review", enabled: true,
    last_run_at: new Date(Date.now() - 3_600_000).toISOString(),
    last_status: "completed", schedule_display: "0 9 * * *",
  },
];

async function renderDrilldown(thread = THREAD, refreshToken = 0) {
  const { ThreadDrilldown } = await import("@/components/ThreadDrilldown");
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route
            path="/"
            element={<ThreadDrilldown thread={thread} profile={undefined} refreshToken={refreshToken} />}
          />
          <Route path="/chat" element={<ChatProbe />} />
        </Routes>
      </MemoryRouter>,
    ),
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  transcriptMock.rendered = [];
  navMock.search = "";
  liveTailMock.state.onEvent = null;
  liveTailMock.state.calls.length = 0;
  apiMocks.getSessionLineage.mockResolvedValue(structuredClone(LINEAGE));
  apiMocks.getDelegations.mockResolvedValue(structuredClone(DELEGATIONS));
  apiMocks.getCronJobs.mockResolvedValue(structuredClone(JOBS));
  apiMocks.getCronJobRuns.mockResolvedValue({
    runs: [
      { id: "cron_nightly-review_20260101_090000", title: "review done", started_at: Date.now() / 1000 - 3600 },
      { id: "cron_output:nightly-review:2026-01-01_09-00-00", title: "Script-only run", started_at: Date.now() / 1000 - 7200 },
    ],
    limit: 5,
  });
  apiMocks.getSessionMessagesPage.mockResolvedValue({
    session_id: "worker-2",
    messages: [{ id: 1, role: "assistant", content: "scout report" }],
    pagination: { limit: 200, offset: 0, order: "latest", returned: 1 },
  });
});

afterEach(async () => {
  if (root) await act(async () => root.unmount());
  container?.remove();
});

describe("ThreadDrilldown", () => {
  it("renders the three sections: lineage tree, delegations, cron", async () => {
    await renderDrilldown();
    await waitFor(() =>
      expect(apiMocks.getSessionLineage).toHaveBeenCalledWith("sess-root", ""),
    );
    await waitFor(() => expect(container.textContent).toContain("Scout worker"));
    // Nested grandchild renders indented in the same flattened tree.
    expect(container.textContent).toContain("Subagent");
    // Delegation goal + derived badge.
    expect(container.textContent).toContain("ship the drill-down");
    expect(container.textContent).toContain("Done");
    // Cron job with last-run line.
    expect(container.textContent).toContain("nightly-review");
    expect(apiMocks.getDelegations).toHaveBeenCalledWith({ sessionId: "sess-root" }, "");
    expect(apiMocks.getCronJobs).toHaveBeenCalledWith("all");
  });

  it("opens a child's inline transcript at its live tip via the shared messages endpoint", async () => {
    await renderDrilldown();
    await waitFor(() => expect(container.textContent).toContain("Scout worker"));

    await click([...container.querySelectorAll("button")].find((b) =>
      b.textContent === "Scout worker"));
    await waitFor(() => expect(transcriptMock.rendered).toEqual(["scout report"]));
    expect(apiMocks.getSessionMessagesPage).toHaveBeenCalledWith(
      "worker-2",
      { offset: 0, limit: 200 },
      "",
    );
  });

  it("streams a RUNNING worker's transcript live while it works (M3)", async () => {
    await renderDrilldown();
    await waitFor(() => expect(container.textContent).toContain("Scout worker"));

    // Scout worker is is_active in the lineage fixture: expanding it opens a
    // live pane anchored at its loaded window's newest row.
    await click([...container.querySelectorAll("button")].find((b) =>
      b.textContent === "Scout worker"));
    await waitFor(() => expect(transcriptMock.rendered).toEqual(["scout report"]));
    await waitFor(() => expect(liveTailMock.state.calls.length).toBe(1));
    expect(liveTailMock.state.calls[0]).toMatchObject({
      sessionId: "worker-2",
      afterRowId: 1,
    });

    await act(async () => {
      liveTailMock.state.onEvent?.({
        type: "hello",
        session_id: "worker-1",
        live_session_id: "worker-2",
        profile: "default",
        after_row_id: 1,
        activity: {
          last_activity_at: Date.now() / 1000,
          last_activity_description: "reading the repo",
          ended_at: null,
          end_reason: null,
          message_count: 13,
          tool_call_count: 4,
          is_active: true,
        },
      });
    });
    await waitFor(() =>
      expect(container.querySelector('[data-testid="transcript-live"]')).not.toBeNull(),
    );

    await act(async () => {
      liveTailMock.state.onEvent?.({
        type: "messages",
        session_id: "worker-2",
        messages: [{ id: 14, role: "assistant", content: "worker streamed row" }],
        last_row_id: 14,
      });
    });
    await waitFor(() => expect(transcriptMock.rendered).toEqual([
      "scout report", "worker streamed row",
    ]));
  });

  it("keeps an idle child's pane read-only: no live tail when the worker is not running", async () => {
    await renderDrilldown();
    await waitFor(() => expect(container.textContent).toContain("Scout worker"));

    // Expand the active worker first so its nested grandchild row renders.
    await click([...container.querySelectorAll("button")].find((b) =>
      b.textContent === "Scout worker"));
    await waitFor(() => expect(container.textContent).toContain("Subagent"));
    expect(liveTailMock.state.calls.length).toBe(1);
    // Collapse it again so its tail closes…
    await click([...container.querySelectorAll("button")].find((b) =>
      b.textContent === "Scout worker"));
    await waitFor(() => expect(liveTailMock.state.calls.length).toBe(1));

    // …and open the IDLE grandchild: transcript pages, no tail connects.
    await click([...container.querySelectorAll("button")].find((b) =>
      b.textContent === "Subagent"));
    await waitFor(() => expect(apiMocks.getSessionMessagesPage).toHaveBeenCalledWith(
      "worker-1a",
      { offset: 0, limit: 200 },
      "",
    ));
    expect(liveTailMock.state.calls.length).toBe(1);
    expect(liveTailMock.state.calls[0]).toMatchObject({ sessionId: "worker-2" });
    expect(container.querySelector('[data-testid="transcript-live"]')).toBeNull();
  });

  it("routes the terminal affordance to the resumed embedded TUI", async () => {
    await renderDrilldown();
    await waitFor(() => expect(container.textContent).toContain("Scout worker"));

    const open = container.querySelector('[aria-label="Open Scout worker live in the terminal"]');
    await click(open);
    await waitFor(() => expect(navMock.search).toContain("resume=worker-2"));
  });

  it("links opencode-worktree children to OpenChamber and leaves ordinary cwd alone", async () => {
    await renderDrilldown();
    await waitFor(() => expect(container.textContent).toContain("Scout worker"));

    const oc = [...container.querySelectorAll("a")].find((a) => a.textContent === "OC");
    expect(oc).not.toBeNull();
    expect(oc?.getAttribute("href")).toBe("http://127.0.0.1:3000");
    // The ordinary-cwd worker row has no OC link (only its terminal affordance).
    expect([...container.querySelectorAll("a")].filter((a) => a.textContent === "OC").length).toBe(1);
  });

  it("expands a cron job into runs; session runs open in the terminal, script rows do not", async () => {
    await renderDrilldown();
    await waitFor(() => expect(container.textContent).toContain("nightly-review"));

    await click([...container.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("nightly-review")));
    await waitFor(() => expect(apiMocks.getCronJobRuns).toHaveBeenCalledWith(
      "nightly-review", 5, undefined,
    ));
    await waitFor(() => expect(container.textContent).toContain("review done"));
    expect(container.textContent).toContain("Script-only run");
    expect(container.textContent).toContain("script");

    const runOpen = container.querySelector(
      '[aria-label^="Open run cron_nightly-review"]');
    await click(runOpen);
    await waitFor(() =>
      expect(navMock.search).toContain("resume=cron_nightly-review_20260101_090000"),
    );
  });

  it("stays honest before the thread exists: no session-scoped fetches", async () => {
    await renderDrilldown({ ...THREAD, exists: false, session_id: null, live_session_id: null });
    await waitFor(() => expect(container.textContent).toContain("Cron"));
    expect(container.textContent).toContain("once the thread exists");
    expect(apiMocks.getSessionLineage).not.toHaveBeenCalled();
    expect(apiMocks.getDelegations).not.toHaveBeenCalled();
    expect(apiMocks.getCronJobs).toHaveBeenCalled();
  });

  it("rides the page's refresh token (sessions.changed refetch)", async () => {
    await renderDrilldown(THREAD, 0);
    await waitFor(() => expect(apiMocks.getSessionLineage).toHaveBeenCalledTimes(1));

    const { ThreadDrilldown } = await import("@/components/ThreadDrilldown");
    await act(async () =>
      root.render(
        <MemoryRouter initialEntries={["/"]}>
          <Routes>
            <Route
              path="/"
              element={<ThreadDrilldown thread={THREAD} profile={undefined} refreshToken={1} />}
            />
            <Route path="/chat" element={<ChatProbe />} />
          </Routes>
        </MemoryRouter>,
      ),
    );
    await waitFor(() => expect(apiMocks.getSessionLineage).toHaveBeenCalledTimes(2));
  });

  it("surfaces section errors as copy, never a broken panel", async () => {
    apiMocks.getSessionLineage.mockRejectedValue(new Error("store busy"));
    await renderDrilldown();
    await waitFor(() => expect(container.textContent).toContain("store busy"));
    // The sibling sections still render their data.
    await waitFor(() => expect(container.textContent).toContain("ship the drill-down"));
  });
});

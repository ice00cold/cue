// @vitest-environment jsdom
/**
 * ThreadInteractions — the M4 (R4) band: decision cards, the goal card, and
 * approval cards. The fetchers and the approval socket are the seams; the
 * assertions pin behaviour: option indexes resolve through the shared
 * dispatcher, results + honest delivery notices render inline, resolved forks
 * leave the band, the goal steers, approval requests answer on the
 * server-request flow, and `sessions.changed` (refreshToken) refetches.
 */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  getDecisions: vi.fn(),
  resolveDecision: vi.fn(),
  withdrawDecision: vi.fn(),
  getGoal: vi.fn(),
  postGoalAction: vi.fn(),
}));

vi.mock("@/lib/thread-interactions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/thread-interactions")>();
  return {
    ...actual,
    getDecisions: api.getDecisions,
    resolveDecision: api.resolveDecision,
    withdrawDecision: api.withdrawDecision,
    getGoal: api.getGoal,
    postGoalAction: api.postGoalAction,
  };
});

// The approvals socket seam: the test drives the handler the hook registered.
const approvalsSocket = vi.hoisted(() => ({
  requestHandler: null as ((request: unknown) => boolean | void) | null,
  cancelHandler: null as ((event: unknown) => void) | null,
}));

vi.mock("@/lib/gatewayClient", () => ({
  GatewayClient: class {
    onRequest(handler: (request: unknown) => boolean | void) {
      approvalsSocket.requestHandler = handler;
      return () => {};
    }
    on(name: string, handler: (event: unknown) => void) {
      if (name === "request.cancel") approvalsSocket.cancelHandler = handler;
      return () => {};
    }
    onState(handler: (state: string) => void) {
      handler("open");
      return () => {};
    }
    connect = vi.fn(async () => undefined);
    close = vi.fn();
  },
}));

let container: HTMLDivElement;
let root: Root;
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

async function waitFor(cond: () => void, timeoutMs = 5000) {
  const start = Date.now();
  for (;;) {
    try {
      cond();
      return;
    } catch {
      /* not yet */
    }
    if (Date.now() - start > timeoutMs) throw new Error("waitFor: condition never became true");
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
  }
}

function click(el: Element | null) {
  if (!el) throw new Error("element not rendered");
  el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
}

const DECISION = {
  id: "d1",
  question: "Which database for the job queue?",
  context: "Backs the cron jobs refactor.",
  options: [
    { index: 1, label: "Postgres", impact: "known ops" },
    { index: 2, label: "SQLite", impact: "zero ops" },
  ],
  recommendation: "Postgres",
  impact: "blocks the refactor",
  status: "pending",
  created_at: "2026-10-13T00:00:00",
};

const NO_GOAL = {
  profile: "default",
  session_id: null,
  exists: false,
  status_line: "No active goal. Set one with /goal <text>.",
  state: null,
};

async function renderBand(refreshToken = 0, profile?: string) {
  const [{ ThreadInteractions }] = await Promise.all([import("@/components/ThreadInteractions")]);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    root.render(<ThreadInteractions profile={profile} refreshToken={refreshToken} />);
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  approvalsSocket.requestHandler = null;
  approvalsSocket.cancelHandler = null;
  api.getDecisions.mockResolvedValue({ profile: "default", pending: [], pending_count: 0 });
  api.getGoal.mockResolvedValue(structuredClone(NO_GOAL));
});

afterEach(async () => {
  if (root) await act(async () => root.unmount());
  container?.remove();
});

describe("ThreadInteractions decision cards", () => {
  it("renders pending forks with options and resolves by option index", async () => {
    api.getDecisions.mockResolvedValue({
      profile: "default",
      pending: [structuredClone(DECISION)],
      pending_count: 1,
    });
    api.resolveDecision.mockResolvedValue({
      profile: "default",
      output: "✓ [d1] resolved → SQLite (cheaper to run)\nResuming the blocked work in the main thread.",
      delivered: "queued",
    });
    await renderBand();

    await waitFor(() => expect(container.textContent).toContain("Which database"));
    expect(container.textContent).toContain("Postgres — known ops");

    // Type a rationale, then click option 2.
    const rationale = container.querySelector<HTMLInputElement>('[aria-label="Rationale for d1"]');
    await act(async () => {
      rationale?.focus();
      // React-controlled input: set through the native setter + input event.
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      setter.call(rationale, "cheaper to run");
      rationale?.dispatchEvent(new Event("input", { bubbles: true }));
    });
    click([...container.querySelectorAll("button")].find((b) => b.textContent === "2") ?? null);

    await waitFor(() => expect(api.resolveDecision).toHaveBeenCalled());
    expect(api.resolveDecision).toHaveBeenCalledWith(
      "d1",
      { choice: "2", rationale: "cheaper to run" },
      "",
    );
    // The settled fork collapses into its aftermath note: the dispatcher's
    // output line plus the honest delivery report — and the interactive card
    // (question, options) is gone.
    await waitFor(() => expect(container.textContent).toContain("resolved → SQLite"));
    expect(container.textContent).toContain("Queued — the agent resumes in this thread.");
    await waitFor(() => expect(container.textContent).not.toContain("Which database"));
  });

  it("free-form answers pass verbatim through the dispatcher", async () => {
    api.getDecisions.mockResolvedValue({
      profile: "default",
      pending: [structuredClone(DECISION)],
      pending_count: 1,
    });
    api.resolveDecision.mockResolvedValue({
      profile: "default",
      output: "✓ [d1] resolved → do both in stages",
      delivered: "unavailable",
      delivery_note: "The main thread does not exist yet.",
    });
    await renderBand();
    await waitFor(() => expect(container.textContent).toContain("Which database"));

    const custom = container.querySelector<HTMLInputElement>('[aria-label="Free-form answer for d1"]');
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      setter.call(custom, "do both in stages");
      custom?.dispatchEvent(new Event("input", { bubbles: true }));
    });
    click([...container.querySelectorAll("button")].find((b) => b.textContent === "Answer") ?? null);

    await waitFor(() =>
      expect(api.resolveDecision).toHaveBeenCalledWith("d1", { choice: "do both in stages", rationale: "" }, ""),
    );
    await waitFor(() => expect(container.textContent).toContain("does not exist yet"));
  });

  it("withdraw closes the fork through the shared dispatcher", async () => {
    api.getDecisions.mockResolvedValue({
      profile: "default",
      pending: [structuredClone(DECISION)],
      pending_count: 1,
    });
    api.withdrawDecision.mockResolvedValue({
      profile: "default",
      output: "✓ [d1] withdrawn",
      delivered: "none",
    });
    await renderBand();
    await waitFor(() => expect(container.textContent).toContain("Which database"));

    click([...container.querySelectorAll("button")].find((b) => b.textContent === "Withdraw") ?? null);
    await waitFor(() => expect(api.withdrawDecision).toHaveBeenCalledWith("d1", { reason: "" }, ""));
    await waitFor(() => expect(container.textContent).not.toContain("Which database"));
  });

  it("a dispatcher refusal shows the error inline, the fork stays", async () => {
    api.getDecisions.mockResolvedValue({
      profile: "default",
      pending: [structuredClone(DECISION)],
      pending_count: 1,
    });
    api.resolveDecision.mockRejectedValue(new Error("Decision d1 is already resolved."));
    await renderBand();
    await waitFor(() => expect(container.textContent).toContain("Which database"));

    click([...container.querySelectorAll("button")].find((b) => b.textContent === "1") ?? null);
    await waitFor(() => expect(container.textContent).toContain("already resolved"));
    expect(container.textContent).toContain("Which database");
  });
});

describe("ThreadInteractions goal card", () => {
  const GOAL = {
    profile: "default",
    session_id: "sess-main",
    exists: true,
    status_line: "⏸ Goal (paused, 3/12 turns): ship the M4 cards",
    state: {
      goal: "ship the M4 cards",
      status: "paused",
      turns_used: 3,
      max_turns: 12,
      created_at: 1,
      last_verdict: null,
      paused_reason: "user-paused",
      waiting_reason: null,
    },
  };

  it("renders the thread's goal with control verbs; resume queues the continuation", async () => {
    api.getGoal.mockResolvedValue(structuredClone(GOAL));
    api.postGoalAction.mockResolvedValue({
      ...structuredClone(GOAL),
      state: { ...GOAL.state, status: "active", turns_used: 0, paused_reason: null },
      output: "▶ Goal resumed: ship the M4 cards",
      delivered: "queued",
    });
    await renderBand();

    await waitFor(() => expect(container.textContent).toContain("ship the M4 cards"));
    expect(container.textContent).toContain("3/12 turns");

    click([...container.querySelectorAll("button")].find((b) => b.textContent === "Resume") ?? null);
    await waitFor(() => expect(api.postGoalAction).toHaveBeenCalledWith("resume", ""));
    await waitFor(() => expect(container.textContent).toContain("Queued — the agent resumes"));
    expect(container.textContent).toContain("0/12 turns");
  });

  it("no goal renders nothing (the composer owns setting one)", async () => {
    api.getGoal.mockResolvedValue(structuredClone(NO_GOAL));
    await renderBand();
    await waitFor(() => expect(api.getGoal).toHaveBeenCalled());
    expect(container.querySelector('[data-testid="goal-card"]')).toBeNull();
  });
});

describe("ThreadInteractions approval cards", () => {
  function approvalRequest(id = "srq-1", params: Record<string, unknown> = {}) {
    return {
      id,
      method: "approval",
      params: { session_id: "sess-1", command: "rm -rf /tmp/build", description: "clean the build dir", ...params },
      respond: vi.fn(),
      fail: vi.fn(),
    };
  }

  it("renders an approval request and answers with its choice", async () => {
    await renderBand();
    await waitFor(() => expect(approvalsSocket.requestHandler).not.toBeNull());
    const request = approvalRequest();
    await act(async () => {
      approvalsSocket.requestHandler?.(request);
    });

    await waitFor(() => expect(container.textContent).toContain("clean the build dir"));
    expect(container.querySelector("pre")?.textContent).toContain("rm -rf /tmp/build");

    click(
      [...container.querySelectorAll("button")].find((b) => b.textContent === "Approve for session") ?? null,
    );
    expect(request.respond).toHaveBeenCalledWith({ choice: "session" });
    await waitFor(() => expect(container.textContent).not.toContain("clean the build dir"));
  });

  it("deny answers deny, and the choice ladder drops always when forbidden", async () => {
    await renderBand();
    const request = approvalRequest("srq-2", { allow_permanent: false, choices: undefined });
    await act(async () => {
      approvalsSocket.requestHandler?.(request);
    });
    await waitFor(() => expect(container.textContent).toContain("clean the build dir"));
    expect([...container.querySelectorAll("button")].map((b) => b.textContent)).toEqual([
      "Approve once",
      "Approve for session",
      "Deny",
    ]);
    click([...container.querySelectorAll("button")].find((b) => b.textContent === "Deny") ?? null);
    expect(request.respond).toHaveBeenCalledWith({ choice: "deny" });
  });

  it("request.cancel withdraws the card", async () => {
    await renderBand();
    const request = approvalRequest("srq-3");
    await act(async () => {
      approvalsSocket.requestHandler?.(request);
    });
    await waitFor(() => expect(container.textContent).toContain("clean the build dir"));
    await act(async () => {
      approvalsSocket.cancelHandler?.({ type: "request.cancel", payload: { id: "srq-3", method: "approval", reason: "timeout" } });
    });
    await waitFor(() => expect(container.textContent).not.toContain("clean the build dir"));
  });

  it("non-approval requests are left for other handlers", async () => {
    await renderBand();
    await waitFor(() => expect(approvalsSocket.requestHandler).not.toBeNull());
    const clarify = { id: "srq-9", method: "clarify", params: {}, respond: vi.fn() };
    let accepted: boolean | void = true;
    await act(async () => {
      accepted = approvalsSocket.requestHandler?.(clarify);
    });
    expect(accepted).toBe(false);
    expect(clarify.respond).not.toHaveBeenCalled();
  });
});

describe("ThreadInteractions refresh signal", () => {
  it("a later sessions.changed signal refetches decisions and the goal", async () => {
    await renderBand(0);
    await waitFor(() => expect(api.getDecisions).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.getGoal).toHaveBeenCalledTimes(1));

    // remount with a bumped token (the page passes its refreshToken through)
    await act(async () => root.unmount());
    await renderBand(1);
    await waitFor(() => expect(api.getDecisions).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(api.getGoal).toHaveBeenCalledTimes(2));
  });
});

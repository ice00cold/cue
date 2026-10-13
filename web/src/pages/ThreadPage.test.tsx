// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLiveChatChannel } from "@/lib/chat-channel-store";

const apiMocks = vi.hoisted(() => ({
  getMainThread: vi.fn(),
  getSessionMessagesPage: vi.fn(),
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  getActiveProfile: vi.fn(async () => ({ current: "default", active: "default" })),
}));

// Passthrough stubs so assertions see exactly what the data layer produced.
const transcriptMock = vi.hoisted(() => ({ rendered: [] as string[] }));
const feedMocks = vi.hoisted(() => {
  const handlers = new Map<string, (event: unknown) => void>();
  return {
    handlers,
    connect: vi.fn(async () => undefined),
    on: vi.fn((event: string, handler: (event: unknown) => void) => {
      handlers.set(event, handler);
      return () => handlers.delete(event);
    }),
    onState: vi.fn((handler: (state: string) => void) => {
      handler("open");
      return () => undefined;
    }),
    onClose: vi.fn((handler: (code: number | undefined) => void) => {
      handlers.set("__close__", handler as (event: unknown) => void);
      return () => handlers.delete("__close__");
    }),
    close: vi.fn(),
    lastCloseCode: null as number | null | undefined,
  };
});

vi.mock("@/lib/api", () => ({
  api: apiMocks,
  setManagementProfile: vi.fn(),
  getManagementProfile: vi.fn(() => ""),
}));
vi.mock("@/lib/eventsFeedClient", () => ({
  EventsFeedClient: class {
    connect = feedMocks.connect;
    on = feedMocks.on;
    onState = feedMocks.onState;
    onClose = feedMocks.onClose;
    close = feedMocks.close;
    lastCloseCode = feedMocks.lastCloseCode;
  },
}));
vi.mock("@/lib/clipboard", () => ({
  copyTextToClipboard: vi.fn(async () => true),
}));
vi.mock("@/components/SessionMessageList", () => ({
  MessageList: ({ messages }: { messages: Array<{ content: string | null }> }) => {
    transcriptMock.rendered = messages.map((m) => m.content ?? "");
    return <div data-testid="transcript">{transcriptMock.rendered.join("|")}</div>;
  },
}));

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

function click(el: Element | null) {
  if (!el) throw new Error("element not rendered");
  el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
}

const MAIN_THREAD = {
  profile: "default",
  session_key: "agent:main:main-thread",
  session_id: "sess-root",
  live_session_id: "sess-tip",
  exists: true,
  is_active: false,
  session: {
    id: "sess-tip",
    source: "cli",
    model: "test/model",
    title: null,
    started_at: 1,
    ended_at: null,
    last_active: Date.now() / 1000 - 30,
    is_active: false,
    message_count: 214,
    tool_call_count: 3,
    input_tokens: 90_000,
    output_tokens: 4_300,
    preview: null,
    estimated_cost_usd: 1.25,
  },
};

function messagesPage(contents: string[], ids: number[]) {
  return {
    session_id: "sess-tip",
    messages: contents.map((content, i) => ({ id: ids[i], role: "user" as const, content })),
    pagination: { limit: 200, offset: 0, order: "latest" as const, returned: contents.length },
  };
}

async function renderThreadPage() {
  const [{ default: ThreadPage }, { ProfileProvider }] = await Promise.all([
    import("./ThreadPage"),
    import("@/contexts/ProfileProvider"),
  ]);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <MemoryRouter>
        <ProfileProvider>
          <ThreadPage />
        </ProfileProvider>
      </MemoryRouter>,
    ),
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  feedMocks.handlers.clear();
  transcriptMock.rendered = [];
  // The thread surface rides the embedded chat's /api/events channel; seed
  // the store the way ChatPage does when its PTY mounts.
  setLiveChatChannel("thread-test-channel");
});

afterEach(async () => {
  setLiveChatChannel(null);
  if (root) await act(async () => root.unmount());
  container?.remove();
});

describe("ThreadPage", () => {
  it("renders the resolved main thread: header status, cards, transcript head", async () => {
    apiMocks.getMainThread.mockResolvedValue(structuredClone(MAIN_THREAD));
    apiMocks.getSessionMessagesPage.mockImplementation(
      async (_id: string, opts: { offset?: number } = {}) =>
        messagesPage(
          opts.offset ? ["older turn", "mid turn"] : ["mid turn", "newest turn"],
          opts.offset ? [1, 2] : [2, 3],
        ),
    );
    await renderThreadPage();

    await waitFor(() => expect(container.textContent).toContain("newest turn"));
    expect(container.textContent).toContain("Main thread");
    expect(container.textContent).toContain("Idle");
    // Session chip carries the live tip's entropy, and the copy affordance.
    expect(container.querySelector('[aria-label="Copy main thread session id"]')).not.toBeNull();
    // Cards read the resolver's summary row.
    expect(container.textContent).toContain("214");
    expect(container.textContent).toContain("94k");
    expect(container.textContent).toContain("$1.25");
    // The transcript pages over the existing messages endpoint (newest page).
    expect(apiMocks.getSessionMessagesPage).toHaveBeenCalledWith(
      "sess-tip",
      { offset: 0, limit: 200 },
      "",
    );
  });

  it("shows the honest empty state until the thread's first contact", async () => {
    apiMocks.getMainThread.mockResolvedValue({
      profile: "default",
      session_key: "agent:main:main-thread",
      session_id: null,
      live_session_id: null,
      exists: false,
      is_active: false,
      session: null,
    });
    await renderThreadPage();

    await waitFor(() => expect(container.textContent).toContain("No thread yet"));
    expect(container.textContent).toContain("first message");
    expect(apiMocks.getSessionMessagesPage).not.toHaveBeenCalled();
  });

  it("pages backwards through the transcript on Load older", async () => {
    apiMocks.getMainThread.mockResolvedValue(structuredClone(MAIN_THREAD));
    const fullPage = Array.from({ length: 200 }, (_, i) => `m${i + 1}`);
    const fullIds = Array.from({ length: 200 }, (_, i) => i + 1);
    apiMocks.getSessionMessagesPage.mockImplementation(
      async (_id: string, opts: { offset?: number } = {}) =>
        opts.offset
          ? messagesPage(["ancient turn"], [0])
          : messagesPage(fullPage, fullIds),
    );
    await renderThreadPage();
    await waitFor(() => expect(container.textContent).toContain("m200"));

    const loadOlder = [...container.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Load older"),
    );
    click(loadOlder ?? null);
    await waitFor(() => expect(container.textContent).toContain("ancient turn"));
    expect(apiMocks.getSessionMessagesPage).toHaveBeenCalledWith(
      "sess-tip",
      { offset: 200, limit: 200 },
      "",
    );
    // Older content renders before the newer page.
    expect(transcriptMock.rendered[0]).toBe("ancient turn");
    expect(transcriptMock.rendered.at(-1)).toBe("m200");
  });

  it("refetches the resolver when sessions.changed lands on the PTY channel", async () => {
    apiMocks.getMainThread.mockResolvedValue(structuredClone(MAIN_THREAD));
    apiMocks.getSessionMessagesPage.mockResolvedValue(messagesPage(["turn"], [1]));
    await renderThreadPage();
    await waitFor(() => expect(apiMocks.getMainThread).toHaveBeenCalledTimes(1));

    const emit = feedMocks.handlers.get("sessions.changed");
    expect(emit).toBeDefined();
    await act(async () => {
      emit?.({ type: "sessions.changed", payload: {} });
    });
    await waitFor(() => expect(apiMocks.getMainThread).toHaveBeenCalledTimes(2));
  });
});

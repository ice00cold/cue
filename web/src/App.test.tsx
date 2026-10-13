// @vitest-environment jsdom
import { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import { I18nProvider } from "@/i18n";
import { SystemActionsProvider } from "@/contexts/SystemActions";

// The behaviour under test is App-level routing and nav, not page internals:
// stub the heavy lazy surfaces the router can land on.
vi.mock("@/pages/ThreadPage", () => ({
  default: () => <div data-testid="thread-home" />,
}));
vi.mock("@/pages/ChatPage", () => ({
  default: () => <div data-testid="chat-host" />,
}));

// No plugins in these tests: the manifest feed is what `buildRoutes` merges
// built-in routes with, and an empty list keeps the route table deterministic.
vi.mock("@/plugins", () => ({
  PluginPage: () => null,
  PluginSlot: () => null,
  usePlugins: () => ({ manifests: [], loading: false }),
}));

const apiMocks = vi.hoisted(() => ({
  getConfig: vi.fn(async () => ({})),
  getStatus: vi.fn(async () => ({
    gateway_running: true,
    gateway_state: "running",
    active_sessions: 0,
  })),
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  getActiveProfile: vi.fn(async () => ({ current: "default", active: "default" })),
  // `provider: "loopback"` hides the auth widget (loopback dashboards are
  // ungated), so the sidebar renders its quiet default.
  getAuthMe: vi.fn(async () => ({ provider: "loopback" })),
  // A decided + managed consent keeps the banner out of the tree.
  getSharedMetricsConsent: vi.fn(async () => ({
    decided: true,
    enabled: false,
    managed: true,
  })),
}));

vi.mock("@/lib/api", () => ({
  api: apiMocks,
  // ProfileProvider mirrors its selection into the api module.
  setManagementProfile: vi.fn(),
  getManagementProfile: vi.fn(() => ""),
}));

vi.mock("@/themes", () => ({
  useTheme: () => ({
    theme: {},
    themeName: "default",
    availableThemes: [],
    setTheme: () => {},
    fontId: "default",
    fontChoices: [],
    setFont: () => {},
  }),
  BUILTIN_THEMES: {},
  THEME_DEFAULT_FONT_ID: "default",
}));

let container: HTMLDivElement;
let root: Root;
let currentPathname = "";

/** Records the router location after each commit so redirect assertions can
 *  observe where the app actually lands, not just what renders. */
function LocationProbe() {
  const { pathname } = useLocation();
  useEffect(() => {
    currentPathname = pathname;
  }, [pathname]);
  return null;
}

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

async function renderAt(initialPath: string) {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <MemoryRouter initialEntries={[initialPath]}>
        <I18nProvider>
          <SystemActionsProvider>
            <App />
            <LocationProbe />
          </SystemActionsProvider>
        </I18nProvider>
      </MemoryRouter>,
    ),
  );
}

const localStorageMock = (() => {
  let store: Record<string, string> = {};
  return {
    getItem: (key: string) => store[key] ?? null,
    setItem: (key: string, value: string) => {
      store[key] = String(value);
    },
    removeItem: (key: string) => {
      delete store[key];
    },
    clear: () => {
      store = {};
    },
  };
})();

beforeEach(() => {
  currentPathname = "";
  vi.stubGlobal("localStorage", localStorageMock);
  vi.stubGlobal("sessionStorage", localStorageMock);
  vi.stubGlobal(
    "matchMedia",
    () =>
      ({
        addEventListener() {},
        matches: false,
        media: "",
        removeEventListener() {},
      }) as unknown as MediaQueryList,
  );
  vi.stubGlobal(
    "ResizeObserver",
    class {
      disconnect() {}
      observe() {}
      unobserve() {}
    },
  );
});

afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
  vi.unstubAllGlobals();
});

describe("App single-thread routing", () => {
  it("redirects /sessions to the thread home", async () => {
    await renderAt("/sessions");

    await vi.waitFor(() => expect(currentPathname).toBe("/"));
    expect(
      container.querySelector('[data-testid="thread-home"]'),
    ).not.toBeNull();
  });

  it("redirects /sessions deep links of any depth to the thread home", async () => {
    await renderAt("/sessions/0199-mainsession");

    await vi.waitFor(() => expect(currentPathname).toBe("/"));
    expect(
      container.querySelector('[data-testid="thread-home"]'),
    ).not.toBeNull();
  });

  it("exposes no Sessions browsing entry in the sidebar nav", async () => {
    await renderAt("/");
    await vi.waitFor(() =>
      expect(container.querySelector('[data-testid="thread-home"]')).not.toBeNull(),
    );

    expect(
      document.querySelectorAll('a[href="/sessions"], a[href="/sessions/"]'),
    ).toHaveLength(0);

    // The main thread is the primary surface: the Thread entry is present.
    const threadLinks = Array.from(document.querySelectorAll<HTMLAnchorElement>('a[href="/"]')).filter(
      (a) => a.textContent?.trim() === "Thread",
    );
    expect(threadLinks.length).toBeGreaterThan(0);
  });
});

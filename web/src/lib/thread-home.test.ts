import { describe, expect, it } from "vitest";
import {
  THREAD_TRANSCRIPT_PAGE_SIZE,
  compactCost,
  compactCount,
  hasOlderThreadPage,
  mergeThreadPages,
  threadPageOffset,
  threadSessionChip,
  threadStatus,
} from "./thread-home";
import type { MainThreadResponse, SessionMessage } from "@/lib/api";

function msg(id: number, content: string): SessionMessage {
  return { id, role: "user", content };
}

function thread(partial: Partial<MainThreadResponse>): MainThreadResponse {
  return {
    profile: "default",
    session_key: "agent:main:main-thread",
    session_id: null,
    live_session_id: null,
    exists: false,
    is_active: false,
    session: null,
    ...partial,
  };
}

describe("threadPageOffset", () => {
  it("addresses pages back from the newest (page 0 = newest)", () => {
    expect(threadPageOffset(0)).toBe(0);
    expect(threadPageOffset(1)).toBe(THREAD_TRANSCRIPT_PAGE_SIZE);
    expect(threadPageOffset(3)).toBe(3 * THREAD_TRANSCRIPT_PAGE_SIZE);
  });

  it("never yields a negative offset", () => {
    expect(threadPageOffset(-1)).toBe(0);
  });
});

describe("mergeThreadPages", () => {
  it("concatenates pages oldest-first", () => {
    const merged = mergeThreadPages([
      [msg(1, "old a"), msg(2, "old b")],
      [msg(3, "new a"), msg(4, "new b")],
    ]);
    expect(merged.map((m) => m.content)).toEqual([
      "old a", "old b", "new a", "new b",
    ]);
  });

  it("drops duplicate ids across page boundaries (refresh drift)", () => {
    const merged = mergeThreadPages([
      [msg(1, "old"), msg(2, "boundary")],
      [msg(2, "boundary"), msg(3, "new")],
    ]);
    expect(merged.map((m) => m.id)).toEqual([1, 2, 3]);
  });

  it("keeps id-less rows (never silently drops unknown payloads)", () => {
    const merged = mergeThreadPages([[{ role: "user", content: "legacy" }]]);
    expect(merged).toHaveLength(1);
  });
});

describe("hasOlderThreadPage", () => {
  it("offers more only while pages come back full", () => {
    const full = Array.from({ length: THREAD_TRANSCRIPT_PAGE_SIZE }, (_, i) => msg(i, "x"));
    expect(hasOlderThreadPage(full)).toBe(true);
    expect(hasOlderThreadPage(full.slice(0, -1))).toBe(false);
    expect(hasOlderThreadPage([])).toBe(false);
  });

  it("assumes more may exist when rows carry no ids", () => {
    expect(hasOlderThreadPage([{ role: "user", content: "legacy" }])).toBe(true);
  });
});

describe("threadStatus", () => {
  it("derives the badge from resolution + liveness", () => {
    expect(threadStatus(null)).toBe("new");
    expect(threadStatus(thread({ exists: false }))).toBe("new");
    expect(
      threadStatus(
        thread({ exists: true, is_active: true, session: {} as MainThreadResponse["session"] }),
      ),
    ).toBe("active");
    expect(
      threadStatus(
        thread({ exists: true, is_active: false, session: {} as MainThreadResponse["session"] }),
      ),
    ).toBe("idle");
  });
});

describe("chip and number formatting", () => {
  it("shortens the session id to its entropy tail", () => {
    expect(threadSessionChip(null)).toBe("—");
    expect(threadSessionChip("20261012-abcd-ef01-2345-678901234567")).toBe("…678901234567");
    expect(threadSessionChip("short")).toBe("short");
  });

  it("compacts counts and costs for the cards", () => {
    expect(compactCount(0)).toBe("0");
    expect(compactCount(942)).toBe("942");
    expect(compactCount(12_843)).toBe("13k");
    expect(compactCount(2_400_000)).toBe("2.4M");
    expect(compactCost(0)).toBe("$0.00");
    expect(compactCost(0.004)).toBe("<$0.01");
    expect(compactCost(1.5)).toBe("$1.50");
    expect(compactCost(2140)).toBe("$2.1k");
  });
});

// live-tail (Cue Phase 6 M3 / R3): the pure half of the live session view —
// SSE frame parsing (chunk boundaries, comments, multi-line data), the
// frame → typed-event mapping (unknown events and broken payloads must not
// kill the stream), the keyset anchor helper, and the pane-merge contract
// that folds streamed rows into a paged window without duplicates.
import { describe, expect, it } from "vitest";
import type { SessionMessage } from "@/lib/api";
import {
  SseFrameParser,
  decodeLiveTailEvent,
  newestRowId,
  parseSseFrame,
} from "@/lib/live-tail";
import { mergeThreadPages } from "@/lib/thread-home";

function msg(id: number, content: string): SessionMessage {
  return { id, role: "assistant", content };
}

describe("parseSseFrame", () => {
  it("parses an event + data frame", () => {
    const frame = parseSseFrame('event: messages\ndata: {"a":1}');
    expect(frame).toEqual({ event: "messages", data: '{"a":1}' });
  });

  it("defaults the event to message and joins multi-line data", () => {
    const frame = parseSseFrame('data: {"a":\ndata: 1}');
    expect(frame).toEqual({ event: "message", data: '{"a":\n1}' });
  });

  it("drops comment-only frames (the server's keepalive heartbeats)", () => {
    expect(parseSseFrame(": keepalive")).toBeNull();
    expect(parseSseFrame("")).toBeNull();
  });
});

describe("SseFrameParser", () => {
  it("reassembles frames split across chunk boundaries", () => {
    const parser = new SseFrameParser();
    expect(parser.push('event: messages\nda')).toEqual([]);
    expect(parser.push('ta: {"x":1}\n\n')).toEqual([
      { event: "messages", data: '{"x":1}' },
    ]);
    // The buffer is consumed: the next frame starts clean.
    expect(parser.push("event: hello\ndata: {}\n\n")).toEqual([
      { event: "hello", data: "{}" },
    ]);
  });

  it("returns several frames from one chunk and skips heartbeats", () => {
    const parser = new SseFrameParser();
    const frames = parser.push(": keepalive\n\nevent: a\ndata: 1\n\nevent: b\ndata: 2\n\n");
    expect(frames).toEqual([
      { event: "a", data: "1" },
      { event: "b", data: "2" },
    ]);
  });

  it("keeps a trailing partial frame for the next chunk", () => {
    const parser = new SseFrameParser();
    expect(parser.push("event: x\ndata: 9\n")).toEqual([]);
    expect(parser.push("\nevent: y\ndata: 8\n\n")).toEqual([
      { event: "x", data: "9" },
      { event: "y", data: "8" },
    ]);
  });
});

describe("decodeLiveTailEvent", () => {
  it("types each frame event", () => {
    expect(
      decodeLiveTailEvent({ event: "hello", data: '{"activity":{"is_active":true}}' }),
    ).toMatchObject({ type: "hello", activity: { is_active: true } });
    expect(
      decodeLiveTailEvent({ event: "rotated", data: '{"session_id":"b","previous":"a"}' }),
    ).toMatchObject({ type: "rotated", session_id: "b", previous: "a" });
    expect(
      decodeLiveTailEvent({
        event: "messages",
        data: '{"session_id":"s","messages":[{"id":3}],"last_row_id":3}',
      }),
    ).toMatchObject({ type: "messages", messages: [{ id: 3 }], last_row_id: 3 });
  });

  it("drops malformed payloads and unknown events instead of throwing", () => {
    expect(decodeLiveTailEvent({ event: "messages", data: "not json" })).toBeNull();
    expect(decodeLiveTailEvent({ event: "messages", data: '{"messages":"nope"}' })).toBeNull();
    expect(decodeLiveTailEvent({ event: "mystery", data: "{}" })).toBeNull();
  });
});

describe("newestRowId", () => {
  it("is the highest durable row id (the keyset anchor)", () => {
    expect(newestRowId([msg(4, "a"), msg(9, "b"), msg(7, "c")])).toBe(9);
  });

  it("is 0 for nothing loaded or rows without ids", () => {
    expect(newestRowId([])).toBe(0);
    expect(newestRowId([{ role: "user", content: "legacy" }])).toBe(0);
  });
});

describe("pane merge with streamed rows", () => {
  it("appends live rows after the window and folds refresh overlap to one copy", () => {
    const window = [msg(1, "one"), msg(2, "two")];
    const streamed = [msg(3, "three")];
    expect(mergeThreadPages([window, streamed]).map((m) => m.content)).toEqual([
      "one", "two", "three",
    ]);

    // The next window refresh also delivers row 3 (it is persisted now): the
    // pane must not render it twice.
    const refreshed = [msg(1, "one"), msg(2, "two"), msg(3, "three")];
    expect(mergeThreadPages([refreshed, streamed]).map((m) => m.content)).toEqual([
      "one", "two", "three",
    ]);
  });
});

/**
 * Live-tail wire types + SSE parsing (Cue Phase 6 M3 / R3) — the pure half of
 * the live session view: the `/api/sessions/{id}/live` frame contract,
 * an incremental SSE parser, and the keyset-anchor helpers. Effects and
 * sockets live in the client/hook; everything here is data-in/data-out for
 * vitest.
 */

import type { SessionMessage } from "@/lib/api";

/** Activity stamp carried by `hello` and re-sent as `activity` when it moves. */
export interface LiveTailActivity {
  last_activity_at: number;
  last_activity_description: string | null;
  ended_at: number | null;
  end_reason: string | null;
  message_count: number | null;
  tool_call_count: number | null;
  is_active: boolean;
}

export interface LiveTailHelloEvent {
  type: "hello";
  session_id: string;
  live_session_id: string;
  profile: string;
  after_row_id: number;
  activity: LiveTailActivity | null;
}

export interface LiveTailMessagesEvent {
  type: "messages";
  session_id: string;
  messages: SessionMessage[];
  last_row_id: number;
}

export interface LiveTailRotatedEvent {
  type: "rotated";
  session_id: string;
  previous: string;
}

export type LiveTailActivityEvent = LiveTailActivity & {
  type: "activity";
  session_id: string;
};

export interface LiveTailDeletedEvent {
  type: "deleted";
  session_id: string;
}

export interface LiveTailErrorEvent {
  type: "error";
  session_id: string;
  error: string;
  message?: string;
}

export type LiveTailEvent =
  | LiveTailHelloEvent
  | LiveTailMessagesEvent
  | LiveTailRotatedEvent
  | LiveTailActivityEvent
  | LiveTailDeletedEvent
  | LiveTailErrorEvent;

/** One parsed SSE frame; comment-only frames (heartbeats) parse to null. */
export interface SseFrame {
  event: string;
  data: string;
}

/** Parse the lines of ONE complete SSE frame (between blank-line separators). */
export function parseSseFrame(raw: string): SseFrame | null {
  let event = "message";
  const data: string[] = [];
  for (const line of raw.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      data.push(line.slice("data:".length).trim());
    }
  }
  if (data.length === 0) return null;
  return { event, data: data.join("\n") };
}

/** Incremental SSE parser: feed decoded chunks, receive whole frames. */
export class SseFrameParser {
  private buffer = "";

  push(chunk: string): SseFrame[] {
    this.buffer += chunk;
    const frames: SseFrame[] = [];
    for (;;) {
      const sep = this.buffer.indexOf("\n\n");
      if (sep === -1) return frames;
      const raw = this.buffer.slice(0, sep);
      this.buffer = this.buffer.slice(sep + 2);
      const frame = parseSseFrame(raw);
      if (frame) frames.push(frame);
    }
  }
}

/** Map one frame onto the typed tail contract; unknown events and
 * undecodable payloads are dropped (a bad frame must not kill the stream). */
export function decodeLiveTailEvent(frame: SseFrame): LiveTailEvent | null {
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(frame.data) as Record<string, unknown>;
  } catch {
    return null;
  }
  if (typeof payload !== "object" || payload === null) return null;
  const type = frame.event;
  if (type === "messages") {
    return Array.isArray(payload.messages)
      ? ({ ...payload, type } as unknown as LiveTailMessagesEvent)
      : null;
  }
  if (
    type === "hello" || type === "rotated" || type === "activity" ||
    type === "deleted" || type === "error"
  ) {
    return { ...payload, type } as unknown as LiveTailEvent;
  }
  return null;
}

/** The keyset anchor: newest durable row id in a list (0 when none/id-less). */
export function newestRowId(messages: SessionMessage[]): number {
  let max = 0;
  for (const message of messages) {
    if (typeof message.id === "number" && message.id > max) max = message.id;
  }
  return max;
}

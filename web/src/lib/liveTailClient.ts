/**
 * Streaming fetch client for the live session tail (Cue Phase 6 M3 / R3).
 *
 * `/api/sessions/{id}/live` is consumed with a streaming `fetch` rather than
 * `EventSource`: EventSource can carry neither the session-token header nor
 * the gated-mode cookie the dashboard's REST auth uses, while a fetch with
 * `credentials: "include"` carries both — the same single auth scheme as
 * every other route (web/AGENTS.md). Resolves `"ended"` when the server
 * closes the body (terminal frame or restart), `"aborted"` when the caller's
 * signal fired; network failures reject for the hook's reconnect ladder.
 */

import { HERMES_BASE_PATH } from "@/lib/api";
import {
  SseFrameParser,
  decodeLiveTailEvent,
  type LiveTailEvent,
} from "@/lib/live-tail";

export type LiveTailStreamEnd = "ended" | "aborted";

export class LiveTailHttpError extends Error {
  readonly status: number;

  constructor(status: number, body: string) {
    super(`live tail: HTTP ${status}${body ? `: ${body.slice(0, 200)}` : ""}`);
    this.name = "LiveTailHttpError";
    this.status = status;
  }
}

export interface LiveTailStreamOptions {
  sessionId: string;
  profile?: string;
  /** Keyset anchor — the newest row id the caller already renders. */
  afterRowId?: number;
  signal: AbortSignal;
  onEvent: (event: LiveTailEvent) => void;
}

export async function streamSessionLiveTail(
  opts: LiveTailStreamOptions,
): Promise<LiveTailStreamEnd> {
  const params = new URLSearchParams({ after_row_id: String(opts.afterRowId ?? 0) });
  if (opts.profile) params.set("profile", opts.profile);
  const url =
    `${HERMES_BASE_PATH}/api/sessions/${encodeURIComponent(opts.sessionId)}/live` +
    `?${params.toString()}`;
  const headers = new Headers();
  const token = window.__HERMES_SESSION_TOKEN__;
  if (token) headers.set("X-Hermes-Session-Token", token);
  try {
    const res = await fetch(url, {
      headers,
      credentials: "include",
      signal: opts.signal,
    });
    if (!res.ok) {
      throw new LiveTailHttpError(res.status, await res.text().catch(() => ""));
    }
    if (!res.body) {
      // Streams are universal in supported browsers; missing body means the
      // environment cannot tail at all — surface it instead of hanging.
      throw new Error("live tail: response has no body stream");
    }
    return await pumpFrames(res.body, opts.onEvent);
  } catch (err) {
    if (opts.signal.aborted) return "aborted";
    throw err;
  }
}

async function pumpFrames(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: LiveTailEvent) => void,
): Promise<LiveTailStreamEnd> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseFrameParser();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return "ended";
    for (const frame of parser.push(decoder.decode(value, { stream: true }))) {
      const event = decodeLiveTailEvent(frame);
      if (event) onEvent(event);
    }
  }
}

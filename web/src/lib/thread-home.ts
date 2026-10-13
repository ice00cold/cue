/**
 * Thread-home helpers (Cue Phase 6 M1) — the pure half of the main-thread
 * surface: transcript paging math over the existing messages endpoint, page
 * merging for `sessions.changed`-driven refreshes, and the status/format
 * derivations the header and cards render. Effects and sockets live in the
 * hook/components; everything here is data-in/data-out for vitest.
 */

import type { MainThreadResponse, SessionMessage } from "@/lib/api";

/** Bounded page the transcript reader fetches per step (endpoint caps at 500). */
export const THREAD_TRANSCRIPT_PAGE_SIZE = 200;

/**
 * Offset of page *pageIndex* (0 = newest page). The messages endpoint pages
 * back from the newest (`order=latest`) but returns each page chronologically,
 * so pages concatenate oldest-first.
 */
export function threadPageOffset(pageIndex: number, pageSize = THREAD_TRANSCRIPT_PAGE_SIZE): number {
  return Math.max(pageIndex, 0) * pageSize;
}

/**
 * Concatenate transcript pages (oldest page first) into one chronological
 * list, dropping duplicate row ids. Refreshes refetch every loaded page in one
 * window arithmetic pass, so pages never overlap — the dedupe is the safety
 * net for boundary drift (a message crossing a page edge mid-refresh) and
 * keeps keys stable for React.
 */
export function mergeThreadPages(pages: SessionMessage[][]): SessionMessage[] {
  const seen = new Set<number>();
  const merged: SessionMessage[] = [];
  for (const page of pages) {
    for (const message of page) {
      const key = message.id;
      if (key !== undefined) {
        if (seen.has(key)) continue;
        seen.add(key);
      }
      merged.push(message);
    }
  }
  return merged;
}

/**
 * Whether an older page exists past the last fetched one: a short page means
 * the transcript's start is already loaded. A page missing ids (older
 * backends) always assumes more may exist — never a dead-end "Load older".
 */
export function hasOlderThreadPage(lastPage: SessionMessage[], pageSize = THREAD_TRANSCRIPT_PAGE_SIZE): boolean {
  if (lastPage.length === 0) return false;
  if (lastPage.some((m) => m.id === undefined)) return true;
  return lastPage.length >= pageSize;
}

/** The status badge the header shows for the resolved main thread. */
export function threadStatus(
  thread: MainThreadResponse | null,
): "new" | "active" | "idle" {
  if (!thread || !thread.exists || !thread.session) return "new";
  return thread.is_active ? "active" : "idle";
}

/** Short-form session id chip: keep the entropy, drop the uuid noise. */
export function threadSessionChip(id: string | null): string {
  if (!id) return "—";
  const tail = id.slice(-12);
  return tail.length < id.length ? `…${tail}` : id;
}

/** Compact integer for the cards (12,843 -> "13k"). */
export function compactCount(value: number | null | undefined): string {
  const n = value ?? 0;
  if (n < 1000) return String(n);
  if (n < 10_000) return `${(n / 1000).toFixed(1)}k`;
  if (n < 1_000_000) return `${Math.round(n / 1000)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

/** Two-decimal dollar cost for the cards. */
export function compactCost(value: number | null | undefined): string {
  const n = value ?? 0;
  if (n === 0) return "$0.00";
  if (n < 0.01) return "<$0.01";
  if (n >= 1000) return `$${compactCount(Math.round(n))}`;
  return `$${n.toFixed(2)}`;
}

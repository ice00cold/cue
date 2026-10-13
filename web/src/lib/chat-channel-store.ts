/**
 * Live PTY channel store.
 *
 * The dashboard keeps ChatPage (and its `/api/pty` child) mounted
 * persistently; that child's tui_gateway publishes every dispatcher frame —
 * including the cross-process `sessions.changed` push — into
 * `/api/pub?channel=<id>`, and the server fans frames out to `/api/events`
 * subscribers on that SAME channel. The channel id is minted per ChatPage
 * mount, so structured surfaces that live outside ChatPage's tree (the Thread
 * home header/transcript) read it from here instead of threading props
 * through the routed tree.
 *
 * `null` until the embedded chat has mounted (and between profile-driven
 * remounts); subscribers re-run on every change.
 */

let current: string | null = null;
const listeners = new Set<() => void>();

export function setLiveChatChannel(channel: string | null): void {
  if (channel === current) return;
  current = channel;
  for (const listener of listeners) listener();
}

export function getLiveChatChannel(): string | null {
  return current;
}

export function subscribeLiveChatChannel(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

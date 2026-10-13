/**
 * Chat PTY activation latch.
 *
 * The dashboard keeps `ChatPage` mounted persistently (just hidden with CSS)
 * on every route so the embedded chat PTY survives tab switches. The downside
 * is that the PTY-connect effect would otherwise open `/api/pty` — which spawns
 * the whole TUI + agent bootstrap (on a fresh checkout this prints
 * `Installing TUI dependencies…` and runs `npm install`) — the moment the
 * dashboard loads *any* page, even one the user never navigates the chat into.
 *
 * The fix is to only open the PTY once the chat tab has actually been active,
 * while keeping activation **sticky** so the PTY still persists across later
 * tab switches. This helper computes that latch: once `true`, it stays `true`.
 */
export function latchChatActivation(previous: boolean, isActive: boolean): boolean {
  return previous || isActive;
}

/**
 * Routes whose visit activates (and shows) the persistent chat host: the
 * Thread home ("/") and the bare terminal view ("/chat", resume-in-chat deep
 * links). Kept next to the activation latch it feeds.
 */
export function isChatSurfaceRoute(path: string): boolean {
  return path === "/" || path === "/chat";
}

/**
 * Thread-interaction API family (Cue Phase 6 M4 / R4) — decisions, the goal
 * card, and the headless main-thread send.
 *
 * Types + calls for the M4 router surfaces. Lives beside `api-sessions.ts`
 * rather than in `api.ts` (size ratchet: that file may only go down), using
 * the same `fetchJSON` transport, session-token header and profile param.
 * The pure derivations the cards render (delivery copy, approval choice
 * sets) stay here too — data-in/data-out for vitest.
 */

import { fetchJSON } from "@/lib/api";

// ── Decisions ───────────────────────────────────────────────────────────────

/** One pending fork as the cards render it (GET /api/decisions). */
export interface DecisionOption {
  /** 1-based option index — the dispatcher's numeric answer vocabulary. */
  index: number;
  label: string;
  impact: string;
}

export interface DecisionCard {
  id: string;
  question: string;
  context: string;
  options: DecisionOption[];
  recommendation: string;
  impact: string;
  status: string;
  created_at: string;
}

export interface DecisionsResponse {
  profile: string;
  pending: DecisionCard[];
  pending_count: number;
}

/** Result of a resolve/withdraw: the dispatcher's own output line plus the
 * honest delivery report for the resume turn scheduled into the main thread. */
export interface DecisionActionResult {
  profile: string;
  output: string;
  delivered: "queued" | "unavailable" | "none";
  delivery_note?: string;
}

/** GET /api/goal — the goal the main thread runs. */
export interface GoalState {
  goal: string;
  status: "active" | "paused" | "done" | "cleared" | string;
  turns_used: number;
  max_turns: number;
  created_at: number;
  last_verdict: string | null;
  paused_reason: string | null;
  waiting_reason: string | null;
  subgoals?: string[];
}

export interface GoalResponse {
  profile: string;
  session_id: string | null;
  exists: boolean;
  status_line: string;
  state: GoalState | null;
}

export interface GoalActionResult extends GoalResponse {
  output: string;
  delivered: "queued" | "unavailable" | "none";
  delivery_note?: string;
}

/** POST /api/main_thread/send — one headless turn's outcome. */
export interface MainThreadSendResponse {
  delivered: boolean;
  profile: string;
  session_id: string;
  reply: string;
}

function withProfile(url: string, profile: string | undefined): string {
  if (!profile || url.includes("profile=")) return url;
  return `${url}${url.includes("?") ? "&" : "?"}profile=${encodeURIComponent(profile)}`;
}

export function getDecisions(profile = ""): Promise<DecisionsResponse> {
  return fetchJSON<DecisionsResponse>(withProfile("/api/decisions", profile));
}

export function resolveDecision(
  id: string,
  body: { choice: string; rationale?: string },
  profile = "",
): Promise<DecisionActionResult> {
  return fetchJSON<DecisionActionResult>(
    withProfile(`/api/decisions/${encodeURIComponent(id)}/resolve`, profile),
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
  );
}

export function withdrawDecision(
  id: string,
  body: { reason?: string },
  profile = "",
): Promise<DecisionActionResult> {
  return fetchJSON<DecisionActionResult>(
    withProfile(`/api/decisions/${encodeURIComponent(id)}/withdraw`, profile),
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
  );
}

export function getGoal(profile = ""): Promise<GoalResponse> {
  return fetchJSON<GoalResponse>(withProfile("/api/goal", profile));
}

export function postGoalAction(
  action: "pause" | "resume" | "clear" | "unwait",
  profile = "",
): Promise<GoalActionResult> {
  return fetchJSON<GoalActionResult>(withProfile("/api/goal", profile), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
}

export function sendMainThreadMessage(
  message: string,
  profile = "",
): Promise<MainThreadSendResponse> {
  return fetchJSON<MainThreadSendResponse>(withProfile("/api/main_thread/send", profile), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
}

// ── Pure derivations the cards render ───────────────────────────────────────

/** One-line copy for a resume delivery report. */
export function deliveryNotice(result: {
  delivered: "queued" | "unavailable" | "none";
  delivery_note?: string;
}): string | null {
  if (result.delivered === "queued") {
    return "Queued — the agent resumes in this thread.";
  }
  if (result.delivered === "unavailable") {
    return result.delivery_note ?? "Resolved, but the resume turn could not be delivered.";
  }
  return null;
}

/** The answer string the dispatcher expects for a card interaction: option
 * indexes ride as their numeric string, free text passes through verbatim. */
export function decisionChoiceValue(input: string): string {
  return input.trim();
}

/** A pending approval as the /api/ws server-request flow delivers it
 * (tui_gateway `_approval_request_payload`; the command arrives redacted). */
export interface GatewayApprovalRequest {
  id: string;
  sessionId: string | null;
  command: string;
  description: string;
  choices: string[];
  allowPermanent: boolean;
  smartDenied: boolean;
}

/** The choices an approval card offers, in display order. The payload's own
 * `choices` win when the backend sent them (it precomputes the offered set);
 * otherwise the classic once/session/always/deny ladder with `always` dropped
 * when a permanent allow is forbidden. */
export function approvalChoices(
  approval: Pick<GatewayApprovalRequest, "choices" | "allowPermanent">,
): string[] {
  if (approval.choices.length > 0) return approval.choices;
  const ladder = approval.allowPermanent
    ? ["once", "session", "always", "deny"]
    : ["once", "session", "deny"];
  return ladder;
}

/** Button copy for one approval choice. */
export function approvalChoiceLabel(choice: string): string {
  switch (choice) {
    case "once":
      return "Approve once";
    case "session":
      return "Approve for session";
    case "always":
      return "Always allow";
    case "deny":
      return "Deny";
    default:
      return choice;
  }
}

/** Parse one `approval` server-request params object into the card shape.
 * Unknown/absent fields degrade to honest empties — a malformed frame never
 * breaks the pane it complements. */
export function parseApprovalRequest(
  params: Record<string, unknown>,
  id: string,
): GatewayApprovalRequest {
  const str = (v: unknown): string => (typeof v === "string" ? v : "");
  const choices = Array.isArray(params.choices)
    ? params.choices.filter((c): c is string => typeof c === "string")
    : [];
  return {
    id,
    sessionId: str(params.session_id) || null,
    command: str(params.command),
    description: str(params.description) || "dangerous command",
    choices,
    allowPermanent: params.allow_permanent !== false,
    smartDenied: params.smart_denied === true,
  };
}

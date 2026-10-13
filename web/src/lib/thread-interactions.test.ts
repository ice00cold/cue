/**
 * Pure derivations of the M4 interaction cards (thread-interactions.ts):
 * delivery notice copy, the approval choice ladder, and approval-request
 * parsing — data-in/data-out, no sockets.
 */

import { describe, expect, it } from "vitest";
import {
  approvalChoiceLabel,
  approvalChoices,
  decisionChoiceValue,
  deliveryNotice,
  parseApprovalRequest,
} from "@/lib/thread-interactions";

describe("deliveryNotice", () => {
  it("queued means the resume turn is in the main thread", () => {
    expect(deliveryNotice({ delivered: "queued" })).toBe(
      "Queued — the agent resumes in this thread.",
    );
  });

  it("unavailable surfaces the backend's reason verbatim", () => {
    expect(
      deliveryNotice({ delivered: "unavailable", delivery_note: "The main thread does not exist yet." }),
    ).toBe("The main thread does not exist yet.");
  });

  it("unavailable without a note gets honest fallback copy", () => {
    expect(deliveryNotice({ delivered: "unavailable" })).toContain("could not be delivered");
  });

  it("none (withdrawals, pauses) is silent", () => {
    expect(deliveryNotice({ delivered: "none" })).toBeNull();
  });
});

describe("decisionChoiceValue", () => {
  it("passes option indexes and free text through, trimmed", () => {
    expect(decisionChoiceValue(" 2 ")).toBe("2");
    expect(decisionChoiceValue(" do both ")).toBe("do both");
  });
});

describe("approvalChoices", () => {
  it("the backend's offered choices win when present", () => {
    expect(approvalChoices({ choices: ["once", "deny"], allowPermanent: true })).toEqual([
      "once",
      "deny",
    ]);
  });

  it("the classic ladder includes always only when a permanent allow is allowed", () => {
    expect(approvalChoices({ choices: [], allowPermanent: true })).toEqual([
      "once",
      "session",
      "always",
      "deny",
    ]);
    expect(approvalChoices({ choices: [], allowPermanent: false })).toEqual([
      "once",
      "session",
      "deny",
    ]);
  });

  it("an absent allow_permanent counts as allowed (backend omits unless forbidden)", () => {
    const parsed = parseApprovalRequest({}, "srq-1");
    expect(parsed.allowPermanent).toBe(true);
  });
});

describe("approvalChoiceLabel", () => {
  it("maps the canonical choices to button copy and keeps unknowns verbatim", () => {
    expect(approvalChoiceLabel("once")).toBe("Approve once");
    expect(approvalChoiceLabel("session")).toBe("Approve for session");
    expect(approvalChoiceLabel("always")).toBe("Always allow");
    expect(approvalChoiceLabel("deny")).toBe("Deny");
    expect(approvalChoiceLabel("yolo")).toBe("yolo");
  });
});

describe("parseApprovalRequest", () => {
  it("reads the wire shape the server-request flow delivers", () => {
    const approval = parseApprovalRequest(
      {
        session_id: "sess-1",
        command: "rm -rf /tmp/build",
        description: "clean the build dir",
        choices: ["once", "session", "deny"],
        request_id: "abc",
      },
      "srq-abc",
    );
    expect(approval).toMatchObject({
      id: "srq-abc",
      sessionId: "sess-1",
      command: "rm -rf /tmp/build",
      description: "clean the build dir",
      choices: ["once", "session", "deny"],
      allowPermanent: true,
      smartDenied: false,
    });
  });

  it("a malformed frame degrades to honest empties, never a broken pane", () => {
    const approval = parseApprovalRequest(
      { session_id: 7, command: null, choices: "yes", smart_denied: 1 },
      "srq-x",
    );
    expect(approval.sessionId).toBeNull();
    expect(approval.command).toBe("");
    expect(approval.description).toBe("dangerous command");
    expect(approval.choices).toEqual([]);
    expect(approval.smartDenied).toBe(false);
  });
});

/**
 * ApprovalCards — pending dangerous-command approvals (Cue M4 / R4).
 *
 * The server-request flow the Desktop already renders, surfaced as cards on
 * the Thread page: an `approval` request from the dashboard backend (for a
 * session it hosts) becomes a card whose buttons answer with the choice the
 * backend's approval queue resolves on. The command arrives pre-redacted
 * server-side. `request.cancel` removes the card wherever it was withdrawn.
 */

import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { ShieldAlert } from "lucide-react";
import type { OpenApproval } from "@/hooks/useGatewayApprovals";
import { approvalChoiceLabel, approvalChoices } from "@/lib/thread-interactions";

export function ApprovalCards({
  approvals,
  onRespond,
}: {
  approvals: OpenApproval[];
  onRespond: (id: string, choice: string) => void;
}) {
  if (approvals.length === 0) return null;
  return (
    <div className="flex min-w-0 flex-col gap-2" data-testid="approval-cards">
      {approvals.map((approval) => {
        const choices = approvalChoices(approval);
        return (
          <Card key={approval.id}>
            <CardContent className="gap-1.5 px-3 py-2.5">
              <div className="flex min-w-0 items-start gap-2">
                <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />
                <div className="min-w-0 flex-1">
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <span className="text-sm">{approval.description}</span>
                    {approval.smartDenied && <Badge tone="destructive">smart-denied</Badge>}
                  </div>
                  {approval.command && (
                    <pre className="mt-1 max-h-24 overflow-auto rounded bg-midground/5 p-2 font-mono text-xs whitespace-pre-wrap">
                      {approval.command}
                    </pre>
                  )}
                  <div className="mt-2 flex flex-wrap gap-2">
                    {choices.map((choice) => (
                      <Button
                        key={choice}
                        size="sm"
                        ghost={choice === "deny"}
                        outlined={choice !== "deny"}
                        onClick={() => onRespond(approval.id, choice)}
                        aria-label={`${approvalChoiceLabel(choice)}: ${approval.command || approval.description}`}
                        className="h-7 px-2 text-xs"
                      >
                        {approvalChoiceLabel(choice)}
                      </Button>
                    ))}
                  </div>
                </div>
              </div>
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}

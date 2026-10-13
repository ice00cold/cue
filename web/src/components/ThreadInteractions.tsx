/**
 * ThreadInteractions — the M4 (R4) band on the Thread home: decision cards,
 * the goal card, and approval cards, between the summary cards and the
 * transcript. Interaction from the surface means exactly this: pending forks
 * resolve, the goal steers, approvals answer — sends stay with the embedded
 * TUI composer below (web/AGENTS.md), and every resume/continuation turn the
 * cards queue lands in the ONE main thread, visible in the transcript above.
 *
 * Refetches ride the page's existing `sessions.changed` signal (a decision's
 * resume turn, a goal's continuation — anything that writes the thread — also
 * refreshes these cards), never a polling loop.
 */

import { useEffect, useRef } from "react";
import { ApprovalCards } from "@/components/ApprovalCards";
import { DecisionCards } from "@/components/DecisionCards";
import { GoalCard } from "@/components/GoalCard";
import { useGatewayApprovals } from "@/hooks/useGatewayApprovals";
import { useDecisions, useGoalCard } from "@/hooks/useThreadCards";

export function ThreadInteractions({
  profile,
  refreshToken,
}: {
  profile: string | undefined;
  refreshToken: number;
}) {
  const decisions = useDecisions(profile);
  const goal = useGoalCard(profile);
  const approvals = useGatewayApprovals();

  // Latest refresh callbacks without re-running the signal effect when they
  // change identity (the useSessionsChangedRefetch idiom); the hooks' own
  // load effect already covers mount and profile switches, so only LATER
  // sessions.changed signals refresh here.
  const refreshRef = useRef({ decisions: decisions.refresh, goal: goal.refresh });
  useEffect(() => {
    refreshRef.current = { decisions: decisions.refresh, goal: goal.refresh };
  }, [decisions.refresh, goal.refresh]);
  const firstSignal = useRef(true);
  useEffect(() => {
    if (firstSignal.current) {
      firstSignal.current = false;
      return;
    }
    refreshRef.current.decisions();
    refreshRef.current.goal();
  }, [refreshToken]);

  return (
    <div className="flex min-w-0 flex-col gap-2">
      <DecisionCards
        pending={decisions.pending}
        error={decisions.error}
        onResolve={decisions.resolve}
        onWithdraw={decisions.withdraw}
      />
      <GoalCard goal={goal.goal} error={goal.error} onAction={goal.act} />
      <ApprovalCards approvals={approvals.approvals} onRespond={approvals.respond} />
    </div>
  );
}

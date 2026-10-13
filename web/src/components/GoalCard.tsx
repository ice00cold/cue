/**
 * GoalCard — the goal the ONE conversation currently runs (Cue M4 / R4).
 *
 * Status over `GET /api/goal`, control verbs (pause / resume / clear) through
 * the shared dispatcher on the backend. Setting goal text is deliberately not
 * offered: a new goal is a composer action (the embedded TUI's `/goal`), this
 * card only steers the goal the thread already runs.
 */

import { useState } from "react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Goal as GoalIcon } from "lucide-react";
import type { GoalResponse } from "@/lib/thread-interactions";
import { deliveryNotice } from "@/lib/thread-interactions";

const STATUS_TONE: Record<string, "default" | "secondary" | "outline"> = {
  active: "default",
  paused: "secondary",
  done: "outline",
};

export function GoalCard({
  goal,
  error,
  onAction,
}: {
  goal: GoalResponse | null;
  error: string | null;
  onAction: (action: "pause" | "resume" | "clear") => Promise<{ output: string; delivered?: "queued" | "unavailable" | "none"; delivery_note?: string } | { error: string }>;
}) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  if (!goal?.exists) return null;

  const run = async (action: "pause" | "resume" | "clear") => {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    const result = await onAction(action);
    setBusy(false);
    if ("error" in result) {
      setActionError(result.error);
      return;
    }
    const delivery = deliveryNotice({ delivered: result.delivered ?? "none", delivery_note: result.delivery_note });
    setNotice(delivery ?? result.output.split("\n")[0]);
  };

  const state = goal.state;
  const status = state?.status ?? "active";
  const paused = status === "paused";

  return (
    <Card data-testid="goal-card">
      <CardContent className="gap-1.5 px-3 py-2.5">
        <div className="flex min-w-0 items-start gap-2">
          <GoalIcon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-text-tertiary" />
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-sm" title={state?.goal ?? ""}>
                {state?.goal}
              </span>
              <Badge tone={STATUS_TONE[status] ?? "secondary"}>{status}</Badge>
              {state && (
                <span className="text-xs tabular-nums text-text-tertiary">
                  {state.turns_used}/{state.max_turns} turns
                </span>
              )}
            </div>
            {state?.paused_reason && (
              <p className="mt-0.5 text-xs text-text-tertiary">paused — {state.paused_reason}</p>
            )}
            <div className="mt-2 flex flex-wrap gap-2">
              <Button
                size="sm"
                outlined
                disabled={busy || paused}
                onClick={() => void run("pause")}
                className="h-7 px-2 text-xs"
              >
                Pause
              </Button>
              <Button
                size="sm"
                outlined
                disabled={busy || !paused}
                onClick={() => void run("resume")}
                className="h-7 px-2 text-xs"
              >
                Resume
              </Button>
              <Button
                size="sm"
                ghost
                disabled={busy}
                onClick={() => void run("clear")}
                className="h-7 px-2 text-xs text-text-tertiary"
              >
                Clear
              </Button>
            </div>
            {(actionError || error) && (
              <p className="mt-1.5 text-xs text-destructive">{actionError ?? error}</p>
            )}
            {notice && <p className="mt-1.5 text-xs text-text-tertiary">{notice}</p>}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * DecisionCards — the pending forks of the ONE conversation (Cue M4 / R4).
 *
 * Each card is a decision waiting on the user: question, context, the options
 * with their impact, the agent's recommendation. Resolving sends the option's
 * index (or free-form text) plus an optional rationale through the shared
 * dispatcher on the backend. A settled fork collapses into a compact result
 * note (the dispatcher's own output line + the honest delivery report) that
 * survives the next refetch — the CLI's one printed line, as a card aftermath.
 * Never a second chat surface — the resume turn lands in the main thread and
 * its reply is visible in the transcript above.
 */

import { useState } from "react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { GitFork } from "lucide-react";
import type { DecisionCard as DecisionCardData } from "@/lib/thread-interactions";
import { deliveryNotice } from "@/lib/thread-interactions";

interface ActionOutcome {
  output: string;
  delivered: "queued" | "unavailable" | "none";
  delivery_note?: string;
}

type Resolver = (
  id: string,
  choice: string,
  rationale: string,
) => Promise<ActionOutcome | { error: string }>;

interface SettledNote extends ActionOutcome {
  id: string;
  question: string;
}

function DecisionRow({
  decision,
  onResolve,
  onWithdraw,
  onSettled,
}: {
  decision: DecisionCardData;
  onResolve: Resolver;
  onWithdraw: (id: string, reason: string) => Promise<ActionOutcome | { error: string }>;
  onSettled: (note: SettledNote) => void;
}) {
  const [custom, setCustom] = useState("");
  const [rationale, setRationale] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const apply = async (run: () => Promise<ActionOutcome | { error: string }>) => {
    setBusy(true);
    setError(null);
    const result = await run();
    setBusy(false);
    if ("error" in result) {
      setError(result.error);
      return;
    }
    onSettled({ id: decision.id, question: decision.question, ...result });
  };

  const recommended = decision.options.find(
    (option) => decision.recommendation && decision.recommendation.includes(option.label),
  );

  return (
    <Card>
      <CardContent className="gap-2 px-3 py-2.5">
        <div className="flex min-w-0 items-start gap-2">
          <GitFork className="mt-0.5 h-3.5 w-3.5 shrink-0 text-text-tertiary" />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs text-text-tertiary">{decision.id}</span>
              <span className="min-w-0 flex-1 text-sm">{decision.question}</span>
              {recommended && <Badge tone="outline">recommends {recommended.index}</Badge>}
            </div>
            {decision.context && (
              <p className="mt-1 text-xs text-text-tertiary">{decision.context}</p>
            )}
            <div className="mt-2 flex flex-col gap-1">
              {decision.options.map((option) => (
                <div key={option.index} className="flex items-baseline gap-2">
                  <Button
                    size="sm"
                    outlined={option.index !== recommended?.index}
                    disabled={busy}
                    onClick={() =>
                      void apply(() => onResolve(decision.id, String(option.index), rationale))
                    }
                    aria-label={`Resolve ${decision.id} with option ${option.index}: ${option.label}`}
                    className="h-6 shrink-0 px-2 text-xs"
                  >
                    {option.index}
                  </Button>
                  <span className="min-w-0 flex-1 text-xs">
                    {option.label}
                    {option.impact && (
                      <span className="text-text-tertiary"> — {option.impact}</span>
                    )}
                  </span>
                </div>
              ))}
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <input
                value={custom}
                onChange={(event) => setCustom(event.target.value)}
                placeholder="free-form answer"
                aria-label={`Free-form answer for ${decision.id}`}
                className="h-7 min-w-0 flex-1 basis-40 rounded border border-current/20 bg-transparent px-2 text-xs outline-none focus:border-current/50"
              />
              <Button
                size="sm"
                outlined
                disabled={busy || !custom.trim()}
                onClick={() => void apply(() => onResolve(decision.id, custom.trim(), rationale))}
                className="h-7 px-2 text-xs"
              >
                Answer
              </Button>
              <Button
                size="sm"
                ghost
                disabled={busy}
                onClick={() => void apply(() => onWithdraw(decision.id, ""))}
                className="h-7 px-2 text-xs text-text-tertiary"
              >
                Withdraw
              </Button>
            </div>
            <input
              value={rationale}
              onChange={(event) => setRationale(event.target.value)}
              placeholder="rationale (recorded with the answer)"
              aria-label={`Rationale for ${decision.id}`}
              className="mt-1.5 h-7 w-full rounded border border-current/20 bg-transparent px-2 text-xs outline-none focus:border-current/50"
            />
            {error && <p className="mt-1.5 text-xs text-destructive">{error}</p>}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

/** Compact aftermath of a settled fork; the newest first, bounded. */
const MAX_NOTES = 3;

export function DecisionCards({
  pending,
  error,
  onResolve,
  onWithdraw,
}: {
  pending: DecisionCardData[];
  error: string | null;
  onResolve: Resolver;
  onWithdraw: (id: string, reason: string) => Promise<ActionOutcome | { error: string }>;
}) {
  const [notes, setNotes] = useState<SettledNote[]>([]);

  if (pending.length === 0 && notes.length === 0) return null;
  const settled = new Set(notes.map((note) => note.id));
  const open = pending.filter((decision) => !settled.has(decision.id));

  return (
    <div className="flex min-w-0 flex-col gap-2" data-testid="decision-cards">
      {error && <p className="px-1 text-xs text-destructive">{error}</p>}
      {open.map((decision) => (
        <DecisionRow
          key={decision.id}
          decision={decision}
          onResolve={onResolve}
          onWithdraw={onWithdraw}
          onSettled={(note) =>
            setNotes((prev) => [note, ...prev.filter((existing) => existing.id !== note.id)].slice(0, MAX_NOTES))
          }
        />
      ))}
      {notes.map((note) => (
        <p key={note.id} className="px-1 text-xs text-text-tertiary">
          <span className="truncate" title={note.output}>
            {note.output.split("\n")[0]}
          </span>
          {deliveryNotice(note) && <span> · {deliveryNotice(note)}</span>}
        </p>
      ))}
    </div>
  );
}

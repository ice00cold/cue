/**
 * ThreadSummaryCards — the structured stat row of the main-thread home.
 *
 * One glance at the ONE conversation: message volume, token/cost footprint,
 * liveness. Renders dashes until the thread exists (the embedded TUI's first
 * message creates it) — an honest empty state, not a spinner wall.
 */

import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Typography } from "@nous-research/ui/ui/components/typography/index";
import type { MainThreadResponse } from "@/lib/api";
import { compactCost, compactCount } from "@/lib/thread-home";
import { timeAgo } from "@/lib/utils";

function StatCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <Card className="min-w-0 flex-1 basis-40">
      <CardContent className="gap-0.5 px-3 py-2">
        <span className="font-sans text-display text-xs tracking-wider text-text-tertiary">
          {label}
        </span>
        <Typography className="font-mono text-base tabular-nums">
          {value}
        </Typography>
        {hint && <span className="truncate text-xs text-text-tertiary">{hint}</span>}
      </CardContent>
    </Card>
  );
}

export function ThreadSummaryCards({ thread }: { thread: MainThreadResponse | null }) {
  const session = thread?.session ?? null;
  const tokens =
    (session?.input_tokens ?? 0) +
    (session?.output_tokens ?? 0) +
    (session?.cache_read_tokens ?? 0) +
    (session?.cache_write_tokens ?? 0);

  return (
    <div className="flex min-w-0 flex-wrap gap-2">
      <StatCard
        label="Messages"
        value={session ? compactCount(session.message_count) : "—"}
        hint={session?.source ? `via ${session.source}` : undefined}
      />
      <StatCard
        label="Tokens"
        value={session ? compactCount(tokens) : "—"}
        hint={
          session
            ? `${compactCount(session.input_tokens)} in / ${compactCount(session.output_tokens)} out`
            : undefined
        }
      />
      <StatCard
        label="Cost"
        value={session ? compactCost(session.estimated_cost_usd) : "—"}
      />
      <StatCard
        label="Last activity"
        value={session ? timeAgo(session.last_active) : "—"}
        hint={session?.last_activity_description ?? session?.model ?? undefined}
      />
    </div>
  );
}

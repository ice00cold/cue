/**
 * ThreadHeader — the structured header of the main-thread home (Phase 6 M1).
 *
 * Data-only chrome around the embedded TUI: status, session identity and a
 * manual re-resolve affordance. Failures surface as a line of copy, never a
 * blocking overlay — the terminal pane below keeps working (web/AGENTS.md).
 */

import { useState } from "react";
import { Check, Copy, RotateCw } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Typography } from "@nous-research/ui/ui/components/typography/index";
import type { MainThreadResponse } from "@/lib/api";
import { copyTextToClipboard } from "@/lib/clipboard";
import { threadSessionChip, threadStatus } from "@/lib/thread-home";

const STATUS_TONE: Record<string, "success" | "secondary" | "outline"> = {
  active: "success",
  idle: "secondary",
  new: "outline",
};

const STATUS_LABEL: Record<string, string> = {
  active: "Active",
  idle: "Idle",
  new: "No thread yet",
};

export function ThreadHeader({
  thread,
  loading,
  error,
  onRefresh,
}: {
  thread: MainThreadResponse | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const status = threadStatus(thread);
  const sessionId = thread?.live_session_id ?? null;

  const copySessionId = () => {
    if (!sessionId) return;
    void copyTextToClipboard(sessionId).then((ok) => {
      if (!ok) return;
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2">
      <Typography className="font-display text-lg font-semibold tracking-wide text-midground">
        Main thread
      </Typography>
      {thread && thread.profile && (
        <span className="text-xs text-text-tertiary">
          {thread.profile === "default" ? "default profile" : `profile: ${thread.profile}`}
        </span>
      )}
      <Badge tone={STATUS_TONE[status]} className="text-xs">
        {STATUS_LABEL[status]}
      </Badge>

      <button
        type="button"
        onClick={copySessionId}
        disabled={!sessionId}
        title={sessionId ?? "The thread is created by the first message"}
        aria-label="Copy main thread session id"
        className="flex items-center gap-1 border border-current/20 px-1.5 py-0.5 font-mono text-xs text-text-secondary hover:text-midground disabled:cursor-not-allowed disabled:opacity-50"
      >
        {threadSessionChip(sessionId)}
        {sessionId && (copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />)}
      </button>

      <div className="ml-auto flex items-center gap-2">
        {error && (
          <span className="max-w-[24rem] truncate text-xs text-destructive" title={error}>
            {error}
          </span>
        )}
        <Button
          ghost
          size="icon"
          onClick={onRefresh}
          aria-label="Re-resolve the main thread"
          disabled={loading}
          className="text-text-secondary hover:text-midground"
        >
          {loading ? <Spinner className="h-4 w-4" /> : <RotateCw className="h-4 w-4" />}
        </Button>
      </div>
    </div>
  );
}

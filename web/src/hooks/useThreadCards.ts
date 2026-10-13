/**
 * Decision + goal card data (Cue Phase 6 M4 / R4).
 *
 * `useDecisions` / `useGoalCard` follow `useMainThread`'s loader-callback
 * idiom: load on mount and on demand, sequence-guarded against superseded
 * loads (profile switch, refresh). Mutations apply their result back into the
 * loaded state and return it, so a card can show the dispatcher's own output
 * line plus the honest delivery report without a refetch round-trip; callers
 * wire `sessions.changed` (the existing refetch signal) to `refresh`.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  type DecisionActionResult,
  type DecisionCard,
  type DecisionsResponse,
  type GoalActionResult,
  type GoalResponse,
  getDecisions,
  getGoal,
  postGoalAction,
  resolveDecision,
  withdrawDecision,
} from "@/lib/thread-interactions";

export interface DecisionsState {
  pending: DecisionCard[];
  error: string | null;
  loading: boolean;
}

export function useDecisions(profile: string | undefined) {
  const [state, setState] = useState<DecisionsState>({
    pending: [],
    error: null,
    loading: true,
  });
  const requestSeq = useRef(0);
  const scopedProfile = profile ?? "";

  const load = useCallback(() => {
    const seq = ++requestSeq.current;
    setState((prev) => ({ ...prev, loading: true }));
    getDecisions(scopedProfile)
      .then((res: DecisionsResponse) => {
        if (seq !== requestSeq.current) return;
        setState({ pending: res.pending, error: null, loading: false });
      })
      .catch((err: unknown) => {
        if (seq !== requestSeq.current) return;
        setState((prev) => ({
          ...prev,
          loading: false,
          error: err instanceof Error ? err.message : String(err),
        }));
      });
  }, [scopedProfile]);

  useEffect(() => {
    let cancelled = false;
    queueMicrotask(() => {
      if (!cancelled) load();
    });
    return () => {
      cancelled = true;
    };
  }, [load]);

  const mutate = useCallback(
    async (
      id: string,
      run: () => Promise<DecisionActionResult>,
    ): Promise<DecisionActionResult | { error: string }> => {
      try {
        const result = await run();
        // The resolved/withdrawn fork left the pending view server-side; apply
        // the same transition locally rather than racing a refetch.
        setState((prev) => ({
          ...prev,
          pending: prev.pending.filter((decision) => decision.id !== id),
        }));
        return result;
      } catch (err: unknown) {
        return { error: err instanceof Error ? err.message : String(err) };
      }
    },
    [],
  );

  const resolve = useCallback(
    (id: string, choice: string, rationale = "") =>
      mutate(id, () => resolveDecision(id, { choice, rationale }, scopedProfile)),
    [mutate, scopedProfile],
  );
  const withdraw = useCallback(
    (id: string, reason = "") => mutate(id, () => withdrawDecision(id, { reason }, scopedProfile)),
    [mutate, scopedProfile],
  );

  return { ...state, refresh: load, resolve, withdraw };
}

export interface GoalCardState {
  goal: GoalResponse | null;
  error: string | null;
  loading: boolean;
}

export function useGoalCard(profile: string | undefined) {
  const [state, setState] = useState<GoalCardState>({
    goal: null,
    error: null,
    loading: true,
  });
  const requestSeq = useRef(0);
  const scopedProfile = profile ?? "";

  const load = useCallback(() => {
    const seq = ++requestSeq.current;
    setState((prev) => ({ ...prev, loading: true }));
    getGoal(scopedProfile)
      .then((res: GoalResponse) => {
        if (seq !== requestSeq.current) return;
        setState({ goal: res, error: null, loading: false });
      })
      .catch((err: unknown) => {
        if (seq !== requestSeq.current) return;
        setState((prev) => ({
          ...prev,
          loading: false,
          error: err instanceof Error ? err.message : String(err),
        }));
      });
  }, [scopedProfile]);

  useEffect(() => {
    let cancelled = false;
    queueMicrotask(() => {
      if (!cancelled) load();
    });
    return () => {
      cancelled = true;
    };
  }, [load]);

  const act = useCallback(
    async (
      action: "pause" | "resume" | "clear" | "unwait",
    ): Promise<GoalActionResult | { error: string }> => {
      try {
        const result = await postGoalAction(action, scopedProfile);
        setState({ goal: result, error: null, loading: false });
        return result;
      } catch (err: unknown) {
        return { error: err instanceof Error ? err.message : String(err) };
      }
    },
    [scopedProfile],
  );

  return { ...state, refresh: load, act };
}

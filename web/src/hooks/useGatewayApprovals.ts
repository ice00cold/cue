/**
 * Approval cards over the server-request flow (Cue Phase 6 M4 / R4).
 *
 * The Dashboard's exact reuse of the flow the Desktop already renders: the
 * `/api/ws` sidecar speaks the tui_gateway JSON-RPC dialect, the shared
 * channel advertises `client.capabilities {server_requests: true}` on
 * `gateway.ready`, and the backend's `approval` server→client requests arrive
 * as `onRequest` frames for sessions THIS backend hosts. Answering routes the
 * response frame back over the socket the request arrived on — the owner
 * backend by construction. `request.cancel` (timeout / interrupt / session
 * close) tears the matching card down; a re-delivered request reuses its id,
 * so the card replaces its older generation.
 *
 * Best-effort like every sidecar (web/AGENTS.md): a dropped socket retries on
 * a bounded backoff ladder and the pane it complements keeps working.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { GatewayClient } from "@/lib/gatewayClient";
import {
  type GatewayApprovalRequest,
  parseApprovalRequest,
} from "@/lib/thread-interactions";

/** One open approval plus its answer route (the request's own respond frame). */
export type OpenApproval = GatewayApprovalRequest & {
  respond: (choice: string) => void;
};

/** Bounded reconnect ladder (ChatSidebar's sidecar contract). */
const MAX_RECONNECT_ATTEMPTS = 5;
/** A connection healthy this long refills the ladder. */
const HEALTHY_OPEN_GRACE_MS = 10_000;

function reconnectDelayMs(attempt: number): number {
  return Math.min(1000 * 2 ** Math.min(attempt, 4), 15_000);
}

export function useGatewayApprovals() {
  const [approvals, setApprovals] = useState<OpenApproval[]>([]);
  // Answer routes keyed by request id: `respond` fires synchronously on click,
  // never as a side effect inside a state updater (idempotent server-side, but
  // a double-invoked updater must not double-answer).
  const respondRoutes = useRef(new Map<string, (choice: string) => void>());

  const respond = useCallback((id: string, choice: string) => {
    respondRoutes.current.get(id)?.(choice);
    respondRoutes.current.delete(id);
    setApprovals((prev) => prev.filter((approval) => approval.id !== id));
  }, []);

  useEffect(() => {
    let unmounting = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let healthyTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    const forget = (id: string) => {
      routes.delete(id);
      setApprovals((prev) => prev.filter((approval) => approval.id !== id));
    };

    const scheduleReconnect = () => {
      if (unmounting || reconnectTimer) return;
      if (healthyTimer) {
        clearTimeout(healthyTimer);
        healthyTimer = null;
      }
      if (attempt >= MAX_RECONNECT_ATTEMPTS) return;
      const delay = reconnectDelayMs(attempt);
      attempt += 1;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        void connect();
      }, delay);
    };

    const gw = new GatewayClient();
    // Captured for the cleanup: the ref object is stable, but the rule is
    // right that `.current` at cleanup time is a different value.
    const routes = respondRoutes.current;
    const offRequest = gw.onRequest((request) => {
      if (request.method !== "approval") return false;
      const approval: OpenApproval = {
        ...parseApprovalRequest(request.params, request.id),
        // The result frame the backend's approval queue resolves on.
        respond: (choice: string) => request.respond({ choice }),
      };
      routes.set(approval.id, approval.respond);
      setApprovals((prev) =>
        prev.some((existing) => existing.id === approval.id)
          ? prev.map((existing) => (existing.id === approval.id ? approval : existing))
          : [...prev, approval],
      );
      return true;
    });
    const offEvent = gw.on("request.cancel", (event) => {
      const id = event.payload?.id;
      if (!id) return;
      forget(id);
    });
    const offState = gw.onState((state) => {
      if (state === "open") {
        if (healthyTimer) clearTimeout(healthyTimer);
        healthyTimer = setTimeout(() => {
          healthyTimer = null;
          attempt = 0;
        }, HEALTHY_OPEN_GRACE_MS);
        return;
      }
      // 'closed'/'error' after having been open, or a failed dial: retry on
      // the ladder. Terminal auth failures are handled by the client's own
      // reload hook; a spent ladder leaves the pane it complements working.
      if (!unmounting && (state === "closed" || state === "error")) {
        scheduleReconnect();
      }
    });

    const connect = async () => {
      if (unmounting) return;
      try {
        await gw.connect();
      } catch {
        if (!unmounting) scheduleReconnect();
      }
    };

    void connect();

    return () => {
      unmounting = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (healthyTimer) clearTimeout(healthyTimer);
      offRequest();
      offEvent();
      offState();
      gw.close();
      routes.clear();
      setApprovals([]);
    };
  }, []);

  return { approvals, respond };
}

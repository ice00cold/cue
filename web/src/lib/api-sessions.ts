/**
 * Sessions-family API types (`/api/sessions*`, `/api/main_thread`).
 *
 * Extracted from api.ts (size ratchet) — shapes shared by the sessions REST
 * surface and its consumers (SessionsPage, the Phase 6 Thread home). The
 * calls themselves live in api.ts; api.ts re-exports these for existing
 * import sites.
 */

export interface SessionInfo {
  id: string;
  source: string | null;
  model: string | null;
  title: string | null;
  started_at: number;
  ended_at: number | null;
  last_active: number;
  is_active: boolean;
  message_count: number;
  tool_call_count: number;
  input_tokens: number;
  output_tokens: number;
  preview: string | null;
  parent_session_id?: string | null;
  // Present on detail/resolver rows (SELECT s.*), absent on trimmed list rows.
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  total_tokens?: number;
  estimated_cost_usd?: number | null;
  session_key?: string | null;
  last_activity_description?: string | null;
  /** Owning profile stamped by the list/detail endpoints (the store the row
   * was read from). Absent on search-endpoint rows, which carry no stamp. */
  profile?: string;
}

export interface SessionLatestDescendantResponse {
  requested_session_id: string;
  session_id: string;
  path: string[];
  changed: boolean;
}

/** GET /api/main_thread — the profile's resolved main thread (Cue M1). */
export interface MainThreadResponse {
  profile: string;
  session_key: string;
  /** Routing-row session id (root of the live compression chain). */
  session_id: string | null;
  /** Compression tip — where new messages land; null until the thread exists. */
  live_session_id: string | null;
  exists: boolean;
  is_active: boolean;
  /** Tip's summary row, same shape as the session detail endpoint; null when
   * the thread has not been created yet. */
  session: SessionInfo | null;
}

export interface PaginatedSessions {
  sessions: SessionInfo[];
  total: number;
  limit: number;
  offset: number;
}

export interface SessionStoreStats {
  total: number;
  active_store: number;
  archived: number;
  messages: number;
  by_source: Record<string, number>;
}

export interface SessionMessage {
  /** Durable row id (AUTOINCREMENT rowid) — stable across refetches. */
  id?: number;
  role: "user" | "assistant" | "system" | "tool";
  content: string | null;
  tool_calls?: Array<{
    id: string;
    function: { name: string; arguments: string };
  }>;
  tool_name?: string;
  tool_call_id?: string;
  timestamp?: number;
}

export interface SessionMessagesResponse {
  session_id: string;
  messages: SessionMessage[];
  pagination?: {
    limit: number;
    offset: number;
    order: "latest" | "oldest";
    returned: number;
  };
}

export interface SessionSearchResult extends SessionInfo {
  session_id: string;
  snippet: string;
  role: string | null;
  session_started: number | null;
  lineage_root?: string;
}

export interface SessionSearchResponse {
  results: SessionSearchResult[];
}

/** One node of the drill-down lineage tree (Cue M2): a child conversation
 * (worker/subagent, cron run, branch) of the focus session, collapsed to its
 * compression tip so its transcript opens where messages land. */
export interface SessionLineageNode {
  id: string;
  /** Live tip of the node's compression chain — address the transcript with this. */
  session_id: string;
  chain: string[];
  source: string | null;
  title: string | null;
  session_key?: string | null;
  parent_session_id?: string | null;
  model?: string | null;
  started_at: number;
  ended_at: number | null;
  end_reason?: string | null;
  message_count: number;
  tool_call_count: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  estimated_cost_usd?: number | null;
  cwd?: string | null;
  is_active: boolean;
  child_count: number;
  children: SessionLineageNode[];
}

/** GET /api/sessions/{id}/lineage — the focus conversation's child tree. */
export interface SessionLineageResponse {
  session_id: string;
  chain: string[];
  live_session_id: string;
  children: SessionLineageNode[];
  truncated: boolean;
  profile: string;
}

/** Bounded per-task status line from a delegation's batch result. */
export interface DelegationTaskSummary {
  task_index: number;
  status: string;
  goal: string;
  error: string | null;
}

/** One durable async_delegations row (Cue M2): the delegation half of the
 * drill-down. The conversation it belongs to is the origin session — reached
 * through the existing sessions endpoints, never re-rendered here. */
export interface DelegationRecord {
  delegation_id: string;
  origin_session: string;
  origin_ui_session_id: string;
  origin_session_id: string;
  parent_session_id: string | null;
  state: string;
  dispatched_at: number;
  completed_at: number | null;
  updated_at: number;
  delivery_state: string;
  task: {
    goal?: string;
    goals?: string[];
    is_batch?: boolean;
    role?: string | null;
    model?: string | null;
    context?: string | null;
    owner_cwd?: string;
  } | null;
  result: {
    status: string;
    summary?: string | null;
    error?: string | null;
    api_calls?: number | null;
    duration_seconds?: number | null;
    exit_reason?: string | null;
    task_count?: number;
    total_duration_seconds?: number | null;
    tasks?: DelegationTaskSummary[];
    truncated?: boolean;
  } | null;
  profile?: string;
}

/** GET /api/delegations — delegation records, newest dispatched first. */
export interface DelegationsResponse {
  delegations: DelegationRecord[];
  count: number;
  limit: number;
  profile: string;
}

/** One run row from /api/cron/jobs/{id}/runs: an agent session (openable via
 * the messages endpoint / ``/chat?resume=``) or a script-only output row whose
 * id carries the ``cron_output:`` prefix and has no session behind it. */
export interface CronRunRow {
  id: string;
  title: string | null;
  preview?: string | null;
  source?: string | null;
  started_at: number;
  last_active?: number;
  ended_at?: number | null;
  is_active?: boolean;
  scheduler_owned?: boolean;
  message_count?: number;
  archived?: boolean;
}

export interface CronJobRunsResponse {
  runs: CronRunRow[];
  limit: number;
}

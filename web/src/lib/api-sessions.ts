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

# Phase 6 — Front-end architecture: A (own dashboard) vs B (OpenChamber provider)

Architecture scout for PLAN.md Phase 6. Evidence gathered read-only on 2026-10-12
(branch `feat/phase-6-frontend`): Cue's own source (web/, web_routers/, tui_gateway/,
gateway/platforms/, hermes_state*), a read-only peek at the OpenChamber render path
(`~/.config/openchamber/settings.json`, `/home/af/.local/share/opencode/opencode.db`), and
OC-FRONTEND-INVESTIGATION.md (Phase 5, same tree). No implementation code changed here.

## TL;DR

**Recommended (A): extend Cue's own dashboard** (`web/` SPA + `hermes_cli/web_server.py` +
`web_routers/` over `hermes_state`) as the native home of the main thread, with drill-down
into every non-main session, and **deep-links into OpenChamber** for opencode build-agent
sessions — the one session class OpenChamber renders natively and Cue should not re-render.

Option B (OC provider D+B1) is a complement, not a primary surface: it fails R2 and R4
**structurally**, not by effort — OpenChamber can only render opencode sessions, and its
approval cards are opencode tool permissions. The decisive evidence is in §4.

## 1. Requirements (PLAN.md Phase 6, frozen)

- **R1** main-thread home — full continuous transcript, any device.
- **R2** session drill-down — list + transcripts of every non-main session (worker agents,
  delegations, cron runs, build agents).
- **R3** live streaming views of active sessions (main + workers) — OpenChamber-class live quality.
- **R4** interaction from the surface — send = lands in main thread; resolve decisions; approvals.

## 2. What exists today (verified in this tree)

### 2.1 The dashboard already renders sessions and transcripts (R1/R2 substrate)

`hermes_cli/web_server.py` (FastAPI; routers mounted at lines 1031–1069, one file per surface)
+ `hermes_cli/web_routers/sessions.py`:

| Endpoint | Gives |
|---|---|
| `GET /api/sessions` | paged list, `is_active` (300 s window), compression-chain collapse to the tip id, subagent scoping (`hermes_cli/session_listing.py::subagent_listing_scope`, gated by `sessions.show_subagents`), source filters, profile stamping |
| `GET /api/sessions/search` | ID + FTS5 content + title search, deduped by compression lineage |
| `GET /api/sessions/{id}/messages` | **paged transcript**, `include_compacted`, `include_ancestors` (matches the gateway's `session.resume` projection), display projection (`_project_for_display`: tool-call labels, steer text, compaction summaries) |
| `GET /api/sessions/{id}/timeline` + `/messages/around` | prompt-addressed bounded pages |
| `GET /api/sessions/{id}/export` | streaming JSON export |
| `PATCH/DELETE /api/sessions/{id}`, bulk-delete, prune, import, stats | management |

Cron runs: `hermes_cli/web_routers/cron.py` — `GET /api/cron/jobs`, `/api/cron/jobs/{id}/runs`,
pause/resume/trigger, plus `cron_run_scheduler_owned()` (scheduler-owned liveness for run
sessions, used by the sessions detail endpoint). The SPA already classifies sources
(`web/src/pages/SessionsPage_sources.tsx`: `cron`, `tool`, `subagent`-style automation sources,
`api_server`, …) and **`SessionsPage.tsx` already renders transcripts inline** (`MessageList`
fed by `/api/sessions/{id}/messages`, lines ~358–680).

### 2.2 The live rails already exist — including a cross-process push signal

- **The dashboard embeds the REAL TUI** (`web/AGENTS.md`): `web/src/pages/ChatPage.tsx` mounts
  xterm.js over `/api/pty` (WS → `ptyprocess` PTY running `hermes --tui`; keep-alive registry,
  reattach by token, channel markers). Anything added to Ink shows up here automatically.
- **`/api/ws` is the full tui_gateway JSON-RPC surface** (`hermes_cli/web_routers/chat_ws.py` →
  `tui_gateway/ws.py::handle_ws`, which "reuses `tui_gateway.server.dispatch` verbatim so every
  RPC, slash command, approval flow and agent event takes the same handlers as Ink over stdio").
  That includes token streaming: `message.delta` / `reasoning.delta` are coalesced at ~30 fps
  (`_STREAMING_EVENT_TYPES`, `_TOKEN_COALESCE_S = 0.033`), TCP_NODELAY + keepalive on the socket —
  the same machinery the Desktop app consumes. Also `tool.start/generating/complete`,
  server→client `approval`/`clarify`/`sudo`/`secret` requests, `session.events.since` replay for
  reconnects, and **live subagent drill-down primitives: `subagent.list` / `subagent.tail`
  (last 16 KiB of a live child's transcript) / `subagent.steer` / `subagent.interrupt`**
  (`tui_gateway/AGENTS.md` § Shared subagent snapshots).
- **`/api/pub` + `/api/events`**: the PTY child's gateway emits every dispatcher frame into
  `/api/pub`; the dashboard fans them out to `/api/events` subscribers per channel
  (`chat_ws.py::_broadcast_event`). `web/src/components/ChatSidebar.tsx` already subscribes.
- **Cross-process change push**: `tui_gateway/change_watcher.py` watches state.db (per profile,
  0.5 s debounce, activity-heartbeat writes deliberately excluded) and broadcasts `sessions.changed`
  globally (`tui_gateway/contracts/events.py:742`). A dashboard client is told to refetch when the
  **messaging gateway** writes a turn — no polling loop required as the primary mechanism.

### 2.3 The main thread is already a first-class window on every surface

- `hermes_cli/cli_init_mixin.py::_resolve_startup_session_id` (lines 323–342): a plain CLI/TUI
  launch **adopts the profile's ONE main thread** via
  `hermes_cli/main_thread.py::resolve_main_thread_session` (get-or-create through the durable
  `gateway_routing` index) and **replays its stored transcript** (`get_messages_as_conversation`)
  — "the same conversation the gateway serves". The dashboard's `/chat` PTY child therefore IS a
  live, token-streaming window into the main thread today, with slash commands and approvals.
- All platforms collapse to one key (`gateway/session.py::build_session_key` →
  `agent:<ns>:main-thread`); cron (`cron/main_thread.py`), the proactive loop
  (`gateway/run_proactive.py`) and mirrors converge there. Concurrency is owned by
  `session_turn_leases` (state.db table, per-conversation single-writer) and the gateway's agent
  cache/eviction paths.
- Persistence cadence (matters for R3): gateway turns are written **per turn/round** through
  `gateway/session_transcript.py` → `SessionDB.append_message`; token-level streaming exists only
  in the owning process's transport. Mid-turn liveness is projected as
  `sessions.last_activity_at/_description` (rate-limited to ≥60 s durable stamps,
  `agent/session_activity.py`).

### 2.4 `api_server` — an existing HTTP ingress whose turns run in the gateway process

`gateway/platforms/api_server.py` (aiohttp, port 8642, `API_SERVER_KEY`, `/p/<profile>/` under
multiplex) + `api_server_openai_routes.py`:

- `POST /v1/chat/completions` — with `X-Hermes-Session-Id` the body's replayed window is
  **discarded**; history loads from state.db (`get_messages_as_conversation`, line 712) and only
  the last message runs as the turn. `X-Hermes-Session-Key` pins the conversation across
  compression rotations. SSE streaming supported (`stream: true`).
- `POST /api/sessions/{id}/chat` + `/chat/stream` — session-scoped chat, streaming variant
  (`session_chat_streaming` capability flag).
- `/v1/runs` + `GET /v1/runs/{id}/events` (SSE), `POST …/approval|steer|stop` — runs with
  approval/steer/stop semantics (`_STATIC_FEATURE_FLAGS`).

A web-originated turn through api_server lands in the main thread with full main-thread
semantics (busy-queuing via the same store/lease machinery) — no new brain, no relay.

### 2.5 State schema (hermes_state_common.py SCHEMA_SQL)

`sessions` (id, source, `session_key`, `parent_session_id`, tokens/cost, title,
`last_activity_at/_description/_provenance`, `profile_name`, archived/pinned/hidden),
`messages` (AUTOINCREMENT rowid → keyset tailing; role/content/tool_calls, reasoning,
`display_kind/_metadata/_order`, active/compacted flags; FTS5 tables),
`gateway_routing` (key → live session id, survives compression rotation),
`session_turn_leases`, `async_delegations` (durable delegation state + `result_json` +
delivery bookkeeping — R2's delegation drill-down data, not yet surfaced over REST),
`gateway_heartbeats` (backend liveness).

### 2.6 OpenChamber render path (verified read-only)

`opencode.db`: `project` → `worktree` (directory, strategy) → `session_v2` (`directory`,
`parent_id` child/subtask nodes, cost/token columns); `session_message` types: assistant (24 007
rows on this install), user, synthetic, idle, system, agent-switched/model-switched/
location-switched, compaction. The Cue project's OpenChamber-managed worktrees
(`~/.local/share/opencode/worktree/<sha>/<branch>`, incl. this branch's session) group natively;
child sessions render as subtask nodes ("Adversarial review round 1" etc.). Live UI rides the
service's SSE bus (`GET /api/event`) — v2 **preview** API (`0.0.0-beta-…`), expect churn
(OC-FRONTEND-INVESTIGATION.md §1). `settings.json`: projects (incl. `/home/af/cue`),
`permissionDefaultMode`, per-session `permissionAutoAccept`, private phone relay
(`wss://relay.openchamber.dev/ws`).

## 3. Option A — extend the dashboard: gap analysis and size

| Req | Already there | Gap | Size |
|---|---|---|---|
| **R1** main-thread home | `/chat` embeds the real TUI which **adopts the main thread with transcript replay** (§2.3); full paged transcript over REST incl. compression ancestors; `sessions.changed` push | Make the thread the explicit landing surface; a tiny resolver endpoint (routing key → live session id) so the SPA doesn't guess after rotation; any-device access posture (auth token exists — same `_SESSION_TOKEN` scheme for REST+WS; loopback-by-default binding is the constraint to document) | **Small** — SPA routing/composition + ~1 endpoint (~50–100 LOC). The transcript itself is NOT rebuilt in React (see invariants) |
| **R2** drill-down | Sessions list + transcripts incl. subagents and cron runs (§2.1); `parent_session_id` on every row; `async_delegations` table exists | Lineage tree view; a `delegations` REST router over `async_delegations` (~100–150 LOC); OC deep-links for sessions whose cwd is an opencode worktree (map directory → OC project; **OC's in-app session URL scheme must be verified at build time** — fallback: link to the project tree) | **Small–medium** |
| **R3** live views | Token-level live for dashboard-driven turns (PTY + `/api/ws` + `/api/events`); `subagent.list/tail/steer` for the live session's children; cross-process `sessions.changed`; turn-level rows for everything else | A read-only live-tail endpoint for arbitrary sessions — SSE keyset-tailing `messages` rows + activity stamps (~150–250 LOC, reuses `_project_for_display`); SPA live-follow hook. **Token-level for gateway-originated turns (Telegram/proactive/cron driving the main thread)** is the one real new seam: either accept turn-level for foreign turns at first, or add a small gateway stream-tap (a plugin/`gateway/builtin_hooks` seam publishing deltas to a bounded ring the dashboard tails; ~200–400 LOC) | **Medium** — SSE tail is contained; the token-tap is optional and incremental |
| **R4** interaction | Send: embedded TUI composer (primary, sanctioned) and `api_server /api/sessions/{id}/chat[/stream]` (headless/secondary — turns run in the gateway process); approvals: TUI server-requests + api_server run approval endpoints; `/decisions` parser shared by every surface (`hermes_cli/decision_command.py`) | Dashboard cards for decisions (+ goals) delegating to the shared dispatchers — the exact pattern CLI/gateway/TUI already use (~100–150 LOC router + SPA cards) | **Small** |

Total: roughly 1–2 focused weeks to R1+R2+R4 + turn-level R3; the token-level foreign-turn tap
is a separate, optional increment.

**Invariants respected by this path:** per-conversation prompt caching is untouched (the web
view is read-only plus existing ingress seams; no system-prompt/toolset mutation mid-conversation);
`web/AGENTS.md`'s "do not re-implement the primary chat experience in React" holds — structured
panels complement the embedded TUI; every new route/WS uses the same session-token auth; one
router file per surface; no direct `opencode.db` writes (links only).

## 4. Option B — OpenChamber provider (D+B1): fails R2/R4 structurally

The Phase 5 investigation already verified the mechanism works (§4.D): provider `cue` →
`http://127.0.0.1:8642/v1`, one dedicated OC session, `X-Hermes-Session-Id`/`-Key` pinning —
this codebase confirms the header semantics in `api_server_openai_routes.py` (§2.4). But mapped
against Phase 6's requirements:

- **R2 is impossible in B.** OpenChamber renders **opencode sessions**; Cue's worker agents,
  delegations and cron runs are Cue sessions in `state.db`. OC's own DB shows the render graph is
  `project/worktree/session_v2` — there is no construct for a foreign store's sessions. Covering
  R2 in OC means a synthetic mirror (`POST /api/session/{id}/synthetic`) **per session**, each
  with drift-while-down, supervision, and beta-API churn — re-implementing the dashboard badly,
  outside the repo.
- **R4 approvals/decisions cannot ride OC.** OC approval cards are **opencode tool permissions**
  (`permission` table, `permissionAutoAccept`) — Cue's decision queue (`decisions.yaml`,
  `hermes_cli/decision_command.py`) is a different construct; the investigation already rules
  this out (§4.B1). Input must go through the provider or the TUI/CLI anyway.
- **R1 is partial.** The OC session shows only OC-originated turns; Telegram/proactive/cron
  traffic appears only via the B1 mirror as whole synthetic messages. "Full continuous
  transcript" degrades to "full, except when the mirror was down, plus delayed".
- **Cache tax + coupling.** opencode sends its agent's system prompt; api_server layers client
  system messages **on top of core** per request (`ephemeral_system_prompt=system_prompt`,
  `_handle_chat_completions` line 732) — a per-turn prompt delta on the ONE cached main-thread
  prefix, plus a coding-harness prompt injected into the relationship. Fixing it needs a
  no-system-prompt OC agent config (unverified) or accepting the tax. The API is v2-preview;
  provider header support is unverified (investigation §Appendix A).
- **What B remains genuinely good at:** opencode build-agent sessions — exactly what OC renders
  natively with subtask nodes, diffs, and phone relay. That is why A *deep-links* there instead
  of competing with it.

## 5. Recommendation

**Recommended (A).** The dashboard already owns every hard part of the Cue side: the session
store + REST transcript surface, the embedded real TUI that adopts the main thread, the
tui_gateway JSON-RPC live rails (token streaming, approvals, subagent tail/steer), a
cross-process `sessions.changed` push, and an api_server ingress whose turns run in the gateway
process. Phase 6 under A is composition plus two contained new seams (live-tail SSE; a
delegations router), each at an existing edge. Under B, R2 and R4 have no native answer at any
effort level, and R1 inherits a per-turn cache tax on the single most expensive cached prefix in
the system. OpenChamber stays what it is best at — the live view for opencode build agents —
reached by deep-links from Cue's session drill-down.

## 6. Milestones (recommended path)

1. **M1 — Main-thread home (R1).** `/` lands on the Thread surface: structured header/cards +
   the embedded TUI as the interaction pane; small `main_thread` resolver endpoint
   (`gateway_routing` → live session id, reuse `gateway/session.py` helpers); transcript pages
   over the existing messages endpoint; `sessions.changed`-driven refetch.
2. **M2 — Drill-down (R2).** Lineage tree (`parent_session_id` + `async_delegations` router);
   cron-run cross-links (endpoints exist); OC deep-link for opencode-worktree sessions
   (verify OC URL scheme; fallback to project link).
3. **M3 — Live views (R3).** Read-only SSE session tail (`/api/sessions/{id}/live`, keyset over
   `messages` + activity stamps, `_project_for_display` reuse); wire Thread + drill-down to it;
   token-level already flows for dashboard-driven turns. Optional follow-up: gateway stream-tap
   (plugin/hook seam) for token-level foreign-origin turns.
4. **M4 — Interaction (R4).** Decision + goal cards over the shared dispatchers
   (`decision_command.py` / `goal_command.py` pattern — no surface-specific parsers); send stays
   TUI-first with `api_server` session_chat as the headless path; approval cards reuse the
   server-request flow the Desktop already renders.

Each milestone lands behind the existing auth scheme, one router file per new surface, with
behaviour tests in `tests/hermes_cli/` (routers) and vitest (SPA).

## 7. Open items to verify at build time

- OpenChamber deep-link URL scheme for a specific session (port 3000 UI; `share_url` column
  exists in `session_v2` but no verified route format) — fallback: project-tree link.
- Whether synthetic appends emit SSE events the OC UI streams live (carried over, investigation
  §Appendix A) — only relevant if a mirror is ever wanted for its own sake.
- Any-device posture for R1: loopback default today; remote access needs a documented story
  (reverse proxy / `serve` + desktop) — product decision, not a code gap.

## Appendix — evidence index

- `hermes_cli/web_server.py` (router mounting 1031–1069), `web_routers/sessions.py`,
  `web_routers/chat_ws.py`, `web_routers/cron.py`, `hermes_cli/session_listing.py`
- `tui_gateway/ws.py` (`handle_ws`, `_STREAMING_EVENT_TYPES`, coalescing),
  `tui_gateway/AGENTS.md` (subagent.list/tail/steer; server→client requests; contracts),
  `tui_gateway/change_watcher.py` + `contracts/events.py:742`
- `hermes_cli/cli_init_mixin.py:323–342`, `hermes_cli/main_thread.py`,
  `gateway/session.py` (key collapse), `gateway/session_transcript.py` (turn-level persistence),
  `agent/session_activity.py` (≥60 s durable stamps)
- `gateway/platforms/api_server.py` (route/capability tables), `api_server_openai_routes.py`
  (`_handle_chat_completions` 636–744: header pinning, history override, ephemeral system prompt)
- `hermes_state_common.py` SCHEMA_SQL (sessions/messages/gateway_routing/session_turn_leases/
  async_delegations/gateway_heartbeats)
- `web/src/pages/SessionsPage.tsx` (transcript rendering), `SessionsPage_sources.tsx`,
  `web/src/components/ChatSidebar.tsx`, `web/AGENTS.md`
- Read-only: `~/.config/openchamber/settings.json`;
  `sqlite3 file:/home/af/.local/share/opencode/opencode.db?mode=ro` (tables, `session_v2` schema
  + cue-project rows, `session_message` type census, `worktree` rows)
- OC-FRONTEND-INVESTIGATION.md (Phase 5, same repo) — options A/B1/B2/C/D, caveats, NO-GO list

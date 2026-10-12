# OpenChamber as a front-end for the main thread — investigation

Phase 5, item 2 (PLAN.md): *"Optional: OpenChamber as a front-end for the main thread
(agent-tool/relay investigation)"*. Investigation only — no gateway or core code changed on
this branch. Evidence was gathered read-only on 2026-10-12 (branch `feat/phase-5-oc-frontend`):
OpenChamber settings, a read-only peek into the opencode DB, the live opencode service's
OpenAPI document, and Cue's own source/state. Appendix A lists what was actually run.

## TL;DR

**Verdict: LATER.** OpenChamber can technically become a real front-end for the ONE main
thread using only sanctioned seams on both sides, but nothing is missing today: the main
thread already has a primary front-end (Telegram) that Phases 3–4 were built around
(decision escalation pings, batched proactive reports, `/decisions`), plus the CLI/TUI
windows. OpenChamber's marginal value is "the UI I already live in while developing Cue" —
real but not worth new moving parts until it is.

When it becomes pressing, the ladder is:

1. **First step (GO when wanted): read-only MCP surface** — a tiny standalone MCP server
   exposing main-thread tail/search, decisions, and goals as tools, registered with
   `opencode2 mcp add cue --url …`. No gateway/core change, no second brain, no prompt-cache
   impact. ~a day.
2. **True front-end (LATER): OpenChamber *provider* → Cue's existing `api_server`** — a
   dedicated OpenChamber session whose "model" is Cue (`/v1/chat/completions` with the
   `X-Hermes-Session-Id`/`X-Hermes-Session-Key` headers pinning the main thread). Turns
   genuinely run in the main thread with its own history; no relay brain, no double model.
   Two caveats to solve at build time (§4.D).
3. **Optional view-completer: one-way mirror relay** — push non-OpenChamber main-thread
   traffic into that same opencode session as synthetic messages via the service API, so
   the OpenChamber window shows the whole relationship, not just its own turns.

**Permanent NO-GO:** an opencode session pointed at `HERMES_HOME` as a "window" (§4.A), a
two-way forwarding pipe (§4.B2), and any direct write to `opencode.db` from outside the
opencode service (§3).

## 1. What OpenChamber is (verified)

OpenChamber (Alex's harness app, UI on port 3000) is an **opencode-native** client: it
manages the `opencode` binary (`~/.opencode/bin/opencode`, v2.0.26 here), launches work via
the `opencode2` CLI, and renders whatever the **opencode background service** owns
(discover/start through `opencode2 service`; `opencode2 api` speaks to it).

Facts read from `~/.config/openchamber/settings.json` and a read-only peek at
`/home/af/.local/share/opencode/opencode.db` (the service's storage):

- **Projects → worktrees → sessions.** `project` rows (e.g. `/home/af/cue`) own `worktree`
  rows `(project_id, directory, strategy)`; `session_v2` rows carry a `directory` and group
  under it. The Cue project already hosts the OpenChamber-managed worktrees
  (`~/.local/share/opencode/worktree/<sha>/<branch>` — this very investigation runs in one).
  Child sessions (`parent_id`) render as subtask nodes (adversarial-review rounds etc.).
- **The message stream** is `session_message` (`type` ∈ user / assistant / synthetic /
  idle / system / compaction / agent-switched / model-switched / location-switched;
  assistant `data` carries `content[]` parts incl. reasoning). ~24k assistant rows on this
  install.
- **Steering/queueing:** `session_inbox` with `delivery` = `steer`|`queue` — how input lands
  in a *running* session. Exposed as `POST /api/session/{id}/inbox/{inboxID}/steer|queue`.
- **Approvals:** the `permission` table (`action` like `external_directory`, `resource`
  glob) plus per-session auto-accept in settings (`permissionDefaultMode`,
  `permissionAutoAccept.sessions`). These are *opencode tool* permissions — not a generic
  approval queue (see §4.B1 for why Cue decisions can't ride them).
- **Notifications + phone:** settings show `notifyOn{Completion,Error,Question,Subtasks}`
  and a private relay (`wss://relay.openchamber.dev/ws`, E2E keys in settings — not
  reproduced here) that mirrors sessions to the phone app.

The service's HTTP API (from the live `/openapi.json`) is the sanctioned programmatic
surface — the relevant endpoints for us:

```
GET  /api/event                              # SSE bus the UI subscribes to
POST /api/session                            # create session
GET  /api/session/{id}/message               # read the stream
POST /api/session/{id}/synthetic             # append a message WITHOUT running a model turn
GET  /api/session/{id}/export                # / POST /api/session/import  (backfill path)
POST /api/session/{id}/prompt                # run a real opencode agent turn (the wrong brain)
GET/POST /api/session/{id}/permission/...    # approvals
PUT  /api/mcp/{server}                       # register an MCP server = inject tools
```

Caveat: this is the **v2 preview** API (`0.0.0-beta-…`); expect churn and re-verify against
`/openapi.json` at build time.

## 2. What the main thread is (verified in this repo)

- **One session per profile**: `gateway/session.py::build_session_key` collapses every
  platform/chat/thread/participant to `agent:<ns>:main-thread`
  (`main_thread_session_key`). Adapters, cron (`cron/main_thread.py`), the proactive loop
  (`gateway/run_proactive.py`) and cross-platform mirrors (`gateway/mirror.py`) all converge
  there; egress still reaches the exact chat via the live origin.
- **Durable state**: routing index `sessions.json` + `gateway_routing` table (key → live
  `session_id`, survives compression rotation), transcript in `state.db`
  (`sessions` + `messages` + FTS), rotated topics under `<home>/archive/main-thread/`
  (`agent/context_rotation.py`, `tools/session_search_archive.py`).
- **Every window already delegates to shared seams**: CLI adopts the main thread
  (`hermes_cli/main_thread.py`), `/decisions` and `/goal` parsing live in
  `hermes_cli/decision_command.py` / `goal_command.py` and every surface (CLI, gateway,
  TUI, dashboard) delegates — a new surface would too.
- **Cue already ships an OpenAI-compatible ingress**: the `api_server` gateway platform
  (`gateway/platforms/api_server.py`, port 8642, `API_SERVER_KEY`, `/p/<profile>/` under
  multiplex) serving `/v1/chat/completions`, `/v1/responses`, `/api/sessions/{id}/chat`,
  `/v1/runs` (+ approvals). This is load-bearing for §4.D.

## 3. The impedance mismatch

OpenChamber renders **opencode sessions**; the main thread is a **Cue session**. No amount
of pointing changes that: OpenChamber's session view, message stream, and approvals are
opencode-service constructs. So "OpenChamber as a front-end" means one of:

- make an opencode session's *content* be the main thread (mirror: sanctioned writes via
  the service API — never direct `opencode.db` writes, which the service owns and the
  troubleshooting docs forbid), or
- make an opencode session's *turns* run Cue's brain (provider → `api_server`), or
- don't fight it: give opencode sessions **tools** that read/act on the main thread (MCP).

None of these touch the main thread's system prompt or toolset mid-conversation, so
**per-conversation prompt caching stays intact in every option** — all integration happens
at existing ingress/egress seams.

## 4. Options

### A. An opencode session pointed at the profile's HERMES_HOME — NO-GO

What it actually is: a *foreign opencode agent* whose cwd is `~/.cuehome` (needs an
`external_directory` permission grant — the exact rows seen in the `permission` table),
reading `state.db`/`sessions.json`/archive through shell tools. The OpenChamber message
stream would show that agent's own conversation, **not** the main thread; there is no
native way to render another store's transcript. Strictly dominated by the MCP option
(structured tools, no directory grant, no illusion of being a window). Reject.

### B1. One-way mirror relay (Cue → opencode session) — viable, LATER; the view-completer

A small Cue-side process tails the main thread (poll `state.db` `messages` for the live
session id, or subscribe to the archive rotation) and appends turns into a dedicated
opencode session via the service API: `POST /api/session` once (directory = the cue
project, so it groups with the dev worktrees), then `POST /api/session/{id}/synthetic` per
turn. Zero model burn on either side (synthetic rows don't run turns); the SSE bus should
make it live (verify). Backfill via `GET …/export` / `POST /api/session/import` or replay
from the archive JSONL.

- **Gives:** OpenChamber's real UI over the ONE thread — desktop *and* phone via its
  private relay, history search, notifications — without a second brain.
- **Costs:** a supervised process (cron? gateway plugin? — decision at build time); drift
  while it's down; API churn hedging; **the input box is a footgun**: anything typed runs a
  real opencode agent turn against the mirrored session (`POST …/prompt` is one Enter
  away). Mitigate by pairing with §4.D (input goes to Cue) or accepting view-only
  discipline (fragile).
- **Decisions can't ride OpenChamber approval cards**: those are opencode tool-permission
  requests; Cue's decision queue is `decisions.yaml` + `/decisions`. A mirror shows decision
  pings as *text*. Structured resolution stays an MCP/tool affair (§4.C).

### B2. Two-way forwarding pipe (opencode agent forwards to Cue) — permanent NO-GO

Give the mirrored session an opencode agent whose only job is to relay prompts to Cue and
re-emit replies. It *works*, but: every message burns a **second model call** (the pipe must
reproduce the reply text), the pipe can silently edit/reformat/truncate the relationship,
latency doubles, and the main thread's turn semantics (busy-queuing, `/stop`, approvals,
batched outbox) would be re-implemented — badly — outside the gateway, which is exactly
where the root AGENTS.md says capability must not accrete. The provider option (§4.D)
achieves the same with zero of these costs.

### C. Cue agent-tool surface over MCP — the first step

A tiny standalone MCP server (new edge process, not core: the footprint ladder's "MCP
server" rung) registered with `opencode2 mcp add cue --url http://127.0.0.1:PORT/mcp`
(`PUT /api/mcp/{server}` under the hood). Tools, mapping 1:1 onto existing seams:

| tool | seam it delegates to |
|---|---|
| `main_thread_tail` / `main_thread_search` | read-only `state.db` `messages` + FTS / `<home>/archive/` |
| `decisions_list` / `decisions_resolve` | `hermes_cli/decision_command.py` (same parser as every surface) |
| `goals_read` / `goals_review` | `tools/goals_tool.py` store |
| `ask` (optional, fire a turn into the main thread) | `cue chat -q` one-shot (adopts the main thread) or `api_server` |

- **Gives:** any OpenChamber session — including the dev sessions building Cue — can read
  the live relationship and act on it; the user can interrogate the main thread from
  wherever they already are. This is the "agent-tool" half of the PLAN item.
- **Costs:** one small service to run; read-only paths are trivially safe, `ask`/resolve
  are write paths that deserve the same authz thought as the CLI (it runs on-loopback, same
  trust boundary as `api_server`'s 127.0.0.1 bind).
- **Explicitly not:** a front-end. The opencode session remains the consumer, not the
  window.

### D. OpenChamber provider → Cue `api_server` — the real front-end, LATER

opencode supports custom providers (baseURL + headers). Define provider `cue` →
`http://127.0.0.1:8642/v1` with `API_SERVER_KEY`, and point one dedicated OpenChamber
session's model at it. The statefulness is already right (verified in
`gateway/platforms/api_server_openai_routes.py::_handle_chat_completions`):

- With `X-Hermes-Session-Id: <main-thread session id>` the request body's replayed window
  is **discarded**; history loads from `state.db` (`get_messages_as_conversation`) and only
  the **last message** runs as the turn. With `X-Hermes-Session-Key:
  agent:<ns>:main-thread` the row is bound to the conversation across compression
  rotations. opencode re-sending its window per request is therefore harmless.
- The turn appends to the main thread like any platform turn — Telegram, CLI and
  OpenChamber converge on ONE transcript, which is the whole Cue thesis. Streaming works
  (`stream: true` SSE). Per-profile wiring via `/p/<profile>/v1/…` under multiplex.

**Two caveats to solve at build time:**

1. **Ephemeral system prompt.** opencode sends its agent's system prompt; the api_server
   layers client system messages *on top of core* per request (`ephemeral_system_prompt`).
   For a main-thread turn that is both a cache-buster (per-turn prompt delta on an
   otherwise stable cached prefix) and relationship pollution (a coding-harness prompt
   injected into the ONE thread). Needs a no-system-prompt opencode agent config for the
   `cue` provider, or an explicit decision to accept the tax. **This is the reason D is
   LATER, not GO.**
2. **Partial view.** The OpenChamber session shows only OpenChamber-originated turns
   (Cue's replies stream back, but Telegram/proactive/cron traffic never enters the
   opencode session). Compose with B1 (mirror non-OpenChamber traffic into the same session
   as synthetic messages; filter what the provider already showed) for the full window.

Minor cosmetics: token/cost stats in OpenChamber would attribute Cue's usage to the fake
`cue` "model"; tool calls happen inside Cue and are invisible in the opencode stream (the
reply text is the interface).

## 5. Cost/benefit summary

| option | build | run cost | correctness risks | value |
|---|---|---|---|---|
| A cwd-window | ~0 | a foreign agent | illusion of a window | ≈0 |
| B1 mirror | small-med | 1 supervised proc; 0 tokens | drift, input-box footgun, API churn | rich view + phone |
| B2 pipe | medium | **2× model burn** | pipe edits the relationship; gateway logic duplicated | true but degraded I/O |
| **C MCP tools** | **~1 day** | 1 tiny proc | low (read-mostly) | dev-workflow integration |
| D provider | small | 0 extra tokens | ephemeral-system-prompt cache tax; partial view | **true front-end** |

## 6. Recommendation

- **LATER** on "OpenChamber as a front-end": Telegram already is the front-end the
  proactive/decision machinery delivers to; OpenChamber adds dev convenience, not a
  missing capability. Revisit when either (a) the phone/desktop Telegram experience stops
  being enough, or (b) D's system-prompt caveat gets a clean answer.
- **When it becomes pressing: GO on C first** (read-only MCP surface; §7), then **D (+B1)**
  if a true front-end is still wanted.
- **NO-GO, permanently:** A (cwd window), B2 (forwarding pipe), direct `opencode.db`
  writes from outside the service, and any variant that mutates the main thread's prompt or
  toolset mid-conversation (prompt-cache sanctity).

## 7. Smallest viable first step (spec for C, when GO)

1. New `services/cue_mcp/` (or plugin-catalog entry): stdio/HTTP MCP server, ~200 lines:
   `main_thread_tail(n)`, `main_thread_search(q)`, `decisions_list()`,
   `decisions_resolve(id, answer)`, `goals_read()`. Read-only opens of
   `<home>/state.db` (`mode=ro`) + `<home>/decisions.yaml` / `goals.yaml`; writes only
   through the shared dispatchers (`hermes_cli/decision_command.py`), never bespoke
   parsers.
2. Register once: `opencode2 mcp add cue --url http://127.0.0.1:<port>/mcp` — every
   OpenChamber session (dev worktrees included) immediately gets the tools.
3. One invariant test: dispatcher delegation (a resolve through MCP writes the same
   decisions.yaml transition `/decisions` produces). No gateway/core files touched.
4. Explicitly out of scope at this rung: `ask` (turn injection) — add only with the CLI
   one-shot's authz story.

## Appendix A — evidence (all read-only)

- `~/.config/openchamber/settings.json` — opencode binary/projects/worktrees, per-session
  `permissionAutoAccept`, `notifyOn*`, private relay URL (keys exist in the file; not
  reproduced here).
- `sqlite3 'file:…/opencode/opencode.db?mode=ro'` — schemas + rows of `session_v2`,
  `worktree`, `session_message` (type census + user/assistant `data` shapes),
  `session_inbox` (`delivery='steer'`), `permission` (`external_directory` globs),
  `session_pending`, `project`, `workspace`.
- `opencode2 api get /openapi.json` on the live service — endpoint list quoted in §1
  (`/api/event`, `/api/session…`, `/synthetic`, `/inbox/*/steer`, `/permission/…`,
  `PUT /api/mcp/{server}`); `opencode2 --help` (subcommands incl. `acp`, `export`,
  `import`, `mcp`, `service`).
- `~/.cuehome/state.db` (mode=ro) — `sessions`/`messages`/`gateway_routing` schemas; the
  live `agent:main:main-thread` row and its routing entry.
- Repo: `gateway/session.py` (key collapse), `gateway/mirror.py`, `cron/main_thread.py`,
  `hermes_cli/main_thread.py`, `gateway/platforms/api_server.py` (`session_key_header`,
  `_declared_conversation_session`, `_handle_session_chat` statefulness) and
  `…/api_server_openai_routes.py` (`_handle_chat_completions`: last-message-as-turn,
  header-pinned history override at the `get_messages_as_conversation` call),
  `hermes_cli/config_defaults.py` (`gateway.api_server` block).

Not verified (flagged inline): whether synthetic appends emit SSE events the OpenChamber
UI streams live; whether opencode provider config carries arbitrary headers; whether a
service-created session auto-appears in the cue project's session tree (expected — it is
directory-keyed; confirm at build time).

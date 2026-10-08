# PLAN — Cue

A fork of Hermes Agent with the session model inverted: one continuous main thread instead of many disposable ones, goals as first-class state, and proactive decision surfacing.

## ⚡ START HERE (for agent sessions working in this repo)

- **This file is the source of truth.** Read it fully before working.
- Work the **lowest-numbered phase with unchecked items**. One phase per branch: `feat/phase-0-foundation`, etc.
- Tick checkboxes in this file **as items complete** — the orchestrator watches this file for progress.
- Commit per logical unit, `type: concise subject` (see AGENTS.md conventions).
- **NEVER touch `~/.hermes`** — always run with `HERMES_HOME=~/.cuehome` (see Gotchas).

## Core thesis

Threads are a computer concept, not a human one. You don't open a new conversation with a friend when the topic changes — the topic changes *inside* the relationship. Same here: one main thread, context rotates, the relationship accumulates.

## The three pillars

### 1. One main thread (no sessions/threads in UX)
- Internally, keep Hermes' session storage as plumbing. User-facing: exactly ONE persistent conversation.
- All platforms attach to the same main session — Telegram, OpenChamber, CLI are windows into one relationship, not separate threads.
- `/new` → repurposed as **context rotation**: compress the current topic into durable artifacts (memory notes, project docs, decision log) and keep going. No fresh start.
- Cron/webhook deliveries land in the main thread instead of spawning new sessions.
- Compression is retuned for topic rotation: when a topic ends, its raw history is droppable because its conclusions live in artifacts.
- session_search becomes archive search over the rolling transcript + artifacts.

### 2. Goals as first-class state
- `goals.yaml` (or table in state.db): statement, why, status, next_action, progress log, review cadence.
- Compact goals block injected into the system prompt (updated only on change — respect prompt caching).
- Agent orients every piece of work toward goals; drift detection flags work that serves no goal.
- Goal review cadence: daily summary + weekly deep review, delivered to the main thread.
- Background autonomous work: heartbeat/cron picks next actions on active goals and executes them.

### 3. Decision surfacing
- Pending-decision queue: when the agent hits a fork in the road, it logs options + its recommendation + impact, and pings the user (Telegram).
- Never silently stall: blocked work gets a logged decision, then the agent moves to other work.
- `/decisions` lists open ones; answers recorded with rationale, work resumes.

## Phases

### Phase 0 — Foundation (isolate + verify)
- [x] Create branch `feat/phase-0-foundation`
- [x] Provision dev env: `source ./activate` with `HERMES_HOME=~/.cuehome` (and isolated `HERMES_RUNTIME_DIR`, e.g. `~/.cuehome/runtime`)
- [x] Verify isolation: one-shot query (`hermes chat -q "ping"`) works AND `~/.hermes` is untouched
- [x] User-facing CLI rename: `hermes` → `cue` entry point (console script, banner, help text). **Internal `HERMES_*` env vars and `get_hermes_home()` stay for now** — internal rename is a dedicated later phase, not Phase 0 churn
- [x] `python scripts/check` green
- [x] Commit per logical unit on the phase branch
- [x] Push: `origin` = `ice00cold/cue` (fork live, all branches, push auth verified — 2026-10-08; transfer to af-claw later if wanted)

### Phase 1 — One main thread
- [ ] Gateway: map all chats/platforms → single persistent session ID
- [ ] Disable/hide session-per-chat UX (`/new`, `/resume`, session pickers)
- [ ] Cron + webhook delivery → main thread (no more thread_id targeting)
- [ ] Context rotation: topic-end compression → artifacts; tune compression thresholds
- [ ] Rolling transcript archive for search

### Phase 2 — Goals subsystem
- [x] Goals state store + tools (`goals_read`, `goals_update`, `goals_review`)
- [x] System prompt injection of goals block
- [x] Drift detection in the agent loop
- [x] Review cron → main thread *(built against the current cron delivery surface — see Open decisions #4)*

### Phase 3 — Decision queue
- [ ] Decisions store + tool
- [ ] Telegram ping on new pending decision
- [ ] `/decisions` command + resolution flow

### Phase 4 — Proactive loop
- [ ] Heartbeat does autonomous goal work between conversations
- [ ] Batched reporting (no spam), quiet hours respected
- [ ] Escalation rules: what wakes the user vs what waits

### Phase 5 — OpenChamber + polish
- [ ] Project lives in OpenChamber for dev (phone/desktop)
- [ ] Optional: OpenChamber as a front-end for the main thread (agent-tool/relay investigation)

## Open decisions
1. **Name** — working name `cue` (matches the proactive-AI concept). Rename = `mv` + rebrand + fork name.
2. **Cross-platform main thread** — recommend ONE shared session across all platforms. Alternative: one main thread per platform (Telegram thread ≠ CLI thread).
3. **Subagents/delegation** — keep (they're workers, not threads). Confirm.
4. **Goals review delivery → main thread (Phase 1 integration point).** The review cadence jobs
   (`goals-review-daily` `0 9 * * *`, `goals-review-weekly` `0 9 * * 1`) are installed by
   `tools/goals_tool.py::ensure_goals_review_jobs` on first goal creation, delivering via the
   current cron surface (`deliver=origin` when the creating session carries one, else `local`).
   When Phase 1's main-thread delivery lands: retarget those jobs' delivery to the main session
   id (single seam — job `deliver`/`origin` resolution in `cron/scheduler_delivery.py` or the
   installer itself), and consider mirroring the review into the main transcript the way
   continuable cron deliveries seed the reply-facing conversation.
5. **Goals block staleness across the one main thread.** The system-prompt goals block renders at
   prompt build only (event-driven: goal change → next build boundary; per-turn rebuild would
   break prompt caching). In a Phase 1 world of one eternal main thread, the natural refresh
   boundary is the compression rebuild — confirm that cadence is acceptable or add an explicit
   goals-changed flag that rides the next sanctioned rebuild.

## Gotchas
- The clone defaults to `~/.hermes` — every run script must set `HERMES_HOME` or Phase 0 has already failed.
- System prompt changes break prompt caching — goals block updates should be event-driven (goal change), not per-turn.
- Upstream moves fast — track `upstream` remote, rebase selectively; don't merge blindly.

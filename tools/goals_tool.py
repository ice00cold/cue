"""Goals tools — ``goals_read`` / ``goals_update`` / ``goals_review`` over ``GoalsStore``.

Plain registry tools: goal state is durable per-profile file state, so handlers
resolve ``<home>/goals.yaml`` through ``get_hermes_home()`` at call time (each
served profile gets its own goals). Creating the first goal also installs the
review-cadence cron jobs (daily summary + weekly deep review) against the
current cron delivery surface.
"""

import json
import logging
from typing import Any, Dict, Optional

from tools.goals_store import GoalsStore, validate_goal_fields
from tools.registry import registry, tool_error

logger = logging.getLogger("tools.goals_tool")

GOALS_TOOLSET = "goals"
REVIEW_JOB_DAILY_NAME = "goals-review-daily"
REVIEW_JOB_WEEKLY_NAME = "goals-review-weekly"
REVIEW_JOB_DAILY_SCHEDULE = "0 9 * * *"
REVIEW_JOB_WEEKLY_SCHEDULE = "0 9 * * 1"

# Static prompts for the cadence jobs. They tell the cron-session agent to pull
# structured data from goals_review and write the summary itself — tools return
# data, the model writes prose.
_REVIEW_JOB_PROMPTS = {
    "daily": (
        "You are running Cue's scheduled daily goal review. Call the goals_review tool "
        "with kind=\"daily\", then write a SHORT summary for the user: one line per "
        "active goal covering what moved (progress since the last review) and what is "
        "next, plus one line on drift if any turn work served no goal (quote the drift "
        "summaries). If there are no active goals, say exactly that in one line and "
        "stop. Do not invent progress; only report what the tool returned."
    ),
    "weekly": (
        "You are running Cue's scheduled weekly deep goal review. Call the goals_review "
        "tool with kind=\"weekly\", then write the review for the user: per active goal "
        "— what moved this week, whether the why still holds, whether next_action is "
        "still the right next move; then call out stale goals (no update for 7+ days), "
        "drift patterns, and give one concrete recommendation per goal (keep / adjust "
        "next_action / pause / drop). If there are no goals, say exactly that in one "
        "line and stop. Do not invent progress; only report what the tool returned."
    ),
}

_STATUS_ENUM = ["active", "paused", "done", "dropped"]
_CADENCE_ENUM = ["daily", "weekly"]


def _store() -> GoalsStore:
    return GoalsStore()


def _goal_view(goal: Dict[str, Any], *, progress_tail: int = 10) -> Dict[str, Any]:
    """One goal for tool results: full fields with a recent progress tail."""
    return {**goal, "progress": goal["progress"][-progress_tail:],
            "progress_total": len(goal["progress"])}


def goals_read(goal_id: Optional[str] = None) -> str:
    """Read all goals (or one by id) + drift/review status."""
    store = _store()
    goals = store.read()
    if goal_id:
        wanted = goal_id.strip().lower()
        goals = [g for g in goals if g["id"].lower() == wanted]
        if not goals:
            return tool_error(f"No goal with id '{goal_id}'.", success=False)
    last_daily = store.last_review_at("daily")
    last_weekly = store.last_review_at("weekly")
    return json.dumps({
        "success": True,
        "goals": [_goal_view(g) for g in goals],
        "counts": store.status_counts(),
        "last_daily_review": last_daily.isoformat() if last_daily else None,
        "last_weekly_review": last_weekly.isoformat() if last_weekly else None,
        "drift_events_total": len(store.load()["drift"]),
    }, ensure_ascii=False)


def goals_update(action: str, goal_id: Optional[str] = None, statement: Optional[str] = None,
                 why: Optional[str] = None, status: Optional[str] = None,
                 next_action: Optional[str] = None, review_cadence: Optional[str] = None,
                 note: Optional[str] = None) -> str:
    """Dispatch one goals_update action against the store."""
    store = _store()
    action = (action or "").strip().lower()
    try:
        if action == "create":
            return _action_create(store, statement=statement, why=why, status=status,
                                  next_action=next_action, review_cadence=review_cadence)
        if action == "update":
            return _action_update(store, goal_id=goal_id, statement=statement, why=why,
                                  status=status, next_action=next_action,
                                  review_cadence=review_cadence)
        if action == "log":
            return _action_log(store, goal_id=goal_id, note=note)
        if action == "remove":
            return _action_remove(store, goal_id=goal_id)
        return tool_error(
            f"Unknown action '{action}'. Use create, update, log, or remove.", success=False)
    except KeyError as exc:
        return tool_error(str(exc.args[0]), success=False)
    except ValueError as exc:
        return tool_error(str(exc), success=False)


def _clean(value: Optional[str]) -> str:
    return str(value).strip() if value is not None else ""


def _action_create(store: GoalsStore, *, statement, why, status, next_action, review_cadence) -> str:
    statement, why, next_action = _clean(statement), _clean(why), _clean(next_action)
    status, cadence = _clean(status).lower() or "active", _clean(review_cadence).lower() or "daily"
    error = validate_goal_fields(statement=statement, why=why, next_action=next_action,
                                 status=status, review_cadence=cadence, require_statement=True)
    if error:
        return tool_error(error, success=False)
    goal = store.add_goal(statement=statement, why=why, status=status,
                          next_action=next_action, review_cadence=cadence)
    jobs_note = ensure_goals_review_jobs()
    return json.dumps({"success": True, "goal": _goal_view(goal), "created": True,
                       "message": "Goal created. " + jobs_note}, ensure_ascii=False)


def _action_update(store: GoalsStore, *, goal_id, statement, why, status, next_action, review_cadence) -> str:
    if not _clean(goal_id):
        return tool_error("goal_id is required for action=update.", success=False)
    updates: Dict[str, Any] = {}
    if statement is not None:
        updates["statement"] = _clean(statement)
    if why is not None:
        updates["why"] = _clean(why)
    if status is not None:
        updates["status"] = _clean(status).lower()
    if next_action is not None:
        updates["next_action"] = _clean(next_action)
    if review_cadence is not None:
        updates["review_cadence"] = _clean(review_cadence).lower()
    if not updates:
        return tool_error("Nothing to update: pass at least one field.", success=False)
    error = validate_goal_fields(
        statement=str(updates.get("statement", "")), why=str(updates.get("why", "")),
        next_action=str(updates.get("next_action", "")),
        status=str(updates.get("status", "")),
        review_cadence=str(updates.get("review_cadence", "")),
        require_statement=bool(updates.get("statement")),
    )
    if error:
        return tool_error(error, success=False)
    goal = store.update_goal(_clean(goal_id), updates)
    return json.dumps({"success": True, "goal": _goal_view(goal)}, ensure_ascii=False)


def _action_log(store: GoalsStore, *, goal_id, note) -> str:
    note = _clean(note)
    if not _clean(goal_id):
        return tool_error("goal_id is required for action=log.", success=False)
    if not note:
        return tool_error("note is required for action=log — what moved on this goal?", success=False)
    error = validate_goal_fields(note=note)
    if error:
        return tool_error(error, success=False)
    goal = store.log_progress(_clean(goal_id), note)
    return json.dumps({"success": True, "goal": _goal_view(goal)}, ensure_ascii=False)


def _action_remove(store: GoalsStore, *, goal_id) -> str:
    if not _clean(goal_id):
        return tool_error("goal_id is required for action=remove.", success=False)
    goal = store.remove_goal(_clean(goal_id))
    return json.dumps({"success": True, "removed": goal["id"],
                       "message": "Goal removed. Prefer status=dropped to keep history."},
                      ensure_ascii=False)


def goals_review(kind: str = "daily", record: bool = True) -> str:
    """Assemble review data (the calling model writes the summary prose)."""
    payload = _store().review_payload(kind, record=record)
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def ensure_goals_review_jobs() -> str:
    """Idempotently create the daily/weekly goals review cron jobs for this home.

    Uses the current cron delivery surface (``deliver=origin`` when the creating
    session carries one, else ``local``). When Phase 1's main-thread delivery
    lands, this is the single integration point: retarget delivery to the main
    session here. Returns a short human-readable note for tool results.
    """
    from cron.jobs import list_jobs

    existing = {str(job.get("name") or "") for job in list_jobs(include_disabled=True)}
    created: list[str] = []
    for kind, name, schedule in (
        ("daily", REVIEW_JOB_DAILY_NAME, REVIEW_JOB_DAILY_SCHEDULE),
        ("weekly", REVIEW_JOB_WEEKLY_NAME, REVIEW_JOB_WEEKLY_SCHEDULE),
    ):
        if name in existing:
            continue
        try:
            _create_review_job(name=name, schedule=schedule, prompt=_REVIEW_JOB_PROMPTS[kind])
            created.append(f"{kind} ({schedule})")
        except Exception as exc:
            logger.warning("goals review job '%s' could not be created", name, exc_info=True)
            return f"Warning: review jobs could not be scheduled ({exc})."
    if not created:
        return "Review cadence already scheduled (daily + weekly)."
    return "Review cadence scheduled: " + ", ".join(created) + "."


def _create_review_job(*, name: str, schedule: str, prompt: str) -> Dict[str, Any]:
    from cron.jobs import create_job
    from cron.scheduler import CronSchedulerRegistrationError, create_job_with_scheduler_registration
    from tools.cronjob_job_args import _origin_from_env

    origin = _origin_from_env(schedule)
    try:
        return create_job_with_scheduler_registration(
            prompt=prompt, schedule=schedule, name=name, origin=origin)
    except CronSchedulerRegistrationError as exc:
        # The job is persisted; only its external trigger registration failed. The
        # built-in ticker reads the store, so keep the job and log rather than fail.
        logger.warning("goals review job '%s' persisted but not registered: %s", name, exc)
        return create_job(
            prompt=prompt, schedule=schedule, name=name, origin=origin)


def check_goals_requirements() -> bool:
    """Goals tools have no external requirements — always available."""
    return True


GOALS_READ_SCHEMA = {
    "name": "goals_read",
    "description": (
        "Read the user's goals (first-class state: statement, why, status, next_action, "
        "progress log, review cadence). Check goals before starting substantive work and "
        "orient the work toward an active goal; a goal's id is the [gN] tag. Pass goal_id "
        "for one goal; omit for all goals plus drift and review status."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal_id": {"type": "string", "description": "Optional goal id (e.g. g1) for a single goal."},
        },
        "required": [],
    },
}

GOALS_UPDATE_SCHEMA = {
    "name": "goals_update",
    "description": (
        "Create or change goals. Actions: create (statement required; optional why, "
        "next_action, status, review_cadence), update (goal_id + fields), log (goal_id + "
        "note — append a progress entry whenever work advances a goal; this is what keeps "
        "reviews accurate and drift detection quiet), remove (goal_id — prefer "
        "status=dropped to keep history). Statuses: active, paused, done, dropped. "
        "Changing goals does NOT rewrite the running conversation's prompt — updates "
        "reach future sessions and scheduled reviews."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "update", "log", "remove"],
                       "description": "What to do."},
            "goal_id": {"type": "string", "description": "Goal id (e.g. g1) — required for update/log/remove."},
            "statement": {"type": "string", "description": "What the user wants to achieve, one sentence."},
            "why": {"type": "string", "description": "Why it matters — the reason the goal exists."},
            "status": {"type": "string", "enum": _STATUS_ENUM},
            "next_action": {"type": "string", "description": "The single next concrete step."},
            "review_cadence": {"type": "string", "enum": _CADENCE_ENUM,
                               "description": "Which review this goal is grouped under (default daily)."},
            "note": {"type": "string", "description": "Progress entry for action=log — what moved."},
        },
        "required": ["action"],
    },
}

GOALS_REVIEW_SCHEMA = {
    "name": "goals_review",
    "description": (
        "Assemble a goal review (structured data — you write the summary prose). kind=daily: "
        "one line per active goal + drift since the last daily review. kind=weekly: deep review "
        "per goal (progress, staleness, drift patterns, keep/adjust/pause/drop recommendations). "
        "record=true (default) stamps the review so the next one only covers new material; pass "
        "record=false for a preview without consuming the window."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": _CADENCE_ENUM, "description": "Review depth (default daily)."},
            "record": {"type": "boolean", "description": "Stamp this review as run (default true)."},
        },
        "required": [],
    },
}

registry.register(
    name="goals_read", toolset=GOALS_TOOLSET, schema=GOALS_READ_SCHEMA,
    handler=lambda args, **kw: goals_read(goal_id=args.get("goal_id")),
    check_fn=check_goals_requirements, emoji="🎯")

registry.register(
    name="goals_update", toolset=GOALS_TOOLSET, schema=GOALS_UPDATE_SCHEMA,
    handler=lambda args, **kw: goals_update(
        action=args.get("action") or "", goal_id=args.get("goal_id"),
        statement=args.get("statement"), why=args.get("why"), status=args.get("status"),
        next_action=args.get("next_action"), review_cadence=args.get("review_cadence"),
        note=args.get("note")),
    check_fn=check_goals_requirements, emoji="🎯")

registry.register(
    name="goals_review", toolset=GOALS_TOOLSET, schema=GOALS_REVIEW_SCHEMA,
    handler=lambda args, **kw: goals_review(kind=args.get("kind") or "daily",
                                            record=bool(args.get("record", True))),
    check_fn=check_goals_requirements, emoji="🎯")

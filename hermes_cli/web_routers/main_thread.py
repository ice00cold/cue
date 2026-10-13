"""Main-thread home routes (Cue's ONE-session model — PLAN.md Phase 6 M1/M4).

``GET /api/main_thread`` (M1) resolves the profile's live main-thread session:
the same ``agent:<ns>:main-thread`` routing key every platform collapses to
(``gateway.session`` key-collapse helpers), read from the durable
``gateway_routing`` index so the Thread surface never guesses a session id
across compression rotations. Read-only by design — the first window that
chats (the embedded TUI) creates the routing row; ``sessions.changed`` then
tells the surface to re-resolve.

``POST /api/main_thread/send`` (M4 / R4) is the HEADLESS send path: one turn
forwarded to the gateway's api_server session-chat ingress on loopback, whose
turns run in the GATEWAY process with full main-thread semantics (busy-queuing
behind any live turn via the durable store/lease machinery). The interactive
composer stays the embedded TUI (web/AGENTS.md — never a second React chat
surface); this endpoint serves scripted/remote sends and the M4 cards' resume
deliveries (:func:`deliver_main_thread_prompt`), with the reply visible through
the M1 transcript / M3 live tail rather than this response.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hermes_cli.web_deps import late
from hermes_cli.web_routers._common import log as _log

router = APIRouter()

_open_session_db_for_profile = late("_open_session_db_for_profile", "hermes_cli.web_server_sessions")

# api_server's own default listener (gateway/platforms/api_server.py).
_DEFAULT_INGRESS_PORT = 8642
# One headless turn can legitimately run minutes (tools, delegations); the
# caller chose the synchronous contract, so the budget is generous, not tight.
_SEND_TIMEOUT_S = 600.0

_NO_THREAD_MESSAGE = (
    "The main thread does not exist yet — its first message creates it. "
    "Send from the chat composer (the embedded TUI) once, then headless sends work.")
_UNREACHABLE_MESSAGE = (
    "The messaging gateway is not reachable on loopback. Headless turns run in the "
    "gateway process (`gateway run` with the api_server platform enabled); the chat "
    "composer below keeps working either way.")

# Strong references for fire-and-forget deliveries: a task nothing references
# can be garbage-collected mid-flight, silently dropping a resume turn.
_DELIVERY_TASKS: Set["asyncio.Task[None]"] = set()


def _main_thread_key(profile: Optional[str]) -> str:
    """Routing key for the store *profile* names.

    A named profile keys under its own namespace; an omitted profile keys under
    the active-scope derivation shared with the CLI/cron/mirror paths — namespace
    parity with the gateway's key collapse
    (``gateway.session.active_profile_main_thread_session_key``).
    """
    from gateway.session import active_profile_main_thread_session_key, main_thread_session_key

    named = (profile or "").strip()
    if not named:
        return active_profile_main_thread_session_key()
    return main_thread_session_key(named)


def _resolve_main_thread(db, key: str):
    """(routing session_id, live tip id, tip summary row) for *key*.

    A routing entry whose target no longer exists resolves as "no thread yet"
    (the CLI resolver's contract) — the next chatting window recreates it, so
    the honest answer is ``None`` rather than a 404. The live tip follows the
    compression chain to where messages actually land, mirroring what the
    messages endpoint does with either id.
    """
    entry_json = db.load_gateway_routing_entries().get(key)
    session_id: Optional[str] = None
    if entry_json:
        try:
            session_id = (json.loads(entry_json) or {}).get("session_id") or None
        except ValueError:
            _log.warning("malformed gateway_routing entry for %s", key)
            session_id = None
    if session_id and not db.get_session(session_id):
        session_id = None
    live_id = db.resolve_resume_session_id(session_id) if session_id else None
    summary = db.get_session(live_id) if live_id else None
    return session_id, live_id, summary


@router.get("/api/main_thread")
async def get_main_thread(profile: Optional[str] = None):
    """The profile's main thread: routing id, compression-aware live id, and the
    tip's summary row — the Thread surface's header/cards data in one call."""
    from hermes_cli.web_routers.sessions import _is_active, _serving_profile, _with_db

    key = _main_thread_key(profile)
    session_id, live_id, summary = await asyncio.to_thread(
        _with_db, profile, lambda db: _resolve_main_thread(db, key), read_only=True)
    return {
        "profile": _serving_profile(profile),
        "session_key": key,
        "session_id": session_id,
        "live_session_id": live_id,
        "exists": bool(session_id),
        "is_active": bool(summary and _is_active(summary, time.time())),
        "session": summary,
    }


# ── M4: the headless send path ───────────────────────────────────────────────


@dataclass(frozen=True)
class MainThreadDelivery:
    """Outcome of one headless turn: delivered, or a machine ``error`` code plus
    human ``message`` copy (surfaced verbatim by the REST route and the card
    delivery banners)."""
    delivered: bool
    session_id: Optional[str] = None
    reply: Optional[str] = None
    error: Optional[str] = None
    message: Optional[str] = None


def _listener_config(home) -> Dict[str, Any]:
    """The listener home's ``platforms.api_server`` settings, flattened the way
    ``PlatformConfig.from_dict`` flattens them (an explicit ``extra:`` value
    wins on a clash with a top-level spelling)."""
    from hermes_cli.config import cfg_get, load_config
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(home))
    try:
        cfg = load_config()
    except Exception:  # health: allow BLE001 -- the forward treats an unreadable listener config as absent ({}), same boundary the cron-fire forwarder draws
        _log.debug("main-thread send: listener config unreadable", exc_info=True)
        return {}
    finally:
        reset_hermes_home_override(token)
    merged: Dict[str, Any] = {}
    for section in (cfg_get(cfg, "platforms", "api_server", default=None),
                    cfg_get(cfg, "platforms", "api_server", "extra", default=None)):
        if isinstance(section, dict):
            merged.update(section)
    return merged


def _api_server_ingress(profile: Optional[str]) -> tuple[str, str]:
    """``(base_url, bearer_key)`` of the gateway api_server that runs *profile*'s turns.

    Port/key resolution mirrors the cron-fire forwarder (``web_server_cron``)
    and the adapter itself: the LISTENER-OWNER profile's config/env decides
    host+port; under multiplex only the default profile binds and non-default
    profiles ride ``/p/<profile>/`` — whose auth checks the ROUTED profile's
    own ``API_SERVER_KEY`` (``api_server._expected_api_key``), never the
    listener's. Loopback is safe: dashboard and gateway share a network
    namespace in every supported deployment.
    """
    import os

    from hermes_cli.web_server_cron import _cron_default_profile, _cron_profile_home, _profile_env_value

    serving = (profile or "").strip()
    multiplex = False
    try:
        # The live gateway's own record, else the explicit flag — never the merged
        # default (an unset flag is settled by the gateway at boot, not here).
        from hermes_cli.gateway_multiplex_mode import default_gateway_multiplexes
        multiplex = default_gateway_multiplexes()
    except Exception:  # health: allow BLE001 -- the gateway's live multiplex verdict is advisory here; unreadable -> single-profile, mirroring the cron-fire forwarder
        _log.debug("main-thread send: multiplex detection failed; assuming single-profile", exc_info=True)

    routed = bool(multiplex and serving and serving != "default")
    listener_profile = "default" if routed else (serving or _cron_default_profile())
    listener_home = _cron_profile_home(listener_profile)[1]
    cfg = _listener_config(listener_home)

    host = str(cfg.get("host") or "").strip() or "127.0.0.1"
    try:
        port = int(cfg.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not port:
        raw = (os.getenv("API_SERVER_PORT", "")
               if listener_profile == _cron_default_profile()
               else _profile_env_value(listener_home, "API_SERVER_PORT"))
        try:
            port = int(raw) if raw else 0
        except ValueError:
            port = 0
    port = port or _DEFAULT_INGRESS_PORT

    key = str(cfg.get("key") or "").strip() or _profile_env_value(listener_home, "API_SERVER_KEY")
    if not key and listener_profile == _cron_default_profile():
        # The listener-owner may carry its key as a process env var instead of
        # .env (systemd units); only its OWN process default may answer — a
        # named profile's value lives in that profile's home or nowhere.
        key = os.getenv("API_SERVER_KEY", "")
    if routed:
        key = _profile_env_value(_cron_profile_home(serving)[1], "API_SERVER_KEY")

    base = f"http://{host}:{port}" + (f"/p/{serving}" if routed else "")
    return base, key


def _ingress_error_message(status: int, body: Any) -> str:
    """Human copy for a refused headless turn; an api_server error body's
    message wins when present."""
    detail = ""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and error.get("message"):
        detail = f" ({error['message']})"
    if status == 401:
        return ("The gateway rejected the dashboard's API key. Give the profile a usable "
                "API_SERVER_KEY (min 16 chars) in its .env and retry." + detail)
    if status == 404:
        return ("The main-thread session moved (compression rotation) between resolve and "
                "send — the surface re-resolves; retry." + detail)
    return f"The gateway api_server refused the turn with HTTP {status}.{detail}"


async def _resolve_delivery_target(profile: Optional[str]) -> tuple[Optional[str], str]:
    """(live tip id, session key) for the profile's main thread; tip is None
    while the thread does not exist yet."""
    from hermes_cli.web_routers.sessions import _with_db

    key = _main_thread_key(profile)
    _routing, live_id, _summary = await asyncio.to_thread(
        _with_db, profile, lambda db: _resolve_main_thread(db, key), read_only=True)
    return live_id, key


async def deliver_main_thread_message(
    profile: Optional[str], message: str, *, timeout_s: float = _SEND_TIMEOUT_S,
) -> MainThreadDelivery:
    """Run one user turn in the profile's main thread through the gateway's
    api_server session-chat ingress.

    ``X-Hermes-Session-Key`` pins the conversation across mid-turn compression
    rotations (the header api_server exists for); the body's history is
    discarded and the store's transcript is authoritative. Never raises — every
    failure is an honest :class:`MainThreadDelivery` so callers can persist
    their state change first and report delivery separately.
    """
    import httpx

    live_id, session_key = await _resolve_delivery_target(profile)
    if not live_id:
        return MainThreadDelivery(False, error="no_thread", message=_NO_THREAD_MESSAGE)
    base, api_key = await asyncio.to_thread(_api_server_ingress, profile)
    headers: Dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["X-Hermes-Session-Key"] = session_key
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                f"{base}/api/sessions/{live_id}/chat",
                json={"message": message}, headers=headers)
    except httpx.HTTPError as exc:
        _log.warning("main-thread send via %s failed (%s: %s)", base, type(exc).__name__, exc)
        return MainThreadDelivery(
            False, session_id=live_id, error="gateway_unreachable", message=_UNREACHABLE_MESSAGE)
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.status_code == 200 and isinstance(body, dict):
        reply = body.get("message")
        return MainThreadDelivery(
            True,
            session_id=str(body.get("session_id") or live_id),
            reply=str(reply.get("content") or "") if isinstance(reply, dict) else "",
        )
    return MainThreadDelivery(
        False, session_id=live_id, error=f"http_{resp.status_code}",
        message=_ingress_error_message(resp.status_code, body))


def deliver_main_thread_prompt(profile: Optional[str], prompt: str, *, label: str) -> None:
    """Schedule one best-effort turn (a resolved decision's resume prompt, a
    resumed goal's continuation) into the main thread.

    Delivery failures log and never fail the caller's request: the state change
    that produced the prompt (the resolution, the pause) already persisted, and
    the reply surfaces through the transcript/live tail whenever the turn lands.
    """
    async def _deliver() -> None:
        outcome = await deliver_main_thread_message(profile, prompt)
        if outcome.delivered:
            _log.info("%s turn delivered to the main thread (session %s)", label, outcome.session_id)
        else:
            _log.warning("%s turn NOT delivered (%s): %s", label, outcome.error, outcome.message)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _log.warning("%s turn dropped: no running loop to deliver it on", label)
        return
    task = loop.create_task(_deliver())
    _DELIVERY_TASKS.add(task)
    task.add_done_callback(_DELIVERY_TASKS.discard)


async def main_thread_delivery_status(profile: Optional[str]) -> tuple[bool, Optional[str]]:
    """``(can_deliver, reason)`` — the synchronous half of the cards' delivery
    report: False (with the same message the send endpoint serves) while the
    thread does not exist yet, so a resume prompt is never silently scheduled
    into a conversation that cannot take it. A reachable gateway is NOT
    pre-checked: that failure is logged by the delivery task, never shown as a
    refused resolution."""
    live_id, _key = await _resolve_delivery_target(profile)
    if not live_id:
        return False, _NO_THREAD_MESSAGE
    return True, None


class MainThreadSendBody(BaseModel):
    message: str


@router.post("/api/main_thread/send")
async def send_main_thread(body: MainThreadSendBody, profile: Optional[str] = None):
    """One headless turn in the profile's main thread (M4 / R4).

    The turn runs in the gateway process; the response carries the final
    assistant message, and the transcript/live tail pick the rows up regardless.
    Failures are structured ``{"error", "message"}`` details with the honest
    status: 409 while the thread does not exist yet, 503 when the gateway is
    unreachable, 502 for every refused turn.
    """
    from hermes_cli.web_routers.sessions import _serving_profile

    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    outcome = await deliver_main_thread_message(profile, message)
    if not outcome.delivered:
        status = {"no_thread": 409, "gateway_unreachable": 503}.get(outcome.error or "", 502)
        raise HTTPException(status_code=status,
                            detail={"error": outcome.error, "message": outcome.message})
    return {
        "delivered": True,
        "profile": _serving_profile(profile),
        "session_id": outcome.session_id,
        "reply": outcome.reply,
    }

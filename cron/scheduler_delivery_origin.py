"""Relay egress discriminators a cron fire carries from its persisted job origin."""

from __future__ import annotations

from typing import Any


def stamp_origin_discriminators(t: Any, route_metadata: dict, media_metadata: dict) -> None:
    """Stamp the origin's ``scope_id`` / ``user_id`` onto a live send's metadata.

    Relay egress is fail-closed on a discriminator and the RelayAdapter's caches are cold after every
    boot, so the persisted origin supplies them. Origin targets only (a fan-out target's recipient is not
    the origin's author); ``setdefault`` never overrides router or home stamping; ``user_id`` is read by
    relay transports only.
    """
    origin = t.origin or {}
    is_origin_target = (
        str(origin.get("platform") or "").lower() == str(t.platform_name).lower()
        and str(origin.get("chat_id") or "") == str(t.chat_id))
    discriminators = (
        ("scope_id", origin.get("scope_id") if is_origin_target else None),
        ("user_id", t.origin_user_id if is_origin_target and t.is_relay else None),
    )
    for key, value in discriminators:
        if value:
            route_metadata.setdefault(key, str(value))
            media_metadata.setdefault(key, str(value))

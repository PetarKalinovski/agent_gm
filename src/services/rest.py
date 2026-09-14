"""Rest — a bed ends the day, in code.

Nothing in the game healed without a potion or a DM decree, and nothing
ended a day except the DM remembering to advance the clock. Sleeping does
both now, with the numbers where the house rule says they live:

  * `Location.rest` says whether a place is safe to sleep in and what it
    costs — {"kind": "inn"|"camp", "price": int, "quality": "poor"|"fair"|"good"}.
    NULL means nowhere to sleep here; a `rest` scene object (a bed, a
    campfire) on a place with no spec is a makeshift camp.
  * `rest_now` charges first (a refusal touches nothing), heals by the
    bed's quality along the health ladder (`progression.rest_steps`), moves
    the clock to first light, asks the cinematic ledger for a "DAY N" card
    (throttled like any other), and writes the ledger line the DM opens the
    morning on. It does NOT run the campaign or world tick: both run inside
    the next turn's context build with the new clock, where a MUST-DELIVER
    beat has a turn to ride on. The day tick is kicked off by the endpoint.

One service, three doors: `POST /api/rest`, the `rest` DM tool, and the
canvas `[E] Rest` on a `rest` object.
"""

from __future__ import annotations

import logging
from typing import Any

from src.models import Location, Player, WorldClock, get_session
from src.services import progression
from src.services.cinematics import request_cinematic

logger = logging.getLogger(__name__)

REST_KINDS = ("inn", "camp")
MORNING_HOUR = 7
PRICE_MAX = 500
MAKESHIFT = {"kind": "camp", "price": 0, "quality": "poor"}


def normalize_rest(raw: Any) -> dict[str, Any] | None:
    """Clamp an authored rest spec, or None when nothing usable survives."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in REST_KINDS:
        return None
    try:
        price = int(raw.get("price") or 0)
    except (TypeError, ValueError):
        price = 0
    quality = str(raw.get("quality") or progression.REST_DEFAULT_QUALITY).strip().lower()
    if quality not in progression.REST_QUALITY:
        quality = progression.REST_DEFAULT_QUALITY
    return {"kind": kind, "price": max(0, min(PRICE_MAX, price)), "quality": quality}


def rest_here(location) -> dict[str, Any] | None:
    """Where the player can sleep here, or None. Pure, NULL-safe."""
    if location is None:
        return None
    spec = normalize_rest(getattr(location, "rest", None))
    if spec:
        return spec
    for obj in (getattr(location, "objects", None) or []):
        if isinstance(obj, dict) and str(obj.get("kind") or "").lower() == "rest":
            return dict(MAKESHIFT)
    return None


def hours_until_morning(clock) -> float:
    """Hours from now to MORNING_HOUR — today if it is still night, else tomorrow."""
    minutes_now = int(clock.hour or 0) * 60 + int(clock.minute or 0)
    target = MORNING_HOUR * 60
    if minutes_now < target:
        return (target - minutes_now) / 60.0
    return (24 * 60 - minutes_now + target) / 60.0


def rest_now(player_id: str, *, pay: bool = True) -> dict[str, Any]:
    """Sleep the night. Returns the summary the client and the DM both use.

    Refusals: {"error": "no_player"|"dead"|"no_rest_here"|"cannot_afford"|"no_clock"}.
    """
    if not player_id:
        return {"error": "no_player"}
    with get_session() as session:
        player = session.get(Player, player_id)
        if player is None:
            return {"error": "no_player"}
        if (player.health_status or "healthy") == "dead":
            return {"error": "dead"}
        location = session.get(Location, player.current_location_id) if player.current_location_id else None
        spec = rest_here(location)
        if spec is None:
            return {"error": "no_rest_here", "location_name": location.name if location else None}
        clock = session.query(WorldClock).first()
        if clock is None:
            return {"error": "no_clock"}

        price = int(spec["price"]) if pay else 0
        purse = int(player.currency or 0)
        if price > purse:
            return {"error": "cannot_afford", "price": price, "currency": purse,
                    "location_name": location.name}

        # Charge first, then heal, then move the clock — one transaction
        before = player.health_status or "healthy"
        after = progression.shift_health(before, progression.rest_steps(spec["quality"]))
        hours = hours_until_morning(clock)
        if price:
            player.currency = purse - price
        player.health_status = after
        clock.advance(hours)
        day, hour, time_of_day = clock.day, clock.hour, clock.get_time_of_day()
        location_name = location.name
        session.commit()

    cinematic = None
    try:
        card = request_cinematic(
            "title_card", player_id=player_id, title=f"DAY {day}", subtitle=location_name,
            source="auto", dm_callable_only=False,
        )
        if card.get("ok"):
            cinematic = card["payload"]
    except Exception as e:
        logger.debug(f"rest title card skipped: {e}")

    healed = before != after
    summary = (
        f"You sleep the night at {location_name}"
        + (f" for {price} coin" if price else "")
        + f". Day {day}, first light."
        + (f" You wake {after.replace('_', ' ')}." if healed else "")
    )
    result = {
        "success": True,
        "kind": spec["kind"],
        "quality": spec["quality"],
        "location_name": location_name,
        "price": price,
        "paid": price,
        "health": {"before": before, "after": after},
        "hours": round(hours, 2),
        "day": day,
        "hour": hour,
        "time_of_day": time_of_day,
        "summary": summary,
        "cinematic": cinematic,
    }

    try:
        from src.web import pending_actions
        pending_actions.record_action(
            player_id, "rest",
            pending_actions.rest_note(location_name, spec["kind"], before, after, price, day),
        )
    except Exception as e:      # CLI play: no ledger, no harm
        logger.debug(f"rest ledger note skipped: {e}")
    logger.info(f"Rest: {player_id} at {location_name} -> day {day} {hour}:00, {before} -> {after}, paid {price}")
    return result

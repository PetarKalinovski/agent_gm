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
  * A night is not a skip button. The first player-visible scheduled event
    due before first light, within earshot, wakes the sleeper: it fires
    then and there, the clock stops at it, the heal is cut to the hours
    actually slept, the bed is not charged (it is paid at first light), and
    the ledger line orders the DM to open on the interruption. Playtest
    2026-10-04: a 20:00 sleep jumped past the seeded Evening Howl, the
    town's set piece, and it never reached the screen.

One service, three doors: `POST /api/rest`, the `rest` DM tool, and the
canvas `[E] Rest` on a `rest` object.
"""

from __future__ import annotations

import logging
from typing import Any

from src.models import Event, Location, Player, WorldClock, get_session
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


def _root_id(session, location) -> str | None:
    """The top of a location's hierarchy — the settlement or region it is in."""
    seen: set[str] = set()
    while location is not None and location.parent_id and location.id not in seen:
        seen.add(location.id)
        parent = session.get(Location, location.parent_id)
        if parent is None:
            break
        location = parent
    return location.id if location is not None else None


def _within_earshot(session, event: Event, root_id: str | None) -> bool:
    """An event with no place is the world's own; a placed one wakes only a
    sleeper in the same settlement — a blockade three towns away is news
    for the morning, not a knock on the door."""
    places = [p for p in (event.locations_involved or []) if isinstance(p, str)]
    if not places:
        return True
    return any(_root_id(session, session.get(Location, p)) == root_id for p in places)


def _waking_event(session, clock, hours: float, location) -> tuple[Event, int] | None:
    """The first unfired, player-visible scheduled event due before first
    light that the sleeper would hear, and the minutes of sleep before it
    wakes them (0 when it is already due, only waiting for the next tick)."""
    now = clock.day * 1440 + int(clock.hour or 0) * 60 + int(clock.minute or 0)
    wake = now + round(hours * 60)
    root_id = _root_id(session, location)
    due: list[tuple[int, Event]] = []
    for event in session.query(Event).filter(
        Event.scheduled_day.isnot(None),
        Event.occurred_day.is_(None),
        Event.player_visible == True,  # noqa: E712
    ).all():
        at = event.scheduled_day * 1440 + int(event.scheduled_hour or 0) * 60
        if at < wake and _within_earshot(session, event, root_id):
            due.append((at, event))
    if not due:
        return None
    at, event = min(due, key=lambda pair: pair[0])
    return event, max(at - now, 0)


def _slept(minutes: int) -> str:
    """How long the broken night lasted, in words: the player reads this, so
    never a fraction of an hour ("after 0.22 hours of sleep")."""
    if minutes <= 0:
        return "before sleep came"
    if minutes < 60:
        return f"after {minutes} minute{'' if minutes == 1 else 's'} of sleep"
    hours = round(minutes / 60)
    about = "" if minutes % 60 == 0 else "about "
    return f"after {about}{hours} hour{'' if hours == 1 else 's'} of sleep"


def _woken_summary(location_name: str, woken_by: dict[str, Any], minutes: int, after: str,
                   healed: bool, price: int) -> str:
    """What the player reads: Woken at 20:00 by The Evening Howl, after ..."""
    at = f"{woken_by['hour']:02d}:{woken_by['minute']:02d}"
    return (f"Woken at {at} by {woken_by['name']}, {_slept(minutes)} at {location_name}."
            + (f" You wake {after.replace('_', ' ')}." if healed else "")
            + (" The night is broken; the bed is not charged." if price else " The night is broken."))


def rest_now(player_id: str, *, pay: bool = True) -> dict[str, Any]:
    """Sleep the night. Returns the summary the client and the DM both use.

    Refusals: {"error": "no_player"|"dead"|"no_rest_here"|"cannot_afford"|"no_clock"}.
    A night broken by a scheduled event (`_waking_event`) returns
    `interrupted: True` and `woken_by` (the fired event), with `hours`,
    `day`/`hour`/`minute`, `health` and `paid` (0) describing the partial night.
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

        # Charge first (a full night only), then heal, then move the clock —
        # one transaction
        before = player.health_status or "healthy"
        full_night = hours_until_morning(clock)
        steps = progression.rest_steps(spec["quality"])
        waking = _waking_event(session, clock, full_night, location)
        woken_by = None
        if waking is None:
            hours, paid = full_night, price
            if paid:
                player.currency = purse - paid
            after = progression.shift_health(before, steps)
            # The wounded of the party are back on their feet by morning
            from src.services import party as party_mod
            party_healed = party_mod.heal(session, player)
        else:
            # A broken night heals only the share of it that was slept
            event, slept_minutes = waking
            hours, paid, party_healed = slept_minutes / 60.0, 0, []
            after = progression.shift_health(before, int(steps * hours / full_night))
        player.health_status = after
        clock.advance(hours)
        day, hour, minute = clock.day, clock.hour, int(clock.minute or 0)
        time_of_day = clock.get_time_of_day()
        if waking is not None:
            # Fired here, at the moment it wakes them. The ledger line below
            # IS its narration (as a campaign world_event's directive is), so
            # the world tick never re-surfaces it as old news
            event.occurred_day, event.occurred_hour = day, hour
            event.delivery_attempts = 1
            event.player_witnessed = True
            event.narrated_to_player = True
            woken_by = {
                "id": event.id,
                "name": event.name,
                "description": event.description,
                "consequences": list(event.consequences or []),
                "day": day,
                "hour": hour,
                "minute": minute,
            }
        location_name = location.name
        session.commit()

    healed = before != after
    cinematic = None    # a "DAY N" card only for a night slept to first light
    if woken_by is None:
        try:
            card = request_cinematic(
                "title_card", player_id=player_id, title=f"DAY {day}", subtitle=location_name,
                source="auto", dm_callable_only=False,
            )
            if card.get("ok"):
                cinematic = card["payload"]
        except Exception as e:
            logger.debug(f"rest title card skipped: {e}")
        summary = (
            f"You sleep the night at {location_name}"
            + (f" for {paid} coin" if paid else "")
            + f". Day {day}, first light."
            + (f" You wake {after.replace('_', ' ')}." if healed else "")
            + (f" {', '.join(party_healed)} {'is' if len(party_healed) == 1 else 'are'} on their feet again." if party_healed else "")
        )
    else:
        summary = _woken_summary(location_name, woken_by, slept_minutes, after, healed, price)
    result = {
        "success": True,
        "kind": spec["kind"],
        "quality": spec["quality"],
        "location_name": location_name,
        "price": price,
        "paid": paid,
        "health": {"before": before, "after": after},
        "hours": round(hours, 2),
        "day": day,
        "hour": hour,
        "minute": minute,
        "time_of_day": time_of_day,
        "summary": summary,
        "cinematic": cinematic,
        "party_healed": party_healed,
        "interrupted": woken_by is not None,
        "woken_by": woken_by,
    }

    try:
        from src.web import pending_actions
        if woken_by is None:
            note = pending_actions.rest_note(location_name, spec["kind"], before, after, paid, day,
                                             party_healed=party_healed)
        else:
            note = _woken_note(location_name, spec["kind"], woken_by, slept_minutes, before, after)
        pending_actions.record_action(player_id, "rest", note)
    except Exception as e:      # CLI play: no ledger, no harm
        logger.debug(f"rest ledger note skipped: {e}")
    logger.info(f"Rest: {player_id} at {location_name} -> day {day} {hour:02d}:{minute:02d}, "
                f"{before} -> {after}, paid {paid}"
                + (f", woken by {woken_by['name']}" if woken_by else ""))
    return result


def _woken_note(location_name: str, kind: str, woken_by: dict[str, Any], minutes: int,
                before: str, after: str) -> str:
    """The ledger line for a broken night: the event is the next scene."""
    where = f"the {kind} at {location_name}" if kind == "camp" else location_name
    at = f"{woken_by['hour']:02d}:{woken_by['minute']:02d}"
    note = (f"They lay down at {where} and were woken at {at} on Day {woken_by['day']} by "
            f"{woken_by['name']} {_slept(minutes)} — it is happening NOW: {woken_by['description']}")
    if woken_by["consequences"]:
        note += f" ({'; '.join(woken_by['consequences'])})"
    if before != after:
        note += f"; they woke {after.replace('_', ' ')} (were {before.replace('_', ' ')})"
    return note + (
        ". The clock and their health are ALREADY moved and the bed was not charged. Open on "
        "this event as it happens — never @time or @health for the broken night; @rest again "
        "only if they go back to bed."
    )

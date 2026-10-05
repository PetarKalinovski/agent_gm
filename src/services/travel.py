"""Travel resolution shared by the DM tool and the canvas.

`move_player` (the DM's tool) and `travel_now` (the canvas REST path) must
never disagree about whether a route is walkable, so both rules live here
once:

  1. a direct Connection with non-empty `requirements` blocks — unless
     the player was waved through it before, or carries the thing a
     requirement names (a Gate Pass for "needs a gate pass");
  2. with no direct Connection, a destination whose every authored entry
     route is requirement-locked stays locked on ANY approach path — the
     gate-bypass guard (playtest: square -> town container -> gated hollow
     walked around the north gate);
  3. the destination's own `dress_code`, evaluated against how the player
     reads (`src/services/perception.py`) and only when a player row is
     supplied — a route question with no player is a question about the map.

Code enforces, the LLM narrates: the canvas walks the player through
unlocked doors with no LLM round-trip, and hands locked ones to the DM.
"""

import re
from typing import Any

from loguru import logger
from sqlalchemy.orm import object_session

from src.models import Event, Faction, Connection, Location, Player, WorldClock, get_session
from src.services import perception
from src.services import standing


# Travel time fallbacks, in hours, when no Connection models the route
HIERARCHY_HOP_HOURS = 0.1   # stepping into a child or back out to the parent
DEFAULT_TRAVEL_HOURS = 0.5  # an unmodeled route between unrelated places


def _apply_dress_code(route: dict[str, Any], destination, player) -> dict[str, Any]:
    """Rule 3: the destination's own standards.

    Evaluated on the DESTINATION regardless of which route was taken — a dress
    code is a door policy, not a road condition — and only when we know who is
    asking. Precedence is deliberate: `route`/`gate` keep the `reason` (they
    are the harder blockers to explain away), while both requirement strings
    ride the existing channel, so a road that needs a key AND a hall that needs
    a coat asks for both.
    """
    if player is None or not getattr(destination, "dress_code", None):
        return route

    verdict = perception.dress_verdict(
        perception.perception_profile(player),
        perception.normalize_dress_code(destination.dress_code),
    )
    if verdict["allowed"]:
        return route

    route["requirements"] = list(route["requirements"]) + [verdict["requirement"]]
    route["reason"] = route["reason"] or "dress"
    route["blocked"] = True
    return route


def _apply_standing(session, route: dict[str, Any], destination, player) -> dict[str, Any]:
    """Rule 4: the people who hold a place refuse someone they hate.

    Fires only when the destination has a `controlling_faction_id` and the
    player's standing with it is `hostile` (pure dict math — this runs once
    per exit per scene load; the Faction row loads only inside the fired
    branch). Precedence: route > gate > dress > standing, via the `or`.
    `requirements_met=True` overrides it for one move, but it is never
    recorded as a cleared road — a pardon is a scene, not a permanent pass.
    """
    if player is None or route.get("reason") == "standing":
        return route
    faction_id = getattr(destination, "controlling_faction_id", None)
    if not faction_id or standing.standing_for(player, faction_id) != "hostile":
        return route
    faction = session.get(Faction, faction_id)
    name = faction.name if faction is not None else "the people who hold this place"
    route["requirements"] = list(route["requirements"]) + [
        f"{name} hold {destination.name}, and they want the player gone — nobody they hate walks in."
    ]
    route["reason"] = route["reason"] or "standing"
    route["blocked"] = True
    return route


# After dark a gate that bars at night is barred to everyone. The pass
# opened the north road at any hour, so after the howl the DM was told the
# gate is "barred for the night — true now" AND to write `@move` through it.
# At night neither a held pass nor an earlier say-so opens such a road by
# itself; the DM's `| through` still can, when the fiction gets them past.
NIGHT_HOURS = frozenset(range(20, 24)) | frozenset(range(0, 6))
CLOSURE = re.compile(r"\b(?:barred|bars?|shut|shuts|closed?|closes|curfew|locked)\b", re.IGNORECASE)
NIGHTLY = re.compile(r"\b(?:night|nightly|nightfall|dark|dusk|evening|sundown|sunset|howl|curfew|dawn)\b",
                     re.IGNORECASE)
# ...and the pass alone never was the fiction that does (fleet8-tamsin
# T11-T13: the guard said the gate was closed past the howl, read the seal,
# "the gate swings wide", and `| through` carried her out on the pass). A
# route barred tonight says so (`barred_tonight` in the route), and the
# DM's `| through` over it needs a won roll or a bribe (commands._h_move)
BARRED_TONIGHT = ("Barred for the night until first light — a pass or an earlier say-so does not "
                  "open it tonight; only a won roll or a bribe gets them through, else they wait for first light")


def _says_barred_at_night(texts) -> bool:
    return any(CLOSURE.search(t) and NIGHTLY.search(t) for t in (str(x) for x in texts or [] if x))


def night_why(route: dict[str, Any], player) -> str | None:
    """What stops them tonight, said plainly, when it is only the night: a
    route barred tonight whose other requirement is a pass they carry.
    "(…the player needs a gate pass…)" told the DM the pass was missing while
    it sat in the pack, beside "a pass does not open it". None otherwise."""
    if not route.get("barred_tonight"):
        return None
    rest = [str(r) for r in route.get("requirements") or [] if r != BARRED_TONIGHT]
    held = held_pass(rest, player) if rest else None
    if rest and not held:
        return None
    return ("barred for the night until first light" + (f"; the {held} they carry opens it by day" if held else ""))


def barred_tonight(connection) -> bool:
    """Is this gated road shut for the night right now? Its own requirement
    says it bars after dark ("gates are barred after the evening howl"), or a
    fired event at either end did ("North Gate barred for the night") — and
    the clock is in NIGHT_HOURS. A road with no requirement never is."""
    if not connection.requirements:
        return False
    session = object_session(connection)
    if session is None:
        return False
    clock = session.query(WorldClock).first()
    if clock is None or (clock.hour or 0) not in NIGHT_HOURS:
        return False
    if _says_barred_at_night(connection.requirements):
        return True
    ends = {connection.from_location_id, connection.to_location_id}
    for event in session.query(Event).filter(Event.occurred_day.isnot(None)).all():
        if ends & set(event.locations_involved or []) and _says_barred_at_night(event.consequences):
            return True
    return False


def _cleared(connection, player) -> bool:
    """True when this player has already been waved through this route, or
    carries what its requirements name — never while it is barred tonight."""
    if barred_tonight(connection):
        return False
    player_id = getattr(player, "id", None)
    if bool(player_id) and player_id in (connection.cleared_for or []):
        return True
    return held_pass(connection.requirements, player) is not None


_PASS_FILLER = frozenset({"the", "a", "an", "of"})


def held_pass(requirements: Any, player) -> str | None:
    """The item in the player's pack that a requirement names, else None.

    fleet7-bram T5/T11: the DM handed over a Gate Pass, and `@move` past "the
    player needs a gate pass" was still refused — the pass meant nothing to
    the road. A whole item name inside the requirement text counts ("Gate
    Pass" in "needs a gate pass from the Captain"); a one-word name only for
    a key or quest item of four letters or more, so a "Lantern" in the pack
    never opens a road that mentions lanterns."""
    if not requirements or player is None:
        return None
    text = " ".join(str(r) for r in requirements if r).lower()
    inventory = player.inventory if isinstance(getattr(player, "inventory", None), list) else []
    for item in inventory:
        if not isinstance(item, dict):
            continue
        words = [w for w in re.findall(r"[a-z0-9']+", str(item.get("name") or "").lower()) if w not in _PASS_FILLER]
        if not words:
            continue
        if len(words) == 1 and (item.get("type") not in ("key", "quest_item") or len(words[0]) < 4):
            continue
        if re.search(r"\b" + r"\s+".join(re.escape(w) for w in words) + r"s?\b", text):
            return str(item.get("name"))
    return None


def clear_route_for(session, from_location_id: str | None, destination_id: str, player_id: str) -> int:
    """Record that `player_id` has satisfied the requirements between the two
    places: the direct connection (either direction) and, for a gated
    destination, every requirement-locked entry into it. Returns how many
    connections were marked. Called when the DM confirms
    `requirements_met=True`; from then on the canvas walks the road freely."""
    if not player_id:
        return 0
    rows = session.query(Connection).filter(
        ((Connection.from_location_id == from_location_id) & (Connection.to_location_id == destination_id)) |
        ((Connection.from_location_id == destination_id) & (Connection.to_location_id == from_location_id)) |
        (Connection.to_location_id == destination_id)
    ).all()
    marked = 0
    for conn in rows:
        if not conn.requirements:
            continue
        cleared = list(conn.cleared_for or [])
        if player_id not in cleared:
            cleared.append(player_id)
            conn.cleared_for = cleared
            marked += 1
    return marked


def check_route(session, from_location_id: str | None, destination_id: str,
                player=None) -> dict[str, Any]:
    """Decide whether the player may walk from one location to another.

    Args:
        session: An open database session (the caller owns the transaction).
        from_location_id: Where the player is standing; None when unplaced.
        destination_id: Where they want to go.
        player: The Player row, when the caller has one. Dress codes are only
            evaluated when it is supplied — a route question with no player is
            a question about the map, not about a person.

    Returns:
        {"travel_time_hours": float, "blocked": bool, "requirements": list,
         "reason": "" | "route" | "gate" | "dress" | "standing"} — `reason` tells the caller
        which rule fired so it can pick the right message for the DM.
    """
    destination = session.get(Location, destination_id)

    # Nothing to check: unknown destination, or a player who is nowhere yet
    if not destination or not from_location_id:
        return {
            "travel_time_hours": DEFAULT_TRAVEL_HOURS,
            "blocked": False,
            "requirements": [],
            "reason": "",
        }

    # Rule 1: a direct connection, either direction when bidirectional
    conn = session.query(Connection).filter(
        ((Connection.from_location_id == from_location_id) & (Connection.to_location_id == destination_id)) |
        ((Connection.from_location_id == destination_id) & (Connection.to_location_id == from_location_id) & (Connection.bidirectional == True))
    ).first()

    if conn:
        requirements = list(conn.requirements or [])
        night = False
        if requirements and _cleared(conn, player):
            requirements = []
        elif requirements and barred_tonight(conn):
            requirements.append(BARRED_TONIGHT)
            night = True
        route = {
            "travel_time_hours": conn.travel_time_hours,
            "blocked": bool(requirements),
            "requirements": requirements,
            "reason": "route" if requirements else "",
        }
        if night:
            route["barred_tonight"] = True
        return _apply_standing(session, _apply_dress_code(route, destination, player), destination, player)
    # No modeled route: entering a child or stepping back out to the parent
    # is a doorway, everything else is an unmodeled walk
    origin = session.get(Location, from_location_id)
    travel_time = DEFAULT_TRAVEL_HOURS
    if destination.parent_id == from_location_id or (origin and origin.parent_id == destination_id):
        travel_time = HIERARCHY_HOP_HOURS

    # Rule 2: gate-bypass guard. One open route in = a public place. Only
    # to-side edges count: a hub whose one locked road leads OUT must not
    # inherit the lock through a bidirectional edge.
    entries = session.query(Connection).filter(
        Connection.to_location_id == destination_id
    ).all()
    entries = [c for c in entries if not (c.requirements and _cleared(c, player))]
    gate_requirements = [r for c in entries for r in (c.requirements or [])]
    if entries and gate_requirements and all(c.requirements for c in entries):
        route = {
            "travel_time_hours": travel_time,
            "blocked": True,
            "requirements": gate_requirements,
            "reason": "gate",
        }
        # The way round through the town meets the same barred gate
        # (fleet8-tamsin T13 went square -> Hollow in one `| through`)
        if any(barred_tonight(c) for c in entries):
            route["requirements"] = gate_requirements + [BARRED_TONIGHT]
            route["barred_tonight"] = True
        return _apply_standing(session, _apply_dress_code(route, destination, player), destination, player)
    return _apply_standing(session, _apply_dress_code({
        "travel_time_hours": travel_time,
        "blocked": False,
        "requirements": [],
        "reason": "",
    }, destination, player), destination, player)
def travel_now(player_id: str, destination_id: str) -> dict[str, Any]:
    """Canvas travel: validate, move, advance the clock. No LLM, one path.

    Runs the same `check_route` the DM tool runs and then calls `move_player`
    itself, so the canvas can never walk somewhere the DM would have blocked.

    Args:
        player_id: The player's ID.
        destination_id: Where the door leads.

    Returns:
        {"success": True, "from_id", "from_name", "destination_id",
         "destination_name", "minutes", "first_visit"} on success;
        {"blocked": True, "requirements", "destination_name", "reason"} when
        the route needs something the DM must adjudicate;
        {"error": "unreachable" | "not_found" | "no_player"} otherwise.
    """
    # Imported here: the DM tools import this module for check_route
    from src.tools.world_read.locations import get_available_destinations
    from src.tools.world_write.player import move_player
    from src.tools.world_write.time import advance_time

    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "no_player"}

        destination = session.get(Location, destination_id)
        if not destination:
            return {"error": "not_found"}

        origin_id = player.current_location_id
        origin = session.get(Location, origin_id) if origin_id else None
        origin_name = origin.name if origin else ""
        destination_name = destination.name
        first_visit = not destination.visited
        route = check_route(session, origin_id, destination_id, player)

    # Anti-teleport guard: the client is not trusted to name a destination
    # the current location does not actually connect to
    reachable = {
        d.get("id")
        for d in (get_available_destinations(origin_id) or [])
        if isinstance(d, dict)
    } if origin_id else set()
    if destination_id not in reachable:
        return {"error": "unreachable"}

    if route["blocked"]:
        return {
            "blocked": True,
            "requirements": route["requirements"],
            "destination_name": destination_name,
            "reason": route.get("reason", ""),
        }

    result = move_player(player_id, destination_id)
    if result.get("blocked"):
        # Belt and braces: the tool is the authority on its own rules
        return {
            "blocked": True,
            "requirements": result.get("requirements", []),
            "destination_name": destination_name,
            "reason": "",
        }
    if not result.get("success"):
        return {"error": "not_found"}

    hours = route["travel_time_hours"]
    clock = advance_time(hours, reason="canvas travel")
    if clock.get("error"):
        logger.warning(f"Canvas travel could not advance the clock: {clock['error']}")

    arrival = place_at_arrival_door(player_id, destination_id, origin_id)
    party_followed = result.get("party_followed") or []
    if party_followed:
        from src.services import party as party_mod
        party_mod.settle(player_id)

    return {
        "success": True,
        "party": party_followed,
        "from_id": origin_id,
        "from_name": origin_name,
        "destination_id": destination_id,
        "destination_name": destination_name,
        "minutes": round(hours * 60),
        "first_visit": first_visit,
        "arrival": arrival,
    }


# How far inside the room from the door the traveller is stood
ARRIVAL_INSET = 7.0


def place_at_arrival_door(player_id: str, destination_id: str, origin_id: str | None) -> dict | None:
    """Stand the player just inside the destination's door back to where
    they came from. Walking east out of the tavern lands you at the square's
    west door, not at the far east wall where your old x/y happened to be.
    Returns the position written, or None when no door leads back (the
    player keeps their coordinates — the scene still loads)."""
    from src.services.scene_exits import compute_exit_doors
    from src.tools.world_read.locations import get_available_destinations

    if not origin_id:
        return None
    try:
        destinations = [
            d for d in (get_available_destinations(destination_id) or [])
            if isinstance(d, dict)
        ]
        with get_session() as session:
            destination = session.get(Location, destination_id)
            player = session.get(Player, player_id)
            if not destination or not player:
                return None
            door = compute_exit_doors(destination, destinations).get(origin_id)
            if not door:
                return None
            # Step ARRIVAL_INSET units from the door toward the room's centre
            dx, dy = 50.0 - door["x"], 50.0 - door["y"]
            length = (dx * dx + dy * dy) ** 0.5 or 1.0
            x = door["x"] + dx / length * ARRIVAL_INSET
            y = door["y"] + dy / length * ARRIVAL_INSET
            player.position_x = round(max(0.0, min(100.0, x)), 2)
            player.position_y = round(max(0.0, min(100.0, y)), 2)
            session.commit()
            return {"x": player.position_x, "y": player.position_y, "slot": door.get("slot")}
    except Exception as e:  # never let placement break a real move
        logger.warning(f"Arrival placement failed: {e}")
        return None

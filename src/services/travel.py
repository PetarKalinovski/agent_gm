"""Travel resolution shared by the DM tool and the canvas.

`move_player` (the DM's tool) and `travel_now` (the canvas REST path) must
never disagree about whether a route is walkable, so both rules live here
once:

  1. a direct Connection with non-empty `requirements` blocks;
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

from typing import Any

from loguru import logger

from src.models import Connection, Location, Player, get_session
from src.services import perception


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
         "reason": "" | "route" | "gate" | "dress"} — `reason` tells the caller
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
        return _apply_dress_code({
            "travel_time_hours": conn.travel_time_hours,
            "blocked": bool(requirements),
            "requirements": requirements,
            "reason": "route" if requirements else "",
        }, destination, player)

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
    gate_requirements = [r for c in entries for r in (c.requirements or [])]
    if entries and gate_requirements and all(c.requirements for c in entries):
        return _apply_dress_code({
            "travel_time_hours": travel_time,
            "blocked": True,
            "requirements": gate_requirements,
            "reason": "gate",
        }, destination, player)

    return _apply_dress_code({
        "travel_time_hours": travel_time,
        "blocked": False,
        "requirements": [],
        "reason": "",
    }, destination, player)


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

    return {
        "success": True,
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

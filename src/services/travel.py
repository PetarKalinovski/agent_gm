"""Travel resolution shared by the DM tool and the canvas.

`move_player` (the DM's tool) and `travel_now` (the canvas REST path) must
never disagree about whether a route is walkable, so both rules live here
once:

  1. a direct Connection with non-empty `requirements` blocks;
  2. with no direct Connection, a destination whose every authored entry
     route is requirement-locked stays locked on ANY approach path — the
     gate-bypass guard (playtest: square -> town container -> gated hollow
     walked around the north gate).

Code enforces, the LLM narrates: the canvas walks the player through
unlocked doors with no LLM round-trip, and hands locked ones to the DM.
"""

from typing import Any

from loguru import logger

from src.models import Connection, Location, Player, get_session


# Travel time fallbacks, in hours, when no Connection models the route
HIERARCHY_HOP_HOURS = 0.1   # stepping into a child or back out to the parent
DEFAULT_TRAVEL_HOURS = 0.5  # an unmodeled route between unrelated places


def check_route(session, from_location_id: str | None, destination_id: str) -> dict[str, Any]:
    """Decide whether the player may walk from one location to another.

    Args:
        session: An open database session (the caller owns the transaction).
        from_location_id: Where the player is standing; None when unplaced.
        destination_id: Where they want to go.

    Returns:
        {"travel_time_hours": float, "blocked": bool, "requirements": list,
         "reason": "" | "route" | "gate"} — `reason` tells the caller which
        rule fired so it can pick the right message for the DM.
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
        return {
            "travel_time_hours": conn.travel_time_hours,
            "blocked": bool(requirements),
            "requirements": requirements,
            "reason": "route" if requirements else "",
        }

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
        return {
            "travel_time_hours": travel_time,
            "blocked": True,
            "requirements": gate_requirements,
            "reason": "gate",
        }

    return {
        "travel_time_hours": travel_time,
        "blocked": False,
        "requirements": [],
        "reason": "",
    }


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
        {"blocked": True, "requirements", "destination_name"} when the route
        needs something the DM must adjudicate;
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
        route = check_route(session, origin_id, destination_id)

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
        }

    result = move_player(player_id, destination_id)
    if result.get("blocked"):
        # Belt and braces: the tool is the authority on its own rules
        return {
            "blocked": True,
            "requirements": result.get("requirements", []),
            "destination_name": destination_name,
        }
    if not result.get("success"):
        return {"error": "not_found"}

    hours = route["travel_time_hours"]
    clock = advance_time(hours, reason="canvas travel")
    if clock.get("error"):
        logger.warning(f"Canvas travel could not advance the clock: {clock['error']}")

    return {
        "success": True,
        "from_id": origin_id,
        "from_name": origin_name,
        "destination_id": destination_id,
        "destination_name": destination_name,
        "minutes": round(hours * 60),
        "first_visit": first_visit,
    }

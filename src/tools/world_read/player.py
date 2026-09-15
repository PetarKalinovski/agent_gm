"""Tools for player read operations."""

from typing import Any

from strands import tool

from src.models import (
    Player,
    WorldClock,
    get_session,
)
from src.services import party as party_mod
from src.services import progression as prog


@tool
def get_player(player_id: str) -> dict[str, Any]:
    """Get player character details.

    `equipped` is what they are actually wearing and wielding right now, with
    the quality of each piece. `level` is how far they have come — you may
    feel it in the fiction, but never say the word or the number.

    Args:
        player_id: The player's ID.

    Returns:
        Dictionary with player details.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        # NOTE: xp is deliberately absent. The DM has no business narrating an
        # experience total, so it never gets to see one.
        return {
            "id": player.id,
            "name": player.name,
            "description": player.description,
            "background": player.background,
            "traits": player.traits,
            "health_status": player.health_status,
            "level": prog.player_level(player),
            "equipped": prog.equipped_labels(player),
            "inventory": player.inventory,
            "active_quests": player.active_quests,
            "party_members": player.party_members,
            # Who travels with them, by name — the engine moves and fights them
            "party": [
                {"id": n.id, "name": n.name, "profession": n.profession, "condition": n.condition}
                for n in party_mod.members(session, player)
            ],
        }


@tool
def get_world_clock() -> dict[str, Any]:
    """Get the current in-game time.

    Returns:
        Dictionary with day, hour, and time of day.
    """
    with get_session() as session:
        clock = session.query(WorldClock).first()
        if not clock:
            return {"day": 1, "hour": 8, "time_of_day": "morning"}

        return {
            "day": clock.day,
            "hour": clock.hour,
            "time_of_day": clock.get_time_of_day(),
        }

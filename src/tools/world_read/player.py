"""Tools for player read operations."""

from typing import Any

from strands import tool

from src.models import (
    Player,
    WorldClock,
    get_session,
)


@tool
def get_player(player_id: str) -> dict[str, Any]:
    """Get player character details.

    Args:
        player_id: The player's ID.

    Returns:
        Dictionary with player details.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        return {
            "id": player.id,
            "name": player.name,
            "description": player.description,
            "traits": player.traits,
            "health_status": player.health_status,
            "inventory": player.inventory,
            "active_quests": player.active_quests,
            "party_members": player.party_members,
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

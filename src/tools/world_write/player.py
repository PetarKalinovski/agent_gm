"""Tools for player write operations."""

from typing import Any

from strands import tool

from src.models import (
    Connection,
    Location,
    Player,
    get_session,
)


@tool
def move_player(player_id: str, destination_id: str) -> dict[str, Any]:
    """Move the player to a new location.

    Args:
        player_id: The player's ID.
        destination_id: The destination location ID.

    Returns:
        Dictionary with result and travel time.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        destination = session.get(Location, destination_id)
        if not destination:
            return {"error": "Destination not found"}

        # Find the connection to determine travel time
        old_location_id = player.current_location_id
        travel_time = 0.5  # Default

        if old_location_id:
            conn = session.query(Connection).filter(
                ((Connection.from_location_id == old_location_id) & (Connection.to_location_id == destination_id)) |
                ((Connection.from_location_id == destination_id) & (Connection.to_location_id == old_location_id) & (Connection.bidirectional == True))
            ).first()

            if conn:
                travel_time = conn.travel_time_hours
            elif destination.parent_id == old_location_id or old_location_id == destination.parent_id:
                # Entering/exiting a building
                travel_time = 0.1

        # Update player location
        player.current_location_id = destination_id
        session.commit()

        # Mark location as visited
        destination.visited = True
        destination.discovered = True
        session.commit()

        return {
            "success": True,
            "destination": destination.name,
            "travel_time_hours": travel_time,
        }


@tool
def update_player_reputation(player_id: str, faction_id: str, delta: int) -> dict[str, Any]:
    """Update player's reputation with a faction.

    Args:
        player_id: The player's ID.
        faction_id: The faction's ID.
        delta: Change in reputation (-100 to 100).

    Returns:
        Dictionary with new reputation.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        reputation = player.reputation.copy() if player.reputation else {}
        current = reputation.get(faction_id, 50)
        new_score = max(0, min(100, current + delta))
        reputation[faction_id] = new_score
        player.reputation = reputation
        session.commit()

        return {"faction_id": faction_id, "new_score": new_score, "delta": delta}


@tool
def update_player_health(player_id: str, new_status: str) -> dict[str, Any]:
    """Update player's health status.

    Args:
        player_id: The player's ID.
        new_status: New health status (healthy, winded, hurt, badly_hurt, critical).

    Returns:
        Dictionary with result.
    """
    valid_statuses = ["healthy", "winded", "hurt", "badly_hurt", "critical"]
    if new_status not in valid_statuses:
        return {"error": f"Invalid status. Must be one of: {valid_statuses}"}

    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        player.health_status = new_status
        session.commit()

        return {"success": True, "new_status": new_status}


@tool
def add_to_inventory(player_id: str, item: str) -> dict[str, Any]:
    """Add an item to player's inventory.

    Args:
        player_id: The player's ID.
        item: The item to add.

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        inventory = player.inventory.copy() if player.inventory else []
        inventory.append(item)
        player.inventory = inventory
        session.commit()

        return {"success": True, "item": item, "inventory_size": len(inventory)}


@tool
def remove_from_inventory(player_id: str, item: str) -> dict[str, Any]:
    """Remove an item from player's inventory.

    Args:
        player_id: The player's ID.
        item: The item to remove.

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        inventory = player.inventory.copy() if player.inventory else []
        if item not in inventory:
            return {"error": "Item not in inventory"}

        inventory.remove(item)
        player.inventory = inventory
        session.commit()

        return {"success": True, "removed": item}

"""Tools for player write operations."""

from typing import Any

from strands import tool

from src.models import (
    Connection,
    Item,
    Location,
    Player,
    get_session,
)


def _normalize_inventory(raw: list) -> list[dict[str, Any]]:
    """Coerce an inventory list to structured Item dicts.

    Legacy inventories may contain bare strings; convert them instead of
    dropping them so items are never silently lost.
    """
    normalized = []
    for entry in raw or []:
        if isinstance(entry, str):
            item_id = entry.lower().replace(" ", "_")
            normalized.append(Item(id=item_id, name=entry, type="misc").to_dict())
        elif isinstance(entry, dict):
            # Copy — mutating the dicts loaded from the JSON column in place
            # would make SQLAlchemy see old == new and skip the UPDATE
            normalized.append(dict(entry))
    return normalized


@tool
def move_player(player_id: str, destination_id: str, requirements_met: bool = False) -> dict[str, Any]:
    """Move the player to a new location.

    If the travel route has requirements (a key, a permit, a guide...),
    the move is blocked until you confirm the player satisfies them by
    calling again with requirements_met=True.

    Args:
        player_id: The player's ID.
        destination_id: The destination location ID.
        requirements_met: Set True only after verifying the player satisfies
            the route's requirements (check their inventory/state first).

    Returns:
        Dictionary with result and travel time, or the blocking requirements.
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

                # Enforce route requirements
                requirements = conn.requirements or []
                if requirements and not requirements_met:
                    return {
                        "blocked": True,
                        "destination": destination.name,
                        "requirements": requirements,
                        "message": (
                            "This route has requirements the player must satisfy: "
                            f"{', '.join(str(r) for r in requirements)}. "
                            "Check the player's inventory/state; if satisfied, call move_player "
                            "again with requirements_met=True. Otherwise narrate why they can't pass."
                        ),
                    }
            elif destination.parent_id == old_location_id or old_location_id == destination.parent_id:
                # Entering/exiting a building
                travel_time = 0.1

        # Update player location and mark destination visited — one transaction
        player.current_location_id = destination_id
        destination.visited = True
        destination.discovered = True
        session.commit()

        return {
            "success": True,
            "destination": destination.name,
            "travel_time_hours": travel_time,
            "reminder": f"Advance time by ~{travel_time}h and describe the new location.",
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
def add_to_inventory(
    player_id: str,
    item_name: str,
    item_type: str = "misc",
    description: str = "",
    value: int = 0,
    quantity: int = 1,
    effects: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add an item to the player's inventory (found, gifted, looted, stolen).

    Stacks with an existing item of the same name if present.

    Args:
        player_id: The player's ID.
        item_name: Display name of the item (e.g., "Rusty Key").
        item_type: One of: consumable, weapon, armor, quest_item, misc.
        description: Short description of the item.
        value: Base value in currency.
        quantity: How many to add.
        effects: Optional effects when used (e.g., {"heal": 30}).

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        inventory = _normalize_inventory(player.inventory)
        item_id = item_name.lower().replace(" ", "_")

        for entry in inventory:
            if entry.get("id") == item_id or entry.get("name", "").lower() == item_name.lower():
                entry["quantity"] = entry.get("quantity", 1) + quantity
                break
        else:
            inventory.append(
                Item(
                    id=item_id,
                    name=item_name,
                    type=item_type,
                    value=value,
                    description=description,
                    effects=effects or {},
                    quantity=quantity,
                ).to_dict()
            )

        player.inventory = inventory
        session.commit()

        return {"success": True, "item": item_name, "quantity_added": quantity, "inventory_size": len(inventory)}


@tool
def remove_from_inventory(player_id: str, item_name: str, quantity: int = 1) -> dict[str, Any]:
    """Remove an item from the player's inventory (dropped, destroyed, stolen, consumed).

    Args:
        player_id: The player's ID.
        item_name: Name (or item id) of the item to remove.
        quantity: How many to remove.

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        inventory = _normalize_inventory(player.inventory)
        item_id = item_name.lower().replace(" ", "_")

        for i, entry in enumerate(inventory):
            if entry.get("id") == item_id or entry.get("name", "").lower() == item_name.lower():
                current = entry.get("quantity", 1)
                if current > quantity:
                    entry["quantity"] = current - quantity
                    removed = quantity
                else:
                    inventory.pop(i)
                    removed = current
                player.inventory = inventory
                session.commit()
                return {"success": True, "removed": entry.get("name", item_name), "quantity_removed": removed}

        return {"error": f"'{item_name}' not in inventory"}

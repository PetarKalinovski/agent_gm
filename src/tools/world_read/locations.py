"""Tools for location read operations."""

from typing import Any

from strands import tool

from src.models import (
    Connection,
    Location,
    NPC,
    Player,
    get_session,
)


@tool
def get_current_location(player_id: str) -> dict[str, Any]:
    """Get the player's current location with full details.

    Args:
        player_id: The player's ID.

    Returns:
        Dictionary with location details including description, NPCs present, and atmosphere.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player or not player.current_location_id:
            return {"error": "Player or location not found"}

        location = session.get(Location, player.current_location_id)
        if not location:
            return {"error": "Location not found"}

        # Get NPCs at this location
        npcs = session.query(NPC).filter(
            NPC.current_location_id == location.id,
            NPC.status == "alive"
        ).all()

        return {
            "id": location.id,
            "name": location.name,
            "type": location.type.value,
            "description": location.description,
            "atmosphere_tags": location.atmosphere_tags,
            "current_state": location.current_state,
            "controlling_faction": location.controlling_faction.name if location.controlling_faction else None,
            "npcs_present": [
                {"id": npc.id, "name": npc.name, "profession": npc.profession}
                for npc in npcs
            ],
            "visited_before": location.visited,
            "position": {"x": location.position_x, "y": location.position_y},
            "is_map_container": location.is_map_container,
        }


@tool
def get_location(location_id: str) -> dict[str, Any]:
    """Get details of a specific location.

    Args:
        location_id: The location's ID.

    Returns:
        Dictionary with location details.
    """
    with get_session() as session:
        location = session.get(Location, location_id)
        if not location:
            return {"error": "Location not found"}

        return {
            "id": location.id,
            "name": location.name,
            "type": location.type.value,
            "description": location.description,
            "atmosphere_tags": location.atmosphere_tags,
            "current_state": location.current_state,
            "parent_id": location.parent_id,
            "controlling_faction_id": location.controlling_faction_id,
            "position": {"x": location.position_x, "y": location.position_y},
            "display_type": location.display_type,
            "is_map_container": location.is_map_container,
            "map_image_path": location.map_image_path,
            "map_width": location.map_width,
            "map_height": location.map_height,
            "pin_icon": location.pin_icon,
            "pin_color": location.pin_color,
            "pin_size": location.pin_size,
            "discovered": location.discovered,
            "visited": location.visited,
        }


@tool
def get_all_locations(location_type: str | None = None, parent_id: str | None = None) -> list[dict[str, Any]]:
    """Get all locations, optionally filtered.

    Args:
        location_type: Optional location type to filter by.
        parent_id: Optional parent ID to get children of.

    Returns:
        List of locations.
    """
    with get_session() as session:
        query = session.query(Location)

        if location_type:
            query = query.filter(Location.type == location_type)
        if parent_id:
            query = query.filter(Location.parent_id == parent_id)

        locations = query.all()

        return [
            {
                "id": loc.id,
                "name": loc.name,
                "type": loc.type.value,
                "parent_id": loc.parent_id,
                "depth": loc.depth,
                "current_state": loc.current_state,
                "controlling_faction_id": loc.controlling_faction_id,
                "discovered": loc.discovered,
                "visited": loc.visited,
                "position": {"x": loc.position_x, "y": loc.position_y},
                "display_type": loc.display_type,
                "is_map_container": loc.is_map_container,
                "pin_icon": loc.pin_icon,
                "pin_color": loc.pin_color,
                "pin_size": loc.pin_size,
            }
            for loc in locations
        ]


@tool
def get_location_children(parent_id: str) -> list[dict[str, Any]]:
    """Get all child locations of a parent location.

    Args:
        parent_id: The parent location's ID.

    Returns:
        List of child locations with map display info.
    """
    with get_session() as session:
        locations = session.query(Location).filter(
            Location.parent_id == parent_id
        ).all()

        return [
            {
                "id": loc.id,
                "name": loc.name,
                "type": loc.type.value,
                "position": {"x": loc.position_x, "y": loc.position_y},
                "display_type": loc.display_type,
                "pin_icon": loc.pin_icon,
                "pin_color": loc.pin_color,
                "pin_size": loc.pin_size,
                "discovered": loc.discovered,
                "visited": loc.visited,
                "is_map_container": loc.is_map_container,
                "current_state": loc.current_state,
            }
            for loc in locations
        ]


@tool
def get_available_destinations(location_id: str) -> list[dict[str, Any]]:
    """Get locations the player can travel to from current location.

    Args:
        location_id: Current location ID.

    Returns:
        List of connected destinations.
    """
    with get_session() as session:
        # Get connections from this location
        connections = session.query(Connection).filter(
            Connection.from_location_id == location_id,
            Connection.discovered == True
        ).all()

        # Also get bidirectional connections
        reverse_connections = session.query(Connection).filter(
            Connection.to_location_id == location_id,
            Connection.bidirectional == True,
            Connection.discovered == True
        ).all()

        destinations = []
        seen_ids = set()

        for conn in connections:
            if conn.to_location_id not in seen_ids:
                loc = session.get(Location, conn.to_location_id)
                if loc:
                    destinations.append({
                        "id": loc.id,
                        "name": loc.name,
                        "type": loc.type.value,
                        "travel_type": conn.travel_type,
                        "travel_time_hours": conn.travel_time_hours,
                        "requirements": conn.requirements,
                        "position": {"x": loc.position_x, "y": loc.position_y},
                    })
                    seen_ids.add(conn.to_location_id)

        for conn in reverse_connections:
            if conn.from_location_id not in seen_ids:
                loc = session.get(Location, conn.from_location_id)
                if loc:
                    destinations.append({
                        "id": loc.id,
                        "name": loc.name,
                        "type": loc.type.value,
                        "travel_type": conn.travel_type,
                        "travel_time_hours": conn.travel_time_hours,
                        "requirements": conn.requirements,
                        "position": {"x": loc.position_x, "y": loc.position_y},
                    })
                    seen_ids.add(conn.from_location_id)

        # Also include child locations (can always go "inside")
        children = session.query(Location).filter(
            Location.parent_id == location_id,
            Location.discovered == True
        ).all()

        for child in children:
            if child.id not in seen_ids:
                destinations.append({
                    "id": child.id,
                    "name": child.name,
                    "type": child.type.value,
                    "travel_type": "enter",
                    "travel_time_hours": 0.1,
                    "requirements": [],
                    "position": {"x": child.position_x, "y": child.position_y},
                })

        # Can always go to parent (exit)
        current = session.get(Location, location_id)
        if current and current.parent_id:
            parent = session.get(Location, current.parent_id)
            if parent:
                destinations.append({
                    "id": parent.id,
                    "name": parent.name,
                    "type": parent.type.value,
                    "travel_type": "exit",
                    "travel_time_hours": 0.1,
                    "requirements": [],
                    "position": {"x": parent.position_x, "y": parent.position_y},
                })

        return destinations


@tool
def get_npcs_at_location(location_id: str) -> list[dict[str, Any]]:
    """Get all NPCs currently at a location.

    Args:
        location_id: The location's ID.

    Returns:
        List of NPCs with basic info.
    """
    with get_session() as session:
        npcs = session.query(NPC).filter(
            NPC.current_location_id == location_id,
            NPC.status == "alive"
        ).all()

        return [
            {
                "id": npc.id,
                "name": npc.name,
                "tier": npc.tier.value,
                "profession": npc.profession,
                "description_physical": npc.description_physical,
                "current_mood": npc.current_mood,
            }
            for npc in npcs
        ]


@tool
def get_location_hierarchy(location_id: str) -> list[dict[str, Any]]:
    """Get the full hierarchy path from root to this location.

    Args:
        location_id: The location's ID.

    Returns:
        List of locations from root to target, in order.
    """
    with get_session() as session:
        hierarchy = []
        current = session.get(Location, location_id)

        while current:
            hierarchy.insert(0, {
                "id": current.id,
                "name": current.name,
                "type": current.type.value,
                "is_map_container": current.is_map_container,
            })
            if current.parent_id:
                current = session.get(Location, current.parent_id)
            else:
                break

        return hierarchy

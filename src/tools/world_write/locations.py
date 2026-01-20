"""Tools for location write operations."""

from typing import Any

from strands import tool

from src.models import (
    Connection,
    Location,
    LocationType,
    get_session,
)


@tool
def add_location(
    name: str,
    description: str,
    location_type: LocationType,
    parent_id: str | None = None,
    travel_time_to_parent: float | None = None,
    position_x: float = 50.0,
    position_y: float = 50.0,
    display_type: str = "pin",
    is_map_container: bool = False,
    map_image_path: str | None = None,
    pin_icon: str = "circle",
    pin_color: str = "#3388ff",
    pin_size: float = 15.0,
    controlling_faction_id: str | None = None,
    atmosphere_tags: list[str] | None = None,
    discovered: bool = False,
) -> dict[str, Any]:
    """Add a new location to the world.

    Args:
        name: Name of the location.
        description: Description of the location.
        location_type: Type of location (e.g., city, building, room).
        parent_id: ID of the parent location (if any).
        travel_time_to_parent: Travel time to parent in hours. Defaults to 0.1 if parent_id is set.
        position_x: X position on parent map (0-100).
        position_y: Y position on parent map (0-100).
        display_type: How to display on map - "pin" (marker) or "area" (zone).
        is_map_container: Whether this location has a navigable map with children.
        map_image_path: Path to background map image for this location.
        pin_icon: Icon type for map marker (circle, star, square).
        pin_color: Hex color for the pin marker.
        pin_size: Size of the pin marker.
        controlling_faction_id: ID of faction controlling this location.
        atmosphere_tags: List of atmosphere tags (e.g., ["dangerous", "wealthy"]).
        discovered: Whether the location is already discovered.

    Returns:
        Dictionary with the created location's details.
    """
    with get_session() as session:
        location = Location(
            name=name,
            description=description,
            type=location_type,
            parent_id=parent_id,
            position_x=position_x,
            position_y=position_y,
            display_type=display_type,
            is_map_container=is_map_container,
            map_image_path=map_image_path,
            pin_icon=pin_icon,
            pin_color=pin_color,
            pin_size=pin_size,
            controlling_faction_id=controlling_faction_id,
            atmosphere_tags=atmosphere_tags or [],
            visited=False,
            discovered=discovered,
        )
        session.add(location)
        session.commit()

        # Add Connection to parent if applicable
        if parent_id:
            parent = session.get(Location, parent_id)
            if parent:
                if travel_time_to_parent is None:
                    travel_time_to_parent = 0.1

                conn = Connection(
                    from_location_id=parent_id,
                    to_location_id=location.id,
                    travel_time_hours=travel_time_to_parent,
                    bidirectional=True,
                    travel_type="walk",
                    discovered=True,
                )
                session.add(conn)
                session.commit()

        return {
            "id": location.id,
            "name": location.name,
            "type": location.type.value,
            "parent_id": location.parent_id,
            "position": {"x": location.position_x, "y": location.position_y},
        }


@tool
def update_location(
    location_id: str,
    name: str | None = None,
    description: str | None = None,
    location_type: LocationType | None = None,
    position_x: float | None = None,
    position_y: float | None = None,
    display_type: str | None = None,
    is_map_container: bool | None = None,
    map_image_path: str | None = None,
    map_width: int | None = None,
    map_height: int | None = None,
    pin_icon: str | None = None,
    pin_color: str | None = None,
    pin_size: float | None = None,
    controlling_faction_id: str | None = None,
    current_state: str | None = None,
    discovered: bool | None = None,
    visited: bool | None = None,
    parent_id: str | None = None,
) -> dict[str, Any]:
    """Update an existing location's details.

    Args:
        location_id: ID of the location to update.
        name: New name of the location.
        description: New description of the location.
        location_type: New type of location.
        position_x: New X position on parent map (0-100).
        position_y: New Y position on parent map (0-100).
        display_type: How to display - "pin" or "area".
        is_map_container: Whether this location has a navigable map.
        map_image_path: Path to background map image.
        map_width: Map image width in pixels.
        map_height: Map image height in pixels.
        pin_icon: Icon type for map marker.
        pin_color: Hex color for the pin.
        pin_size: Size of the pin marker.
        controlling_faction_id: ID of controlling faction.
        current_state: State of location (peaceful, under_siege, destroyed).
        discovered: Whether location is discovered.
        visited: Whether location has been visited.

    Returns:
        Dictionary with the updated location's details.
    """
    with get_session() as session:
        location = session.get(Location, location_id)
        if not location:
            return {"error": "Location not found"}

        if name is not None:
            location.name = name
        if description is not None:
            location.description = description
        if location_type is not None:
            location.type = location_type
        if position_x is not None:
            location.position_x = position_x
        if position_y is not None:
            location.position_y = position_y
        if display_type is not None:
            location.display_type = display_type
        if is_map_container is not None:
            location.is_map_container = is_map_container
        if map_image_path is not None:
            location.map_image_path = map_image_path
        if map_width is not None:
            location.map_width = map_width
        if map_height is not None:
            location.map_height = map_height
        if pin_icon is not None:
            location.pin_icon = pin_icon
        if pin_color is not None:
            location.pin_color = pin_color
        if pin_size is not None:
            location.pin_size = pin_size
        if controlling_faction_id is not None:
            location.controlling_faction_id = controlling_faction_id
        if current_state is not None:
            location.current_state = current_state
        if discovered is not None:
            location.discovered = discovered
        if visited is not None:
            location.visited = visited
        if parent_id is not None:
            location.parent_id = parent_id

        session.commit()

        return {
            "id": location.id,
            "name": location.name,
            "type": location.type.value,
            "position": {"x": location.position_x, "y": location.position_y},
            "updated": True,
        }


@tool
def delete_location(location_id: str) -> dict[str, Any]:
    """Delete a location from the world.

    Args:
        location_id: ID of the location to delete.

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        location = session.get(Location, location_id)
        if not location:
            return {"error": "Location not found"}

        session.delete(location)
        session.commit()

        return {"success": True, "deleted_location_id": location_id}


@tool
def add_location_connection(
    from_location_id: str,
    to_location_id: str,
    travel_type: str,
    travel_time_hours: float,
    bidirectional: bool = True,
    difficulty: int = 0,
    description: str = "",
    requirements: list[str] | None = None,
    discovered: bool = True,
) -> dict[str, Any]:
    """Add a connection between two locations.

    Args:
        from_location_id: ID of the starting location.
        to_location_id: ID of the destination location.
        travel_type: Type of travel (e.g., road, hyperspace, stairs).
        travel_time_hours: Travel time in hours.
        bidirectional: Whether the connection is bidirectional.
        difficulty: Difficulty level 0-100.
        description: Description of the route.
        requirements: List of requirements to use this route.
        discovered: Whether the connection is discovered.

    Returns:
        Dictionary with the created connection's details.
    """
    with get_session() as session:
        conn = Connection(
            from_location_id=from_location_id,
            to_location_id=to_location_id,
            travel_type=travel_type,
            travel_time_hours=travel_time_hours,
            bidirectional=bidirectional,
            difficulty=difficulty,
            description=description,
            requirements=requirements or [],
            discovered=discovered,
        )
        session.add(conn)
        session.commit()

        return {
            "id": conn.id,
            "from_location_id": conn.from_location_id,
            "to_location_id": conn.to_location_id,
            "travel_type": conn.travel_type,
            "travel_time_hours": conn.travel_time_hours,
        }


@tool
def update_connection(
    connection_id: str,
    travel_type: str | None = None,
    travel_time_hours: float | None = None,
    difficulty: int | None = None,
    description: str | None = None,
    bidirectional: bool | None = None,
    discovered: bool | None = None,
) -> dict[str, Any]:
    """Update an existing connection.

    Args:
        connection_id: ID of the connection to update.
        travel_type: New travel type.
        travel_time_hours: New travel time in hours.
        difficulty: New difficulty level.
        description: New description.
        bidirectional: Whether connection is bidirectional.
        discovered: Whether connection is discovered.

    Returns:
        Dictionary with updated connection details.
    """
    with get_session() as session:
        conn = session.get(Connection, connection_id)
        if not conn:
            return {"error": "Connection not found"}

        if travel_type is not None:
            conn.travel_type = travel_type
        if travel_time_hours is not None:
            conn.travel_time_hours = travel_time_hours
        if difficulty is not None:
            conn.difficulty = difficulty
        if description is not None:
            conn.description = description
        if bidirectional is not None:
            conn.bidirectional = bidirectional
        if discovered is not None:
            conn.discovered = discovered

        session.commit()

        return {
            "id": conn.id,
            "travel_type": conn.travel_type,
            "travel_time_hours": conn.travel_time_hours,
            "updated": True,
        }


@tool
def delete_connection(connection_id: str) -> dict[str, Any]:
    """Delete a connection between locations.

    Args:
        connection_id: ID of the connection to delete.

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        conn = session.get(Connection, connection_id)
        if not conn:
            return {"error": "Connection not found"}

        session.delete(conn)
        session.commit()

        return {"success": True, "deleted_connection_id": connection_id}

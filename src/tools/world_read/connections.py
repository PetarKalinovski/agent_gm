"""Tools for connection read operations."""

from typing import Any

from strands import tool

from src.models import (
    Connection,
    Location,
    get_session,
)


@tool
def get_all_connections(from_location_id: str | None = None, to_location_id: str | None = None) -> list[dict[str, Any]]:
    """Get all connections between locations, optionally filtered.

    Args:
        from_location_id: Optional from location ID to filter by.
        to_location_id: Optional to location ID to filter by.

    Returns:
        List of connections with details.
    """
    with get_session() as session:
        query = session.query(Connection)

        if from_location_id:
            query = query.filter(Connection.from_location_id == from_location_id)
        if to_location_id:
            query = query.filter(Connection.to_location_id == to_location_id)

        connections = query.all()

        result = []
        for conn in connections:
            from_loc = session.get(Location, conn.from_location_id)
            to_loc = session.get(Location, conn.to_location_id)
            result.append({
                "id": conn.id,
                "from_location": {
                    "id": conn.from_location_id,
                    "name": from_loc.name if from_loc else "Unknown"
                },
                "to_location": {
                    "id": conn.to_location_id,
                    "name": to_loc.name if to_loc else "Unknown"
                },
                "travel_type": conn.travel_type,
                "travel_time_hours": conn.travel_time_hours,
                "difficulty": conn.difficulty,
                "requirements": conn.requirements,
                "bidirectional": conn.bidirectional,
                "discovered": conn.discovered,
            })

        return result

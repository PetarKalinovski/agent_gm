"""Location repository for database operations."""

from typing import Any

from src.repositories.base import BaseRepository
from src.models import Location, LocationType, Connection, NPC
from src.core.results import Result, ErrorCodes


class LocationRepository(BaseRepository[Location]):
    """Repository for Location operations.

    Provides typed access to location data with common query patterns.
    """

    model_class = Location
    not_found_code = ErrorCodes.LOCATION_NOT_FOUND
    not_found_message = "Location not found"

    def get_by_id(self, location_id: str) -> Result[Location]:
        """Get location by ID.

        Args:
            location_id: The location's ID.

        Returns:
            Result containing the location or error.
        """
        return self._get_by_id(location_id)

    def get_by_name(self, name: str) -> Result[Location]:
        """Get location by name (first match).

        Args:
            name: The location's name.

        Returns:
            Result containing the location or error.
        """
        location = self.session.query(Location).filter(Location.name == name).first()
        if not location:
            return Result.fail("Location not found", ErrorCodes.LOCATION_NOT_FOUND)
        return Result.ok(location)

    def get_all(
        self,
        location_type: str | None = None,
        parent_id: str | None = None,
        faction_id: str | None = None,
        discovered: bool | None = None
    ) -> Result[list[Location]]:
        """Get all locations with optional filters.

        Args:
            location_type: Optional type filter.
            parent_id: Optional parent ID filter.
            faction_id: Optional controlling faction filter.
            discovered: Optional discovery status filter.

        Returns:
            Result containing list of locations.
        """
        query = self.session.query(Location)

        if location_type:
            try:
                type_enum = LocationType(location_type)
                query = query.filter(Location.type == type_enum)
            except ValueError:
                pass

        if parent_id:
            query = query.filter(Location.parent_id == parent_id)

        if faction_id:
            query = query.filter(Location.controlling_faction_id == faction_id)

        if discovered is not None:
            query = query.filter(Location.discovered == discovered)

        return Result.ok(query.all())

    def get_children(self, location_id: str) -> Result[list[Location]]:
        """Get all child locations of a location.

        Args:
            location_id: The parent location's ID.

        Returns:
            Result containing list of child locations.
        """
        children = self.session.query(Location).filter(
            Location.parent_id == location_id
        ).all()
        return Result.ok(children)

    def get_hierarchy(self, location_id: str) -> Result[list[Location]]:
        """Get the full hierarchy from a location to root.

        Args:
            location_id: Starting location's ID.

        Returns:
            Result containing list of locations from child to root.
        """
        result = self.get_by_id(location_id)
        if not result.success:
            return result

        hierarchy = []
        current = result.data

        while current:
            hierarchy.append(current)
            if current.parent_id:
                current = self.session.get(Location, current.parent_id)
            else:
                current = None

        return Result.ok(hierarchy)

    def get_with_npcs(self, location_id: str) -> Result[dict[str, Any]]:
        """Get location with all NPCs present.

        Args:
            location_id: The location's ID.

        Returns:
            Result containing location and list of NPCs.
        """
        location = self.session.get(Location, location_id)
        if not location:
            return Result.fail("Location not found", ErrorCodes.LOCATION_NOT_FOUND)

        npcs = self.session.query(NPC).filter(
            NPC.current_location_id == location_id,
            NPC.status == "alive"
        ).all()

        return Result.ok({
            "location": location,
            "npcs": npcs,
        })

    def get_connections(
        self,
        location_id: str,
        include_hidden: bool = False
    ) -> Result[list[Connection]]:
        """Get all travel connections from a location.

        Args:
            location_id: The location's ID.
            include_hidden: Whether to include hidden/undiscovered connections.

        Returns:
            Result containing list of connections.
        """
        query = self.session.query(Connection).filter(
            Connection.from_location_id == location_id
        )

        if not include_hidden:
            query = query.filter(
                (Connection.hidden == False) | (Connection.discovered == True)
            )

        connections = query.all()

        # Also get reverse connections if bidirectional
        reverse_query = self.session.query(Connection).filter(
            Connection.to_location_id == location_id,
            Connection.bidirectional == True
        )

        if not include_hidden:
            reverse_query = reverse_query.filter(
                (Connection.hidden == False) | (Connection.discovered == True)
            )

        reverse_connections = reverse_query.all()

        return Result.ok(connections + reverse_connections)

    def get_available_destinations(
        self,
        location_id: str
    ) -> Result[list[dict[str, Any]]]:
        """Get all available destinations from a location.

        Args:
            location_id: The starting location's ID.

        Returns:
            Result containing list of destination info dicts.
        """
        connections_result = self.get_connections(location_id)
        if not connections_result.success:
            return connections_result

        destinations = []
        for conn in connections_result.data:
            # Determine destination ID
            if conn.from_location_id == location_id:
                dest_id = conn.to_location_id
            else:
                dest_id = conn.from_location_id

            dest = self.session.get(Location, dest_id)
            if dest and dest.discovered:
                destinations.append({
                    "id": dest.id,
                    "name": dest.name,
                    "type": dest.type.value,
                    "travel_type": conn.travel_type,
                    "travel_time_hours": conn.travel_time_hours,
                    "difficulty": conn.difficulty,
                })

        return Result.ok(destinations)

    def mark_visited(self, location_id: str, day: int) -> Result[Location]:
        """Mark location as visited.

        Args:
            location_id: The location's ID.
            day: Current game day.

        Returns:
            Result containing updated location.
        """
        result = self.get_by_id(location_id)
        if not result.success:
            return result

        location = result.data
        location.visited = True
        location.discovered = True
        location.last_visited_day = day

        return Result.ok(location)

    def discover(self, location_id: str) -> Result[Location]:
        """Mark location as discovered (but not visited).

        Args:
            location_id: The location's ID.

        Returns:
            Result containing updated location.
        """
        result = self.get_by_id(location_id)
        if not result.success:
            return result

        location = result.data
        location.discovered = True

        return Result.ok(location)

    def update_state(self, location_id: str, state: str) -> Result[Location]:
        """Update location state.

        Args:
            location_id: The location's ID.
            state: New state (peaceful, under_siege, destroyed, etc.).

        Returns:
            Result containing updated location.
        """
        result = self.get_by_id(location_id)
        if not result.success:
            return result

        location = result.data
        location.current_state = state

        return Result.ok(location)

    def to_dict(self, location: Location) -> dict[str, Any]:
        """Convert location to dictionary for tool responses.

        Args:
            location: The Location model instance.

        Returns:
            Dictionary representation of the location.
        """
        return {
            "id": location.id,
            "name": location.name,
            "type": location.type.value,
            "parent_id": location.parent_id,
            "description": location.description,
            "atmosphere_tags": location.atmosphere_tags,
            "current_state": location.current_state,
            "controlling_faction_id": location.controlling_faction_id,
            "visited": location.visited,
            "discovered": location.discovered,
        }

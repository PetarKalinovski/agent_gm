"""Player repository for database operations."""

from typing import Any

from src.repositories.base import BaseRepository
from src.models import Player, Location
from src.core.results import Result, ErrorCodes


class PlayerRepository(BaseRepository[Player]):
    """Repository for Player operations.

    Provides typed access to player data with common query patterns.
    """

    model_class = Player
    not_found_code = ErrorCodes.PLAYER_NOT_FOUND
    not_found_message = "Player not found"

    def get_by_id(self, player_id: str) -> Result[Player]:
        """Get player by ID.

        Args:
            player_id: The player's ID.

        Returns:
            Result containing the player or error.
        """
        return self._get_by_id(player_id)

    def get_by_name(self, name: str) -> Result[Player]:
        """Get player by name.

        Args:
            name: The player's name.

        Returns:
            Result containing the player or error.
        """
        player = self.session.query(Player).filter(Player.name == name).first()
        if not player:
            return Result.fail("Player not found", ErrorCodes.PLAYER_NOT_FOUND)
        return Result.ok(player)

    def get_with_location(self, player_id: str) -> Result[dict[str, Any]]:
        """Get player with current location data in a single query.

        Args:
            player_id: The player's ID.

        Returns:
            Result containing player and location data.
        """
        player = self.session.get(Player, player_id)
        if not player:
            return Result.fail("Player not found", ErrorCodes.PLAYER_NOT_FOUND)

        location = None
        if player.current_location_id:
            location = self.session.get(Location, player.current_location_id)

        return Result.ok({
            "player": player,
            "location": location,
        })

    def get_all(self) -> Result[list[Player]]:
        """Get all players.

        Returns:
            Result containing list of all players.
        """
        return self._get_all()

    def update_position(
        self,
        player_id: str,
        position_x: float,
        position_y: float,
        direction: str | None = None
    ) -> Result[Player]:
        """Update player position.

        Args:
            player_id: The player's ID.
            position_x: New X position.
            position_y: New Y position.
            direction: Optional new facing direction.

        Returns:
            Result containing updated player.
        """
        result = self.get_by_id(player_id)
        if not result.success:
            return result

        player = result.data
        player.position_x = position_x
        player.position_y = position_y
        if direction:
            player.facing_direction = direction

        return Result.ok(player)

    def move_to_location(self, player_id: str, location_id: str) -> Result[Player]:
        """Move player to a new location.

        Args:
            player_id: The player's ID.
            location_id: The target location's ID.

        Returns:
            Result containing updated player.
        """
        result = self.get_by_id(player_id)
        if not result.success:
            return result

        player = result.data
        player.current_location_id = location_id

        return Result.ok(player)

    def update_health(self, player_id: str, health_status: str) -> Result[Player]:
        """Update player health status.

        Args:
            player_id: The player's ID.
            health_status: New health status.

        Returns:
            Result containing updated player.
        """
        result = self.get_by_id(player_id)
        if not result.success:
            return result

        player = result.data
        player.health_status = health_status

        return Result.ok(player)

    def add_to_inventory(
        self,
        player_id: str,
        item: dict[str, Any]
    ) -> Result[Player]:
        """Add item to player inventory.

        Args:
            player_id: The player's ID.
            item: Item data to add.

        Returns:
            Result containing updated player.
        """
        result = self.get_by_id(player_id)
        if not result.success:
            return result

        player = result.data
        inventory = player.inventory.copy() if player.inventory else []
        inventory.append(item)
        player.inventory = inventory

        return Result.ok(player)

    def remove_from_inventory(
        self,
        player_id: str,
        item_id: str
    ) -> Result[Player]:
        """Remove item from player inventory.

        Args:
            player_id: The player's ID.
            item_id: The item's ID to remove.

        Returns:
            Result containing updated player.
        """
        result = self.get_by_id(player_id)
        if not result.success:
            return result

        player = result.data
        inventory = player.inventory.copy() if player.inventory else []
        inventory = [i for i in inventory if i.get("id") != item_id]
        player.inventory = inventory

        return Result.ok(player)

    def update_currency(self, player_id: str, delta: int) -> Result[Player]:
        """Update player currency.

        Args:
            player_id: The player's ID.
            delta: Amount to add (positive) or remove (negative).

        Returns:
            Result containing updated player or error if insufficient funds.
        """
        result = self.get_by_id(player_id)
        if not result.success:
            return result

        player = result.data
        new_amount = player.currency + delta

        if new_amount < 0:
            return Result.fail(
                "Insufficient funds",
                ErrorCodes.INSUFFICIENT_FUNDS
            )

        player.currency = new_amount
        return Result.ok(player)

    def add_party_member(self, player_id: str, member_id: str) -> Result[Player]:
        """Add NPC to player's party.

        Args:
            player_id: The player's ID.
            member_id: The NPC's ID to add to party.

        Returns:
            Result containing updated player.
        """
        result = self.get_by_id(player_id)
        if not result.success:
            return result

        player = result.data
        party = player.party_members.copy() if player.party_members else []

        if member_id not in party:
            party.append(member_id)
            player.party_members = party

        return Result.ok(player)

    def remove_party_member(self, player_id: str, member_id: str) -> Result[Player]:
        """Remove NPC from player's party.

        Args:
            player_id: The player's ID.
            member_id: The NPC's ID to remove from party.

        Returns:
            Result containing updated player.
        """
        result = self.get_by_id(player_id)
        if not result.success:
            return result

        player = result.data
        party = player.party_members.copy() if player.party_members else []
        party = [m for m in party if m != member_id]
        player.party_members = party

        return Result.ok(player)

    def add_quest(self, player_id: str, quest_id: str) -> Result[Player]:
        """Add quest to player's active quests.

        Args:
            player_id: The player's ID.
            quest_id: The quest's ID.

        Returns:
            Result containing updated player.
        """
        result = self.get_by_id(player_id)
        if not result.success:
            return result

        player = result.data
        quests = player.active_quests.copy() if player.active_quests else []

        if quest_id not in quests:
            quests.append(quest_id)
            player.active_quests = quests

        return Result.ok(player)

    def complete_quest(self, player_id: str, quest_id: str) -> Result[Player]:
        """Move quest from active to completed.

        Args:
            player_id: The player's ID.
            quest_id: The quest's ID.

        Returns:
            Result containing updated player.
        """
        result = self.get_by_id(player_id)
        if not result.success:
            return result

        player = result.data

        # Remove from active
        active = player.active_quests.copy() if player.active_quests else []
        active = [q for q in active if q != quest_id]
        player.active_quests = active

        # Add to completed
        completed = player.completed_quests.copy() if player.completed_quests else []
        if quest_id not in completed:
            completed.append(quest_id)
            player.completed_quests = completed

        return Result.ok(player)

"""NPC repository for database operations."""

from typing import Any

from src.repositories.base import BaseRepository
from src.models import NPC, NPCTier, NPCRelationship
from src.core.results import Result, ErrorCodes


class NPCRepository(BaseRepository[NPC]):
    """Repository for NPC operations.

    Provides typed access to NPC data with common query patterns.
    """

    model_class = NPC
    not_found_code = ErrorCodes.NPC_NOT_FOUND
    not_found_message = "NPC not found"

    def get_by_id(self, npc_id: str) -> Result[NPC]:
        """Get NPC by ID.

        Args:
            npc_id: The NPC's ID.

        Returns:
            Result containing the NPC or error.
        """
        return self._get_by_id(npc_id)

    def get_by_name(self, name: str) -> Result[NPC]:
        """Get NPC by name (first match).

        Args:
            name: The NPC's name.

        Returns:
            Result containing the NPC or error.
        """
        npc = self.session.query(NPC).filter(NPC.name == name).first()
        if not npc:
            return Result.fail("NPC not found", ErrorCodes.NPC_NOT_FOUND)
        return Result.ok(npc)

    def get_all(
        self,
        tier: str | None = None,
        faction_id: str | None = None,
        location_id: str | None = None,
        status: str | None = None
    ) -> Result[list[NPC]]:
        """Get all NPCs with optional filters.

        Args:
            tier: Optional tier filter (major, minor, ambient).
            faction_id: Optional faction ID filter.
            location_id: Optional location ID filter.
            status: Optional status filter (alive, dead, etc.).

        Returns:
            Result containing list of NPCs.
        """
        query = self.session.query(NPC)

        if tier:
            try:
                tier_enum = NPCTier(tier)
                query = query.filter(NPC.tier == tier_enum)
            except ValueError:
                pass

        if faction_id:
            query = query.filter(NPC.faction_id == faction_id)

        if location_id:
            query = query.filter(NPC.current_location_id == location_id)

        if status:
            query = query.filter(NPC.status == status)

        return Result.ok(query.all())

    def get_at_location(
        self,
        location_id: str,
        include_dead: bool = False
    ) -> Result[list[NPC]]:
        """Get all NPCs at a specific location.

        Args:
            location_id: The location's ID.
            include_dead: Whether to include dead NPCs.

        Returns:
            Result containing list of NPCs at the location.
        """
        query = self.session.query(NPC).filter(
            NPC.current_location_id == location_id
        )

        if not include_dead:
            query = query.filter(NPC.status == "alive")

        return Result.ok(query.all())

    def get_with_relationship(
        self,
        npc_id: str,
        player_id: str
    ) -> Result[tuple[NPC, NPCRelationship | None]]:
        """Get NPC and their relationship with a player in a single query.

        Args:
            npc_id: The NPC's ID.
            player_id: The player's ID.

        Returns:
            Result containing tuple of (NPC, relationship or None).
        """
        npc = self.session.get(NPC, npc_id)
        if not npc:
            return Result.fail("NPC not found", ErrorCodes.NPC_NOT_FOUND)

        relationship = self.session.query(NPCRelationship).filter(
            NPCRelationship.npc_id == npc_id,
            NPCRelationship.player_id == player_id
        ).first()

        return Result.ok((npc, relationship))

    def validate_for_conversation(self, npc_id: str) -> Result[NPC]:
        """Validate NPC exists and can participate in conversation.

        This is the key method to fix the "nothing to say" bug.

        Args:
            npc_id: The NPC's ID.

        Returns:
            Result containing the NPC or error with specific reason.
        """
        npc = self.session.get(NPC, npc_id)

        if not npc:
            return Result.fail(
                f"Cannot find anyone to talk to (NPC {npc_id} not found)",
                ErrorCodes.NPC_NOT_FOUND
            )

        if npc.status != "alive":
            return Result.fail(
                f"{npc.name} is not able to speak (status: {npc.status})",
                ErrorCodes.NPC_UNAVAILABLE
            )

        return Result.ok(npc)

    def move_to_location(self, npc_id: str, location_id: str) -> Result[NPC]:
        """Move NPC to a new location.

        Args:
            npc_id: The NPC's ID.
            location_id: The target location's ID.

        Returns:
            Result containing updated NPC.
        """
        result = self.get_by_id(npc_id)
        if not result.success:
            return result

        npc = result.data
        npc.current_location_id = location_id

        return Result.ok(npc)

    def update_mood(self, npc_id: str, mood: str) -> Result[NPC]:
        """Update NPC's current mood.

        Args:
            npc_id: The NPC's ID.
            mood: New mood state.

        Returns:
            Result containing updated NPC.
        """
        result = self.get_by_id(npc_id)
        if not result.success:
            return result

        npc = result.data
        npc.current_mood = mood

        return Result.ok(npc)

    def update_status(self, npc_id: str, status: str) -> Result[NPC]:
        """Update NPC's status (alive, dead, etc.).

        Args:
            npc_id: The NPC's ID.
            status: New status.

        Returns:
            Result containing updated NPC.
        """
        result = self.get_by_id(npc_id)
        if not result.success:
            return result

        npc = result.data
        npc.status = status

        return Result.ok(npc)

    def add_goal(self, npc_id: str, goal: str) -> Result[NPC]:
        """Add a goal to NPC.

        Args:
            npc_id: The NPC's ID.
            goal: Goal to add.

        Returns:
            Result containing updated NPC.
        """
        result = self.get_by_id(npc_id)
        if not result.success:
            return result

        npc = result.data
        goals = npc.goals.copy() if npc.goals else []
        if goal not in goals:
            goals.append(goal)
            npc.goals = goals

        return Result.ok(npc)

    def add_secret(self, npc_id: str, secret: str) -> Result[NPC]:
        """Add a secret to NPC.

        Args:
            npc_id: The NPC's ID.
            secret: Secret to add.

        Returns:
            Result containing updated NPC.
        """
        result = self.get_by_id(npc_id)
        if not result.success:
            return result

        npc = result.data
        secrets = npc.secrets.copy() if npc.secrets else []
        if secret not in secrets:
            secrets.append(secret)
            npc.secrets = secrets

        return Result.ok(npc)

    def to_dict(self, npc: NPC) -> dict[str, Any]:
        """Convert NPC to dictionary for tool responses.

        Args:
            npc: The NPC model instance.

        Returns:
            Dictionary representation of the NPC.
        """
        return {
            "id": npc.id,
            "name": npc.name,
            "tier": npc.tier.value,
            "species": npc.species,
            "profession": npc.profession,
            "faction_id": npc.faction_id,
            "current_location_id": npc.current_location_id,
            "description_physical": npc.description_physical,
            "description_personality": npc.description_personality,
            "voice_pattern": npc.voice_pattern,
            "goals": npc.goals,
            "secrets": npc.secrets,
            "current_mood": npc.current_mood,
            "status": npc.status,
        }

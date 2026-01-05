"""Tools for NPC read operations."""

from typing import Any

from strands import tool

from src.models import (
    NPC,
    NPCRelationship,
    NPCTier,
    get_session,
)


@tool
def get_npc(npc_id: str) -> dict[str, Any]:
    """Get full details of an NPC.

    Args:
        npc_id: The NPC's ID.

    Returns:
        Dictionary with NPC details.
    """
    with get_session() as session:
        npc = session.get(NPC, npc_id)
        if not npc:
            return {"error": "NPC not found"}

        return {
            "id": npc.id,
            "name": npc.name,
            "tier": npc.tier.value,
            "species": npc.species,
            "profession": npc.profession,
            "faction_id": npc.faction_id,
            "description_physical": npc.description_physical,
            "description_personality": npc.description_personality,
            "voice_pattern": npc.voice_pattern,
            "goals": npc.goals,
            "current_mood": npc.current_mood,
            "status": npc.status,
        }


@tool
def get_all_npcs(
    tier: str | None = None,
    faction_id: str | None = None,
    location_id: str | None = None,
) -> list[dict[str, Any]]:
    """Get all NPCs, optionally filtered.

    Args:
        tier: Optional tier to filter by (major, minor, ambient).
        faction_id: Optional faction ID to filter by.
        location_id: Optional location ID to filter by.

    Returns:
        List of NPCs with basic info.
    """
    with get_session() as session:
        query = session.query(NPC)

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

        npcs = query.all()

        return [
            {
                "id": npc.id,
                "name": npc.name,
                "tier": npc.tier.value,
                "profession": npc.profession,
                "faction_id": npc.faction_id,
                "current_location_id": npc.current_location_id,
                "status": npc.status,
            }
            for npc in npcs
        ]


@tool
def get_npc_relationship(npc_id: str, player_id: str) -> dict[str, Any]:
    """Get the relationship between an NPC and the player.

    Args:
        npc_id: The NPC's ID.
        player_id: The player's ID.

    Returns:
        Dictionary with relationship details.
    """
    with get_session() as session:
        rel = session.query(NPCRelationship).filter(
            NPCRelationship.npc_id == npc_id,
            NPCRelationship.player_id == player_id
        ).first()

        if not rel:
            return {
                "summary": "You have not met this person before.",
                "trust_level": 50,
                "current_disposition": "neutral",
                "key_moments": [],
                "recent_messages": [],
            }

        return {
            "summary": rel.summary,
            "trust_level": rel.trust_level,
            "current_disposition": rel.current_disposition,
            "key_moments": rel.key_moments,
            "recent_messages": rel.recent_messages[-10:],  # Last 10 messages
            "revealed_secrets": rel.revealed_secrets,
        }

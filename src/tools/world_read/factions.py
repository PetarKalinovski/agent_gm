"""Tools for faction read operations."""

from typing import Any

from strands import tool

from src.models import (
    Faction,
    FactionRelationship,
    Player,
    get_session,
)


@tool
def get_faction(faction_id: str) -> dict[str, Any]:
    """Get details of a faction.

    Args:
        faction_id: The faction's ID.

    Returns:
        Dictionary with faction details.
    """
    with get_session() as session:
        faction = session.get(Faction, faction_id)
        if not faction:
            return {"error": "Faction not found"}

        return {
            "id": faction.id,
            "name": faction.name,
            "ideology": faction.ideology,
            "power_level": faction.power_level,
            "goals_short": faction.goals_short,
            "goals_long": faction.goals_long,
        }


@tool
def get_all_factions() -> list[dict[str, Any]]:
    """Get all factions in the world.

    Returns:
        List of factions with basic info.
    """
    with get_session() as session:
        factions = session.query(Faction).all()

        return [
            {
                "id": f.id,
                "name": f.name,
                "power_level": f.power_level,
                "ideology": f.ideology[:100] + "..." if len(f.ideology) > 100 else f.ideology,
            }
            for f in factions
        ]


@tool
def get_faction_full(faction_id: str) -> dict[str, Any]:
    """Get complete details of a faction including secrets and relationships.

    Args:
        faction_id: The faction's ID.

    Returns:
        Dictionary with full faction details.
    """
    with get_session() as session:
        faction = session.get(Faction, faction_id)
        if not faction:
            return {"error": "Faction not found"}

        # Get relationships
        relationships = session.query(FactionRelationship).filter(
            (FactionRelationship.faction_a_id == faction_id) |
            (FactionRelationship.faction_b_id == faction_id)
        ).all()

        rel_list = []
        for rel in relationships:
            other_id = rel.faction_b_id if rel.faction_a_id == faction_id else rel.faction_a_id
            other = session.get(Faction, other_id)
            rel_list.append({
                "faction_id": other_id,
                "faction_name": other.name if other else "Unknown",
                "relationship_type": rel.relationship_type,
                "stability": rel.stability,
            })

        return {
            "id": faction.id,
            "name": faction.name,
            "ideology": faction.ideology,
            "methods": faction.methods,
            "aesthetic": faction.aesthetic,
            "power_level": faction.power_level,
            "resources": faction.resources,
            "goals_short": faction.goals_short,
            "goals_long": faction.goals_long,
            "leadership": faction.leadership,
            "secrets": faction.secrets,
            "history_notes": faction.history_notes,
            "relationships": rel_list,
            "member_count": len(faction.members),
            "controlled_location_count": len(faction.controlled_locations),
        }


@tool
def get_faction_relationships(faction_id: str | None = None) -> list[dict[str, Any]]:
    """Get faction relationships, optionally filtered by faction.

    Args:
        faction_id: Optional faction ID to filter by. If None, returns all relationships.

    Returns:
        List of faction relationships.
    """
    with get_session() as session:
        query = session.query(FactionRelationship)

        if faction_id:
            query = query.filter(
                (FactionRelationship.faction_a_id == faction_id) |
                (FactionRelationship.faction_b_id == faction_id)
            )

        relationships = query.all()

        result = []
        for rel in relationships:
            faction_a = session.get(Faction, rel.faction_a_id)
            faction_b = session.get(Faction, rel.faction_b_id)
            result.append({
                "id": rel.id,
                "faction_a": {"id": rel.faction_a_id, "name": faction_a.name if faction_a else "Unknown"},
                "faction_b": {"id": rel.faction_b_id, "name": faction_b.name if faction_b else "Unknown"},
                "relationship_type": rel.relationship_type,
                "public_reason": rel.public_reason,
                "stability": rel.stability,
            })

        return result


@tool
def get_player_reputation(player_id: str, faction_id: str) -> dict[str, Any]:
    """Get the player's reputation with a faction.

    Args:
        player_id: The player's ID.
        faction_id: The faction's ID.

    Returns:
        Dictionary with reputation details.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        score = player.reputation.get(faction_id, 50)

        # Determine standing based on score
        if score >= 80:
            standing = "revered"
        elif score >= 60:
            standing = "friendly"
        elif score >= 40:
            standing = "neutral"
        elif score >= 20:
            standing = "unfriendly"
        else:
            standing = "hostile"

        return {
            "score": score,
            "standing": standing,
        }

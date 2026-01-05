"""Tools for NPC write operations."""

from typing import Any

from strands import tool

from src.models import (
    NPC,
    NPCRelationship,
    NPCTier,
    WorldClock,
    get_session,
)


@tool
def add_npc(
    name: str,
    tier: NPCTier,
    species: str | None,
    age: int | None,
    profession: str | None,
    faction_id: str | None,
    current_location_id: str | None,
    home_location_id: str | None,
    description_physical: str | None,
    description_personality: str | None,
    voice_pattern: str | None,
    goals: list[str] | None,
    secrets: list[str] | None,
    current_mood: str | None,
) -> dict[str, Any]:
    """Add a new NPC to the world.

    Args:
        name: Name of the NPC.
        tier: Tier of the NPC (e.g., main, secondary, minor).
        species: Species of the NPC.
        age: Age of the NPC.
        profession: Profession of the NPC.
        faction_id: ID of the faction the NPC belongs to.
        current_location_id: Current location ID of the NPC.
        home_location_id: Home location ID of the NPC.
        description_physical: Physical description of the NPC.
        description_personality: Personality description of the NPC.
        voice_pattern: Description of the NPC's voice pattern.
        goals: List of the NPC's goals.
        secrets: List of secrets the NPC holds.
        current_mood: Current mood of the NPC.

    Returns:
        Dictionary with the created NPC's details.
    """
    with get_session() as session:
        npc = NPC(
            name=name,
            tier=tier,
            species=species,
            age=age,
            profession=profession,
            faction_id=faction_id,
            current_location_id=current_location_id,
            home_location_id=home_location_id,
            description_physical=description_physical,
            description_personality=description_personality,
            voice_pattern=voice_pattern,
            goals=goals,
            secrets=secrets,
            current_mood=current_mood,
        )
        session.add(npc)
        session.commit()

        return {
            "id": npc.id,
            "name": npc.name,
            "profession": npc.profession,
            "current_location_id": npc.current_location_id,
        }


@tool
def update_npc(
    npc_id: str,
    description_physical: str | None = None,
    description_personality: str | None = None,
    voice_pattern: str | None = None,
    profession: str | None = None,
    status: str | None = None,
    add_goal: str | None = None,
    remove_goal: str | None = None,
    add_secret: str | None = None,
    remove_secret: str | None = None,
    add_skill: str | None = None,
    add_notable_item: str | None = None,
) -> dict[str, Any]:
    """Update an NPC's attributes, goals, or secrets.

    Use this when an NPC changes physically, gains new goals, or evolves their character.
    NPCs can call this on themselves to reflect changes from events or interactions.

    Args:
        npc_id: The NPC's ID.
        description_physical: New physical description (e.g., "now bears a scar across their left cheek").
        description_personality: New personality description.
        voice_pattern: New speech pattern description.
        profession: New profession.
        status: New status (alive, dead, missing, imprisoned).
        add_goal: Add a new goal to the NPC's goals list.
        remove_goal: Remove a goal from the NPC's goals list (exact match).
        add_secret: Add a new secret to the NPC's secrets list.
        remove_secret: Remove a secret from the NPC's secrets list (exact match).
        add_skill: Add a new skill to the NPC's skills list.
        add_notable_item: Add a notable item to the NPC's inventory.

    Returns:
        Dictionary with updated NPC details.
    """
    with get_session() as session:
        npc = session.get(NPC, npc_id)
        if not npc:
            return {"error": "NPC not found"}

        # Update simple attributes
        if description_physical is not None:
            npc.description_physical = description_physical
        if description_personality is not None:
            npc.description_personality = description_personality
        if voice_pattern is not None:
            npc.voice_pattern = voice_pattern
        if profession is not None:
            npc.profession = profession
        if status is not None:
            npc.status = status

        # Update goals
        if add_goal:
            goals = npc.goals.copy() if npc.goals else []
            if add_goal not in goals:
                goals.append(add_goal)
                npc.goals = goals
        if remove_goal:
            goals = npc.goals.copy() if npc.goals else []
            if remove_goal in goals:
                goals.remove(remove_goal)
                npc.goals = goals

        # Update secrets
        if add_secret:
            secrets = npc.secrets.copy() if npc.secrets else []
            if add_secret not in secrets:
                secrets.append(add_secret)
                npc.secrets = secrets
        if remove_secret:
            secrets = npc.secrets.copy() if npc.secrets else []
            if remove_secret in secrets:
                secrets.remove(remove_secret)
                npc.secrets = secrets

        # Update skills
        if add_skill:
            skills = npc.skills.copy() if npc.skills else []
            if add_skill not in skills:
                skills.append(add_skill)
                npc.skills = skills

        # Update inventory
        if add_notable_item:
            inventory = npc.inventory_notable.copy() if npc.inventory_notable else []
            if add_notable_item not in inventory:
                inventory.append(add_notable_item)
                npc.inventory_notable = inventory

        session.commit()

        return {
            "success": True,
            "npc_id": npc.id,
            "npc_name": npc.name,
            "updated_fields": {
                "physical": description_physical is not None,
                "personality": description_personality is not None,
                "voice": voice_pattern is not None,
                "profession": profession is not None,
                "status": status is not None,
                "goals": len(npc.goals),
                "secrets": len(npc.secrets),
                "skills": len(npc.skills),
            }
        }


@tool
def delete_npc(npc_id: str) -> dict[str, Any]:
    """Delete an NPC from the world.

    Args:
        npc_id: ID of the NPC to delete.

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        npc = session.get(NPC, npc_id)
        if not npc:
            return {"error": "NPC not found"}

        session.delete(npc)
        session.commit()

        return {"success": True, "deleted_npc_id": npc_id}


@tool
def move_npc(npc_id: str, destination_id: str) -> dict[str, Any]:
    """Move an NPC to a new location.

    Use this when an NPC travels with the player or moves on their own.

    Args:
        npc_id: The NPC's ID.
        destination_id: The destination location ID.

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        npc = session.get(NPC, npc_id)
        if not npc:
            return {"error": "NPC not found"}

        from src.models import Location
        destination = session.get(Location, destination_id)
        if not destination:
            return {"error": "Destination not found"}

        # Update NPC location
        old_location = npc.current_location_id
        npc.current_location_id = destination_id
        session.commit()

        return {
            "success": True,
            "npc_name": npc.name,
            "from_location": old_location,
            "to_location": destination.name,
        }


@tool
def update_npc_mood(npc_id: str, new_mood: str, reason: str = "") -> dict[str, Any]:
    """Update an NPC's current mood.

    Args:
        npc_id: The NPC's ID.
        new_mood: The new mood (e.g., "happy", "angry", "nervous").
        reason: Reason for the mood change.

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        npc = session.get(NPC, npc_id)
        if not npc:
            return {"error": "NPC not found"}

        npc.current_mood = new_mood
        session.commit()

        return {"success": True, "npc": npc.name, "new_mood": new_mood}


@tool
def update_npc_relationship(
    npc_id: str,
    player_id: str,
    trust_delta: int = 0,
    new_disposition: str | None = None,
    add_key_moment: str | None = None,
    add_message: dict | None = None
) -> dict[str, Any]:
    """Update the relationship between an NPC and player.

    Args:
        npc_id: The NPC's ID.
        player_id: The player's ID.
        trust_delta: Change in trust level (-100 to 100).
        new_disposition: New disposition (e.g., "friendly", "hostile").
        add_key_moment: A key moment to record.
        add_message: A message to add to recent history.

    Returns:
        Dictionary with updated relationship.
    """
    with get_session() as session:
        rel = session.query(NPCRelationship).filter(
            NPCRelationship.npc_id == npc_id,
            NPCRelationship.player_id == player_id
        ).first()

        if not rel:
            # Create new relationship
            rel = NPCRelationship(
                npc_id=npc_id,
                player_id=player_id,
                trust_level=50,
                current_disposition="neutral",
            )
            session.add(rel)

        # Update trust
        rel.trust_level = max(0, min(100, rel.trust_level + trust_delta))

        # Update disposition
        if new_disposition:
            rel.current_disposition = new_disposition

        # Add key moment
        if add_key_moment:
            moments = rel.key_moments.copy() if rel.key_moments else []
            moments.append(add_key_moment)
            rel.key_moments = moments

        # Add message
        if add_message:
            messages = rel.recent_messages.copy() if rel.recent_messages else []
            messages.append(add_message)
            # Keep only last 20 messages
            rel.recent_messages = messages[-20:]

        # Update last interaction
        clock = session.query(WorldClock).first()
        if clock:
            rel.last_interaction_day = clock.day

        session.commit()

        return {
            "trust_level": rel.trust_level,
            "current_disposition": rel.current_disposition,
        }


@tool
def reveal_secret(npc_id: str, player_id: str, secret_index: int) -> dict[str, Any]:
    """Mark a secret as revealed to the player.

    Args:
        npc_id: The NPC's ID.
        player_id: The player's ID.
        secret_index: Index of the secret in the NPC's secrets list.

    Returns:
        Dictionary with the revealed secret.
    """
    with get_session() as session:
        npc = session.get(NPC, npc_id)
        if not npc:
            return {"error": "NPC not found"}

        if secret_index >= len(npc.secrets):
            return {"error": "Secret index out of range"}

        rel = session.query(NPCRelationship).filter(
            NPCRelationship.npc_id == npc_id,
            NPCRelationship.player_id == player_id
        ).first()

        if not rel:
            rel = NPCRelationship(
                npc_id=npc_id,
                player_id=player_id,
            )
            session.add(rel)

        revealed = rel.revealed_secrets.copy() if rel.revealed_secrets else []
        if secret_index not in revealed:
            revealed.append(secret_index)
            rel.revealed_secrets = revealed

        session.commit()

        return {
            "secret": npc.secrets[secret_index],
            "total_secrets": len(npc.secrets),
            "revealed_count": len(revealed),
        }

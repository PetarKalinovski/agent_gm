"""Context assembly for the game session."""

from typing import Any

from src.models import (
    Event,
    Faction,
    Location,
    NPC,
    NPCRelationship,
    Player,
    WorldClock,
    get_session,
)


def assemble_context(player_id: str) -> dict[str, Any]:
    """Assemble the full context for a DM turn.

    Args:
        player_id: The player's ID.

    Returns:
        Dictionary with all relevant context.
    """
    with get_session() as session:
        # Get player
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        # Get current location
        location = None
        if player.current_location_id:
            location = session.get(Location, player.current_location_id)

        # Get world clock
        clock = session.query(WorldClock).first()

        # Get NPCs at current location
        npcs_here = []
        if location:
            npcs_here = session.query(NPC).filter(
                NPC.current_location_id == location.id,
                NPC.status == "alive"
            ).all()

        # Get recent events
        recent_events = []
        if clock:
            recent_events = session.query(Event).filter(
                Event.occurred_day >= clock.day - 7,
                Event.player_visible == True
            ).order_by(Event.occurred_day.desc()).limit(10).all()

        # Get controlling faction info
        controlling_faction = None
        if location and location.controlling_faction_id:
            controlling_faction = session.get(Faction, location.controlling_faction_id)

        # Build context
        context = {
            "player": {
                "id": player.id,
                "name": player.name,
                "health_status": player.health_status,
                "traits": player.traits,
                "inventory": player.inventory,
                "active_quests": player.active_quests,
                "party_members": player.party_members,
            },
            "location": None,
            "clock": {
                "day": clock.day if clock else 1,
                "hour": clock.hour if clock else 8,
                "time_of_day": clock.get_time_of_day() if clock else "morning",
            },
            "npcs_present": [],
            "recent_events": [],
            "faction_control": None,
        }

        if location:
            context["location"] = {
                "id": location.id,
                "name": location.name,
                "type": location.type.value,
                "description": location.description,
                "atmosphere_tags": location.atmosphere_tags,
                "current_state": location.current_state,
                "visited_before": location.visited,
            }

        for npc in npcs_here:
            context["npcs_present"].append({
                "id": npc.id,
                "name": npc.name,
                "tier": npc.tier.value,
                "profession": npc.profession,
                "mood": npc.current_mood,
            })

        for event in recent_events:
            context["recent_events"].append({
                "name": event.name,
                "description": event.description,
                "day": event.occurred_day,
            })

        if controlling_faction:
            context["faction_control"] = {
                "id": controlling_faction.id,
                "name": controlling_faction.name,
            }

        return context


def assemble_npc_context(player_id: str, npc_id: str) -> dict[str, Any]:
    """Assemble context for an NPC conversation.

    Args:
        player_id: The player's ID.
        npc_id: The NPC's ID.

    Returns:
        Dictionary with NPC and relationship context.
    """
    with get_session() as session:
        npc = session.get(NPC, npc_id)
        if not npc:
            return {"error": "NPC not found"}

        # Get relationship
        rel = session.query(NPCRelationship).filter(
            NPCRelationship.npc_id == npc_id,
            NPCRelationship.player_id == player_id
        ).first()

        # Get NPC's faction
        faction = None
        if npc.faction_id:
            faction = session.get(Faction, npc.faction_id)

        # Get player's reputation with faction
        player = session.get(Player, player_id)
        faction_reputation = None
        if player and faction:
            rep_score = player.reputation.get(faction.id, 50)
            if rep_score >= 70:
                faction_reputation = "friendly"
            elif rep_score >= 40:
                faction_reputation = "neutral"
            else:
                faction_reputation = "unfriendly"

        context = {
            "npc": {
                "id": npc.id,
                "name": npc.name,
                "tier": npc.tier.value,
                "species": npc.species,
                "profession": npc.profession,
                "description_physical": npc.description_physical,
                "description_personality": npc.description_personality,
                "voice_pattern": npc.voice_pattern,
                "goals": npc.goals,
                "secrets": npc.secrets,
                "current_mood": npc.current_mood,
            },
            "relationship": {
                "summary": rel.summary if rel else "First meeting",
                "trust_level": rel.trust_level if rel else 50,
                "current_disposition": rel.current_disposition if rel else "neutral",
                "key_moments": rel.key_moments if rel else [],
                "recent_messages": rel.recent_messages[-10:] if rel else [],
                "revealed_secrets": rel.revealed_secrets if rel else [],
            },
            "faction": None,
            "faction_reputation": faction_reputation,
        }

        if faction:
            context["faction"] = {
                "id": faction.id,
                "name": faction.name,
                "ideology": faction.ideology,
            }

        return context


def get_location_catchup(player_id: str, location_id: str) -> dict[str, Any]:
    """Get catch-up information for a location the player hasn't visited recently.

    Args:
        player_id: The player's ID.
        location_id: The location's ID.

    Returns:
        Dictionary with changes since last visit.
    """
    with get_session() as session:
        location = session.get(Location, location_id)
        if not location:
            return {"error": "Location not found"}

        clock = session.query(WorldClock).first()
        if not clock:
            return {"changes": [], "days_since_visit": 0}

        last_visit = location.last_visited_day or 0
        days_since = clock.day - last_visit

        if days_since <= 1:
            return {"changes": [], "days_since_visit": days_since}

        # Get events that affected this location
        events = session.query(Event).filter(
            Event.occurred_day > last_visit,
            Event.locations_involved.contains([location_id])
        ).all()

        # Get NPCs who moved here or away
        # This would require tracking NPC movement history - simplified for now

        changes = []
        for event in events:
            changes.append({
                "type": "event",
                "name": event.name,
                "description": event.description,
                "day": event.occurred_day,
            })

        return {
            "location_name": location.name,
            "days_since_visit": days_since,
            "current_state": location.current_state,
            "changes": changes,
        }

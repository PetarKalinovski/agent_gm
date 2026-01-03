"""Tools for reading world state."""

from typing import Any

from strands import tool

from src.models import (
    Connection,
    Event,
    Faction,
    Location,
    NPC,
    NPCRelationship,
    Player,
    WorldClock,
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
        }


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
            Connection.discovered == True  # Only show discovered routes
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
                    "travel_time_hours": 0.1,  # Minimal time to enter a building
                    "requirements": [],
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
                })

        return destinations


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


@tool
def get_world_clock() -> dict[str, Any]:
    """Get the current in-game time.

    Returns:
        Dictionary with day, hour, and time of day.
    """
    with get_session() as session:
        clock = session.query(WorldClock).first()
        if not clock:
            return {"day": 1, "hour": 8, "time_of_day": "morning"}

        return {
            "day": clock.day,
            "hour": clock.hour,
            "time_of_day": clock.get_time_of_day(),
        }


@tool
def get_player(player_id: str) -> dict[str, Any]:
    """Get player character details.

    Args:
        player_id: The player's ID.

    Returns:
        Dictionary with player details.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        return {
            "id": player.id,
            "name": player.name,
            "description": player.description,
            "traits": player.traits,
            "health_status": player.health_status,
            "inventory": player.inventory,
            "active_quests": player.active_quests,
            "party_members": player.party_members,
        }


@tool
def get_recent_events(days_back: int = 7, player_visible_only: bool = True) -> list[dict[str, Any]]:
    """Get recent world events.

    Args:
        days_back: How many days of history to retrieve.
        player_visible_only: Only return events the player would know about.

    Returns:
        List of recent events.
    """
    with get_session() as session:
        clock = session.query(WorldClock).first()
        if not clock:
            return []

        min_day = clock.day - days_back

        query = session.query(Event).filter(
            Event.occurred_day >= min_day
        )

        if player_visible_only:
            query = query.filter(Event.player_visible == True)

        events = query.order_by(Event.occurred_day.desc(), Event.occurred_hour.desc()).all()

        return [
            {
                "id": event.id,
                "name": event.name,
                "description": event.description,
                "event_type": event.event_type,
                "occurred_day": event.occurred_day,
                "occurred_hour": event.occurred_hour,
                "witnessed": event.player_witnessed,
            }
            for event in events
        ]

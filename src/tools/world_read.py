"""Tools for reading world state."""

from typing import Any

from strands import tool

from src.models import (
    Connection,
    Event,
    Faction,
    FactionRelationship,
    HistoricalEvent,
    Location,
    NPC,
    NPCRelationship,
    Player,
    WorldBible,
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
def get_world_state_summary(player_id: str) -> dict[str, Any]:
    """Get a comprehensive summary of the current world state.

    This is the Context Assembler - gathers all relevant world state for the DM.

    Args:
        player_id: The player's ID.

    Returns:
        Dictionary with world state summary including:
        - Current time
        - Player location and status
        - Faction power balance
        - Active conflicts
        - Recent events
        - NPCs at player's location
    """
    with get_session() as session:
        # Time
        clock = session.query(WorldClock).first()
        time_info = {
            "day": clock.day if clock else 1,
            "hour": clock.hour if clock else 8,
            "time_of_day": clock.get_time_of_day() if clock else "morning",
        }

        # Player
        player = session.get(Player, player_id)
        player_info = None
        location_info = None
        npcs_here = []

        if player:
            player_info = {
                "name": player.name,
                "health_status": player.health_status,
                "active_quests": player.active_quests,
            }

            # Location
            if player.current_location_id:
                location = session.get(Location, player.current_location_id)
                if location:
                    location_info = {
                        "id": location.id,
                        "name": location.name,
                        "type": location.type.value,
                        "current_state": location.current_state,
                        "controlling_faction": location.controlling_faction.name if location.controlling_faction else None,
                    }

                    # NPCs at location
                    npcs = session.query(NPC).filter(
                        NPC.current_location_id == location.id,
                        NPC.status == "alive"
                    ).all()
                    npcs_here = [
                        {"id": n.id, "name": n.name, "profession": n.profession, "mood": n.current_mood}
                        for n in npcs
                    ]

        # Factions
        factions = session.query(Faction).all()
        faction_summary = [
            {"id": f.id, "name": f.name, "power_level": f.power_level}
            for f in factions
        ]

        # Active conflicts (war relationships)
        conflicts = session.query(FactionRelationship).filter(
            FactionRelationship.relationship_type == "war"
        ).all()
        active_conflicts = []
        for c in conflicts:
            fa = session.get(Faction, c.faction_a_id)
            fb = session.get(Faction, c.faction_b_id)
            active_conflicts.append({
                "between": [fa.name if fa else "Unknown", fb.name if fb else "Unknown"],
                "stability": c.stability,
            })

        # Recent events (last 3 days)
        recent_events = []
        if clock:
            events = session.query(Event).filter(
                Event.occurred_day >= clock.day - 3,
                Event.player_visible == True
            ).order_by(Event.occurred_day.desc()).limit(5).all()
            recent_events = [
                {"name": e.name, "type": e.event_type, "day": e.occurred_day}
                for e in events
            ]

        # Tensions (rival relationships with low stability)
        tensions = session.query(FactionRelationship).filter(
            FactionRelationship.relationship_type == "rival",
            FactionRelationship.stability < 30
        ).all()
        tension_list = []
        for t in tensions:
            fa = session.get(Faction, t.faction_a_id)
            fb = session.get(Faction, t.faction_b_id)
            tension_list.append({
                "between": [fa.name if fa else "Unknown", fb.name if fb else "Unknown"],
                "stability": t.stability,
                "could_escalate": t.stability < 20,
            })

        return {
            "time": time_info,
            "player": player_info,
            "location": location_info,
            "npcs_at_location": npcs_here,
            "factions": faction_summary,
            "active_conflicts": active_conflicts,
            "rising_tensions": tension_list,
            "recent_events": recent_events,
        }


@tool
def get_world_bible() -> dict[str, Any]:
    """Get the World Bible configuration.

    Returns:
        Dictionary with the world's static configuration.
    """
    with get_session() as session:
        bible = session.query(WorldBible).first()
        if not bible:
            return {"error": "No World Bible found. Create one first."}

        return {
            "id": bible.id,
            "name": bible.name,
            "genre": bible.genre,
            "sub_genres": bible.sub_genres,
            "tone": bible.tone,
            "themes": bible.themes,
            "time_period": bible.time_period,
            "setting_description": bible.setting_description,
            "current_situation": bible.current_situation,
            "technology_level": bible.technology_level,
            "magic_system": bible.magic_system,
            "rules": bible.rules,
            "major_events_history": bible.major_events_history,
            "major_conflicts": bible.major_conflicts,
            "faction_overview": bible.faction_overview,
            "narration_style": bible.narration_style,
            "dialogue_style": bible.dialogue_style,
            "violence_level": bible.violence_level,
            "mature_themes": bible.mature_themes,
            "excluded_elements": bible.excluded_elements,
            "naming_conventions": bible.naming_conventions,
            "visual_style": bible.visual_style,
            "color_palette": bible.color_palette,
            "pc_guidelines": bible.pc_guidelines,
            "pc_starting_situation": bible.pc_starting_situation,
        }


@tool
def get_world_bible_for_generation() -> str:
    """Get the World Bible formatted as a prompt for content generation.

    Returns:
        Formatted string for use in generation prompts.
    """
    with get_session() as session:
        bible = session.query(WorldBible).first()
        if not bible:
            return "No World Bible found."

        return bible.get_generation_prompt()


@tool
def get_world_bible_for_dm() -> str:
    """Get the World Bible formatted as context for the DM agent.

    Returns:
        Formatted string with key world info for DM context.
    """
    with get_session() as session:
        bible = session.query(WorldBible).first()
        if not bible:
            return "No World Bible found."

        return bible.get_dm_context()


@tool
def get_historical_events() -> list[dict[str, Any]]:
    """Get all historical events that shaped the world.

    Returns:
        List of historical events sorted by time (most recent first).
    """
    with get_session() as session:
        events = session.query(HistoricalEvent).all()

        # Sort by time_ago (this is imperfect since it's a string, but works for display)
        return [
            {
                "id": e.id,
                "name": e.name,
                "time_ago": e.time_ago,
                "event_type": e.event_type,
                "description": e.description,
                "involved_parties": e.involved_parties,
                "key_figures": e.key_figures,
                "locations_affected": e.locations_affected,
                "consequences": e.consequences,
                "common_knowledge": e.common_knowledge,
                "artifacts_left": e.artifacts_left,
            }
            for e in events
        ]


@tool
def get_historical_event(event_id: str) -> dict[str, Any]:
    """Get a specific historical event.

    Args:
        event_id: The event's ID.

    Returns:
        Dictionary with the historical event details.
    """
    with get_session() as session:
        event = session.get(HistoricalEvent, event_id)
        if not event:
            return {"error": "Historical event not found"}

        return {
            "id": event.id,
            "name": event.name,
            "time_ago": event.time_ago,
            "event_type": event.event_type,
            "description": event.description,
            "involved_parties": event.involved_parties,
            "key_figures": event.key_figures,
            "locations_affected": event.locations_affected,
            "consequences": event.consequences,
            "common_knowledge": event.common_knowledge,
            "artifacts_left": event.artifacts_left,
        }


@tool
def get_all_locations(location_type: str | None = None, parent_id: str | None = None) -> list[dict[str, Any]]:
    """Get all locations, optionally filtered.

    Args:
        location_type: Optional location type to filter by.
        parent_id: Optional parent ID to get children of.

    Returns:
        List of locations.
    """
    with get_session() as session:
        query = session.query(Location)

        if location_type:
            query = query.filter(Location.type == location_type)
        if parent_id:
            query = query.filter(Location.parent_id == parent_id)

        locations = query.all()

        return [
            {
                "id": loc.id,
                "name": loc.name,
                "type": loc.type.value,
                "parent_id": loc.parent_id,
                "depth": loc.depth,
                "current_state": loc.current_state,
                "controlling_faction_id": loc.controlling_faction_id,
                "discovered": loc.discovered,
                "visited": loc.visited,
            }
            for loc in locations
        ]


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
            from src.models import NPCTier
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

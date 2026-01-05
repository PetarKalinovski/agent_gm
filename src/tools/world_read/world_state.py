"""Tools for world state summary operations."""

from typing import Any

from strands import tool

from src.models import (
    Event,
    Faction,
    FactionRelationship,
    Location,
    NPC,
    Player,
    WorldClock,
    get_session,
)


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

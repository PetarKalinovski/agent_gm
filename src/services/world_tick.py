"""World Tick - Pre-turn world simulation.

Runs before each DM response to check what happened in the world since
the last player action. Surfaces scheduled events, faction movements,
and NPC goal progress so the DM can weave them into the narrative.
"""

from typing import Any

from src.models import (
    DMState,
    Event,
    Faction,
    NPC,
    NPCTier,
    WorldClock,
    get_session,
)


def run_world_tick() -> dict[str, Any]:
    """Run the world tick and return everything the DM needs to know.

    Checks:
    1. Scheduled events whose time has arrived
    2. Faction goals that should produce visible effects
    3. Major NPC goals that should advance
    4. The DM's own narrative state

    Returns a context dict the DM can use to inform its response.
    """
    with get_session() as session:
        clock = session.query(WorldClock).first()
        if not clock:
            return {"tick": "no_clock"}

        dm_state = session.query(DMState).first()
        if not dm_state:
            dm_state = DMState()
            session.add(dm_state)
            session.commit()

        current_day = clock.day
        current_hour = clock.hour

        # 1. Fire scheduled events whose time has come
        fired_events = _check_scheduled_events(session, current_day, current_hour)

        # 2. Check faction pressures (goals that should be visible)
        faction_pressures = _check_faction_goals(session)

        # 3. Check major NPC goals
        npc_agendas = _check_npc_goals(session)

        # 4. Load the DM's narrative state
        narrative_state = {
            "current_arc": dm_state.current_arc,
            "planned_beats": dm_state.planned_beats or [],
            "completed_beats": dm_state.completed_beats or [],
            "tension": dm_state.tension,
            "active_threats": dm_state.active_threats or [],
            "world_pressures": dm_state.world_pressures or [],
        }

        # 5. Update last tick time
        dm_state.last_tick_day = current_day
        dm_state.last_tick_hour = current_hour
        session.commit()

        return {
            "game_time": f"Day {current_day}, {current_hour:02d}:00",
            "fired_events": fired_events,
            "faction_pressures": faction_pressures,
            "npc_agendas": npc_agendas,
            "narrative_state": narrative_state,
        }


def _check_scheduled_events(session, current_day: int, current_hour: int) -> list[dict]:
    """Find and fire scheduled events whose time has passed."""
    pending = session.query(Event).filter(
        Event.scheduled_day.isnot(None),
        Event.occurred_day.is_(None),  # Not yet fired
    ).all()

    fired = []
    for event in pending:
        event_time = (event.scheduled_day, event.scheduled_hour or 0)
        current_time = (current_day, current_hour)

        if event_time <= current_time:
            # Fire this event — mark it as occurred
            event.occurred_day = current_day
            event.occurred_hour = current_hour

            fired.append({
                "id": event.id,
                "name": event.name,
                "description": event.description,
                "event_type": event.event_type,
                "consequences": event.consequences or [],
                "factions_involved": event.factions_involved or [],
                "locations_involved": event.locations_involved or [],
                "npcs_involved": event.npcs_involved or [],
                "player_visible": event.player_visible,
            })

    if fired:
        session.commit()

    return fired


def _check_faction_goals(session) -> list[dict]:
    """Summarize active faction goals that could drive world events."""
    factions = session.query(Faction).all()

    pressures = []
    for faction in factions:
        short_term = faction.goals_short if hasattr(faction, 'goals_short') and faction.goals_short else []
        long_term = faction.goals_long if hasattr(faction, 'goals_long') and faction.goals_long else []

        if short_term or long_term:
            pressures.append({
                "faction": faction.name,
                "faction_id": faction.id,
                "short_term_goals": short_term[:2],  # Top 2 only
                "long_term_goals": long_term[:1],     # Top 1 only
                "power_level": getattr(faction, 'power_level', None),
            })

    return pressures


def _check_npc_goals(session) -> list[dict]:
    """Summarize major NPC goals that could drive narrative."""
    major_npcs = session.query(NPC).filter(
        NPC.tier == NPCTier.MAJOR,
        NPC.status == "alive",
    ).all()

    agendas = []
    for npc in major_npcs:
        goals = npc.goals if hasattr(npc, 'goals') and npc.goals else []
        if goals:
            agendas.append({
                "npc": npc.name,
                "npc_id": npc.id,
                "location": npc.current_location_id,
                "goals": goals[:2],  # Top 2 goals
                "mood": npc.current_mood,
            })

    return agendas


def format_world_tick_context(tick_result: dict[str, Any]) -> str:
    """Format the world tick result into a readable context block for the DM prompt.

    Args:
        tick_result: Output of run_world_tick().

    Returns:
        Formatted string to inject into the DM's context.
    """
    parts = []

    # Narrative state (always show)
    ns = tick_result.get("narrative_state", {})
    if ns.get("current_arc"):
        parts.append(f"**YOUR NARRATIVE ARC**: {ns['current_arc']}")
        if ns.get("planned_beats"):
            beats = ", ".join(ns["planned_beats"][:5])
            parts.append(f"  Next beats to deliver: {beats}")
        parts.append(f"  Tension: {ns.get('tension', 'low')}")

    if ns.get("active_threats"):
        threats = ", ".join(ns["active_threats"])
        parts.append(f"  Active threats: {threats}")

    if ns.get("world_pressures"):
        pressures = ", ".join(ns["world_pressures"])
        parts.append(f"  World pressures: {pressures}")

    # Fired events (important — these just happened)
    fired = tick_result.get("fired_events", [])
    if fired:
        parts.append("\n**EVENTS THAT JUST FIRED** (weave these into your narration):")
        for e in fired:
            vis = " [player can learn about this]" if e["player_visible"] else " [hidden from player]"
            parts.append(f"  - {e['name']}: {e['description']}{vis}")
            if e["consequences"]:
                parts.append(f"    Consequences: {', '.join(e['consequences'])}")

    # Faction pressures (background)
    factions = tick_result.get("faction_pressures", [])
    if factions:
        parts.append("\n**FACTION AGENDAS** (use to drive background tension):")
        for f in factions:
            goals = "; ".join(f["short_term_goals"])
            parts.append(f"  - {f['faction']}: {goals}")

    # NPC agendas (major NPCs with their own plans)
    npcs = tick_result.get("npc_agendas", [])
    if npcs:
        parts.append("\n**MAJOR NPC AGENDAS** (NPCs pursuing their own goals):")
        for n in npcs:
            goals = "; ".join(n["goals"])
            parts.append(f"  - {n['npc']} ({n['mood']}): {goals}")

    if not parts:
        return ""

    return "\n### WORLD STATE (pre-turn briefing)\n" + "\n".join(parts)

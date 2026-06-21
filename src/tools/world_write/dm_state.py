"""Tools for writing DM narrative director state."""

from typing import Any

from strands import tool

from src.models import DMState, get_session


@tool
def update_dm_state(
    current_arc: str | None = None,
    planned_beats: list[str] | None = None,
    completed_beats: list[str] | None = None,
    tension: str | None = None,
    active_threats: list[str] | None = None,
    world_pressures: list[str] | None = None,
) -> dict[str, Any]:
    """Update the DM's narrative director state.

    Use this to evolve your narrative plan as the story progresses.
    Call this when:
    - Starting a new narrative arc
    - A planned beat has been delivered (move it to completed_beats)
    - Tension should shift (e.g. after a climactic moment)
    - New threats emerge or old ones resolve
    - World pressures change (new deadlines, resolved crises)

    Only provide fields you want to change — others remain untouched.

    Args:
        current_arc: The story you're currently driving toward.
        planned_beats: Ordered list of narrative moments you want to unfold.
        completed_beats: Beats that have already been delivered.
        tension: Pacing level: "low", "rising", "high", "climax", "falling".
        active_threats: Complications and dangers currently in play.
        world_pressures: Background forces creating urgency.

    Returns:
        The updated DM state.
    """
    with get_session() as session:
        state = session.query(DMState).first()
        if not state:
            state = DMState()
            session.add(state)

        if current_arc is not None:
            state.current_arc = current_arc
        if planned_beats is not None:
            state.planned_beats = planned_beats
        if completed_beats is not None:
            state.completed_beats = completed_beats
        if tension is not None:
            state.tension = tension
        if active_threats is not None:
            state.active_threats = active_threats
        if world_pressures is not None:
            state.world_pressures = world_pressures

        session.commit()

        return {
            "current_arc": state.current_arc,
            "planned_beats": state.planned_beats or [],
            "completed_beats": state.completed_beats or [],
            "tension": state.tension,
            "active_threats": state.active_threats or [],
            "world_pressures": state.world_pressures or [],
        }


@tool
def schedule_world_event(
    name: str,
    description: str,
    event_type: str,
    scheduled_day: int,
    scheduled_hour: int,
    factions_involved: list[str] | None = None,
    locations_involved: list[str] | None = None,
    npcs_involved: list[str] | None = None,
    consequences: list[str] | None = None,
    player_visible: bool = True,
) -> dict[str, Any]:
    """Schedule a future world event to fire at a specific game time.

    Use this to plant time-bombs in the narrative: faction raids, NPC arrivals,
    weather changes, deadlines expiring, etc. These events will be surfaced to you
    when their scheduled time arrives.

    Args:
        name: Event name.
        description: What happens when this event fires.
        event_type: "macro" (faction-level), "meso" (NPC/location), "player".
        scheduled_day: Game day when this should fire.
        scheduled_hour: Game hour (0-23) when this should fire.
        factions_involved: Faction IDs involved.
        locations_involved: Location IDs involved.
        npcs_involved: NPC IDs involved.
        consequences: What should change when this fires.
        player_visible: Whether the player can learn about this.

    Returns:
        The created scheduled event.
    """
    from src.models import Event

    with get_session() as session:
        event = Event(
            name=name,
            description=description,
            event_type=event_type,
            scheduled_day=scheduled_day,
            scheduled_hour=scheduled_hour,
            factions_involved=factions_involved or [],
            locations_involved=locations_involved or [],
            npcs_involved=npcs_involved or [],
            consequences=consequences or [],
            player_visible=player_visible,
            player_witnessed=False,
        )
        session.add(event)
        session.commit()

        return {
            "id": event.id,
            "name": event.name,
            "scheduled_day": event.scheduled_day,
            "scheduled_hour": event.scheduled_hour,
            "description": event.description,
        }

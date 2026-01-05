"""Tools for event write operations."""

from typing import Any

from strands import tool

from src.models import (
    Event,
    WorldClock,
    get_session,
)


@tool
def create_event(
    name: str,
    description: str,
    event_type: str,
    factions_involved: list[str] | None = None,
    locations_involved: list[str] | None = None,
    npcs_involved: list[str] | None = None,
    consequences: list[str] | None = None,
    player_visible: bool = True,
    player_witnessed: bool = False,
) -> dict[str, Any]:
    """Create a new world event.

    Args:
        name: Event name.
        description: Event description.
        event_type: Type of event (macro, meso, player).
        factions_involved: List of faction IDs involved.
        locations_involved: List of location IDs involved.
        npcs_involved: List of NPC IDs involved.
        consequences: List of consequence descriptions.
        player_visible: Whether the player can learn about this.
        player_witnessed: Whether the player saw it happen.

    Returns:
        Dictionary with the created event.
    """
    with get_session() as session:
        clock = session.query(WorldClock).first()

        event = Event(
            name=name,
            description=description,
            event_type=event_type,
            occurred_day=clock.day if clock else 1,
            occurred_hour=clock.hour if clock else 8,
            factions_involved=factions_involved or [],
            locations_involved=locations_involved or [],
            npcs_involved=npcs_involved or [],
            consequences=consequences or [],
            player_visible=player_visible,
            player_witnessed=player_witnessed,
        )
        session.add(event)
        session.commit()

        return {
            "id": event.id,
            "name": event.name,
            "description": event.description,
        }

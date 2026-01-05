"""Tools for event read operations."""

from typing import Any

from strands import tool

from src.models import (
    Event,
    WorldClock,
    get_session,
)


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

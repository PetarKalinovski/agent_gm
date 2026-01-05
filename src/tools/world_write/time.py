"""Tools for time management operations."""

from typing import Any

from strands import tool

from src.models import (
    WorldClock,
    get_session,
)


@tool
def advance_time(hours: float, reason: str = "") -> dict[str, Any]:
    """Advance the world clock.

    Args:
        hours: Number of hours to advance.
        reason: Reason for time advancement (for logging).

    Returns:
        Dictionary with new time.
    """
    with get_session() as session:
        clock = session.query(WorldClock).first()
        if not clock:
            return {"error": "World clock not initialized"}

        clock.advance(hours)
        session.commit()

        return {
            "day": clock.day,
            "hour": clock.hour,
            "time_of_day": clock.get_time_of_day(),
            "advanced_by": hours,
        }

"""Tools for reading DM narrative director state."""

from typing import Any

from strands import tool

from src.models import DMState, get_session


@tool
def get_dm_state() -> dict[str, Any]:
    """Get the DM's current narrative director state.

    Returns the DM's narrative plan: current arc, planned beats, tension level,
    active threats, and world pressures. Use this at the start of every turn
    to stay aligned with your narrative intentions.

    Returns:
        Dictionary with the DM's narrative state.
    """
    with get_session() as session:
        state = session.query(DMState).first()
        if not state:
            return {
                "current_arc": None,
                "planned_beats": [],
                "completed_beats": [],
                "tension": "low",
                "active_threats": [],
                "world_pressures": [],
            }

        return {
            "current_arc": state.current_arc,
            "planned_beats": state.planned_beats or [],
            "completed_beats": state.completed_beats or [],
            "tension": state.tension,
            "active_threats": state.active_threats or [],
            "world_pressures": state.world_pressures or [],
        }

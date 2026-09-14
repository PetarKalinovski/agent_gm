"""The DM's door into the same rest service the canvas and the API use."""

from typing import Any

from strands import tool

from src.services.rest import rest_now


@tool
def rest(player_id: str) -> dict[str, Any]:
    """The player turns in for the night where they are.

    The engine decides whether this place is safe to sleep in, charges the
    bed, heals what sleep heals, and wakes them at first light — the clock
    and their health are ALREADY moved when this returns. Narrate the
    MORNING; never advance the clock or heal them for the night yourself.

    Args:
        player_id: The player's ID.

    Returns:
        What the night did, or a refusal: "no_rest_here" means there is
        nowhere safe to sleep here (say so in the fiction), "cannot_afford"
        means the bed costs more than they carry.
    """
    result = rest_now(player_id)
    if "error" in result:
        reason = result["error"]
        if reason == "no_rest_here":
            result["instruction"] = (
                "There is nowhere safe to sleep here. Say so in the fiction; an inn or a "
                "camp is where the night can be spent."
            )
        elif reason == "cannot_afford":
            result["instruction"] = (
                f"The bed costs {result.get('price')} and they carry {result.get('currency')}. "
                "Let the keeper refuse them in their own voice, or let them earn it."
            )
        return result
    result["message"] = (
        f"They slept at {result['location_name']} and it is now the morning of Day "
        f"{result['day']} (already applied — do NOT call advance_time or "
        "update_player_health for this). Open on the morning."
    )
    return result

"""Combat write tools — the DM starts fights; code resolves them."""

from typing import Any

from strands import tool

from src.services.combat import build_combat_start


@tool
def start_combat(player_id: str, npc_ids: list[str]) -> dict[str, Any]:
    """Start real-time combat between the player and NPCs at their location.

    Use this when the fiction demands violence with named NPCs who are
    PRESENT — an ambush springs, negotiations collapse, a duel is accepted.
    The player then fights in real time on the canvas (dodging, striking);
    you do NOT resolve the fight. You'll receive a [COMBAT OUTCOME] message
    when it ends, with health and deaths already applied by the engine —
    then you narrate the aftermath.

    Args:
        player_id: The player's ID.
        npc_ids: The NPC(s) attacking or being attacked. They must be alive
            and at the player's current location. Never a party member —
            the player's companions are added to the fight automatically,
            on the player's side.

    Returns:
        Dictionary with the combat roster, or an error explaining what to fix.
    """
    return build_combat_start(npc_ids, player_id)

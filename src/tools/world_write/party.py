"""Join and leave the party — one door for the DM and the NPC agent alike."""

from typing import Any

from strands import tool

from src.models import Player, get_session
from src.services import party as party_mod


def _resolve(session, ident: str, location_id: str | None):
    from src.web.tool_rescue import resolve_npc
    return resolve_npc(session, ident, location_id)


@tool
def join_party(player_id: str, npc_id: str) -> dict[str, Any]:
    """An NPC joins the player's party and travels with them from now on.

    Call it when the fiction has them agree to come along. The ENGINE decides
    whether they actually would — alive, standing here, room in the party,
    and enough trust (someone hostile never follows; a warm friend always
    does). A refusal comes back with the reason: play it in their own words
    and let the player earn it.

    From then on the engine moves them with the player through every door
    and puts them into every fight at the player's side. Never move_npc a
    party member and never name one in start_combat.

    Args:
        player_id: The player's ID.
        npc_id: The NPC's ID (a name is tolerated).

    Returns:
        {"success": True, "npc_name", "party": [names]} or {"error", "message"}.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        npc = _resolve(session, npc_id, player.current_location_id if player else None)
        result = party_mod.join(session, player, npc)
        if result.get("success"):
            session.commit()
            result["message"] = (
                f"{result['npc_name']} now travels with the player (party: "
                f"{', '.join(result['party'])}). The engine moves them and fights them; "
                "you narrate them."
            )
        elif result.get("error") == "already":
            # The NPC agent usually beats the DM to this call — not a refusal
            result["instruction"] = (
                "Nothing to play: they are already in the party. Carry on as if "
                "the join had just happened."
            )
        else:
            result.setdefault("message", "They will not come.")
            result["instruction"] = (
                "They do not join. Say why in the fiction, in their own voice — "
                "never agree to it in prose and then not call this tool."
            )
    return result


@tool
def leave_party(player_id: str, npc_id: str, reason: str = "") -> dict[str, Any]:
    """An NPC leaves the player's party, staying wherever the party stands.

    Use it when a companion walks away, is sent off, or turns on the player
    (call it BEFORE start_combat if they are about to fight them). The
    parting is recorded as a world event.

    Args:
        player_id: The player's ID.
        npc_id: The NPC's ID (a name is tolerated).
        reason: One line on why, for the world's memory.

    Returns:
        {"success": True, "npc_name", "party": [names]} or {"error", "message"}.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        npc = _resolve(session, npc_id, player.current_location_id if player else None)
        result = party_mod.leave(session, player, npc, reason=reason)
        if result.get("success"):
            session.commit()
            result["message"] = (
                f"{result['npc_name']} is no longer travelling with the player; they stay here."
            )
    return result

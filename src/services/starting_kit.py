"""What every new character carries on their first step into the world.

Playtest 2026-10-04: a fresh character had 25 coin, an empty pack, and bare
hands; the first fight was lost before a key was pressed, and no merchant in
the starting town sold a blade. Real-time combat needs something in the hand
and something to drink, so every character starts with the cheapest of both
— a crude knife (equipped) and one healing draught (bound to Q). Anything
better is bought, found, or won.
"""

from __future__ import annotations

import logging

from src.models import Player, get_session

logger = logging.getLogger(__name__)

KIT_WEAPON = "Worn Knife"
KIT_DRAUGHT = "Willow-Bark Draught"


def grant_starting_kit(player_id: str) -> list[str]:
    """Give the kit unless they already carry a weapon or a draught.

    Idempotent (a second call finds the knife and does nothing). Returns the
    names of what was given.
    """
    from src.tools.world_write.player import add_to_inventory

    with get_session() as session:
        player = session.get(Player, player_id)
        if player is None:
            return []
        inventory = [i for i in (player.inventory or []) if isinstance(i, dict)]
        has_weapon = any(i.get("slot") == "weapon" for i in inventory)
        has_draught = any(i.get("effect_tag") for i in inventory)

    given: list[str] = []
    try:
        if not has_weapon:
            add_to_inventory(
                player_id=player_id, item_name=KIT_WEAPON, item_type="weapon",
                description="A plain knife with a cracked horn grip. It has seen better owners.",
                slot="weapon", tier="crude", visual_desc="a plain knife with a cracked horn grip",
            )
            given.append(KIT_WEAPON)
        if not has_draught:
            add_to_inventory(
                player_id=player_id, item_name=KIT_DRAUGHT, item_type="consumable",
                description="Bitter, cloudy, and it works.", effect_tag="heal_light",
            )
            given.append(KIT_DRAUGHT)
            with get_session() as session:
                player = session.get(Player, player_id)
                if player is not None and not player.quick_slot_item_id:
                    # the id add_to_inventory gave it
                    player.quick_slot_item_id = KIT_DRAUGHT.lower().replace(" ", "_")
                    session.commit()
    except Exception as e:  # a missing kit must never block a character
        logger.warning(f"starting kit for {player_id} failed: {e}")
    if given:
        logger.info(f"Starting kit for {player_id}: {', '.join(given)}")
    return given

"""Reading the campaign. There is deliberately no write tool.

The story is authored once and then belongs to the engine — the DM can read
its premise, its act, its clocks and its fixed truths, and can change none of
them. That asymmetry IS the enforcement: a DM that cannot rewrite a truth
cannot quietly retcon the ending it has forgotten.
"""

from typing import Any

from strands import tool

from src.services.campaign import campaign_dm_view


@tool
def get_campaign(player_id: str) -> dict[str, Any]:
    """Read the authored campaign for this character: premise, current act,
    villain, live deadline clocks, delivered beats, and the fixed truths you
    are allowed to know.

    SEALED truths are included with their statements so you know what NOT to
    say — never reveal a truth marked sealed. There is no tool to change a
    truth: the story is set in stone. Read it, deliver it, do not rewrite it.

    Args:
        player_id: The player's ID.

    Returns:
        Dictionary with the campaign, the current act, the truths, the live
        clocks and the beats already delivered. "campaign" is null when this
        character has no campaign — play the world as it comes.
    """
    return campaign_dm_view(player_id)

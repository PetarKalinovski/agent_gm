"""The DM's one cinematic tool. The engine owns rarity.

`play_cinematic` is a request, not a command: `src/services/cinematics.py`
enforces the cooldowns, the per-day caps and the once-per-boss identity rule
against a DB ledger. A refusal is the normal case, not an error — it comes
back with NO "event" key (so nothing can reach the client) and an instruction
to say nothing about it, because the observed failure mode is a DM that types
the title card in prose after being told it would not play.
"""

from typing import Any

from strands import tool

from src.services.cinematics import request_cinematic


@tool
def play_cinematic(
    player_id: str,
    kind: str,
    title: str | None = None,
    subtitle: str | None = None,
    npc_id: str | None = None,
    mood: str | None = None,
) -> dict[str, Any]:
    """Play a cinematic moment on the player's screen. Use sparingly.

    kind:
      "title_card" - a chapter break. title = 2-5 words, subtitle = one line.
      "boss_intro" - the arrival of a named dangerous enemy. Pass npc_id.
                     Call it in the same turn as start_combat, BEFORE it.
      "vision"     - dream/memory/prophecy treatment; call it, then narrate.
      "weather"    - set world weather. mood = rain|snow|fog|embers|ash|clear.

    The engine owns all presentation and enforces cooldowns: if this returns
    rejected, that is normal — say nothing about it and narrate the moment in
    prose instead.

    Args:
        player_id: The player's ID.
        kind: One of "title_card", "boss_intro", "vision", "weather".
        title: The card's headline (required for a title card; the character's
            name for a boss intro).
        subtitle: One short line under the title.
        npc_id: The NPC this is about — required for "boss_intro".
        mood: For "weather", the label to set: rain, snow, fog, embers, ash or
            clear.

    Returns:
        Dictionary describing the cinematic that played, or — when the engine
        refused it — what to do instead.
    """
    result = request_cinematic(
        kind,
        player_id=player_id,
        title=title,
        subtitle=subtitle,
        npc_id=npc_id,
        mood=mood,
        source="dm",
    )

    if not result.get("ok"):
        # No "event" key: a rejection must not reach the client at all.
        return {
            "success": False,
            "rejected": True,
            "reason": result.get("reason"),
            "instruction": result.get("instruction"),
        }

    payload = result["payload"]
    return {"success": True, "event": "cinematic", **payload}

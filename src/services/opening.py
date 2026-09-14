"""The opening turn — a new character's first moment is a story, not a prompt.

Before this, the web client printed "Your story begins at X. What do you do?"
and waited. No scene, no people, no hook; the campaign spine that was
authored at character creation sat unused until the player happened to type.

Now the first entry into the world runs an opening sequence, all in code:

  1. a title card (the campaign's title over its first act, or the world's
     name over its tagline) — the one documented bypass of the cinematic
     rarity ledger, still written to it so later cards throttle against it;
  2. the scene, straight from the Location row (exactly what
     `describe_location` would have printed, with zero model latency);
  3. an auto-sent `[OPENING]` directive the DM answers like any turn — open
     on a person, a pressure, or a problem, and END by handing the player
     their first lead.

`is_first_turn` is the idempotence key: once the DM has a message, a refresh
gets `{"opening": False}` and the client shows history instead.
"""

from __future__ import annotations

import logging
from typing import Any

from src.models import DMState, Location, NPC, Player, WorldBible, WorldClock, get_session
from src.services.campaign import _act_entry, get_campaign_row
from src.services.cinematics import request_cinematic
from src.services.lead import compute_lead

logger = logging.getLogger(__name__)

OPENING_TAG = "[OPENING]"

_HAND_OVER = (
    "END this response by handing them one concrete next thing to do. If no quest is "
    "active, give them one NOW: call get_available_quests_for_npc for someone present and "
    "activate_quest what you find, or create_quest(..., start_active=True) with one "
    'objective of kind "talk_to" or "go_to" carrying the real target id. Every spoken '
    "line goes through speak or prompt_npc_agent."
)


CAMPAIGN_WAIT_SECONDS = 45.0
CAMPAIGN_POLL_SECONDS = 2.0


def wait_for_campaign(player_id: str, timeout: float = CAMPAIGN_WAIT_SECONDS,
                      poll: float = CAMPAIGN_POLL_SECONDS, sleep=None) -> str | None:
    """Give a campaign that is still being authored a moment to finish.

    The client is buffering sprites for minutes at this point, so a short wait
    costs nothing and turns "Emberfall" into the campaign's own title card.
    Returns the campaign status seen last (None when there is no row).
    """
    import time as _time
    sleep = sleep or _time.sleep
    deadline = _time.monotonic() + max(0.0, timeout)
    status = None
    while True:
        with get_session() as session:
            row = get_campaign_row(session, player_id)
            status = row.status if row is not None else None
        if status != "pending" or _time.monotonic() >= deadline:
            return status
        sleep(poll)


def is_first_turn(dm: Any) -> bool:
    """True when the DM has no conversation with this player yet."""
    try:
        messages = getattr(dm.agent, "messages", None)
    except Exception as e:  # a DM that cannot load its history gets no opening
        logger.debug(f"first-turn check failed: {e}")
        return False
    return not messages


def _card_text(campaign, bible) -> tuple[str | None, str | None]:
    if campaign is not None and (campaign.title or "").strip():
        act = _act_entry(campaign, 1) if campaign.acts else {}
        return campaign.title.strip(), (act.get("title") or "").strip() or None
    if bible is not None and (bible.name or "").strip():
        return bible.name.strip(), (bible.tagline or "").strip() or None
    return None, None


def build_opening_directive(session, player, campaign, location, bible, dm_state,
                            npc_names: list[str], time_of_day: str) -> str:
    """The `[OPENING]` message the DM answers. Reads the spine's premise and
    the player's lead; never a beat directive (those stay sealed)."""
    where = location.name if location is not None else "where they stand"
    lines = [
        f"{OPENING_TAG} This is {player.name}'s first moment in the world. The player has "
        f"ALREADY been shown {where} ({time_of_day}) — a title card and the scene description are "
        "on their screen. Do not call describe_location and do not re-describe the place.",
    ]
    if npc_names:
        lines.append(f"Present: {', '.join(npc_names[:6])}.")

    if campaign is not None:
        act = _act_entry(campaign, 1) if campaign.acts else {}
        story = [f'The story you are delivering: "{campaign.title}".']
        if campaign.premise:
            story.append(campaign.premise.strip())
        if campaign.stakes_personal:
            story.append(f"Why it is {player.name}'s: {campaign.stakes_personal.strip()}")
        if act.get("title") or act.get("premise"):
            story.append(f"Act I — {act.get('title') or ''}: {act.get('premise') or ''}".strip())
        lines.append(" ".join(s for s in story if s))
    else:
        world = []
        if bible is not None and bible.current_situation:
            world.append(f"The world right now: {bible.current_situation.strip()}")
        if bible is not None and bible.pc_starting_situation:
            world.append(f"Their starting situation: {bible.pc_starting_situation.strip()}")
        if dm_state is not None and dm_state.current_arc:
            world.append(f"Your arc: {dm_state.current_arc.strip()}")
        if world:
            lines.append(" ".join(world))

    lead = compute_lead(session, player)
    if lead and lead.get("text"):
        lines.append(
            f'Their lead, already on their screen: "{lead["text"]}". Make the opening the '
            "moment that line starts to matter."
        )

    lines.append(
        "Open on a person, a pressure, or a problem — someone present wants something, or "
        "something here is wrong. A few paragraphs, second person, present tense. "
        + _HAND_OVER
    )
    return "\n".join(lines)


def opening_payload(player_id: str) -> dict[str, Any] | None:
    """Everything the client plays before the DM's first words."""
    if not player_id:
        return None
    with get_session() as session:
        player = session.get(Player, player_id)
        if player is None:
            return None
        location = session.get(Location, player.current_location_id) if player.current_location_id else None
        clock = session.query(WorldClock).first()
        time_of_day = clock.get_time_of_day() if clock else "day"
        bible = session.query(WorldBible).first()
        dm_state = session.query(DMState).first()
        campaign = get_campaign_row(session, player_id)
        if campaign is not None and campaign.status != "active":
            campaign = None
        npc_names: list[str] = []
        scene = None
        if location is not None:
            npc_names = [
                n.name for n in session.query(NPC).filter(
                    NPC.current_location_id == location.id, NPC.status == "alive"
                ).all() if n.name
            ]
            scene = {
                "location": location.name,
                "description": location.description or "",
                "time_of_day": time_of_day,
                "atmosphere": [],
                "npcs": npc_names,
            }
        title, subtitle = _card_text(campaign, bible)
        directive = build_opening_directive(
            session, player, campaign, location, bible, dm_state, npc_names, time_of_day
        )

    cinematic = None
    if title:
        try:
            result = request_cinematic(
                "title_card", player_id=player_id, title=title, subtitle=subtitle,
                source="opening", dm_callable_only=False,
            )
            if result.get("ok"):
                cinematic = result["payload"]
            else:
                logger.info(f"opening title card refused: {result.get('reason')}")
        except Exception as e:
            logger.warning(f"opening title card failed: {e}")

    return {"opening": True, "cinematic": cinematic, "scene": scene, "dm_message": directive}

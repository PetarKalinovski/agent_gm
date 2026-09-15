"""Offscreen world simulation — the world moves even when the player isn't looking.

Once per in-game day, a fast-model agent advances the world: a faction makes
progress on a goal, a major NPC acts on their agenda, a consequence of recent
events lands. Changes are made through the same world-write tools the DM uses,
so they surface naturally — as scheduled events firing, as things NPCs know,
as changed world state the player discovers.

Runs in the background after a player turn (never blocks the turn itself).
"""

from typing import Any

from loguru import logger

from src.models import DMState, WorldClock, get_session


WORLD_SIM_PROMPT = """You are the offscreen world simulator for a persistent RPG world.
One in-game day has passed. Advance the world by making 1-3 SMALL, CONCRETE changes.

Rules:
- Read the world first: call get_world_state_summary and get_recent_events.
- Favor consequences: if recent events (including player actions and deaths)
  should ripple, ripple them. A faction that lost someone reacts. A crime gets
  investigated. A rumor spreads.
- Advance ONE faction's short-term goal a single step (use update_faction to
  record progress in its description/goals, or create_event for something that
  happened).
- Optionally move ONE major NPC to a location that fits their goals (move_npc).
  NEVER move an NPC into the player's current location — walking the story's
  antagonist up to the player is the DM's call, not yours. NEVER move an NPC
  who is named in the DM's planned beats or active threats (check the world
  state summary): they are staged for a scene that hasn't happened yet.
- Schedule at most ONE future event with schedule_world_event (e.g. "caravan
  arrives in 2 days") so the world has momentum.
- Record what happened with create_event (event_type "macro" for faction-level,
  "meso" for local). Set player_visible=True only for things the player could
  plausibly hear about.
- Do NOT touch the player, their inventory, or their location. Do NOT kill
  named NPCs. Do NOT create new locations or NPCs. Small moves, big world.

Finish with a 1-2 sentence summary of what changed (this is only logged, the
player never sees it directly)."""


def _walk_npc_ids(value: Any, found: set[str]) -> None:
    """Collect every "npc_id" (and list under "npc_ids") nested in JSON."""
    if isinstance(value, dict):
        npc_id = value.get("npc_id")
        if isinstance(npc_id, str) and npc_id:
            found.add(npc_id)
        for item in value.get("npc_ids") or []:
            if isinstance(item, str) and item:
                found.add(item)
        for child in value.values():
            _walk_npc_ids(child, found)
    elif isinstance(value, list):
        for child in value:
            _walk_npc_ids(child, found)


def staged_npc_ids(session) -> set[str]:
    """NPCs the campaign has cast or written into a beat that has not been
    delivered yet. The simulator must not move them: they are staged for a
    scene the engine will fire on its own clock."""
    from src.models.campaign import Campaign, StoryBeat

    staged: set[str] = set()
    campaigns = session.query(Campaign).filter(
        Campaign.status.in_(("pending", "active"))
    ).all()
    for campaign in campaigns:
        for npc_id in (campaign.villain_npc_id, campaign.successor_npc_id):
            if npc_id:
                staged.add(npc_id)
        for npc_id in campaign.ally_npc_ids or []:
            if isinstance(npc_id, str) and npc_id:
                staged.add(npc_id)
        beats = session.query(StoryBeat).filter(
            StoryBeat.campaign_id == campaign.id,
            StoryBeat.status.in_(("pending", "armed", "fired")),
        ).all()
        for beat in beats:
            for blob in (beat.trigger_data, beat.met_condition,
                         beat.consequence_met, beat.consequence_missed):
                _walk_npc_ids(blob, staged)
    return staged


def sim_move_npc(npc_id: str, destination_id: str) -> dict[str, Any]:
    """Move an NPC to a new location, within the simulator's staging rules.

    Refused in code (the prompt asks, the code decides): a move INTO a
    location where a player is standing — walking the antagonist up to the
    player is the DM's scene, not the simulator's — and any move of an NPC
    the campaign has staged for a beat that has not fired yet. Playtesting
    found the bandit meant for Wolf's Hollow drinking in the player's
    tavern on day one.

    Args:
        npc_id: The NPC's ID.
        destination_id: The destination location ID.

    Returns:
        Dictionary with result, or an "error" explaining the refusal.
    """
    from src.models import Player
    from src.tools.world_write import move_npc

    with get_session() as session:
        player_locations = {
            p.current_location_id for p in session.query(Player).all()
            if p.current_location_id
        }
        if destination_id in player_locations:
            return {
                "error": "refused: a player is standing there. The simulator never "
                         "walks an NPC up to the player — leave that meeting to the DM.",
            }
        if npc_id in staged_npc_ids(session):
            return {
                "error": "refused: this NPC is staged by the campaign for a scene that "
                         "has not happened yet. Pick a different NPC or skip the move.",
            }
        from src.services import party as party_mod
        if party_mod.is_member(session, npc_id):
            return {
                "error": "refused: this NPC travels in a player's party. They go where "
                         "the player goes — pick a different NPC.",
            }
    return move_npc(npc_id, destination_id)


def _get_sim_tools() -> list:
    from strands import tool

    from src.tools.world_read import get_world_state_summary, get_recent_events, get_all_npcs
    from src.tools.world_write import (
        create_event,
        schedule_world_event,
        update_faction,
        update_dm_state,
    )

    return [
        get_world_state_summary,
        get_recent_events,
        get_all_npcs,
        create_event,
        tool(name="move_npc")(sim_move_npc),
        schedule_world_event,
        update_faction,
        update_dm_state,
    ]


def _run_sim_agent() -> str:
    """Run one world-simulation turn. Returns the agent's summary."""
    from src.agents.base import create_agent

    agent = create_agent(
        agent_name="world_sim",
        system_prompt=WORLD_SIM_PROMPT,
        tools=_get_sim_tools(),
        callback_handler=None,
    )
    result = agent("One in-game day has passed. Advance the world.")
    return str(result)


def maybe_advance_world(run_agent=None) -> dict[str, Any]:
    """Run the world turn if a new in-game day has started since the last one.

    Safe to call after every player turn: the day is claimed atomically
    before the (slow) agent runs, so concurrent calls can't double-simulate.

    Args:
        run_agent: Override for the sim runner (tests).

    Returns:
        Dict with whether the world advanced and the sim summary.
    """
    with get_session() as session:
        clock = session.query(WorldClock).first()
        dm_state = session.query(DMState).first()
        if not clock or not dm_state:
            return {"advanced": False, "reason": "world not initialized"}

        last = dm_state.last_world_turn_day
        if last is not None and clock.day <= last:
            return {"advanced": False, "reason": "already simulated today"}

        # Claim the day BEFORE running so a concurrent trigger no-ops
        dm_state.last_world_turn_day = clock.day
        session.commit()
        day = clock.day

        if last is None:
            # The opening day is authored — the seed, the campaign's first
            # act, the NPCs where the author put them. Simulating it on the
            # player's first turn reshuffled that staging before it was
            # played. The world starts moving on its own from day two.
            return {"advanced": False, "reason": "the opening day is authored, not simulated"}

    logger.info(f"World turn: advancing the world for Day {day}")
    try:
        summary = (run_agent or _run_sim_agent)()
        logger.info(f"World turn complete: {str(summary)[:200]}")
        return {"advanced": True, "day": day, "summary": str(summary)}
    except Exception as e:
        logger.error(f"World turn failed: {e}")
        # Leave the day claimed — a broken sim shouldn't retry every turn
        return {"advanced": False, "reason": str(e)}

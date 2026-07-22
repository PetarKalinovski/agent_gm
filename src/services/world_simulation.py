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


def _get_sim_tools() -> list:
    from src.tools.world_read import get_world_state_summary, get_recent_events, get_all_npcs
    from src.tools.world_write import (
        create_event,
        move_npc,
        schedule_world_event,
        update_faction,
        update_dm_state,
    )

    return [
        get_world_state_summary,
        get_recent_events,
        get_all_npcs,
        create_event,
        move_npc,
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

    logger.info(f"World turn: advancing the world for Day {day}")
    try:
        summary = (run_agent or _run_sim_agent)()
        logger.info(f"World turn complete: {str(summary)[:200]}")
        return {"advanced": True, "day": day, "summary": str(summary)}
    except Exception as e:
        logger.error(f"World turn failed: {e}")
        # Leave the day claimed — a broken sim shouldn't retry every turn
        return {"advanced": False, "reason": str(e)}

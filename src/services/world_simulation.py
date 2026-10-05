"""Offscreen world simulation — the world moves even when the player isn't looking.

Once per in-game day, a fast-model agent advances the world: a faction makes
progress on a goal, a major NPC acts on their agenda, a consequence of recent
events lands. Changes are made through the same world-write tools the DM uses,
so they surface naturally — as scheduled events firing, as things NPCs know,
as changed world state the player discovers.

Runs in the background after a player turn (never blocks the turn itself).
"""

import re
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
- locations_involved takes ids from the PLACES list you are given, and only
  those — a place that is not on it does not exist. An event that names one
  is refused.
- The FIXED TRUTHS you are given are the story's: no event may contradict
  them, and no event may reveal, hint at, or restate them. Do not explain the
  world's central mystery (no rituals, no cults, no culprits) — that story is
  told elsewhere.
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


# ----------------------------------------------------------------------
# Events the simulator writes: real places, the story's truths intact
# ----------------------------------------------------------------------
# fleet5-wenna T18 (a night's rest ran the day tick): "Northern Howl
# Investigation" sent Captain Coldiron — kept in the square by the campaign —
# on a patrol to 'northern_hills', and "Third Night of the Calling" had the
# Fangs finish a "three-night ritual" at 'north_gate' and 'town_square'. None
# of those places exists; the truth is whistle and scent-marker training.
# The model now gets the real places and the campaign's fixed truths, and an
# event that names a place that does not resolve, or puts a staged person
# somewhere they are not, is refused.

def _norm_place(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", str(text or "").lower().replace("'", "").replace("’", ""))
    return " ".join(w for w in words if w not in ("the", "a", "an"))


def _resolve_places(session, labels: list[str] | None) -> tuple[list[str], list[str]]:
    """(location ids, labels that name no place). An id or a place's name
    ("Market Square", "market_square") resolves; nothing else does."""
    from src.models import Location

    rows = session.query(Location).all()
    by_id = {row.id: row.id for row in rows}
    by_name = {_norm_place(row.name): row.id for row in rows}
    resolved: list[str] = []
    unknown: list[str] = []
    for label in labels or []:
        place = by_id.get(str(label).strip()) or by_name.get(_norm_place(label))
        if place:
            if place not in resolved:
                resolved.append(place)
        else:
            unknown.append(str(label))
    return resolved, unknown


def _event_refusal(session, locations: list[str] | None,
                   npcs: list[str] | None) -> tuple[str | None, list[str]]:
    """(why the event is refused, or None; its resolved location ids)."""
    from src.models import NPC

    resolved, unknown = _resolve_places(session, locations)
    if unknown:
        verdict = "is not a place" if len(unknown) == 1 else "are not places"
        return (f"refused: {', '.join(repr(u) for u in unknown[:3])} {verdict} in this world. "
                "locations_involved takes ids from the PLACES list only — pick one of those or leave it "
                "empty."), resolved
    staged = staged_npc_ids(session)
    for npc_id in npcs or []:
        npc = session.get(NPC, npc_id) if npc_id in staged else None
        if npc is None or not resolved:
            continue
        # Where they stand, and every place that holds it (the town's event
        # still has the captain in its square)
        around, place = set(), npc.current_location
        while place is not None and place.id not in around:
            around.add(place.id)
            place = place.parent
        if around & set(resolved):
            continue
        where = npc.current_location.name if npc.current_location is not None else "where they are"
        return (f"refused: {npc.name} is staged by the campaign at {where}; an event cannot send them "
                "anywhere else. Set it where they are, or leave them out of npcs_involved."), resolved
    return None, resolved


def sim_create_event(name: str, description: str, event_type: str = "meso",
                     factions_involved: list[str] | None = None,
                     locations_involved: list[str] | None = None,
                     npcs_involved: list[str] | None = None,
                     consequences: list[str] | None = None,
                     player_visible: bool = True) -> dict[str, Any]:
    """Record something that happened offscreen today.

    Args:
        name: Event name.
        description: What happened (never a fixed truth, never a revelation).
        event_type: "macro" (faction-level) or "meso" (local).
        factions_involved: Faction IDs involved.
        locations_involved: Location ids from the PLACES list — nothing else.
        npcs_involved: NPC IDs involved.
        consequences: What changed.
        player_visible: Whether the player could plausibly hear of it.

    Returns:
        The created event, or an "error" explaining the refusal.
    """
    from src.tools.world_write import create_event

    with get_session() as session:
        refusal, places = _event_refusal(session, locations_involved, npcs_involved)
    if refusal:
        return {"error": refusal}
    return create_event(name=name, description=description, event_type=event_type,
                        factions_involved=factions_involved, locations_involved=places,
                        npcs_involved=npcs_involved, consequences=consequences,
                        player_visible=player_visible)


def sim_schedule_world_event(name: str, description: str, event_type: str, scheduled_day: int,
                             scheduled_hour: int, factions_involved: list[str] | None = None,
                             locations_involved: list[str] | None = None,
                             npcs_involved: list[str] | None = None,
                             consequences: list[str] | None = None,
                             player_visible: bool = True) -> dict[str, Any]:
    """Schedule a future world event to fire at a game time.

    Args:
        name: Event name.
        description: What happens when it fires (never a fixed truth).
        event_type: "macro" (faction-level) or "meso" (local).
        scheduled_day: Game day it fires.
        scheduled_hour: Game hour (0-23) it fires.
        factions_involved: Faction IDs involved.
        locations_involved: Location ids from the PLACES list — nothing else.
        npcs_involved: NPC IDs involved.
        consequences: What should change when it fires.
        player_visible: Whether the player can learn about it.

    Returns:
        The scheduled event, or an "error" explaining the refusal.
    """
    from src.tools.world_write import schedule_world_event

    with get_session() as session:
        refusal, places = _event_refusal(session, locations_involved, npcs_involved)
    if refusal:
        return {"error": refusal}
    return schedule_world_event(name=name, description=description, event_type=event_type,
                                scheduled_day=scheduled_day, scheduled_hour=scheduled_hour,
                                factions_involved=factions_involved, locations_involved=places,
                                npcs_involved=npcs_involved, consequences=consequences,
                                player_visible=player_visible)


def world_facts(session) -> str:
    """What the simulator must hold to: every real place (id — name), the
    people the campaign has staged, and the campaign's fixed truths as
    constraints it may never contradict or reveal."""
    from src.models import NPC, Location
    from src.models.campaign import Campaign

    lines = ["PLACES (the only ones that exist — locations_involved takes these ids):"]
    lines += [f"- {row.id} — {row.name}" for row in session.query(Location).order_by(Location.name).all()]
    staged = sorted(staged_npc_ids(session))
    if staged:
        names = [f"{npc.name} (at {npc.current_location.name if npc.current_location else 'nowhere'})"
                 for npc in session.query(NPC).filter(NPC.id.in_(staged)).all()]
        lines.append("STAGED BY THE STORY (never moved, never sent anywhere by an event): " + "; ".join(names))
    truths = []
    for campaign in session.query(Campaign).filter(Campaign.status.in_(("pending", "active"))).all():
        truths += [str(t.get("statement") or "").strip() for t in campaign.fixed_truths or []
                   if isinstance(t, dict) and str(t.get("statement") or "").strip()]
    if truths:
        lines.append("FIXED TRUTHS (constraints — no event contradicts them; none reveals, hints at or "
                     "restates them):")
        lines += [f"- {statement}" for statement in dict.fromkeys(truths)]
    return "\n".join(lines)


def _get_sim_tools() -> list:
    from strands import tool

    from src.tools.world_read import get_world_state_summary, get_recent_events, get_all_npcs
    from src.tools.world_write import (
        update_faction,
        update_dm_state,
    )

    return [
        get_world_state_summary,
        get_recent_events,
        get_all_npcs,
        tool(name="create_event")(sim_create_event),
        tool(name="move_npc")(sim_move_npc),
        tool(name="schedule_world_event")(sim_schedule_world_event),
        update_faction,
        update_dm_state,
    ]


def _run_sim_agent() -> str:
    """Run one world-simulation turn. Returns the agent's summary."""
    from src.agents.base import create_agent

    with get_session() as session:
        facts = world_facts(session)
    agent = create_agent(
        agent_name="world_sim",
        system_prompt=WORLD_SIM_PROMPT,
        tools=_get_sim_tools(),
        callback_handler=None,
    )
    result = agent(f"One in-game day has passed. Advance the world.\n\n{facts}")
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

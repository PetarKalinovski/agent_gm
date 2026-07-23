"""Real-time combat integration — code applies outcomes, the DM narrates.

Flow:
1. The DM calls the `start_combat` tool when the fiction demands a fight.
   The tool result carries `event: combat_start`; the stream tracker turns
   it into an SSE event and the frontend spawns the named NPCs as real-time
   combatants (Hades-style: dodge/attack/telegraphs) using their sprites.
2. The player fights. The room's exits lock while combat is live.
3. The frontend POSTs the outcome to /api/combat/result. THIS module
   applies the mechanics in code — health mapping, NPC deaths — before the
   DM ever sees it, so the DM can only narrate what is already true.
4. The frontend auto-sends a [COMBAT OUTCOME] turn so the DM narrates the
   aftermath (and the world reacts: reputation, events, consequences).
"""

from typing import Any

from loguru import logger

from src.models import NPC, Event, Player, WorldClock, get_session
from src.services.tension import apply_tension_floor

ARCHETYPES = ("brute", "skirmisher", "stalker")

# Hits-taken → minimum health severity. Combat can only worsen health —
# surviving a fight never heals you.
_HITS_TO_STATUS = {0: None, 1: "winded", 2: "hurt", 3: "badly_hurt"}
_HEALTH_ORDER = ["healthy", "winded", "hurt", "badly_hurt", "critical", "dead"]

_BRUTE_WORDS = ("huge", "massive", "hulking", "brawn", "heavy", "giant", "bear", "ox", "broad")
_STALKER_WORDS = ("stealth", "shadow", "hunter", "predator", "wolf", "cat", "silent", "ambush")


def infer_archetype(npc: NPC) -> str:
    """Pick a combat archetype from the NPC's authored fields."""
    if npc.combat_archetype in ARCHETYPES:
        return npc.combat_archetype
    text = " ".join(
        filter(None, [npc.description_physical or "", npc.description_personality or "", npc.profession or ""])
    ).lower()
    if any(w in text for w in _BRUTE_WORDS):
        return "brute"
    if any(w in text for w in _STALKER_WORDS):
        return "stalker"
    return "skirmisher"


def _resolve_npc(session, ident: str, location_id: str | None):
    """Resolve an NPC by id, falling back to (unique) name — LLMs pass
    names instead of UUIDs constantly; the tool should just cope."""
    npc = session.get(NPC, ident)
    if npc:
        return npc
    matches = session.query(NPC).filter(NPC.name.ilike(f"%{ident.strip()}%")).all()
    if len(matches) == 1:
        return matches[0]
    here = [n for n in matches if n.current_location_id == location_id]
    return here[0] if len(here) == 1 else None


def build_combat_start(npc_ids: list[str], player_id: str) -> dict[str, Any]:
    """Validate combatants and build the combat_start event payload."""
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        enemies = []
        for npc_id in npc_ids:
            npc = _resolve_npc(session, npc_id, player.current_location_id)
            if not npc:
                return {"error": f"NPC {npc_id!r} not found (pass the npc_id from get_npcs_at_location)"}
            if npc.status != "alive":
                return {"error": f"{npc.name} is not alive — they can't fight."}
            if npc.current_location_id != player.current_location_id:
                return {
                    "error": (
                        f"{npc.name} is not at the player's location. Move them there "
                        "first (move_npc) or pick someone who is present."
                    )
                }
            enemies.append({
                "npc_id": npc.id,
                "name": npc.name,
                "archetype": infer_archetype(npc),
            })

    if not enemies:
        return {"error": "No combatants given."}

    # Danger is now mechanically real — the music layer follows immediately
    apply_tension_floor("high", "combat started")

    return {
        "success": True,
        "event": "combat_start",
        "enemies": enemies,
        "instruction": (
            "Real-time combat has begun ON THE CANVAS — the player is fighting "
            "it with their own reflexes right now. Write ONE sentence that "
            "FREEZE-FRAMES the instant violence erupts (weapons coming out, the "
            "first step forward) and then END your turn immediately. Absolutely "
            "no exchanges of blows, no parries, no hits, no wounds, no outcome "
            "of any kind — anything you write about the fight will contradict "
            "what the player actually does. You will receive a [COMBAT OUTCOME] "
            "message when it's over; mechanics (health, deaths) will already be "
            "applied — then you narrate the aftermath."
        ),
    }


def apply_combat_result(
    player_id: str,
    outcome: str,
    hits_taken: int = 0,
    defeated_npc_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Apply a finished fight to the database. Returns what changed.

    outcome: "victory" | "defeat" | "fled"
    Health only ever worsens here; "defeat" means downed → critical (the DM
    decides on death afterward, with full context — permadeath is a
    narrative call, not a physics one).
    """
    if outcome not in ("victory", "defeat", "fled"):
        return {"error": f"Invalid outcome {outcome!r}"}

    defeated_npc_ids = defeated_npc_ids or []
    changes: dict[str, Any] = {"outcome": outcome, "npc_deaths": [], "health": None}

    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        # --- Player health: monotonic worsening ---
        if outcome == "defeat":
            floor = "critical"
        else:
            floor = _HITS_TO_STATUS.get(min(hits_taken, 3))
        if floor:
            current_idx = _HEALTH_ORDER.index(player.health_status) if player.health_status in _HEALTH_ORDER else 0
            floor_idx = _HEALTH_ORDER.index(floor)
            if floor_idx > current_idx:
                player.health_status = floor
                changes["health"] = floor

        # --- Defeated NPCs die, permanently, with a world event ---
        clock = session.query(WorldClock).first()
        day = clock.day if clock else 1
        hour = clock.hour if clock else 8
        for npc_id in defeated_npc_ids:
            npc = session.get(NPC, npc_id)
            if not npc or npc.status == "dead":
                continue
            npc.status = "dead"
            session.add(Event(
                name=f"Death of {npc.name}",
                description=(
                    f"{npc.name} was slain by {player.name} in combat on Day {day}."
                ),
                event_type="player",
                occurred_day=day,
                occurred_hour=hour,
                locations_involved=[player.current_location_id] if player.current_location_id else [],
                npcs_involved=[npc.id],
                player_visible=True,
                player_witnessed=True,
                narrated_to_player=False,
                consequences=[f"{npc.name} is dead — their faction and friends will react."],
            ))
            changes["npc_deaths"].append({"npc_id": npc.id, "npc_name": npc.name})

        session.commit()

    # Fight's over — the aftermath stays hot (and the danger counter resets:
    # the fight happened between turns, so no play turn registered it)
    if outcome == "defeat":
        apply_tension_floor("climax", "player downed in combat")
    else:
        apply_tension_floor("high", f"combat {outcome}")

    logger.info(f"Combat result applied: {changes}")
    return changes


def combat_outcome_message(changes: dict[str, Any], fled: bool = False) -> str:
    """The system turn text the frontend sends so the DM narrates aftermath."""
    parts = [f"[COMBAT OUTCOME — resolved in real-time by the player, mechanics already applied: {changes['outcome']}."]
    if changes.get("npc_deaths"):
        names = ", ".join(d["npc_name"] for d in changes["npc_deaths"])
        parts.append(f"Killed: {names} (already marked dead — do NOT call kill_npc).")
    if changes.get("health"):
        parts.append(f"Player health is now '{changes['health']}' (already applied — do NOT call update_player_health for these wounds).")
    parts.append(
        "Narrate the aftermath in the fiction — the scene, reactions, consequences. "
        "Apply follow-on consequences with tools (reputation, events, quest updates) as the story demands.]"
    )
    return " ".join(parts)

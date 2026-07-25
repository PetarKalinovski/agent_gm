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

import re
from typing import Any

from loguru import logger

from src.models import NPC, Event, NPCTier, Player, WorldClock, get_session
from src.services.tension import apply_tension_floor

ARCHETYPES = ("brute", "skirmisher", "stalker", "ranged")

# Hits-taken → minimum health severity. Combat can only worsen health —
# surviving a fight never heals you.
_HITS_TO_STATUS = {0: None, 1: "winded", 2: "hurt", 3: "badly_hurt"}
_HEALTH_ORDER = ["healthy", "winded", "hurt", "badly_hurt", "critical", "dead"]

_BRUTE_WORDS = ("huge", "massive", "hulking", "brawn", "heavy", "giant", "bear", "ox", "broad")
_STALKER_WORDS = ("stealth", "shadow", "hunter", "predator", "wolf", "cat", "silent", "ambush")

# Fights from a distance. Word-boundary matched, unlike the lists above:
# bare substrings would fire on "damage"/"image" ("mage") and "elbow" ("bow").
_RANGED_RE = re.compile(
    r"\b(?:archer|archery|bow|bowman|longbow|crossbow|sling|slinger|sniper|"
    r"caster|spellcaster|mage|spitter|gunner|gunslinger|thrower|marksman|"
    r"arrow|quiver|musket|rifle)\b"
)


def infer_archetype(npc: NPC) -> str:
    """Pick a combat archetype from the NPC's authored fields."""
    if npc.combat_archetype in ARCHETYPES:
        return npc.combat_archetype
    text = " ".join(
        filter(None, [npc.description_physical or "", npc.description_personality or "", npc.profession or ""])
    ).lower()
    # The weapon wins over the body: a hulking archer still fights at range
    if _RANGED_RE.search(text):
        return "ranged"
    if any(w in text for w in _BRUTE_WORDS):
        return "brute"
    if any(w in text for w in _STALKER_WORDS):
        return "stalker"
    return "skirmisher"


# ============================================================================
# Danger tiers — the absolute ladder
#
# Danger is a property of the ENEMY, never of the player. Nothing below reads
# player level, gear, or session time: a harmless beggar has 1 HP and deals 0
# damage at level 1 and at level 40, and that invariance is the only thing
# that makes growth legible. Python is authoritative — the resolved numbers
# travel to the client inside combat_start; the JS table is a fallback only.
# ============================================================================

DANGER_TIERS = ("harmless", "weak", "standard", "dangerous", "deadly", "boss")

# Shape of the fight. Times in ms, distances in normalized 0-100 scene units.
ARCH_ROWS: dict[str, dict] = {
    "brute":      {"hp_mul": 1.4, "speed": 4.8, "windup": 650, "lunge_dist": 10,
                   "lunge_ms": 215, "recover": 800, "engage": 7,  "scale_mul": 1.12},
    "skirmisher": {"hp_mul": 0.6, "speed": 8.5, "windup": 340, "lunge_dist": 8,
                   "lunge_ms": 150, "recover": 430, "engage": 6,  "scale_mul": 0.95},
    "stalker":    {"hp_mul": 0.8, "speed": 6.8, "windup": 280, "lunge_dist": 12,
                   "lunge_ms": 140, "recover": 520, "engage": 9,  "scale_mul": 1.00,
                   "circles": True},
    "ranged":     {"hp_mul": 0.7, "speed": 6.0, "windup": 900, "lunge_dist": 0,
                   "lunge_ms": 120, "recover": 900, "engage": 22, "scale_mul": 0.98,
                   "ranged": True, "standoff": 14, "ring_radius": 9},
}

# Weight of the fight. The `standard` row reproduces today's shipped numbers
# exactly (brute 7 / skirmisher 3 / stalker 4, windups 650/340/280), so a world
# where every danger_tier is NULL plays identically to before the ladder.
TIER_ROWS: dict[str, dict] = {
    # hp is the REFERENCE hp (archetype hp_mul multiplies it, except boss).
    # damage is in PLAYER HEARTS and is ABSOLUTE — it never scales.
    "harmless":  {"hp": 1,  "damage": 0, "speed_mul": 0.55, "windup_mul": 1.00,
                  "recover_mul": 1.00, "lunge_mul": 1.00, "scale_mul": 0.90,
                  "stagger": 1200, "knock_mul": 1.6, "flees": True,  "xp": 0},
    "weak":      {"hp": 3,  "damage": 1, "speed_mul": 0.88, "windup_mul": 1.30,
                  "recover_mul": 1.20, "lunge_mul": 1.10, "scale_mul": 0.95,
                  "stagger": 1100, "knock_mul": 1.2, "xp": 5},
    "standard":  {"hp": 5,  "damage": 1, "speed_mul": 1.00, "windup_mul": 1.00,
                  "recover_mul": 1.00, "lunge_mul": 1.00, "scale_mul": 1.00,
                  "stagger": 900,  "knock_mul": 1.0, "xp": 15},
    "dangerous": {"hp": 8,  "damage": 2, "speed_mul": 1.10, "windup_mul": 0.85,
                  "recover_mul": 0.88, "lunge_mul": 0.92, "scale_mul": 1.08,
                  "stagger": 750,  "knock_mul": 0.7, "xp": 40},
    "deadly":    {"hp": 14, "damage": 2, "speed_mul": 1.20, "windup_mul": 0.72,
                  "recover_mul": 0.78, "lunge_mul": 0.86, "scale_mul": 1.16,
                  "stagger": 620,  "knock_mul": 0.5, "xp": 100},
    "boss":      {"hp": 24, "damage": 2, "speed_mul": 1.12, "windup_mul": 0.80,
                  "recover_mul": 0.85, "lunge_mul": 0.90, "scale_mul": 1.50,
                  "stagger": 520,  "knock_mul": 0.0, "boss": True, "xp": 300},
}

WINDUP_FLOOR_MS = 220   # human reaction floor — no enemy telegraph is shorter

# The one boss pattern set (v1), applied to any boss-tier NPC regardless of
# archetype. The entrance itself is a cinematic, queued server-side — this
# table is only the fight.
BOSS_PATTERN = {
    "phase2_at":         0.5,    # fraction of max hp
    "phase_transition_ms": 1200, # invulnerable roar
    "slam_first_at":     4000,
    "slam_every":        9000,   # phase 1
    "slam_every_p2":     6500,
    "slam_windup":       900,    # ring grows over this
    "slam_radius":       18,     # normalized units
    "slam_damage":       3,      # hearts — the only damage above 2 in the game
    "slam_recover":      1100,   # the punish window; land your heavy here
    "combo_count":       3,      # phase 2 basic attack becomes a 3-lunge chain
    "combo_gap":         180,
    "p2_speed_mul":      1.15,
    "p2_windup_mul":     0.78,
    "p2_stagger":        440,
}


def _r(x: float) -> int:
    """Round-half-UP. Python's round() is banker's rounding — do not use it here."""
    return int(x + 0.5)


def combat_params(tier: str, archetype: str) -> dict:
    """Fixed table lookup. Deterministic, pure, no DB, no player state.

    The two-argument signature IS the no-scaling guarantee: there is no code
    path from any player value into an enemy stat.
    """
    T = TIER_ROWS.get(tier) or TIER_ROWS["standard"]
    A = ARCH_ROWS.get(archetype) or ARCH_ROWS["skirmisher"]
    is_boss_tier = bool(T.get("boss"))

    hp = T["hp"] if is_boss_tier else max(1, _r(T["hp"] * A["hp_mul"]))

    params = {
        "hp": hp,
        "damage": T["damage"],
        "speed": round(A["speed"] * T["speed_mul"], 2),
        "windup": max(WINDUP_FLOOR_MS, _r(A["windup"] * T["windup_mul"])),
        "lungeDist": A["lunge_dist"],
        "lungeMs": _r(A["lunge_ms"] * T["lunge_mul"]),
        "recover": _r(A["recover"] * T["recover_mul"]),
        "engage": A["engage"],
        "scaleMul": round(A["scale_mul"] * T["scale_mul"], 3),
        "stagger": T["stagger"],
        "knockMul": T["knock_mul"],
        "circles": bool(A.get("circles")),
        "ranged": bool(A.get("ranged")),
        "standoff": A.get("standoff", 0),
        "ringRadius": A.get("ring_radius", 0),
        "flees": bool(T.get("flees")),
        "boss": is_boss_tier,
        "tier": tier,
        "archetype": archetype,
    }
    if is_boss_tier:
        params["bossPattern"] = dict(BOSS_PATTERN)
    return params


# Evaluated in this order, first match wins — highest danger first, so
# "veteran guard" resolves `dangerous` rather than `standard`.
_TIER_WORDS: list[tuple[str, tuple[str, ...]]] = [
    ("deadly", (
        "champion", "warlord", "archmage", "high priest", "demon", "wraith",
        "revenant", "giant", "wyvern", "chimera", "abomination", "nightmare",
        "blademaster", "master assassin", "legendary", "monstrosity", "titan",
    )),
    ("dangerous", (
        "captain", "veteran", "knight", "assassin", "duelist", "duellist",
        "berserker", "ogre", "troll", "bear", "alpha", "beast", "witch",
        "sorcerer", "warden", "inquisitor", "executioner", "elite", "guardian",
        "commander", "war-chief", "warchief", "hunter of",
    )),
    ("standard", (
        "guard", "soldier", "bandit", "raider", "mercenary", "sellsword",
        "hunter", "watchman", "sentry", "marine", "trooper", "warrior",
        "brawler", "pirate", "smuggler", "enforcer", "wolf", "boar", "hound",
        "constable", "legionary", "swordsman", "archer", "gunman",
    )),
    ("weak", (
        "cutpurse", "pickpocket", "thug", "lackey", "apprentice", "recruit",
        "conscript", "deckhand", "dockworker", "labourer", "laborer", "miner",
        "hooligan", "footpad", "initiate", "stray", "rat", "novice",
    )),
    ("harmless", (
        "beggar", "child", "kid", "infant", "drunk", "drunkard", "invalid",
        "cripple", "elderly", "grandmother", "grandfather", "urchin",
        "peasant", "farmhand", "scribe", "clerk", "servant", "maid", "cook",
        "baker", "weaver", "tailor", "minstrel", "pilgrim", "patient",
        "wounded", "bedridden", "blind", "prisoner", "refugee", "orphan",
    )),
]

# Last resort: narrative weight is not combat weight, but an AMBIENT extra is
# more likely a bystander than a soldier.
_TIER_BY_NPC_TIER = {
    NPCTier.AMBIENT: "weak",
    NPCTier.MINOR: "standard",
    NPCTier.MAJOR: "standard",
}


def infer_danger_tier(npc: NPC) -> str:
    """Keyword fallback for NPCs with no authored tier.

    NEVER returns "boss" — a boss is an authored set-piece with an entrance
    and a named health bar, and one must never appear by accident.
    """
    text = " ".join(filter(None, [
        npc.profession or "", npc.description_physical or "",
        npc.description_personality or "", npc.species or "",
    ])).lower()
    for tier, words in _TIER_WORDS:
        if any(w in text for w in words):
            return tier
    return _TIER_BY_NPC_TIER.get(npc.tier, "standard")


def resolve_danger_tier(npc: NPC) -> str:
    """Row wins; otherwise infer. The ONLY place a tier is decided."""
    if npc.danger_tier in DANGER_TIERS:
        return npc.danger_tier
    return infer_danger_tier(npc)


def is_boss(npc: NPC) -> bool:
    """The single boss flag. `NPC.boss_title` is a display epithet only —
    a title without the tier does nothing."""
    return resolve_danger_tier(npc) == "boss"


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

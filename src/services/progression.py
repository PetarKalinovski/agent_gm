"""Progression: levels, XP, gear tiers, consumables and code-rolled checks.

Every progression NUMBER in Forge lives in this module — nothing else may hold
one. The DM and the creator agent pick *labels* (quest `scale`, gear `tier`,
consumable `effect_tag`, check `skill`/`difficulty`/`modifier`); code turns
labels into numbers here, so the LLM can never price a legendary sword at five
coins or decide for itself that the sneak worked.

Two hard contracts:

- **Legacy worlds are untouched.** Every read goes through a defaulting helper
  (`player_level`, `player_xp`, `resolve_loadout`), so a pre-overhaul row with
  `level=NULL, xp=NULL, equipment=NULL` produces exactly today's combat
  numbers — 5 hearts at healthy, 1 damage. Asserted by
  `test_level1_unarmed_matches_legacy`.
- **`combat_stats` is pure dict math over an already-loaded `Player` row.** It
  runs after every state-mutating tool; it must never issue a query.

Session-taking functions (`grant_xp`, `equip`, `unequip`, `apply_consumable`,
`auto_equip`) mutate inside the CALLER'S open transaction and never commit —
quest completion and combat resolution need the XP grant to land or roll back
together with the rest of their work.

Combat XP totals are NOT computed here: the tier engine (`src/services/combat.py`)
resolves an NPC's `danger_tier` and hands the total to `grant_xp`.
"""

import random
import re
from typing import Any, NamedTuple

from loguru import logger

# ---------------------------------------------------------------------------
# Levels and XP
# ---------------------------------------------------------------------------

# Cumulative XP required to BE this level. Index = level.
XP_THRESHOLDS = {1: 0, 2: 100, 3: 250, 4: 450, 5: 700,
                 6: 1000, 7: 1400, 8: 1900, 9: 2500, 10: 3200}
MAX_LEVEL = 10


class LevelRow(NamedTuple):
    """What a level buys. `gear_tier_cap` is the highest gear tier INDEX the
    player may equip — the capability half of levelling: gold alone cannot
    buy power."""

    hearts_bonus: int
    damage_bonus: int
    dodge_cooldown_ms: int
    gear_tier_cap: int
    unlock_text: str | None


LEVEL_TABLE = {
    1:  LevelRow(0, 0, 750, 2, None),
    2:  LevelRow(1, 0, 750, 2, "You take a hit better than you used to."),
    3:  LevelRow(1, 0, 700, 3, "Superior gear no longer fights you."),
    4:  LevelRow(2, 0, 700, 3, "You take a hit better than you used to."),
    5:  LevelRow(2, 1, 650, 4, "Your strikes land harder."),
    6:  LevelRow(3, 1, 650, 4, "You take a hit better than you used to."),
    7:  LevelRow(3, 1, 600, 5, "The finest gear answers to you."),
    8:  LevelRow(4, 1, 550, 5, "You move out of the way faster."),
    9:  LevelRow(4, 2, 550, 5, "Your strikes land harder."),
    10: LevelRow(4, 2, 500, 5, "You are the thing others tell stories about."),
}

# The only XP faucets are quest completion and combat kills, both code-driven.
# Combat totals arrive from the tier engine; quests are scaled by a DM label.
QUEST_XP = {"errand": 60, "job": 120, "mission": 220, "saga": 400}
DEFAULT_QUEST_SCALE = "job"

# ---------------------------------------------------------------------------
# Gear — one ladder, three effect tables
# ---------------------------------------------------------------------------

TIER_LABELS = ("crude", "common", "fine", "superior", "master", "legendary")  # index 0..5
SLOTS = ("weapon", "armor", "trinket")

WEAPON_DAMAGE = (0, 1, 2, 3, 4, 5)          # added to base damage 1
ARMOR_HEARTS = (0, 1, 1, 2, 2, 3)           # added to base 5 full hearts
TRINKET_IFRAMES = (0, 40, 80, 120, 160, 200)  # ms added to the 350ms dodge i-frame

TIER_PRICE = {
    "weapon":  (10, 40, 120, 300, 700, 2000),
    "armor":   (15, 50, 150, 350, 800, 2200),
    "trinket": (20, 60, 160, 380, 850, 2400),
}

# Legendary is never sold — it is won. `stock_merchant` refuses it.
LEGENDARY_INDEX = TIER_LABELS.index("legendary")

# ---------------------------------------------------------------------------
# Consumables
# ---------------------------------------------------------------------------

CONSUMABLES = {
    # tag            hearts  status_steps  buff        duration_ms  price
    "heal_light":  {"hearts": 2, "status_steps": 1, "buff": None,     "ms": 0,     "price": 25},
    "heal_strong": {"hearts": 4, "status_steps": 2, "buff": None,     "ms": 0,     "price": 70},
    "surge":       {"hearts": 0, "status_steps": 0, "buff": "damage", "ms": 20000, "price": 60},
}
SURGE_DAMAGE_BONUS = 1

# ---------------------------------------------------------------------------
# Skill checks
# ---------------------------------------------------------------------------

SKILLS = ("stealth", "persuasion", "athletics", "perception", "cunning")
DIFFICULTY_DC = {"easy": 8, "tricky": 12, "hard": 16, "desperate": 20}
MODIFIERS = ("none", "advantage", "disadvantage")
OUTCOMES = ("critical_success", "success", "partial", "failure", "critical_failure")

# Player.traits words that grant +2 in a domain. Fixed, code-owned.
TRAIT_BONUS = {
    "stealth":    {"quick", "nimble", "silent", "sneaky", "small", "light-footed"},
    "persuasion": {"charming", "silver-tongued", "honest", "commanding", "kind", "noble"},
    "athletics":  {"strong", "tough", "hardy", "athletic", "big", "trained"},
    "perception": {"sharp", "observant", "wary", "paranoid", "keen"},
    "cunning":    {"cunning", "clever", "educated", "scholarly", "crafty"},
}
TRAIT_BONUS_VALUE = 2

# How far below the DC still counts as "you got it, but it cost you".
PARTIAL_BAND = 3

# The DM reads nothing else about the roll — no dice, no DC, no odds.
OUTCOME_INSTRUCTIONS = {
    "critical_success": (
        "CRITICAL SUCCESS — it works, and something extra breaks their way (an "
        "overheard secret, a faster route, a witness who now owes them). Narrate "
        "the fiction only: never mention rolls, dice, difficulty, or odds."
    ),
    "success": (
        "SUCCESS — it works cleanly. Narrate the fiction only: never mention "
        "rolls, dice, difficulty, or odds."
    ),
    "partial": (
        "PARTIAL SUCCESS — they get what they wanted but it costs them something "
        "concrete. Apply that cost with a tool (time, health, an item, money, "
        "trust, standing). Narrate the fiction only: never mention rolls, dice, "
        "difficulty, or odds."
    ),
    "failure": (
        "FAILURE — it does not work. Apply a concrete consequence with a tool "
        "(time lost, injury, item lost, money lost, trust or standing damaged). "
        "Do not soften it into a success. Narrate the fiction only: never mention "
        "rolls, dice, difficulty, or odds."
    ),
    "critical_failure": (
        "CRITICAL FAILURE — it fails and the situation gets worse: they are seen, "
        "the rope snaps, the lie is caught. Apply a real consequence with a tool, "
        "and consider whether this starts a fight. Narrate the fiction only: never "
        "mention rolls, dice, difficulty, or odds."
    ),
}

# ---------------------------------------------------------------------------
# Combat formulas — the contract with game.js (state.combat)
# ---------------------------------------------------------------------------

HEALTH_FRACTION = {"healthy": 1.0, "winded": 0.8, "hurt": 0.6,
                   "badly_hurt": 0.4, "critical": 0.2, "dead": 0.0}
HEALTH_ORDER = ("healthy", "winded", "hurt", "badly_hurt", "critical", "dead")

BASE_HEARTS = 5
BASE_DAMAGE = 1
BASE_IFRAME_MS = 350
HEART_CAP = 12

# One RNG for the whole module so tests can pin it. Never `random.random()`.
_RNG = random.Random()


def set_seed(n: int) -> None:
    """Pin the check RNG (tests only)."""
    _RNG.seed(n)


# ---------------------------------------------------------------------------
# Defaulting readers — a NULL column must never reach arithmetic
# ---------------------------------------------------------------------------


def player_level(p) -> int:
    """Level of a player row, clamped to 1..MAX_LEVEL. NULL reads as 1."""
    try:
        level = int(getattr(p, "level", None) or 1)
    except (TypeError, ValueError):
        level = 1
    return max(1, min(MAX_LEVEL, level))


def player_xp(p) -> int:
    """Total XP of a player row. NULL reads as 0."""
    try:
        return max(0, int(getattr(p, "xp", None) or 0))
    except (TypeError, ValueError):
        return 0


def gear_tier_cap(p) -> int:
    """Highest gear tier index this player may equip."""
    return LEVEL_TABLE[player_level(p)].gear_tier_cap


def level_for_xp(xp: int) -> int:
    """Highest level whose cumulative threshold this XP total has reached."""
    level = 1
    for candidate in range(1, MAX_LEVEL + 1):
        if xp >= XP_THRESHOLDS[candidate]:
            level = candidate
    return level


def min_level_for_tier(tier_idx: int) -> int:
    """Lowest level whose gear_tier_cap reaches `tier_idx`.

    Derived from LEVEL_TABLE, never a second table — retuning the level table
    retunes this automatically.
    """
    for level in range(1, MAX_LEVEL + 1):
        if LEVEL_TABLE[level].gear_tier_cap >= tier_idx:
            return level
    return MAX_LEVEL


def tier_index(label: str | None) -> int | None:
    """'fine' -> 2. Unknown or missing -> None."""
    if not label or not isinstance(label, str):
        return None
    try:
        return TIER_LABELS.index(label.strip().lower())
    except ValueError:
        return None


def tier_label(index: int | None) -> str | None:
    """2 -> 'fine'. Out of range -> None."""
    if index is None or not isinstance(index, int):
        return None
    if 0 <= index < len(TIER_LABELS):
        return TIER_LABELS[index]
    return None


# ---------------------------------------------------------------------------
# Items: the mechanical reading of an inventory entry
# ---------------------------------------------------------------------------


def mechanize_item(item: dict | None) -> dict | None:
    """Mechanical reading of an inventory entry, or None if it is plain stuff.

    Returns {"slot": str|None, "tier": int|None, "effect_tag": str|None}.

    Legacy repair: an item with no `slot` but `type` in ("weapon", "armor")
    reads as that slot at tier index 1 ("common"), so worlds that already
    contain an 'Iron Sword' become equippable the moment this ships.
    Legacy repair 2: `type == "consumable"` with `effects {"heal": n}` and no
    effect_tag reads as heal_light (n < 40) or heal_strong (n >= 40).
    """
    if not isinstance(item, dict):
        return None

    raw_slot = item.get("slot")
    slot = raw_slot.strip().lower() if isinstance(raw_slot, str) else None
    if slot not in SLOTS:
        slot = None

    item_type = (item.get("type") or "").strip().lower()
    tier: int | None = None
    if slot:
        # An authored slot with a missing/garbled tier is still gear — the
        # cheapest honest reading is "common".
        tier = tier_index(item.get("tier"))
        if tier is None:
            tier = 1
    elif item_type in ("weapon", "armor"):
        slot = item_type
        tier = tier_index(item.get("tier"))
        if tier is None:
            tier = 1

    raw_tag = item.get("effect_tag")
    effect_tag = raw_tag.strip().lower() if isinstance(raw_tag, str) else None
    if effect_tag not in CONSUMABLES:
        effect_tag = None
    if not effect_tag and item_type == "consumable":
        effects = item.get("effects") or {}
        heal = effects.get("heal") if isinstance(effects, dict) else None
        if isinstance(heal, (int, float)) and heal > 0:
            effect_tag = "heal_light" if heal < 40 else "heal_strong"

    if not slot and not effect_tag:
        return None
    return {"slot": slot, "tier": tier, "effect_tag": effect_tag}


def catalog_price(item: dict | None) -> int | None:
    """Code-owned price for mechanical items; None for plain items (whose
    authored `value` stands)."""
    mech = mechanize_item(item)
    if not mech:
        return None
    if mech["slot"] and mech["tier"] is not None:
        return TIER_PRICE[mech["slot"]][mech["tier"]]
    if mech["effect_tag"]:
        return CONSUMABLES[mech["effect_tag"]]["price"]
    return None


def _inventory(p) -> list[dict]:
    """Structured inventory entries only (legacy bare strings are ignored by
    the mechanical layer — `get_inventory` still repairs them for display)."""
    return [e for e in (getattr(p, "inventory", None) or []) if isinstance(e, dict)]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")


def find_item(p, item_id: str | None) -> dict | None:
    """Locate an inventory entry by id, then by name (LLMs and HUD buttons
    both pass whatever they have)."""
    if not item_id:
        return None
    wanted = str(item_id).strip()
    lowered = wanted.lower()
    slug = _slug(wanted)
    for entry in _inventory(p):
        if str(entry.get("id") or "") == wanted:
            return entry
    for entry in _inventory(p):
        if str(entry.get("id") or "").lower() in (lowered, slug):
            return entry
        if str(entry.get("name") or "").strip().lower() == lowered:
            return entry
        if _slug(str(entry.get("name") or "")) == slug:
            return entry
    return None


def item_key(item: dict) -> str:
    """The id an equipment pointer / quick slot stores for an entry."""
    return str(item.get("id") or _slug(str(item.get("name") or "")))


# ---------------------------------------------------------------------------
# Equipment
# ---------------------------------------------------------------------------


def equipment_pointers(p) -> dict[str, str | None]:
    """The raw {slot: item_id|None} map, always with all three slots."""
    stored = getattr(p, "equipment", None) or {}
    if not isinstance(stored, dict):
        stored = {}
    return {slot: stored.get(slot) or None for slot in SLOTS}


def resolve_loadout(p) -> dict[str, dict | None]:
    """{"weapon": item_dict|None, "armor": ..., "trinket": ...}.

    Silently drops pointers to items no longer in the inventory and to items
    above the player's gear_tier_cap (a level cannot be lost, so the latter
    only fires if a table is retuned).
    """
    cap = gear_tier_cap(p)
    loadout: dict[str, dict | None] = {slot: None for slot in SLOTS}
    for slot, item_id in equipment_pointers(p).items():
        if not item_id:
            continue
        item = find_item(p, item_id)
        if not item:
            continue
        mech = mechanize_item(item)
        if not mech or mech["slot"] != slot:
            continue
        if mech["tier"] is None or mech["tier"] > cap:
            continue
        loadout[slot] = item
    return loadout


def can_equip(p, item: dict | None) -> tuple[bool, str]:
    """(ok, reason_code). reason codes: "" | not_equipment | level."""
    mech = mechanize_item(item)
    if not mech or not mech["slot"]:
        return False, "not_equipment"
    if mech["tier"] is None:
        return False, "not_equipment"
    if mech["tier"] > gear_tier_cap(p):
        return False, "level"
    return True, ""


def equip(session, p, item_id: str) -> dict[str, Any]:
    """Equip an inventory item into its slot. Mutates `p.equipment`.

    The item STAYS in the bag — equipment is a pointer, so every existing
    inventory code path keeps working. The caller commits.
    """
    item = find_item(p, item_id)
    if not item:
        return {"error": "Not in your inventory.", "reason": "missing"}

    ok, reason = can_equip(p, item)
    if not ok:
        if reason == "level":
            mech = mechanize_item(item) or {}
            required = min_level_for_tier(mech.get("tier") or 0)
            return {
                "error": f"{item.get('name', 'That')} is beyond you for now.",
                "reason": "level",
                "required_level": required,
                "level": player_level(p),
            }
        return {"error": "That is not something you can wear or wield.", "reason": "not_equipment"}

    mech = mechanize_item(item) or {}
    slot = mech["slot"]
    pointers = equipment_pointers(p)
    previous_id = pointers.get(slot)
    key = item_key(item)
    replaced = None
    if previous_id and previous_id != key:
        previous = find_item(p, previous_id)
        replaced = previous.get("name") if previous else None

    pointers[slot] = key
    p.equipment = dict(pointers)  # new dict: SQLAlchemy must see the change
    return {
        "success": True,
        "slot": slot,
        "item_id": key,
        "item_name": item.get("name") or key,
        "tier": tier_label(mech.get("tier")),
        "replaced": replaced,
        "equipment": dict(pointers),
    }


def unequip(session, p, slot: str) -> dict[str, Any]:
    """Clear one equipment slot. Mutates `p.equipment`. The caller commits."""
    slot = (slot or "").strip().lower()
    if slot not in SLOTS:
        return {"error": f"Unknown slot {slot!r}.", "reason": "not_equipment", "valid": list(SLOTS)}

    pointers = equipment_pointers(p)
    item_id = pointers.get(slot)
    item = find_item(p, item_id) if item_id else None
    pointers[slot] = None
    p.equipment = dict(pointers)
    return {
        "success": True,
        "slot": slot,
        "item_name": (item.get("name") if item else None),
        "equipment": dict(pointers),
    }


def auto_equip(session, p, item_id: str) -> dict[str, Any] | None:
    """Equip a just-acquired item IF its slot is empty and the level allows it.

    A sword that sits in the bag doing nothing is a bug report — most players
    will never open the character panel. Returns the `equip` result when it
    fired, None otherwise. The caller commits.
    """
    item = find_item(p, item_id)
    if not item:
        return None
    mech = mechanize_item(item)
    if not mech or not mech["slot"]:
        return None
    if resolve_loadout(p).get(mech["slot"]):
        return None  # they already wear something there — never silently swap
    ok, _reason = can_equip(p, item)
    if not ok:
        return None
    result = equip(session, p, item_key(item))
    return result if result.get("success") else None


def equipment_summary(p) -> dict[str, dict | None]:
    """{"weapon": {"item_id", "name", "tier"} | None, ...} for the HUD."""
    summary: dict[str, dict | None] = {}
    for slot, item in resolve_loadout(p).items():
        if not item:
            summary[slot] = None
            continue
        mech = mechanize_item(item) or {}
        summary[slot] = {
            "item_id": item_key(item),
            "name": item.get("name") or item_key(item),
            "tier": tier_label(mech.get("tier")),
        }
    return summary


def equipped_labels(p) -> dict[str, str]:
    """{"weapon": "Iron Sword (fine)"} — what `get_player` shows the DM."""
    labels = {}
    for slot, entry in equipment_summary(p).items():
        if entry:
            tier = entry.get("tier")
            labels[slot] = f"{entry['name']} ({tier})" if tier else entry["name"]
    return labels


def annotate_item(p, item: dict) -> dict[str, Any]:
    """The mechanical keys `build_player_state` / `get_inventory` add to an
    inventory entry: slot, tier, effect_tag, equipped, equippable, price."""
    mech = mechanize_item(item) or {}
    key = item_key(item)
    equipped = any(pid == key for pid in equipment_pointers(p).values())
    ok, _reason = can_equip(p, item)
    price = catalog_price(item)
    return {
        "slot": mech.get("slot"),
        "tier": tier_label(mech.get("tier")),
        "effect_tag": mech.get("effect_tag"),
        "equipped": bool(equipped and mech.get("slot")),
        "equippable": ok,
        "price": price if price is not None else item.get("value", 0),
    }


# ---------------------------------------------------------------------------
# Consumables
# ---------------------------------------------------------------------------


def default_quick_slot(p) -> str | None:
    """The item the Q key binds to when the player never chose one: the first
    healing draught in the bag, else the first consumable of any kind."""
    fallback = None
    for entry in _inventory(p):
        mech = mechanize_item(entry)
        tag = mech.get("effect_tag") if mech else None
        if not tag:
            continue
        if CONSUMABLES[tag]["hearts"] > 0:
            return item_key(entry)
        if fallback is None:
            fallback = item_key(entry)
    return fallback


def quick_slot_payload(p) -> dict[str, Any] | None:
    """What `state.combat.quick_slot` carries — enough for the client to apply
    a potion optimistically without a second request."""
    chosen = getattr(p, "quick_slot_item_id", None) or default_quick_slot(p)
    item = find_item(p, chosen) if chosen else None
    if not item:
        return None
    mech = mechanize_item(item) or {}
    tag = mech.get("effect_tag")
    if not tag:
        return None
    return {
        "item_id": item_key(item),
        "name": item.get("name") or item_key(item),
        "effect_tag": tag,
        "hearts": CONSUMABLES[tag]["hearts"],
        "quantity": int(item.get("quantity", 1) or 1),
    }


def _shift_health(status: str, steps: int) -> str:
    """Move `steps` toward 'healthy' along HEALTH_ORDER."""
    try:
        idx = HEALTH_ORDER.index(status)
    except ValueError:
        idx = 0
    return HEALTH_ORDER[max(0, idx - max(0, steps))]


def _consume_one(p, item: dict) -> int:
    """Decrement the stack (removing it at zero). Returns what is left."""
    inventory = [dict(e) if isinstance(e, dict) else e for e in (getattr(p, "inventory", None) or [])]
    key = item_key(item)
    remaining = 0
    for i, entry in enumerate(inventory):
        if not isinstance(entry, dict) or item_key(entry) != key:
            continue
        remaining = int(entry.get("quantity", 1) or 1) - 1
        if remaining > 0:
            entry["quantity"] = remaining
        else:
            remaining = 0
            inventory.pop(i)
        break
    p.inventory = inventory  # new list: SQLAlchemy must see the change
    return remaining


def apply_consumable(session, p, item_id: str, in_combat: bool) -> dict[str, Any]:
    """Drink/apply one consumable. Mutates the player. The caller commits.

    `hearts` applies only IN combat, `status_steps` only OUT of it — hearts do
    not exist outside a fight. One item, two honest readings of the fiction.
    """
    item = find_item(p, item_id)
    if not item:
        return {"error": "Nothing left to drink.", "reason": "missing"}

    mech = mechanize_item(item) or {}
    tag = mech.get("effect_tag")
    if not tag:
        return {"error": "That does nothing when swallowed.", "reason": "not_consumable"}
    if getattr(p, "health_status", "healthy") == "dead":
        return {"error": "That will not help them now.", "reason": "dead"}

    row = CONSUMABLES[tag]
    name = item.get("name") or item_key(item)

    if row["buff"]:
        effect = {
            "kind": "buff",
            "hearts": 0,
            "buff": row["buff"],
            "bonus": SURGE_DAMAGE_BONUS,
            "duration_ms": row["ms"],
        }
    elif in_combat:
        effect = {"kind": "heal", "hearts": row["hearts"], "buff": None, "duration_ms": 0}
    else:
        before = getattr(p, "health_status", "healthy") or "healthy"
        after = _shift_health(before, row["status_steps"])
        p.health_status = after
        effect = {"kind": "heal", "hearts": 0, "buff": None, "duration_ms": 0,
                  "health": f"{before} -> {after}"}

    remaining = _consume_one(p, item)
    return {"success": True, "item_id": item_key(item), "item_name": name,
            "effect_tag": tag, "effect": effect, "remaining": remaining}


# ---------------------------------------------------------------------------
# Combat stats — the ONLY place player combat numbers are computed
# ---------------------------------------------------------------------------


def combat_stats(p) -> dict[str, Any]:
    """The merged `state.combat` payload. Pure dict math — never a query.

    At level 1 with nothing equipped this returns max_hearts 5, hearts
    5/4/3/2/1 by health and light_damage 1 — byte-identical to the legacy
    `_playerHpFromHealth` map in game.js.
    """
    level = player_level(p)
    row = LEVEL_TABLE[level]
    loadout = resolve_loadout(p)

    def _tier_of(slot: str) -> int:
        item = loadout.get(slot)
        mech = mechanize_item(item) if item else None
        idx = mech.get("tier") if mech else None
        return idx if isinstance(idx, int) else 0

    health = getattr(p, "health_status", "healthy") or "healthy"
    fraction = HEALTH_FRACTION.get(health, 1.0)

    max_hearts = min(HEART_CAP, BASE_HEARTS + row.hearts_bonus + ARMOR_HEARTS[_tier_of("armor")])
    hearts = 0 if health == "dead" else max(1, round(max_hearts * fraction))
    light = BASE_DAMAGE + row.damage_bonus + WEAPON_DAMAGE[_tier_of("weapon")]

    return {
        "level": level,
        "max_hearts": max_hearts,
        "hearts": hearts,
        "light_damage": light,
        "heavy_damage": light * 2 + 1,
        "dodge_cooldown_ms": row.dodge_cooldown_ms,
        "iframe_ms": BASE_IFRAME_MS + TRINKET_IFRAMES[_tier_of("trinket")],
        "quick_slot": quick_slot_payload(p),
        "parry_unlocked": True,
        "heavy_unlocked": True,
    }


# ---------------------------------------------------------------------------
# XP
# ---------------------------------------------------------------------------


def quest_scale(quest) -> str:
    """The quest's effort label, defaulted and clamped. NULL reads as "job"."""
    raw = getattr(quest, "scale", None)
    if isinstance(raw, str) and raw.strip().lower() in QUEST_XP:
        return raw.strip().lower()
    return DEFAULT_QUEST_SCALE


def xp_for_quest(quest) -> int:
    """XP a completed quest pays, from its `scale` label."""
    return QUEST_XP[quest_scale(quest)]


def strip_authored_xp(rewards: dict | None) -> dict:
    """The DM authors quest rewards. XP is NOT theirs to author — strip and log."""
    rewards = dict(rewards or {})
    for forbidden in ("xp", "experience", "level", "levels"):
        if forbidden in rewards:
            logger.warning(
                f"Quest rewards tried to grant {forbidden}={rewards[forbidden]!r} — ignored"
            )
            rewards.pop(forbidden)
    return rewards


def xp_progress(p) -> dict[str, int]:
    """{"level", "xp", "xp_into_level", "xp_for_level"} for the HUD bar."""
    level = player_level(p)
    xp = player_xp(p)
    if level >= MAX_LEVEL:
        return {"level": level, "xp": xp, "xp_into_level": 0, "xp_for_level": 0}
    return {
        "level": level,
        "xp": xp,
        "xp_into_level": xp - XP_THRESHOLDS[level],
        "xp_for_level": XP_THRESHOLDS[level + 1] - XP_THRESHOLDS[level],
    }


def grant_xp(session, p, amount: int, source: str) -> dict[str, Any]:
    """Add XP, resolve any number of level-ups, return the progression block.

    Runs inside the CALLER'S transaction (quest completion, combat result) and
    does NOT commit. Returns {} when there is nothing to grant, so callers can
    write `if progression:`.
    """
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return {}
    if amount <= 0:
        return {}

    previous_level = player_level(p)
    total = player_xp(p) + amount
    level = level_for_xp(total)

    p.xp = total
    p.level = level

    # Every crossed level's unlock text, deduped (the table repeats a few
    # lines by design — three identical toasts would read as a bug).
    unlocked: list[str] = []
    for crossed in range(previous_level + 1, level + 1):
        text = LEVEL_TABLE[crossed].unlock_text
        if text and text not in unlocked:
            unlocked.append(text)

    progress = xp_progress(p)
    stats = combat_stats(p)
    block = {
        "xp_gained": amount,
        "source": source,
        "xp": total,
        "level": level,
        "previous_level": previous_level,
        "leveled_up": level > previous_level,
        "xp_into_level": progress["xp_into_level"],
        "xp_for_level": progress["xp_for_level"],
        "unlocked": unlocked,
        "stats": {"max_hearts": stats["max_hearts"], "damage": stats["light_damage"]},
    }
    logger.info(
        f"XP +{amount} ({source}) -> {total} total, level {previous_level} -> {level}"
    )
    return block


# ---------------------------------------------------------------------------
# Skill checks
# ---------------------------------------------------------------------------


def trait_bonus(p, skill: str) -> int:
    """+2 once if any of the player's traits sits in the skill's word set."""
    words = TRAIT_BONUS.get(skill, set())
    for trait in (getattr(p, "traits", None) or []):
        if not isinstance(trait, str):
            continue
        normalized = trait.strip().lower()
        if normalized in words:
            return TRAIT_BONUS_VALUE
        # "very quick", "quick-witted" — a trait phrase still counts
        for token in re.split(r"[^a-z-]+", normalized):
            if token and token in words:
                return TRAIT_BONUS_VALUE
    return 0


def roll_skill_check(p, skill: str, difficulty: str, modifier: str = "none", *,
                     situation: dict | None = None) -> dict[str, Any]:
    """Roll d20 + level + trait bonus against the difficulty's DC.

    Returns {"outcome", "skill", "difficulty", "modifier", "_debug"} — the
    `_debug` key holds roll/DC/bonuses FOR LOGS ONLY and callers MUST strip it
    before it can reach the DM. Invalid words return
    {"error", "valid"} rather than a silent coercion — a silent coercion is a
    number the DM did not choose.

    `situation` is the world's own word about this attempt, built by the
    `roll_check` tool from `perception.situational_factor` +
    `perception.combine_modifier` (this module must never import perception).
    Only its "effective" key is read, and it replaces `modifier` for the roll;
    `modifier` is still reported back unchanged, because it is the DM's word.
    `situation=None` is byte-identical to the pre-attire behaviour.
    """
    skill = (skill or "").strip().lower()
    difficulty = (difficulty or "").strip().lower()
    modifier = (modifier or "none").strip().lower()

    if skill not in SKILLS:
        return {"error": f"Unknown skill {skill!r}.", "valid": list(SKILLS)}
    if difficulty not in DIFFICULTY_DC:
        return {"error": f"Unknown difficulty {difficulty!r}.", "valid": list(DIFFICULTY_DC)}
    if modifier not in MODIFIERS:
        return {"error": f"Unknown modifier {modifier!r}.", "valid": list(MODIFIERS)}

    effective = (situation or {}).get("effective") or modifier
    if effective not in MODIFIERS:
        effective = modifier

    rolls = [_RNG.randint(1, 20)]
    if effective in ("advantage", "disadvantage"):
        rolls.append(_RNG.randint(1, 20))
    natural = max(rolls) if effective == "advantage" else min(rolls) if effective == "disadvantage" else rolls[0]

    level = player_level(p)
    bonus = trait_bonus(p, skill)
    dc = DIFFICULTY_DC[difficulty]
    total = natural + level + bonus

    if natural == 20:
        outcome = "critical_success"
    elif natural == 1:
        outcome = "critical_failure"
    elif total >= dc:
        outcome = "success"
    elif total >= dc - PARTIAL_BAND:
        outcome = "partial"
    else:
        outcome = "failure"

    result = {
        "outcome": outcome,
        "skill": skill,
        "difficulty": difficulty,
        "modifier": modifier,
        "_debug": {
            "rolls": rolls,
            "natural": natural,
            "level_bonus": level,
            "trait_bonus": bonus,
            "total": total,
            "dc": dc,
            "effective_modifier": effective,
        },
    }
    # Absent when nothing applied, so a legacy caller's return shape is untouched
    if situation:
        result["situation"] = situation
    return result

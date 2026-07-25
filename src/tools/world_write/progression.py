"""DM tools for the progression engine: the roll and the merchant's shelf.

Two tools, both label-in / label-out. The DM picks WORDS — a skill, a
difficulty, a gear tier — and `src/services/progression.py` owns every number
behind them. `roll_check` never tells the DM what it rolled (the dice live in
the log, stripped from the return value), and `stock_merchant` prices from
`TIER_PRICE`, so a legendary blade can never turn up on a village shelf for
five coins.
"""

from typing import Any

from loguru import logger
from strands import tool

from src.models import Item, NPC, Player, get_session
from src.services import progression as prog

# Gear kinds a merchant may stock, plus the `Item.type` each maps to. A
# trinket is not one of the legacy item types, so it lands as "misc".
GEAR_KINDS = prog.SLOTS
_ITEM_TYPE_BY_SLOT = {"weapon": "weapon", "armor": "armor", "trinket": "misc"}
_STOCK_KINDS = tuple(GEAR_KINDS) + ("consumable",)
MAX_STOCK_COUNT = 10


@tool
def roll_check(
    player_id: str,
    skill: str,
    difficulty: str,
    modifier: str = "none",
    reason: str = "",
) -> dict[str, Any]:
    """Resolve a risky, uncertain action with an engine roll. Use this BEFORE
    narrating whether it worked.

    Call it for anything the player could plausibly fail: sneaking, lying,
    persuading, climbing, picking a lock, spotting an ambush, forcing a door,
    holding a poker face. Do NOT call it for safe actions (walking, asking a
    question, buying openly) or for real-time fights (that is start_combat).

    You choose the WORDS. The engine owns the numbers and will not tell you
    what they were — narrate the outcome it returns and nothing about how it
    was decided.

    Args:
        player_id: The player's ID.
        skill: One of "stealth", "persuasion", "athletics", "perception", "cunning".
        difficulty: One of "easy", "tricky", "hard", "desperate".
        modifier: "advantage" if the fiction clearly favours them (a distraction
            they set up, a lie backed by evidence, a rope they brought),
            "disadvantage" if it clearly works against them (wounded, watched,
            no tools), otherwise "none".
        reason: One short phrase for the log, e.g. "slipping past the dock guard".

    Returns:
        Dictionary with the outcome label ("critical_success", "success",
        "partial", "failure" or "critical_failure") and how to narrate it.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        result = prog.roll_skill_check(player, skill, difficulty, modifier)

    if "error" in result:
        # Never silently coerce — a coerced word is a number the DM did not choose
        return result

    # The dice, the DC and the bonuses are for the log ONLY. Everything the DM
    # can read, it can leak into prose.
    debug = result.pop("_debug", {})
    outcome = result["outcome"]
    logger.info(
        f"roll_check {result['skill']}/{result['difficulty']} "
        f"({result['modifier']}) -> {outcome} "
        f"[roll {debug.get('natural')} + lvl {debug.get('level_bonus')} "
        f"+ trait {debug.get('trait_bonus')} = {debug.get('total')} vs DC {debug.get('dc')}]"
        + (f" :: {reason}" if reason else "")
    )

    return {
        "success": True,
        "outcome": outcome,
        "skill": result["skill"],
        "difficulty": result["difficulty"],
        "event": "check_result",
        "instruction": prog.OUTCOME_INSTRUCTIONS[outcome],
    }


def _stock_error(message: str, valid: list[str]) -> dict[str, Any]:
    """A refusal that teaches the rule instead of quietly accepting it."""
    return {"error": message, "valid": valid}


def _prepare_entry(entry: Any) -> dict[str, Any]:
    """Validate one stock line into an Item, or return an {"error": ...}."""
    if not isinstance(entry, dict):
        return _stock_error(
            "Each entry must be a dictionary like "
            '{"kind": "weapon", "tier": "common", "name": "Ashwood Blade"}.',
            list(_STOCK_KINDS),
        )

    kind = str(entry.get("kind") or "").strip().lower()
    name = str(entry.get("name") or "").strip()
    if kind not in _STOCK_KINDS:
        return _stock_error(
            f"Unknown kind {kind!r}. A merchant stocks gear or consumables.",
            list(_STOCK_KINDS),
        )
    if not name:
        return _stock_error("Every entry needs a 'name' — the flavour is yours.", [])

    try:
        count = int(entry.get("count") or 1)
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(MAX_STOCK_COUNT, count))

    description = str(entry.get("description") or "")
    # Same slug rule as progression.item_key, so an equipment pointer written
    # against this item resolves back to it.
    item_id = prog._slug(name) or "stock_item"

    if kind in GEAR_KINDS:
        tier = str(entry.get("tier") or "").strip().lower()
        tier_idx = prog.tier_index(tier)
        if tier_idx is None:
            return _stock_error(
                f"Unknown tier {tier!r} for {name}. Gear tiers are fixed words.",
                list(prog.TIER_LABELS),
            )
        if tier_idx >= prog.LEGENDARY_INDEX:
            return _stock_error(
                f"Legendary gear is never for sale — {name} cannot sit on a shelf. "
                "Legendary is won: make it a quest reward or the prize of a boss. "
                "Stock master or below.",
                list(prog.TIER_LABELS[:prog.LEGENDARY_INDEX]),
            )
        item = Item(
            id=item_id,
            name=name,
            type=_ITEM_TYPE_BY_SLOT[kind],
            value=prog.TIER_PRICE[kind][tier_idx],
            description=description,
            stackable=False,
            quantity=count,
            slot=kind,
            tier=prog.TIER_LABELS[tier_idx],
        )
        return {"item": item}

    effect_tag = str(entry.get("effect_tag") or "").strip().lower()
    if effect_tag not in prog.CONSUMABLES:
        return _stock_error(
            f"Unknown effect_tag {effect_tag!r} for {name}. A consumable is one of "
            "these three; the engine decides what each one does.",
            list(prog.CONSUMABLES),
        )
    item = Item(
        id=item_id,
        name=name,
        type="consumable",
        value=prog.CONSUMABLES[effect_tag]["price"],
        description=description,
        stackable=True,
        quantity=count,
        effect_tag=effect_tag,
    )
    return {"item": item}


@tool
def stock_merchant(npc_id: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Fill a merchant's stock with real, priced gear.

    Each entry is {"kind": ..., "tier"/"effect_tag": ..., "name": ..., "count": 1}:
      - kind "weapon", "armor" or "trinket" -> requires "tier": one of
        "crude", "common", "fine", "superior", "master", "legendary"
      - kind "consumable" -> requires "effect_tag": "heal_light", "heal_strong"
        or "surge"
    "name" is yours (flavour: "Ashwood Blade", "Harbor Mail", "Dockhand's Draught").
    Prices, damage, and protection are set by the engine from the tier — do not
    invent numbers, and do not tell the player a price you have not read back
    from get_inventory.

    Stock what the merchant would plausibly have. A village smith carries crude
    and common; only a capital armourer carries master. Legendary is never for
    sale — it is a reward.

    Args:
        npc_id: The merchant NPC's ID.
        entries: The stock lines, as described above.

    Returns:
        Dictionary with what was stocked and the price of each piece.
    """
    if not isinstance(entries, list) or not entries:
        return _stock_error(
            "Pass at least one stock entry, e.g. "
            '[{"kind": "weapon", "tier": "common", "name": "Ashwood Blade"}].',
            list(_STOCK_KINDS),
        )

    # Validate everything BEFORE writing anything: a half-stocked shelf with an
    # error message is a shop the DM cannot reason about.
    prepared: list[Item] = []
    for entry in entries:
        outcome = _prepare_entry(entry)
        if "error" in outcome:
            return outcome
        prepared.append(outcome["item"])

    with get_session() as session:
        npc = session.get(NPC, npc_id)
        if not npc:
            return {"error": "NPC not found"}

        # Imported here (not at module scope) so the item helpers stay in one file
        from src.tools.world_write.items import _validate_inventory

        inventory = _validate_inventory(npc.inventory_notable)
        stocked = []
        for item in prepared:
            for existing in inventory:
                if existing.get("id") == item.id and existing.get("stackable", True):
                    existing["quantity"] = existing.get("quantity", 1) + item.quantity
                    break
            else:
                inventory.append(item.to_dict())
            stocked.append({"name": item.name, "price": item.value, "quantity": item.quantity})

        npc.inventory_notable = inventory
        npc_name = npc.name
        session.commit()

    return {
        "success": True,
        "npc": npc_name,
        "stocked": stocked,
        "instruction": (
            "These prices are the engine's. Quote them as written and sell with "
            "transfer_item(is_purchase=True) — never invent a discount or a markup."
        ),
    }

"""Tools for item and inventory management.

Prices for anything mechanical (tiered gear, potions) come from
`progression.catalog_price`, never from the `value` an LLM wrote: the DM picks
the WORD ("a fine blade") and the engine picks the coin. Plain stuff — a
letter, a lucky stone, a crate of turnips — keeps its authored value.
"""

from typing import Any

from strands import tool

from src.models import (
    Item,
    NPC,
    Player,
    get_session,
)
from src.services import perception
from src.services import progression as prog
from src.services import standing


def _mechanical_annotations(owner, entry: dict[str, Any], is_player: bool) -> dict[str, Any]:
    """The mechanical keys `get_inventory` adds to an entry.

    A player gets the full reading (equipped/equippable too); an NPC's shelf
    gets slot/tier/effect_tag/price, which is what the DM needs to quote a
    true price without inventing one.
    """
    if is_player:
        return prog.annotate_item(owner, entry)
    mech = prog.mechanize_item(entry) or {}
    price = prog.catalog_price(entry)
    return {
        "slot": mech.get("slot"),
        "tier": prog.tier_label(mech.get("tier")),
        "effect_tag": mech.get("effect_tag"),
        "price": price if price is not None else entry.get("value", 0),
    }


def _validate_inventory(raw: list) -> list[dict[str, Any]]:
    """Coerce raw inventory entries to validated Item dicts.

    Legacy entries may be bare strings — convert them rather than dropping
    them so items are never silently lost.
    """
    validated = []
    for item_data in raw or []:
        try:
            if isinstance(item_data, str):
                item = Item(
                    id=item_data.lower().replace(" ", "_"),
                    name=item_data,
                    type="misc",
                )
            else:
                item = Item.from_dict(item_data)
            validated.append(item.to_dict())
        except Exception:
            # Truly malformed entry — keep a recognizable stub instead of losing it
            validated.append(Item(id="unknown_item", name=str(item_data), type="misc").to_dict())
    return validated


@tool
def create_item_template(
    item_id: str,
    name: str,
    item_type: str,
    value: int,
    description: str,
    effects: dict[str, Any] | None = None,
    stackable: bool = True,
    slot: str | None = None,
    tier: str | None = None,
    effect_tag: str | None = None,
    visual_desc: str | None = None,
    attire: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an item template that can be used throughout the game.

    Args:
        item_id: Unique identifier for this item type (e.g., "health_potion").
        name: Display name of the item.
        item_type: Type of item (consumable, weapon, armor, quest_item, misc).
        value: Base value in currency. Ignored for gear and potions — the
            engine prices those from their tier.
        description: Description of the item.
        effects: Dictionary of effects (e.g., {"heal": 30, "buff_strength": 5}).
        stackable: Whether multiple can stack in one inventory slot.
        slot: If this is gear: "weapon", "armor" or "trinket". Requires tier.
        tier: Gear quality: "crude", "common", "fine", "superior", "master",
            "legendary". Only meaningful with slot.
        effect_tag: If this is a potion or draught: "heal_light", "heal_strong"
            or "surge". The engine owns what it does.
        visual_desc: One short concrete phrase describing what this looks like
            when worn or held ("a soot-grey hood that swallows the face").
        attire: ARMOR ONLY. How this garment makes its wearer READ to strangers:
            {"style": "noble"|"fine"|"common"|"rough"|"filthy",
             "uniform_faction_id": "<faction id>" or null,
             "concealing": true|false}
            Set uniform_faction_id when the piece is a faction's recognisable
            colours — strangers will then treat the wearer as that faction,
            with everything that implies. Set concealing for hoods, masks and
            veils: strangers cannot identify the wearer at all, and people who
            know them have to work for it. The engine owns what each word does.

    Returns:
        Dictionary with the validated item template.
    """
    # `attire` is armor-only; anywhere else it is dropped with a log line
    mech = prog.mechanize_item({"slot": slot, "tier": tier, "type": item_type}) or {}

    # Create and validate using Pydantic model
    item = Item(
        id=item_id,
        name=name,
        type=item_type,
        value=value,
        description=description,
        effects=effects or {},
        stackable=stackable,
        quantity=1,  # Default quantity for template
        slot=slot,
        tier=tier,
        effect_tag=effect_tag,
        visual_desc=visual_desc,
        attire=perception.normalize_attire(attire, slot=mech.get("slot")),
    )

    # Anything mechanical is priced by the table, whatever `value` said
    price = prog.catalog_price(item.to_dict())
    if price is not None:
        item.value = price

    return {"success": True, "item": item.to_dict()}


@tool
def get_inventory(owner_id: str, owner_type: str = "player", for_player_id: str = "") -> dict[str, Any]:
    """Get the inventory of a player or NPC.

    Each item carries its `price` — the true, engine-owned number. Quote THAT
    when you sell something; never invent a price. For a merchant's shelf,
    pass `for_player_id` and the prices already carry what this merchant
    thinks of that player (standing and disposition).

    Args:
        owner_id: The ID of the owner.
        owner_type: Type of owner ("player" or "npc").
        for_player_id: When reading an NPC's shelf: the player who is buying.

    Returns:
        Dictionary with inventory items and currency.
    """
    with get_session() as session:
        if owner_type == "player":
            owner = session.get(Player, owner_id)
            if not owner:
                return {"error": "Player not found"}
            is_player = True
            raw = owner.inventory
            currency = owner.currency
        elif owner_type == "npc":
            owner = session.get(NPC, owner_id)
            if not owner:
                return {"error": "NPC not found"}
            is_player = False
            raw = owner.inventory_notable
            currency = getattr(owner, 'currency', None)
        else:
            return {"error": "Invalid owner_type"}

        inventory = _validate_inventory(raw)
        buyer = session.get(Player, for_player_id) if (not is_player and for_player_id) else None
        mul, reason = standing.price_multiplier(session, buyer, owner) if buyer is not None else (1.0, "")
        for entry in inventory:
            entry.update(_mechanical_annotations(owner, entry, is_player))
            if buyer is not None:
                entry["price"] = standing.buy_price(standing.base_price(entry), mul)

        result: dict[str, Any] = {"inventory": inventory, "currency": currency}
        if buyer is not None and reason:
            result["pricing_note"] = f"Prices already reflect that the player is {reason}."
        return result


@tool
def adjust_currency(owner_id: str, amount: int, owner_type: str = "player") -> dict[str, Any]:
    """Add or remove currency from a player or NPC.

    Args:
        owner_id: The ID of the owner.
        amount: Amount to add (positive) or remove (negative).
        owner_type: Type of owner ("player" or "npc").

    Returns:
        Dictionary with new currency amount.
    """
    with get_session() as session:
        if owner_type == "player":
            owner = session.get(Player, owner_id)
            if not owner:
                return {"error": "Player not found"}
            owner.currency = max(0, owner.currency + amount)
            session.commit()
            return {"success": True, "new_currency": owner.currency}
        elif owner_type == "npc":
            owner = session.get(NPC, owner_id)
            if not owner:
                return {"error": "NPC not found"}
            purse = standing.npc_purse(owner)
            if purse is None:
                # A bottomless till: coin comes and goes without a number
                return {"success": True, "new_currency": None, "bottomless": True}
            owner.currency = max(0, purse + amount)
            session.commit()
            return {"success": True, "new_currency": owner.currency}
        else:
            return {"error": "Invalid owner_type"}


@tool
def transfer_item(
    from_id: str,
    to_id: str,
    item_id: str,
    quantity: int = 1,
    from_type: str = "player",
    to_type: str = "player",
    is_purchase: bool = False,
) -> dict[str, Any]:
    """Transfer items between inventories (give, trade, buy, sell).

    Args:
        from_id: Source owner ID.
        to_id: Destination owner ID.
        item_id: The item template ID.
        quantity: How many to transfer.
        from_type: Source type ("player" or "npc").
        to_type: Destination type ("player" or "npc").
        is_purchase: If True, charge the recipient. Gear and potions are
            charged at the engine's catalog price, not at any price you name.

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        # Get source
        if from_type == "player":
            source = session.get(Player, from_id)
            source_inv_field = "inventory"
        else:
            source = session.get(NPC, from_id)
            source_inv_field = "inventory_notable"

        if not source:
            return {"error": "Source not found"}

        # Get destination
        if to_type == "player":
            dest = session.get(Player, to_id)
            dest_inv_field = "inventory"
        else:
            dest = session.get(NPC, to_id)
            dest_inv_field = "inventory_notable"

        if not dest:
            return {"error": "Destination not found"}

        # Get inventories
        source_inv_raw = getattr(source, source_inv_field) or []
        dest_inv_raw = getattr(dest, dest_inv_field) or []

        # Parse inventories as Item objects
        source_inv = [Item.from_dict(item) for item in _validate_inventory(source_inv_raw)]
        dest_inv = [Item.from_dict(item) for item in _validate_inventory(dest_inv_raw)]

        # Find item in source
        source_item = None
        source_item_idx = None
        for idx, item in enumerate(source_inv):
            if item.id == item_id:
                source_item = item
                source_item_idx = idx
                break

        if not source_item:
            return {"error": f"Item {item_id} not found in source inventory"}

        if source_item.quantity < quantity:
            return {"error": "Not enough quantity"}

        # Mechanical items are priced by the table; plain stuff keeps the
        # value whoever authored it wrote. Buying from a merchant costs what
        # that merchant thinks of the player; selling to one pays the
        # buyback fraction (src/services/standing.py owns both numbers).
        base = standing.base_price(source_item.to_dict())
        if is_purchase and to_type == "player" and from_type == "npc":
            mul, _ = standing.price_multiplier(session, dest, source)
            unit_price = standing.buy_price(base, mul)
        elif is_purchase and to_type == "npc" and from_type == "player":
            unit_price = standing.sell_price(base)
        else:
            unit_price = base

        # Handle purchase
        total_cost = 0
        if is_purchase:
            total_cost = unit_price * quantity
            if to_type == "player":
                if dest.currency < total_cost:
                    return {"error": "Not enough currency"}
                dest.currency -= total_cost
                if from_type == "player":
                    source.currency += total_cost
                elif from_type == "npc":
                    purse = standing.npc_purse(source)
                    if purse is not None:
                        source.currency = purse + total_cost
            elif to_type == "npc":
                # THE sale path: the merchant pays the player
                purse = standing.npc_purse(dest)
                if purse is not None:
                    if purse < total_cost:
                        return {"error": "NPC cannot afford that", "npc_currency": purse, "price": total_cost}
                    dest.currency = purse - total_cost
                if from_type == "player":
                    source.currency = (source.currency or 0) + total_cost

        # Remove from source
        source_item.quantity -= quantity
        if source_item.quantity <= 0:
            source_inv.pop(source_item_idx)

        # Add to destination
        dest_item_idx = None
        for idx, item in enumerate(dest_inv):
            if item.id == item_id and item.stackable:
                dest_item_idx = idx
                break

        if dest_item_idx is not None:
            dest_inv[dest_item_idx].quantity += quantity
        else:
            # Create new item in dest inventory — the mechanical fields travel
            # with it (a bought sword that arrives untiered is a bought stick),
            # stamped with the catalog price. Appearance travels too: a bought
            # hood that arrives with no `attire` stops concealing anything.
            new_item = Item(
                id=source_item.id,
                name=source_item.name,
                type=source_item.type,
                value=base,
                description=source_item.description,
                effects=source_item.effects,
                stackable=source_item.stackable,
                quantity=quantity,
                slot=source_item.slot,
                tier=source_item.tier,
                effect_tag=source_item.effect_tag,
                visual_desc=source_item.visual_desc,
                attire=source_item.attire,
            )
            dest_inv.append(new_item)

        # Convert back to dicts and update inventories
        setattr(source, source_inv_field, [item.to_dict() for item in source_inv])
        setattr(dest, dest_inv_field, [item.to_dict() for item in dest_inv])

        # A sword that sits in the bag doing nothing is a bug report — and a
        # bought one strictly better than what they wear goes on (counter
        # and @buy alike: progression.auto_equip)
        equipped = None
        if to_type == "player":
            auto = prog.auto_equip(session, dest, source_item.id,
                                   upgrade=is_purchase and from_type == "npc")
            if auto:
                equipped = auto.get("item_name")

        session.commit()

        result = {
            "success": True,
            "item_id": item_id,
            "quantity": quantity,
            "cost": total_cost if is_purchase else 0,
            "unit_price": unit_price if is_purchase else 0,
        }
        if equipped:
            result["auto_equipped"] = equipped
        item_name = source_item.name

    # Receiving completes an "obtain"; handing over completes a "deliver"
    from src.services import objectives as objectives_mod
    ticked: list = []
    if to_type == "player":
        ticked = objectives_mod.note_items_gained(to_id, [item_name])
    elif from_type == "player" and to_type == "npc":
        ticked = objectives_mod.note_item_given(from_id, to_id, item_name)
    if ticked:
        result["objectives_done"] = objectives_mod.done_lines(ticked)
    return result


@tool
def use_item(user_id: str, item_id: str, user_type: str = "player") -> dict[str, Any]:
    """Use a consumable item and apply its effects.

    Args:
        user_id: The user's ID.
        item_id: The item template ID.
        user_type: Type of user ("player" or "npc").

    Returns:
        Dictionary with effects applied.
    """
    with get_session() as session:
        if user_type == "player":
            user = session.get(Player, user_id)
            inv_field = "inventory"
        else:
            user = session.get(NPC, user_id)
            inv_field = "inventory_notable"

        if not user:
            return {"error": "User not found"}

        # Anything the engine recognises as a draught (an authored effect_tag,
        # or a legacy healing potion the repair rules read as one) resolves
        # through the same code the HUD's Use button calls, so the DM tool and
        # the player's own hand agree on what a potion does.
        if user_type == "player":
            entry = prog.find_item(user, item_id)
            mech = prog.mechanize_item(entry) if entry else None
            if mech and mech.get("effect_tag"):
                result = prog.apply_consumable(session, user, item_id, in_combat=False)
                if "error" in result:
                    return result
                session.commit()
                return {
                    "success": True,
                    "item_used": result["item_name"],
                    "effects": result["effect"],
                    "remaining": result["remaining"],
                }

        inventory_raw = getattr(user, inv_field) or []

        # Parse inventory as Item objects
        inventory = [Item.from_dict(item) for item in _validate_inventory(inventory_raw)]

        # Find item
        item = None
        item_idx = None
        for idx, inv_item in enumerate(inventory):
            if inv_item.id == item_id:
                item = inv_item
                item_idx = idx
                break

        if not item:
            return {"error": "Item not in inventory"}

        effects_applied = {}

        # Apply effects
        if "heal" in item.effects:
            heal_amount = item.effects["heal"]
            # For player, improve health status
            if user_type == "player":
                current = user.health_status
                statuses = ["critical", "badly_hurt", "hurt", "winded", "healthy"]
                current_idx = statuses.index(current) if current in statuses else 0
                new_idx = min(len(statuses) - 1, current_idx + (heal_amount // 20))
                user.health_status = statuses[new_idx]
                effects_applied["health_restored"] = f"{current} -> {statuses[new_idx]}"

        # Remove one from inventory (for consumables)
        if item.type == "consumable":
            item.quantity -= 1
            if item.quantity <= 0:
                inventory.pop(item_idx)

        # Convert back to dicts and save
        setattr(user, inv_field, [item.to_dict() for item in inventory])
        session.commit()

        return {
            "success": True,
            "item_used": item.name,
            "effects": effects_applied,
        }

@tool
def spawn_item_to_user(
    user_id: str,
    item_data: dict[str, Any],
    quantity: int = 1,
    user_type: str = "player",
) -> dict[str, Any]:
    """Spawn an item directly into a player's or NPC's inventory.

    Args:
        user_id: The user's ID.
        item_data: Dictionary with item template data. May carry "slot"+"tier"
            (gear) or "effect_tag" (a potion) — the engine then owns its price —
            plus "visual_desc" and, on armor, "attire".
        quantity: How many to spawn.
        user_type: Type of user ("player" or "npc").

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        if user_type == "player":
            user = session.get(Player, user_id)
            inv_field = "inventory"
        else:
            user = session.get(NPC, user_id)
            inv_field = "inventory_notable"

        if not user:
            return {"error": "User not found"}

        inventory_raw = getattr(user, inv_field) or []

        # Parse inventory as Item objects
        inventory = [Item.from_dict(item) for item in _validate_inventory(inventory_raw)]

        # Create new item — mechanical fields travel with it, priced by the
        # table rather than by whatever `value` the template carried
        new_item = Item(
            id=item_data["id"],
            name=item_data["name"],
            type=item_data["type"],
            value=item_data["value"],
            description=item_data["description"],
            effects=item_data.get("effects", {}),
            stackable=item_data.get("stackable", True),
            quantity=quantity,
            slot=item_data.get("slot"),
            tier=item_data.get("tier"),
            effect_tag=item_data.get("effect_tag"),
            visual_desc=item_data.get("visual_desc"),
            attire=perception.normalize_attire(
                item_data.get("attire"),
                slot=(prog.mechanize_item(item_data) or {}).get("slot"),
            ),
        )
        price = prog.catalog_price(new_item.to_dict())
        if price is not None:
            new_item.value = price

        # Add to inventory
        dest_item_idx = None
        for idx, item in enumerate(inventory):
            if item.id == new_item.id and item.stackable:
                dest_item_idx = idx
                break

        if dest_item_idx is not None:
            inventory[dest_item_idx].quantity += quantity
        else:
            inventory.append(new_item)

        # Convert back to dicts and save
        setattr(user, inv_field, [item.to_dict() for item in inventory])

        equipped = None
        if user_type == "player":
            auto = prog.auto_equip(session, user, new_item.id)
            if auto:
                equipped = auto.get("item_name")

        session.commit()

        result = {
            "success": True,
            "item_id": new_item.id,
            "quantity": quantity,
        }
        if equipped:
            result["auto_equipped"] = equipped
        return result
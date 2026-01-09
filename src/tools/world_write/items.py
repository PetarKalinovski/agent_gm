"""Tools for item and inventory management."""

from typing import Any

from strands import tool

from src.models import (
    Item,
    NPC,
    Player,
    get_session,
)


@tool
def create_item_template(
    item_id: str,
    name: str,
    item_type: str,
    value: int,
    description: str,
    effects: dict[str, Any] | None = None,
    stackable: bool = True,
) -> dict[str, Any]:
    """Create an item template that can be used throughout the game.

    Args:
        item_id: Unique identifier for this item type (e.g., "health_potion").
        name: Display name of the item.
        item_type: Type of item (consumable, weapon, armor, quest_item, misc).
        value: Base value in currency.
        description: Description of the item.
        effects: Dictionary of effects (e.g., {"heal": 30, "buff_strength": 5}).
        stackable: Whether multiple can stack in one inventory slot.

    Returns:
        Dictionary with the validated item template.
    """
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
    )

    return {"success": True, "item": item.to_dict()}


@tool
def get_inventory(owner_id: str, owner_type: str = "player") -> dict[str, Any]:
    """Get the inventory of a player or NPC.

    Args:
        owner_id: The ID of the owner.
        owner_type: Type of owner ("player" or "npc").

    Returns:
        Dictionary with inventory items and currency.
    """
    with get_session() as session:
        if owner_type == "player":
            owner = session.get(Player, owner_id)
            if not owner:
                return {"error": "Player not found"}

            # Validate all items in inventory
            validated_inventory = []
            for item_data in (owner.inventory or []):
                try:
                    item = Item.from_dict(item_data)
                    validated_inventory.append(item.to_dict())
                except Exception:
                    # Skip invalid items
                    continue

            return {
                "inventory": validated_inventory,
                "currency": owner.currency,
            }
        elif owner_type == "npc":
            owner = session.get(NPC, owner_id)
            if not owner:
                return {"error": "NPC not found"}

            # Validate all items in inventory
            validated_inventory = []
            for item_data in (owner.inventory_notable or []):
                try:
                    item = Item.from_dict(item_data)
                    validated_inventory.append(item.to_dict())
                except Exception:
                    # Skip invalid items
                    continue

            return {
                "inventory": validated_inventory,
                "currency": getattr(owner, 'currency', 0),  # Default to 0 if not set
            }
        else:
            return {"error": "Invalid owner_type"}


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
            # For now, NPCs don't have currency field - would need migration
            return {"error": "NPC currency not yet implemented"}
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
        is_purchase: If True, deduct item value from recipient's currency.

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
        source_inv = [Item.from_dict(item) for item in source_inv_raw]
        dest_inv = [Item.from_dict(item) for item in dest_inv_raw]

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

        # Handle purchase
        total_cost = 0
        if is_purchase:
            total_cost = source_item.value * quantity
            if to_type == "player":
                if dest.currency < total_cost:
                    return {"error": "Not enough currency"}
                dest.currency -= total_cost
                if from_type == "player":
                    source.currency += total_cost

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
            # Create new item in dest inventory
            new_item = Item(
                id=source_item.id,
                name=source_item.name,
                type=source_item.type,
                value=source_item.value,
                description=source_item.description,
                effects=source_item.effects,
                stackable=source_item.stackable,
                quantity=quantity,
            )
            dest_inv.append(new_item)

        # Convert back to dicts and update inventories
        setattr(source, source_inv_field, [item.to_dict() for item in source_inv])
        setattr(dest, dest_inv_field, [item.to_dict() for item in dest_inv])
        session.commit()

        return {
            "success": True,
            "item_id": item_id,
            "quantity": quantity,
            "cost": total_cost if is_purchase else 0,
        }


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

        inventory_raw = getattr(user, inv_field) or []

        # Parse inventory as Item objects
        inventory = [Item.from_dict(item) for item in inventory_raw]

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
        item_data: Dictionary with item template data.
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
        inventory = [Item.from_dict(item) for item in inventory_raw]

        # Create new item
        new_item = Item(
            id=item_data["id"],
            name=item_data["name"],
            type=item_data["type"],
            value=item_data["value"],
            description=item_data["description"],
            effects=item_data.get("effects", {}),
            stackable=item_data.get("stackable", True),
            quantity=quantity,
        )

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
        session.commit()

        return {
            "success": True,
            "item_id": new_item.id,
            "quantity": quantity,
        }
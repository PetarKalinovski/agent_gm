"""Tools for player write operations."""

from typing import Any

from strands import tool

from src.models import (
    Item,
    Location,
    Player,
    get_session,
)
from src.services import perception
from src.services import progression as prog
from src.services.travel import check_route, clear_route_for, place_at_arrival_door


def _normalize_inventory(raw: list) -> list[dict[str, Any]]:
    """Coerce an inventory list to structured Item dicts.

    Legacy inventories may contain bare strings; convert them instead of
    dropping them so items are never silently lost.
    """
    normalized = []
    for entry in raw or []:
        if isinstance(entry, str):
            item_id = entry.lower().replace(" ", "_")
            normalized.append(Item(id=item_id, name=entry, type="misc").to_dict())
        elif isinstance(entry, dict):
            # Copy — mutating the dicts loaded from the JSON column in place
            # would make SQLAlchemy see old == new and skip the UPDATE
            normalized.append(dict(entry))
    return normalized


@tool
def move_player(player_id: str, destination_id: str, requirements_met: bool = False) -> dict[str, Any]:
    """Move the player to a new location.

    If the travel route has requirements (a key, a permit, a guide...),
    the move is blocked until you confirm the player satisfies them by
    calling again with requirements_met=True.

    Args:
        player_id: The player's ID.
        destination_id: The destination location ID.
        requirements_met: Set True only after verifying the player satisfies
            the route's requirements (check their inventory/state first).

    Returns:
        Dictionary with result and travel time, or the blocking requirements.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        destination = session.get(Location, destination_id)
        if not destination:
            return {"error": "Destination not found"}

        # One shared route check with the canvas (src/services/travel.py):
        # a direct connection's requirements, the gate-bypass guard that stops
        # a hierarchy hop from walking around a locked gate, and the
        # destination's own dress code (which needs the player row)
        route = check_route(session, player.current_location_id, destination_id, player)
        travel_time = route["travel_time_hours"]
        requirements = route["requirements"]

        if route["blocked"] and not requirements_met:
            if route["reason"] == "gate":
                message = (
                    f"{destination.name} is behind a controlled route: "
                    f"{', '.join(str(r) for r in requirements)}. There is no way "
                    "around it. Check the player's inventory/state; if satisfied, call "
                    "move_player again with requirements_met=True. Otherwise narrate "
                    "why they can't get through."
                )
            elif route["reason"] == "dress":
                message = (
                    f"{destination.name} will not take them dressed as they are: "
                    f"{', '.join(str(r) for r in requirements)} "
                    "You decide whether that stands. If they talk their way past, "
                    "bribe the door, borrow a coat, or are simply known here, call "
                    "move_player again with requirements_met=True. Otherwise narrate "
                    "the refusal — and let it cost them something."
                )
            else:
                message = (
                    "This route has requirements the player must satisfy: "
                    f"{', '.join(str(r) for r in requirements)}. "
                    "Check the player's inventory/state; if satisfied, call move_player "
                    "again with requirements_met=True. Otherwise narrate why they can't pass."
                )
            return {
                "blocked": True,
                "destination": destination.name,
                "requirements": requirements,
                "message": message,
            }

        origin_id = player.current_location_id
        # The DM waved them through: that road stays open for this player —
        # the canvas can walk it (both ways) without asking again
        if route["blocked"] and requirements_met and route["reason"] in ("route", "gate"):
            clear_route_for(session, origin_id, destination_id, player_id)

        # Update player location and mark destination visited — one transaction
        player.current_location_id = destination_id
        destination.visited = True
        destination.discovered = True
        destination_name = destination.name
        session.commit()

    # Stand them just inside the door they came through (never blocks a move)
    place_at_arrival_door(player_id, destination_id, origin_id)

    return {
        "success": True,
        "destination": destination_name,
        "travel_time_hours": travel_time,
        "reminder": f"Advance time by ~{travel_time}h and describe the new location.",
    }


@tool
def update_player_reputation(player_id: str, faction_id: str, delta: int) -> dict[str, Any]:
    """Update player's reputation with a faction.

    Args:
        player_id: The player's ID.
        faction_id: The faction's ID.
        delta: Change in reputation (-100 to 100).

    Returns:
        Dictionary with new reputation.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        reputation = player.reputation.copy() if player.reputation else {}
        current = reputation.get(faction_id, 50)
        new_score = max(0, min(100, current + delta))
        reputation[faction_id] = new_score
        player.reputation = reputation
        session.commit()

        return {"faction_id": faction_id, "new_score": new_score, "delta": delta}


@tool
def update_player_health(player_id: str, new_status: str, cause: str = "") -> dict[str, Any]:
    """Update player's health status.

    "dead" is final: it fails the player's active quests, records the death
    as a permanent world event (NPCs and future characters will know), and
    ends this character's story. Only use it when the fiction demands it —
    after clear warnings, at critical health, or for plainly fatal choices.

    Args:
        player_id: The player's ID.
        new_status: New health status (healthy, winded, hurt, badly_hurt, critical, dead).
        cause: Required when new_status is "dead" — how they died (e.g.
            "torn apart by the harbor leviathan").

    Returns:
        Dictionary with result.
    """
    valid_statuses = ["healthy", "winded", "hurt", "badly_hurt", "critical", "dead"]
    if new_status not in valid_statuses:
        return {"error": f"Invalid status. Must be one of: {valid_statuses}"}

    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        player.health_status = new_status

        if new_status != "dead":
            session.commit()
            return {"success": True, "new_status": new_status}

        # Death is permanent and the world remembers it
        from src.models import Event, Quest, QuestStatus, WorldClock

        clock = session.query(WorldClock).first()
        day = clock.day if clock else 1
        hour = clock.hour if clock else 8

        failed_quests = []
        active_ids = list(player.active_quests or [])
        if active_ids:
            for quest in session.query(Quest).filter(Quest.id.in_(active_ids)).all():
                quest.status = QuestStatus.FAILED
                failed_quests.append(quest.title)
        player.active_quests = []

        cause_text = cause or "unknown causes"
        session.add(Event(
            name=f"Death of {player.name}",
            description=f"{player.name} died on Day {day}: {cause_text}.",
            event_type="player",
            occurred_day=day,
            occurred_hour=hour,
            locations_involved=[player.current_location_id] if player.current_location_id else [],
            player_visible=True,
            player_witnessed=True,
            narrated_to_player=True,
            consequences=[f"Quests abandoned: {', '.join(failed_quests)}"] if failed_quests else [],
        ))
        session.commit()

        return {
            "success": True,
            "new_status": "dead",
            "player_died": True,
            "quests_failed": failed_quests,
            "instruction": (
                "The character is dead. Deliver a final, definitive epilogue narration "
                "for this character — their last moments and what they leave behind. "
                "Do not offer a way back; the death screen handles what happens next."
            ),
        }


@tool
def add_to_inventory(
    player_id: str,
    item_name: str,
    item_type: str = "misc",
    description: str = "",
    value: int = 0,
    quantity: int = 1,
    effects: dict[str, Any] | None = None,
    slot: str | None = None,
    tier: str | None = None,
    effect_tag: str | None = None,
    visual_desc: str | None = None,
    attire: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add an item to the player's inventory (found, gifted, looted, stolen).

    Stacks with an existing item of the same name if present.

    Args:
        player_id: The player's ID.
        item_name: Display name of the item (e.g., "Rusty Key").
        item_type: One of: consumable, weapon, armor, quest_item, misc.
        description: Short description of the item.
        value: Base value in currency. Ignored for gear and potions — the
            engine prices those from their tier.
        quantity: How many to add.
        effects: Optional effects when used (e.g., {"heal": 30}).
        slot: If this is gear: "weapon", "armor" or "trinket". Requires tier.
        tier: Gear quality — "crude", "common", "fine", "superior", "master",
            "legendary". You pick the word; the engine sets the numbers.
        effect_tag: If this is a potion or draught: "heal_light", "heal_strong"
            or "surge". The engine owns what it does and what it costs.
        visual_desc: One short concrete phrase describing what this looks like
            when worn or held ("a boiled-leather coat with a brass badge").
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
        Dictionary with result.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        inventory = _normalize_inventory(player.inventory)
        item_id = item_name.lower().replace(" ", "_")

        for entry in inventory:
            if entry.get("id") == item_id or entry.get("name", "").lower() == item_name.lower():
                entry["quantity"] = entry.get("quantity", 1) + quantity
                break
        else:
            # `attire` is armor-only. On a sword it is a flavour mistake, not a
            # rules violation: drop it with a log line rather than spend a turn
            # refusing (src/services/perception.normalize_attire).
            mech = prog.mechanize_item({"slot": slot, "tier": tier, "type": item_type}) or {}
            item = Item(
                id=item_id,
                name=item_name,
                type=item_type,
                value=value,
                description=description,
                effects=effects or {},
                quantity=quantity,
                slot=slot,
                tier=tier,
                effect_tag=effect_tag,
                visual_desc=visual_desc,
                attire=perception.normalize_attire(attire, slot=mech.get("slot")),
            )
            # Anything mechanical is priced by the table, not by the fiction
            price = prog.catalog_price(item.to_dict())
            if price is not None:
                item.value = price
            inventory.append(item.to_dict())

        player.inventory = inventory

        # Auto-equip: a sword that sits in the bag doing nothing is a bug
        # report. Only into an EMPTY slot — code never swaps worn gear.
        auto = prog.auto_equip(session, player, item_id)
        session.commit()

        result = {
            "success": True,
            "item": item_name,
            "quantity_added": quantity,
            "inventory_size": len(inventory),
        }
        if auto:
            result["auto_equipped"] = auto.get("item_name")
        return result


@tool
def remove_from_inventory(player_id: str, item_name: str, quantity: int = 1) -> dict[str, Any]:
    """Remove an item from the player's inventory (dropped, destroyed, stolen, consumed).

    Args:
        player_id: The player's ID.
        item_name: Name (or item id) of the item to remove.
        quantity: How many to remove.

    Returns:
        Dictionary with result.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        inventory = _normalize_inventory(player.inventory)
        item_id = item_name.lower().replace(" ", "_")

        for i, entry in enumerate(inventory):
            if entry.get("id") == item_id or entry.get("name", "").lower() == item_name.lower():
                current = entry.get("quantity", 1)
                if current > quantity:
                    entry["quantity"] = current - quantity
                    removed = quantity
                else:
                    inventory.pop(i)
                    removed = current
                player.inventory = inventory
                session.commit()
                return {"success": True, "removed": entry.get("name", item_name), "quantity_removed": removed}

        return {"error": f"'{item_name}' not in inventory"}

"""Inventory: one structured representation, no silent item loss."""

from src.models import Player, get_session
from src.tools.world_write.items import get_inventory
from src.tools.world_write.player import add_to_inventory, remove_from_inventory


def test_add_and_get_inventory_roundtrip(player):
    add_to_inventory(player, "Rusty Key", item_type="quest_item", description="Opens something")
    result = get_inventory(player)
    names = [i["name"] for i in result["inventory"]]
    assert "Rusty Key" in names


def test_add_stacks_same_item(player):
    add_to_inventory(player, "Health Potion", quantity=1)
    add_to_inventory(player, "Health Potion", quantity=2)
    result = get_inventory(player)
    potions = [i for i in result["inventory"] if i["name"] == "Health Potion"]
    assert len(potions) == 1
    assert potions[0]["quantity"] == 3


def test_legacy_string_items_are_not_silently_dropped(player):
    # Simulate a legacy inventory containing bare strings
    with get_session() as session:
        p = session.get(Player, player)
        p.inventory = ["Old Sword", {"id": "coin", "name": "Coin", "type": "misc", "quantity": 5}]
        session.commit()

    result = get_inventory(player)
    names = [i["name"] for i in result["inventory"]]
    assert "Old Sword" in names, "legacy string items must be repaired, not dropped"
    assert "Coin" in names


def test_remove_from_inventory_decrements_then_removes(player):
    add_to_inventory(player, "Arrow", quantity=3)
    remove_from_inventory(player, "Arrow", quantity=2)
    result = get_inventory(player)
    arrows = [i for i in result["inventory"] if i["name"] == "Arrow"]
    assert arrows and arrows[0]["quantity"] == 1

    remove_from_inventory(player, "Arrow", quantity=1)
    result = get_inventory(player)
    assert not [i for i in result["inventory"] if i["name"] == "Arrow"]


def test_remove_missing_item_errors(player):
    result = remove_from_inventory(player, "Nonexistent Thing")
    assert "error" in result

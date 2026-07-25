"""The LLM picks the word; the engine picks the coin.

The failure this file exists to prevent: a DM writing `value=5` on a master
blade, or a merchant quietly stocking a legendary sword. Prices for anything
mechanical come from `progression.TIER_PRICE` / `CONSUMABLES`, and plain
stuff — a letter, a lucky stone — keeps whatever value the fiction gave it.
"""

import pytest

from src.models import Item, NPC, NPCTier, Player, get_session
from src.services import progression as prog
from src.tools.world_read.player import get_player
from src.tools.world_write.items import get_inventory, transfer_item, use_item
from src.tools.world_write.player import add_to_inventory
from src.tools.world_write.progression import stock_merchant


@pytest.fixture()
def merchant(db):
    """A shopkeeper with an empty shelf."""
    with get_session() as session:
        npc = NPC(name="Coldiron", tier=NPCTier.MINOR, profession="armourer")
        session.add(npc)
        session.commit()
        return npc.id


def _npc_inventory(npc_id):
    with get_session() as session:
        return list(session.get(NPC, npc_id).inventory_notable or [])


def _player_row(player_id):
    with get_session() as session:
        p = session.get(Player, player_id)
        return {
            "currency": p.currency,
            "inventory": list(p.inventory or []),
            "equipment": dict(p.equipment or {}),
            "health": p.health_status,
        }


def _set_currency(player_id, amount):
    with get_session() as session:
        session.get(Player, player_id).currency = amount
        session.commit()


def _stock(npc_id, entry):
    """Put one authored item straight on a shelf, bypassing stock_merchant."""
    with get_session() as session:
        npc = session.get(NPC, npc_id)
        npc.inventory_notable = list(npc.inventory_notable or []) + [entry.to_dict()]
        session.commit()


def _item(player_id, name):
    return next(i for i in get_inventory(player_id)["inventory"] if i["name"] == name)


# --- Catalog pricing -------------------------------------------------------


def test_catalog_price_overrides_authored_value(player):
    add_to_inventory(player, "Ashwood Blade", item_type="weapon", value=5,
                     slot="weapon", tier="master")
    blade = _item(player, "Ashwood Blade")
    assert prog.TIER_PRICE["weapon"][4] == 700
    assert blade["value"] == 700, "an authored 5-coin master blade is a 700-coin blade"
    assert blade["price"] == 700


def test_consumable_price_comes_from_the_effect_tag(player):
    add_to_inventory(player, "Dockhand's Draught", item_type="consumable", value=1,
                     effect_tag="heal_strong")
    draught = _item(player, "Dockhand's Draught")
    assert draught["value"] == prog.CONSUMABLES["heal_strong"]["price"] == 70


def test_plain_item_keeps_authored_value(player):
    add_to_inventory(player, "Lucky Stone", item_type="misc", value=12)
    stone = _item(player, "Lucky Stone")
    assert stone["value"] == 12
    assert stone["price"] == 12, "a plain item is worth what the fiction says"
    assert stone["slot"] is None and stone["tier"] is None


def test_purchase_charges_catalog_price(player, merchant):
    _set_currency(player, 500)
    stock_merchant(merchant, [{"kind": "armor", "tier": "fine", "name": "Harbor Mail"}])

    result = transfer_item(merchant, player, "harbor_mail",
                           from_type="npc", to_type="player", is_purchase=True)
    assert result["success"]
    assert result["cost"] == prog.TIER_PRICE["armor"][2] == 150
    assert _player_row(player)["currency"] == 350


def test_purchase_charges_the_table_even_when_the_shelf_lies(player, merchant):
    """A merchant row hand-edited to 3 coins still costs what the tier costs."""
    _set_currency(player, 500)
    _stock(merchant, Item(id="grave_edge", name="Grave Edge", type="weapon",
                          value=3, slot="weapon", tier="fine"))

    result = transfer_item(merchant, player, "grave_edge",
                           from_type="npc", to_type="player", is_purchase=True)
    assert result["cost"] == prog.TIER_PRICE["weapon"][2] == 120
    assert _player_row(player)["currency"] == 380


def test_purchase_blocked_when_short(player, merchant):
    _set_currency(player, 5)
    stock_merchant(merchant, [{"kind": "weapon", "tier": "crude", "name": "Bent Knife"}])

    result = transfer_item(merchant, player, "bent_knife",
                           from_type="npc", to_type="player", is_purchase=True)
    assert result == {"error": "Not enough currency"}
    assert _player_row(player)["currency"] == 5
    assert _npc_inventory(merchant), "a failed purchase leaves the shelf untouched"


def test_plain_purchase_charges_the_authored_value(player, merchant):
    _set_currency(player, 100)
    _stock(merchant, Item(id="tin_whistle", name="Tin Whistle", type="misc", value=12))

    result = transfer_item(merchant, player, "tin_whistle",
                           from_type="npc", to_type="player", is_purchase=True)
    assert result["cost"] == 12
    assert _player_row(player)["currency"] == 88


def test_purchase_carries_the_mechanics_across(player, merchant):
    _set_currency(player, 500)
    stock_merchant(merchant, [{"kind": "weapon", "tier": "common", "name": "Ashwood Blade"}])
    transfer_item(merchant, player, "ashwood_blade",
                  from_type="npc", to_type="player", is_purchase=True)

    blade = _item(player, "Ashwood Blade")
    assert blade["slot"] == "weapon" and blade["tier"] == "common", \
        "a bought sword that arrives untiered is a bought stick"
    assert blade["value"] == prog.TIER_PRICE["weapon"][1] == 40


# --- stock_merchant --------------------------------------------------------


def test_stock_merchant_creates_priced_gear(merchant):
    result = stock_merchant(merchant, [
        {"kind": "weapon", "tier": "common", "name": "Ashwood Blade"},
        {"kind": "consumable", "effect_tag": "heal_light", "name": "Dockhand's Draught", "count": 3},
    ])
    assert result["success"] and result["npc"] == "Coldiron"
    assert result["stocked"] == [
        {"name": "Ashwood Blade", "price": 40, "quantity": 1},
        {"name": "Dockhand's Draught", "price": 25, "quantity": 3},
    ]

    shelf = {i["name"]: i for i in _npc_inventory(merchant)}
    assert shelf["Ashwood Blade"]["slot"] == "weapon"
    assert shelf["Ashwood Blade"]["tier"] == "common"
    assert shelf["Ashwood Blade"]["value"] == 40
    assert shelf["Dockhand's Draught"]["effect_tag"] == "heal_light"
    assert shelf["Dockhand's Draught"]["quantity"] == 3


def test_stock_merchant_prices_show_up_in_get_inventory(merchant):
    stock_merchant(merchant, [{"kind": "trinket", "tier": "fine", "name": "Salt Charm"}])
    listing = get_inventory(merchant, owner_type="npc")["inventory"]
    charm = next(i for i in listing if i["name"] == "Salt Charm")
    assert charm["price"] == prog.TIER_PRICE["trinket"][2] == 160


def test_stock_merchant_rejects_legendary(merchant):
    result = stock_merchant(merchant, [
        {"kind": "weapon", "tier": "legendary", "name": "The Ashen Answer"},
    ])
    assert "error" in result and "legendary" in result["error"].lower()
    assert "legendary" not in result["valid"], "the error must name only the legal words"
    assert _npc_inventory(merchant) == [], "a refused shelf stays empty"


def test_stock_merchant_rejects_unknown_tier(merchant):
    result = stock_merchant(merchant, [
        {"kind": "weapon", "tier": "mythic", "name": "Starbiter"},
    ])
    assert "error" in result and "mythic" in result["error"]
    assert result["valid"] == list(prog.TIER_LABELS)


def test_stock_merchant_rejects_unknown_effect_tag(merchant):
    result = stock_merchant(merchant, [
        {"kind": "consumable", "effect_tag": "invisibility", "name": "Ghost Water"},
    ])
    assert "error" in result
    assert result["valid"] == list(prog.CONSUMABLES)


def test_stock_merchant_is_all_or_nothing(merchant):
    result = stock_merchant(merchant, [
        {"kind": "weapon", "tier": "common", "name": "Ashwood Blade"},
        {"kind": "weapon", "tier": "legendary", "name": "The Ashen Answer"},
    ])
    assert "error" in result
    assert _npc_inventory(merchant) == [], \
        "one bad line must not leave half a shelf the DM cannot reason about"


def test_stock_merchant_needs_a_real_npc(db):
    assert "error" in stock_merchant("nobody", [
        {"kind": "weapon", "tier": "crude", "name": "Bent Knife"},
    ])


# --- Auto-equip ------------------------------------------------------------


def test_auto_equip_fills_an_empty_slot(player):
    result = add_to_inventory(player, "Ashwood Blade", item_type="weapon",
                              slot="weapon", tier="common")
    assert result.get("auto_equipped") == "Ashwood Blade"
    assert _player_row(player)["equipment"]["weapon"] == "ashwood_blade"
    assert _item(player, "Ashwood Blade")["equipped"] is True


def test_auto_equip_does_not_replace_worn_gear(player):
    add_to_inventory(player, "Bent Knife", item_type="weapon", slot="weapon", tier="crude")
    result = add_to_inventory(player, "Ashwood Blade", item_type="weapon",
                              slot="weapon", tier="common")
    assert "auto_equipped" not in result
    assert _player_row(player)["equipment"]["weapon"] == "bent_knife"


def test_auto_equip_respects_the_level_gate(player):
    result = add_to_inventory(player, "Kingsteel Plate", item_type="armor",
                              slot="armor", tier="master")
    assert "auto_equipped" not in result
    assert _player_row(player)["equipment"].get("armor") is None
    assert _item(player, "Kingsteel Plate")["equippable"] is False


def test_purchase_auto_equips(player, merchant):
    _set_currency(player, 500)
    stock_merchant(merchant, [{"kind": "armor", "tier": "common", "name": "Harbor Mail"}])
    result = transfer_item(merchant, player, "harbor_mail",
                           from_type="npc", to_type="player", is_purchase=True)
    assert result.get("auto_equipped") == "Harbor Mail"


# --- What the DM is allowed to read ---------------------------------------


def test_get_player_shows_level_and_gear_but_never_xp(player):
    with get_session() as session:
        p = session.get(Player, player)
        p.level, p.xp = 5, 800
        session.commit()
    add_to_inventory(player, "Ashwood Blade", item_type="weapon", slot="weapon", tier="fine")

    view = get_player(player)
    assert view["level"] == 5
    assert view["equipped"]["weapon"] == "Ashwood Blade (fine)"
    assert "xp" not in view, "the DM has no business narrating an experience total"


def test_get_inventory_annotates_every_entry(player):
    add_to_inventory(player, "Lucky Stone", item_type="misc", value=12)
    stone = _item(player, "Lucky Stone")
    for key in ("slot", "tier", "effect_tag", "equipped", "equippable", "price"):
        assert key in stone


# --- use_item ---------------------------------------------------------------


def test_use_item_delegates_a_draught_to_the_engine(player):
    with get_session() as session:
        session.get(Player, player).health_status = "hurt"
        session.commit()
    add_to_inventory(player, "Harbor Draught", item_type="consumable",
                     effect_tag="heal_light", quantity=2)

    result = use_item(player, "harbor_draught")
    assert result["success"]
    assert result["effects"]["health"] == "hurt -> winded"
    assert result["remaining"] == 1
    assert _player_row(player)["health"] == "winded"


def test_use_item_leaves_plain_items_to_the_legacy_path(player):
    add_to_inventory(player, "Rusty Key", item_type="quest_item")
    result = use_item(player, "rusty_key")
    assert result["success"] and result["effects"] == {}

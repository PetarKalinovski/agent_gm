"""The counter: buying at the merchant's price, selling for what they pay."""

import asyncio

import pytest

from src.models import Faction, Location, LocationType, NPC, NPCRelationship, Player, get_session
from src.services import standing
from src.tools.world_write.items import get_inventory, transfer_item
from src.tools.world_write.player import add_to_inventory
from src.tools.world_write.progression import stock_merchant
from src.web import pending_actions, server


@pytest.fixture()
def shop(db, tmp_path, monkeypatch):
    """A merchant with real stock, standing at the player's location."""
    monkeypatch.setattr(server, "get_active_db_path", lambda: tmp_path / "test.db")
    with get_session() as session:
        guild = Faction(name="Muckers' Guild", ideology="")
        session.add(guild)
        session.flush()
        square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True)
        session.add(square)
        session.flush()
        sorrel = NPC(name="Sorrel Tack", current_location_id=square.id, faction_id=guild.id)
        session.add(sorrel)
        player = Player(name="Rook", current_location_id=square.id, currency=100)
        session.add(player)
        session.commit()
        ids = {"player": player.id, "npc": sorrel.id, "square": square.id, "guild": guild.id}
    stock_merchant(ids["npc"], [{"kind": "weapon", "tier": "common", "name": "Iron Sword"},
                               {"kind": "consumable", "effect_tag": "heal_light", "name": "Healing Draught"}])
    return ids


def _stock(npc_id, player_id):
    return {i["name"]: i for i in get_inventory(npc_id, "npc", for_player_id=player_id)["inventory"]}


def _player(player_id):
    with get_session() as session:
        p = session.get(Player, player_id)
        return {"coin": p.currency, "items": {i["name"]: i for i in p.inventory or []}}


def _trade(npc_id, player_id, item_id, *, selling):
    request = server.ShopTradeRequest(player_id=player_id, item_id=item_id, quantity=1)
    result = asyncio.run(server._shop_trade(npc_id, request, selling=selling))
    if hasattr(result, "status_code"):
        import json
        return result.status_code, json.loads(result.body)
    return 200, result


def test_the_three_price_readers_agree(shop):
    with get_session() as session:
        player = session.get(Player, shop["player"])
        npc = session.get(NPC, shop["npc"])
        view = server._shop_view(session, player, npc)
    listed = {i["name"]: i["price"] for i in view["stock"]}
    tool = {name: i["price"] for name, i in _stock(shop["npc"], shop["player"]).items()}
    assert listed == tool
    status, data = _trade(shop["npc"], shop["player"], "iron_sword", selling=False)
    assert status == 200 and data["cost"] == listed["Iron Sword"]
    assert _player(shop["player"])["coin"] == 100 - listed["Iron Sword"]


def test_selling_finally_pays_the_buyback_fraction(shop):
    add_to_inventory(shop["player"], "Copper Ring", item_type="misc", value=20)
    before = _player(shop["player"])["coin"]
    status, data = _trade(shop["npc"], shop["player"], "copper_ring", selling=True)
    assert status == 200 and data["cost"] == standing.sell_price(20) == 8
    after = _player(shop["player"])
    assert after["coin"] == before + 8 and "Copper Ring" not in after["items"]
    with get_session() as session:
        shelf = {i["name"]: i for i in session.get(NPC, shop["npc"]).inventory_notable}
        assert shelf["Copper Ring"]["value"] == 20, "the item keeps its worth on the shelf"
    block = pending_actions.format_pending_actions(pending_actions.drain(shop["player"]))
    assert "Traded: They sold Copper Ring to Sorrel Tack for 8 coin" in block


def test_worn_and_quest_items_are_not_for_sale(shop):
    add_to_inventory(shop["player"], "Bone Whistle", item_type="quest_item")
    status, data = _trade(shop["npc"], shop["player"], "bone_whistle", selling=True)
    assert status == 409 and data["reason"] == "quest_item"
    _trade(shop["npc"], shop["player"], "iron_sword", selling=False)   # auto-equips into the empty slot
    status, data = _trade(shop["npc"], shop["player"], "iron_sword", selling=True)
    assert status == 409 and data["reason"] == "equipped"


def test_a_real_purse_runs_out_and_null_is_bottomless(shop):
    add_to_inventory(shop["player"], "Gold Idol", item_type="misc", value=1000, quantity=2)
    with get_session() as session:
        session.get(NPC, shop["npc"]).currency = 500
        session.commit()
    status, data = _trade(shop["npc"], shop["player"], "gold_idol", selling=True)
    assert status == 200 and data["cost"] == 400
    status, data = _trade(shop["npc"], shop["player"], "gold_idol", selling=True)
    assert status == 409 and data["reason"] == "coin"
    with get_session() as session:
        npc = session.get(NPC, shop["npc"])
        assert npc.currency == 100
        npc.currency = None
        session.commit()
    status, data = _trade(shop["npc"], shop["player"], "gold_idol", selling=True)
    assert status == 200, "NULL till never runs dry"


def test_standing_moves_the_price_and_a_hostile_merchant_refuses(shop):
    base = _stock(shop["npc"], shop["player"])["Iron Sword"]["price"]
    with get_session() as session:
        session.get(Player, shop["player"]).reputation = {shop["guild"]: 90}
        session.commit()
    assert _stock(shop["npc"], shop["player"])["Iron Sword"]["price"] == standing.buy_price(base, 0.8)
    with get_session() as session:
        session.add(NPCRelationship(npc_id=shop["npc"], player_id=shop["player"],
                                    current_disposition="hostile", trust_level=5))
        session.commit()
    with get_session() as session:
        _, _, err = server._shop_guard(session, shop["player"], shop["npc"], need_stock=True)
    assert err is not None and err.status_code == 409


def test_buying_charges_the_standing_price_end_to_end(shop):
    with get_session() as session:
        session.get(Player, shop["player"]).reputation = {shop["guild"]: 5}
        session.commit()
    price = _stock(shop["npc"], shop["player"])["Healing Draught"]["price"]
    result = transfer_item(shop["npc"], shop["player"], "healing_draught", 1,
                           from_type="npc", to_type="player", is_purchase=True)
    assert result["cost"] == price == result["unit_price"]
    assert _player(shop["player"])["coin"] == 100 - price


def test_a_bought_upgrade_goes_on_at_the_counter_and_the_dm_hears_it(shop):
    """fleet9-tamsin: a bought blade sat in the bag behind the knife she wore.
    The counter puts a strictly better one in her hand, and the next reply's
    context says so — the prose never reaches into the bag for it."""
    add_to_inventory(shop["player"], "Worn Knife", item_type="weapon", slot="weapon", tier="crude")
    status, data = _trade(shop["npc"], shop["player"], "iron_sword", selling=False)
    assert status == 200 and data["auto_equipped"] == "Iron Sword"
    assert data["state"]["equipment"]["weapon"]["name"] == "Iron Sword"
    block = pending_actions.format_pending_actions(pending_actions.drain(shop["player"]))
    assert "They bought Iron Sword from Sorrel Tack" in block
    assert "They immediately wield the Iron Sword they just got." in block

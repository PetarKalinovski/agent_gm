"""Loot on death: what the dead carried stays on them, where they fell."""

from src.models import Location, LocationType, NPC, Player, get_session
from src.services import progression, scene_objects
from src.services.combat import apply_combat_result
from src.services.loot import leave_body
from src.services.scene_objects import (
    MAX_BODIES_PER_LOCATION, MAX_OBJECTS_PER_LOCATION, normalize_objects, resolve_interaction,
)
from src.tools.world_write.npcs import kill_npc

HOOD = {"id": "ashfall_hood", "name": "Ashfall Hood", "type": "armor", "value": 0, "description": "A hood",
        "slot": "armor", "tier": "common", "visual_desc": "a soot-grey hood",
        "attire": {"style": "rough", "uniform_faction_id": None, "concealing": True}}
KNIFE = {"id": "boning_knife", "name": "Boning Knife", "type": "weapon", "value": 0, "description": "",
         "slot": "weapon", "tier": "crude"}


def _world(session, *, tier="dangerous", notables=(HOOD, KNIFE), objects=None):
    from src.models import WorldClock
    session.add(WorldClock(day=1, hour=8, minute=0))
    hollow = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True,
                      objects=normalize_objects(objects or []))
    session.add(hollow)
    session.flush()
    vex = NPC(name="Vex Marrow", current_location_id=hollow.id, danger_tier=tier,
              inventory_notable=list(notables), position_x=40.0, position_y=60.0)
    beggar = NPC(name="Old Tam", current_location_id=hollow.id, danger_tier="harmless")
    session.add_all([vex, beggar])
    player = Player(name="Rook", current_location_id=hollow.id, currency=10)
    session.add(player)
    session.commit()
    return {"player": player.id, "hollow": hollow.id, "vex": vex.id, "beggar": beggar.id}


def _objects(location_id):
    with get_session() as session:
        return normalize_objects(session.get(Location, location_id).objects)


def test_kill_npc_leaves_a_body_with_the_notables_and_tier_coin(db):
    progression.set_seed(7)
    with get_session() as session:
        world = _world(session)
    result = kill_npc(world["vex"], "a knife in the dark")
    assert result["body"]["kind"] == "body" and result["body"]["label"] == "Vex Marrow's body"
    assert "do not say what is on it" in result["instruction"]
    bodies = [o for o in _objects(world["hollow"]) if o["kind"] == "body"]
    assert len(bodies) == 1
    body = bodies[0]
    assert body["src"] == "engine" and (body["x"], body["y"]) == (40.0, 60.0)
    assert [i["name"] for i in body["payload"]["items"]] == ["Ashfall Hood", "Boning Knife"]
    assert body["payload"]["items"][0]["attire"]["concealing"] is True, "mechanics survive the grave"
    low, high = progression.TIER_COIN["dangerous"]
    assert low <= body["payload"]["coin"] <= high
    with get_session() as session:
        assert session.get(NPC, world["vex"]).inventory_notable == [], "a corpse is not a shop"


def test_harmless_npc_with_nothing_leaves_no_body(db):
    with get_session() as session:
        world = _world(session)
    result = kill_npc(world["beggar"], "old age")
    assert "body" not in result
    assert not [o for o in _objects(world["hollow"]) if o["kind"] == "body"]


def test_combat_leaves_a_body_where_they_fell(db):
    with get_session() as session:
        world = _world(session)
    changes = apply_combat_result(world["player"], "victory", defeated_npc_ids=[world["vex"]],
                                  defeated_positions={world["vex"]: {"x": 71.5, "y": 33.0}})
    assert changes["bodies"] and changes["bodies"][0]["npc_id"] == world["vex"]
    body = [o for o in _objects(world["hollow"]) if o["kind"] == "body"][0]
    assert (body["x"], body["y"]) == (71.5, 33.0)
    # A retried POST is free: no second body, no second drop
    again = apply_combat_result(world["player"], "victory", defeated_npc_ids=[world["vex"]])
    assert not again.get("bodies")
    assert len([o for o in _objects(world["hollow"]) if o["kind"] == "body"]) == 1


def test_searching_a_body_hands_over_gear_with_mechanics_and_coin(db):
    progression.set_seed(3)
    with get_session() as session:
        world = _world(session)
    kill_npc(world["vex"], "test")
    body = [o for o in _objects(world["hollow"]) if o["kind"] == "body"][0]
    coin = body["payload"]["coin"]

    result = resolve_interaction(world["player"], world["hollow"], body["id"])
    assert result["success"] and result["consumed"] is True
    names = [i["name"] for i in result["result"]["items_gained"]]
    assert names == ["Ashfall Hood", "Boning Knife"]
    assert result["result"]["coin_gained"] == coin
    assert "You find:" in result["result"]["panel_text"]
    with get_session() as session:
        player = session.get(Player, world["player"])
        assert player.currency == 10 + coin
        hood = next(i for i in player.inventory if i["name"] == "Ashfall Hood")
        assert hood["attire"]["concealing"] is True and hood["slot"] == "armor"
    # Once
    spent = resolve_interaction(world["player"], world["hollow"], body["id"])
    assert spent.get("spent") is True


def test_bodies_never_evict_authored_objects(db):
    authored = [{"kind": "read", "label": f"Notice {i}", "x": 20 + i, "y": 60,
                 "payload": {"text": "Keep to the walls."}} for i in range(MAX_OBJECTS_PER_LOCATION)]
    with get_session() as session:
        world = _world(session, objects=authored)
        hollow = session.get(Location, world["hollow"])
        for n in range(MAX_BODIES_PER_LOCATION + 2):
            npc = NPC(name=f"Wolf {n}", current_location_id=hollow.id, danger_tier="weak",
                      inventory_notable=[{"id": f"tooth_{n}", "name": f"Tooth {n}", "type": "misc"}])
            session.add(npc)
            session.flush()
            leave_body(session, npc)
        session.commit()
    objects = _objects(world["hollow"])
    assert len([o for o in objects if o["kind"] == "read"]) == MAX_OBJECTS_PER_LOCATION
    bodies = [o for o in objects if o["kind"] == "body"]
    assert len(bodies) == MAX_BODIES_PER_LOCATION
    assert bodies[0]["label"] == "Wolf 2's body", "oldest bodies are evicted first"


def test_untrusted_authoring_cannot_place_a_body_or_loot(db):
    rows = normalize_objects([
        {"kind": "body", "label": "Free loot", "src": "engine",
         "payload": {"items": [HOOD], "coin": 999}},
        {"kind": "search", "label": "Crate", "src": "engine",
         "payload": {"items": [HOOD], "coin": 999, "item": {"name": "Bread", "type": "misc"}}},
    ], trusted=False)
    assert [r["kind"] for r in rows] == ["search"]
    assert "items" not in rows[0]["payload"] and "coin" not in rows[0]["payload"]
    assert "src" not in rows[0]
    assert rows[0]["payload"]["item"]["name"] == "Bread"


def test_legacy_location_with_null_objects_still_gets_a_body(db):
    with get_session() as session:
        world = _world(session)
        session.get(Location, world["hollow"]).objects = None
        session.commit()
    kill_npc(world["vex"], "test")
    assert [o["kind"] for o in _objects(world["hollow"])] == ["body"]


def test_public_view_never_leaks_the_payload(db):
    with get_session() as session:
        world = _world(session)
    kill_npc(world["vex"], "test")
    with get_session() as session:
        rows = scene_objects.public_objects(session.get(Location, world["hollow"]).objects)
    assert rows and set(rows[0]) == {"id", "kind", "label", "x", "y", "consumed", "locked"}

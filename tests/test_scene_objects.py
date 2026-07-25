"""Scene objects: the model writes words, the engine owns everything else."""

from src.models import Location, LocationType, Player, get_session
from src.services.scene_objects import (
    MAX_OBJECTS_PER_LOCATION,
    normalize_objects,
    public_objects,
    resolve_interaction,
)


def _scene(objects) -> tuple[str, str]:
    """A player standing in a location carrying the given objects."""
    with get_session() as session:
        location = Location(
            name="Market Square", type=LocationType.DISTRICT,
            objects=normalize_objects(objects),
        )
        session.add(location)
        session.flush()
        player = Player(name="Tester", current_location_id=location.id)
        session.add(player)
        session.commit()
        return player.id, location.id


def _inventory(player_id: str) -> list[dict]:
    with get_session() as session:
        return list(session.get(Player, player_id).inventory or [])


def _stored_objects(location_id: str) -> list[dict]:
    with get_session() as session:
        return list(session.get(Location, location_id).objects or [])


# --- normalize_objects ------------------------------------------------------

def test_normalize_drops_unknown_kinds():
    objects = normalize_objects([
        {"kind": "smash", "label": "Window"},
        {"kind": "read", "label": "Notice", "payload": {"text": "Bounty stands at the pier."}},
        "not a dict",
        None,
    ])

    assert [o["kind"] for o in objects] == ["read"]
    assert objects[0]["label"] == "Notice"


def test_normalize_clamps_and_caps():
    objects = normalize_objects([
        {"kind": "take", "label": f"Crate {i}", "x": -40, "y": 480,
         "payload": {"item": {"name": f"Thing {i}", "type": "misc"}}}
        for i in range(7)
    ])

    assert len(objects) == MAX_OBJECTS_PER_LOCATION
    for obj in objects:
        assert obj["x"] == 10.0 and obj["y"] == 90.0


def test_normalize_strips_effects_and_mechanical_keys():
    objects = normalize_objects([{
        "kind": "take", "label": "Vial",
        "payload": {"item": {
            "name": "Green Vial", "type": "potion", "value": -5, "quantity": 99,
            "effects": {"heal": 30}, "slot": "trinket", "tier": "legendary",
            "effect_tag": "heal_strong", "damage": 12,
        }},
    }])

    item = objects[0]["payload"]["item"]
    assert set(item) == {"name", "type", "description", "value", "quantity"}
    assert item["type"] == "misc", "an invented item type falls back to misc"
    assert item["value"] == 0 and item["quantity"] == 5


def test_normalize_defaults_coordinates_and_consumed():
    objects = normalize_objects([{"kind": "search", "label": "Straw bedding"}])

    assert objects[0]["x"] == 50.0 and objects[0]["y"] == 65.0
    assert objects[0]["consumed"] is False


def test_normalize_truncates_label_and_text():
    objects = normalize_objects([{
        "kind": "read", "label": "L" * 200, "payload": {"text": "T" * 900},
    }])

    assert len(objects[0]["label"]) == 48
    assert len(objects[0]["payload"]["text"]) == 400


def test_normalize_assigns_stable_ids():
    objects = normalize_objects([
        {"kind": "take", "label": "Bundle of oilcloth"},
        {"kind": "take", "label": "Bundle of oilcloth"},
        {"kind": "read", "id": "notice", "label": "Notice"},
    ])

    ids = [o["id"] for o in objects]
    assert len(set(ids)) == 3, "generated ids must be unique"
    assert ids[0] == "bundle_of_oilcloth_0"
    assert ids[2] == "notice"


def test_normalize_drops_lock_on_non_open_kinds():
    objects = normalize_objects([
        {"kind": "search", "label": "Woodpile", "payload": {"lock": "a brass key"}},
        {"kind": "open", "label": "Strongbox", "payload": {"lock": "a brass key"}},
    ])

    assert "lock" not in objects[0]["payload"]
    assert objects[1]["payload"]["lock"] == "a brass key"


def test_normalize_preserves_consumed_flag():
    objects = normalize_objects([
        {"kind": "take", "id": "crate", "label": "Crate", "consumed": True},
    ])

    assert objects[0]["consumed"] is True, "re-normalizing must not un-spend an object"


# --- public_objects ---------------------------------------------------------

def test_public_objects_hides_payload():
    public = public_objects([
        {"kind": "open", "id": "box", "label": "Iron strongbox",
         "payload": {"lock": "a small brass key", "item": {"name": "Ledger"}}},
        {"kind": "take", "id": "cloth", "label": "Bundle of oilcloth",
         "payload": {"item": {"name": "Bundle of oilcloth"}}},
    ])

    assert set(public[0]) == {"id", "kind", "label", "x", "y", "consumed", "locked"}
    assert public[0]["locked"] is True
    assert public[1]["locked"] is False
    assert "a small brass key" not in str(public), "the lock reason is part of the reveal"
    assert "payload" not in str(public)


# --- resolve_interaction ----------------------------------------------------

def test_take_adds_item_and_consumes(db):
    player_id, location_id = _scene([{
        "kind": "take", "id": "cloth", "label": "Bundle of oilcloth",
        "payload": {"item": {"name": "Bundle of oilcloth", "type": "misc", "value": 3}},
    }])

    result = resolve_interaction(player_id, location_id, "cloth")

    assert result["success"] is True and result["consumed"] is True
    assert result["result"]["floater"] == "+ Bundle of oilcloth"
    assert result["result"]["items_gained"] == [{"name": "Bundle of oilcloth", "quantity": 1}]
    assert result["result"]["panel_text"] is None
    assert [i["name"] for i in _inventory(player_id)] == ["Bundle of oilcloth"]
    assert _stored_objects(location_id)[0]["consumed"] is True


def test_take_twice_is_spent(db):
    player_id, location_id = _scene([{
        "kind": "take", "id": "cloth", "label": "Bundle of oilcloth",
        "payload": {"item": {"name": "Bundle of oilcloth"}},
    }])

    resolve_interaction(player_id, location_id, "cloth")
    second = resolve_interaction(player_id, location_id, "cloth")

    assert second == {"success": False, "spent": True, "label": "Bundle of oilcloth"}
    assert len(_inventory(player_id)) == 1
    assert _inventory(player_id)[0]["quantity"] == 1


def test_read_never_consumes(db):
    player_id, location_id = _scene([{
        "kind": "read", "id": "notice", "label": "Notice nailed by the door",
        "payload": {"text": "Bounty stands at the pier. Ask for Vell."},
    }])

    first = resolve_interaction(player_id, location_id, "notice")
    second = resolve_interaction(player_id, location_id, "notice")

    for result in (first, second):
        assert result["success"] is True and result["consumed"] is False
        assert result["result"]["panel_title"] == "Notice nailed by the door"
        assert result["result"]["panel_text"].startswith("Bounty stands at the pier")
        assert result["result"]["floater"] is None
        assert result["result"]["items_gained"] == []
    assert _stored_objects(location_id)[0]["consumed"] is False


def test_locked_open_does_nothing(db):
    player_id, location_id = _scene([{
        "kind": "open", "id": "box", "label": "Iron strongbox",
        "payload": {"lock": "a small brass key", "item": {"name": "Ledger"}},
    }])

    result = resolve_interaction(player_id, location_id, "box")

    assert result == {
        "success": False, "locked": True, "label": "Iron strongbox",
        "lock_hint": "a small brass key",
        "chat_prompt": "I try to open the iron strongbox.",
    }
    assert _inventory(player_id) == []
    assert _stored_objects(location_id)[0]["consumed"] is False


def test_unlocked_open_yields_item_and_text(db):
    player_id, location_id = _scene([{
        "kind": "open", "id": "crate", "label": "Fish crate",
        "payload": {"text": "Packed in coarse salt.", "item": {"name": "Salted eels", "quantity": 2}},
    }])

    result = resolve_interaction(player_id, location_id, "crate")

    assert result["consumed"] is True
    assert result["result"]["floater"] == "+ Salted eels"
    assert result["result"]["panel_text"] == "Packed in coarse salt."
    assert _inventory(player_id)[0]["quantity"] == 2


def test_search_yields_text_and_optional_item(db):
    player_id, location_id = _scene([
        {"kind": "search", "id": "bedding", "label": "Straw bedding",
         "payload": {"text": "Nothing but a broken comb."}},
        {"kind": "search", "id": "woodpile", "label": "Woodpile",
         "payload": {"text": "A hatchet, forgotten.", "item": {"name": "Hatchet", "type": "weapon"}}},
    ])

    bare = resolve_interaction(player_id, location_id, "bedding")
    with_item = resolve_interaction(player_id, location_id, "woodpile")

    assert bare["result"]["panel_text"] == "Nothing but a broken comb."
    assert bare["result"]["floater"] is None and bare["result"]["items_gained"] == []
    assert with_item["result"]["floater"] == "+ Hatchet"
    assert with_item["result"]["panel_text"] == "A hatchet, forgotten."
    assert [i["name"] for i in _inventory(player_id)] == ["Hatchet"]


def test_unknown_object_and_wrong_room_rejected(db):
    player_id, location_id = _scene([{"kind": "read", "id": "notice", "label": "Notice",
                                      "payload": {"text": "Words."}}])

    assert resolve_interaction(player_id, location_id, "ghost") == {
        "success": False, "error": "no such object",
    }
    assert resolve_interaction("nobody", location_id, "notice") == {
        "success": False, "error": "no player",
    }
    assert resolve_interaction(player_id, "elsewhere", "notice") == {
        "success": False, "error": "not here",
    }

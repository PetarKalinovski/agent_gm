"""Items that stay honest, and commands that say what they dropped (round 4).

fleet5-tamsin T12: `@give Silver Coin | kind: trinket | tier: common` became
an equipped combat trinket worth 60, standing in for the authored Greywater
Silver Piece still in the Hollow's crate. fleet5-wenna T3: `@new-npc Ennis
Quill | … | status: dead` came back ✓ and the dead carter stood alive in the
square for eight turns. fleet5-tamsin T2/T8: `@move Market Square` from the
square said nothing, and the DM kept narrating a walk.
"""

import pytest

from src.models import (
    Connection, DMState, Location, LocationType, NPC, Player, Quest, QuestStatus, WorldClock, get_session,
)
from src.services import memory_files
from src.services.commands import CommandRunner
from src.services.scene_brief import build_scene_brief

WHISTLE = {"id": "whistle_1", "kind": "take", "label": "Cracked bone whistle", "x": 60, "y": 74,
           "payload": {"item": {"name": "Cracked Bone Whistle", "type": "quest_item",
                                "description": "Split down the barrel — silent now."}}}
CRATE = {"id": "crate_2", "kind": "search", "label": "Split supply crate", "x": 80, "y": 56,
         "payload": {"item": {"name": "Greywater Silver Piece", "type": "misc",
                              "description": "Freshly minted.", "value": 12}}}
BUNDLE = {"id": "bundle_0", "kind": "take", "label": "Dropped sweetroot bundle", "x": 30, "y": 66,
          "payload": {"item": {"name": "Sweetroot Bundle", "type": "consumable", "description": "Chewy."}}}
SATCHEL = {"id": "satchel_0", "kind": "open", "label": "Lost traveler's satchel", "x": 50, "y": 60,
           "payload": {"item": {"name": "Water-Stained Letter", "type": "quest_item", "description": "A letter."},
                       "lock": "ask the keeper first"}}


@pytest.fixture()
def w(db, tmp_path, monkeypatch):
    monkeypatch.setattr(memory_files, "memory_root", lambda: tmp_path / "memory")
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    with get_session() as session:
        session.add(WorldClock(day=1, hour=19, minute=0))
        session.add(DMState(tension="low"))
        square = Location(name="Market Square", type=LocationType.DISTRICT, discovered=True, visited=True,
                          objects=[BUNDLE])
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, discovered=True,
                          objects=[WHISTLE, CRATE])
        lantern = Location(name="The Drowned Lantern", type=LocationType.BUILDING, discovered=True,
                           objects=[SATCHEL])
        session.add_all([square, hollow, lantern])
        session.flush()
        for other in (hollow, lantern):
            session.add(Connection(from_location_id=square.id, to_location_id=other.id, travel_type="walk",
                                   travel_time_hours=0.25, bidirectional=True, discovered=True))
        brann = NPC(name="Captain Brann Coldiron", profession="watch captain", current_location_id=square.id)
        session.add(brann)
        session.add(Quest(title="Teeth on the North Road", description="", status=QuestStatus.ACTIVE,
                          objectives=[{"id": "o1", "text": "Reach Wolf's Hollow", "kind": "go_to",
                                       "target_id": hollow.id, "done": True}]))
        player = Player(name="Tamsin Rook", current_location_id=square.id, currency=35)
        session.add(player)
        session.commit()
        return {"player": player.id, "square": square.id, "hollow": hollow.id, "lantern": lantern.id,
                "brann": brann.id}


def _run(w, line):
    return CommandRunner(w["player"], present_ids=[w["brann"]]).run(line)


def _pack(w):
    with get_session() as session:
        player = session.get(Player, w["player"])
        return {i["name"]: i for i in player.inventory or []}, dict(player.equipment or {})


def _place(w, where):
    with get_session() as session:
        session.get(Player, w["player"]).current_location_id = w[where]
        session.commit()


def _objects(w, where):
    with get_session() as session:
        return {o["id"]: o for o in session.get(Location, w[where]).objects}


# ----------------------------------------------------------------------
# Coin words are never gear
# ----------------------------------------------------------------------

def test_a_coin_given_as_a_trinket_is_kept_as_a_clue_never_worn(w):
    res = _run(w, "@give Lucky Coin | kind: trinket | tier: common")
    assert res.ok and "money or evidence — kept as a clue" in res.message and "@coin +N" in res.message
    pack, equipment = _pack(w)
    coin = pack["Lucky Coin"]
    assert coin["type"] == "quest_item" and not coin.get("slot") and not coin.get("tier")
    assert not equipment.get("trinket"), "never auto-equipped"


@pytest.mark.parametrize("line", [
    "@give Minted Silver Piece | kind: trinket",
    "@give Copper Mark | kind: weapon | tier: crude",
    "@give Scrap of Blue Cloth | kind: armor | tier: common",
    "@give Brass Buttons | tier: common",
])
def test_money_and_evidence_are_clues_whatever_kind(w, line):
    res = _run(w, line)
    assert res.ok, res.message
    item = next(iter(_pack(w)[0].values()))
    assert item["type"] == "quest_item" and not item.get("slot")


@pytest.mark.parametrize("line, slot", [
    ("@give Shoulder Piece | kind: armor | tier: common", "armor"),     # a piece of armour, no metal word
    ("@give Wolf Token | kind: trinket | tier: common", "trinket"),     # a token is a charm
])
def test_gear_that_only_sounds_like_money_stays_gear(w, line, slot):
    res = _run(w, line)
    assert res.ok and "money" not in res.message
    item = next(iter(_pack(w)[0].values()))
    assert item.get("slot") == slot


def test_a_ferry_token_key_stays_a_key_and_plain_coin_stays_plain(w):
    assert _run(w, "@give Ferry Token | kind: key | about: A notched brass token.").ok
    assert _run(w, "@give Spare Coins x3").ok
    pack = _pack(w)[0]
    assert pack["Ferry Token"]["type"] == "quest_item"
    assert pack["Spare Coins"]["type"] == "misc" and pack["Spare Coins"]["quantity"] == 3


# ----------------------------------------------------------------------
# Never a substitute for an authored thing
# ----------------------------------------------------------------------

def test_a_give_of_what_a_thing_elsewhere_holds_is_refused(w):
    res = _run(w, "@give Silver Coin | kind: trinket | tier: common")
    assert not res.ok
    assert "that is the Greywater Silver Piece in the split supply crate at Wolf's Hollow" in res.message
    assert "@interact" in res.message
    assert _pack(w)[0] == {}
    res = _run(w, "@give Whistle")
    assert not res.ok and "that is the cracked bone whistle at Wolf's Hollow" in res.message


def test_supplies_a_crate_also_holds_are_still_gifts(w):
    _place(w, "hollow")
    assert _run(w, "@give Sweetroot Bundle | kind: potion").ok, "a snack is a snack"
    assert _run(w, "@give Silver Locket | kind: trinket | tier: common").ok, "one shared word is not the same thing"


def test_a_give_of_what_a_thing_here_holds_takes_it_through_the_thing(w):
    _place(w, "hollow")
    res = _run(w, "@give Cracked Bone Whistle | kind: clue")
    assert res.ok and "taken through it as @interact Cracked bone whistle" in res.message
    whistle = _pack(w)[0]["Cracked Bone Whistle"]
    assert whistle["quantity"] == 1 and "Split down the barrel" in whistle.get("description", "")
    assert _objects(w, "hollow")["whistle_1"]["consumed"] is True, "no second whistle on the ground"


def test_a_locked_thing_here_is_never_given_around(w):
    _place(w, "lantern")
    res = _run(w, "@give Water-Stained Letter | kind: clue")
    assert not res.ok and "locked (ask the keeper first)" in res.message
    assert "@interact Lost traveler's satchel | through" in res.message
    assert _pack(w)[0] == {}


# ----------------------------------------------------------------------
# @new-npc status, dropped keys, a move to nowhere new
# ----------------------------------------------------------------------

def test_new_npc_dead_is_on_record_standing_nowhere(w):
    res = _run(w, "@new-npc Ennis Quill | trade: carter | looks: dead three weeks | status: dead")
    assert res.ok and "is dead" in res.message and "ignored" not in res.message
    with get_session() as session:
        npc = session.query(NPC).filter(NPC.name == "Ennis Quill").one()
        assert npc.status == "dead" and npc.current_location_id is None and npc.home_location_id is None
        brief = build_scene_brief(session, session.get(Player, w["player"]))["text"]
    here = brief.split("People here", 1)[1].split("Price list", 1)[0]
    # No dossier of his own: only what the living know of him (round 8,
    # scene_brief._knows_the_lost: "Knows: Ennis Quill (carter) is dead …")
    assert "- Ennis Quill [" not in here
    assert all("Ennis Quill" not in line or line.lstrip().startswith("Knows:") for line in here.splitlines())
    with get_session() as session:
        assert not session.get(Location, w["square"]).objects or all(
            o["kind"] != "body" for o in session.get(Location, w["square"]).objects), "no body, no loot"


def test_new_npc_missing_belongs_where_they_were_last_seen(w):
    res = _run(w, "@new-npc Mirelle Ash | trade: clerk | status: missing")
    assert res.ok and "is missing" in res.message
    with get_session() as session:
        npc = session.query(NPC).filter(NPC.name == "Mirelle Ash").one()
        assert npc.status == "missing" and npc.current_location_id is None
        assert npc.home_location_id == w["square"]


def test_new_npc_odd_status_is_made_alive_and_said(w):
    res = _run(w, "@new-npc Osk Brine | trade: jailer | looks: a | manner: b | voice: c | status: imprisoned")
    assert res.ok and "status 'imprisoned' is not one @new-npc takes" in res.message
    with get_session() as session:
        npc = session.query(NPC).filter(NPC.name == "Osk Brine").one()
        assert npc.status == "alive" and npc.current_location_id == w["square"]


@pytest.mark.parametrize("line, dropped", [
    ("@give Gate Key | kind: key | value: 5", "ignored value: 5 (@give takes"),
    ("@new-npc Tobin Reed | trade: ferryman | looks: a | manner: b | voice: c | age: forty",
     "ignored age: forty (@new-npc takes"),
    ("@quest-done Teeth on the North Road | reward: 50 coin",
     "ignored reward: 50 coin (@quest-done takes no key: value options)"),
    ("@npc Coldiron | wants: a quiet road", "ignored wants: a quiet road (@npc takes"),
])
def test_a_dropped_key_is_reported(w, line, dropped):
    res = _run(w, line)
    assert dropped in res.message, res.message


@pytest.mark.parametrize("line", [
    "@event Brawl | Smoke: the stall burned | witnessed",
    "@health hurt | wound: a wolf's bite",
    "@coin -3 | fare: the ferryman",
    "@move Wolf's Hollow | through",
])
def test_free_text_and_known_keys_are_not_reported(w, line):
    res = _run(w, line)
    assert "ignored" not in res.message, res.message


def test_a_move_to_where_they_already_are_says_so(w):
    """A silent ✓ read as a move (fleet6-tamsin T16 answered speaker_not_here
    with a no-op @move; fleet6-wenna T6/T9/T11 toured a stable yard on three):
    it fails, and names what would have worked."""
    res = _run(w, "@move Market Square")
    assert not res.ok and res.result.get("already_here")
    assert res.message.startswith("already here — they are in Market Square; nothing moved")
    assert "@npc-move <name> | Market Square" in res.message and "@new-place <spot>" in res.message


def test_a_move_to_where_they_already_are_still_ticks_a_go_to_here(w):
    """Before the short-circuit, a same-place move_player ticked a "go to"
    aimed here (a quest taken up where its place is); it still does."""
    with get_session() as session:
        session.add(Quest(title="Mind the Square", description="", status=QuestStatus.ACTIVE,
                          objectives=[{"id": "o1", "text": "Reach Market Square", "kind": "go_to",
                                       "target_id": w["square"], "done": False}]))
        session.commit()
    res = _run(w, "@move Market Square")
    assert res.ok and res.result.get("already_here") and res.result.get("objectives_done")
    with get_session() as session:
        quest = session.query(Quest).filter(Quest.title == "Mind the Square").one()
        assert quest.objectives[0]["done"] is True


def test_plain_stuff_a_crate_elsewhere_holds_is_still_a_gift(w):
    """Only a clue is never re-made elsewhere: the market crate's Coil of
    Hemp Rope must not refuse the coil of rope someone hands over across
    town — but rope a quest step asks for is the authored one."""
    rope = {"id": "crate_9", "kind": "search", "label": "Abandoned market crate", "x": 40, "y": 60,
            "payload": {"item": {"name": "Coil of Hemp Rope", "type": "misc", "description": "Twenty feet."}}}
    with get_session() as session:
        session.get(Location, w["hollow"]).objects = [WHISTLE, CRATE, rope]
        session.commit()
    assert _run(w, "@give Coil of Rope").ok, "plain stuff elsewhere is just a gift"
    with get_session() as session:
        session.add(Quest(title="Rope for the Well", description="", status=QuestStatus.ACTIVE,
                          objectives=[{"id": "o1", "text": "Find the hemp rope", "kind": "obtain",
                                       "item_name": "Coil of Hemp Rope", "done": False}]))
        session.commit()
    res = _run(w, "@give Hemp Rope")
    assert not res.ok and "that is the Coil of Hemp Rope in the abandoned market crate at Wolf's Hollow" in res.message

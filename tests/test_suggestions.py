"""Suggested actions: code-derived chips, the lead first, never more than four."""

import pytest

from src.models import Connection, Location, LocationType, NPC, NPCRelationship, Player, get_session
from src.services import lead as lead_mod
from src.services import objectives as obj
from src.services.scene_objects import ENGINE_SRC
from src.services.suggestions import MAX_CHIPS, object_verb, suggest_actions


def _world(session):
    square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True,
                      objects=[{"kind": "read", "label": "Notice board", "x": 30, "y": 60,
                                "payload": {"text": "WOLVES. Keep to the walls."}}])
    hollow = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True)
    lantern = Location(name="The Drowned Lantern", type=LocationType.BUILDING, depth=0, discovered=True)
    session.add_all([square, hollow, lantern])
    session.flush()
    session.add(Connection(from_location_id=square.id, to_location_id=hollow.id, travel_type="walk",
                           travel_time_hours=0.75, requirements=["gate pass"], bidirectional=True, discovered=True))
    session.add(Connection(from_location_id=square.id, to_location_id=lantern.id, travel_type="walk",
                           travel_time_hours=0.1, bidirectional=True, discovered=True))
    brann = NPC(name="Captain Brann Coldiron", current_location_id=square.id)
    piper = NPC(name="Piper Vance", current_location_id=square.id)
    ghost = NPC(name="Ghost", current_location_id=square.id, status="dead")
    session.add_all([brann, piper, ghost])
    player = Player(name="Rook", current_location_id=square.id)
    session.add(player)
    session.flush()
    # Both have met the player: chips name only people the player knows
    session.add_all([NPCRelationship(npc_id=brann.id, player_id=player.id),
                     NPCRelationship(npc_id=piper.id, player_id=player.id)])
    session.commit()
    return {"player": player.id, "square": square.id, "hollow": hollow.id, "lantern": lantern.id, "brann": brann.id}


def _chips(world, lead=None):
    with get_session() as session:
        player = session.get(Player, world["player"])
        return suggest_actions(session, player, lead, player.current_location_id)


def test_no_lead_offers_people_exits_and_objects(db):
    with get_session() as session:
        world = _world(session)
    chips = _chips(world)
    assert 2 <= len(chips) <= MAX_CHIPS
    kinds = [c["kind"] for c in chips]
    assert kinds[0] == "talk" and chips[0]["label"] == "Talk to Brann"
    assert "Talk to Piper" in [c["label"] for c in chips]
    assert not any("Ghost" in c["label"] for c in chips)
    travel = [c for c in chips if c["kind"] == "travel"]
    assert travel and travel[0]["label"] == "Head to The Drowned Lantern" and travel[0]["locked"] is False


def test_lead_on_a_present_npc_becomes_an_ask_chip(db):
    with get_session() as session:
        world = _world(session)
        lead = {"text": "Find out what is organizing the wolves", "kind": "talk_to", "source": "quest",
                "quest_title": "Teeth on the North Road",
                "target": {"type": "npc", "id": world["brann"], "name": "Captain Brann Coldiron", "here": True},
                "route_next_hop_location_id": None}
    chips = _chips(world, lead)
    assert chips[0]["kind"] == "talk" and chips[0]["npc_id"] == world["brann"]
    assert chips[0]["label"] == "Ask Brann about what is organizing the wolves"
    assert sum(1 for c in chips if c.get("npc_id") == world["brann"]) == 1, "no duplicate Brann chip"


def test_lead_elsewhere_becomes_a_travel_chip_even_through_a_locked_road(db):
    with get_session() as session:
        world = _world(session)
        lead = {"text": "Reach Wolf's Hollow", "kind": "go_to", "source": "quest", "quest_title": "Teeth",
                "target": {"type": "location", "id": world["hollow"], "name": "Wolf's Hollow", "here": False},
                "route_next_hop_location_id": world["hollow"]}
    chips = _chips(world, lead)
    assert chips[0] == {"kind": "travel", "location_id": world["hollow"], "locked": True,
                        "label": "Head to Wolf's Hollow", "text": "I try to get through to Wolf's Hollow."} \
        or (chips[0]["kind"] == "travel" and chips[0]["locked"] is True)


def test_untargeted_lead_is_offered_verbatim(db):
    with get_session() as session:
        world = _world(session)
    chips = _chips(world, {"text": "Earn Maera's trust", "kind": "custom", "source": "quest", "target": None})
    assert chips[0] == {"kind": "lead", "label": "Earn Maera's trust", "text": "I earn Maera's trust."}


def test_a_lead_still_ahead_on_the_clock_is_no_chip(db):
    """fleet7-tamsin: tomorrow's cart is nothing to do at 23:40 (lead.beat_due_later)."""
    with get_session() as session:
        world = _world(session)
    lead = {"text": "Tomorrow morning: the cart that limped in short is being unloaded.", "kind": "story",
            "source": "beat", "target": None, "route_next_hop_location_id": None,
            "due": {"day": 2, "hour": 9, "when": "tomorrow morning"}}
    chips = _chips(world, lead)
    assert chips and not any("cart" in c["label"].lower() or c["kind"] == "lead" for c in chips)


def test_empty_room_pads_with_look_around(db):
    with get_session() as session:
        world = _world(session)
        player = session.get(Player, world["player"])
        player.current_location_id = world["lantern"]
        session.commit()
    chips = _chips(world)
    assert any(c["kind"] == "look" for c in chips)
    assert len(chips) >= 2


def test_chips_match_the_real_lead(db):
    with get_session() as session:
        world = _world(session)
        player = session.get(Player, world["player"])
        from src.models import Quest, QuestStatus
        quest = Quest(title="Teeth", description="", status=QuestStatus.ACTIVE,
                      objectives=obj.normalize_objectives(
                          [{"text": "Find Coldiron", "kind": "talk_to", "target_id": world["brann"]}], session=session))
        session.add(quest)
        session.flush()
        player.active_quests = [quest.id]
        session.commit()
        lead = lead_mod.compute_lead(session, player)
        chips = suggest_actions(session, player, lead, player.current_location_id)
    assert chips[0]["label"].startswith("Ask Brann about")


@pytest.mark.parametrize("label, verb", [
    ("Town Well", "Examine"),
    ("Weathered statue", "Examine"),
    ("Notice board", "Read"),
    ("Wanted posters", "Read"),
    ("A torn letter", "Read"),
    ("Brass plaque", "Read"),
    ("Tavern sign", "Read"),
    ("Old map", "Read"),
    ("Floorboard", "Examine"),      # "board" only as its own word
    ("Signal fire", "Examine"),     # "sign" only as its own word
])
def test_read_objects_say_read_only_when_they_carry_writing(label, verb):
    assert object_verb("read", label) == verb


def test_other_object_kinds_keep_their_verb():
    assert object_verb("take", "Town Well") == "Take"
    assert object_verb("open", "Iron chest") == "Open"
    assert object_verb("body", "Brann's body") == "Search"
    assert object_verb("rest", "Straw bed") == "Rest at"


def test_object_chips_read_naturally(db):
    """Playtest 2026-10-04: a chip read "Read the Town Well"."""
    with get_session() as session:
        world = _world(session)
        lantern = session.get(Location, world["lantern"])
        lantern.objects = [
            {"kind": "read", "label": "Town Well", "x": 20, "y": 60, "payload": {"text": "Deep."}},
            {"kind": "read", "label": "A torn letter", "x": 40, "y": 60, "payload": {"text": "Run."}},
            {"kind": "body", "label": "Brann's body", "x": 60, "y": 60, "src": ENGINE_SRC,
             "payload": {"items": [], "coin": 3}},
        ]
        player = session.get(Player, world["player"])
        player.current_location_id = lantern.id
        session.commit()
    chips = _chips(world)
    by_object = {c["label"]: c["text"] for c in chips if c["kind"] == "interact"}
    assert by_object == {
        "Examine the Town Well": "I examine the Town Well.",
        "Read the torn letter": "I read the torn letter.",
        "Search Brann's body": "I search Brann's body.",
    }


def _lead_on_brann(world, text, **extra):
    return {"text": text, "kind": extra.pop("kind", "talk_to"), "source": "quest",
            "target": {"type": "npc", "id": world["brann"], "name": "Captain Brann Coldiron", "here": True},
            "route_next_hop_location_id": None, **extra}


def test_a_lead_that_only_names_the_person_asks_about_the_quest(db):
    """A lead of "Talk to Captain Brann Coldiron" must not read "Ask Brann about Captain Brann Coldiron"."""
    with get_session() as session:
        world = _world(session)
    chips = _chips(world, _lead_on_brann(world, "Talk to Captain Brann Coldiron", quest_title="Teeth"))
    assert chips[0]["label"] == "Ask Brann about Teeth"
    assert chips[0]["text"] == "I go to Captain Brann Coldiron and ask about Teeth."

    chips = _chips(world, _lead_on_brann(world, "Talk to Captain Brann Coldiron"))
    assert chips[0]["label"] == "Talk to Brann" and chips[0]["npc_id"] == world["brann"]


def test_a_lead_that_is_already_a_question_is_offered_verbatim(db):
    with get_session() as session:
        world = _world(session)
    chips = _chips(world, _lead_on_brann(world, "Ask the Captain what the howling means.", quest_title="Teeth"))
    assert chips[0]["label"] == "Ask the Captain what the howling means"
    assert chips[0]["text"] == "I ask the Captain what the howling means."


def test_a_delivery_lead_offers_the_hand_over(db):
    with get_session() as session:
        world = _world(session)
    chips = _chips(world, _lead_on_brann(world, "Deliver the Gate Pass to Captain Brann Coldiron",
                                         kind="deliver", item_name="Gate Pass", quest_title="Teeth"))
    assert chips[0]["label"] == "Give Brann the Gate Pass"
    assert chips[0]["text"] == "I give Captain Brann Coldiron the Gate Pass."


@pytest.mark.parametrize("label, phrase", [
    ("Notice board", "the Notice board"),
    ("A torn letter", "the torn letter"),
    ("Brann's body", "Brann's body"),
    # loot.body_label is "<full NPC name>'s body": the possessive is not the first word
    ("Captain Brann Coldiron's body", "Captain Brann Coldiron's body"),
    ("The Grey Alpha's body", "the Grey Alpha's body"),
    ("A miner's pick", "the miner's pick"),
    ("", "the thing"),
])
def test_object_phrases(label, phrase):
    from src.services.suggestions import _object_phrase
    assert _object_phrase(label) == phrase


def test_a_body_chip_for_a_titled_npc_takes_no_article(db):
    from src.models import NPC as _NPC
    from src.services.loot import body_label
    with get_session() as session:
        world = _world(session)
        lantern = session.get(Location, world["lantern"])
        brann = session.get(_NPC, world["brann"])
        lantern.objects = [{"kind": "body", "label": body_label(brann), "x": 60, "y": 60, "src": ENGINE_SRC,
                            "payload": {"items": [], "coin": 3}}]
        session.get(Player, world["player"]).current_location_id = lantern.id
        session.commit()
    labels = [c["label"] for c in _chips(world) if c["kind"] == "interact"]
    assert labels == ["Search Captain Brann Coldiron's body"]


def test_a_kill_lead_on_someone_here_is_an_attack_not_a_chat(db):
    with get_session() as session:
        world = _world(session)
    lead = {"text": "Kill the Grey Alpha", "kind": "kill", "source": "quest", "quest_title": "Teeth",
            "target": {"type": "npc", "id": world["brann"], "name": "The Grey Alpha", "here": True},
            "route_next_hop_location_id": None}
    chips = _chips(world, lead)
    assert chips[0] == {"kind": "lead", "npc_id": world["brann"],
                        "label": "Attack the Grey Alpha", "text": "I attack the Grey Alpha."}


def test_a_delivery_of_a_possessive_item_reads_naturally(db):
    with get_session() as session:
        world = _world(session)
    chips = _chips(world, _lead_on_brann(world, "Deliver Maera's letter to Captain Brann Coldiron",
                                         kind="deliver", item_name="Maera's letter", quest_title="Teeth"))
    assert chips[0]["label"] == "Give Brann Maera's letter"
    assert chips[0]["text"] == "I give Captain Brann Coldiron Maera's letter."

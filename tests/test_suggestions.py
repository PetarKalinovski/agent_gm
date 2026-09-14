"""Suggested actions: code-derived chips, the lead first, never more than four."""

from src.models import Connection, Location, LocationType, NPC, Player, get_session
from src.services import lead as lead_mod
from src.services import objectives as obj
from src.services.suggestions import MAX_CHIPS, suggest_actions


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

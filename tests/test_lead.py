"""The Lead: one code-chosen next step, and a route the canvas can light.

Pins `src/services/lead.py`: newest quest wins, a finished quest falls
through, the campaign beat's player-facing lead is the fallback, and the
next-hop search prunes roads the player cannot walk — except when the
locked road is the destination itself.
"""

from src.models import (
    Campaign, Connection, Location, LocationType, NPC, Player, Quest, QuestStatus,
    StoryBeat, get_session,
)
from src.services import lead as lead_mod
from src.services import objectives as obj


def _world(session):
    """square —(road)— gate —(locked road)— hollow ; square —(road)— lantern ; ruins undiscovered"""
    square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True)
    gate = Location(name="North Gate", type=LocationType.DISTRICT, depth=0, discovered=True)
    hollow = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True)
    lantern = Location(name="The Drowned Lantern", type=LocationType.BUILDING, depth=0, discovered=True)
    ruins = Location(name="Old Ruins", type=LocationType.POI, depth=0, discovered=False)
    session.add_all([square, gate, hollow, lantern, ruins])
    session.flush()

    def road(a, b, **kw):
        session.add(Connection(from_location_id=a.id, to_location_id=b.id, travel_type="walk",
                               travel_time_hours=0.5, bidirectional=True, discovered=True, **kw))

    road(square, gate)
    road(gate, hollow, requirements=["gate pass"])
    road(square, lantern)
    road(lantern, ruins)
    brann = NPC(name="Captain Brann Coldiron", current_location_id=square.id)
    vex = NPC(name="Vex Marrow", current_location_id=hollow.id)
    session.add_all([brann, vex])
    player = Player(name="Rook", current_location_id=square.id)
    session.add(player)
    session.commit()
    return {"player": player.id, "square": square.id, "gate": gate.id, "hollow": hollow.id,
            "lantern": lantern.id, "ruins": ruins.id, "brann": brann.id, "vex": vex.id}


def _activate(session, world, title, objectives):
    quest = Quest(title=title, description="", status=QuestStatus.ACTIVE,
                  objectives=obj.normalize_objectives(objectives, session=session))
    session.add(quest)
    session.flush()
    player = session.get(Player, world["player"])
    player.active_quests = list(player.active_quests or []) + [quest.id]
    session.commit()
    return quest.id


def _lead(world):
    with get_session() as session:
        return lead_mod.compute_lead(session, session.get(Player, world["player"]))


def test_no_quest_no_campaign_means_no_lead(db):
    with get_session() as session:
        world = _world(session)
    assert _lead(world) is None
    assert lead_mod.format_lead_context(None) == lead_mod.NO_LEAD_CONTEXT
    assert "NOTHING to do" in lead_mod.NO_LEAD_CONTEXT


def test_newest_activated_quest_wins_and_a_finished_one_falls_through(db):
    with get_session() as session:
        world = _world(session)
        first = _activate(session, world, "Old Errand", [{"text": "Go to the Lantern", "kind": "go_to", "target_id": world["lantern"]}])
        newest = _activate(session, world, "Teeth on the North Road", [
            {"text": "Find Coldiron", "kind": "talk_to", "target_id": world["brann"]},
            {"text": "Reach the hollow", "kind": "go_to", "target_id": world["hollow"]},
        ])
    lead = _lead(world)
    assert lead["source"] == "quest" and lead["quest_id"] == newest
    assert lead["text"] == "Find Coldiron"
    assert lead["target"]["type"] == "npc" and lead["target"]["here"] is True
    assert lead["route_next_hop_location_id"] is None

    obj.note_conversation(world["player"], world["brann"])
    lead = _lead(world)
    assert lead["text"] == "Reach the hollow"
    assert lead["target"]["here"] is False
    assert lead["route_next_hop_location_id"] == world["gate"]

    with get_session() as session:
        quest = session.get(Quest, newest)
        quest.objectives = [dict(o, done=True) for o in obj.normalize_objectives(quest.objectives)]
        session.commit()
    lead = _lead(world)
    assert lead["quest_id"] == first, "a quest with nothing left to do yields to the next"


def test_npc_target_routes_to_where_they_stand(db):
    with get_session() as session:
        world = _world(session)
        _activate(session, world, "Hunt", [{"text": "Find Vex", "kind": "talk_to", "target_id": world["vex"]}])
    lead = _lead(world)
    assert lead["target"]["location_id"] == world["hollow"]
    assert lead["route_next_hop_location_id"] == world["gate"]


def test_locked_road_is_pruned_unless_it_is_the_destination(db):
    with get_session() as session:
        world = _world(session)
        player = session.get(Player, world["player"])
        player.current_location_id = world["gate"]
        session.commit()
    with get_session() as session:
        player = session.get(Player, world["player"])
        # The hollow IS the target: light the locked door
        assert lead_mod.next_hop(session, player, world["hollow"]) == world["hollow"]
        # Something beyond it would have to be reached through the lock — nothing else leads there
        assert lead_mod.next_hop(session, player, world["lantern"]) == world["square"]


def test_undiscovered_target_points_nowhere(db):
    with get_session() as session:
        world = _world(session)
        _activate(session, world, "Digging", [{"text": "Search the ruins", "kind": "go_to", "target_id": world["ruins"]}])
    lead = _lead(world)
    assert lead["text"] == "Search the ruins"
    assert lead["route_next_hop_location_id"] is None
    with get_session() as session:
        player = session.get(Player, world["player"])
        assert lead_mod.next_hop(session, player, world["ruins"]) is None


def test_dead_npc_target_leaves_text_only(db):
    with get_session() as session:
        world = _world(session)
        _activate(session, world, "Hunt", [{"text": "Find Vex", "kind": "talk_to", "target_id": world["vex"]}])
        session.get(NPC, world["vex"]).status = "dead"
        session.commit()
    lead = _lead(world)
    assert lead["target"] is None and lead["text"] == "Find Vex"


def test_beat_lead_is_the_fallback_and_stalled_beats_count(db):
    with get_session() as session:
        world = _world(session)
        campaign = Campaign(player_id=world["player"], title="The Whistle", status="active")
        session.add(campaign)
        session.flush()
        session.add_all([
            StoryBeat(campaign_id=campaign.id, act=1, sequence=2, beat_type="revelation",
                      title="Later", status="armed", lead="Follow the whistle north.",
                      lead_target={"type": "location", "id": world["hollow"]}),
            StoryBeat(campaign_id=campaign.id, act=1, sequence=1, beat_type="revelation",
                      title="First", status="stalled", lead="Ask Coldiron about the howling.",
                      lead_target={"type": "npc", "id": world["brann"]}),
            StoryBeat(campaign_id=campaign.id, act=1, sequence=0, beat_type="revelation",
                      title="No lead", status="armed", lead=None),
            StoryBeat(campaign_id=campaign.id, act=1, sequence=0, beat_type="revelation",
                      title="Delivered", status="delivered", lead="Old news"),
        ])
        session.commit()
    lead = _lead(world)
    assert lead["source"] == "beat" and lead["text"] == "Ask Coldiron about the howling."
    assert lead["target"]["id"] == world["brann"] and lead["target"]["here"] is True
    block = lead_mod.format_lead_context(lead)
    assert "story spine" in block and "Ask Coldiron about the howling." in block

    # A quest always outranks the spine
    with get_session() as session:
        _activate(session, world, "Errand", ["Buy bread"])
    assert _lead(world)["source"] == "quest"


def test_context_block_names_where_the_target_is(db):
    with get_session() as session:
        world = _world(session)
        _activate(session, world, "Hunt", [{"text": "Find Vex", "kind": "talk_to", "target_id": world["vex"]}])
    block = lead_mod.format_lead_context(_lead(world))
    assert '"Find Vex"  (quest: Hunt)' in block
    assert "Vex Marrow is at Wolf's Hollow" in block
    assert "Never contradict it" in block

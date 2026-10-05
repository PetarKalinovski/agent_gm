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


def _cart_campaign(session, world, *, clock=(1, 23, 40)):
    """fleet7-tamsin after T14: no quest, the clock at 23:40, the Greywater cart
    due at 09:00 and the act's time beat telling it as already unloaded."""
    from src.models import Event, WorldClock

    session.add(WorldClock(day=clock[0], hour=clock[1], minute=clock[2]))
    session.add(Event(name="The Greywater Cart Arrives Short", event_type="meso", player_visible=True,
                      description="The overdue supply cart finally limps through the South Gate half-empty.",
                      scheduled_day=2, scheduled_hour=9))
    campaign = Campaign(player_id=world["player"], title="The Second Signature", status="active", current_act=2)
    session.add(campaign)
    session.flush()
    session.add_all([
        StoryBeat(campaign_id=campaign.id, act=1, sequence=4, beat_type="setpiece", title="The Cart Limps In",
                  status="armed", trigger_type="time", trigger_data={"delay": "days"},
                  scheduled_day=3, scheduled_hour=18,
                  lead="The cart that limped in short is being unloaded at the Stable Yard.",
                  lead_target={"type": "npc", "id": world["brann"]}),
        StoryBeat(campaign_id=campaign.id, act=2, sequence=7, beat_type="revelation", title="The Fence's Shelf",
                  status="armed", trigger_type="after_beat", lead="Ask the Keeper about the lost satchel.",
                  lead_target={"type": "location", "id": world["lantern"]}),
        # A time beat whose lead is an invitation that stands now, not its happening
        StoryBeat(campaign_id=campaign.id, act=1, sequence=9, beat_type="deadline_event", title="The Evening Howl",
                  status="armed", trigger_type="time", scheduled_day=6, scheduled_hour=18,
                  lead="The stable hand keeps looking toward the north wall."),
    ])
    session.commit()
    return campaign.id


def test_a_beat_lead_about_tomorrow_yields_to_one_that_stands_now(db):
    """fleet7-tamsin T15-T20: "The cart that limped in short is being
    unloaded" was the lead at 23:40 with its cart due at 09:00, and the DM
    showed its wreckage being picked over — twice."""
    with get_session() as session:
        world = _world(session)
        _cart_campaign(session, world)
    # The next beat in order whose lead stands now — a time beat's invitation
    # is no happening of its own
    lead = _lead(world)
    assert lead["text"] == "The stable hand keeps looking toward the north wall." and not lead.get("due")
    with get_session() as session:
        session.query(StoryBeat).filter(StoryBeat.title == "The Evening Howl").one().status = "delivered"
        session.commit()
    assert _lead(world)["text"] == "Ask the Keeper about the lost satchel."
    with get_session() as session:
        beats = {b.title: b for b in session.query(StoryBeat).all()}
        assert lead_mod.beat_due_later(session, beats["The Cart Limps In"], (1, 23)) == (2, 9)
        assert lead_mod.beat_due_later(session, beats["The Evening Howl"], (1, 23)) is None


def test_a_future_beat_lead_alone_says_when_and_points_nowhere(db):
    from src.models import Event

    with get_session() as session:
        world = _world(session)
        _cart_campaign(session, world)
        for beat in session.query(StoryBeat).filter(StoryBeat.title != "The Cart Limps In").all():
            beat.status = "delivered"
        session.commit()
    lead = _lead(world)
    assert lead["text"] == "Tomorrow morning: the cart that limped in short is being unloaded at the Stable Yard."
    assert lead["due"] == {"day": 2, "hour": 9, "when": "tomorrow morning"}
    assert lead["target"] is None and lead["route_next_hop_location_id"] is None
    block = lead_mod.format_lead_context(lead)
    assert "It has NOT happened yet: it is due tomorrow morning (day 2, 09:00)" in block
    assert "never narrate it early" in block and "let it pay off" not in block

    # The cart came in: the lead is true now, and points where it did
    with get_session() as session:
        session.query(Event).one().occurred_day = 2
        session.commit()
    lead = _lead(world)
    assert lead["text"] == "The cart that limped in short is being unloaded at the Stable Yard."
    assert not lead.get("due") and lead["target"]["id"] == world["brann"]


def test_a_lead_that_stands_now_is_not_dated_by_its_beats_event(db):
    """Only a lead that tells the happening waits for it. fleet7-bram's spine
    titled a beat "The Evening Howl" (the seed's 20:00 event) and gave it the
    hook "Sorrel Tack keeps looking toward the north wall" — dated "Tonight"
    it lost its target at the start of play; leads that say the cart has not
    come, or only talk of it, are true now (round 1-6 spines)."""
    from src.models import Event, WorldClock

    with get_session() as session:
        world = _world(session)
        session.add(WorldClock(day=1, hour=17, minute=30))
        session.add_all([
            Event(name="The Evening Howl", event_type="meso", player_visible=True, scheduled_day=1,
                  scheduled_hour=20, description="At the stroke of eight, howling rises from the northern hills.",
                  consequences=["North Gate barred for the night"]),
            Event(name="The Greywater Cart Arrives Short", event_type="meso", player_visible=True,
                  description="The overdue supply cart finally limps through the South Gate half-empty.",
                  scheduled_day=2, scheduled_hour=9),
        ])
        campaign = Campaign(player_id=world["player"], title="The Whistle", status="active")
        session.add(campaign)
        session.flush()
        beats = [
            StoryBeat(campaign_id=campaign.id, act=1, sequence=1, beat_type="setpiece", title="The Evening Howl",
                      status="armed", trigger_type="time", scheduled_day=1, scheduled_hour=20,
                      lead="Sorrel Tack keeps looking toward the north wall like he's seen something.",
                      lead_target={"type": "npc", "id": world["brann"]}),
            StoryBeat(campaign_id=campaign.id, act=1, sequence=2, beat_type="setpiece",
                      title="The Gate Shuts on the Missing Cart", status="armed", trigger_type="time",
                      scheduled_day=2, scheduled_hour=9,
                      lead="The cart from Greywater still hasn't come. Someone should find out why."),
            StoryBeat(campaign_id=campaign.id, act=1, sequence=3, beat_type="setpiece",
                      title="The Cart Stays Lost", status="armed", trigger_type="time",
                      scheduled_day=2, scheduled_hour=9,
                      lead="Everyone in the square is talking about the Greywater cart."),
            StoryBeat(campaign_id=campaign.id, act=1, sequence=4, beat_type="setpiece",
                      title="The Watcher at the Wall", status="armed", trigger_type="time",
                      scheduled_day=2, scheduled_hour=0,
                      lead="Ask around whether anyone's seen something strange near the north wall at night."),
            StoryBeat(campaign_id=campaign.id, act=1, sequence=5, beat_type="setpiece",
                      title="The Cart Comes Short", status="armed", trigger_type="time",
                      scheduled_day=2, scheduled_hour=9,
                      lead="Word is the overdue cart finally limped in short — see what the driver is saying."),
        ]
        session.add_all(beats)
        session.commit()
        due = {b.title: lead_mod.beat_due_later(session, b, (1, 17)) for b in beats}
    assert due == {"The Evening Howl": None, "The Gate Shuts on the Missing Cart": None,
                   "The Cart Stays Lost": None, "The Watcher at the Wall": None,
                   "The Cart Comes Short": (2, 9)}
    lead = _lead(world)
    assert lead["text"].startswith("Sorrel Tack keeps looking") and not lead.get("due")
    assert lead["target"]["id"] == world["brann"]


def test_when_phrases():
    when = lead_mod.when_phrase
    assert when((1, 23), (2, 9)) == "tomorrow morning"
    assert when((1, 17), (1, 20)) == "tonight"
    assert when((2, 1), (2, 9)) == "in the morning"
    assert when((1, 8), (1, 14)) == "this afternoon"
    assert when((1, 22), (2, 5)) == "at first light tomorrow"
    assert when((1, 23), (2, 0)) == "tonight, after midnight"       # midnight is no first light
    assert when((2, 1), (2, 3)) == "tonight"
    assert when((1, 10), (4, 9)) == "in 3 days"


def test_context_block_names_where_the_target_is(db):
    with get_session() as session:
        world = _world(session)
        _activate(session, world, "Hunt", [{"text": "Find Vex", "kind": "talk_to", "target_id": world["vex"]}])
    block = lead_mod.format_lead_context(_lead(world))
    assert '"Find Vex"  (quest: Hunt)' in block
    assert "Vex Marrow is at Wolf's Hollow" in block
    assert "Never contradict it" in block

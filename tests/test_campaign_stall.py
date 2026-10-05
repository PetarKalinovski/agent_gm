"""A beat the DM never narrated is parked, not claimed told.

Before: three failed delivery attempts marked the beat delivered anyway — a
truth the player never heard got un-sealed and the act advanced past a scene
that never happened. Now the beat stalls: its lead goes on the tracker, it is
delivered when its keywords or its met_condition finally show up, and after
STALLED_SKIP_DAYS it is skipped so the act can still close.
"""

from src.models import Campaign, DMState, Location, LocationType, Player, StoryBeat, WorldClock, get_session
from src.services import campaign as campaign_service
from src.services import lead as lead_mod
from src.services.campaign import (
    BEAT_DELIVERY_ATTEMPTS, STALLED_SKIP_DAYS, mark_beat_delivery, run_campaign_tick,
)


def _world(session, *, attempts=BEAT_DELIVERY_ATTEMPTS):
    session.add(WorldClock(day=1, hour=8, minute=0))
    session.add(DMState())
    square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True)
    hollow = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True)
    session.add_all([square, hollow])
    session.flush()
    player = Player(name="Rook", current_location_id=square.id)
    session.add(player)
    session.flush()
    campaign = Campaign(
        player_id=player.id, title="The Whistle", status="active", current_act=1,
        acts=[{"act": 1, "title": "Teeth", "premise": "p1"}, {"act": 2, "title": "Two", "premise": "p2"},
              {"act": 3, "title": "Three", "premise": "p3"}],
        fixed_truths=[{"id": "t1", "kind": "the_secret", "statement": "Kestrel breeds them.", "revealed": False}],
    )
    session.add(campaign)
    session.flush()
    beat = StoryBeat(
        campaign_id=campaign.id, act=1, sequence=1, beat_type="revelation", title="The kennel",
        directive="Kestrel shows the kennel.", status="fired", fired_day=1, fired_hour=8,
        delivery_attempts=attempts, delivery_keywords=["kennel", "Kestrel"],
        reveals_truth_ids=["t1"], lead="Ask Coldiron about the kennel.",
        lead_target={"type": "location", "id": hollow.id},
        met_condition={"check": "player_visited", "location_id": hollow.id},
        trigger_type="time",
    )
    session.add(beat)
    session.commit()
    return {"player": player.id, "campaign": campaign.id, "beat": beat.id, "hollow": hollow.id}


def _beat(world):
    with get_session() as session:
        b = session.get(StoryBeat, world["beat"])
        c = session.get(Campaign, world["campaign"])
        return b.status, c.current_act, [t.get("revealed") for t in c.fixed_truths]


def test_three_failed_attempts_stall_the_beat_instead_of_delivering_it(db):
    with get_session() as session:
        world = _world(session)
    run_campaign_tick(world["player"])
    status, act, revealed = _beat(world)
    assert status == "stalled"
    assert act == 1, "nothing was told, so nothing advances"
    assert revealed == [False], "a truth the player never heard stays sealed"
    with get_session() as session:
        lead = lead_mod.compute_lead(session, session.get(Player, world["player"]))
    assert lead and lead["source"] == "beat" and lead["text"] == "Ask Coldiron about the kennel."
    assert lead["target"]["id"] == world["hollow"]


def test_a_stalled_beat_is_delivered_when_the_words_finally_come(db):
    with get_session() as session:
        world = _world(session)
    run_campaign_tick(world["player"])
    assert mark_beat_delivery(world["player"], "You sit by the well.")["delivered"] is False
    result = mark_beat_delivery(world["player"], "Kestrel walks you past the kennel.")
    assert result["delivered"] is True
    status, act, revealed = _beat(world)
    assert status == "delivered" and revealed == [True]
    assert act == 2, "the only act-1 beat is delivered, so the act closes"


def test_a_stalled_beat_is_delivered_when_its_condition_is_met(db):
    with get_session() as session:
        world = _world(session)
    run_campaign_tick(world["player"])
    with get_session() as session:
        session.get(Location, world["hollow"]).visited = True
        session.commit()
    result = mark_beat_delivery(world["player"], "Nothing about it in the prose.")
    assert result["delivered"] is True
    assert _beat(world)[0] == "delivered"


def test_a_stalled_beat_is_skipped_after_the_grace_days_and_the_act_closes(db):
    with get_session() as session:
        world = _world(session)
    run_campaign_tick(world["player"])
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.day = 1 + STALLED_SKIP_DAYS + 1
        session.commit()
    run_campaign_tick(world["player"])
    status, act, revealed = _beat(world)
    assert status == "skipped"
    assert revealed == [False], "skipped un-seals nothing"
    assert act == 2


def test_a_fired_beat_still_under_the_limit_is_resurfaced(db):
    with get_session() as session:
        world = _world(session, attempts=0)
        # ...where it happens: one whose place the player left waits there
        # (round 6, tests/test_campaign_round6.py)
        session.get(Player, world["player"]).current_location_id = world["hollow"]
        session.commit()
    tick = run_campaign_tick(world["player"])
    assert tick.get("fired_beat") and tick["fired_beat"]["title"] == "The kennel"
    assert _beat(world)[0] == "fired"

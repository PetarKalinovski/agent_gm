"""Act pacing, stalled-beat recovery and sealed-truth wording (round 4).

fleet5-bram: Bram finished the act-1 job on day 1, and the act-2 payoff
"A Name on the Whistle" (triggered by that very completion) stayed pending
because act 1 still held clock beats for day 2 and day 6 — Coldiron, who had
promised to tell what he knew, stonewalled three turns. fleet5-tamsin: "A
Name on Minted Silver" stalled in the square, its successor "The Whistle in
Her Hand" sat pending behind it, and nothing brought it back when she reached
Wolf's Hollow.
"""

import pytest

from src.models import (
    NPC, Campaign, DMState, Location, LocationType, Player, Quest, QuestStatus, StoryBeat, WorldClock,
    get_session,
)
from src.services import campaign as camp
from src.services.campaign import BEAT_DELIVERY_ATTEMPTS, run_campaign_tick


@pytest.fixture(autouse=True)
def no_generation(monkeypatch):
    monkeypatch.setattr(camp, "_spawn_generation", lambda player_id: None)
    camp.reset_state()
    yield
    camp.reset_state()


ACTS = [{"act": 1, "title": "Teeth", "premise": "p1"}, {"act": 2, "title": "Names", "premise": "p2"},
        {"act": 3, "title": "Stripes", "premise": "p3"}]
TRUTHS = [
    {"id": "truth_villain", "kind": "villain_identity", "revealed": False,
     "statement": "Aldric Vane drills the wolves; Coldiron's predecessor drummed him out.",
     "spoiler_keywords": ["aldric vane", "drummed out"]},
    {"id": "truth_location", "kind": "the_location", "revealed": True,
     "statement": "The wolves are kenneled at Wolf's Hollow.", "spoiler_keywords": ["kennel"]},
]


def _bram_world(session, *, quest_status=QuestStatus.ACTIVE, act=1):
    """pt_l's spine at the end of fleet5-bram: Day 1 23:10, act 1 holding
    only the day-2 revelation, its after_beat and the day-6 deadline."""
    session.add(WorldClock(day=1, hour=23, minute=10))
    session.add(DMState())
    square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True)
    hollow = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True)
    session.add_all([square, hollow])
    session.flush()
    coldiron = NPC(name="Captain Brann Coldiron", current_location_id=square.id)
    sorrel = NPC(name="Sorrel Tack", current_location_id=square.id)
    session.add_all([coldiron, sorrel])
    session.flush()
    teeth = Quest(title="Teeth on the North Road", status=quest_status, assigned_by_npc_id=coldiron.id)
    session.add(teeth)
    player = Player(name="Bram Holloway", current_location_id=square.id)
    session.add(player)
    session.flush()
    campaign = Campaign(player_id=player.id, title="The Toll Road", status="active", current_act=act,
                        acts=ACTS, fixed_truths=[dict(t) for t in TRUTHS],
                        last_beat_day=1, last_beat_hour=20)
    session.add(campaign)
    session.flush()

    def beat(act, seq, title, **kw):
        row = StoryBeat(campaign_id=campaign.id, act=act, sequence=seq, title=title,
                        beat_type=kw.pop("beat_type", "revelation"), directive=kw.pop("directive", title), **kw)
        session.add(row)
        session.flush()
        return row

    beat(1, 1, "The Gate Pass", trigger_type="quest_status", status="delivered",
         trigger_data={"quest_id": teeth.id, "status": "active"})
    watcher = beat(1, 2, "The Watcher at the Wall", trigger_type="time", trigger_data={"delay": "soon"},
                   status="armed", scheduled_day=2, scheduled_hour=0, fallback_day=5, fallback_hour=0,
                   delivery_keywords=["hooded watcher", "north wall", "buried"],
                   lead_target={"type": "npc", "id": sorrel.id})
    beat(1, 3, "Piper's Pattern", trigger_type="after_beat",
         trigger_data={"beat_id": watcher.id, "delay": "soon"}, status="pending")
    beat(1, 4, "The Cracked Whistle", trigger_type="location",
         trigger_data={"location_id": hollow.id}, status="delivered", reveals_truth_ids=["truth_location"])
    beat(1, 5, "The Greywater Cart Fails to Come", beat_type="deadline_event", trigger_type="time",
         trigger_data={"delay": "days"}, status="armed", scheduled_day=6, scheduled_hour=18,
         fallback_day=6, fallback_hour=18, clock_label="The Greywater cart",
         consequence_missed=[{"op": "world_event", "name": "The cart never came", "description": "No cart.",
                              "scope": "meso"}])
    payoff = beat(2, 6, "A Name on the Whistle", trigger_type="quest_status",
                  trigger_data={"quest_id": teeth.id, "status": "completed"}, status="pending",
                  lead_target={"type": "npc", "id": coldiron.id}, reveals_truth_ids=["truth_villain"],
                  directive="Coldiron goes pale and names Aldric Vane.",
                  delivery_keywords=["Aldric Vane", "drummed out"])
    beat(2, 7, "The Fence's Ledger", trigger_type="milestone", trigger_data={"milestone": "midway"},
         status="pending")
    beat(2, 8, "The Roster is a Lie", beat_type="twist", trigger_type="after_beat",
         trigger_data={"beat_id": payoff.id, "delay": "days"}, status="pending")
    beat(3, 9, "Into the Hollow", beat_type="setpiece", trigger_type="quest_status",
         trigger_data={"quest_id": teeth.id, "status": "completed"}, status="pending")
    beat(3, 10, "The Whistle's Last Note", beat_type="confrontation", trigger_type="time",
         trigger_data={"delay": "days"}, status="pending", cinematic="finale")
    session.commit()
    return {"player": player.id, "campaign": campaign.id, "quest": teeth.id, "square": square.id,
            "hollow": hollow.id, "sorrel": sorrel.id}


def _statuses(world):
    with get_session() as session:
        campaign = session.get(Campaign, world["campaign"])
        beats = {b.title: b for b in session.query(StoryBeat).all()}
        return campaign.current_act, {t: b.status for t, b in beats.items()}, {
            t: (b.scheduled_day, b.scheduled_hour) for t, b in beats.items()}


def _complete(world):
    with get_session() as session:
        session.get(Quest, world["quest"]).status = QuestStatus.COMPLETED
        session.commit()


def _set_clock(day, hour, minute=0):
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.day, clock.hour, clock.minute = day, hour, minute
        session.commit()


def test_with_the_job_still_open_act_one_waits_for_its_clocks(db):
    with get_session() as session:
        world = _bram_world(session)
    tick = run_campaign_tick(world["player"])
    act, status, _ = _statuses(world)
    assert act == 1 and not tick.get("fired_beat")
    assert status["A Name on the Whistle"] == "pending"


def test_completing_the_job_pulls_the_payoff_ahead_of_act_one_clocks(db):
    with get_session() as session:
        world = _bram_world(session)
    _complete(world)
    tick = run_campaign_tick(world["player"])
    act, status, when = _statuses(world)
    assert act == 2, "only clocks held act 1, and play met an act-2 trigger"
    assert tick["fired_beat"]["title"] == "A Name on the Whistle", "the payoff lands the same turn"
    # The clocks keep their schedules and their consequences
    assert status["The Watcher at the Wall"] == "armed" and when["The Watcher at the Wall"] == (2, 0)
    assert status["The Greywater Cart Fails to Come"] == "armed"
    assert when["The Greywater Cart Fails to Come"] == (6, 18)
    assert any(e["type"] == "campaign_act" and e["act"] == 2 for e in camp.pop_pending_events(world["player"]))
    assert status["Into the Hollow"] == "pending", "never into the finale act"


def test_leftover_clocks_still_fire_and_arm_their_successors_in_a_later_act(db):
    with get_session() as session:
        world = _bram_world(session)
    _complete(world)
    run_campaign_tick(world["player"])
    with get_session() as session:
        session.query(StoryBeat).filter(StoryBeat.title == "A Name on the Whistle").one().status = "delivered"
        session.commit()
    _set_clock(2, 1)
    tick = run_campaign_tick(world["player"])
    assert tick["fired_beat"]["title"] == "The Watcher at the Wall"
    with get_session() as session:
        session.query(StoryBeat).filter(StoryBeat.title == "The Watcher at the Wall").one().status = "delivered"
        session.commit()
    run_campaign_tick(world["player"])
    act, status, _ = _statuses(world)
    assert act == 2 and status["Piper's Pattern"] == "armed", "an act-1 after_beat still arms in act 2"
    # ...and the deadline still lands, with its missed consequence
    with get_session() as session:
        for b in session.query(StoryBeat).filter(StoryBeat.status == "armed").all():
            if b.title != "The Greywater Cart Fails to Come":
                b.status = "delivered"
        session.commit()
    _set_clock(6, 18)
    tick = run_campaign_tick(world["player"])
    assert tick["fired_beat"]["title"] == "The Greywater Cart Fails to Come"
    assert tick["fired_beat"]["variant"] == "missed"


def test_play_can_still_tell_a_beat_the_act_left_behind(db):
    """After pulling ahead, Sorrel telling the watcher at the wall in his own
    words is that act-1 beat played — it never fires later as news."""
    with get_session() as session:
        world = _bram_world(session)
    _complete(world)
    run_campaign_tick(world["player"])
    line = "A hooded watcher stood at the north wall last night, and he buried something there."
    result = camp.mark_beat_delivery(world["player"], line, speakers=["Sorrel Tack"],
                                     spoken=[{"npc_id": world["sorrel"], "npc_name": "Sorrel Tack", "text": line}])
    act, status, _ = _statuses(world)
    assert status["The Watcher at the Wall"] == "delivered" and act == 2
    assert result["played"]


def test_an_undelivered_play_beat_in_the_act_holds_it(db):
    with get_session() as session:
        world = _bram_world(session)
        whistle = session.query(StoryBeat).filter(StoryBeat.title == "The Cracked Whistle").one()
        whistle.status, whistle.reveals_truth_ids = "armed", []    # nothing play already told
        session.commit()
    _complete(world)
    run_campaign_tick(world["player"])
    assert _statuses(world)[0] == 1, "a place-triggered act-1 beat is still to be played"


def test_a_beat_awaiting_delivery_holds_the_act(db):
    with get_session() as session:
        world = _bram_world(session)
        watcher = session.query(StoryBeat).filter(StoryBeat.title == "The Watcher at the Wall").one()
        watcher.status, watcher.delivery_attempts, watcher.fired_day = "fired", 1, 1
        session.commit()
    _complete(world)
    tick = run_campaign_tick(world["player"])
    assert _statuses(world)[0] == 1
    assert tick["fired_beat"]["title"] == "The Watcher at the Wall", "one beat at a time"


def test_the_finale_act_is_never_pulled_ahead(db):
    with get_session() as session:
        world = _bram_world(session, act=2)
        for b in session.query(StoryBeat).filter(StoryBeat.act == 2).all():
            b.status = "delivered" if b.trigger_type != "after_beat" else "armed"
            if b.status == "armed":
                b.scheduled_day, b.scheduled_hour, b.fallback_day = 9, 0, 12
        session.commit()
    _complete(world)
    run_campaign_tick(world["player"])
    act, status, _ = _statuses(world)
    assert act == 2 and status["Into the Hollow"] == "pending", "act 3 waits for act 2 (the villain's hold-back)"


def test_the_payoff_never_tells_the_dm_to_bring_the_named_villain_to_the_player(db):
    """pt_l replay: the pulled-ahead "A Name on the Whistle" (Coldiron names
    Aldric Vane) said "Aldric Vane is at Wolf's Hollow … comes to them HERE"."""
    with get_session() as session:
        world = _bram_world(session)
        vane = NPC(name="Aldric Vane", current_location_id=world["hollow"], danger_tier="boss")
        session.add(vane)
        session.flush()
        session.get(Campaign, world["campaign"]).villain_npc_id = vane.id
        coldiron = session.query(NPC).filter_by(name="Captain Brann Coldiron").one()
        coldiron.current_location_id = world["hollow"]      # away too: he still comes, or it waits
        session.commit()
    _complete(world)
    fired = run_campaign_tick(world["player"])["fired_beat"]
    assert fired["title"] == "A Name on the Whistle"
    where = " ".join(n for n in fired["notes"] if n.startswith("WHERE PEOPLE ARE"))
    assert "Captain Brann Coldiron is at Wolf's Hollow" in where
    assert "Aldric Vane" not in where


# --- Stalled beats ------------------------------------------------------------

def _tamsin_world(session, *, at_hollow=False, attempts=BEAT_DELIVERY_ATTEMPTS, status="stalled",
                  whistle="She finds the cracked bone whistle."):
    """pt_m: 'A Name on Minted Silver' (lead: Wolf's Hollow) stalled in the square."""
    session.add(WorldClock(day=1, hour=21, minute=0))
    session.add(DMState())
    square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True)
    hollow = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True)
    session.add_all([square, hollow])
    session.flush()
    quest = Quest(title="Teeth on the North Road", status=QuestStatus.ACTIVE)
    session.add(quest)
    player = Player(name="Tamsin Rook", current_location_id=hollow.id if at_hollow else square.id)
    session.add(player)
    session.flush()
    campaign = Campaign(player_id=player.id, title="The Toll Road", status="active", current_act=1,
                        acts=ACTS, fixed_truths=[], last_beat_day=1, last_beat_hour=8)
    session.add(campaign)
    session.flush()
    silver = StoryBeat(campaign_id=campaign.id, act=1, sequence=2, title="A Name on Minted Silver",
                       beat_type="revelation", trigger_type="quest_status",
                       trigger_data={"quest_id": quest.id, "status": "active"},
                       directive="On the approach to Wolf's Hollow, Vex Marrow is spotted with fresh silver.",
                       status=status, fired_day=1, fired_hour=20, delivery_attempts=attempts,
                       delivery_keywords=["Vex Marrow", "minted silver"],
                       lead_target={"type": "location", "id": hollow.id})
    session.add(silver)
    session.flush()
    session.add(StoryBeat(campaign_id=campaign.id, act=1, sequence=4, title="The Whistle in Her Hand",
                          beat_type="revelation", trigger_type="after_beat",
                          trigger_data={"beat_id": silver.id, "delay": "soon"}, status="pending",
                          directive=whistle))
    session.commit()
    return {"player": player.id, "hollow": hollow.id, "square": square.id}


def _beat(title):
    with get_session() as session:
        b = session.query(StoryBeat).filter(StoryBeat.title == title).one()
        return b.status, b.delivery_attempts


def test_a_stalled_predecessor_arms_its_successor(db):
    with get_session() as session:
        world = _tamsin_world(session)
    run_campaign_tick(world["player"])
    assert _beat("The Whistle in Her Hand")[0] == "armed"


def test_a_stalled_beat_refires_once_when_the_player_reaches_its_place(db):
    with get_session() as session:
        world = _tamsin_world(session)
    assert not (run_campaign_tick(world["player"]).get("fired_beat") or {}).get("title") == "A Name on Minted Silver"
    assert _beat("A Name on Minted Silver")[0] == "stalled", "still in the square: it waits"

    with get_session() as session:
        session.get(Player, world["player"]).current_location_id = world["hollow"]
        session.commit()
    fired = run_campaign_tick(world["player"])["fired_beat"]
    assert fired["title"] == "A Name on Minted Silver" and fired["attempt"] == 1
    assert any("Deliver it HERE" in n for n in fired["notes"])
    assert "MUST DELIVER" in camp.format_campaign_context(camp.get_last_tick(world["player"]))
    assert _beat("A Name on Minted Silver") == ("fired", BEAT_DELIVERY_ATTEMPTS + 1)

    # A fresh round of attempts, then parked again — and never re-fired twice
    attempts = [run_campaign_tick(world["player"])["fired_beat"]["attempt"] for _ in range(2)]
    assert attempts == [2, 3]
    tick = run_campaign_tick(world["player"])
    assert (tick.get("fired_beat") or {}).get("title") != "A Name on Minted Silver"
    assert _beat("A Name on Minted Silver")[0] == "stalled"
    tick = run_campaign_tick(world["player"])
    assert (tick.get("fired_beat") or {}).get("title") != "A Name on Minted Silver"


def test_the_refired_beat_is_delivered_by_its_words(db):
    with get_session() as session:
        world = _tamsin_world(session, at_hollow=True)
    run_campaign_tick(world["player"])
    result = camp.mark_beat_delivery(world["player"], "Vex Marrow flips a coin of minted silver at you.")
    assert result["delivered"] is True
    assert _beat("A Name on Minted Silver")[0] == "delivered"


PT_M_WHISTLE = ("Deliver the find at Wolf's Hollow: a cracked bone whistle, carved with old Lantern Watch "
                "tally-marks, half-buried where the drilled wolves gather.")


def _move(world, where):
    with get_session() as session:
        session.get(Player, world["player"]).current_location_id = world[where]
        session.commit()


def test_a_successor_carrying_on_its_predecessors_scene_waits_for_that_place(db):
    """pt_m's 'The Whistle in Her Hand' has no lead of its own. Armed by the
    stall in the square, it must not fire there when its clock comes."""
    with get_session() as session:
        world = _tamsin_world(session, whistle=PT_M_WHISTLE)
    run_campaign_tick(world["player"])
    assert _beat("The Whistle in Her Hand")[0] == "armed"
    _set_clock(2, 8)                                    # past its 'soon', inside its backstop
    tick = run_campaign_tick(world["player"])
    assert (tick.get("fired_beat") or {}).get("title") != "The Whistle in Her Hand"
    assert _beat("The Whistle in Her Hand")[0] == "armed", "still in the square: it waits"
    _move(world, "hollow")
    first = run_campaign_tick(world["player"])["fired_beat"]
    assert first["title"] == "A Name on Minted Silver", "the stalled scene comes back first"
    camp.mark_beat_delivery(world["player"], "Vex Marrow flips a coin of minted silver at you.")
    _set_clock(2, 11)
    assert run_campaign_tick(world["player"])["fired_beat"]["title"] == "The Whistle in Her Hand"


def test_a_successor_that_stalls_elsewhere_comes_back_at_that_place(db):
    with get_session() as session:
        world = _tamsin_world(session, whistle=PT_M_WHISTLE)
        silver = session.query(StoryBeat).filter_by(title="A Name on Minted Silver").one()
        silver.status = "delivered"
        whistle = session.query(StoryBeat).filter_by(title="The Whistle in Her Hand").one()
        whistle.status, whistle.delivery_attempts, whistle.fired_day, whistle.fired_hour = \
            "fired", BEAT_DELIVERY_ATTEMPTS, 1, 20
        session.commit()
    run_campaign_tick(world["player"])
    assert _beat("The Whistle in Her Hand") == ("stalled", BEAT_DELIVERY_ATTEMPTS)
    _move(world, "hollow")
    fired = run_campaign_tick(world["player"])["fired_beat"]
    assert fired["title"] == "The Whistle in Her Hand" and any("Deliver it HERE" in n for n in fired["notes"])


@pytest.mark.parametrize("directive", [
    "She finds the cracked bone whistle.",                       # names no place
    "Piper catches her in Market Square with the whistle's tally.",  # names another place
    # Names the Hollow only as where someone heads (forge_v2 "Coldiron's Last Roster Call")
    "Have Coldiron prepare to march what's left of the Watch against Wolf's Hollow.",
])
def test_a_successor_naming_no_place_of_its_predecessors_fires_anywhere(db, directive):
    with get_session() as session:
        world = _tamsin_world(session, whistle=directive)
        session.query(StoryBeat).filter_by(title="A Name on Minted Silver").one().status = "delivered"
        session.commit()
    run_campaign_tick(world["player"])
    _set_clock(2, 8)
    assert run_campaign_tick(world["player"])["fired_beat"]["title"] == "The Whistle in Her Hand"


def test_scene_places_read_where_a_directive_sets_its_scene():
    from types import SimpleNamespace as Loc

    square, hollow = Loc(id="sq", name="Market Square"), Loc(id="wh", name="Wolf's Hollow")
    places = [square, hollow]
    assert camp._scene_places("Stage the finale at Market Square or Wolf's Hollow, wherever they end up.",
                              places) == {"sq", "wh"}                       # pt_j 3.14
    assert camp._scene_places("Aldric Vane faces Tamsin at the heart of Wolf’s Hollow.", places) == {"wh"}
    assert camp._scene_places("Have Sorrel, near the stables in the Market Square, tell her.", places) == {"sq"}
    assert camp._scene_places("Lead the Watch toward Wolf's Hollow; if Bram hasn't moved on Wolf's Hollow, "
                              "the gates close.", places) == set()        # claude_sandbox 3.10, pt_a 3.12


def test_a_beat_that_stalls_at_its_own_place_is_not_refired(db):
    with get_session() as session:
        world = _tamsin_world(session, at_hollow=True, status="fired")
    run_campaign_tick(world["player"])          # third attempt spent: it stalls right here
    assert _beat("A Name on Minted Silver") == ("stalled", 2 * BEAT_DELIVERY_ATTEMPTS)
    tick = run_campaign_tick(world["player"])
    assert (tick.get("fired_beat") or {}).get("title") != "A Name on Minted Silver"


# --- What the DM is told about sealed truths ---------------------------------

def test_the_campaign_block_forbids_promising_a_sealed_truth(db):
    with get_session() as session:
        world = _bram_world(session)
    text = camp.format_campaign_context(run_campaign_tick(world["player"]))
    assert camp.SEALED_PROMISE_LINE in text
    assert "I'll tell you what I know" in camp.SEALED_PROMISE_LINE


def test_no_promise_line_when_nothing_is_sealed(db):
    with get_session() as session:
        world = _bram_world(session)
        campaign = session.query(Campaign).one()
        campaign.fixed_truths = [dict(t, revealed=True) for t in TRUTHS]
        session.commit()
    assert camp.SEALED_PROMISE_LINE not in camp.format_campaign_context(run_campaign_tick(world["player"]))


def test_the_unrevealed_villain_names_no_price():
    """fleet5-tamsin T16: "I want his badge off every chest in that town" —
    the sealed act-2 demand, in words no keyword check could catch."""
    for line in (camp.VILLAIN_HELD_LINE, camp.VILLAIN_HELD_LINE_NAMED):
        assert "no price or demand" in line and "what would make them stop" in line


def test_stems_meet_across_doubled_consonants_and_tenses():
    assert "drum" in camp._word_forms("drummed") and "strip" in camp._word_forms("stripped")
    assert "disband" in camp._word_forms("disbanding") & camp._word_forms("disbanded")
    assert "kil" not in camp._word_forms("killed") and "pas" not in camp._word_forms("passed")
    truth = {"id": "t", "statement": "Vane was drummed out and lost his stripes.",
             "spoiler_keywords": ["drummed out", "stripes"]}
    assert camp.sealed_truth_hits([truth], "They drum a man out and cut the stripes off his sleeve.") == {
        "t": ["drummed out", "stripes"]}

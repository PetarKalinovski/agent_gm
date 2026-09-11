"""The campaign spine: a story that fires in code, not in prose.

Every test here persists a canned generator payload directly — nothing in this
module may touch the network, so `_spawn_generation` is stubbed for the whole
file and `generate_campaign_payload` is never called.
"""

import sys
import types

import pytest

from src.models import (
    NPC,
    Campaign,
    DMState,
    Event,
    Faction,
    FactionRelationship,
    Location,
    LocationType,
    NPCTier,
    Player,
    Quest,
    QuestStatus,
    StoryBeat,
    WorldClock,
    get_session,
)
from src.services import campaign as camp


# ----------------------------------------------------------------------
# Fixtures + canned payload
# ----------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_generation(monkeypatch):
    """Hard guarantee: no test in this file can start an LLM generation."""
    calls = []
    monkeypatch.setattr(camp, "_spawn_generation", lambda player_id: calls.append(player_id))
    camp.reset_state()
    yield calls
    camp.reset_state()


@pytest.fixture()
def world(player):
    """A small authored world to cast a campaign from."""
    ids = {"player": player}
    with get_session() as session:
        session.add(WorldClock(day=1, hour=8))
        session.add(DMState())

        lantern = Location(name="The Drowned Lantern", type=LocationType.BUILDING,
                           description="A tavern that smells of tar")
        kennels = Location(name="The Kennels", type=LocationType.DISTRICT,
                           description="Where the howlers are kept")
        session.add_all([lantern, kennels])

        wardens = Faction(name="The Warden's Men", ideology="Order at any price", power_level=60)
        kennel_clan = Faction(name="The Kennels", ideology="Blood pays", power_level=10)
        session.add_all([wardens, kennel_clan])
        session.flush()

        kestrel = NPC(name="Kestrel Vane", tier=NPCTier.MAJOR, profession="houndmaster",
                      faction_id=kennel_clan.id, current_location_id=kennels.id)
        piper = NPC(name="Piper Vance", tier=NPCTier.MINOR, profession="feed hauler",
                    current_location_id=kennels.id)
        halloway = NPC(name="Warden Halloway", tier=NPCTier.MAJOR, profession="warden",
                       faction_id=wardens.id, current_location_id=lantern.id)
        session.add_all([kestrel, piper, halloway])

        quest = Quest(title="Follow the Feed", description="Find who feeds the kennels",
                      objectives=["Follow a crate"], rewards={"currency": 50},
                      status=QuestStatus.NOT_STARTED, scale="mission")
        session.add(quest)
        session.flush()

        ids.update({
            "lantern": lantern.id, "kennels": kennels.id,
            "wardens": wardens.id, "kennel_clan": kennel_clan.id,
            "kestrel": kestrel.id, "piper": piper.id, "halloway": halloway.id,
            "quest": quest.id,
        })
        session.commit()
    return ids


def _truths():
    return [
        {"key": "truth_villain", "kind": "villain_identity",
         "statement": "Kestrel Vane breeds the howlers she is paid to hunt.",
         "spoiler_keywords": ["Kestrel", "breeds"]},
        {"key": "truth_betrayal", "kind": "the_betrayal",
         "statement": "Warden Halloway sold the western district to Kestrel for silence.",
         "spoiler_keywords": ["Halloway", "sold"]},
        {"key": "truth_cost", "kind": "the_cost",
         "statement": "The feed crates carry the Warden's seal.",
         "spoiler_keywords": ["seal"]},
    ]


def _beat(key, act, sequence, **kwargs):
    """A beat with every optional field defaulted the way the schema expects."""
    beat = {
        "key": key, "act": act, "sequence": sequence,
        "beat_type": "revelation", "title": f"Beat {key}",
        "directive": f"Deliver {key}.", "fixed_truths": [],
        "reveals_truth_ids": [], "delivery_keywords": [key],
        "trigger_type": "time", "trigger_data": {"delay": "soon"},
        "cinematic": "none", "consequence_met": [], "consequence_missed": [],
        "clock_label": None, "directive_missed": None, "met_condition": None,
    }
    beat.update(kwargs)
    return beat


def _filler(count, act=3, start=90):
    """Beats that pad a payload past MIN_BEATS without ever being eligible:
    act 3 is not armed until the campaign gets there."""
    return [
        _beat(f"f{i}", act, start + i, trigger_type="location",
              trigger_data={"location_id": "nowhere"})
        for i in range(count)
    ]


def _payload(world, beats=None):
    return {
        "title": "The Long Howl",
        "premise": "The howlers are bred, not born.",
        "stakes_personal": "Kestrel trained you.",
        "villain_npc_id": world["kestrel"],
        "villain_name": "Kestrel Vane",
        "villain_faction_id": world["kennel_clan"],
        "successor_npc_id": world["halloway"],
        "successor_name": "Warden Halloway",
        "ally_npc_ids": [world["piper"]],
        "acts": [
            {"act": 1, "title": "The Kennel Fire", "premise": "Something is wrong with the feed."},
            {"act": 2, "title": "The Hunt Turns", "premise": "Kestrel's hounds run openly."},
            {"act": 3, "title": "The Kennels Burn", "premise": "It ends at the gate."},
        ],
        "fixed_truths": _truths(),
        "beats": beats if beats is not None else _full_beats(world),
    }


def _full_beats(world):
    """The default seven-beat campaign: every trigger type represented."""
    return [
        _beat("b1", 1, 1, delivery_keywords=["kennel", "brand"],
              reveals_truth_ids=["truth_villain"], cinematic="revelation",
              fixed_truths=["The feed crates carry the Warden's seal."],
              consequence_met=[
                  {"op": "move_npc", "npc_id": world["piper"], "to": "player"},
                  {"op": "activate_quest", "quest_id": world["quest"]},
                  {"op": "world_event", "name": "Piper names the Warden",
                   "description": "She said it out loud.", "scope": "meso",
                   "consequences": ["The Warden knows"]},
              ]),
        _beat("b2", 1, 2, beat_type="deadline_event", title="The Howl Returns",
              clock_label="The Howl returns", trigger_data={"deadline": "short"},
              trigger_type="time", delivery_keywords=["howl"],
              directive_missed="The howlers come through the western gate.",
              met_condition={"check": "quest_status", "quest_id": world["quest"],
                             "status": "completed"},
              consequence_met=[
                  {"op": "faction_power", "faction_id": world["wardens"], "shift": "weakened"},
              ],
              consequence_missed=[
                  {"op": "npc_status", "npc_id": world["piper"], "status": "dead"},
                  {"op": "fail_quest", "quest_id": world["quest"]},
                  {"op": "faction_relationship", "faction_a_id": world["wardens"],
                   "faction_b_id": world["kennel_clan"], "type": "war"},
                  {"op": "world_event", "name": "The western district falls",
                   "description": "Gate down.", "scope": "macro"},
                  {"op": "tension_floor", "level": "high"},
              ]),
        _beat("b3", 1, 3, trigger_type="location",
              trigger_data={"location_id": world["kennels"]},
              delivery_keywords=["hounds"]),
        _beat("b4", 2, 1, trigger_type="quest_status",
              trigger_data={"quest_id": world["quest"], "status": "completed"},
              delivery_keywords=["crates"]),
        _beat("b5", 2, 2, trigger_type="milestone", trigger_data={"milestone": "early"},
              delivery_keywords=["ledger"]),
        _beat("b6", 2, 3, trigger_type="after_beat",
              trigger_data={"beat_id": "b4", "delay": "soon"},
              delivery_keywords=["gate"]),
        _beat("b7", 3, 1, beat_type="confrontation", title="The Kennels Burn",
              cinematic="finale", delivery_keywords=["burn"]),
    ]


def _set_clock(day, hour=8):
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.day, clock.hour = day, hour
        session.commit()


def _beats():
    with get_session() as session:
        return {b.title: b for b in session.query(StoryBeat).all()}


def _beat_row(title):
    with get_session() as session:
        return session.query(StoryBeat).filter(StoryBeat.title == title).first()


def _campaign_row(player_id):
    with get_session() as session:
        return camp.get_campaign_row(session, player_id)


# ----------------------------------------------------------------------
# Legacy worlds
# ----------------------------------------------------------------------

def test_world_without_campaign_ticks_clean(player, no_generation):
    assert camp.run_campaign_tick(player) == {}
    assert camp.format_campaign_context({}) == ""
    assert camp.format_campaign_context(None) == ""
    assert camp.campaign_state(player) is None
    assert camp.campaign_public_view(player) == {"campaign": None}
    # ...and the missing campaign is authored in the background, never inline
    assert no_generation == [player]


# ----------------------------------------------------------------------
# Persistence + validation
# ----------------------------------------------------------------------

def test_persist_campaign_creates_beats_and_arms_act_one(world):
    result = camp.persist_campaign(world["player"], _payload(world))
    assert result["status"] == "active" and result["beats"] == 7

    campaign = _campaign_row(world["player"])
    assert campaign.status == "active" and campaign.current_act == 1
    assert len(campaign.acts) == 3
    assert campaign.villain_npc_id == world["kestrel"]

    rows = _beats()
    # act 1 is armed with a hard backstop; later acts stay pending
    assert rows["Beat b1"].status == "armed" and rows["Beat b1"].fallback_day is not None
    assert rows["The Howl Returns"].status == "armed"
    assert rows["Beat b4"].status == "pending"
    assert rows["The Kennels Burn"].status == "pending"

    with get_session() as session:
        dm_state = session.query(DMState).first()
    assert dm_state.current_arc.startswith("[CAMPAIGN]")
    assert "The Howl returns" in (dm_state.world_pressures or [])
    assert "Kestrel Vane" in (dm_state.active_threats or [])


def test_persist_drops_unknown_entity_ids(world):
    beats = _full_beats(world)
    beats[0]["consequence_met"] = [
        {"op": "npc_mood", "npc_id": "ghost-id", "mood": "furious"},
        {"op": "world_event", "name": "Still happens", "description": "x", "scope": "meso"},
    ]
    payload = _payload(world, beats)
    payload["villain_npc_id"] = "not-a-real-npc"
    payload["villain_name"] = "Someone Not In The Roster"
    payload["ally_npc_ids"] = ["ghost-id"]

    assert camp.persist_campaign(world["player"], payload)["status"] == "active"

    beat = _beat_row("Beat b1")
    assert [op["op"] for op in beat.consequence_met] == ["world_event"]

    campaign = _campaign_row(world["player"])
    assert campaign.villain_npc_id is None
    assert campaign.villain_name == "Someone Not In The Roster"
    assert campaign.ally_npc_ids == []


def test_persist_downgrades_deadline_without_variants(world):
    beats = _full_beats(world)
    beats[1]["consequence_missed"] = []
    camp.persist_campaign(world["player"], _payload(world, beats))

    beat = _beat_row("The Howl Returns")
    assert beat.beat_type == "setpiece" and beat.clock_label is None


def test_persist_downgrades_unresolvable_after_beat(world):
    beats = _full_beats(world)
    beats[5]["trigger_data"] = {"beat_id": "no-such-key", "delay": "soon"}
    camp.persist_campaign(world["player"], _payload(world, beats))

    beat = _beat_row("Beat b6")
    assert beat.trigger_type == "time" and beat.trigger_data == {"delay": "days"}


def test_persist_fails_when_too_few_beats_survive(world):
    beats = [_beat("b1", 1, 1), {"beat_type": "nonsense", "trigger_type": "time"}]
    result = camp.persist_campaign(world["player"], _payload(world, beats))
    assert result["status"] == "failed"

    campaign = _campaign_row(world["player"])
    assert campaign.status == "failed" and campaign.generation_error
    assert camp.run_campaign_tick(world["player"]) == {}


# ----------------------------------------------------------------------
# Triggers
# ----------------------------------------------------------------------

def test_time_beat_fires_once_on_schedule(world):
    beats = [_beat("b1", 1, 1, delivery_keywords=["kennel"])] + _filler(5)
    camp.persist_campaign(world["player"], _payload(world, beats))

    assert camp.run_campaign_tick(world["player"])["fired_beat"] is None  # 6h away

    _set_clock(1, 14)
    fired = camp.run_campaign_tick(world["player"])["fired_beat"]
    assert fired["title"] == "Beat b1" and fired["attempt"] == 1

    # Undelivered: re-surfaced as the SAME beat, not fired a second time
    again = camp.run_campaign_tick(world["player"])["fired_beat"]
    assert again["beat_id"] == fired["beat_id"] and again["attempt"] == 2
    assert _beat_row("Beat b1").fired_day == 1


def test_location_beat_fires_on_entry(world):
    beats = [_beat("b1", 1, 1, trigger_type="location",
                   trigger_data={"location_id": world["kennels"]})] + _filler(5)
    camp.persist_campaign(world["player"], _payload(world, beats))

    assert camp.run_campaign_tick(world["player"])["fired_beat"] is None

    with get_session() as session:
        session.get(Player, world["player"]).current_location_id = world["kennels"]
        session.commit()

    assert camp.run_campaign_tick(world["player"])["fired_beat"]["title"] == "Beat b1"


def test_quest_status_beat_fires_on_completion(world):
    beats = [_beat("b1", 1, 1, trigger_type="quest_status",
                   trigger_data={"quest_id": world["quest"], "status": "completed"})] + _filler(5)
    camp.persist_campaign(world["player"], _payload(world, beats))

    assert camp.run_campaign_tick(world["player"])["fired_beat"] is None

    with get_session() as session:
        session.get(Quest, world["quest"]).status = QuestStatus.COMPLETED
        session.commit()

    assert camp.run_campaign_tick(world["player"])["fired_beat"]["title"] == "Beat b1"


def test_milestone_beat_fires_on_completed_quest_count(world):
    beats = [_beat("b1", 1, 1, trigger_type="milestone",
                   trigger_data={"milestone": "early"})] + _filler(5)
    camp.persist_campaign(world["player"], _payload(world, beats))

    assert camp.run_campaign_tick(world["player"])["fired_beat"] is None

    with get_session() as session:
        session.get(Player, world["player"]).completed_quests = [world["quest"]]
        session.commit()

    assert camp.run_campaign_tick(world["player"])["fired_beat"]["title"] == "Beat b1"


def test_after_beat_arms_when_prerequisite_is_delivered(world):
    beats = [
        _beat("b1", 1, 1, delivery_keywords=["kennel"]),
        _beat("b2", 1, 2, trigger_type="after_beat",
              trigger_data={"beat_id": "b1", "delay": "immediate"},
              delivery_keywords=["gate"]),
    ] + _filler(4)
    camp.persist_campaign(world["player"], _payload(world, beats))
    assert _beat_row("Beat b2").status == "pending"

    _set_clock(1, 14)
    camp.run_campaign_tick(world["player"])
    camp.mark_beat_delivery(world["player"], "The kennel doors stand open.")

    _set_clock(1, 20)
    assert camp.run_campaign_tick(world["player"])["fired_beat"]["title"] == "Beat b2"


def test_after_beat_with_an_unreachable_prerequisite_still_arms(world):
    """A prerequisite that can never land must not stall the act forever."""
    beats = [
        _beat("b1", 1, 1, trigger_type="after_beat",
              trigger_data={"beat_id": "b2", "delay": "immediate"}),
        _beat("b2", 2, 1),  # authored into a LATER act — unreachable from act 1
    ] + _filler(4)
    camp.persist_campaign(world["player"], _payload(world, beats))

    assert camp.run_campaign_tick(world["player"])["fired_beat"]["title"] == "Beat b1"


def test_fallback_deadline_fires_stalled_beat(world):
    beats = [_beat("b1", 1, 1, trigger_type="location",
                   trigger_data={"location_id": world["kennels"]})] + _filler(5)
    camp.persist_campaign(world["player"], _payload(world, beats))

    _set_clock(1 + camp.FALLBACK_DAYS, 8)
    assert camp.run_campaign_tick(world["player"])["fired_beat"]["title"] == "Beat b1"


# ----------------------------------------------------------------------
# Pacing
# ----------------------------------------------------------------------

def test_only_one_beat_fires_per_tick_and_priority_order(world):
    beats = [
        _beat("b1", 1, 1),
        _beat("b2", 1, 2, beat_type="setpiece"),
        _beat("b3", 1, 3, beat_type="deadline_event", title="The Deadline",
              clock_label="clock", trigger_data={"deadline": "short"},
              met_condition={"check": "npc_status", "npc_id": world["kestrel"], "status": "dead"},
              consequence_missed=[{"op": "npc_mood", "npc_id": world["piper"], "mood": "afraid"}]),
    ] + _filler(3)
    camp.persist_campaign(world["player"], _payload(world, beats))

    _set_clock(1 + camp.DEADLINE_DAYS["short"], 8)
    tick = camp.run_campaign_tick(world["player"])
    assert tick["fired_beat"]["title"] == "The Deadline"
    assert len([b for b in _beats().values() if b.status == "fired"]) == 1


def test_beat_spacing_blocks_pileup(world):
    beats = [
        _beat("b1", 1, 1, delivery_keywords=["kennel"]),
        _beat("b2", 1, 2, delivery_keywords=["gate"]),
        _beat("b3", 1, 3, beat_type="deadline_event", title="The Deadline",
              clock_label="clock", trigger_data={"deadline": "short"},
              delivery_keywords=["howl"],
              met_condition={"check": "npc_status", "npc_id": world["kestrel"], "status": "dead"},
              consequence_missed=[{"op": "npc_mood", "npc_id": world["piper"], "mood": "afraid"}]),
    ] + _filler(3)
    camp.persist_campaign(world["player"], _payload(world, beats))

    _set_clock(1 + camp.DEADLINE_DAYS["short"], 8)
    first = camp.run_campaign_tick(world["player"])["fired_beat"]
    camp.mark_beat_delivery(world["player"], first["delivery_keywords"][0])

    # One hour later: the spacing guard holds the second ordinary beat back
    _set_clock(1 + camp.DEADLINE_DAYS["short"], 9)
    assert camp.run_campaign_tick(world["player"])["fired_beat"] is None

    _set_clock(1 + camp.DEADLINE_DAYS["short"], 8 + camp.CAMPAIGN_BEAT_SPACING_HOURS)
    assert camp.run_campaign_tick(world["player"])["fired_beat"] is not None


def test_deadline_ignores_spacing(world):
    beats = [
        _beat("b1", 1, 1, delivery_keywords=["kennel"]),
        _beat("b2", 1, 2, beat_type="deadline_event", title="The Deadline",
              clock_label="clock", trigger_data={"deadline": "short"},
              delivery_keywords=["howl"],
              met_condition={"check": "npc_status", "npc_id": world["kestrel"], "status": "dead"},
              consequence_missed=[{"op": "npc_mood", "npc_id": world["piper"], "mood": "afraid"}]),
    ] + _filler(4)
    camp.persist_campaign(world["player"], _payload(world, beats))

    _set_clock(1, 14)
    assert camp.run_campaign_tick(world["player"])["fired_beat"]["title"] == "Beat b1"
    camp.mark_beat_delivery(world["player"], "kennel")

    _set_clock(1 + camp.DEADLINE_DAYS["short"], 8)
    assert camp.run_campaign_tick(world["player"])["fired_beat"]["title"] == "The Deadline"


# ----------------------------------------------------------------------
# Consequences
# ----------------------------------------------------------------------

def test_deadline_met_applies_met_ops(world):
    camp.persist_campaign(world["player"], _payload(world))
    with get_session() as session:
        session.get(Quest, world["quest"]).status = QuestStatus.COMPLETED
        session.commit()

    _set_clock(1 + camp.DEADLINE_DAYS["short"], 8)
    fired = camp.run_campaign_tick(world["player"])["fired_beat"]
    assert fired["title"] == "The Howl Returns" and fired["variant"] == "met"

    with get_session() as session:
        assert session.get(Faction, world["wardens"]).power_level == 60 - 15
        assert session.get(NPC, world["piper"]).status == "alive"


def test_deadline_missed_applies_missed_ops(world):
    camp.persist_campaign(world["player"], _payload(world))

    _set_clock(1 + camp.DEADLINE_DAYS["short"], 8)
    fired = camp.run_campaign_tick(world["player"])["fired_beat"]
    assert fired["variant"] == "missed"
    assert fired["directive"] == "The howlers come through the western gate."
    assert fired["cinematic"] == "countdown_expired"

    with get_session() as session:
        assert session.get(NPC, world["piper"]).status == "dead"
        assert session.get(Quest, world["quest"]).status == QuestStatus.FAILED
        link = session.query(FactionRelationship).first()
        assert link.relationship_type == "war"
        assert session.query(Event).filter(Event.name == "The western district falls").first()
        assert session.query(DMState).first().tension == "high"


def test_faction_power_shift_is_code_owned_and_clamped(world):
    beats = [_beat("b1", 1, 1, consequence_met=[
        {"op": "faction_power", "faction_id": world["kennel_clan"], "shift": "weakened"},
    ])] + _filler(5)
    camp.persist_campaign(world["player"], _payload(world, beats))

    _set_clock(1, 14)
    fired = camp.run_campaign_tick(world["player"])["fired_beat"]
    assert "The Kennels are weakened" in fired["applied_effects"]

    with get_session() as session:  # 10 - 15 would be negative; clamped to 1
        assert session.get(Faction, world["kennel_clan"]).power_level == 1


def test_give_item_adds_single_quantity(world):
    beats = [_beat("b1", 1, 1, consequence_met=[
        {"op": "give_item", "item_name": "Sealed Writ", "description": "Wax, unbroken."},
    ])] + _filler(5)
    camp.persist_campaign(world["player"], _payload(world, beats))

    _set_clock(1, 14)
    camp.run_campaign_tick(world["player"])

    with get_session() as session:
        inventory = session.get(Player, world["player"]).inventory
    assert len(inventory) == 1
    assert inventory[0]["name"] == "Sealed Writ" and inventory[0]["quantity"] == 1


def test_move_npc_to_player_follows_the_player(world):
    beats = [_beat("b1", 1, 1, consequence_met=[
        {"op": "move_npc", "npc_id": world["piper"], "to": "player"},
    ])] + _filler(5)
    camp.persist_campaign(world["player"], _payload(world, beats))
    with get_session() as session:
        session.get(Player, world["player"]).current_location_id = world["lantern"]
        session.commit()

    _set_clock(1, 14)
    fired = camp.run_campaign_tick(world["player"])["fired_beat"]
    assert fired["applied_effects"] == ["Piper Vance arrived at The Drowned Lantern"]

    with get_session() as session:
        assert session.get(NPC, world["piper"]).current_location_id == world["lantern"]


def test_quest_ops_are_audit_exempt_and_emit_events(world):
    camp.persist_campaign(world["player"], _payload(world))

    _set_clock(1, 14)
    tick = camp.run_campaign_tick(world["player"])
    fired = tick["fired_beat"]
    assert "quest" in fired["audit_exempt"] and "consequence" in fired["audit_exempt"]
    types = [e["type"] for e in tick["events"]]
    assert "quest_activated" in types and "campaign_beat" in types
    # ...and the SAME events are queued for the SSE drain
    assert [e["type"] for e in camp.pop_pending_events(world["player"])] == types
    assert camp.pop_pending_events(world["player"]) == []


def test_tension_floor_op_routes_through_the_tension_service(world):
    beats = [_beat("b1", 1, 1, consequence_met=[
        {"op": "tension_floor", "level": "climax"},
    ])] + _filler(5)
    camp.persist_campaign(world["player"], _payload(world, beats))

    _set_clock(1, 14)
    fired = camp.run_campaign_tick(world["player"])["fired_beat"]
    assert fired["applied_effects"] == []  # invisible plumbing, not a story fact

    with get_session() as session:
        assert session.query(DMState).first().tension == "climax"


# ----------------------------------------------------------------------
# Delivery, acts, resolution
# ----------------------------------------------------------------------

def test_beat_delivery_by_keyword_reveals_truths(world):
    beats = [_beat("b1", 1, 1, delivery_keywords=["Kestrel", "brand"],
                   reveals_truth_ids=["truth_villain"])] + _filler(5)
    camp.persist_campaign(world["player"], _payload(world, beats))
    _set_clock(1, 14)
    camp.run_campaign_tick(world["player"])

    result = camp.mark_beat_delivery(world["player"], "The brand on the crate is fresh.")
    assert result["delivered"] is True

    assert _beat_row("Beat b1").status == "delivered"
    campaign = _campaign_row(world["player"])
    truth = next(t for t in campaign.fixed_truths if t["id"] == "truth_villain")
    assert truth["revealed"] is True and truth["revealed_day"] == 1
    with get_session() as session:
        assert "Beat b1" in (session.query(DMState).first().completed_beats or [])


def test_delivery_without_keywords_leaves_the_beat_fired(world):
    beats = [_beat("b1", 1, 1, delivery_keywords=["Kestrel"])] + _filler(5)
    camp.persist_campaign(world["player"], _payload(world, beats))
    _set_clock(1, 14)
    camp.run_campaign_tick(world["player"])

    assert camp.mark_beat_delivery(world["player"], "You buy a pie.")["delivered"] is False
    assert _beat_row("Beat b1").status == "fired"


def test_beat_redelivery_then_giveup(world):
    beats = [_beat("b1", 1, 1, delivery_keywords=["Kestrel"])] + _filler(5)
    camp.persist_campaign(world["player"], _payload(world, beats))
    _set_clock(1, 14)

    attempts = [camp.run_campaign_tick(world["player"])["fired_beat"]["attempt"]]
    for _ in range(2):
        camp.mark_beat_delivery(world["player"], "nothing relevant")
        attempts.append(camp.run_campaign_tick(world["player"])["fired_beat"]["attempt"])
    assert attempts == [1, 2, 3]

    camp.mark_beat_delivery(world["player"], "still nothing")
    assert camp.run_campaign_tick(world["player"])["fired_beat"] is None
    assert _beat_row("Beat b1").status == "delivered"


def test_act_transition_arms_next_act(world):
    beats = [_beat("b1", 1, 1, delivery_keywords=["kennel"]),
             _beat("b2", 2, 1, delivery_keywords=["gate"])] + _filler(4)
    camp.persist_campaign(world["player"], _payload(world, beats))
    _set_clock(1, 14)
    camp.run_campaign_tick(world["player"])

    result = camp.mark_beat_delivery(world["player"], "The kennel is empty.")
    assert result["act_advanced"] == 2
    assert _campaign_row(world["player"]).current_act == 2
    assert _beat_row("Beat b2").status == "armed"

    event = next(e for e in result["events"] if e["type"] == "campaign_act")
    assert event["cinematic"] == "act_open" and event["act_title"] == "The Hunt Turns"


def test_finale_resolves_campaign(world):
    beats = [_beat("b1", 1, 1, delivery_keywords=["kennel"]),
             _beat("b2", 2, 1, delivery_keywords=["gate"]),
             _beat("b3", 3, 1, beat_type="confrontation", cinematic="finale",
                   delivery_keywords=["burn"])] + _filler(3, act=1, start=50)
    # the act-1 fillers must also be delivered before act 2 opens
    camp.persist_campaign(world["player"], _payload(world, beats))

    for day, text in ((1, "kennel"), (11, "f0"), (21, "f1"), (31, "f2")):
        _set_clock(day, 14)
        camp.run_campaign_tick(world["player"])
        camp.mark_beat_delivery(world["player"], text)
    assert _campaign_row(world["player"]).current_act == 2

    _set_clock(32, 14)
    camp.run_campaign_tick(world["player"])
    camp.mark_beat_delivery(world["player"], "gate")
    assert _campaign_row(world["player"]).current_act == 3

    _set_clock(33, 14)
    assert camp.run_campaign_tick(world["player"])["fired_beat"]["title"] == "Beat b3"
    result = camp.mark_beat_delivery(world["player"], "The kennels burn.")

    campaign = _campaign_row(world["player"])
    assert campaign.status == "won"
    resolved = next(e for e in result["events"] if e["type"] == "campaign_resolved")
    assert resolved["cinematic"] == "finale" and resolved["status"] == "won"
    # a resolved campaign fires nothing more
    assert camp.run_campaign_tick(world["player"]) == {}


# ----------------------------------------------------------------------
# Villain integrity
# ----------------------------------------------------------------------

def test_villain_death_swaps_successor(world):
    camp.persist_campaign(world["player"], _payload(world))
    with get_session() as session:
        session.get(NPC, world["kestrel"]).status = "dead"
        session.commit()

    camp.run_campaign_tick(world["player"])

    campaign = _campaign_row(world["player"])
    assert campaign.status == "active"
    assert campaign.villain_npc_id == world["halloway"]
    assert campaign.villain_name == "Warden Halloway"
    assert campaign.successor_npc_id is None
    with get_session() as session:
        assert session.query(Event).filter(Event.name == "The mantle passes").first()


def test_villain_death_without_successor_forces_finale(world):
    payload = _payload(world)
    payload["successor_npc_id"] = None
    payload["successor_name"] = ""
    camp.persist_campaign(world["player"], payload)
    with get_session() as session:
        session.get(NPC, world["kestrel"]).status = "dead"
        session.commit()

    fired = camp.run_campaign_tick(world["player"])["fired_beat"]

    assert _campaign_row(world["player"]).current_act == 3
    rows = _beats()
    assert rows["Beat b1"].status == "skipped" and rows["Beat b4"].status == "skipped"
    assert fired["title"] == "The Kennels Burn" and fired["variant"] == "met"


# ----------------------------------------------------------------------
# The DM context block
# ----------------------------------------------------------------------

def test_context_block_hides_unfired_beats(world):
    camp.persist_campaign(world["player"], _payload(world))
    _set_clock(1, 14)
    tick = camp.run_campaign_tick(world["player"])
    block = camp.format_campaign_context(tick)

    assert "### CAMPAIGN: The Long Howl" in block
    assert "THIS TURN — MUST DELIVER" in block
    assert "Deliver b1." in block
    assert "ALREADY APPLIED BY THE ENGINE" in block
    assert 'Quest activated: "Follow the Feed"' in block
    # nothing that has not fired may leak
    for hidden in ("Beat b3", "Beat b4", "Beat b5", "The Kennels Burn", "Deliver b4."):
        assert hidden not in block


def test_context_block_marks_sealed_truths(world):
    beats = [_beat("b1", 1, 1, delivery_keywords=["kennel"],
                   reveals_truth_ids=["truth_villain"])] + _filler(5)
    camp.persist_campaign(world["player"], _payload(world, beats))
    _set_clock(1, 14)
    camp.run_campaign_tick(world["player"])
    camp.mark_beat_delivery(world["player"], "the kennel burns")

    block = camp.format_campaign_context(camp.run_campaign_tick(world["player"]))
    assert "[KNOWN]  villain_identity: Kestrel Vane breeds" in block
    assert "[SEALED] the_betrayal:" in block
    assert "The player does NOT know this." in block
    assert "Story so far (already delivered): Beat b1" in block


def test_context_block_shows_clocks_and_resurface_warning(world):
    camp.persist_campaign(world["player"], _payload(world))
    _set_clock(1, 14)
    camp.run_campaign_tick(world["player"])
    camp.mark_beat_delivery(world["player"], "nothing")

    block = camp.format_campaign_context(camp.run_campaign_tick(world["player"]))
    assert "CLOCKS — visible countdowns" in block
    assert "The Howl returns:" in block
    assert "have not delivered it" in block


# ----------------------------------------------------------------------
# Read models
# ----------------------------------------------------------------------

def test_campaign_state_clock_urgency(world):
    camp.persist_campaign(world["player"], _payload(world))

    state = camp.campaign_state(world["player"])
    assert state["title"] == "The Long Howl" and state["act"] == 1
    assert state["beats_total"] == 3 and state["beats_delivered"] == 0
    clock = state["clocks"][0]
    assert clock["label"] == "The Howl returns"
    assert clock["days_left"] == 2 and clock["urgency"] == "soon"

    _set_clock(1 + camp.DEADLINE_DAYS["short"], 2)
    clock = camp.campaign_state(world["player"])["clocks"][0]
    assert clock["days_left"] == 0 and clock["hours_left"] == 6
    assert clock["urgency"] == "imminent"

    _set_clock(1 + camp.DEADLINE_DAYS["short"], 12)
    assert camp.campaign_state(world["player"])["clocks"][0]["urgency"] == "overdue"


def test_public_view_redacts_sealed_truths(world):
    camp.persist_campaign(world["player"], _payload(world))
    _set_clock(1, 14)
    camp.run_campaign_tick(world["player"])
    camp.mark_beat_delivery(world["player"], "the kennel brand")

    view = camp.campaign_public_view(world["player"])
    blob = str(view)
    assert "Kestrel Vane breeds the howlers" in blob      # revealed by the beat
    assert "Warden Halloway sold the western district" not in blob
    assert view["sealed_truth_count"] == 2
    assert view["delivered_beats"][0]["title"] == "Beat b1"

    spoiled = camp.campaign_public_view(world["player"], spoilers=True)
    assert "Warden Halloway sold the western district" in str(spoiled)


def test_dm_view_shows_sealed_truths_the_player_may_not_see(world):
    """`get_campaign` is a read tool: the DM must know what it is keeping."""
    camp.persist_campaign(world["player"], _payload(world))
    _set_clock(1, 14)
    camp.run_campaign_tick(world["player"])
    camp.mark_beat_delivery(world["player"], "the kennel brand")

    view = camp.campaign_dm_view(world["player"])
    kinds = {t["kind"]: t for t in view["truths"]}
    assert kinds["villain_identity"]["revealed"] is True
    assert kinds["the_betrayal"]["revealed"] is False
    assert "sold the western district" in kinds["the_betrayal"]["statement"]
    assert view["villain_name"] == "Kestrel Vane"
    assert [b["title"] for b in view["delivered_beats"]] == ["Beat b1"]
    assert camp.campaign_dm_view("no-such-player") == {"campaign": None}


# ----------------------------------------------------------------------
# Cinematics hand-off (integration doc R9)
# ----------------------------------------------------------------------

def test_campaign_cinematic_labels_are_deferred_to_cinematics(world, monkeypatch):
    calls = []
    fake = types.ModuleType("src.services.cinematics")
    fake.CAMPAIGN_CINEMATIC_MAP = {
        "act_open": "title_card", "finale": "title_card",
        "countdown_expired": "title_card", "revelation": None,
        "betrayal": None, "arrival": None, "none": None,
    }
    fake.request_cinematic = lambda kind, **kw: calls.append((kind, kw))
    monkeypatch.setitem(sys.modules, "src.services.cinematics", fake)

    camp.persist_campaign(world["player"], _payload(world))
    _set_clock(1, 14)
    camp.run_campaign_tick(world["player"])          # b1: cinematic "revelation"
    assert calls == []                               # carried by toast + prose in v1

    _set_clock(1 + camp.DEADLINE_DAYS["short"], 8)
    camp.mark_beat_delivery(world["player"], "kennel brand")
    camp.run_campaign_tick(world["player"])          # the missed deadline

    assert calls, "a countdown_expired beat must queue a title card"
    kind, kwargs = calls[0]
    assert kind == "title_card"
    assert kwargs["source"] == "campaign" and kwargs["defer"] is True
    assert kwargs["title"] == "The Howl returns" and kwargs["subtitle"] == "Too late"


def test_every_campaign_cinematic_label_renders_card_text(world):
    """Whatever the label, the campaign only ever hands over strings."""
    from src.models.campaign import CINEMATIC_LABELS

    camp.persist_campaign(world["player"], _payload(world))
    with get_session() as session:
        campaign = camp.get_campaign_row(session, world["player"])
        for label in CINEMATIC_LABELS:
            request = camp._cinematic_request(label, campaign, 2, "The Howl returns", "met")
            if label == "none":
                assert request is None
                continue
            assert request[0] == label
            assert isinstance(request[1], str) and request[1]
            assert isinstance(request[2], str)


def test_missing_cinematics_module_never_breaks_a_turn(world, monkeypatch):
    monkeypatch.setitem(sys.modules, "src.services.cinematics", None)
    camp.persist_campaign(world["player"], _payload(world))
    _set_clock(1, 14)
    assert camp.run_campaign_tick(world["player"])["fired_beat"]["title"] == "Beat b1"


# ----------------------------------------------------------------------
# Quest completion — one path for the DM tool and the beat op (R12)
# ----------------------------------------------------------------------

def test_complete_quest_op_pays_the_same_as_the_dm_tool(world):
    from src.services import progression as prog
    from src.tools.world_write.quests import update_quest_status

    beats = [_beat("b1", 1, 1, consequence_met=[
        {"op": "complete_quest", "quest_id": world["quest"]},
    ])] + _filler(5)
    camp.persist_campaign(world["player"], _payload(world, beats))

    _set_clock(1, 14)
    fired = camp.run_campaign_tick(world["player"])["fired_beat"]
    assert fired["applied_effects"][0].startswith('Quest completed: "Follow the Feed"')

    with get_session() as session:
        engine_player = session.get(Player, world["player"])
        engine_state = (engine_player.xp, engine_player.level, engine_player.currency,
                        list(engine_player.completed_quests or []))
        # a "mission" quest is worth its scale in XP, and rewards are applied
        assert engine_state[0] == prog.QUEST_XP["mission"]
        assert engine_state[2] == 150  # 100 starting + 50 reward

    # The DM tool on an identical quest lands on identical numbers
    with get_session() as session:
        twin = Quest(title="Twin", description="", objectives=[],
                     rewards={"currency": 50}, status=QuestStatus.ACTIVE, scale="mission")
        session.add(twin)
        session.commit()
        twin_id = twin.id
    with get_session() as session:
        p = session.get(Player, world["player"])
        p.xp, p.level, p.currency, p.completed_quests = 0, 1, 100, []
        session.commit()

    result = update_quest_status(twin_id, "completed")
    assert result["progression"]["xp_gained"] == prog.QUEST_XP["mission"]
    with get_session() as session:
        p = session.get(Player, world["player"])
        assert (p.xp, p.level, p.currency) == engine_state[:3]


def test_complete_quest_is_idempotent(world):
    from src.tools.world_write.quests import update_quest_status

    with get_session() as session:
        session.get(Quest, world["quest"]).status = QuestStatus.ACTIVE
        session.commit()

    update_quest_status(world["quest"], "completed")
    with get_session() as session:
        first = (session.get(Player, world["player"]).xp,
                 session.get(Player, world["player"]).currency)

    again = update_quest_status(world["quest"], "completed")
    assert "progression" not in again and "rewards_applied" not in again
    with get_session() as session:
        p = session.get(Player, world["player"])
        assert (p.xp, p.currency) == first


def test_quest_rewards_cannot_inject_xp_through_a_quest(world):
    from src.tools.world_write.quests import update_quest_status

    with get_session() as session:
        quest = session.get(Quest, world["quest"])
        quest.status = QuestStatus.ACTIVE
        quest.rewards = {"xp": 9999, "currency": 10}
        session.commit()

    result = update_quest_status(world["quest"], "completed")
    assert result["progression"]["xp_gained"] == 220  # the scale label, not the DM's 9999
    with get_session() as session:
        assert session.get(Player, world["player"]).xp == 220


# ----------------------------------------------------------------------
# Generation orchestration (no network — the generator is injected)
# ----------------------------------------------------------------------

def test_tick_stays_inside_its_latency_budget(world):
    """The tick runs on the hot path, inside `_build_context`. It is pure
    SQLite and must stay a rounding error next to the DM call."""
    import time

    camp.persist_campaign(world["player"], _payload(world))
    camp.run_campaign_tick(world["player"])  # warm the connection pool

    timings = []
    for _ in range(5):
        start = time.perf_counter()
        camp.run_campaign_tick(world["player"])
        timings.append(time.perf_counter() - start)
    best = min(timings)
    assert best < 0.05, f"campaign tick took {best * 1000:.1f}ms"


def test_ensure_campaign_claims_a_row_then_persists(world):
    payload = _payload(world)
    seen = {}

    def fake_generate(brief):
        seen["brief"] = brief
        return payload

    result = camp.ensure_campaign(world["player"], generate=fake_generate, background=False)
    assert result["status"] == "active"
    assert "Kestrel Vane" in seen["brief"] and world["kestrel"] in seen["brief"]

    # a second call no-ops instead of re-authoring
    assert camp.ensure_campaign(world["player"], generate=fake_generate,
                                background=False)["status"] == "ready"
    with get_session() as session:
        assert session.query(Campaign).count() == 1


def test_ensure_campaign_marks_failure_and_the_world_plays_on(world):
    def boom(brief):
        raise RuntimeError("model said no")

    result = camp.ensure_campaign(world["player"], generate=boom, background=False)
    assert result["status"] == "failed"
    assert _campaign_row(world["player"]).generation_error == "model said no"
    assert camp.run_campaign_tick(world["player"]) == {}
    assert camp.campaign_state(world["player"]) is None


# ----------------------------------------------------------------------
# The read tool (there is deliberately no write tool)
# ----------------------------------------------------------------------


def test_get_campaign_tool_hands_the_dm_the_dm_view(world):
    from src.tools.world_read.campaign import get_campaign

    camp.persist_campaign(world["player"], _payload(world))
    assert get_campaign(world["player"]) == camp.campaign_dm_view(world["player"])


def test_get_campaign_tool_is_quiet_without_a_campaign(world):
    from src.tools.world_read.campaign import get_campaign

    assert get_campaign(world["player"]) == {"campaign": None}


def test_no_campaign_write_tool_exists():
    """The DM physically cannot author or alter a beat, a truth or an act."""
    import src.tools.world_write as ww

    assert not [name for name in ww.__all__ if "campaign" in name.lower()]


# ---------------------------------------------------------------------------
# A pending row that never finished is regenerated, not honoured forever
# ---------------------------------------------------------------------------

def test_stale_pending_campaign_is_regenerated(db):
    from datetime import datetime, timedelta

    from src.models import Location, LocationType, Player, get_session
    from src.models.campaign import Campaign
    from src.services import campaign as campaign_service

    with get_session() as session:
        loc = Location(name="Square", type=LocationType.DISTRICT)
        session.add(loc)
        session.flush()
        player = Player(name="Rook", current_location_id=loc.id)
        session.add(player)
        session.flush()
        corpse = Campaign(player_id=player.id, status="pending",
                          created_at=datetime.utcnow() - timedelta(minutes=30))
        session.add(corpse)
        session.commit()
        player_id, corpse_id = player.id, corpse.id

    calls = []

    def fake_generate(brief):
        calls.append(brief)
        raise RuntimeError("stop here — the claim is what we test")

    result = campaign_service.ensure_campaign(player_id, generate=fake_generate, background=False)

    assert calls, "a stale pending row must trigger a fresh generation"
    assert result.get("campaign_id") != corpse_id
    with get_session() as session:
        rows = session.query(Campaign).filter(Campaign.player_id == player_id).all()
        assert len(rows) == 1 and rows[0].id != corpse_id


def test_fresh_pending_campaign_is_left_alone(db):
    from src.models import Location, LocationType, Player, get_session
    from src.models.campaign import Campaign
    from src.services import campaign as campaign_service

    with get_session() as session:
        loc = Location(name="Square", type=LocationType.DISTRICT)
        session.add(loc)
        session.flush()
        player = Player(name="Rook", current_location_id=loc.id)
        session.add(player)
        session.flush()
        session.add(Campaign(player_id=player.id, status="pending"))
        session.commit()
        player_id = player.id

    calls = []
    result = campaign_service.ensure_campaign(
        player_id, generate=lambda brief: calls.append(brief), background=False
    )
    assert result["status"] == "pending" and calls == []


# ---------------------------------------------------------------------------
# Nearly-right model JSON is repaired, not rejected
# ---------------------------------------------------------------------------

def test_trailing_commas_and_comments_are_repaired():
    from src.agents.campaign_forge import _parse_payload

    raw = """```json
    {
      // the pitch
      "title": "The Whistle and the Wolf",
      "acts": [{"number": 1, "title": "The Hollow Howl",},],
      "beats": [],
    }
    ```"""
    data = _parse_payload(raw)
    assert data["title"] == "The Whistle and the Wolf"
    assert data["acts"][0]["number"] == 1


def test_truncated_reply_is_salvaged():
    from src.agents.campaign_forge import _parse_payload

    raw = ('{"title": "T", "premise": "P", "beats": [{"key": "a", "title": "One"}, '
           '{"key": "b", "title": "Two", "directive": "The courier is se')
    data = _parse_payload(raw)
    assert data["title"] == "T"
    assert [b["key"] for b in data["beats"]] == ["a", "b"]


def test_garbage_reply_still_raises():
    import pytest as _pytest
    from src.agents.campaign_forge import _parse_payload

    with _pytest.raises(ValueError):
        _parse_payload("I cannot help with that.")


def test_dangling_key_before_a_closer_is_dropped():
    from src.agents.campaign_forge import _parse_payload

    raw = '{"title": "The Long Howl", "beats": [{"key": "b1", "act": 1, "sequence":}}'
    data = _parse_payload(raw)
    assert data["title"] == "The Long Howl"
    assert data["beats"] == [{"key": "b1", "act": 1}]

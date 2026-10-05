"""The encounter director: a parley holds the lingering ambush roll.

fleet4-bram T6 and fleet4-wenna T9: the villain invited the talk, the player
put the key question, and the lingering roll answered with a one-sentence
wolf ambush. A hostile voice talking with the player now buys the talk a turn
(at most PARLEY_MAX_SKIPS in a row); arrivals are unchanged.
"""

import random

import pytest

from src.models import NPC, Faction, Location, LocationType, Player, WorldClock, get_session
from src.services import encounters


class _AlwaysStrikes(random.Random):
    def random(self):
        return 0.0


@pytest.fixture()
def hollow(db):
    for state in (encounters._linger, encounters._since_ambush, encounters._parley, encounters._parley_skips,
                  encounters._talking, encounters._held, encounters._drawn, encounters._standoff,
                  encounters._standoff_held):
        state.clear()
    with get_session() as session:
        place = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True)
        fangs = Faction(name="The Ashen Fangs")
        session.add_all([place, fangs, WorldClock(day=1, hour=12)])
        session.flush()
        player = Player(name="Wenna Cole", currency=10, current_location_id=place.id)
        people = {
            "wolf": NPC(name="Hollow Wolf", species="wolf", danger_tier="weak", faction_id=fangs.id,
                        current_location_id=place.id, current_mood="watchful"),
            "aldric": NPC(name="Aldric Vane", danger_tier="boss", faction_id=fangs.id,
                          current_location_id=place.id, current_mood="patient"),
            "vex": NPC(name="Vex Marrow", danger_tier="dangerous", faction_id=fangs.id,
                       current_location_id=place.id, current_mood="watchful"),
            "piper": NPC(name="Piper Quillow", danger_tier="harmless",
                         current_location_id=place.id, current_mood="hopeful"),
        }
        session.add(player)
        session.add_all(people.values())
        session.commit()
        ids = {key: npc.id for key, npc in people.items()}
        ids["player"] = player.id
    return ids


def _lingering_roll(pid):
    encounters.note_turn(pid, None)  # location only matters for the linger count
    return encounters.roll(pid, arrived=False, rng=_AlwaysStrikes())


def test_the_villain_talking_holds_the_lingering_roll_twice_then_danger_speaks(hollow):
    pid = hollow["player"]
    for _ in range(2):
        assert encounters.note_parley(pid, [hollow["aldric"]]) is True
        assert _lingering_roll(pid) is None                     # the question gets its answer
    assert encounters.note_parley(pid, [hollow["aldric"]]) is True
    struck = _lingering_roll(pid)
    assert struck and struck["names"] == ["Hollow Wolf"]        # talking cannot stall a pack forever


def test_the_packs_own_side_counts_as_a_parley(hollow):
    """Vex is not 'hostile' by mood or standing — but his wolves are the ambush."""
    assert encounters.note_parley(hollow["player"], [hollow["vex"]]) is True


def test_a_friendly_voice_is_no_parley(hollow):
    pid = hollow["player"]
    assert encounters.note_parley(pid, [hollow["piper"]]) is False
    assert _lingering_roll(pid) is not None


def test_arrival_rolls_ignore_the_parley(hollow):
    pid = hollow["player"]
    encounters.note_parley(pid, [hollow["aldric"]])
    encounters.note_turn(pid, None)
    assert encounters.roll(pid, arrived=True, rng=_AlwaysStrikes()) is not None


def test_a_turn_without_talk_resets_the_cap(hollow):
    pid = hollow["player"]
    for _ in range(2):
        encounters.note_parley(pid, [hollow["aldric"]])
        assert _lingering_roll(pid) is None
    encounters.note_parley(pid, [])                              # the talk ended
    assert encounters._parley_skips.get(pid) is None
    encounters.note_parley(pid, [hollow["aldric"]])              # a new parley gets its own grace
    assert _lingering_roll(pid) is None


def test_a_held_roll_does_not_erase_that_the_last_reply_was_a_talk(hollow):
    """The next turn's lingering roll runs BEFORE the context is built; the
    complication director asks in_parley after it. A held roll spent the
    parley flag, so a complication cut into the villain's talk."""
    pid = hollow["player"]
    encounters.note_parley(pid, [hollow["aldric"]])
    assert _lingering_roll(pid) is None                          # held by the talk
    assert encounters.in_parley(pid) is True                     # ...which is still the last reply
    encounters.note_parley(pid, [hollow["piper"]])               # the talk ended
    assert encounters.in_parley(pid) is False
    encounters.note_parley(pid, [hollow["aldric"]])
    encounters.reset(pid)                                        # a fight broke it off
    assert encounters.in_parley(pid) is False


# fleet6-bram T10, word for word: the reply before had no hostile voice in it
# (the ash-gray figure only signed to his wolves), so nothing held the roll
BRAM_T10 = ("I stay still, hand on my blade but not drawing yet. This is no pack — it's a man with trained "
            "wolves. I call out into the dark, voice hard and clear: \"I'm here to talk, not fight. You want "
            "to tell me what you're doing with those carts, or do we do this the hard way?\"")


def _lingering(pid, words):
    encounters.note_turn(pid, None)
    return encounters.roll(pid, arrived=False, rng=_AlwaysStrikes(), player_input=words)


def test_the_player_opening_a_parley_holds_the_lingering_roll(hollow):
    pid = hollow["player"]
    encounters.note_turn(pid, None)
    assert _lingering(pid, BRAM_T10) is None                     # his words get their answer
    assert encounters.in_parley(pid) is True                     # ...and no complication cuts in
    assert _lingering(pid, BRAM_T10) is None
    assert _lingering(pid, BRAM_T10) is not None                 # talk cannot stall the pack forever


def test_reaching_for_a_weapon_or_talking_to_no_one_holds_nothing(hollow):
    pid = hollow["player"]
    encounters.note_turn(pid, None)
    assert _lingering(pid, "I draw my knife and charge the nearest wolf.") is not None
    encounters._since_ambush.clear()
    with get_session() as session:
        for key in ("aldric", "vex"):
            session.get(NPC, hollow[key]).current_location_id = None
        session.commit()
    # wolves alone are no one to talk to (BRAM_T10's "I stay still" before
    # them is a stand-off the die decides now — round 8, roll_intent)
    assert _lingering(pid, 'I call out into the dark: "I\'m here to talk, not fight."') is not None


def test_opens_parley_reads_words_not_weapons():
    assert encounters.opens_parley(BRAM_T10)
    assert encounters.opens_parley('I lower my blade. "Who are you?"')
    assert not encounters.opens_parley("I lunge at him before he can whistle.")
    assert not encounters.opens_parley("[COMBAT OUTCOME — victory]")


def test_a_second_ambush_here_draws_someone_new_when_the_room_allows(hollow):
    """fleet6-bram T10 fought round 4's fight again: the same two weak wolves."""
    pid = hollow["player"]
    with get_session() as session:
        place = session.get(NPC, hollow["wolf"]).current_location_id
        for name, tier in (("Gray-Flank Wolf", "weak"), ("Pale Wolf", "weak"), ("Old Ridge Wolf", "standard"),
                           ("Lame Wolf", "weak")):
            session.add(NPC(name=name, species="wolf", danger_tier=tier, current_location_id=place))
        session.commit()
    encounters.note_turn(pid, place)
    first = encounters.roll(pid, arrived=True, rng=_AlwaysStrikes())
    assert first["names"] == ["Gray-Flank Wolf", "Hollow Wolf", "Lame Wolf"]   # weakest first
    encounters._since_ambush.clear()
    second = encounters.roll(pid, arrived=True, rng=_AlwaysStrikes())
    # Someone not sent before, and the tier not sent before, leads
    assert second["names"][:2] == ["Old Ridge Wolf", "Pale Wolf"]


def test_a_held_npc_sits_out_one_roll(hollow):
    pid = hollow["player"]
    encounters.hold(hollow["wolf"])
    encounters.note_turn(pid, None)
    assert encounters.roll(pid, arrived=True, rng=_AlwaysStrikes()) is None
    assert encounters.roll(pid, arrived=True, rng=_AlwaysStrikes())["names"] == ["Hollow Wolf"]


# --- Round 6 (fleet7) ---------------------------------------------------------

def _campaign_naming(hollow, directive):
    """A live campaign (villain Aldric) with one act-3 beat still pending."""
    from src.models import Campaign, StoryBeat

    with get_session() as session:
        campaign = Campaign(player_id=hollow["player"], title="The Toll Road", status="active", current_act=1,
                            villain_npc_id=hollow["aldric"], acts=[], fixed_truths=[])
        session.add(campaign)
        session.flush()
        session.add(StoryBeat(campaign_id=campaign.id, act=3, sequence=11, beat_type="setpiece",
                              title="The Cached Cargo", directive=directive, status="pending",
                              trigger_type="after_beat", trigger_data={}))
        session.commit()


def test_the_director_never_sends_a_beast_a_story_beat_still_needs(hollow):
    """fleet7-wenna T14: the ambush killed Hollow Wolf and Gray-Flank Wolf, and
    act-3 "The Cached Cargo" still has them prowl the den's edge."""
    _campaign_naming(hollow, "Have Hollow Wolf prowl the den's edge at Wolf's Hollow, answering to posture.")
    pid = hollow["player"]
    encounters.note_turn(pid, None)
    # The den still bites: one of its pack's rank and file, made real
    struck = encounters.roll(pid, arrived=True, rng=_AlwaysStrikes())
    assert struck and len(struck["npc_ids"]) == 1 and struck["npc_ids"] != [hollow["wolf"]]
    with get_session() as session:
        stand_in = session.get(NPC, struck["npc_ids"][0])
        assert stand_in.species == "wolf" and stand_in.danger_tier == "weak"
        assert stand_in.faction_id == session.get(NPC, hollow["wolf"]).faction_id
        assert stand_in.current_location_id == session.get(Player, pid).current_location_id
        # ...and the story's wolf is still an enemy here: a fight the player
        # starts with it is theirs
        assert "Hollow Wolf" in [n.name for n in encounters.enemies_here(
            session, session.get(Player, pid), beasts_only=True, limit=None)]
    # The stand-in lives on as anyone: the next ambush draws it, no new one
    encounters._since_ambush.clear()
    again = encounters.roll(pid, arrived=True, rng=_AlwaysStrikes())
    assert again["npc_ids"] == struck["npc_ids"]


def test_a_scout_held_for_its_choice_spawns_no_stand_in(hollow):
    _campaign_naming(hollow, "Have Hollow Wolf prowl the den's edge at Wolf's Hollow.")
    pid = hollow["player"]
    with get_session() as session:
        scout = NPC(name="Rib-Thin Wolf", species="wolf", danger_tier="weak",
                    current_location_id=session.get(Player, pid).current_location_id)
        session.add(scout)
        session.commit()
        scout_id = scout.id
    encounters.hold(scout_id)
    encounters.note_turn(pid, None)
    assert encounters.roll(pid, arrived=True, rng=_AlwaysStrikes()) is None
    with get_session() as session:
        assert session.query(NPC).filter(NPC.species == "wolf").count() == 2


def test_a_place_name_is_no_beast_of_that_name(hollow):
    """"Wolf's Hollow" names no Hollow Wolf: the wolf is still the director's."""
    _campaign_naming(hollow, "Stage the den at Wolf's Hollow, crates stamped Greywater.")
    pid = hollow["player"]
    encounters.note_turn(pid, None)
    assert encounters.roll(pid, arrived=True, rng=_AlwaysStrikes())["names"] == ["Hollow Wolf"]


STANDOFF = ("The wolf stays at the fence line, head low, watching you now instead of the boy.\n\n"
            "Hollow Wolf stares at you across the trough, one ear hanging wrong.\n\n"
            "You can hold still and see if it loses interest, make noise to drive it off, or back toward "
            "the market square yourself and leave it the yard.")


def test_a_choice_put_to_the_player_facing_the_wolf_holds_one_roll(hollow):
    """fleet7-bram T8 -> T9: the reply offered three ways out; the lingering
    roll sent the wolf before the player's choice was read."""
    pid = hollow["player"]
    encounters.note_parley(pid, [], STANDOFF)
    encounters.note_turn(pid, None)
    assert encounters.roll(pid, arrived=False, rng=_AlwaysStrikes(),
                           player_input='I hurl stones at the fence. "Get! Go on!"') is None
    # Once: the same standoff offered again does not hold the next roll
    encounters.note_parley(pid, [], STANDOFF)
    struck = encounters.roll(pid, arrived=False, rng=_AlwaysStrikes(), player_input="I hold still.")
    assert struck and struck["names"] == ["Hollow Wolf"]


def test_a_blow_or_no_choice_holds_nothing(hollow):
    pid = hollow["player"]
    encounters.note_parley(pid, [], STANDOFF)
    encounters.note_turn(pid, None)
    assert encounters.roll(pid, arrived=False, rng=_AlwaysStrikes(),
                           player_input="I charge the wolf with my knife.") is not None
    encounters._since_ambush.clear()
    encounters.note_parley(pid, [], "Hollow Wolf stares at you across the trough. The horses stamp.")
    assert encounters.roll(pid, arrived=False, rng=_AlwaysStrikes(), player_input="I wait.") is not None


def test_a_standoff_with_a_stranger_reads_the_name_the_player_saw(hollow):
    """note_parley reads the reply as the player saw it: an unmet hostile is
    "the Ash-Cloaked Man" there, never "Vex Marrow" (dialogue.NameGate)."""
    from src.services.dialogue import role_label

    pid = hollow["player"]
    with get_session() as session:
        vex = session.get(NPC, hollow["vex"])
        vex.current_mood, vex.description_physical = "hostile", "A lean, menacing man in an ash-gray cloak."
        session.commit()
        label = role_label(vex)
    assert label == "Ash-Cloaked Man"
    masked = (f"The {label.lower()} plants himself in the path, blade low.\n\n"
              "You could talk your way past him, or turn back for the road.")
    encounters.note_parley(pid, [], masked)
    for _ in range(2):                              # lingering: people strike too
        encounters.note_turn(pid, None)
    assert encounters.roll(pid, arrived=False, rng=_AlwaysStrikes(), player_input="I stand my ground.") is None

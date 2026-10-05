"""Round 8 (fleet9): danger with choices — beasts, thieves, the barred night.

  * fleet9-tamsin T12-T13 and fleet9-wenna T14 (and fleet8-wenna before
    them): gentling, feeding, holding ground or backing toward a door with a
    wolf in front of them never got a roll, and the only answer the engine
    had was the ambush. wenna's "calming a hungry wolf at the fence" fell to
    the beat's willing stable hand (no die cast), her prose sent the wolf
    "back… into the black… and is gone" with no @npc-move, and T16 forced the
    fight anyway.
  * fleet9-tamsin T12: Piper paid "even with the wolf still prowling the
    trough".
  * fleet9-bram T11: the evening's only threat — a pickpocket — came and went
    in one reply with an unchosen roll and @coin -6, every sentence of it
    dropped; "answered" all the same.
  * all three: the 20:00 bar was a dead end, and the evening had no danger.
  * fleet9-wenna T1-T15: an interview treadmill — no choice with a cost.
"""

import random

import pytest

from src.models import (
    NPC, DMState, Location, LocationType, Player, Quest, QuestStatus, WorldClock, get_session, reset_engine,
)
from src.services import complications, encounters, roll_intent, settle, stakes
from src.services.commands import CommandResult
from src.web.turn_audit import theft_shown
# The round-7 yard (a scout wolf at the fence, the stable hand) and its
# scripted-DM stream are this file's too
from tests.test_stakes_round7 import _clear_directors, _force, _roll_events, sorrel_beat, stream, yard  # noqa: F401
from src.tools.world_write.progression import roll_check


class _NeverStrikes(random.Random):
    def random(self):
        return 0.99


class _AlwaysStrikes(random.Random):
    def random(self):
        return 0.0


TAMSIN_T12 = ("I step forward slowly, hand on my knife hilt but not drawing it yet, keeping my body between the wolf "
              "and Piper. \"Easy,\" I call out, low and steady, like I'm gentling a spooked horse. \"You're looking for "
              "an easier meal than us, aren't you?\"  I don't run—running invites the chase—but I angle toward the barn "
              "door, trying to edge Piper and Sorrel that direction without breaking eye contact with the torn-ear.")
TAMSIN_T13 = ("I keep moving backward toward the barn, steady and deliberate, my knife hand loose and ready. \"Go,\" I "
              "tell Sorrel and Piper without breaking eye contact with the wolf. \"Get inside—I'm right behind you.\"  "
              "The moment they're through the door, I'll slip in after them and help bar it.")
WENNA_T14 = ("I go very still, hand dropping to my satchel where I keep scraps of food—I always carry something. "
             "\"Easy,\" I whisper, not to the Stable Hand but to the wolf itself. I pull out a piece of dried meat I'd "
             "meant to eat tomorrow and toss it slowly toward the fence line, keeping my movements deliberate and calm.")
WENNA_T14_PROSE = ("The meat lands in the mud just past the fence rail, and for one long breath nothing moves. Then the "
                   "wolf's ribs shift as it exhales, slow, ears still pinned toward the dark behind you — not toward "
                   "the food at all. It doesn't come for the scrap. It backs instead, unhurried, into the black beyond "
                   "the lantern's reach, and is gone.")


# ---------------------------------------------------------------------------
# 1. Facing a beast is an act the dice decide
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("words", [TAMSIN_T12, TAMSIN_T13, WENNA_T14])
def test_gentling_feeding_or_backing_away_from_a_beast_is_rolled_first(words):
    family, _ = roll_intent.risky_act(words)
    assert family == "facing a beast"
    line = roll_intent.directive(words)
    assert line.startswith(roll_intent.ROLL_FIRST_MARK)
    # (a name the command can resolve: "@fight it" names nobody)
    assert "a win holds it" in line and "@fight <its name>" in line


@pytest.mark.parametrize("words", [
    # a walk, a glance, a counter — wolves only a topic (fleet-wenna T4, fleet3-tamsin T5)
    "I head back toward Market Square, the wolves still on my mind.",
    "I glance back toward the cloaked man, then ask Piper about the wolves.",
    "I step back from the counter and nod. \"Thank you.\"",
    # no beast anywhere near it
    "I go very still and listen to the Captain finish.",
])
def test_a_stand_off_needs_a_beast_in_the_players_own_words(words):
    assert roll_intent.risky_act(words) is None


def test_a_blow_is_no_stand_off():
    words = "I slash hard at the nearest wolf's shoulder, still backing toward the trees."
    assert (roll_intent.risky_act(words) or ("",))[0] != "facing a beast"


def test_driving_a_beast_off_says_what_each_outcome_means():
    line = roll_intent.directive("I make myself big and shout to drive the wolf off.")
    assert "(intimidation:" in line and "a win holds it" in line


@pytest.mark.parametrize("words,risky", [
    ("I haul myself over the low west wall where the stones are broken.", True),
    ("I go over the wall in the dark.", True),
    ("How do I get over the wall?", False),
])
def test_a_climb_over_the_wall_is_rolled_first(words, risky):
    act = roll_intent.risky_act(words)
    assert (act is not None and act[0] == "athletics") is risky


def test_a_roll_about_the_wolf_is_never_the_beat_tellers_words(sorrel_beat, monkeypatch):
    """fleet9-wenna T14: 'calming a hungry wolf at the fence' — no roll, and
    Sorrel told the beat unasked."""
    _force(monkeypatch, 15)
    result = roll_check(sorrel_beat["player"], "persuasion", "tricky",
                        reason="calming a hungry wolf at the fence with food and a steady voice")
    assert result.get("no_roll") is not True and result["event"] == "check_result"
    # ...while pressing the teller by name about it is still the beat's
    from src.models import get_session as _gs

    with _gs() as session:
        player = session.get(Player, sorrel_beat["player"])
        assert stakes.beat_tells_it(session, player, "persuasion", "pressing Sorrel about the wolf").name \
            == "Sorrel Tack"


# ---------------------------------------------------------------------------
# 2. A won roll sends the beast off; a lost one is the fight
# ---------------------------------------------------------------------------

@pytest.fixture()
def fence(yard):
    """The yard, with the wild the scout came from."""
    with get_session() as session:
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, discovered=True)
        session.add(hollow)
        session.flush()
        session.get(NPC, yard["wolf"]).home_location_id = hollow.id
        session.commit()
        return {**yard, "hollow": hollow.id}


def _where(npc_id):
    with get_session() as session:
        return session.get(NPC, npc_id).current_location_id


def test_a_won_roll_against_the_wolf_sends_it_out_of_the_scene(fence):
    pid = fence["player"]
    encounters.note_turn(pid, fence["yard"])
    assert encounters.note_check(pid, "success", "persuasion", "calming the hungry wolf") == [fence["wolf"]]
    lines = encounters.send_off(pid, "Your voice stays low. Its hackles drop a little.")
    assert _where(fence["wolf"]) == fence["hollow"]
    assert lines and lines[0].startswith("(engine) @npc-move Torn-Ear Wolf | Wolf's Hollow")
    assert "never start a fight with it here" in lines[0]


def test_the_prose_sending_it_off_moves_it_without_a_roll(fence):
    """fleet9-wenna T14: no @npc-move, and the wolf stayed at the fence."""
    lines = encounters.send_off(fence["player"], WENNA_T14_PROSE)
    assert _where(fence["wolf"]) == fence["hollow"] and "your story sent it off" in lines[0]


@pytest.mark.parametrize("prose", [
    # it stays, calm, in the scene: held, not moved (fleet8-wenna T11)
    "Its ribs heave once, twice, and then it sits, haunches lowering into the mud, still watching.",
    # it comes at them: nothing settled
    "It backs into the dark — then lunges at your outstretched hand.",
])
def test_a_beast_that_stays_or_strikes_is_not_moved(fence, prose):
    pid = fence["player"]
    encounters.note_check(pid, "success", "persuasion", "calming the hungry wolf")
    encounters.send_off(pid, prose)
    assert _where(fence["wolf"]) == fence["yard"]


def test_the_wolfs_howl_fading_is_not_the_wolf_leaving(fence):
    encounters.send_off(fence["player"], "Somewhere past the paddock the wolf's howl fades into the night.")
    assert _where(fence["wolf"]) == fence["yard"]


def test_a_beast_with_no_home_elsewhere_is_held_not_moved(yard):
    pid = yard["player"]
    encounters.send_off(pid, WENNA_T14_PROSE)
    assert _where(yard["wolf"]) == yard["yard"] and encounters._held.get(yard["wolf"]) == encounters.CALM_HOLD_ROLLS


def test_never_after_a_fight(fence):
    pid = fence["player"]
    encounters.note_check(pid, "success", "persuasion", "calming the hungry wolf")
    assert encounters.send_off(pid, WENNA_T14_PROSE, fought=True) == []
    assert _where(fence["wolf"]) == fence["yard"]


def test_a_lost_stand_off_roll_is_the_fight_whatever_the_odds(fence):
    pid = fence["player"]
    encounters.reset(pid)                                    # inside the cooldown, too
    encounters.note_turn(pid, fence["yard"])
    assert encounters.note_check(pid, "failure", "persuasion", "calming the hungry wolf") == []
    struck = encounters.roll(pid, arrived=False, rng=_NeverStrikes(), player_input="I hold my ground.")
    assert struck and struck["names"] == ["Torn-Ear Wolf"] and struck["chance"] == 1.0
    # ...once: the fight is on
    assert encounters.roll(pid, arrived=False, rng=_NeverStrikes()) is None


def test_facing_the_beast_down_holds_the_ambush_for_the_roll_not_forever(fence):
    """fleet9-tamsin T13: backing toward the barn, and the lingering roll sent
    the wolf before any die was cast."""
    pid, place = fence["player"], fence["yard"]
    for _ in range(encounters.STANDOFF_MAX_HOLDS):
        encounters.note_turn(pid, place)
        assert encounters.roll(pid, arrived=False, rng=_AlwaysStrikes(), player_input=TAMSIN_T13) is None
    encounters.note_turn(pid, place)                         # the DM never rolled: the pack is not stalled
    assert encounters.roll(pid, arrived=False, rng=_AlwaysStrikes(), player_input=TAMSIN_T13) is not None


def test_a_cast_die_starts_the_stand_off_count_over(fence):
    pid, place = fence["player"], fence["yard"]
    encounters.note_turn(pid, place)
    assert encounters.roll(pid, arrived=False, rng=_AlwaysStrikes(), player_input=WENNA_T14) is None
    encounters.note_check(pid, "partial", "persuasion", "calming the wolf")
    encounters._held.clear()
    for _ in range(encounters.STANDOFF_MAX_HOLDS):
        encounters.note_turn(pid, place)
        assert encounters.roll(pid, arrived=False, rng=_AlwaysStrikes(), player_input=WENNA_T14) is None


def test_a_missed_look_provokes_nothing(fence):
    pid = fence["player"]
    encounters.note_turn(pid, fence["yard"])
    encounters.note_check(pid, "failure", "perception", "spotting the wolf in the dark")
    assert encounters.roll(pid, arrived=False, rng=_NeverStrikes()) is None


def test_a_scout_sent_home_is_never_pressed_as_unanswered(fence):
    """fleet9-wenna T16: '[COMPLICATION — UNANSWERED… @fight Torn-Ear Wolf]'
    for the wolf her prose had sent off two replies before."""
    pid = fence["player"]
    complications._open[pid] = {"kind": "scout", "entity_id": fence["wolf"], "entity_name": "Torn-Ear Wolf",
                                "stub": None, "what": "a scout", "ignored": "it comes for them: @fight Torn-Ear Wolf",
                                "replies": 1, "where": fence["yard"]}
    encounters.send_off(pid, WENNA_T14_PROSE)
    complications.note_turn(pid, player_input="I follow his gaze to the north wall.", location_id=fence["yard"])
    assert complications.open_complication(pid) is None
    for _ in range(3):
        assert complications.unanswered_line(pid) is None


def test_a_scout_left_behind_is_let_go_not_pressed(fence):
    pid = fence["player"]
    with get_session() as session:
        session.get(Player, pid).current_location_id = fence["hollow"]     # the player walked off
        session.commit()
    complications._open[pid] = {"kind": "scout", "entity_id": fence["wolf"], "entity_name": "Torn-Ear Wolf",
                                "stub": None, "what": "a scout", "ignored": "@fight Torn-Ear Wolf",
                                "replies": 3, "where": fence["yard"], "walked": True}
    assert complications.unanswered_line(pid) is None and complications.open_complication(pid) is None


def test_the_send_off_reaches_the_next_ledger_through_the_stream(stream, fence):
    shown, complete, _ = stream("I toss the wolf a scrap of meat and keep very still.", [{"data": WENNA_T14_PROSE}])
    assert "is gone" in shown
    assert any("@npc-move Torn-Ear Wolf | Wolf's Hollow" in w for w in complete.get("engine_walks") or [])
    assert _where(fence["wolf"]) == fence["hollow"]


# ---------------------------------------------------------------------------
# 3. A payout waits out the danger
# ---------------------------------------------------------------------------

@pytest.fixture()
def finished_job(yard):
    with get_session() as session:
        quest = Quest(title="Counting the Howls", status=QuestStatus.ACTIVE, assigned_by_npc_id=yard["sorrel"],
                      objectives=[{"text": "Stand out the next howl", "kind": "custom", "done": True}],
                      rewards={"currency": 10})
        session.add(quest)
        session.commit()
        return {**yard, "quest": quest.id}


def test_no_payout_with_the_wolf_at_the_trough_then_the_first_calm_reply_pays(finished_job):
    """fleet9-tamsin T12: Piper paid with the wolf still prowling."""
    pid, present = finished_job["player"], {finished_job["sorrel"]: "Sorrel Tack"}
    settle.reset(pid)
    for _ in range(settle.PAY_MAX + 1):                     # held, and no push spent
        assert settle.pay_lines(pid, present) == []
    with get_session() as session:
        session.get(NPC, finished_job["wolf"]).status = "dead"
        session.commit()
    lines = settle.pay_lines(pid, present)
    assert lines and lines[0].startswith("[PAY THIS REPLY")


@pytest.mark.parametrize("words,held", [
    (TAMSIN_T12, True),                                     # mid stand-off
    ("I pick the lock on the tack chest while he counts.", True),
    ("\"I'll want the ten coin and the tincture now,\" I say.", False),   # the payment itself is the talk
    ("How long have you worked the yard?", False),
])
def test_a_risky_act_under_way_holds_the_payout_but_talk_of_pay_does_not(finished_job, words, held):
    pid, present = finished_job["player"], {finished_job["sorrel"]: "Sorrel Tack"}
    settle.reset(pid)
    with get_session() as session:
        session.get(NPC, finished_job["wolf"]).status = "dead"
        session.commit()
    settle.note_turn(pid, words)
    assert (settle.pay_lines(pid, present) == []) is held


def test_a_fight_waiting_holds_the_payout(finished_job):
    pid, present = finished_job["player"], {finished_job["sorrel"]: "Sorrel Tack"}
    settle.reset(pid)
    with get_session() as session:
        session.get(NPC, finished_job["wolf"]).status = "dead"
        session.commit()
    settle.note_turn(pid, "I nod.", fight_pending=True)
    assert settle.pay_lines(pid, present) == []


# ---------------------------------------------------------------------------
# 4. A thief is a choice before it is a cost
# ---------------------------------------------------------------------------

def _row(line, ok=True):
    return CommandResult(line=line, verb="", ok=ok, tool=None, result={})


@pytest.fixture()
def market(db):
    complications.reset_state()
    _clear_directors()
    with get_session() as session:
        square = Location(name="Market Square", type=LocationType.DISTRICT, discovered=True)
        session.add_all([square, WorldClock(day=1, hour=19), DMState(tension="low")])
        session.flush()
        player = Player(name="Bram Holloway", current_location_id=square.id, currency=22)
        thief = NPC(name="Corran Pike", profession="drover", current_location_id=square.id, current_mood="calm")
        session.add_all([player, thief])
        session.commit()
        ids = {"player": player.id, "square": square.id, "thief": thief.id}
    complications._open[ids["player"]] = {
        "kind": "pickpocket", "entity_id": None, "entity_name": "Corran Pike", "stub": "@new-npc Corran Pike",
        "what": "a hand at their belt", "ignored": complications._IGNORED["pickpocket"], "replies": 0,
        "where": ids["square"],
    }
    yield ids
    _clear_directors()


def test_the_pickpocket_line_asks_for_the_choice_not_the_verdict(market, monkeypatch):
    menu = {k: (k, what, bite) for k, _, what, bite in complications._TOWN}
    monkeypatch.setattr(complications, "_pick", lambda *a, **k: menu["pickpocket"])
    pid = market["player"]
    complications._open.pop(pid)
    for _ in range(10):
        complications.note_turn(pid, player_input="I ask the drover about the road.")
    line = complications.directive_line(pid)
    assert "the thief in sight" in line
    assert "Nothing is charged and nothing is rolled THIS reply" in line
    assert "Write the command for whatever it costs" not in line


def test_a_cost_nobody_saw_is_shown_next_reply_and_never_charged_twice(market):
    """fleet9-bram T11: @coin -6 behind three dropped sentences, 'answered'."""
    pid = market["player"]
    shown = "Sorrel doesn't notice any of it."
    cost = [_row("@coin -6 | a quick hand in the press lifts coin from his purse")]
    complications.note_turn(pid, player_input="I watch Sorrel's face.", commands=cost,
                            location_id=market["square"], cost_seen=theft_shown(shown, cost))
    warnings = complications.pop_warnings(pid)
    assert [w["check"] for w in warnings] == ["complication_choice_skipped"]
    line = complications.unanswered_line(pid)
    assert line.startswith("[COMPLICATION — UNSEEN.") and "@coin -6" in line
    assert "never write the command again" in line
    assert complications.open_complication(pid) is None


def test_the_thief_stays_quarry_for_two_replies_and_is_never_the_ambush(market):
    pid = market["player"]
    complications.note_turn(pid, player_input="I keep talking.", location_id=market["square"])
    assert complications.open_complication(pid)["kind"] == "pickpocket"      # the bringing reply: the choice
    with get_session() as session:
        player = session.get(Player, pid)
        assert [n.name for n in encounters.enemies_here(session, player, beasts_only=False)] == ["Corran Pike"]
    encounters.note_turn(pid, market["square"])
    encounters.note_turn(pid, market["square"])
    assert encounters.roll(pid, arrived=False, rng=_AlwaysStrikes()) is None   # a thief is chased, never sent
    with get_session() as session:
        assert encounters.enemies_here(session, session.get(Player, pid), beasts_only=False) == []


def test_a_cost_the_player_saw_after_their_answer_closes_it(market):
    pid = market["player"]
    complications.note_turn(pid, player_input="I keep talking.", location_id=market["square"])
    cost = [_row("@coin -6 | the thief is gone with it")]
    shown = "When you look again your purse hangs lighter, its strings cut clean."
    complications.note_turn(pid, player_input="I let him go.", commands=cost, location_id=market["square"],
                            cost_seen=theft_shown(shown, cost))
    assert complications.open_complication(pid) is None and complications.pop_warnings(pid) == []


def test_a_tip_the_player_gives_meanwhile_is_no_thiefs_take(market):
    pid = market["player"]
    complications.note_turn(pid, player_input="I keep talking.", location_id=market["square"])
    tip = [_row("@coin -2 | a few coins pressed into the stable hand's palm for his trouble")]
    complications.note_turn(pid, player_input="I press two coins into his hand.", commands=tip,
                            location_id=market["square"], cost_seen=True)
    assert complications.open_complication(pid)["kind"] == "pickpocket"


def test_theft_shown_reads_what_the_player_saw():
    cost = [_row("@remove Dried Lavender Bundle")]
    assert theft_shown("A lean drover is already slipping off through the press with your purse.", cost)
    assert theft_shown("The thief is gone into the crowd, and so is the lavender from your pack.", cost)
    assert not theft_shown("Sorrel doesn't notice any of it.", cost)


# ---------------------------------------------------------------------------
# 5. The barred night has ways; a long talk ends on a choice
# ---------------------------------------------------------------------------

@pytest.fixture()
def emberfall(tmp_path):
    from src.data.playtest_seed import create_playtest_world

    reset_engine()
    create_playtest_world(str(tmp_path / "emberfall.db"))
    _clear_directors()
    settle.reset()
    with get_session() as session:
        places = {loc.name: loc.id for loc in session.query(Location).all()}
        player = Player(name="Wenna Cole", current_location_id=places["Market Square"], currency=20)
        session.add(player)
        session.commit()
        ids = {"player": player.id, **places}
    yield ids
    _clear_directors()
    reset_engine()


def _at(hour, minute=0):
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.hour, clock.minute = hour, minute
        session.commit()


def _main_quest_active():
    with get_session() as session:
        quest = session.query(Quest).filter_by(title="Teeth on the North Road").one()
        quest.status = QuestStatus.ACTIVE
        session.commit()


def _brief(pid):
    from src.services.scene_brief import build_scene_brief

    with get_session() as session:
        return build_scene_brief(session, session.get(Player, pid))["text"]


def test_the_brief_names_the_ways_still_open_when_the_night_bars_the_road(emberfall):
    _main_quest_active()
    _at(22, 10)
    text = _brief(emberfall["player"])
    assert "Tonight the way to Wolf's Hollow is barred at North Gatehouse until first light" in text
    assert "a bribe (@coin -N" in text and "@move Wolf's Hollow | through" in text
    assert "roll_check FIRST (athletics, hard)" in text and "the dark past the wall has teeth" in text
    assert "@rest at The Drowned Lantern (8 coin)" in text
    _at(10)
    assert "Tonight the way" not in _brief(emberfall["player"])


def test_a_gate_keeper_the_player_never_met_is_named_by_their_label(emberfall):
    _main_quest_active()
    _at(22, 10)
    with get_session() as session:
        session.add(NPC(name="Dagny Holt", profession="gate sergeant", current_location_id=emberfall["North Gatehouse"]))
        session.commit()
    text = _brief(emberfall["player"])
    assert "whoever keeps it (the Gate Sergeant (Dagny Holt, a name the player has not learned) at North " \
           "Gatehouse)" in text


def test_the_night_ways_wait_for_a_reason_to_go(emberfall):
    _at(22, 10)                                             # no quest points north, not at the gate
    assert "Tonight the way" not in _brief(emberfall["player"])


def _stand(pid, place_id):
    with get_session() as session:
        session.get(Player, pid).current_location_id = place_id
        session.commit()


def test_at_the_gate_itself_the_ways_are_named_without_a_quest(emberfall):
    _at(22, 10)
    _stand(emberfall["player"], emberfall["North Gatehouse"])
    assert "Tonight the way to Wolf's Hollow is barred at North Gatehouse" in _brief(emberfall["player"])


def test_outside_the_wall_there_is_no_bed_behind_the_bar_to_offer(emberfall):
    _main_quest_active()
    _at(22, 10)
    _stand(emberfall["player"], emberfall["Wolf's Hollow"])
    assert "Tonight the way" not in _brief(emberfall["player"])


def test_a_climb_over_the_wall_at_night_draws_the_edge_scout(emberfall):
    pid = emberfall["player"]
    _at(22, 10)
    with get_session() as session:
        session.get(Player, pid).current_location_id = emberfall["North Gatehouse"]
        session.commit()
    line = complications.directive_line(pid, player_input="I haul myself over the low west wall in the dark.")
    assert line and "going over the wall in the dark" in line


def _talk(pid, n, words="\"And the howls — same hour every night?\" I ask."):
    for _ in range(n):
        complications.note_turn(pid, player_input=words)


def test_five_talk_only_replies_bring_the_gate_closing(emberfall):
    """fleet9-wenna T1-T9: evening, the road to the Hollow about to bar, and
    nothing but questions."""
    pid = emberfall["player"]
    _main_quest_active()
    _at(18, 30)
    _talk(pid, complications.TALK_TURNS - 1)
    assert complications.directive_line(pid, player_input="\"What else?\"") is None
    _talk(pid, 1)
    line = complications.directive_line(pid, player_input="\"What else?\"")
    assert line.startswith("[COMPLICATION — the talk has run on")
    assert "the way to Wolf's Hollow bars at nightfall (20:00)" in line and "about 90 minutes" in line
    assert "a bribe, a hard climb over the wall or a bed till dawn" in line
    assert complications.talk_streak(pid) == 0


def test_with_the_howl_due_the_talk_push_is_the_gate_itself(emberfall):
    pid = emberfall["player"]
    _main_quest_active()
    _at(19, 30)                                             # The Evening Howl within the hour
    _talk(pid, complications.TALK_TURNS)
    line = complications.directive_line(pid, player_input="\"Go on.\"")
    assert "about 30 minutes" in line


def test_with_no_gate_someone_packs_up_for_the_night(emberfall):
    pid = emberfall["player"]
    _at(18)
    _talk(pid, complications.TALK_TURNS)
    line = complications.directive_line(pid, player_input="\"Go on.\"")
    assert "Hesk Bramble is packing up for the night" in line and "The Drowned Lantern" in line
    opened = complications.open_complication(pid)
    assert opened["kind"] == "leaving" and opened["ignored"] == "they are gone for the night: @npc-move Hesk " \
                                                                 "Bramble | The Drowned Lantern"


def test_someone_packed_up_for_the_night_is_back_at_first_light(emberfall):
    """"Till morning" is a promise: the seed's hawker sent to the tavern at
    dusk kept his stall shut for good."""
    from src.services import world_tick

    pid = emberfall["player"]
    _at(18)
    _talk(pid, complications.TALK_TURNS)
    complications.directive_line(pid, player_input="\"Go on.\"")
    with get_session() as session:
        hesk = session.query(NPC).filter_by(name="Hesk Bramble").one()
        hesk.current_location_id = emberfall["The Drowned Lantern"]
        session.commit()
        hesk_id = hesk.id
    complications.note_turn(pid, player_input="\"Good night, then.\"",
                            commands=[_row("@npc-move Hesk Bramble | The Drowned Lantern")],
                            location_id=emberfall["Market Square"])
    assert complications.open_complication(pid) is None
    try:
        with get_session() as session:
            clock = session.query(WorldClock).first()
            clock.day, clock.hour = clock.day + 1, world_tick.FIRST_LIGHT_HOUR
            session.commit()
            assert [b["name"] for b in world_tick.back_at_first_light(session, clock)] == ["Hesk Bramble"]
        assert _where(hesk_id) == emberfall["Market Square"]
    finally:
        world_tick._gone_in.pop(hesk_id, None)


@pytest.mark.parametrize("breaker", [
    {"roll_outcomes": ["success"]},
    {"commands": [_row("@interact Notice board")]},
])
def test_doing_something_breaks_the_talk_streak_and_a_walk_does_not(emberfall, breaker):
    pid = emberfall["player"]
    _talk(pid, 3)
    complications.note_turn(pid, player_input="I head for the yard.", commands=[_row("@move Stable Yard")])
    assert complications.talk_streak(pid) == 3
    complications.note_turn(pid, player_input="\"Show me.\"", **breaker)
    assert complications.talk_streak(pid) == 0


def test_a_vigil_is_never_talked_out_of(emberfall):
    pid = emberfall["player"]
    _main_quest_active()
    _at(18, 30)
    _talk(pid, complications.TALK_TURNS)
    assert complications.directive_line(
        pid, player_input="\"I'll stay,\" I say, and settle in to watch for the howl.") is None


# ---------------------------------------------------------------------------
# Review fixes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prose", [
    # the player backs off, or someone else slips away, with the wolf named
    # earlier in the sentence: a stand-off's prose, not the wolf leaving
    "The wolf watches as you back into the shadows of the barn.",
    "The wolf snarls while Piper slips into the dark behind the barn.",
])
def test_someone_else_going_into_the_dark_is_not_the_wolf_leaving(fence, prose):
    assert encounters.send_off(fence["player"], prose) == []
    assert _where(fence["wolf"]) == fence["yard"]


def test_watching_it_slink_off_is_the_wolf_leaving(fence):
    encounters.send_off(fence["player"], "The wolf turns, and you watch it slink off into the dark.")
    assert _where(fence["wolf"]) == fence["hollow"]


@pytest.mark.parametrize("prose,shown_next", [
    ("Your voice stays low. Its hackles drop a little.", True),
    (WENNA_T14_PROSE, False),                     # the prose already showed it go
])
def test_a_wolf_moved_on_a_won_roll_alone_is_shown_going_next_reply(fence, prose, shown_next):
    pid = fence["player"]
    encounters.note_check(pid, "success", "persuasion", "calming the hungry wolf")
    line = encounters.send_off(pid, prose)[0]
    assert ("open the next one with it slinking off" in line) is shown_next


def test_the_scene_knows_the_beast_the_words_do_not_name():
    """fleet8-wenna T12: the wolf only in her quotes; fleet7-bram T9: "it"."""
    words = ("I keep my voice gentle and don't break eye contact. \"Sorrel, do you have any food? This one's "
             "starving.\" I'm already reaching into my satchel with my free hand, slow and deliberate.")
    assert not roll_intent.faces_a_beast(words) and roll_intent.faces_a_beast(words, beast_here=True)
    assert roll_intent.faces_a_beast("I hurl stones at the fence, shouting to make myself big.", beast_here=True)
    # ...a counter or a board is still no stand-off, nor a blow
    assert not roll_intent.faces_a_beast("I step back from the board, thinking.", beast_here=True)
    assert not roll_intent.faces_a_beast("I hold my ground and slash at it.", beast_here=True)


def test_calming_the_wolf_beside_the_beat_teller_is_still_rolled(sorrel_beat, monkeypatch):
    _force(monkeypatch, 15)
    result = roll_check(sorrel_beat["player"], "persuasion", "tricky",
                        reason="calming the wolf while the stable hand watches")
    assert result.get("no_roll") is not True and result["event"] == "check_result"


@pytest.mark.parametrize("line", [
    "@coin -5 | bribe for the gate sergeant",       # the barred night's price, not the thief's take
    "@coin +10 | the job's reward",                 # a payout
    "@remove Healing Draught | drunk",
])
def test_other_coin_and_things_meanwhile_are_not_the_thiefs_take(market, line):
    pid = market["player"]
    complications.note_turn(pid, player_input="I keep talking.", location_id=market["square"])
    complications.note_turn(pid, player_input="I pay the sergeant.", commands=[_row(line)],
                            location_id=market["square"], cost_seen=False)
    assert complications.open_complication(pid)["kind"] == "pickpocket"
    assert not (complications.unanswered_line(pid) or "").startswith("[COMPLICATION — UNSEEN")


def test_a_bare_coin_loss_is_the_thiefs_take(market):
    pid = market["player"]
    complications.note_turn(pid, player_input="I keep talking.", location_id=market["square"])
    complications.note_turn(pid, player_input="I let him go.", commands=[_row("@coin -4")],
                            location_id=market["square"], cost_seen=True)
    assert complications.open_complication(pid) is None


def test_a_cross_word_with_the_giver_never_holds_their_payout(finished_job):
    pid, present = finished_job["player"], {finished_job["sorrel"]: "Sorrel Tack"}
    settle.reset(pid)
    with get_session() as session:
        session.get(NPC, finished_job["wolf"]).status = "dead"
        session.get(NPC, finished_job["sorrel"]).current_mood = "hostile"
        session.commit()
    lines = settle.pay_lines(pid, present)
    assert lines and lines[0].startswith("[PAY THIS REPLY")


def test_the_thief_is_a_fight_chip_right_after_the_reply_that_brings_them(stream, yard):
    """The post-reply snapshot was built before the count marked the thief."""
    pid = yard["player"]
    with get_session() as session:
        session.get(NPC, yard["wolf"]).status = "dead"
        session.commit()
    complications._open[pid] = {
        "kind": "pickpocket", "entity_id": None, "entity_name": "Corran Pike", "stub": "@new-npc Corran Pike",
        "what": "a hand at their belt", "ignored": complications._IGNORED["pickpocket"], "replies": 0,
        "where": yard["yard"],
    }
    reply = ("A shoulder bumps yours in the press of the yard, and your purse strings hang loose.\n"
             "@new-npc Corran Pike | trade: drover | looks: lean, a drover's oilskin\n"
             "A lean drover is already moving off toward the gate, not hurrying.\n")
    _, _, frames = stream("\"Busy night,\" I say to the stable hand.", [{"data": reply}])
    states = [f["state"] for f in frames if f.get("type") == "state"]
    fights = [c for c in (states[-1].get("suggestions") or []) if c.get("kind") == "fight"]
    with get_session() as session:
        thief = session.query(NPC).filter_by(name="Corran Pike").one()
        assert [c["npc_id"] for c in fights] == [thief.id]
    encounters._quarry.clear()

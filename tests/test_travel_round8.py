"""Travel, consent and who is where (round 7 playtests -> round 8).

- fleet8-wenna T3: "I head toward the North Gatehouse… I'm going to remember
  every detail of this" — "remember every detail" was read as the place and
  no travel line formed; Piper's pitch stopped her instead, and the same
  reply wrote @quest-start before she said yes.
- fleet8-tamsin T11/T12: the travel line was issued, the reply wrote no
  @move and staged the gate and a guard there; the database kept her in the
  square. T17/T20 "toward the north road" formed no line at all.
- fleet8-tamsin T11-T13: after the howl the guard read the seal and the gate
  swung wide — `@move Wolf's Hollow | through` on the pass alone.
- fleet8-bram T2: the Captain walked him to the gatehouse and spoke there,
  and stayed in the square in the database all session.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from src.models import (
    NPC, Connection, Location, Player, Quest, QuestStatus, WorldClock, get_session, reset_engine,
)
from src.services import job_offers, travel_intent
from src.services.commands import CommandRunner
from src.services.travel_intent import destination_phrase, destination_phrases

WENNA_T3 = ("I head toward the North Gatehouse, but my mind is already turning over what Coldiron didn't say. "
            "A carter dies three weeks ago, wolves start hunting in formation, and the Captain won't even speak "
            "the man's name—that's not caution, that's something deeper. I'll get that pass and head to Wolf's "
            "Hollow, but I'm going to remember every detail of this, and I'm coming back with enough questions "
            "to make him answer.")
TAMSIN_T17 = ("I pocket the bright coin with the whistle—both pieces of proof—and move toward the tree line on my "
              "left, away from the second growl. No point fighting two wolves when I've got what I came for. I "
              "keep low and quick, using the ruined hut as cover until I'm into the shadows, then I pick up pace "
              "toward the north road. The Watch Captain needs to see this whistle and hear about the coin before "
              "anyone else realizes I have them.")
TAMSIN_T20 = ("I nod once, tight and careful, then move toward the treeline at a steady pace — not running, not "
              "looking back. \"The hills are learning to count,\" I repeat quietly, committing it to memory. Once "
              "I'm into the darker trees, I pick up speed toward the north road, the whistle and coin heavy in my "
              "pocket like proof of something I don't yet understand.")
TAMSIN_T11 = ("I'm already moving toward the North Gatehouse, pulling Piper with me. \"East ridge, past the quarry "
              "track—closer than Wolf's Hollow. Got it.\" I release her sleeve and head for the gate, mind already "
              "sketching the quarry route. \"Lock your door tonight.\"")
BRAM_T2_PROSE = (
    "The Captain's stride doesn't break, but his shoulders set a little harder at that, like a man absorbing a "
    "blow he's decided not to answer. He says nothing until you're both past the well, the market noise falling "
    "away behind you, the Gatehouse's torches swimming into view ahead — two squat towers and a barred iron "
    "gate, a knot of Watch men stamping against the cold.")


@pytest.fixture()
def emberfall(tmp_path):
    from src.data.playtest_seed import create_playtest_world

    reset_engine()
    create_playtest_world(str(tmp_path / "emberfall.db"))
    job_offers.reset()
    with get_session() as session:
        places = {loc.name: loc.id for loc in session.query(Location).all()}
        people = {npc.name: npc.id for npc in session.query(NPC).all()}
        quests = {q.title: q.id for q in session.query(Quest).all()}
        player = Player(name="Wenna Cole", current_location_id=places["Market Square"], currency=25)
        session.add(player)
        session.commit()
        ids = {"player": player.id, **places, **people, **quests}
    yield ids
    job_offers.reset()
    reset_engine()


def _put(ids, where, *, hour=None, inventory=None):
    with get_session() as session:
        player = session.get(Player, ids["player"])
        player.current_location_id = ids[where]
        if inventory is not None:
            player.inventory = inventory
        if hour is not None:
            session.query(WorldClock).first().hour = hour
        session.commit()


def _where(ids, who="player"):
    with get_session() as session:
        row = session.get(Player, ids["player"]) if who == "player" else session.get(NPC, ids[who])
        return session.get(Location, row.current_location_id).name


PASS = [{"id": "gate_pass", "name": "Gate Pass", "type": "quest_item"}]


# ---------------------------------------------------------------------------
# 1. The parser: a verb is no place; the last PLACE named wins; roads go places
# ---------------------------------------------------------------------------

def test_going_to_remember_is_no_destination():
    """fleet8-wenna T3: the gatehouse, not "remember every detail of this"."""
    assert destination_phrases(WENNA_T3) == ["the North Gatehouse"]
    assert destination_phrase("I'm going to head for the gate.") == "the gate"
    # fleet3-tamsin T12's "what I'm going to tell the Captain" was a false destination
    assert destination_phrase("I need water, needle and thread, and time to think about what I'm going to "
                              "tell the Captain.") is None
    assert destination_phrase("I'm going to sleep.") is None
    assert destination_phrase("I head to town.") == "town"


def test_a_time_clause_holds_only_the_going_inside_it():
    """fleet8-tamsin T17/T20: the going after "until I'm into the shadows," and
    "Once I'm into the darker trees," is now; "before I…" still plans."""
    assert destination_phrase(TAMSIN_T17) == "the north road"
    assert destination_phrase(TAMSIN_T20) == "the north road"
    assert destination_phrase("When I reach the gate I head for the Hollow.") is None
    assert destination_phrase("Before I head for an inn to sleep, I want to see who's still lingering.") is None
    assert destination_phrase("As I prepare to walk into Wolf's Hollow, I check my knife.") is None


def test_the_line_takes_the_last_place_named_that_is_a_place(emberfall):
    line = travel_intent.directive(emberfall["player"], WENNA_T3)
    assert line.startswith("[THIS REPLY: the player goes to North Gatehouse — write @move North Gatehouse first")
    # a later phrase that names no place never hides the one that does
    text = "I make for the gatehouse, and then I wander over to the notice board."
    assert destination_phrases(text) == ["the gatehouse", "the notice board"]
    assert "@move North Gatehouse first" in travel_intent.directive(emberfall["player"], text)


def test_the_north_road_goes_where_the_road_goes(emberfall):
    pid = emberfall["player"]
    # From the square the north road is the gatehouse it leaves from
    line = travel_intent.directive(pid, "I make for the north road.")
    assert line.startswith('[THIS REPLY: the player goes to North Gatehouse (they said "the north road") — '
                           "write @move North Gatehouse first")
    # ...from the stable yard too (its nearer end)
    _put(emberfall, "Stable Yard")
    assert "@move North Gatehouse first" in travel_intent.directive(
        pid, "I head toward the north road with purpose, determined not to look back.")
    # ...from the gatehouse it is the Hollow, at its far end
    _put(emberfall, "North Gatehouse", inventory=PASS)
    assert "@move Wolf's Hollow first" in travel_intent.directive(pid, "I make for the north road.")
    # ...and from the Hollow, home is the one way out
    _put(emberfall, "Wolf's Hollow")
    for text in (TAMSIN_T17, "I head back to town.", "I keep moving toward Emberfall's lights.",
                 "I turn and head back down the road toward Emberfall, keeping my pace steady."):
        assert "@move North Gatehouse first" in travel_intent.directive(pid, text), text
    # A bare "the road" is the road they stand on
    assert travel_intent.directive(pid, "I back away slowly and head for the road.") is None


def test_the_line_is_read_back_after_the_reply(emberfall):
    pid = emberfall["player"]
    assert travel_intent.issued("x\n" + travel_intent.directive(pid, "I make for the north road.") + "\ny") == {
        "place": "North Gatehouse", "shut": False, "why": ""}
    shut = travel_intent.issued(travel_intent.directive(pid, "I make for the Hollow."))
    assert shut["place"] == "Wolf's Hollow" and shut["shut"] is True and "gate pass" in shut["why"]
    assert travel_intent.issued("[THIS REPLY: @commands first; at most 3 short speaker lines]") is None


# ---------------------------------------------------------------------------
# 2. The engine walks them when the reply ignored the line
# ---------------------------------------------------------------------------

def _walk(ids, prose, *, context_input=TAMSIN_T11, results=(), rolls=(), fought=False, spoken_text=""):
    from src.web.server import _walk_as_said

    context = "THE SCENE\n" + (travel_intent.directive(ids["player"], context_input) or "")
    runner = CommandRunner(ids["player"])
    runner.results = list(results)
    with get_session() as session:
        left_from = session.get(Player, ids["player"]).current_location_id
    return _walk_as_said(ids["player"], context, runner, left_from, prose, list(rolls), fought=fought,
                         spoken_text=spoken_text)


TAMSIN_T11_PROSE = ("The North Gatehouse rises ahead, iron-barred and lantern-lit, a Watch man stamping his feet "
                    "against the cold beside the great crossbar.")


def test_an_ignored_travel_line_is_walked_by_the_engine(emberfall):
    """fleet8-tamsin T11: line issued, no @move, the gate staged in prose."""
    walk = _walk(emberfall, TAMSIN_T11_PROSE)
    assert walk["ledger"].startswith("(engine) @move North Gatehouse: the player said they go to North Gatehouse")
    assert "the engine walked them there, as they said" in walk["ledger"]
    assert _where(emberfall) == "North Gatehouse"


def test_a_reply_that_wrote_its_own_move_is_left_alone(emberfall):
    moved = SimpleNamespace(verb="move", ok=False, line="@move Market Square", message="already here")
    assert _walk(emberfall, TAMSIN_T11_PROSE, results=[moved]) is None
    assert _where(emberfall) == "Market Square"
    # no line, no walk
    assert _walk(emberfall, TAMSIN_T11_PROSE, context_input="I ask Piper about the howls.") is None


def test_a_held_player_a_failed_roll_or_a_fight_stays_and_is_told(emberfall):
    """fleet8-wenna T3: a hand on her sleeve before she had gone three steps."""
    held = _walk(emberfall, "The Herbalist's Apprentice catches your sleeve before you've gone three steps.",
                 context_input=WENNA_T3)
    assert held["audit"]["check"] == "travel_not_taken"
    assert "they are still at Market Square" in held["audit"]["detail"]
    assert _walk(emberfall, TAMSIN_T11_PROSE, rolls=["failure"])["audit"]["check"] == "travel_not_taken"
    assert _walk(emberfall, TAMSIN_T11_PROSE, fought=True)["audit"]["check"] == "travel_not_taken"
    assert _where(emberfall) == "Market Square"


@pytest.mark.parametrize("prose", [
    # fleet7-bram T3: the place named two sentences on never excuses the hold
    "You leave him to his notice board. Before you've gone three steps, a young woman with green-stained fingers "
    "falls into step half-beside you, words already spilling out. The gatehouse is three minutes north.",
    # fleet4-tamsin T12
    "He's already turning back when a quick step cuts across your path — a young woman, working up the nerve to "
    "plant herself directly in your way before you can escape toward the gatehouse.",
    # fleet5-tamsin T3
    "But before you can take more than a step toward the gatehouse, she's scrambling after you.",
])
def test_a_hold_short_of_the_place_is_never_walked(emberfall, prose):
    """Replayed over every fleet: these three were walked off mid-conversation."""
    walk = _walk(emberfall, prose)
    assert walk["audit"]["check"] == "travel_not_taken"
    assert _where(emberfall) == "Market Square"


@pytest.mark.parametrize("prose", [
    "Nothing stops you. The North Gatehouse rises ahead, a Watch man stamping his feet.",
    "A watcher stands at the treeline. He doesn't move to block you; the gatehouse lamps grow ahead.",  # -tamsin T20
    "The guard stops you under the Gatehouse arch with a raised hand.",                                 # there
    "Piper catches your sleeve, then lets go. You reach the North Gatehouse as the lamps are lit.",
])
def test_a_hold_that_is_none_or_at_the_place_still_walks(emberfall, prose):
    assert _walk(emberfall, prose)["ledger"].startswith("(engine) @move North Gatehouse")
    assert _where(emberfall) == "North Gatehouse"


def test_a_reply_that_slept_is_never_walked_after(emberfall):
    """fleet-bram T18: "I head toward the nearest boarding house" and the reply
    wrote @rest — the night passed; nothing walks them on in the morning."""
    rested = SimpleNamespace(verb="rest", ok=True, line="@rest", message="")
    assert _walk(emberfall, "You find a cot and sleep hard.", context_input="I head for the tavern.",
                 results=[rested]) is None
    assert _where(emberfall) == "Market Square"


def test_a_shut_way_shown_shut_is_no_nag_but_one_passed_anyway_is(emberfall):
    """The line said shut and the reply showed the stop: it did what was asked."""
    _put(emberfall, "North Gatehouse", hour=22, inventory=PASS)
    shown = _walk(emberfall, "The guard shakes his head. \"Barred till first light, seal or no seal.\"",
                  context_input="I make for the Hollow.")
    assert shown is None
    # ...said by the guard in his own line, the prose only showing his head shake
    assert _walk(emberfall, "The guard shakes his head.", context_input="I make for the Hollow.",
                 spoken_text="Barred till first light, seal or no seal.") is None
    # fleet8-tamsin T12: the guard read the seal and the gate swung wide
    passed = _walk(emberfall, "The gate's barred for the night, but the guard squints at the seal. The gate swings "
                              "wide enough to let you through.", context_input="I make for the Hollow.")
    assert passed["audit"]["check"] == "travel_not_taken" and "They are still at North Gatehouse" in \
        passed["audit"]["detail"]
    assert _where(emberfall) == "North Gatehouse"


def test_a_shut_way_is_never_walked_and_says_what_stops_them(emberfall):
    """fleet8-tamsin T20: "the road home" at 23:00, the gate barred for the night."""
    _put(emberfall, "Wolf's Hollow", hour=23, inventory=PASS)
    walk = _walk(emberfall, "The trees close ranks behind you; the road back to the gate stretches out pale.",
                 context_input=TAMSIN_T20)
    assert walk["audit"]["check"] == "travel_not_taken"
    detail = walk["audit"]["detail"]
    assert "The player set out for North Gatehouse, but the way is shut to them" in detail
    assert "They are still at Wolf's Hollow" in detail and "only a won roll or a bribe" in detail
    assert _where(emberfall) == "Wolf's Hollow"


def test_a_way_the_reply_opened_is_theirs_to_take_next(emberfall):
    """The line said shut (no pass); the reply handed the pass over and wrote
    no @move: nothing walks them 45 minutes up a road nobody narrated."""
    from src.web.server import _walk_as_said

    context = travel_intent.directive(emberfall["player"], "I make for the Hollow.")
    assert "the way is shut to them" in context
    _put(emberfall, "Market Square", inventory=PASS)
    runner = CommandRunner(emberfall["player"])
    assert _walk_as_said(emberfall["player"], context, runner, emberfall["Market Square"], "", []) is None
    assert _where(emberfall) == "Market Square"


def test_a_place_staged_ahead_with_its_own_speaker_is_an_arrival(emberfall):
    """arrival_without_move: "X rises ahead" and someone who stands at X speaks."""
    from src.web.turn_audit import audit_turn

    with get_session() as session:
        guard = NPC(name="Sergeant Hobb", profession="Gate guard", current_location_id=emberfall["North Gatehouse"])
        session.add(guard)
        session.commit()
        guard_id = guard.id
    state = {"location_id": emberfall["Market Square"], "location": "Market Square"}
    spoken = [{"npc_id": guard_id, "npc_name": "Gate Guard", "text": "Gate's closed past the howl."}]
    warnings = audit_turn(TAMSIN_T11_PROSE, [], state, state, spoken=spoken)
    hit = [w for w in warnings if w["check"] == "arrival_without_move"]
    assert hit and "walked the player up to North Gatehouse" in hit[0]["detail"]
    assert "@move North Gatehouse first" in hit[0]["detail"]
    # Nobody from there speaking: a gate seen ahead is no arrival
    assert "arrival_without_move" not in [w["check"] for w in audit_turn(TAMSIN_T11_PROSE, [], state, state)]
    # The real T11 guard had no row: a voice of the place's own counts ("Gate Guard" at the Gatehouse)...
    voice = [{"npc_id": None, "npc_name": "Gate Guard", "text": "Gate's closed past the howl."}]
    hit = [w for w in audit_turn(TAMSIN_T11_PROSE, [], state, state, spoken=voice)
           if w["check"] == "arrival_without_move"]
    assert hit and "voiced Gate Guard, who is at North Gatehouse" in hit[0]["detail"]
    # ...an unbacked voice of nowhere in particular does not
    other = [{"npc_id": None, "npc_name": "Shawled Woman", "text": "Wolves again."}]
    assert "arrival_without_move" not in [w["check"] for w in audit_turn(TAMSIN_T11_PROSE, [], state, state,
                                                                          spoken=other)]
    # ...nor when the move landed
    moved = {"location_id": emberfall["North Gatehouse"], "location": "North Gatehouse"}
    assert "arrival_without_move" not in [w["check"] for w in audit_turn(TAMSIN_T11_PROSE, [], state, moved,
                                                                            spoken=spoken)]


# ---------------------------------------------------------------------------
# 3. Consent: a job its giver pitches this reply waits for the player's yes
# ---------------------------------------------------------------------------

def _start(ids, said, line="@quest-start Counting the Howls"):
    runner = CommandRunner(ids["player"])
    runner.player_input = said
    return runner.run(line)


def _status(ids, title="Counting the Howls"):
    with get_session() as session:
        return session.get(Quest, ids[title]).status


def test_a_job_pitched_in_this_reply_waits_for_the_yes(emberfall):
    """fleet8-wenna T3: she was walking off; Piper pitched and the same reply started it."""
    result = _start(emberfall, WENNA_T3)
    assert not result.ok
    assert "wait for their answer — they have not said yes" in result.message
    assert _status(emberfall) == QuestStatus.NOT_STARTED
    # fleet6-tamsin T4: talking to the stable boy; Piper pitched and started it at once
    assert not _start(emberfall, "I turn toward the straw-haired boy. \"You've got good eyes. Did you see "
                                 "his face?\"").ok
    # ...and by the other door, the same
    assert not _start(emberfall, WENNA_T3, "@quest-new Counting the Howls | about: count | step: custom count").ok
    assert _status(emberfall) == QuestStatus.NOT_STARTED


@pytest.mark.parametrize("said", [
    'I nod slowly. "I\'m in," I say.',                                   # fleet6-wenna T2
    "Alright. I'll talk to him.",                                        # fleet7-bram T5
    "I'll ask the stable hand what he hears after dark.",
    "Tell me about Counting the Howls first.",                           # asked for by name
])
def test_a_yes_in_the_players_own_words_starts_it(emberfall, said):
    assert _start(emberfall, said).ok
    assert _status(emberfall) == QuestStatus.ACTIVE


def test_a_job_pitched_in_an_earlier_reply_starts_and_the_opening_hands_its_job_over(emberfall):
    # fleet8-bram T9 -> T10: Piper pitched it a reply before
    job_offers.note_reply(emberfall["player"], [{
        "npc_id": emberfall["Piper Quillow"],
        "text": "Ten coin and a tincture of mine if you'll stand out after dark and listen where I can't.",
    }])
    assert job_offers.offered_before(emberfall["player"], emberfall["Counting the Howls"])
    assert _start(emberfall, "I turn to face Piper fully. \"Before I go back out there — what do you know?\"").ok
    # The [OPENING] writes its job's start as the directive says; the CLI never checks
    assert _start(emberfall, "[OPENING] This is Wenna Cole's first moment in the world.",
                  "@quest-start Teeth on the North Road").ok
    runner = CommandRunner(emberfall["player"])
    assert runner.run("@quest-start A Letter for a Buried Man").ok


def test_an_answer_the_offer_book_read_as_a_no_never_blocks_the_start(emberfall):
    """fleet7-bram T2: "I'll take a look … I'm not doing it for the Watch" was
    read as a decline, and the start that followed was right. The reader now
    hears it as the yes it is (a reason, not a refusal)."""
    pid, captain = emberfall["player"], emberfall["Captain Brann Coldiron"]
    job_offers.note_reply(pid, [{"npc_id": captain, "text": "Walk up there, you tell me which it is. Sixty coin, "
                                                            "a tooth for proof. Take it or leave it."}])
    said = ("I nod slow, weighing it. \"I'll take a look,\" I say. \"But I'm not doing it for the Watch. "
            "We clear on that?\"")
    job_offers.start_directive(pid, said, addressed_npc_id=captain)
    assert job_offers.open_offers(pid)[emberfall["Teeth on the North Road"]]["status"] == "accepted"
    assert _start(emberfall, said, "@quest-start Teeth on the North Road").ok


def test_a_pitch_the_offer_book_missed_is_never_stuck_behind_the_guard(emberfall):
    """Pitched in prose (no speaker line the offer book could read) and its
    start refused: the next reply's start stands unless the player said no."""
    first = CommandRunner(emberfall["player"])
    first.player_input = "I look around the square."
    assert not first.run("@quest-start Counting the Howls").ok
    first.ledger()                                   # the reply ends
    # A yes in words the reader misses ("You've got yourself a helper")
    second = CommandRunner(emberfall["player"])
    second.player_input = "You've got yourself a helper, then."
    assert second.run("@quest-start Counting the Howls").ok
    assert _status(emberfall) == QuestStatus.ACTIVE


def test_a_no_after_a_missed_pitch_still_holds(emberfall):
    first = CommandRunner(emberfall["player"])
    first.player_input = "I look around the square."
    assert not first.run("@quest-start Counting the Howls").ok
    first.ledger()
    second = CommandRunner(emberfall["player"])
    second.player_input = "No. Not tonight."
    assert not second.run("@quest-start Counting the Howls").ok
    assert _status(emberfall) == QuestStatus.NOT_STARTED


# ---------------------------------------------------------------------------
# 4. A gate barred for the night: never the pass alone
# ---------------------------------------------------------------------------

def _gate_at_night(ids, hour=21):
    _put(ids, "North Gatehouse", hour=hour, inventory=PASS)


def test_through_on_the_pass_alone_is_refused_at_night(emberfall):
    """fleet8-tamsin T11-T13."""
    _gate_at_night(emberfall)
    result = CommandRunner(emberfall["player"]).run("@move Wolf's Hollow | through")
    assert not result.ok and result.result["refused"] == "barred_tonight"
    assert result.message.startswith("the way to Wolf's Hollow is barred for the night — a pass does not open it")
    assert "a won roll" in result.message and "first light" in result.message
    # ...and the plain move says the same, not "write | through"
    plain = CommandRunner(emberfall["player"]).run("@move Wolf's Hollow")
    assert not plain.ok and "a pass does not open it" in plain.message
    # ...a failed roll opens nothing
    runner = CommandRunner(emberfall["player"])
    runner.roll_outcomes.append("failure")
    assert not runner.run("@move Wolf's Hollow | through").ok
    assert _where(emberfall) == "North Gatehouse"
    # From the square, round through the town: the same barred gate
    _put(emberfall, "Market Square")
    assert CommandRunner(emberfall["player"]).run("@move Wolf's Hollow | through").result["refused"] == "barred_tonight"


@pytest.mark.parametrize("how", ["roll now", "roll last reply", "bribe", "bribe by another name"])
def test_a_won_roll_or_a_bribe_opens_it(emberfall, how):
    _gate_at_night(emberfall)
    if how == "roll last reply":
        last = CommandRunner(emberfall["player"])
        last.roll_outcomes.append("partial")
        last.ledger()                      # the server takes each reply's ledger at its end
    runner = CommandRunner(emberfall["player"])
    if how == "roll now":
        runner.roll_outcomes.append("success")
    if how == "bribe":
        assert runner.run("@coin -3 | slipped to the gate sergeant").ok
    if how == "bribe by another name":                  # an alias of @coin is the same coin
        assert runner.run("@gold -3 | slipped to the gate sergeant").ok
    assert runner.run("@move Wolf's Hollow | through").ok
    assert _where(emberfall) == "Wolf's Hollow"


@pytest.mark.parametrize("how", ["a tip elsewhere", "a won word about something else"])
def test_a_roll_or_coin_about_something_else_opens_nothing(emberfall, how):
    """Round-7 travel review: any won roll or any coin paid out in two
    replies opened the barred gate — a tip in the stable yard included."""
    _gate_at_night(emberfall)
    last = CommandRunner(emberfall["player"])
    if how == "a tip elsewhere":
        assert last.run("@coin -1 | tossed to the stable hand for his trouble tonight").ok
    else:
        last.rolls.append({"outcome": "success", "reason": "coaxing the herbalist into talking"})
        last.roll_outcomes.append("success")
    last.ledger()
    assert not CommandRunner(emberfall["player"]).run("@move Wolf's Hollow | through").ok
    # ...while a roll FOR the way does open it
    runner = CommandRunner(emberfall["player"])
    runner.rolls.append({"outcome": "partial", "reason": "talking the night sentry into lifting the bar"})
    runner.roll_outcomes.append("partial")
    assert runner.run("@move Wolf's Hollow | through").ok


def test_an_engine_charged_price_is_no_bribe(emberfall):
    """stakes.settle_owed_cost runs its own runner after a reply: its @coin
    is the engine's price, and that runner is never "the last reply"."""
    _gate_at_night(emberfall)
    CommandRunner(emberfall["player"]).ledger()
    assert CommandRunner(emberfall["player"]).run("@coin -2 | the price of the failed climb").ok
    assert not CommandRunner(emberfall["player"]).run("@move Wolf's Hollow | through").ok


def test_the_cli_reads_the_dice_too(emberfall):
    """The terminal's replies run the same runner: a won roll there opens the
    barred gate as it does on the web."""
    import io

    from rich.console import Console

    from src.game.cli_stream import CliReplyHandler

    _gate_at_night(emberfall)
    handler = CliReplyHandler(Console(file=io.StringIO()), emberfall["player"])
    handler.start_turn([])
    handler(data="@move Wolf's Hollow | through" + chr(10))
    assert _where(emberfall) == "North Gatehouse"
    rolled = json.dumps({"event": "check_result", "outcome": "success", "skill": "stealth"})
    handler(message={"role": "user", "content": [{"toolResult": {
        "toolUseId": "t1", "status": "success", "content": [{"text": rolled}]}}]})
    handler(data=chr(10).join(["You slip along the wall while the sergeant dozes.",
                               "@move Wolf's Hollow | through", ""]))
    handler.finish_turn()
    assert _where(emberfall) == "Wolf's Hollow"


def test_by_day_the_pass_still_opens_the_road(emberfall):
    _gate_at_night(emberfall, hour=10)
    assert CommandRunner(emberfall["player"]).run("@move Wolf's Hollow").ok


def test_the_brief_and_the_travel_line_say_the_pass_is_no_good_tonight(emberfall):
    from src.services.scene_brief import _exits
    from src.services.travel import check_route

    _gate_at_night(emberfall)
    with get_session() as session:
        player = session.get(Player, emberfall["player"])
        here = session.get(Location, emberfall["North Gatehouse"])
        exits = _exits(session, player, here)
        route = check_route(session, emberfall["Market Square"], emberfall["Wolf's Hollow"], player)
    hollow = next(line for line in exits if "Wolf's Hollow" in line)
    assert "BLOCKED" in hollow and "does not open it tonight" in hollow
    assert route["barred_tonight"] is True and route["reason"] == "gate"
    line = travel_intent.directive(emberfall["player"], "I make for the Hollow.")
    assert "the way is barred for the night and a pass does not open it" in line
    assert "only a won roll or a paid bribe in this reply gets them through" in line
    # ...and the ways still open are named, not a dead end (round 8)
    assert "a price (a bribe" in line and "a hard climb over the wall" in line and "a bed till first light" in line
    # ...and never that the pass in their pack is missing
    assert "(barred for the night until first light; the Gate Pass they carry opens it by day)" in line
    assert "needs a gate pass" not in line
    assert travel_intent.issued(line)["shut"] is True


# ---------------------------------------------------------------------------
# 5. Who walks along goes along
# ---------------------------------------------------------------------------

def _walked(ids, prose, *, frm="Market Square", to="North Gatehouse"):
    from src.web.server import _walked_along

    _put(ids, to)
    return _walked_along(ids["player"], prose, ids[frm])


def test_the_captain_who_walked_him_to_the_gatehouse_is_there(emberfall):
    """fleet8-bram T2."""
    lines = _walked(emberfall, BRAM_T2_PROSE)
    assert lines == ["(engine) Captain Brann Coldiron walked along with the player to North Gatehouse in your "
                     "story, so the engine moved them there too. If they turned back, write @npc-move Captain "
                     "Brann Coldiron | Market Square."]
    assert _where(emberfall, "Captain Brann Coldiron") == "North Gatehouse"
    assert _where(emberfall, "Piper Quillow") == "Market Square"


@pytest.mark.parametrize("prose", [
    "The Captain watches you go, arms folded.",
    "The Captain doesn't follow; he walks you to the edge of the square with his eyes only.",
    "Piper waves you off. He falls in beside you.",                    # "he" is never Piper
    "The walk takes you across the square to the gatehouse.",
    "The Captain watches you both go, arms folded.",
    "The Captain's warning goes with you all the way to the gate.",    # his warning, not him
    "The hawker shrugs. You're both of a mind, it seems.",
    "The Captain says nothing; he walks you to the edge of the square and stops.",
])
def test_nobody_left_behind_is_carried(emberfall, prose):
    assert _walked(emberfall, prose) == []
    assert _where(emberfall, "Captain Brann Coldiron") == "Market Square"
    assert _where(emberfall, "Piper Quillow") == "Market Square"


def test_his_jaw_then_he_falls_in_is_him(emberfall):
    assert _walked(emberfall, "Coldiron's jaw sets; he falls in beside you without a word.")
    assert _where(emberfall, "Captain Brann Coldiron") == "North Gatehouse"


def test_a_named_companion_by_role_label_goes_but_never_the_villain_nor_through_a_gate(emberfall):
    from src.models.campaign import Campaign

    assert _walked(emberfall, "The Herbalist's Apprentice falls in beside you, chattering.")
    assert _where(emberfall, "Piper Quillow") == "North Gatehouse"
    # The campaign villain is staged by the story, never by the prose
    with get_session() as session:
        session.add(Campaign(player_id=emberfall["player"], status="active",
                             villain_npc_id=emberfall["Captain Brann Coldiron"]))
        session.commit()
    assert _walked(emberfall, "The Captain falls in beside you.") == []
    # A gated place is never walked into: square -> Hollow is the gate's road
    _put(emberfall, "Market Square")
    with get_session() as session:
        session.get(NPC, emberfall["Hesk Bramble"]).current_location_id = emberfall["Market Square"]
        session.commit()
    assert _walked(emberfall, "The Hawker falls in beside you.", to="Wolf's Hollow") == []


# ---------------------------------------------------------------------------
# Through /api/play: the engine's walk lands in the next ledger
# ---------------------------------------------------------------------------

class _FakeAgent:
    def __init__(self, events):
        self.events = events
        self.messages = []

    async def stream_async(self, context):
        for event in self.events:
            yield event


class _FakeDM:
    def __init__(self, events, player_id):
        self.agent = _FakeAgent(events)
        self.last_brief = {"present": {}}
        self._player_name = "Tamsin Rook"
        self.player_id = player_id

    def _build_context(self, player_input, **_kwargs):
        line = travel_intent.directive(self.player_id, player_input) or ""
        return f"THE SCENE\n{line}\nPlayer says: {player_input}"


def test_the_stream_walks_them_and_tells_the_dm(emberfall, monkeypatch):
    from src.services import memory_files
    from src.web import server
    from src.web.streaming import ToolUsageTracker

    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    dm = _FakeDM([{"data": TAMSIN_T11_PROSE + "\n"}], emberfall["player"])
    monkeypatch.setattr(server, "get_or_create_session", lambda pid: {"dm": dm, "tool_tracker": ToolUsageTracker()})
    monkeypatch.setattr(server, "_engine_ambush", lambda *a, **k: None)
    monkeypatch.setattr(server, "maybe_advance_world", lambda: None)
    server._pending_command_ledger.pop(emberfall["player"], None)

    async def run():
        response = await server.play_sse(server.GameRequest(player_input=TAMSIN_T11, player_id=emberfall["player"]))
        return [chunk async for chunk in response.body_iterator]

    frames = []
    for chunk in asyncio.run(run()):
        for line in str(chunk).splitlines():
            if line.startswith("data: ") and line[6:].strip() not in ("", "{}"):
                frames.append(json.loads(line[6:]))
    complete = next(f for f in frames if f.get("type") == "complete")
    assert complete["commands"] == []
    assert complete["engine_walks"][0].startswith("(engine) @move North Gatehouse")
    assert "arrival_without_move" not in [w["check"] for w in complete["audit"]]
    assert any(f.get("type") == "scene" and f.get("location") == "North Gatehouse" for f in frames)
    assert _where(emberfall) == "North Gatehouse"
    ledger = server._pending_command_ledger.pop(emberfall["player"])
    assert any("the engine walked them there, as they said" in line for line in ledger)

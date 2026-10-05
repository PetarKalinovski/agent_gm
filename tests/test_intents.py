"""What the player reaches for: the thing held out, a room, a page.

Round 8 (fleet9): the Gate Pass and Piper's chart were held out ("holds it
out — not quite letting go yet", "She holds the chart out… Will you take
it?"), the player typed "I take the pass carefully" / "I take the chart and
tuck it into my satchel", and no @give was ever written (fleet9-wenna T6-T10,
fleet9-bram T5-T6 and T18-T19). A room was paid for in prose (wenna T20), the
guest ledger granted and never read (T20), a crate opened "just enough to
see what's inside" put its rope in the pack (bram T14) and kept it after
"leave the rope where it is" (T15). src/services/intents.py binds each in
the context, and gives what was taken when the reply did not.
"""

from types import SimpleNamespace

import pytest

from src.data.playtest_seed import create_playtest_world
from src.models import NPC, Event, Location, Player, Quest, QuestStatus, get_session, reset_engine
from src.services import intents, memory_files, world_tick
from src.services.commands import CommandRunner
from src.tools.world_write.items import _validate_inventory
from src.tools.world_write.quests import activate_quest

# fleet9-wenna T6, as the DM wrote it (real names)
HELD_OUT_PASS = (
    "The square has settled into its evening rhythm.\n\n"
    "Captain Brann Coldiron: \"Gatehouse send you back empty-handed?\"\n\n"
    "He pulls a folded paper from inside his coat, breath fogging faintly, and holds it out — not quite "
    "letting go yet.\n\n"
    "Captain Brann Coldiron: \"This gets you past the gate at first light, not before.\"\n\n"
    "The pass waits in his hand, close enough to take.\n"
    "@move Market Square\n"
)
# fleet9-wenna T9
HELD_OUT_CHART = (
    "Her shoulders drop half an inch.\n\n"
    "Piper Quillow: \"I can't pay much. Ten coin, and a tincture I made myself.\"\n\n"
    "She holds the chart out a little further, like letting go of it is the hard part.\n\n"
    "Piper Quillow: \"Will you help me prove it?\"\n"
)


@pytest.fixture()
def world(tmp_path):
    reset_engine()
    create_playtest_world(str(tmp_path / "emberfall.db"))
    intents.reset()
    with get_session() as session:
        place = {loc.name: loc.id for loc in session.query(Location).all()}
        people = {npc.name: npc.id for npc in session.query(NPC).all()}
        player = Player(name="Wenna Cole", current_location_id=place["Market Square"], currency=25)
        session.add(player)
        session.commit()
        teeth = session.query(Quest).filter_by(title="Teeth on the North Road").one().id
        howl = session.query(Event).filter_by(name="The Evening Howl").one().id
        ids = {"player": player.id, "teeth": teeth, "howl": howl, **place, **people}
    yield ids
    intents.reset()
    reset_engine()


def _present(w, *names):
    return {w[n]: n for n in names}


def _pack(w):
    with get_session() as session:
        return [str(e.get("name")) for e in session.get(Player, w["player"]).inventory or []]


def _held_by(w, name):
    with get_session() as session:
        return [str(e.get("name")) for e in _validate_inventory(session.get(NPC, w[name]).inventory_notable or [])]


def _start_teeth(w):
    assert activate_quest(quest_id=w["teeth"]).get("success") is not False


def _step_done(w, quest_id, text_start):
    with get_session() as session:
        objectives = session.get(Quest, quest_id).objectives
    return next(o for o in objectives if str(o.get("text", "")).startswith(text_start)).get("done")


def _move(w, place):
    with get_session() as session:
        session.get(Player, w["player"]).current_location_id = w[place]
        session.commit()


# -- the seed: who carries the pass ----------------------------------------

def test_the_captain_carries_the_gate_pass_and_it_is_not_for_sale(world):
    from src.services.scene_brief import build_scene_brief

    assert "Gate Pass" in _held_by(world, "Captain Brann Coldiron")
    with get_session() as session:
        entry = next(e for e in session.get(NPC, world["Captain Brann Coldiron"]).inventory_notable
                     if e["name"] == "Gate Pass")
        assert entry["type"] == "quest_item"
        brief = build_scene_brief(session, session.get(Player, world["player"]))["text"]
    sells = [line for line in brief.splitlines() if "Sells" in line and "Coldiron" in line]
    assert sells and "Lantern Watch Coat" in sells[0] and "Gate Pass" not in sells[0]


def test_the_pass_he_carries_makes_the_captain_no_stall(world):
    # A thief's or trader's "wares and till" is a shelf of two or more things
    # for sale: the coat alone was none, and the pass beside it is no ware
    from src.services.scene_brief import _persona_here

    with get_session() as session:
        player = session.get(Player, world["player"])
        captain = session.get(NPC, world["Captain Brann Coldiron"])
        hesk = session.get(NPC, world["Hesk Bramble"])
        place = session.get(Location, world["Market Square"])
        assert not any("wares and till" in x for x in _persona_here(session, player, "thief", [captain], place))
        assert any("wares and till" in x for x in _persona_here(session, player, "thief", [captain, hesk], place))


def test_holder_lines_say_who_carries_an_open_step_item(world):
    assert intents.holder_lines(world["player"], _present(world, "Captain Brann Coldiron")) == ""  # no quest yet
    _start_teeth(world)
    here = intents.holder_lines(world["player"], _present(world, "Captain Brann Coldiron"))
    assert "Gate Pass" in here and "Captain Brann Coldiron's keeping, here" in here
    assert "@give Gate Pass" in here and "Never send the player elsewhere" in here
    away = intents.holder_lines(world["player"], {})
    assert "at Market Square" in away and "only they hand it over" in away


# -- @give from the hand that holds it --------------------------------------

def test_give_of_a_held_key_moves_it_from_the_holder_and_ticks_the_step(world):
    _start_teeth(world)
    runner = CommandRunner(world["player"], present_ids=[world["Captain Brann Coldiron"]])
    result = runner.run("@give Gate Pass | kind: key")
    assert result.ok, result.message
    assert "Gate Pass" in _pack(world)
    assert "Gate Pass" not in _held_by(world, "Captain Brann Coldiron")
    assert "Lantern Watch Coat" in _held_by(world, "Captain Brann Coldiron")
    assert _step_done(world, world["teeth"], "Get Captain Coldiron's gate pass") is True
    # never twice: the pack has it now, the holder no longer does
    again = runner.run("@give Gate Pass | kind: key")
    assert _pack(world).count("Gate Pass") == 1 or not again.ok


def test_give_of_a_merchants_ware_never_empties_his_shelf(world):
    runner = CommandRunner(world["player"], present_ids=[world["Hesk Bramble"], world["Piper Quillow"]])
    assert runner.run("@give Willow-Bark Draught | kind: potion").ok
    with get_session() as session:
        stock = {e["name"]: e.get("quantity") for e in session.get(NPC, world["Hesk Bramble"]).inventory_notable}
    assert stock["Willow-Bark Draught"] == 3


def test_give_from_a_holder_who_is_not_here_makes_no_transfer(world):
    _move(world, "Stable Yard")
    runner = CommandRunner(world["player"], present_ids=[world["Sorrel Tack"]])
    assert runner.run("@give Gate Pass | kind: key").ok
    assert "Gate Pass" in _held_by(world, "Captain Brann Coldiron")


# -- the take line and the engine's fallback ---------------------------------

def test_player_takes_reads_the_hand_closing(world):
    takes = intents.player_takes('I take the pass with a quiet nod, tucking it into my coat. "Thanks," I say.')
    assert [t["phrase"] for t in takes] == ["pass"]
    assert [t["phrase"] for t in intents.player_takes("I pocket the key and finish my ale in one long pull.")] \
        == ["key"]
    assert [t["phrase"] for t in intents.player_takes("I nod and take the pass, tucking it into my coat.")] \
        == ["pass"]
    # an "it" whose thing is named before it in its sentence is that thing
    assert [t["phrase"] for t in intents.player_takes("I fold the pass carefully and tuck it into my satchel.")] \
        == ["pass"]
    for nothing in ("I take a pull of the ale and nod slowly.", "I take her hand gently.",
                    "I take a step back, then another.", "I tuck my notebook away.",
                    '"I\'ll take it," I say.', "I don't take the coin."):
        assert intents.player_takes(nothing) == [], nothing


def test_held_out_reads_the_reply(world):
    phrases = [o["phrase"] for o in intents.held_out(HELD_OUT_PASS)]
    assert "folded paper" in phrases and "pass" in phrases
    assert [o["phrase"] for o in intents.held_out("She slides a brass key across the bar, her hand withdrawing.")] \
        == ["brass key"]
    chart = intents.held_out(HELD_OUT_CHART)
    assert chart[0]["phrase"] == "chart" and chart[0]["speaker"] == "Piper Quillow"
    assert intents.held_out("He holds his hand out for the coin. She sets a bowl of stew on the table.") == []


def test_taking_the_held_out_pass_binds_a_give_of_the_step_item(world):
    _start_teeth(world)
    line = intents.take_directive(world["player"], "I take the pass carefully, but before I turn away, I ask.",
                                  HELD_OUT_PASS, _present(world, "Captain Brann Coldiron"))
    assert line and line.startswith(intents.TAKE_MARK)
    assert "@give Gate Pass | kind: key" in line and "from Captain Brann Coldiron" in line
    assert "pulls it back" in line


def test_the_engine_gives_what_the_reply_left_in_the_holders_hand(world):
    _start_teeth(world)
    present = _present(world, "Captain Brann Coldiron")
    line = intents.take_directive(world["player"], "I take the pass with a quiet nod.", HELD_OUT_PASS, present)
    runner = CommandRunner(world["player"], present_ids=list(present))
    # fleet9-wenna T8: "The Captain turns back to his road without another word, pass given, business done."
    took = intents.taken_fallback(world["player"], f"ctx\n\n{line}\n\nPlayer says: …", runner.results,
                                  "The Captain turns back to his road without another word, pass given.")
    assert took and took["line"] == "@give Gate Pass | kind: key"
    assert "engine put it in their pack" in took["ledger"]
    assert runner.run(took["line"]).ok
    assert "Gate Pass" in _pack(world) and "Gate Pass" not in _held_by(world, "Captain Brann Coldiron")
    assert _step_done(world, world["teeth"], "Get Captain Coldiron's gate pass") is True


def test_no_fallback_when_the_reply_gave_it_or_the_holder_kept_it(world):
    _start_teeth(world)
    present = _present(world, "Captain Brann Coldiron")
    line = intents.take_directive(world["player"], "I take the pass.", HELD_OUT_PASS, present)
    runner = CommandRunner(world["player"], present_ids=list(present))
    runner.run("@give Gate Pass | kind: key")
    assert intents.taken_fallback(world["player"], line, runner.results, "He lets it go.") is None
    # ...kept back in the story: nothing is given
    with get_session() as session:
        session.get(Player, world["player"]).inventory = []
        session.commit()
    line = intents.take_directive(world["player"], "I take the pass.", HELD_OUT_PASS, present)
    assert intents.taken_fallback(world["player"], line, [], "He pulls it back before your fingers close.") is None
    # ...and a context that never carried the line gets nothing
    intents.take_directive(world["player"], "I take the pass.", HELD_OUT_PASS, present)
    assert intents.taken_fallback(world["player"], "no line here", [], "") is None


def test_a_chart_held_out_and_taken_is_given_by_the_engine(world):
    present = _present(world, "Piper Quillow")
    line = intents.take_directive(world["player"], "I take the chart and tuck it into my satchel alongside my "
                                  "notebook.", HELD_OUT_CHART, present)
    assert line and "@give Chart | kind: clue" in line and "from Piper Quillow" in line
    took = intents.taken_fallback(world["player"], line, [], "Piper flinches at the howl.")
    runner = CommandRunner(world["player"], present_ids=list(present))
    assert runner.run(took["line"]).ok and "Chart" in _pack(world)


def test_a_thing_already_handed_over_by_a_give_is_no_take(world):
    # fleet7-wenna T18-T19: @quest-done paid the charm ("That's yours"), the
    # player then "close[s] my fingers around the fang" — the same charm
    reply = ("He presses a small leather pouch into your good hand and a fang bound in brass wire.\n"
             "Watch Captain: \"That's yours, for walking in where I couldn't send anyone.\"\n"
             "@quest-done Teeth on the North Road\n")
    assert intents.take_directive(world["player"], "I close my fingers around the fang and the coin pouch.",
                                  reply, _present(world, "Captain Brann Coldiron")) is None


def test_a_thing_lying_in_the_scene_is_interacts_not_a_hand_over(world):
    reply = "Something pale lies half-buried in the mud by the board — a dropped bundle of sweetroot."
    assert intents.take_directive(world["player"], "I take the sweetroot.", reply, {}) is None


# -- a room for the night ----------------------------------------------------

def test_asking_for_a_room_at_the_inn_binds_rest_at_its_price(world):
    _move(world, "The Drowned Lantern")
    line = intents.room_directive(world["player"], 'I set down three coin for the room and food. "Is that enough?"')
    assert line and "8 coin" in line and "@rest" in line and "Never a @coin for the room" in line
    assert intents.room_directive(world["player"], '"Can I stay the night?"')
    # an order to someone else, and a night already slept, are no asks
    assert intents.asks_for_room('"Sorrel, stay here tonight. Listen for the howl."') == ""
    assert intents.asks_for_room('"Maera, I owe you 8 coin for the room and the meal."') == ""
    assert intents.asks_for_room("I scan the room for the Keeper.") == ""
    # ...nor a night the player says they will NOT sleep — the negation sat
    # inside the matched words, past the look-behind
    for words in ("I can't sleep tonight, not with that howl.", "I doubt I'll sleep tonight.",
                  "We won't stay the night.", "I'll not stay the night."):
        assert intents.asks_for_room(words) == "", words
    assert intents.asks_for_room("I'll stay here tonight, if there's a bed.")


def test_no_room_line_where_nobody_sleeps(world):
    assert intents.room_directive(world["player"], "I need a room for the night.") is None


def test_a_room_handed_over_in_prose_is_flagged_without_rest(world):
    from src.web.turn_audit import room_without_rest

    prose = ("The Keeper takes the coin without counting it, sliding it into an apron pocket, and sets a bowl "
             "and a key on the bar.")
    said = "I set down three coin for the room and food."
    warning = room_without_rest(prose, [], said, world["The Drowned Lantern"])
    assert warning and warning["check"] == "room_without_rest" and "8 coin" in warning["detail"]
    rested = [SimpleNamespace(ok=True, verb="rest")]
    assert room_without_rest(prose, rested, said, world["The Drowned Lantern"]) is None
    assert room_without_rest(prose, [], "I ask about the howls.", world["The Drowned Lantern"]) is None


# -- a page asked for ----------------------------------------------------------

def test_asking_to_see_a_thing_here_binds_an_interact(world):
    _move(world, "The Drowned Lantern")
    line = intents.read_directive(world["player"], '"I\'d like to see your guest ledger," I say, "if I may."')
    assert line and "@interact Guest ledger" in line and "never add to them" in line
    assert intents.read_directive(world["player"], "I note the way he reads people like a ledger.") is None
    _move(world, "Market Square")
    assert intents.read_directive(world["player"], "I head straight for the Notice board, scanning it.")
    assert intents.read_directive(world["player"], '"You looking for someone to read those notes, or just '
                                                    'hoping the board grows eyes?"') is None


# -- a lid lifted only to look, and a thing left where it was ----------------

BRAM_T14 = ("I crouch down and fish the sweetroot out of the mud, shake it off. Then I move to that crate, keeping "
            "low, and ease the lid open just enough to see what's inside without disturbing it more than it "
            "already is.")


def _crate(w):
    with get_session() as session:
        objects = session.get(Location, w["Market Square"]).objects
    return next(o for o in objects if o["label"] == "Abandoned market crate")


def test_opening_a_crate_only_to_look_takes_nothing(world):
    runner = CommandRunner(world["player"], present_ids=[])
    runner.player_input = BRAM_T14
    sweet = runner.run("@interact Dropped sweetroot bundle")
    looked = runner.run("@interact Abandoned market crate")
    assert sweet.ok and "Sweetroot Bundle" in _pack(world)
    assert looked.ok and looked.result.get("looked") is True
    assert "Coil of Hemp Rope" not in _pack(world) and not _crate(world).get("consumed")
    assert "left where it lies" in looked.message
    assert any("Coil of Hemp Rope" in e and "left it there" in e for e in memory_files.read_journal(world["player"]))
    # ...nor does a @give of what it holds
    refused = runner.run("@give Coil of Hemp Rope | kind: misc")
    assert not refused.ok and "only looked" in refused.message
    # the next reply they reach for it: theirs
    later = CommandRunner(world["player"], present_ids=[])
    later.player_input = "I search the crate and take the rope."
    assert later.run("@interact Abandoned market crate").ok and "Coil of Hemp Rope" in _pack(world)


def test_care_in_a_search_is_no_look_only():
    # A not-to-move or a not-to-take whose object is not the thing: the search
    # still takes what it finds
    crate = ("Abandoned market crate", "Coil of Hemp Rope")
    for words in ("I search the crate, careful not to move too fast.",
                  "I tell Sorrel not to move while I search the crate.",
                  "I don't take my eyes off the Captain as I open the crate.",
                  "I search the crate without making a sound."):
        assert not intents.look_only(words, *crate, container=True), words
    for words in ("I open the crate, careful not to touch anything.", "I look in the crate but I don't touch it.",
                  BRAM_T14):
        assert intents.look_only(words, *crate, container=True), words


def test_a_thing_the_last_reply_took_goes_back_when_the_player_leaves_it(world):
    runner = CommandRunner(world["player"], present_ids=[])
    runner.player_input = "I search the abandoned crate."
    assert runner.run("@interact Abandoned market crate").ok and "Coil of Hemp Rope" in _pack(world)
    line = intents.put_back_line(world["player"], "I pocket the sweetroot and leave the rope where it is — "
                                                  "might be someone's, might be bait.")
    assert line and "Coil of Hemp Rope" in line and "never @give or @remove it" in line
    assert "Coil of Hemp Rope" not in _pack(world) and not _crate(world).get("consumed")
    # spent: only the reply right after counts
    assert intents.put_back_line(world["player"], "I leave the rope where it is.") is None


def test_words_that_leave_nothing_keep_the_take(world):
    runner = CommandRunner(world["player"], present_ids=[])
    runner.player_input = "I search the abandoned crate."
    runner.run("@interact Abandoned market crate")
    assert intents.put_back_line(world["player"], "I coil the rope over my shoulder and head out.") is None
    assert "Coil of Hemp Rope" in _pack(world)
    # ...nor do people left be
    intents.note_taken(world["player"], world["Market Square"], "crate", "Abandoned market crate",
                       [{"name": "Coil of Hemp Rope", "quantity": 1}])
    assert intents.put_back_line(world["player"], "I nod to the pair and leave them be.") is None
    assert "Coil of Hemp Rope" in _pack(world)


# -- the howl the roll-first hold would have thrown away ----------------------

def test_a_must_deliver_sentence_survives_the_roll_first_drop(world, monkeypatch):
    monkeypatch.setattr(world_tick, "_must_deliver", {world["howl"]})
    held = ("Right as your hand closes around the coin, the howl breaks over the square — three long notes, then "
            "silence. Piper takes the coin with a grateful little nod.")
    kept = intents.must_deliver_sentences(held)
    assert "the howl breaks over the square" in kept and "grateful" not in kept
    monkeypatch.setattr(world_tick, "_must_deliver", set())
    assert intents.must_deliver_sentences(held) == ""


def test_an_event_the_player_spoke_of_is_never_chronicled_unseen(world, monkeypatch):
    monkeypatch.setattr(world_tick, "_referred", set())
    with get_session() as session:
        howl = session.get(Event, world["howl"])
        howl.occurred_day, howl.occurred_hour, howl.delivery_attempts = 1, 20, 1
        session.commit()
    world_tick.mark_event_delivery("You cross the yard to Sorrel.", spoken_text="",
                                   player_input="The howl echoes in my ears as I cross to the stables.")
    assert world["howl"] in world_tick._referred
    world_tick._chronicle_offscreen(world["player"], [{"id": world["howl"], "name": "The Evening Howl", "day": 1,
                                                       "hour": 20, "description": "Howling rises."}])
    story = memory_files.read_chronicle(world["player"])
    assert "The Evening Howl happened (never staged in full, but the player knows of it" in story
    assert "did not see it" not in story
    world_tick._chronicle_offscreen(world["player"], [{"id": "other", "name": "The Cart", "day": 2, "hour": 9,
                                                       "description": ""}])
    assert "The Cart happened offscreen (the player did not see it)" in memory_files.read_chronicle(world["player"])


# -- a voice with no row --------------------------------------------------------

def test_a_role_voice_heard_twice_gets_a_new_npc_line(world):
    line = {"npc_id": None, "label": "Gate Watchman", "npc_name": "Gate Watchman", "_where": world["North Gatehouse"]}
    intents.note_speakers(world["player"], [line], world["North Gatehouse"])
    assert intents.new_npc_directive(world["player"]) is None          # once is a passer-by
    intents.note_speakers(world["player"], [line, dict(line)], world["North Gatehouse"])
    directive = intents.new_npc_directive(world["player"])
    assert directive and "@new-npc <their name> | trade: gate watchman" in directive
    assert "at: North Gatehouse" in directive
    intents.new_npc_directive(world["player"])
    assert intents.new_npc_directive(world["player"]) is None          # said VOICE_LINES times, then the audit's
    # a crowd is nobody
    crowd = {"npc_id": None, "label": "Townsfolk", "_where": world["Market Square"]}
    intents.note_speakers(world["player"], [crowd], None)
    intents.note_speakers(world["player"], [crowd], None)
    assert intents.new_npc_directive(world["player"]) is None


def test_a_role_voice_whose_trade_someone_has_points_at_them(world):
    # fleet7-bram T6-T10: a nameless "Stable Boy" while Sorrel stood elsewhere
    line = {"npc_id": None, "label": "Stable Boy", "_where": world["Stable Yard"]}
    intents.note_speakers(world["player"], [line], None)
    intents.note_speakers(world["player"], [line], None)
    directive = intents.new_npc_directive(world["player"])
    assert directive and "Sorrel Tack" in directive and "@npc-move Sorrel Tack" in directive


# -- the context's share ----------------------------------------------------------

def test_build_context_carries_the_take_line_and_the_holder(world, monkeypatch):
    from src.agents import dm_orchestrator
    from src.agents.dm_orchestrator import DMOrchestrator

    _start_teeth(world)
    monkeypatch.setattr(dm_orchestrator, "run_world_tick", lambda *a, **k: {})
    monkeypatch.setattr(dm_orchestrator, "format_world_tick_context", lambda t: "")
    monkeypatch.setattr(dm_orchestrator, "run_campaign_tick", lambda pid: {})
    monkeypatch.setattr(dm_orchestrator, "format_campaign_context", lambda t: "")
    monkeypatch.setattr(dm_orchestrator, "no_spine_context", lambda pid: "", raising=False)
    monkeypatch.setattr(dm_orchestrator, "last_reply_text", lambda agent: HELD_OUT_PASS)
    fake = SimpleNamespace(context=SimpleNamespace(player_id=world["player"]), _player_name="Wenna Cole")
    fake._player_block = lambda: ["### THE PLAYER"]
    fake._quest_block = lambda: ["### ACTIVE QUESTS", "- Teeth on the North Road"]
    context = DMOrchestrator._build_context(fake, "I take the pass carefully.")
    assert "### WHO CARRIES WHAT THE QUESTS ASK FOR" in context
    assert context.index(intents.TAKE_MARK) < context.index("Player says:")
    assert "@give Gate Pass | kind: key" in context
    # an engine turn spends the issued line and adds none
    engine = DMOrchestrator._build_context(fake, "[COMBAT OUTCOME] Victory.")
    assert intents.TAKE_MARK not in engine and intents.issued_take(world["player"]) is None


# -- haggling is asking to be paid, never paying -----------------------------------

def test_paying_half_now_is_no_haggle():
    from src.services.roll_intent import risky_act

    # fleet9-wenna T10: the player paying Piper — no roll, and no hold to drop the howl
    wenna = ('I take the chart and tuck it into my satchel. I pull out five coin and offer it to her. '
             '"This is half now for the tincture and your three weeks of watching."')
    assert risky_act(wenna) is None
    assert risky_act('"Half now for the work, half when I bring you something," I say, handing her the coin.') is None
    # ...nor said before the hand moves: the money is theirs to get, not the player's
    assert risky_act('"I want to pay you half now, half when I bring the proof."') is None
    assert risky_act('"I\'d like to pay up front for the tincture."') is None
    # asking to be paid sooner or more still is (fleet2-bram T1, fleet6-tamsin T6)
    assert risky_act('"But I want coin up front, half now for the work."')[0] == "haggling"
    assert risky_act('"I want the ten coin and the tincture now."')[0] == "haggling"
    assert risky_act('"Half up front, or find someone else."')[0] == "haggling"


# -- the hand-over audit: across the bar, and in what the filters dropped ---------

def test_a_key_slid_across_the_bar_with_no_give_is_flagged():
    from src.web.turn_audit import audit_turn

    state = {"inventory": [], "currency": 20, "location_id": "lantern"}
    warnings = audit_turn("She slides a brass key across the bar, her hand withdrawing before your fingers "
                          "close on it.", [], state, dict(state))
    slid = [w for w in warnings if w["check"] == "item_handover_without_command"]
    assert slid and "slides a brass key across" in slid[0]["detail"] and "@give" in slid[0]["detail"]
    for served in ("She slides a mug of ale across the bar.", "He sets a bowl of stew across the table."):
        assert not [w for w in audit_turn(served, [], state, dict(state))
                    if w["check"] == "item_handover_without_command"], served


def test_a_hand_over_the_filters_dropped_is_still_audited():
    from src.web.turn_audit import audit_turn

    state = {"inventory": [], "currency": 20}
    shown = "The Captain nods once and turns back to the road."
    assert not audit_turn(shown, [], state, dict(state))
    warnings = audit_turn(shown, [], state, dict(state),
                          unshown="The Captain presses the pass into your hand without ceremony.")
    assert any(w["check"] in ("item_without_tool", "item_handover_without_command") for w in warnings)


# -- through /api/play with a scripted DM ------------------------------------------

class _FakeAgent:
    def __init__(self, events):
        self.events = events
        self.messages = []

    async def stream_async(self, context):
        for event in self.events:
            yield event


class _FakeDM:
    def __init__(self, events, present, context_lines):
        self.agent = _FakeAgent(events)
        self.last_brief = {"present": present}
        self._player_name = "Wenna Cole"
        self._lines = context_lines

    def _build_context(self, player_input, **_kwargs):
        return "THE SCENE\n" + "\n".join(self._lines) + "\nPlayer says: " + player_input


def _play(w, monkeypatch, events, player_input, present, context_lines):
    import asyncio
    import json

    from src.web import server
    from src.web.streaming import ToolUsageTracker

    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    dm = _FakeDM(events, present, context_lines)
    monkeypatch.setattr(server, "get_or_create_session", lambda pid: {"dm": dm, "tool_tracker": ToolUsageTracker()})
    monkeypatch.setattr(server, "_engine_ambush", lambda *a, **k: None)
    monkeypatch.setattr(server, "maybe_advance_world", lambda: None)
    server._pending_audit_feedback.pop(w["player"], None)
    server._pending_engine_notes.pop(w["player"], None)
    server._pending_command_ledger.pop(w["player"], None)

    async def run():
        response = await server.play_sse(server.GameRequest(player_input=player_input, player_id=w["player"]))
        return [chunk async for chunk in response.body_iterator]

    frames = []
    for chunk in asyncio.run(run()):
        for line in str(chunk).splitlines():
            if line.startswith("data: ") and line[6:].strip() not in ("", "{}"):
                frames.append(json.loads(line[6:]))
    return frames, next(f for f in frames if f.get("type") == "complete")


def test_the_stream_gives_the_pass_the_reply_left_unwritten(world, monkeypatch):
    from src.web import server

    _start_teeth(world)
    present = _present(world, "Captain Brann Coldiron")
    said = "I take the pass with a quiet nod, tucking it into my coat."
    line = intents.take_directive(world["player"], said, HELD_OUT_PASS, present)
    frames, complete = _play(world, monkeypatch, [{"data": (
        "The Captain turns back to his road without another word, pass given, business done.\n")}],
        said, present, [line])
    assert any("@give Gate Pass | kind: key" in w for w in complete.get("engine_walks") or [])
    assert "Gate Pass" in _pack(world) and "Gate Pass" not in _held_by(world, "Captain Brann Coldiron")
    assert any(f.get("type") == "objective_done" for f in frames)
    assert not [w for w in complete["audit"] if w["check"] == "item_without_tool"]
    ledger = server._pending_command_ledger.get(world["player"]) or []
    assert sum("Gate Pass" in entry for entry in ledger) == 1


def test_a_refused_engine_give_is_never_booked_as_the_replys(world, monkeypatch):
    # The Hollow's authored whistle cannot be handed over in the square: the
    # engine's try is refused, and that refusal must not read next turn as a
    # line the DM wrote (a FAILED ledger line, [SETTLE THIS FIRST]) — an
    # audit line says what happened instead
    from src.services import settle
    from src.web import server

    present = _present(world, "Piper Quillow")
    held = "Piper Quillow: \"Look at this.\"\nShe holds a cracked bone whistle out, waiting.\n"
    said = "I take the whistle and turn it over."
    line = intents.take_directive(world["player"], said, held, present)
    assert line and "@give Cracked Bone Whistle" in line
    frames, complete = _play(world, monkeypatch, [{"data": "Piper watches you turn it over.\n"}],
                             said, present, [line])
    assert not [c for c in complete["commands"] if "Cracked Bone Whistle" in c["line"]]
    assert any(w["check"] == "taken_not_given" for w in complete["audit"])
    assert settle.recent_refusals(world["player"]) == []
    assert not any("FAILED" in entry for entry in server._pending_command_ledger.get(world["player"]) or [])
    assert "Cracked Bone Whistle" not in _pack(world)


def test_the_holder_saying_it_is_theirs_without_a_give_is_flagged(world):
    # fleet9-tamsin T3: the job started, the Captain said "That's your pass.
    # North Gatehouse, show it" — no @give, the step open to T18
    _start_teeth(world)
    captain = world["Captain Brann Coldiron"]
    said = [{"npc_id": captain, "text": "Smart. First light's better than dark anyway. That's your pass. "
                                        "North Gatehouse, show it."}]
    warning = intents.handed_aloud_warning(world["player"], said, [])
    assert warning and "@give Gate Pass" in warning["detail"] and "That's your pass" in warning["detail"]
    # said with the @give, an offer on a condition, or by someone else: nothing
    assert intents.handed_aloud_warning(world["player"], said, [SimpleNamespace(ok=True, verb="give")]) is None
    offer = [{"npc_id": captain, "text": "You'll need my pass. Say the word and it's yours."}]
    assert intents.handed_aloud_warning(world["player"], offer, []) is None
    other = [{"npc_id": world["Piper Quillow"], "text": "That's your pass, then? Lucky."}]
    assert intents.handed_aloud_warning(world["player"], other, []) is None


def test_the_stream_keeps_the_howl_the_roll_first_hold_would_drop(world, monkeypatch):
    from src.services.roll_intent import ROLL_FIRST_MARK

    monkeypatch.setattr(world_tick, "_must_deliver", {world["howl"]})
    frames, complete = _play(world, monkeypatch, [
        {"data": "Right as your hand closes around the coin, the howl breaks over the square — three long notes, "
                 "then silence. Piper takes your coin with a grateful little nod.\n"},
        {"current_tool_use": {"name": "roll_check", "toolUseId": "t1", "input": {}}},
        {"data": "Piper steadies, closing her fingers over the coin.\n"},
    ], "I haggle with Piper over the tincture.", {}, [f"{ROLL_FIRST_MARK}, before any text.]"])
    shown = "".join(f.get("data") or "" for f in frames if f.get("type") == "token")
    assert "the howl breaks over the square" in shown and "grateful" not in shown
    assert shown.index("the howl breaks") < shown.index("Piper steadies")


def test_the_holder_keeping_it_is_read_off_the_reply():
    # taken back, or held on to: not given
    for kept in ("He pulls it back before your fingers close.", "He tucks the pass back inside his coat.",
                 "His grip tightens on it.", "He holds it back a moment, eyes hard.",
                 "She keeps hold of the pass for now."):
        assert intents._kept_back(kept, {"pass"}), kept
        assert intents._kept_back(kept), kept
    # the opposite, or the player's own hand, or a gaze held — or still held
    # out to a player who said they took it (fleet9-wenna T7: the reply only
    # failed to say so; the engine gives it)
    for given in ("She lets you have it this time, not snatching it back.",
                  "He doesn't let go of your gaze as he says it.",
                  "You tuck it away inside your coat.", "The Captain turns back to his road, pass given.",
                  "He nods once, pass still in his hand, waiting for you to close your fingers on it.",
                  "She holds the chart out a little further, like letting go of it is the hard part."):
        assert not intents._kept_back(given, {"pass"}), given


def test_another_thing_kept_is_not_the_one_taken():
    # fleet9-wenna T8: the pass taken a second time, the reply said "pass
    # given, business done" and then had Piper hold out HER chart — the old
    # reader saw "holds the chart out" and left the pass in the Captain's hand
    t8 = ("The Captain turns back to his road without another word, pass given, business done. You cross "
          "the square toward the young woman.\n\nShe holds the chart out, hands trembling slightly.")
    assert not intents._kept_back(t8, {"pass"})
    # "it" is what its sentence named last: Piper's coin, not the chart
    assert not intents._kept_back("Piper takes the coin and tucks it away into her apron.", {"chart"})
    assert intents._kept_back("Piper looks at the chart, then tucks it away into her apron.", {"chart"})
    assert intents._kept_back("She takes the chart back.", {"chart"})
    assert not intents._kept_back("She takes the cup back.", {"chart"})


def test_the_engine_gives_the_pass_the_reply_left_held_out(world):
    # fleet9-wenna T7: "I take the pass carefully" got "pass still in his
    # hand, waiting for you to close your fingers on it" — and no @give
    _start_teeth(world)
    present = _present(world, "Captain Brann Coldiron")
    line = intents.take_directive(world["player"], "I take the pass carefully, but before I turn away, I ask.",
                                  HELD_OUT_PASS, present)
    took = intents.taken_fallback(world["player"], line, [], (
        "He nods once, pass still in his hand, waiting for you to close your fingers on it — and over your "
        "shoulder, the herbalist's apprentice holds a folded chart out toward nobody in particular."))
    assert took and took["line"] == "@give Gate Pass | kind: key"


def test_a_person_or_an_idiom_is_never_taken():
    # "I take Sorrel with me" named someone the last reply named too, and the
    # engine would have put a "Sorrel" in the pack; "take it slow" takes nothing
    reply = ("Sorrel leans on the fence.\nPiper Quillow: \"Here, look.\"\n"
             "She holds the chart out, waiting.\n")
    offered = intents.held_out(reply)
    prose, speech = intents._reply_parts(reply)
    said = prose + "\n" + "\n".join(w for _, w in speech)
    player = SimpleNamespace(inventory=[])
    present = {"p": "Piper Quillow", "s": "Sorrel Tack"}
    for words in ("I take Sorrel with me to the fence.", "I take it slow and ask about the marks first.",
                  "I take it you two know each other?", "I take him at his word.", "I take the first watch."):
        for take in intents.player_takes(words):
            assert intents._resolve_take(take, offered, said, [], player, None, present, {}) is None, words
    # ...and a take after a breath is still read ("and pocket the note" was
    # swallowed into the breath's object)
    assert [t["phrase"] for t in intents.player_takes("I take a deep breath and take the chart.")] == ["chart"]
    found = [intents._resolve_take(t, offered, said, [], player, None, present, {})
             for t in intents.player_takes("I take a deep breath and take the chart.")]
    assert found and found[0]["name"] == "Chart"

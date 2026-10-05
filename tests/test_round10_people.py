"""Round 10 (fleet10): people stay who and where they are; the voice.

- A speaker label borrowing a name in use — the cart driver the Captain
  called "Hesk" (fleet10-wenna T13-T14) — never moves the market's hawker,
  never fills his memory: the line goes out under the role the prose gave
  the speaker, and the DM is told to name them.
- The engine's walk-off never takes "He" two clauses back for someone else
  ("…and the runner jogs off toward the Stable Yard", fleet10-tamsin T10),
  nor repeats the reply's own @npc-move; a departure told after the fact
  ("where the Drover stood is empty", fleet10-wenna T17) moves them.
- A made-up suspect where the story already has a stranger is audited, and
  the brief offers the authored one (fleet10-wenna T7).
- The "Already used — vary" line reads compounds by their parts ("straw" of
  "straw-colored") and the narration's own habits.
- A closing narrator menu is dropped (fleet10-wenna T9/T10).
- Audit false positives: 'Scuse, a wound already carried, a walk into the
  place the last @move reached.
- talk_to never ticks on words said to someone else, or before setting out.
- The journal keeps testimony and what answered a ruled step.
- A held road crossed on a pass gets its note; a job's first thing pressed
  into the player's hands in the reply that started it is given.
"""

from types import SimpleNamespace

import pytest

from src.data.playtest_seed import create_playtest_world
from src.models import NPC, Location, Player, Quest, get_session, reset_engine
from src.models.campaign import Campaign
from src.services import dialogue, intents, memory_files, suspects, travel_intent, voice_tics
from src.tools.world_write.quests import activate_quest
from src.web import server
from src.web import turn_audit
from src.web.dialogue_markup import MetaSentenceFilter


@pytest.fixture()
def world(tmp_path):
    reset_engine()
    create_playtest_world(str(tmp_path / "emberfall.db"))
    intents.reset()
    voice_tics.reset()
    dialogue.forget_seen()
    with get_session() as session:
        place = {loc.name: loc.id for loc in session.query(Location).all()}
        people = {npc.name: npc.id for npc in session.query(NPC).all()}
        player = Player(name="Wenna Cole", current_location_id=place["Market Square"], currency=25)
        session.add(player)
        session.commit()
        teeth = session.query(Quest).filter_by(title="Teeth on the North Road").one().id
        howls = session.query(Quest).filter_by(title="Counting the Howls").one().id
        ids = {"player": player.id, "teeth": teeth, "howls": howls, **place, **people}
    yield ids
    intents.reset()
    voice_tics.reset()
    dialogue.forget_seen()
    reset_engine()


def _place(w, who):
    with get_session() as session:
        return session.get(NPC, w[who]).current_location_id


def _move_player(w, place):
    with get_session() as session:
        session.get(Player, w["player"]).current_location_id = w[place]
        session.commit()


def _add_npc(w, name, place, **fields):
    with get_session() as session:
        npc = NPC(name=name, current_location_id=w[place], home_location_id=w[place], **fields)
        session.add(npc)
        session.commit()
        w[name] = npc.id
    return w[name]


# -- 1. a name in use, borrowed -------------------------------------------------

DRIVER_BEFORE = ("Past the doorway, the Watch Captain stands over a hunched man wrapped in a horse blanket.\n\n"
                 "The driver — Hesk, the Captain called him — flinches at the sound of his own name.")


def test_a_label_borrowing_the_hawkers_name_is_the_driver_not_the_hawker(world):
    # fleet10-wenna T14: "Hesk:" at the gatehouse, Hesk Bramble at his stall
    _move_player(world, "North Gatehouse")
    note = server._speech_from_markup(
        {"speaker": "Hesk", "text": "They came out of the treeline together — a line. Like soldiers."},
        [world["Captain Brann Coldiron"]], world["player"], where=world["North Gatehouse"],
        left_from=world["North Gatehouse"], before=DRIVER_BEFORE)
    assert not note.get("npc_id")
    assert note["npc_name"] == "Driver" and note["label"] == "Driver"
    assert note["_borrowed"]["name"] == "Hesk Bramble"
    assert "counter" in note["_borrowed"]["why"]
    warning = turn_audit.name_in_use_borrowed_warning([note["_borrowed"]])
    assert warning["check"] == "name_in_use_borrowed"
    assert "@new-npc" in warning["detail"] and "North Gatehouse" in warning["detail"]
    assert "@npc-move Hesk Bramble | North Gatehouse" in warning["detail"]


def test_the_whole_name_or_prose_that_shows_him_still_means_him(world):
    _move_player(world, "North Gatehouse")
    whole = server._speech_from_markup({"speaker": "Hesk Bramble", "text": "Knives, twine, remedies."},
                                       [], world["player"], where=world["North Gatehouse"], before="")
    assert whole["npc_id"] == world["Hesk Bramble"] and "_borrowed" not in whole
    shown = server._speech_from_markup({"speaker": "Hesk", "text": "Knives, twine, remedies."},
                                       [], world["player"], where=world["North Gatehouse"],
                                       before="The hawker hurries up from the square, his tray still on his hip.")
    assert shown["npc_id"] == world["Hesk Bramble"]


def test_someone_seen_here_today_or_at_home_is_no_borrowing(world):
    with get_session() as session:
        hesk = session.get(NPC, world["Hesk Bramble"])
        assert dialogue.borrowed_speaker(session, world["player"], hesk, "Hesk", [], world["North Gatehouse"],
                                         before=DRIVER_BEFORE)
        # where the reply began: a goodbye or a walk along
        assert dialogue.borrowed_speaker(session, world["player"], hesk, "Hesk", [], world["North Gatehouse"],
                                         left_from=world["Market Square"], before=DRIVER_BEFORE) is None
    dialogue.note_seen(world["player"], world["North Gatehouse"], [world["Hesk Bramble"]])
    with get_session() as session:
        hesk = session.get(NPC, world["Hesk Bramble"])
        assert dialogue.borrowed_speaker(session, world["player"], hesk, "Hesk", [], world["North Gatehouse"],
                                         before=DRIVER_BEFORE) is None
        sorrel = session.get(NPC, world["Sorrel Tack"])
        sorrel.current_location_id = world["Market Square"]
        session.commit()
        # back at the yard he keeps
        assert dialogue.borrowed_speaker(session, world["player"], sorrel, "Sorrel", [], world["Stable Yard"],
                                         before=DRIVER_BEFORE) is None


def test_the_engine_never_walks_a_merchant_or_someone_seen_elsewhere_over(world):
    _move_player(world, "North Gatehouse")
    gate = world["North Gatehouse"]
    assert server._bring_speakers_here(world["player"], [{"npc_id": world["Hesk Bramble"], "_where": gate}]) == []
    assert _place(world, "Hesk Bramble") == world["Market Square"]
    # an ordinary person who walked up still comes (fleet6-tamsin T13)
    assert server._bring_speakers_here(world["player"], [{"npc_id": world["Piper Quillow"], "_where": gate}])
    assert _place(world, "Piper Quillow") == gate
    # ...but not one the player saw at the square earlier today
    with get_session() as session:
        session.get(NPC, world["Piper Quillow"]).current_location_id = world["Market Square"]
        session.commit()
    dialogue.note_seen(world["player"], world["Market Square"], [world["Piper Quillow"]])
    assert server._bring_speakers_here(world["player"], [{"npc_id": world["Piper Quillow"], "_where": gate}]) == []
    assert _place(world, "Piper Quillow") == world["Market Square"]


def test_a_known_name_with_nobody_new_in_the_prose_is_that_person(world):
    # review: Piper, seen in the square today, walks up at the yard with no
    # @npc-move — her line is hers (speaker_not_here asks for the move), never
    # "Stranger" with an order to @new-npc a second Piper
    dialogue.note_seen(world["player"], world["Market Square"], [world["Piper Quillow"]])
    _move_player(world, "Stable Yard")
    note = server._speech_from_markup({"speaker": "Piper", "text": "There you are!"}, [world["Sorrel Tack"]],
                                      world["player"], where=world["Stable Yard"], left_from=world["Stable Yard"],
                                      before="Piper hurries up, breathless, basket swinging.")
    assert note.get("npc_id") == world["Piper Quillow"] and "_borrowed" not in note


def test_the_borrowed_voice_keeps_its_role_through_the_scene(world):
    # fleet10-wenna T15: "Hesk's shoulders ease…" names no trade, and the
    # driver's next line is still the driver's
    _move_player(world, "North Gatehouse")
    gate = world["North Gatehouse"]
    first = server._speech_from_markup({"speaker": "Hesk", "text": "They came out of the treeline together."},
                                       [world["Captain Brann Coldiron"]], world["player"], where=gate,
                                       left_from=gate, before=DRIVER_BEFORE)
    assert first["npc_name"] == "Driver" and not first.get("npc_id")
    again = server._speech_from_markup({"speaker": "Hesk", "text": "Man-made, I'd swear it on my cart."},
                                       [world["Captain Brann Coldiron"]], world["player"], where=gate,
                                       left_from=gate, before="Hesk's shoulders ease a fraction.")
    assert again["npc_name"] == "Driver" and not again.get("npc_id") and again["_borrowed"]["role"] == "Driver"


def test_the_role_comes_from_the_prose():
    assert dialogue.role_from_prose(DRIVER_BEFORE) == "Driver"
    assert dialogue.role_from_prose("He stands over a hunched man wrapped in a horse blanket.") == "Hunched Man"
    assert dialogue.role_from_prose("The square is quiet.") == dialogue.STRANGER


# -- 2. the walk-off reads the leaving verb's own subject ----------------------------------

def _patterns(w, *names):
    with get_session() as session:
        return server._people_patterns([session.get(NPC, w[n]) for n in names])


PLACES = None


def _places(w):
    return {"stable yard": (w["Stable Yard"], "name"), "yard": (w["Stable Yard"], "word")}


def test_the_runner_who_jogs_off_is_not_the_captain(world):
    # fleet10-tamsin T10
    prose = ("He steps back, already turning to the next worry in his ledger, and the runner jogs off toward "
             "the Stable Yard.")
    assert server._left_ids(prose, _patterns(world, "Captain Brann Coldiron"), _places(world)) == []
    # ...while "He nods once… and ducks back off toward the Stable Yard" is him (fleet9-bram T12)
    went = server._left_ids("Sorrel waits. He nods once, decisive, and ducks back off toward the Stable Yard at "
                            "a trot.", _patterns(world, "Sorrel Tack"), _places(world))
    assert went == [(world["Sorrel Tack"], world["Stable Yard"])]


@pytest.mark.parametrize("prose", [
    # review probes: the "he" who leaves is the runner of his own sentence
    "Coldiron grunts. Behind him a runner turns, and he heads off toward the Stable Yard.",
    "Coldiron grunts. A runner skids up, gasps out the news, and then he dashes off toward the Stable Yard.",
])
def test_a_he_beside_someone_else_is_not_the_captain(world, prose):
    assert server._left_ids(prose, _patterns(world, "Captain Brann Coldiron"), _places(world)) == []
    # ...while a "he" with nobody else about is still him
    went = server._left_ids("Coldiron grunts. He heads off toward the Stable Yard without a word.",
                            _patterns(world, "Captain Brann Coldiron"), _places(world))
    assert went == [(world["Captain Brann Coldiron"], world["Stable Yard"])]


def test_no_walk_off_where_the_reply_already_wrote_the_npc_move(world):
    move = SimpleNamespace(ok=True, verb="npc-move", line="@npc-move Dell | Stable Yard",
                           result={"npc_name": "Dell", "to_location": "Stable Yard"})
    lines = server._walked_off(world["player"], "The Captain strides off toward the Stable Yard.",
                               results=[move])
    assert lines == [] and _place(world, "Captain Brann Coldiron") == world["Market Square"]
    assert server._walked_off(world["player"], "The Captain strides off toward the Stable Yard.")
    assert _place(world, "Captain Brann Coldiron") == world["Stable Yard"]


# -- 3. a departure told after the fact ------------------------------------------------------

YARD_T17 = ("You find Sorrel still at the paddock fence, Ember's ears still pinned flat, but the spot by the far "
            "post where the Drover stood is empty.")


def test_where_the_drover_stood_is_empty_and_he_left(world):
    _move_player(world, "Stable Yard")
    drover = _add_npc(world, "Corran Pike", "Stable Yard", profession="drover",
                      description_physical="lean and weathered, a drover's oilskin, one leather glove")
    people = _patterns(world, "Sorrel Tack", "Corran Pike")
    assert [g[0] for g in server._gone_ids(YARD_T17, people)] == [drover]
    looked = "Sorrel looks up from the far post where the Drover waited."
    said = [{"npc_id": world["Sorrel Tack"], "_at": len(looked),
             "text": "He left right after the runner came through — didn't say where."}]
    assert [g[0] for g in server._gone_ids(looked, people, said)] == [drover]
    # nowhere else to send him (he lives here): the DM is asked for the move
    audits: list = []
    server._walked_off(world["player"], YARD_T17, results=[], audits=audits)
    assert audits and audits[0]["check"] == "left_but_present" and "@npc-move Corran Pike" in audits[0]["detail"]
    assert _place(world, "Corran Pike") == world["Stable Yard"]


@pytest.mark.parametrize("prose, line", [
    # fleet2-wenna T13 (every speaker resolved): the stable hand's ash-cloaked
    # man "walked off", and the prose's last "he" was the Captain's back
    ("He glances toward the notice board, toward Coldiron's distant back, then drops his voice.",
     "Just looked at me a good long while, and then he walked off toward the north road."),
    ("The Captain frowns at the map.", "My brother? He left this morning, before the bell."),
    ("Coldiron listens.", "The man in grey — he's gone, sir. Gone before I could shout."),
    ("Coldiron listens.", "Aye, he left. Didn't say a word."),
])
def test_a_line_about_someone_else_sends_nobody_off(world, prose, line):
    people = _patterns(world, "Captain Brann Coldiron", "Sorrel Tack")
    said = [{"npc_id": world["Sorrel Tack"], "_at": len(prose), "text": line}]
    assert server._gone_ids(prose, people, said) == []


def test_a_reported_departure_goes_home_when_home_is_elsewhere(world):
    _move_player(world, "Stable Yard")
    with get_session() as session:
        session.get(NPC, world["Piper Quillow"]).current_location_id = world["Stable Yard"]
        session.commit()
    lines = server._walked_off(world["player"], "Piper's gone already, her basket with her.", audits=[])
    assert lines and _place(world, "Piper Quillow") == world["Market Square"]


@pytest.mark.parametrize("text", [
    "The howls started up right when he left, and the Drover stood there.",
    "The Drover has gone quiet, eyes on the hills.",
    "The Drover left the cart at the Hollow and walked back.",
    "If the Drover leaves, the yard is yours.",
    "The Drover left, but a minute later he's back at the post.",
])
def test_no_departure_in_these(world, text):
    _add_npc(world, "Corran Pike", "Stable Yard", profession="drover")
    assert server._gone_ids(text, _patterns(world, "Corran Pike")) == []


# -- 4. the invented suspect ----------------------------------------------------------------

def _campaign(w):
    with get_session() as session:
        session.add(Campaign(player_id=w["player"], title="Teeth", status="active",
                             villain_npc_id=w["Aldric Vane"], successor_npc_id=w["Vex Marrow"]))
        session.commit()


def test_the_brief_offers_the_stranger_the_scene_already_has(world):
    _campaign(world)
    with get_session() as session:
        player = session.get(Player, world["player"])
        sorrel = session.get(NPC, world["Sorrel Tack"])
        line = suspects.brief_line(session, player, [sorrel])
        assert line and "Ash-Cloaked Man" in line[0] and "never a new made-up suspect" in line[0]
        assert suspects.brief_line(session, player, [session.get(NPC, world["Hesk Bramble"])]) == []


def test_a_made_up_watcher_beside_that_clue_is_an_invented_suspect(world):
    # fleet10-wenna T7
    _campaign(world)
    _move_player(world, "Stable Yard")
    pike = _add_npc(world, "Corran Pike", "Stable Yard", profession="drover")
    line = ("@new-npc Corran Pike | trade: drover | looks: lean and weathered, a drover's oilskin, one leather "
            "glove | manner: patient, unreadable | voice: low, says little | at: Stable Yard")
    made = SimpleNamespace(ok=True, verb="new-npc", line=line, result={"id": pike, "name": "Corran Pike",
                                                                      "created": True})
    prose = "A lean, weathered man in a drover's oilskin stands near the far post. He's watching you."
    warning = turn_audit.invented_suspect_warning(world["player"], [made], prose,
                                                  [world["Sorrel Tack"], pike])
    assert warning and warning["check"] == "invented_suspect" and "Ash-Cloaked Man" in warning["detail"]
    plain = SimpleNamespace(ok=True, verb="new-npc", result={"id": pike, "name": "Ilse Marrick", "created": True},
                            line="@new-npc Ilse Marrick | trade: laundress | looks: red-knuckled | manner: sharp, quick")
    assert turn_audit.invented_suspect_warning(world["player"], [plain], "The laundress wrings a shirt out.",
                                               [world["Sorrel Tack"]]) is None


# -- 5. repeated details ---------------------------------------------------------------------

def test_a_compound_look_is_read_by_its_parts():
    npc = SimpleNamespace(id="s", name="Sorrel Tack", description_physical=(
        "A friendly young man all elbows and straw-colored hair, warm brown eyes, a halter rope coiled over one "
        "shoulder."), danger_tier=None, profession="Stable hand", species="human", faction=None, goals=[],
        secrets=[], description_personality="")
    looks = voice_tics.looks_words(npc)
    assert "straw" in looks and "colored" not in looks
    drover = SimpleNamespace(description_physical="lean and weathered, a drover's oilskin, one leather glove")
    assert "glove" in voice_tics.looks_words(drover) and "leather" not in voice_tics._forms("boiled-leather")[1:]
    person = voice_tics.Person("s", "Sorrel Tack", None, "he", looks)
    import re
    person.pattern = re.compile(r"\bSorrel\b")
    voice_tics.reset("p")
    voice_tics.note("p", "Sorrel grins, straw hair flopping.", None, [person])
    voice_tics.note("p", "Sorrel shakes his head, straw hair catching lamplight.", None, [person])
    assert any("straw" in t for t in voice_tics.tics("p", "s"))


def test_the_narrations_own_habits_join_the_line():
    voice_tics.reset("p")
    for text in ("His eyes drift north, toward the hills. He watches you the way a soldier watches an officer.",
                 "The square is loud. She watches you the way a soldier watches an officer, eyes still fixed north."):
        voice_tics.note("p", text, None, [])
    habits = voice_tics.scene_tics("p")
    assert any("soldier" in h for h in habits) and any("north" in h for h in habits)
    line = voice_tics.brief_lines("p", [])
    assert line and "the narration itself" in line[0]
    # the plot's own things never count
    keys = voice_tics.scene_phrases("like a whistle split down the middle", skip={"whistle"})
    assert keys and not any("whistle" in key for key in keys)


# -- 6. a closing narrator menu ----------------------------------------------------------------

def _filtered(text, chunk=7):
    f = MetaSentenceFilter()
    out = "".join(f.feed(text[i:i + chunk]) for i in range(0, len(text), chunk)) + f.flush()
    return out, f.dropped


@pytest.mark.parametrize("menu", [
    # fleet10-wenna T9, T10
    "The yard is yours to use as you please — wait here with the drover's silence for company, go back to "
    "find Piper before dark, or use the time for something else entirely.",
    "Eight bells is still a way off — you can wait it out here with the Drover's silence for company, or go "
    "find out more about the cart while there's still light to ask by.",
    "You could try to hold out here till first light despite the hurt, or force yourself up now.",
])
def test_a_closing_menu_is_dropped(menu):
    out, dropped = _filtered("The light thins over the yard.\n\n" + menu + "\n")
    assert menu not in out and dropped
    assert "The light thins over the yard." in out


@pytest.mark.parametrize("fiction", [
    "You can see the gate from here, or what's left of it.\n",
    "You can wait here, or go after him. The horses stamp and blow behind you.\n",
    'He shrugs. "You can stay, or go."\n',
    # a stake or a feeling a paragraph ends on (review)
    "The ice is thin past the ford. One wrong step and you could lose a foot, or worse.\n",
    "The wind has teeth tonight. Stay out long enough and you might freeze, or starve, or both.\n",
    "The howl dies away. You could swear it came from the ridge, or just beyond it.\n",
])
def test_fiction_that_looks_like_a_menu_stays(fiction):
    out, dropped = _filtered(fiction)
    assert out.strip() == fiction.strip() and not dropped


# -- 7. audit false positives ----------------------------------------------------------------------

def test_a_clipped_word_is_no_name():
    # fleet10-bram T4
    assert turn_audit.unknown_named_people(["'Scuse me, Sergeant—", "'Tis cold, Captain."], set(), set()) == []


def test_a_wound_already_carried_is_no_new_damage():
    # fleet10-bram T18: hurt since the T16 fight
    warn = [{"check": "damage_without_tool", "detail": "x"}]
    turn_audit.note_reply_moves("pb", None, True)
    assert turn_audit.drop_told_wound(warn, "pb", "You're bleeding, you're spent.", "hurt") == []
    assert turn_audit.drop_told_wound(warn, "pb", "You're bleeding now.", "healthy") == warn
    assert turn_audit.drop_told_wound(warn, "pb", "You're badly hurt and bleeding.", "hurt") == warn
    turn_audit.note_reply_moves("pc", None, False)
    turn_audit.note_reply_moves("pc", None, False)
    assert turn_audit.drop_told_wound(warn, "pc", "You're bleeding, you're spent.", "hurt") == warn


def test_walking_into_where_the_last_move_landed_is_no_arrival():
    # fleet10-wenna T13 after T12's @move North Gatehouse
    text = "The Watchman barely glances at you and waves you past without really weighing your words."
    assert turn_audit._arrival_claim(text, "North Gatehouse")
    assert turn_audit._arrival_claim(text, "North Gatehouse", arrived_here=True) is None
    assert turn_audit._arrival_claim("Then you step into the side room.", "North Gatehouse") is None
    warn = [{"check": "arrival_without_move", "detail": "Narration describes arriving somewhere but …"}]
    before = {"location_id": "gate", "location": "North Gatehouse"}
    turn_audit.note_reply_moves("pw", "gate", False)
    assert turn_audit.drop_inner_arrival(warn, "pw", text, before) == []
    turn_audit.note_reply_moves("pw", None, False)
    assert turn_audit.drop_inner_arrival(warn, "pw", text, before) == warn


# -- 8. talk_to --------------------------------------------------------------------------------------

T6_INPUT = ('I nod, already moving. "Before dark, I will. And Piper — keep counting those gaps."\n\n'
            "I head toward the Stable Yard with the pass warm against my ribs.")


def test_words_to_someone_else_before_setting_out_are_no_talk(world):
    with get_session() as session:
        sorrel, piper = session.get(NPC, world["Sorrel Tack"]), session.get(NPC, world["Piper Quillow"])
        assert not dialogue._talked_to(sorrel, T6_INPUT, {sorrel.id}, [], None, here=True, elsewhere=[piper],
                                       moved=True)
        asked = 'I head to the stable yard. "What does the yard hear after dark?"'
        assert dialogue._talked_to(sorrel, asked, {sorrel.id}, [], None, here=True, elsewhere=[piper], moved=True)


def test_the_talk_to_step_waits_for_the_asking(world):
    # fleet10-tamsin T6, end to end
    activate_quest(quest_id=world["howls"])
    _move_player(world, "Stable Yard")
    said = [{"npc_id": world["Sorrel Tack"], "npc_name": "Sorrel", "label": "Sorrel",
             "text": "Help you with something?", "introduces": True}]
    ticked = dialogue.record_turn_dialogue(world["player"], T6_INPUT, said, left_from=world["Market Square"])
    assert ticked == []


# -- 9. the journal keeps what people said ------------------------------------------------------------

HESK_LINE = ("They came out of the treeline together — not a pack scattered hunting, a line. Then something "
             "whistled, high and cracked-sounding, and they just stopped.")


def test_testimony_and_a_ruled_step_are_journaled(world):
    activate_quest(quest_id=world["teeth"])
    activate_quest(quest_id=world["howls"])
    said = [{"npc_id": None, "npc_name": "Driver", "text": HESK_LINE},
            {"npc_id": world["Captain Brann Coldiron"], "npc_name": "Watch Captain",
             "text": "Gate bars at the howl — get back before then."}]
    step = SimpleNamespace(ok=True, verb="step-done", result={"records": [
        {"quest_id": world["howls"], "text": "Stand out the next howl and mark where it rises", "kind": "custom"}]})
    prose = "You finish the ridge line on the page, marking an X well past where the Hollow should sit."
    written = memory_files.journal_testimony(world["player"], said, [step], prose)
    assert any(e.startswith('Driver: "They came out') for e in written)
    assert not any("Gate bars" in e for e in written)
    assert any(e.startswith("Stand out the next howl") and "marking an X" in e for e in written)
    # never twice
    assert memory_files.journal_testimony(world["player"], said, [], "") == []


# -- 10. the gate on the way; the job's first thing ------------------------------------------------------

def _give_pass(w):
    with get_session() as session:
        player = session.get(Player, w["player"])
        player.inventory = list(player.inventory or []) + [{"name": "Gate Pass", "type": "key", "quantity": 1}]
        session.commit()


def test_a_held_road_on_the_way_gets_its_note(world):
    _move_player(world, "Stable Yard")
    with get_session() as session:
        player = session.get(Player, world["player"])
        assert travel_intent.gated_hops(session, player, world["Stable Yard"], world["Wolf's Hollow"]) == []
    _give_pass(world)
    with get_session() as session:
        player = session.get(Player, world["player"])
        hops = travel_intent.gated_hops(session, player, world["Stable Yard"], world["Wolf's Hollow"])
    assert hops == [{"from": "North Gatehouse", "to": "Wolf's Hollow", "item": "Gate Pass"}]
    line = travel_intent.directive(world["player"], "I head for Wolf's Hollow.")
    assert line and line.startswith("[THIS REPLY: the player goes to Wolf's Hollow") and "Gate Pass" in line
    assert travel_intent.issued(line)["place"] == "Wolf's Hollow"
    moved = SimpleNamespace(ok=True, tool="move_player", verb="move",
                            result={"success": True, "destination": "Wolf's Hollow"})
    note = server._gated_hop_note(world["player"], [moved], world["Stable Yard"],
                                  "You're past the paddock fence and into the lane; the quarry road climbs ahead.")
    assert note and "North Gatehouse" in note and "Gate Pass" in note
    assert server._gated_hop_note(world["player"], [moved], world["Stable Yard"],
                                  "At the gatehouse the watchman squints at your pass and waves you on.") is None


def test_the_pass_pressed_into_her_palm_with_the_job_is_given(world):
    # fleet10-tamsin T2
    activate_quest(quest_id=world["teeth"])
    started = SimpleNamespace(ok=True, tool="activate_quest", verb="quest-start", result={"id": world["teeth"]})
    prose = ("He presses a folded square of stamped leather into your palm — the gate pass — and jerks his chin "
             "north.")
    took = intents.started_take(world["player"], [started], prose)
    assert took and took["line"] == "@give Gate Pass | kind: key"
    gave = SimpleNamespace(ok=True, tool="add_to_inventory", verb="give", result={})
    assert intents.started_take(world["player"], [started, gave], prose) is None
    assert intents.started_take(world["player"], [started], "He nods toward the gate.") is None
    _move_player(world, "Stable Yard")                  # the Captain is not here
    assert intents.started_take(world["player"], [started], prose) is None


@pytest.mark.parametrize("prose", [
    "He'll hand you the gate pass once you've proven yourself.",
    "He says he'll press the gate pass into your hand at the gatehouse, not before.",
    "If you bring the whistle back, he hands you the gate pass and twenty coin.",
    "He doesn't hand you the gate pass. Not yet.",
])
def test_a_pass_promised_or_held_back_is_not_given(world, prose):
    # review probes: each gave the pass with the job
    activate_quest(quest_id=world["teeth"])
    started = SimpleNamespace(ok=True, tool="activate_quest", verb="quest-start", result={"id": world["teeth"]})
    assert intents.started_take(world["player"], [started], prose) is None

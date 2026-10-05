"""Names in every player-facing text (fleet5 playtest critics, round 4).

  * The HUD leaked what the speech hid (all three runs, flagged major): the
    lead read "Get Captain Coldiron's gate pass for the north road" and "Ask
    Sorrel Tack what the stable yard hears after dark", the quest panel and
    the chips ("Talk to Brann") the same, while every speaker line said
    "Watch Captain" — each player asked "Coldiron?" at T1.
  * The word-name exemption let "Piper's whole face lights up" through
    before anyone said Piper (fleet5-tamsin T3).
  * A met person was named by parts never said (fleet5-wenna T19 "Sorrel
    Tack" when only "Sorrel" had been).
  * Seed voice examples were copied onto every player ("carter", "mister").
  * A talk_to step ticked on an NPC's greeting before the player had asked
    anything (fleet5-wenna T6).
"""

import asyncio
import json

import pytest

from src.models import (
    Campaign, Faction, Location, LocationType, NPC, NPCRelationship, Player, Quest, QuestStatus, WorldClock,
    get_session, reset_engine,
)
from src.services import dialogue, memory_files
from src.services import lead as lead_mod
from src.services import objectives as obj
from src.services.dialogue import NameGate


@pytest.fixture()
def mem(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    monkeypatch.setattr(memory_files, "memory_root", lambda: root)
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    return root


@pytest.fixture()
def world(db, mem):
    with get_session() as session:
        session.add(WorldClock(day=1, hour=20, minute=0))
        fangs = Faction(name="The Ashen Fangs")
        session.add(fangs)
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True)
        square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True,
                          visited=True)
        session.add_all([hollow, square])
        session.flush()
        brann = NPC(name="Captain Brann Coldiron", profession="Captain of the Lantern Watch",
                    current_location_id=square.id,
                    description_physical="An old, weathered veteran with a gray beard cropped short.")
        sorrel = NPC(name="Sorrel Tack", profession="Stable hand", current_location_id=square.id,
                     description_physical="A friendly young man all elbows and straw-colored hair.")
        piper = NPC(name="Piper Quillow", profession="Herbalist's apprentice", current_location_id=square.id,
                    description_physical="A small, quick girl with ink on her fingers.")
        aldric = NPC(name="Aldric Vane", profession="Ashen Fangs wolf-caller", faction_id=fangs.id,
                     danger_tier="boss", current_location_id=hollow.id,
                     description_physical="A broad, still man in a wolf-pelt mantle.")
        session.add_all([brann, sorrel, piper, aldric])
        player = Player(name="Wenna Cole", current_location_id=square.id, currency=25)
        session.add(player)
        session.flush()
        quest = Quest(title="Teeth on the North Road", status=QuestStatus.ACTIVE, assigned_by_npc_id=brann.id,
                      description="Captain Coldiron wants eyes he can spare.",
                      objectives=obj.normalize_objectives([
                          {"text": "Get Captain Coldiron's gate pass for the north road", "kind": "obtain",
                           "item_name": "Gate Pass"},
                          {"text": "Ask Sorrel Tack what the stable yard hears after dark", "kind": "talk_to",
                           "target_id": sorrel.id},
                          {"text": "Bring proof back to Captain Brann Coldiron", "kind": "talk_to",
                           "target_id": brann.id},
                      ], session=session))
        session.add(quest)
        session.flush()
        player.active_quests = [quest.id]
        session.commit()
        return {"player": player.id, "square": square.id, "hollow": hollow.id, "brann": brann.id,
                "sorrel": sorrel.id, "piper": piper.id, "aldric": aldric.id, "quest": quest.id}


def _meet(world, *keys):
    with get_session() as session:
        for key in keys:
            session.add(NPCRelationship(npc_id=world[key], player_id=world["player"]))
        session.commit()


def _state(world, heard_now=""):
    from src.web import server

    return server.build_player_state(world["player"], heard_now=heard_now)


def _hud(state):
    """Everything the HUD shows that this rule covers, as one string."""
    return json.dumps([state.get(k) for k in ("lead", "quests", "suggestions", "campaign", "party")])


# ----------------------------------------------------------------------
# Item 1: the lead, the quest panel, the chips and the toasts
# ----------------------------------------------------------------------

def test_fleet5_an_unmet_captains_lead_reads_with_his_role(world):
    state = _state(world)
    assert state["lead"]["text"] == "Get the Watch Captain's gate pass for the north road"
    steps = state["quests"][0]["objectives"]
    assert [s["text"] for s in steps] == [
        "Get the Watch Captain's gate pass for the north road",
        "Ask the Stable Hand what the stable yard hears after dark",
        "Bring proof back to the Watch Captain",
    ]
    assert [s["target_name"] for s in steps[1:]] == ["Stable Hand", "Watch Captain"]
    hud = _hud(state)
    assert "Coldiron" not in hud and "Brann" not in hud and "Sorrel" not in hud
    assert "Approach the watch captain" in [c["label"] for c in state["suggestions"]]


def test_once_coldiron_is_heard_the_lead_says_it_and_nothing_more(world):
    dialogue.remember_heard(world["player"], ["Coldiron"])
    state = _state(world)
    assert state["lead"]["text"] == "Get Captain Coldiron's gate pass for the north road"
    assert state["quests"][0]["objectives"][2]["text"] == "Bring proof back to Captain Coldiron"
    assert state["quests"][0]["objectives"][2]["target_name"] == "Captain Coldiron"
    assert "Brann" not in _hud(state)


def test_a_name_said_this_turn_shows_on_the_post_turn_state(world):
    # The post-turn state is built before the memory files keep the turn
    state = _state(world, heard_now='Watch Captain: "Coldiron. Remember it."')
    assert state["lead"]["text"] == "Get Captain Coldiron's gate pass for the north road"


def test_a_lead_target_is_named_as_far_as_heard(world):
    with get_session() as session:
        quest = session.get(Quest, world["quest"])
        steps = obj.normalize_objectives(quest.objectives)
        steps[0]["done"] = True
        quest.objectives = steps
        session.commit()
    state = _state(world)
    assert state["lead"]["text"] == "Ask the Stable Hand what the stable yard hears after dark"
    assert state["lead"]["target"]["name"] == "Stable Hand"
    assert state["lead"]["target"]["id"] == world["sorrel"]
    chip = state["suggestions"][0]
    assert chip["npc_id"] == world["sorrel"] and "Sorrel" not in json.dumps(chip)
    dialogue.remember_heard(world["player"], ["Sorrel"])
    state = _state(world)
    assert state["lead"]["text"] == "Ask Sorrel what the stable yard hears after dark"
    assert state["lead"]["target"]["name"] == "Sorrel"


def test_fleet5_wenna_talk_chips_use_the_heard_name(world):
    # Met, but only "Coldiron" was ever said: "Talk to Brann" gave away the rest
    _meet(world, "brann")
    dialogue.remember_heard(world["player"], ["Coldiron"])
    chips = _state(world)["suggestions"]
    talk = next(c for c in chips if c.get("npc_id") == world["brann"])
    assert talk["label"] == "Talk to Coldiron"
    assert talk["text"] == "I approach Captain Coldiron and start talking."


def test_the_dm_context_keeps_the_real_names(world):
    with get_session() as session:
        block = lead_mod.format_lead_context(lead_mod.compute_lead(session, session.get(Player, world["player"])))
    assert '"Get Captain Coldiron\'s gate pass for the north road"' in block
    assert "never assume they read a name here" in block


def test_toasts_quest_list_and_story_panel_are_gated(world, tmp_path, monkeypatch):
    from src.web import server

    # The handler re-inits the active database: keep it on this test's
    monkeypatch.setattr(server, "get_active_db_path", lambda: str(tmp_path / "test.db"))

    toast = {"type": "objective_done", "quest_id": world["quest"], "objective_id": "o2",
             "quest_title": "Teeth on the North Road",
             "text": "Ask Sorrel Tack what the stable yard hears after dark", "kind": "talk_to"}
    shown, = dialogue.player_facing(world["player"], [toast])
    assert shown["text"] == "Ask the Stable Hand what the stable yard hears after dark"
    assert shown["quest_id"] == world["quest"] and shown["objective_id"] == "o2"
    assert "Sorrel" not in json.dumps(asyncio.run(server.get_quests(world["player"])))

    with get_session() as session:
        session.add(Campaign(
            player_id=world["player"], title="The Pattern in the Howling", status="active", current_act=1,
            acts=[{"act": 1, "title": "The Pattern",
                   "premise": "Wenna must help Coldiron's thin Watch stand against Vane."}],
            villain_npc_id=world["aldric"], villain_name="Aldric Vane",
        ))
        session.commit()
    view = asyncio.run(server.get_campaign_view(world["player"]))
    assert view["campaign"]["act_premise"] == (
        "Wenna must help the Watch Captain's thin Watch stand against the Wolf-Mantled Man.")
    assert "Aldric" not in json.dumps(view) and "Vane" not in json.dumps(view)
    assert "Vane" not in json.dumps(_state(world)["campaign"])
    # The author's debug flag still reads the truth
    assert "Aldric Vane" in json.dumps(asyncio.run(server.get_campaign_view(world["player"], spoilers=1)))


def test_the_session_roster_names_strangers_by_role_and_hides_their_trade(world):
    _meet(world, "piper")
    rows = [{"id": world["sorrel"], "name": "Sorrel Tack", "profession": "Stable hand"},
            {"id": world["piper"], "name": "Piper Quillow", "profession": "Herbalist's apprentice"}]
    assert dialogue.player_facing_people(world["player"], rows) == [
        {"id": world["sorrel"], "name": "Stable Hand", "profession": None},
        {"id": world["piper"], "name": "Piper Quillow", "profession": "Herbalist's apprentice"},
    ]


def test_without_a_readable_world_the_text_is_left_as_written(world, monkeypatch):
    monkeypatch.setattr(NameGate, "for_player", classmethod(lambda cls, pid, heard_now="": None))
    assert dialogue.player_facing(world["player"], ["Ask Sorrel Tack"]) == ["Ask Sorrel Tack"]
    assert dialogue.player_facing_state({"lead": {"text": "Ask Sorrel Tack"}}, None)["lead"]["text"] == (
        "Ask Sorrel Tack")


# ----------------------------------------------------------------------
# Item 2: a word-name opening a sentence
# ----------------------------------------------------------------------

def _gate(world, present=("brann", "sorrel", "piper"), context=""):
    return NameGate.for_turn(world["player"], "", present_ids=[world[k] for k in present], context=context)


def _move_player(world, where):
    with get_session() as session:
        session.get(Player, world["player"]).current_location_id = world[where]
        session.commit()


def test_fleet5_tamsin_t3_a_possessive_word_name_is_the_person(world):
    gate = _gate(world)
    assert gate.mask("Piper's whole face lights up.") == "The Herbalist's Apprentice's whole face lights up."
    # Not near, only named in the DM's context: owning a face still says person
    _move_player(world, "hollow")
    far = _gate(world, present=(), context="Piper Quillow keeps charts.")
    assert far.mask("Piper's whole face lights up.") == "The Herbalist's Apprentice's whole face lights up."
    # The villain standing there and smiling is the villain
    assert far.mask("Piper nods. Vane smiles.") == "The Herbalist's Apprentice nods. The Wolf-Mantled Man smiles."


def test_a_word_name_opening_a_sentence_is_the_person_while_they_are_near(world):
    assert _gate(world).mask("Piper turns to the map.") == "The Herbalist's Apprentice turns to the map."
    _move_player(world, "hollow")
    # Only in the context, doing nothing a person does: the word
    far = _gate(world, present=(), context="Piper Quillow keeps charts.")
    assert far.mask("Piper turns to the map.") == "Piper turns to the map."


def test_ordinary_words_stay_words(world):
    with get_session() as session:
        session.add(NPC(name="Will Thorne", profession="Ferryman", current_location_id=world["square"]))
        session.commit()
    gate = _gate(world)
    # A mass noun opens a sentence bare, Sorrel standing there or not
    assert gate.mask("Tack hangs by the door. Mend the tack.") == "Tack hangs by the door. Mend the tack."
    assert gate.mask("A piper played somewhere.") == "A piper played somewhere."
    assert gate.mask("Will you come? Will Thorne waits.") == "Will you come? The Ferryman waits."


def test_streaming_holds_a_word_name_until_the_next_word_decides(world):
    gate = _gate(world)
    out = gate.feed("Piper") + gate.feed("'s whole face") + gate.feed(" lights up.\n") + gate.flush()
    assert out == "The Herbalist's Apprentice's whole face lights up.\n"
    gate = _gate(world)
    out = gate.feed("Tack") + gate.feed(" hangs by the door.\n") + gate.flush()
    assert out == "Tack hangs by the door.\n"


# ----------------------------------------------------------------------
# Item 3: a met person is shown by the part of the name that was said
# ----------------------------------------------------------------------

def test_fleet5_wenna_t19_a_met_person_shows_only_what_was_heard(world):
    _meet(world, "sorrel", "brann")
    dialogue.remember_heard(world["player"], ["Sorrel"])
    gate = _gate(world)
    assert gate.mask("Sorrel Tack shrugs. You nod at Tack.") == "Sorrel shrugs. You nod at Sorrel."
    assert gate.masked[0] == ("Sorrel Tack", "Sorrel")
    # Met before the name rule and nothing heard: the name they were given
    assert gate.mask("Brann Coldiron frowns.") == "Brann Coldiron frowns."
    # All of it heard: as written
    gate.hear('"Tack, get the horses."')
    assert gate.mask("Sorrel Tack runs.") == "Sorrel Tack runs."


# ----------------------------------------------------------------------
# Item 4: seed voices without words to copy
# ----------------------------------------------------------------------

def test_seed_voice_patterns_carry_no_address_words_to_copy(tmp_path):
    from src.data.playtest_seed import create_playtest_world

    reset_engine()
    try:
        create_playtest_world(str(tmp_path / "emberfall.db"))
        with get_session() as session:
            voices = {n.name: n.voice_pattern or "" for n in session.query(NPC).all()}
    finally:
        reset_engine()
    # fleet5-wenna T0/T2/T10 "carter" (the dead man's trade); fleet5-tamsin T5
    # "mister" to a woman
    for name, voice in voices.items():
        low = voice.lower()
        for bait in ("'carter'", "'smith'", "'healer'", "mister", "missus"):
            assert bait not in low, (name, voice)
    assert "trade" in voices["Captain Brann Coldiron"].lower()


# ----------------------------------------------------------------------
# Item 5: a talk_to step is earned by talking TO them
# ----------------------------------------------------------------------

# fleet5-wenna T6, verbatim: reading the notice board, talking to Piper
FLEET5_WENNA_T6 = (
    "I squeeze Piper's hand back, then turn toward the notice board to orient myself. \"I'll find Sorrel at "
    "the stables first, get his ear for the night sounds, then straight to Coldiron for the pass,\" I say "
    "quietly, more to myself than to her. I scan the board quickly—looking for anything about the stables, "
    "the north road, or Wolf's Hollow—before heading off to find this elbows-and-straw-haired boy who knows "
    "what the dark sounds like."
)


def _ask_sorrel_done(world):
    with get_session() as session:
        return obj.normalize_objectives(session.get(Quest, world["quest"]).objectives)[1]["done"]


def _sorrel_says(world, player_input, text="You're new, aren't you?", addressed=None, also=()):
    speeches = [{"npc_id": world["sorrel"], "label": "Stable Hand", "introduces": False, "text": text}]
    speeches += [{"npc_id": world[k], "label": "x", "introduces": False, "text": "Hm."} for k in also]
    return dialogue.record_turn_dialogue(world["player"], player_input, speeches, addressed)


def test_fleet5_wenna_t6_a_greeting_ticks_nothing(world):
    assert _sorrel_says(world, FLEET5_WENNA_T6) == []
    assert not _ask_sorrel_done(world)
    # T7: she asks him
    lines = _sorrel_says(world, '"Sorrel? What does the stable yard hear after dark?"', "Howls. Wrong ones.")
    assert _ask_sorrel_done(world) and any("Objective met" in line for line in lines)


@pytest.mark.parametrize("player_input", [
    'I approach the stable hand. "What does the yard hear after dark?"',
    '"What did you hear that night?" I ask.',                           # only he answered
    'I lean on the rail beside him. "Evening, Sorrel."',
    "I meet the stable hand's eyes and wait.",
])
def test_talking_to_him_ticks(world, player_input):
    _sorrel_says(world, player_input)
    assert _ask_sorrel_done(world), player_input


def test_the_talk_chip_ticks_whatever_was_typed(world):
    _sorrel_says(world, "I wave.", addressed=world["sorrel"])
    assert _ask_sorrel_done(world)


@pytest.mark.parametrize("player_input, also", [
    # fleet5-tamsin T8: walking back, no word said
    ("I head back toward the square's light, already turning over what I'll tell Sorrel first.", ()),
    ('"Piper, what did the stable boy see?"', ("piper",)),                # someone else addressed
    ('"What did you all hear?"', ("piper",)),                              # two answered, nobody named
    ("[COMBAT OUTCOME] The wolves are dead.", ()),                        # an engine directive
])
def test_speaking_up_unasked_ticks_nothing(world, player_input, also):
    _sorrel_says(world, player_input, also=also)
    assert not _ask_sorrel_done(world), player_input


# ----------------------------------------------------------------------
# Review (round 4): the other places a name reaches the screen — the canvas
# (a click fills the sidebar and the input, and a talk chip prefers the
# canvas name to its own words), the counter, the ambush toast, the cards
# ----------------------------------------------------------------------

@pytest.fixture()
def api(world, tmp_path, monkeypatch):
    from src.services.asset_manager import AssetManager
    from src.web import server

    monkeypatch.setattr(server, "get_active_db_path", lambda: str(tmp_path / "test.db"))
    monkeypatch.setattr(AssetManager, "spawn_generation", lambda self, key, factory: False)
    return world


def test_the_canvas_names_people_as_far_as_heard(api):
    from src.web import server

    def shown():
        scene = asyncio.run(server.get_location_assets(api["square"], api["player"]))
        return {n["id"]: n["name"] for n in scene["npcs"]}

    names = shown()
    assert names[api["sorrel"]] == "Stable Hand" and names[api["brann"]] == "Watch Captain"
    assert "Sorrel" not in json.dumps(names) and "Coldiron" not in json.dumps(names)
    dialogue.remember_heard(api["player"], ["Coldiron"])
    assert shown()[api["brann"]] == "Captain Coldiron"


def test_a_quote_voiced_for_the_clicked_stranger_is_labelled_by_role(api, monkeypatch):
    from src.web import server
    from src.web.turn_filters import DialogueDeduper

    monkeypatch.setattr(server, "_enrich_speech_event", lambda note: None)
    # The canvas now sends the name it shows; the id is who it is
    request = server.GameRequest(player_id=api["player"], player_input="Evening.",
                                 npc_id=api["sorrel"], npc_name="Stable Hand")
    gate = NameGate.for_turn(api["player"], request.player_input, present_ids=[api["sorrel"]])
    notes = server._synthesized_speech(request, 'He grins. "The horses go quiet first, every time."',
                                       DialogueDeduper(), names=gate)
    assert [(n["npc_id"], n["npc_name"]) for n in notes] == [(api["sorrel"], "Stable Hand")]


def test_the_counter_is_headed_by_what_the_player_calls_them(api):
    from src.web import server

    with get_session() as session:
        view = server._shop_view(session, session.get(Player, api["player"]), session.get(NPC, api["piper"]))
    assert view["npc_name"] == "Herbalist's Apprentice"


def test_the_ambush_toast_names_the_roster_as_shown(api, monkeypatch):
    from src.web import pending_actions, server

    monkeypatch.setattr(server.encounters, "roll", lambda pid, arrived: {
        "npc_ids": [api["sorrel"]], "names": ["Sorrel Tack"], "where": "Market Square"})
    monkeypatch.setattr(server, "build_combat_start", lambda ids, pid: {
        "enemies": [{"npc_id": api["sorrel"], "name": "Sorrel Tack", "archetype": "brute"}],
        "allies": [{"npc_id": api["piper"], "name": "Piper Quillow", "archetype": "skirmisher"}],
        "boss": None})
    # A companion met, but only ever called "Piper"
    _meet(api, "piper")
    dialogue.remember_heard(api["player"], ["Piper"])
    try:
        ambush = server._engine_ambush(api["player"], arrived=True)
        assert ambush["line"] == "Stable Hand come out of nowhere."
        assert ambush["combat_start"]["enemies"][0]["name"] == "Stable Hand"
        assert ambush["combat_start"]["allies"][0]["name"] == "Piper"
        # The DM's ledger keeps who it really was
        assert "Sorrel Tack" in json.dumps(pending_actions.drain(api["player"]))
    finally:
        server._pending_combat.pop(api["player"], None)
        pending_actions.clear(api["player"])


def test_story_cards_and_mid_reply_toasts_are_named_as_heard(api, monkeypatch):
    from src.web import server

    monkeypatch.setattr(server, "pop_pending_events", lambda pid: [])
    monkeypatch.setattr(server, "drain_pending_cinematics", lambda pid: [
        {"kind": "boss_intro", "title": "Aldric Vane", "subtitle": "Ashen Fangs wolf-caller",
         "npc_id": api["aldric"]}])
    monkeypatch.setattr(server.objectives_mod, "pop_pending_events", lambda pid: [
        {"type": "objective_done", "quest_id": api["quest"], "objective_id": "o1",
         "text": "Get Captain Coldiron's gate pass for the north road"}])

    def frames(heard_now=""):
        return [json.loads(f[len("data: "):]) for f in server._drain_story_events(api["player"], heard_now)]

    toast, card = frames()
    assert card["title"] == "The Wolf-Mantled Man" and card["npc_id"] == api["aldric"]
    assert toast["text"] == "Get the Watch Captain's gate pass for the north road"
    # Said earlier in this very reply: no memory file holds it yet
    toast, card = frames('Watch Captain: "Coldiron. And that whistle is Vane\'s."')
    assert toast["text"] == "Get Captain Coldiron's gate pass for the north road"
    assert card["title"] == "Vane"

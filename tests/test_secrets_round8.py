"""Secrets, the dead and borrowed names — round 8 (fleet9, 2026-10-05).

- fleet9-bram T1: a persuasion roll on two plain questions came up critical
  and the Captain said "I've got eleven able bodies on the whole Watch
  roster" — the act-2 truth, in the first exchange, at trust 50. The line is
  now cut as it streams (`campaign.seal_line`, through
  `NameGate.player_name_in`), the dossier says no roll buys a sealed truth,
  and roll_check answers a plain question with no roll.
- fleet9-tamsin T18: "that whistle" — one word of a sealed truth. One word
  is now enough (`sealed_word_slipped`) when it names a thing a sealed truth
  keeps and the player has never met it. tamsin's own T18 is NOT one: the
  main quest's fourth step named the cracked bone whistle on her quest log
  from T3, and the seed's open text (Vex's and Vane's looks, the Fangs'
  look, the Hollow's Thing) says "whistle" throughout.
- fleet9-tamsin T2: the dead carter told as a survivor (`lost_person_alive`;
  a "Knows:" dossier line; the opening's arc line names him).
- fleet9-wenna T12: "Old Marrow worked nights before me" put the successor
  villain's surname in heard_names (`dialogue.borrowed_name`,
  `name_collision`, the brief's "Names in use").
- fleet9-bram T2: the notice quoted up to its signature, then "Signed with a
  captain's initial" (`read_text_altered`).
- fleet9-bram T0: the Wolf-Tooth Charm pitched as "a tooth off one of the
  brutes" (`reward_misnamed`; reward_words now quotes the name to say).

Replayed over every data/playtests/*/transcript.jsonl (26 runs, 23 with a
campaign DB; truths sealed until a beat revealing them fired):
- the guard would cut 7 speaker lines: fleet-bram T18, fleet3-tamsin
  T14/T15, fleet4-tamsin T19 (Maera's "eleven of his men"), fleet9-bram T1 —
  sealed counts said early — and fleet3-tamsin T20's two confession lines
  (no beat, no @reveal: held for their scene). No other line.
  Judged whole, every pair of one speaker's lines in one reply (398 — a
  split long line is two of them) cuts nothing more.
- sealed_word_slipped: 3 hits, 3 true (fleet4-tamsin T8 Vane's "a name
  struck off a roster", fleet8-bram T16 Piper's roster, fleet9-bram T1).
  Words anyone says ("terms", "numbers", "pattern", "cargo") are kept by
  no truth.
- lost_person_alive (the seed's Tobin given to every run): 1 hit, true —
  fleet9-tamsin T2. fleet-bram T13 ("three weeks of guessing … one carter
  walked back out alive", the carter being the player) no longer fires: a
  lost person's own time counts only beside the survival words.
- name_collision: 1 hit of 244 single-word mentions of an absent person
  (fleet9-wenna T12, true).
- read_text_altered: 2 of 14 reads, both true (the signature dropped:
  fleet5-wenna T6, fleet9-bram T2).
- reward_misnamed: 17 hits over 1102 lines, all true (every "a tooth off
  …" pitch from fleet2 on, and fleet8-bram T8's "a tooth to wear" payout).
- roll_check on a question: 31 of 65 player-typed roll turns typed no
  risky act and asked something; the tool refuses only a persuasion or
  cunning roll whose reason is getting words out of someone, with no danger
  in the room (fleet9-bram T1's "pressing the Captain to admit how bad it
  really is"), and never when what they do or say presses (a grab, a lie,
  coin, a blade, "…or I"): of those 31, 12 keep their roll that way, and
  fleet9-bram T1 is still answered with no roll.
"""

import pytest

from src.models import (
    NPC, Campaign, DMState, Location, LocationType, NPCRelationship, Player, Quest, QuestStatus, StoryBeat,
    WorldClock, get_session,
)
from src.services import campaign as camp
from src.services import dialogue
from src.services import memory_files
from src.services import stakes

ROSTER_STATEMENT = ("Captain Coldiron has falsified the Watch roster, claiming twenty able bodies when only eleven "
                    "remain, and has known for weeks that the road cannot hold without outside help.")
VILLAIN_STATEMENT = "Aldric Vane commands the Ashen Fangs and drills the wolf pack with a bone whistle of his own making."
T1_LINE = ("I've got eleven able bodies on the whole Watch roster. I say we're stretched thin. Truth is we're one "
           "bad night from not holding the gate at all.")


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(camp, "_spawn_generation", lambda player_id: None)
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    camp.reset_state()
    stakes.reset_state()
    yield
    camp.reset_state()
    stakes.reset_state()


@pytest.fixture()
def emberfall(db):
    """fleet9's Emberfall in miniature: the square at dusk, act 1."""
    with get_session() as session:
        session.add(WorldClock(day=1, hour=18))
        session.add(DMState())
        town = Location(name="Emberfall", type=LocationType.CITY, visited=True, discovered=True)
        session.add(town)
        session.flush()
        square = Location(name="Market Square", type=LocationType.DISTRICT, visited=True, discovered=True,
                          parent_id=town.id, description="A cobbled square with a notice board.")
        yard = Location(name="Stable Yard", type=LocationType.DISTRICT, visited=True, discovered=True,
                        parent_id=town.id, description="A muddy yard and the north paddock fence.")
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, discovered=True,
                          description="A birch hollow and a ruined hut.")
        session.add_all([square, yard, hollow])
        session.flush()
        people = {
            "coldiron": NPC(name="Captain Brann Coldiron", profession="Captain of the Lantern Watch",
                            description_physical="A broad man with an eyebrow scar.", current_location_id=square.id,
                            secrets=["The Watch roster is a lie — eleven able bodies, not twenty"]),
            "piper": NPC(name="Piper Quillow", profession="Herbalist's apprentice",
                         description_physical="A young woman with green-stained fingers.",
                         current_location_id=square.id),
            "sorrel": NPC(name="Sorrel Tack", profession="Stable hand", description_physical="A young man, wiry.",
                          current_location_id=yard.id),
            "vex": NPC(name="Vex Marrow", profession="Ashen Fangs scout",
                       description_physical="A lean, menacing man in an ash-gray cloak.",
                       current_location_id=hollow.id),
            "vane": NPC(name="Aldric Vane", profession="Ashen Fangs wolf-caller", danger_tier="boss",
                        description_physical="A broad, still man in a wolf-pelt mantle.",
                        current_location_id=hollow.id),
            "tobin": NPC(name="Tobin", profession="Carter", status="dead",
                         description_physical="A Greywater carter, weathered and broad, buried three weeks ago."),
        }
        session.add_all(people.values())
        player = Player(name="Bram Holloway", current_location_id=square.id)
        session.add(player)
        session.flush()
        teeth = Quest(title="Teeth on the North Road", status=QuestStatus.NOT_STARTED,
                      assigned_by_npc_id=people["coldiron"].id, description="Find what drills the wolves.",
                      rewards={"currency": 60, "items": [{"name": "Wolf-Tooth Charm", "kind": "trinket",
                                                          "about": "A wolf's fang bound in brass wire."}]})
        session.add(teeth)
        campaign = Campaign(
            player_id=player.id, title="The Wolf-Caller's Toll", status="active", current_act=1,
            premise="Someone drills the wolves.", villain_npc_id=people["vane"].id, villain_name="Aldric Vane",
            successor_npc_id=people["vex"].id,
            acts=[{"act": 1, "title": "Teeth", "premise": "The howls keep time."},
                  {"act": 2, "title": "Whistles", "premise": "The pack answers."},
                  {"act": 3, "title": "The Howl", "premise": "It ends at the hollow."}],
            fixed_truths=[
                {"id": "truth_betrayal", "kind": "the_betrayal", "revealed": False, "statement": ROSTER_STATEMENT,
                 "spoiler_keywords": ["roster", "eleven", "twenty", "coldiron"]},
                {"id": "truth_villain", "kind": "villain_identity", "revealed": False,
                 "statement": VILLAIN_STATEMENT, "spoiler_keywords": ["vane", "whistle"]},
            ])
        session.add(campaign)
        session.commit()
        ids = {k: v.id for k, v in people.items()}
        ids.update(player=player.id, campaign=campaign.id, square=square.id, yard=yard.id, hollow=hollow.id,
                   teeth=teeth.id)
        return ids


def _tick(w):
    return camp.run_campaign_tick(w["player"])


def _truth(w, truth_id):
    with get_session() as session:
        row = session.get(Campaign, w["campaign"])
        return next(t for t in row.fixed_truths if t["id"] == truth_id)


# ----------------------------------------------------------------------
# 1. The sealed truth is cut from the line as it streams
# ----------------------------------------------------------------------

def test_fleet9_bram_t1_the_roster_figure_is_cut_from_the_captains_line(emberfall):
    w = emberfall
    _tick(w)
    gate = dialogue.NameGate.for_turn(w["player"], "What's really happening on that north road?",
                                      present_ids=[w["coldiron"], w["piper"]])
    shown = gate.player_name_in(w["coldiron"], T1_LINE, "Watch Captain")
    assert "eleven" not in shown and "roster" not in shown
    assert shown.startswith("I say we're stretched thin.")
    assert gate.sealed_cuts and gate.sealed_cuts[0]["npc_id"] == w["coldiron"]
    cuts = camp.pop_seal_cuts(w["player"])
    assert len(cuts) == 1 and cuts[0]["truth_ids"] == ["truth_betrayal"] and "eleven" in cuts[0]["terms"]
    assert camp.pop_seal_cuts(w["player"]) == [], "a cut is reported once"
    # What the player heard is what the spine reads: the truth stays sealed
    camp.mark_beat_delivery(w["player"], "\n" + shown, speakers=["Watch Captain"],
                            spoken=[{"npc_id": w["coldiron"], "npc_name": "Watch Captain", "text": shown}])
    assert _truth(w, "truth_betrayal")["revealed"] is False


def test_a_line_that_is_all_sealed_truth_becomes_a_deflection(emberfall):
    w = emberfall
    _tick(w)
    shown = camp.seal_line(w["player"], w["coldiron"], "Eleven able bodies on the roster, not twenty.")
    assert shown == camp.SEAL_DEFLECTION


def test_a_history_replay_reports_no_second_cut(emberfall):
    w = emberfall
    _tick(w)
    camp.seal_line(w["player"], w["coldiron"], T1_LINE)
    assert len(camp.pop_seal_cuts(w["player"])) == 1
    assert camp.seal_line(w["player"], w["coldiron"], T1_LINE) != T1_LINE      # the replay still cuts it
    assert camp.pop_seal_cuts(w["player"]) == []


def test_the_beat_that_reveals_it_lets_it_through(emberfall):
    w = emberfall
    with get_session() as session:
        session.add(StoryBeat(campaign_id=w["campaign"], act=1, sequence=1, title="Eleven, Not Twenty",
                              beat_type="revelation", directive="Coldiron admits the roster.",
                              trigger_type="time", trigger_data={"delay": "days"}, status="fired",
                              reveals_truth_ids=["truth_betrayal"], delivery_keywords=["eleven", "roster"]))
        session.commit()
    _tick(w)
    assert camp.seal_line(w["player"], w["coldiron"], T1_LINE) == T1_LINE


def test_the_players_own_words_and_an_earned_reveal_let_it_through(emberfall):
    w = emberfall
    _tick(w)
    # The player said it first: their guess, confirmed
    assert camp.seal_line(w["player"], w["coldiron"], T1_LINE,
                          player_input='"Eleven men on your roster, Captain?"') == T1_LINE
    # Trust earned, @reveal written: his told secret says it
    with get_session() as session:
        session.add(NPCRelationship(player_id=w["player"], npc_id=w["coldiron"], trust_level=70,
                                    revealed_secrets=[0]))
        session.commit()
    assert camp.seal_line(w["player"], w["coldiron"], T1_LINE) == T1_LINE


def test_a_cut_is_audited_for_the_next_reply(emberfall):
    from src.web.turn_audit import sealed_speech_warnings

    w = emberfall
    tick = _tick(w)
    shown = camp.seal_line(w["player"], w["coldiron"], T1_LINE, speaker="Watch Captain")
    lines = [{"npc_name": "Watch Captain", "npc_id": w["coldiron"], "text": shown}]
    warnings = sealed_speech_warnings(tick["audit"], lines, "", "")
    assert [x["check"] for x in warnings] == ["sealed_truth_cut"]
    assert "NOT said" in warnings[0]["detail"] and "critical success" in warnings[0]["detail"]


def test_the_holders_dossier_says_no_roll_buys_it(emberfall):
    from src.services.scene_brief import build_scene_brief

    w = emberfall
    with get_session() as session:
        text = build_scene_brief(session, session.get(Player, w["player"]))["text"]
    coldiron = text[text.index("Captain Brann Coldiron ["):text.index("Piper Quillow [")]
    piper = text[text.index("Piper Quillow ["):]
    assert camp.SEALED_HOLDER_LINE in coldiron
    assert "not even a critical success" in coldiron
    assert camp.SEALED_HOLDER_LINE not in piper.split("\n- ")[0]


# Review fixes (round 8): what the guard cut was never heard; a long line is
# judged whole; a stalled reveal stays deliverable; a reload after a restart
# is cut too; the dossier never says "confess" and "never" at once

def test_a_name_in_the_cut_sentences_is_never_heard(emberfall):
    w = emberfall
    _tick(w)
    gate = dialogue.NameGate.for_turn(w["player"], "What's wrong with the Watch?", present_ids=[w["piper"]])
    line = "Brann Coldiron keeps eleven men on a roster that says twenty. Ask around."
    gate.hear(line)                         # the server hears a line before the guard reads it
    assert gate.player_name_in(w["piper"], line, "Piper") == "Ask around."
    assert not {"Brann", "Coldiron"} & set(gate.heard_this_turn() + gate.known_words())
    assert w["coldiron"] not in gate.known_ids()
    assert "Coldiron" not in gate.said_this_turn()


def test_a_long_line_split_in_two_is_judged_whole(emberfall):
    w = emberfall
    _tick(w)
    gate = dialogue.NameGate.for_turn(w["player"], "What's wrong with the Watch?", present_ids=[w["piper"]])
    head = "The Captain keeps the Watch roster pinned up by his door for the council to see."
    tail = "Count the bunks yourself and you'll find twenty names for half as many men. Ask him why."
    gate.hear(head + " " + tail)            # heard whole, sent as two bubbles
    assert gate.player_name_in(w["piper"], head, "Piper") == camp.SEAL_DEFLECTION
    assert gate.player_name_in(w["piper"], tail, "Piper") == "Ask him why."
    # ...each half alone says one term: no telling, nothing cut
    assert camp.seal_line(w["player"], w["piper"], head) == head


def test_a_stalled_beat_that_reveals_it_can_still_be_told(emberfall):
    w = emberfall
    with get_session() as session:
        session.add(StoryBeat(campaign_id=w["campaign"], act=1, sequence=1, title="Eleven, Not Twenty",
                              beat_type="revelation", directive="Coldiron admits the roster.",
                              trigger_type="time", trigger_data={"delay": "days"}, status="stalled",
                              reveals_truth_ids=["truth_betrayal"], delivery_keywords=["eleven", "roster"]))
        session.commit()
    _tick(w)
    assert camp.seal_line(w["player"], w["coldiron"], T1_LINE) == T1_LINE
    with get_session() as session:
        assert camp.sealed_holder_line(session, w["player"], session.get(NPC, w["coldiron"])) is None


def test_a_reload_before_any_tick_still_cuts(emberfall):
    w = emberfall
    camp.reset_state()                      # a restart: no tick in this process yet
    assert camp.seal_line(w["player"], w["coldiron"], T1_LINE).startswith("I say we're stretched thin.")
    tick = _tick(w)                         # ...and the first tick takes over
    assert camp._seal_audit(w["player"]) is tick["audit"]


def test_trust_that_opens_the_secret_gets_the_confession_line_not_a_contradiction(emberfall):
    from src.services.scene_brief import build_scene_brief

    w = emberfall
    with get_session() as session:
        session.add(NPCRelationship(player_id=w["player"], npc_id=w["coldiron"], trust_level=70))
        session.commit()
        text = build_scene_brief(session, session.get(Player, w["player"]))["text"]
    coldiron = text[text.index("Captain Brann Coldiron ["):text.index("Piper Quillow [")]
    assert "Would now confess if pressed" in coldiron
    assert camp.SEALED_CONFESS_LINE in coldiron and camp.SEALED_HOLDER_LINE not in coldiron
    # Told (an earned @reveal): the guard lets him say it, and no line calls it SEALED beside "Already told you"
    with get_session() as session:
        rel = session.query(NPCRelationship).filter(NPCRelationship.npc_id == w["coldiron"]).one()
        rel.revealed_secrets = [0]
        session.commit()
        assert camp.sealed_holder_line(session, w["player"], session.get(NPC, w["coldiron"])) is None
    assert camp.seal_line(w["player"], w["coldiron"], T1_LINE) == T1_LINE


# ----------------------------------------------------------------------
# roll_check on a pure question
# ----------------------------------------------------------------------

def test_fleet9_bram_t1_a_question_gets_no_roll(emberfall):
    from src.tools.world_write.progression import note_turn_input, roll_check

    w = emberfall
    stakes.begin_turn(w["player"])
    note_turn_input(w["player"], 'I study the Captain\'s scarred face. "What\'s really happening on that north '
                                 'road? And why should I trust a uniform?"')
    result = roll_check(w["player"], "persuasion", "tricky",
                        reason="pressing the Captain to admit how bad it really is")
    assert result.get("no_roll") is True and "event" not in result
    assert "answers or refuses in their own voice" in result["instruction"]
    assert "anything SEALED" in result["instruction"]


@pytest.mark.parametrize("typed, skill, reason", [
    ('I lean on him about the roster. "How many men, Captain?"', "persuasion", "getting him to admit the count"),
    ('"Let me through the gate, it can\'t wait. Why not?"', "persuasion", "getting the guard to explain"),
    ('"What\'s your name?" I ask, palming the purse into my coat.', "cunning", "ditching the stolen purse unseen"),
    ('"Where were you two nights back?"', "perception", "reading his face for the truth"),
    # Social acts roll_intent's binding classifier does not know (review)
    ('I grab his collar and slam him against the wall. "Tell me where Vex is."', "persuasion",
     "getting him to talk"),
    ('I lie and tell him the Captain sent me. "So what did you see on the road?"', "cunning",
     "getting him to tell what he saw"),
    ('I slide two coins across the bar. "What do you know about the satchel?"', "persuasion",
     "getting her to share what she knows"),
    ('I keep pressing him. "How many men do you really have?"', "persuasion", "getting him to admit the count"),
    ('"Tell me what you know, or I start breaking fingers."', "persuasion", "getting him to talk"),
])
def test_an_act_a_plea_or_another_skill_still_rolls(emberfall, typed, skill, reason):
    from src.tools.world_write.progression import note_turn_input, roll_check

    w = emberfall
    stakes.begin_turn(w["player"])
    note_turn_input(w["player"], typed)
    result = roll_check(w["player"], skill, "tricky", reason=reason)
    assert result.get("outcome") and not result.get("no_roll")


def test_an_input_from_another_reply_is_never_read(emberfall):
    from src.tools.world_write.progression import note_turn_input, roll_check

    w = emberfall
    stakes.begin_turn(w["player"])
    note_turn_input(w["player"], '"What happened on the north road?"')
    stakes.begin_turn(w["player"])           # the next reply: its input was never noted
    result = roll_check(w["player"], "persuasion", "tricky", reason="getting him to admit the count")
    assert result.get("outcome")


# ----------------------------------------------------------------------
# 2. One word of a sealed thing
# ----------------------------------------------------------------------

def test_a_truths_thing_words_are_its_nouns_never_a_name_a_deed_or_the_open_world():
    truths = [
        {"id": "a", "statement": ROSTER_STATEMENT, "spoiler_keywords": ["roster", "eleven", "coldiron"]},
        {"id": "b", "statement": "The Ashen Fangs' toll will not stop; Vane will call off the pack if the Watch "
                                 "publicly admits what was done.", "spoiler_keywords": ["vane", "admit", "call off"]},
        {"id": "c", "statement": "Vane drills the wolves with a bone whistle and the carved word SING.",
         "spoiler_keywords": ["whistle", "sing", "drilled"]},
        {"id": "d", "statement": "The Greywater cart was taken and its driver is dead.",
         "spoiler_keywords": ["driver", "greywater"]},
    ]
    probes = camp.sealed_object_terms(truths, names=["Captain Brann Coldiron", "Aldric Vane"])
    assert probes == {"a": ["roster"], "c": ["whistle", "sing"]}
    # A word the world uses openly (a Thing's label, someone's looks) is no secret
    assert camp.sealed_object_terms(truths, names=["Aldric Vane"], open_text="A cracked bone whistle.") == {
        "a": ["roster"], "c": ["sing"]}


def test_a_word_everyone_says_is_no_thing_kept():
    # fleet8-wenna's cost truth kept "terms", fleet9-wenna's "numbers" (review)
    truths = [{"id": "cost", "statement": "Ending it means the Watch accepts the Fangs' terms and gives up the "
                                          "numbers on the roster.",
               "spoiler_keywords": ["terms", "numbers", "roster"]}]
    assert camp.sealed_object_terms(truths) == {"cost": ["roster"]}


def test_fleet9_tamsin_t18_whistle_on_the_quest_log_is_met_roster_is_not(emberfall):
    from src.web.turn_audit import sealed_speech_warnings

    w = emberfall
    with get_session() as session:
        quest = session.get(Quest, w["teeth"])
        quest.status = QuestStatus.ACTIVE
        quest.objectives = [{"text": "Bring the cracked bone whistle back to Captain Coldiron as proof",
                             "kind": "custom"}]
        session.commit()
    tick = _tick(w)
    assert tick["audit"]["sealed_words"] == {"truth_betrayal": ["roster"]}
    whistle = [{"npc_name": "Captain Coldiron", "npc_id": w["coldiron"],
                "text": "Find out who's holding that whistle's other end, if there is one."}]
    assert sealed_speech_warnings(tick["audit"], whistle, "", "") == []
    roster = [{"npc_name": "Piper", "npc_id": w["piper"],
               "text": "I've seen him at night, lips moving over the roster."}]
    warnings = sealed_speech_warnings(tick["audit"], roster, "", "")
    assert [x["check"] for x in warnings] == ["sealed_word_slipped"] and '"roster"' in warnings[0]["detail"]
    # The reply's own prose showed it, or the player said it: met
    assert sealed_speech_warnings(tick["audit"], roster, "The roster board hangs by the door.", "") == []
    assert sealed_speech_warnings(tick["audit"], roster, "", "Who keeps the roster?") == []


def test_a_slipped_word_is_canon_from_then_on(emberfall):
    w = emberfall
    _tick(w)
    camp.mark_beat_delivery(w["player"], "Piper: lips moving over the roster.", speakers=["Piper"], spoken=[])
    assert _truth(w, "truth_betrayal").get("seen_words") == ["roster"]
    assert "truth_betrayal" not in _tick(w)["audit"]["sealed_words"]


# ----------------------------------------------------------------------
# 3. The dead stay dead
# ----------------------------------------------------------------------

LOST = [{"name": "Tobin", "profession": "Carter", "status": "dead",
         "about": "A Greywater carter, weathered and broad, buried three weeks ago."}]


def test_fleet9_tamsin_t2_the_dead_carter_told_as_a_survivor():
    from src.web.turn_audit import lost_person_alive

    line = ("A carter. Three weeks back. Driver survived — cart didn't, cargo didn't, and he hasn't said three "
            "words straight since.")
    hit = lost_person_alive([{"npc_name": "Watch Captain", "text": line}], LOST)
    assert hit and hit["check"] == "lost_person_alive" and "Tobin is dead" in hit["detail"]
    assert lost_person_alive([{"npc_name": "Piper", "text": "Tobin's alive, I swear I saw him."}], LOST)
    assert lost_person_alive([{"npc_name": "Hawker", "text": "Three weeks back. The carter came back alive but "
                                                             "raving."}], LOST)


@pytest.mark.parametrize("line", [
    "A carter went out three weeks back, came back dead in his own cart, horses gone quiet and wrong.",
    "The carter three weeks gone — he saw the whistle. That's why he's in the ground and you're still breathing.",
    "Carter's three weeks dead on the north road. Go up there and come back breathing to say it.",
    "Took a cart three nights back, driver alive, cargo gone, third time this month.",
    "Tobin never came back from the north road.",
    # Review: the dead's life before, a habit, someone else — the "Knows:"
    # line puts the dead in every mouth, and this is how they are spoken of
    "Back when Tobin was still alive, he drank here every night.",
    "Last time I saw Tobin alive, he was hitching his mule.",
    "Tobin came back through here every tenday with salt.",
    "Tobin is dead, three weeks now. The other drivers came back alive.",
    # fleet-bram T13, in a world with a dead carter: "three weeks" was the
    # Captain's own guessing, and the carter who walked out was the player
    "You did more up there in one night than I've managed in three weeks of guessing. That's worth paying for. "
    "Whoever's whistling at that pack isn't going to stop because one carter walked back out alive.",
])
def test_the_dead_spoken_of_as_dead_or_someone_else_alive_is_no_flag(line):
    from src.web.turn_audit import lost_person_alive

    assert lost_person_alive([{"npc_name": "Captain", "text": line}], LOST) is None


def test_the_people_here_know_the_dead_and_the_opening_names_him(emberfall):
    from src.services import opening
    from src.services.scene_brief import build_scene_brief

    w = emberfall
    with get_session() as session:
        player = session.get(Player, w["player"])
        text = build_scene_brief(session, player)["text"]
        coldiron = text[text.index("Captain Brann Coldiron ["):text.index("Piper Quillow [")]
        assert "Knows: Tobin (Carter) is dead — A Greywater carter, weathered and broad, buried three weeks ago." \
            in coldiron
        assert "never a survivor" in coldiron
        dm_state = session.query(DMState).first()
        dm_state.current_arc = "A carter dead on the north road three weeks ago is the loose thread."
        session.commit()
        directive = opening.build_opening_directive(session, player, None, session.get(Location, w["square"]),
                                                    None, dm_state, ["Watch Captain"], "evening")
    arc = next(line for line in directive.splitlines() if line.startswith("Your arc:"))
    assert "The carter this speaks of is Tobin — dead (A Greywater carter" in arc
    assert "never a survivor" in arc


# ----------------------------------------------------------------------
# 4. A name is one person
# ----------------------------------------------------------------------

WENNA_T12 = "Old Marrow worked nights before me, used to grumble the horses heard things."


def test_fleet9_wenna_t12_a_borrowed_surname_is_flagged():
    from src.web.turn_audit import name_collision

    people = [{"id": "vex", "name": "Vex Marrow", "profession": "Ashen Fangs scout",
               "looks": "A lean, menacing man in an ash-gray cloak.", "status": "alive"},
              {"id": "sorrel", "name": "Sorrel Tack", "profession": "Stable hand", "looks": "A young man.",
               "status": "alive"}]
    hit = name_collision([{"npc_name": "Stable Hand", "npc_id": "sorrel", "text": WENNA_T12}], people)
    assert hit and hit["check"] == "name_collision" and "Vex Marrow" in hit["detail"]
    for text in ("That's Vex Marrow, the Fangs' scout.", "Vex came by the yard asking after the north road.",
                 "That's Sorrel's old grandad talking, not me."):
        assert name_collision([{"npc_name": "Piper", "npc_id": "piper", "text": text}], people) is None
    # ...nor of someone standing right there
    assert name_collision([{"npc_name": "Stable Hand", "npc_id": "sorrel", "text": WENNA_T12}], people,
                          present_ids=["vex"]) is None


def test_a_borrowed_name_is_never_heard_now_or_from_memory(emberfall):
    w = emberfall
    gate = dialogue.NameGate.for_turn(w["player"], "Who worked the yard before you?", present_ids=[w["sorrel"]])
    gate.hear(WENNA_T12)
    gate.player_name_in(w["sorrel"], WENNA_T12, "Stable Hand")
    assert "Marrow" not in gate.heard_this_turn() and "Marrow" not in gate.known_words()
    assert w["vex"] not in gate.known_ids()
    assert "Marrow" not in gate.said_this_turn()
    # Filed in his memory, the line still names nobody next turn
    memory_files.append_exchange(w["player"], w["sorrel"], "Who worked the yard before you?", [WENNA_T12],
                                 "Day 1 19:00")
    assert "Marrow" not in dialogue.heard_corpus(w["player"])
    with get_session() as session:
        assert dialogue.display_name(session, w["player"], session.get(NPC, w["vex"])) != "Vex Marrow"
    # A name said as theirs still is
    gate = dialogue.NameGate.for_turn(w["player"], "", present_ids=[w["piper"]])
    gate.hear("Vex came by asking after the north road.")
    gate.player_name_in(w["piper"], "Vex came by asking after the north road.", "Piper")
    assert "Vex" in gate.heard_this_turn()


def test_the_brief_lists_the_names_in_use(emberfall):
    from src.services.scene_brief import build_scene_brief

    w = emberfall
    with get_session() as session:
        text = build_scene_brief(session, session.get(Player, w["player"]))["text"]
    line = next(x for x in text.splitlines() if x.startswith("Names in use"))
    assert "Vex Marrow" in line and "Aldric Vane" in line and "Tobin" in line
    assert "never give any of these names" in line


# ----------------------------------------------------------------------
# 5. Authored words are quoted whole
# ----------------------------------------------------------------------

NOTICE = ("TAKEN BY WOLVES — cart, driver unharmed, cargo gone, north road, third such this month. The Watch pays "
          "for true accounts of the howls. — Capt. B. Coldiron.\n\nBeneath it, smaller: 'Sweetroot for nerves. "
          "Ask Piper.'")
_READ = {"ok": True, "verb": "interact", "line": "@interact Notice board",
         "result": {"success": True, "kind": "read", "result": {"panel_text": NOTICE}}}


def test_fleet9_bram_t2_the_notice_paraphrased_past_its_signature():
    from src.web.turn_audit import read_text_altered

    t2 = ("*TAKEN BY WOLVES — cart, driver unharmed, cargo gone, north road, third such this month. The Watch pays "
          "for true accounts of the howls.* Signed with a captain's initial. Beneath it, smaller, almost an "
          "afterthought: *Sweetroot for nerves. Ask Piper.*")
    hit = read_text_altered([_READ], t2)
    assert hit and hit["check"] == "read_text_altered" and "capt b coldiron" in hit["detail"]
    t13 = ("*TAKEN BY WOLVES — cart, driver unharmed, cargo gone, north road, third such this month. The Watch "
           "pays for true accounts of the howls. — Capt. B. Coldiron.* Beneath it, in a smaller, different hand: "
           "*Sweetroot for nerves. Ask Piper.*")
    assert read_text_altered([_READ], t13) is None
    # Told about, not quoted: not judged here
    assert read_text_altered([_READ], "The board warns of wolves and asks for accounts of the howls.") is None


# ----------------------------------------------------------------------
# 6. The reward by its own name
# ----------------------------------------------------------------------

OFFERS = [{"giver_id": "coldiron", "title": "Teeth on the North Road", "coin": 60, "items": ["Wolf-Tooth Charm"]},
          {"giver_id": "piper", "title": "Counting the Howls", "coin": 10, "items": ["Yarrow Tincture"]}]


@pytest.mark.parametrize("who, line", [
    ("coldiron", "Sixty coin, a tooth off one of the brutes strung on a Watch cord, and the Watch remembers."),
    ("coldiron", "Read it yourself before you decide whether sixty coin and a tooth on a cord is worth your neck."),
])
def test_fleet9_bram_t0_t1_the_charm_pitched_as_a_trophy(who, line):
    from src.web.turn_audit import reward_misnamed

    hit = reward_misnamed([{"npc_name": "Watch Captain", "npc_id": who, "text": line}], OFFERS)
    assert hit and hit["check"] == "reward_misnamed" and '"Wolf-Tooth Charm"' in hit["detail"]


@pytest.mark.parametrize("who, line", [
    ("coldiron", "Sixty coin and a Wolf-Tooth Charm, and the Watch remembers a favour."),
    ("piper", "Nobody believes a girl who sells tea and yarrow, but if you stood watch with Sorrel and wrote it "
              "down, that's proof. I'll pay — ten coin and a tincture that clears your head."),
    ("piper", "Sixty coin and a tooth? The Captain's generous tonight."),
])
def test_the_charm_by_name_or_another_giver_is_no_flag(who, line):
    from src.web.turn_audit import reward_misnamed

    assert reward_misnamed([{"npc_name": "Someone", "npc_id": who, "text": line}], OFFERS) is None


def test_the_offer_says_the_reward_in_quotes(emberfall):
    from src.services import opening

    w = emberfall
    with get_session() as session:
        player = session.get(Player, w["player"])
        text = opening.build_opening_directive(session, player, None, session.get(Location, w["square"]),
                                               None, None, ["Watch Captain"], "evening")
    assert 'a Wolf-Tooth Charm (say "a Wolf-Tooth Charm"; handed over finished' in text
    assert "say the words in quotes" in text

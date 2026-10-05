"""Who knows whom: the relationship, the NPC's name, the player's name.

Round 5 (fleet6, all three runs): one NPCRelationship row meant three facts
— the player knows this NPC's name (the name gate unmasked anyone with a
row), the two have a relationship (trust, the giver's thanks, @trust, the
secret gates), and the NPC knows the PLAYER's name (the dossier said so).
Each conflation failed:

  * fleet6-bram: Coldiron gave the job (T0), the pass (T2) and paid (T20) as
    "Watch Captain" and never got a row, so @quest-done's +10 giver thanks
    did nothing; `@trust Hesk Bramble -5` was refused as "still a stranger".
  * fleet6-wenna: "Maera" was said by Piper (T12) and the player (T14); her
    labels flipped but the dossier kept her a stranger and the prose said
    "the Keeper" through T18. Her "Keeper" lines at T13 went out with no
    npc_id after a mid-reply @move.
  * fleet6-bram T19 / fleet6-tamsin T15: Piper said "Bram" / "Tamsin", which
    neither player ever gave — the brief told the DM she knew it.
  * fleet6-tamsin: "Ask Sorrel Tack…" ticked on a goodbye at T7 (the asking,
    to "the straw-haired boy", was T5); "Bring what you heard back to Piper"
    stayed open on a completed quest.
"""

import pytest

from src.models import (
    Location, LocationType, NPC, NPCRelationship, Player, Quest, QuestStatus, WorldClock, get_session,
)
from src.services import dialogue, memory_files
from src.services import objectives as obj
from src.services.commands import CommandRunner
from src.services.dialogue import NameGate, player_has_met, record_turn_dialogue
from src.services.scene_brief import build_scene_brief
from src.web.turn_audit import audit_turn, player_name_said

STATE = {"location_id": "x", "health": "healthy", "currency": 30, "inventory": []}


@pytest.fixture()
def world(db, monkeypatch):
    monkeypatch.setattr(obj, "publish", lambda player_id, records: None)
    with get_session() as session:
        session.add(WorldClock(day=1, hour=19, minute=0))
        square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True, visited=True)
        inn = Location(name="The Drowned Lantern", type=LocationType.BUILDING, depth=0, discovered=True)
        session.add_all([square, inn])
        session.flush()
        brann = NPC(name="Captain Brann Coldiron", profession="Captain of the Lantern Watch",
                    current_location_id=square.id,
                    description_physical="An old, weathered veteran with a gray beard cropped short.")
        piper = NPC(name="Piper Quillow", profession="Herbalist's apprentice", current_location_id=square.id,
                    description_physical="A young woman with bright, emotional eyes, chapped hands stained green.")
        sorrel = NPC(name="Sorrel Tack", profession="Stable hand", current_location_id=square.id,
                     description_physical="A friendly young man all elbows and straw-colored hair, warm brown "
                                          "eyes, a halter rope coiled over one shoulder.")
        hesk = NPC(name="Hesk Bramble", profession="Hawker of blades, tools and remedies",
                   current_location_id=square.id, secrets=["He buys wolf-taken goods through a fence."],
                   description_physical="A stooped, wiry hawker with a leather apron hung with knives.")
        maera = NPC(name="Maera Duskwell", profession="Keeper of The Drowned Lantern", current_location_id=inn.id,
                    description_physical="A middle-aged woman with a warm, smooth manner and a copper clasp.")
        session.add_all([brann, piper, sorrel, hesk, maera])
        player = Player(name="Wenna Cole", current_location_id=square.id, currency=30)
        session.add(player)
        session.commit()
        return {"player": player.id, "square": square.id, "inn": inn.id, "brann": brann.id, "piper": piper.id,
                "sorrel": sorrel.id, "hesk": hesk.id, "maera": maera.id}


def _row(world, key):
    with get_session() as session:
        rel = session.query(NPCRelationship).filter_by(player_id=world["player"], npc_id=world[key]).first()
        if rel is not None:
            session.expunge(rel)
        return rel


def _knows_name(world, key):
    with get_session() as session:
        return player_has_met(session, world["player"], world[key])


def _speech(world, key, text, label):
    return {"npc_id": world[key], "text": text, "label": label}


# --- the row is the relationship, the name is its own fact ---------------------

def test_a_row_from_before_the_split_still_means_both_names(world):
    with get_session() as session:
        session.add(NPCRelationship(npc_id=world["piper"], player_id=world["player"]))
        session.commit()
    rel = _row(world, "piper")
    assert rel.name_known is None and rel.knows_player_name is None
    assert dialogue.name_known(rel) and dialogue.knows_player_name(rel) and _knows_name(world, "piper")
    assert not dialogue.name_known(None) and not dialogue.knows_player_name(None)


def test_a_stranger_who_speaks_gets_a_row_but_keeps_their_mask(world):
    """fleet6-bram T0: the Watch Captain hired Bram under his role label."""
    record_turn_dialogue(world["player"], '"What do you need?"',
                         [_speech(world, "brann", "Walk the north road for me.", "Watch Captain")])
    rel = _row(world, "brann")
    assert rel is not None and rel.trust_level == 50
    assert rel.name_known is False and rel.knows_player_name is False
    assert not _knows_name(world, "brann")
    gate = NameGate.for_player(world["player"])
    assert gate.name_for(world["brann"]) == "Watch Captain"
    assert gate.mask("Captain Coldiron nods.") == "The Watch Captain nods."
    with get_session() as session:
        assert dialogue.display_name(session, world["player"], session.get(NPC, world["brann"])) == "Watch Captain"
    # Their name said, under their name: now it is known — on the same row
    record_turn_dialogue(world["player"], "Who are you?",
                         [_speech(world, "brann", "Coldiron. Captain of what's left of the Watch.", "Coldiron")])
    assert _row(world, "brann").name_known is True and _knows_name(world, "brann")


def test_a_name_heard_from_someone_else_unmasks_the_row(world):
    """fleet6-wenna: the Keeper spoke at T13 as a stranger; "Maera" was said
    by Piper and the player — her row says so now, and the brief stops
    telling the DM to call her "the Keeper"."""
    record_turn_dialogue(world["player"], "Evening.",
                         [_speech(world, "maera", "New in town?", "Keeper")])
    assert _row(world, "maera").name_known is False
    gate = NameGate.for_turn(world["player"], '"Piper Quillow sent me. She said Maera has work."',
                             present_ids=[world["maera"]])
    assert world["maera"] in gate.known_ids() and "Maera" in gate.known_words()
    assert dialogue.note_names_known(world["player"], gate.known_ids()) == [world["maera"]]
    assert _row(world, "maera").name_known is True
    with get_session() as session:
        player = session.get(Player, world["player"])
        player.current_location_id = world["inn"]
        session.commit()
        brief = build_scene_brief(session, session.get(Player, world["player"]))
    assert world["maera"] not in brief["strangers"] and "NOT known to the player" not in brief["text"]


def test_a_name_heard_with_no_row_is_known_from_the_heard_file(world):
    """fleet6-tamsin T7: the player read "Piper" on the notice board before
    ever trading a word with her — no relationship, but a known name."""
    assert not _knows_name(world, "piper")
    dialogue.remember_heard(world["player"], ["Piper"])
    assert _knows_name(world, "piper") and _row(world, "piper") is None
    assert dialogue.note_names_known(world["player"], [world["piper"]]) == []   # no row: none written


# --- trust, thanks and secrets land on the row ---------------------------------

def test_trust_and_secrets_on_a_stranger_land_on_their_row(world):
    """fleet6-bram T14: `@trust Hesk Bramble -5` was refused as "still a
    stranger" and the failed roll cost nothing."""
    r = CommandRunner(world["player"], present_ids=[world["hesk"]])
    res = r.run("@trust Hesk Bramble -5 | frightened")
    assert res.ok, res.message
    rel = _row(world, "hesk")
    assert rel.trust_level == 45 and rel.current_disposition == "frightened" and rel.name_known is False
    res = r.run("@reveal Hesk Bramble 0")
    assert not res.ok and "does not trust them" in res.message      # the gate reads the row
    assert _row(world, "hesk").name_known is False and not _knows_name(world, "hesk")
    assert NameGate.for_player(world["player"]).name_for(world["hesk"]) == "Hawker"


def _quest(world, giver, steps):
    with get_session() as session:
        quest = Quest(title="Counting the Howls", status=QuestStatus.ACTIVE, assigned_by_npc_id=world[giver],
                      rewards={"currency": 10}, objectives=steps)
        session.add(quest)
        player = session.get(Player, world["player"])
        player.active_quests = [quest.id]
        session.commit()
        return quest.id


STEPS = [
    {"id": "o1", "text": "Ask Sorrel Tack what the stable yard hears after dark", "kind": "talk_to",
     "target_id": None, "done": True},
    {"id": "o2", "text": "Bring what you heard back to Piper", "kind": "talk_to", "target_id": None, "done": False},
]


def _steps(world):
    steps = [dict(s) for s in STEPS]
    steps[0]["target_id"], steps[1]["target_id"] = world["sorrel"], world["piper"]
    return steps


def _done(quest_id):
    with get_session() as session:
        return {o["id"]: o["done"] for o in session.get(Quest, quest_id).objectives}


def test_a_giver_who_pays_in_person_is_thanked_and_the_report_step_closes(world):
    """fleet6-tamsin T11: Piper paid, had no row, got no thanks, and "Bring
    what you heard back to Piper" stayed open on the finished quest."""
    quest_id = _quest(world, "piper", _steps(world))
    r = CommandRunner(world["player"], present_ids=[world["piper"]])
    assert r.run("@quest-done Counting the Howls").ok
    rel = _row(world, "piper")
    assert rel.trust_level == 60 and rel.name_known is False
    assert _done(quest_id) == {"o1": True, "o2": True}


def test_a_giver_elsewhere_closes_no_report_step(world):
    """fleet6-wenna T20: Maera's coin arrived at the gatehouse; she was not there."""
    quest_id = _quest(world, "maera", [{"id": "o1", "text": "Tell Maera what the letter said",
                                        "kind": "talk_to", "target_id": world["maera"], "done": False}])
    assert CommandRunner(world["player"]).run("@quest-done Counting the Howls").ok
    assert _done(quest_id) == {"o1": False}
    assert _row(world, "maera").trust_level == 60      # the thanks still lands


# --- the player's name -----------------------------------------------------------

def test_who_said_the_players_name():
    assert player_name_said([], "Wenna Cole", set(), "", '"I\'m Wenna Cole, and I collect stories."')
    assert player_name_said([], "Wenna Cole", set(), "", "I tell her my name and nothing else.")
    assert player_name_said([], "Wenna Cole", set(), "You tell her your name.", "I sit.")
    # The player's own narration names nobody to anyone
    assert not player_name_said([], "Wenna Cole", set(), "", "Wenna leans on the bar. \"A drink.\"")
    assert not player_name_said([], "Wenna Cole", set(), "", "[OPENING] This is Wenna Cole's first moment.")
    line = [{"npc_id": "p1", "npc_name": "Piper", "text": "This is Wenna. She's with me."}]
    assert player_name_said(line, "Wenna Cole", {"p1"}, "", "I nod.")
    assert not player_name_said(line, "Wenna Cole", set(), "", "I nod."), "a stranger's guess teaches nobody"


def test_a_met_npc_who_never_heard_the_name_is_flagged_and_learns_it_when_told(world):
    """fleet6-bram T19: Piper, an old acquaintance, said "Bram" though he never gave it."""
    record_turn_dialogue(world["player"], '"What did you hear?"',
                         [_speech(world, "piper", "I'm Piper. Three howls, staggered.", "Piper")])
    rel = _row(world, "piper")
    assert rel.name_known is True and rel.knows_player_name is False
    with get_session() as session:
        knew = {r.npc_id for r in session.query(NPCRelationship).filter(
            NPCRelationship.player_id == world["player"], dialogue.knows_player_name_clause())}
        brief = build_scene_brief(session, session.get(Player, world["player"]))
    assert world["piper"] not in knew
    assert "Knows the player's name: no" in brief["text"]
    spoken = [{"npc_id": world["piper"], "npc_name": "Piper", "text": "And Wenna — thank you."}]
    warnings = audit_turn("She smiles.", [], STATE, STATE, spoken=spoken, player_name="Wenna Cole",
                          met_before=knew, player_input="I nod.")
    assert any(w["check"] == "name_before_introduction" for w in warnings)

    # Told it, in front of her: she knows it from now on
    said = [{"npc_id": world["piper"], "npc_name": "Piper", "text": "Wenna, then."}]
    assert player_name_said(said, "Wenna Cole", knew, "", '"Call me Wenna."')
    assert world["piper"] in dialogue.note_player_name_heard(world["player"], said)
    assert _row(world, "piper").knows_player_name is True
    with get_session() as session:
        brief = build_scene_brief(session, session.get(Player, world["player"]))
    piper_block = brief["text"].split("Piper Quillow [")[1]
    assert "Knows the player's name: yes" in piper_block.split("\n- ")[0]


def test_a_bystander_who_overheard_the_name_knows_it_when_they_first_speak(world):
    """fleet4-wenna: "I'm Wenna," to Piper at T2 with Sorrel in the square;
    "Miss Wenna," from him at T20 is no leak."""
    said = [{"npc_id": world["piper"], "npc_name": "Piper", "text": "Wenna. Good."}]
    record_turn_dialogue(world["player"], '"I\'m Wenna."', [_speech(world, "piper", "Wenna. Good.", "Piper")])
    learned = dialogue.note_player_name_heard(world["player"], said)
    assert world["piper"] in learned and world["sorrel"] in learned
    assert _row(world, "sorrel") is None, "overhearing is no relationship"
    with get_session() as session:
        brief = build_scene_brief(session, session.get(Player, world["player"]))
    sorrel_block = brief["text"].split("real name Sorrel Tack")[1].split("\n- ")[0]
    assert "Knows the player's name: yes" in sorrel_block
    record_turn_dialogue(world["player"], "Morning.", [_speech(world, "sorrel", "Miss Wenna!", "Stable Hand")])
    assert _row(world, "sorrel").knows_player_name is True


def test_a_new_row_remembers_the_player_naming_themself_before(world):
    """An exchange filed before the column existed: "I'm Wenna Cole," to a
    stranger who then had no row (fleet6-wenna T14)."""
    memory_files.append_exchange(world["player"], world["maera"], "I'm Wenna Cole.", ["Welcome."], "Day 1 19:00")
    record_turn_dialogue(world["player"], "And the satchel?",
                         [_speech(world, "maera", "Upstairs, Wenna Cole.", "Keeper")])
    rel = _row(world, "maera")
    assert rel.knows_player_name is True and rel.name_known is False


# --- speaker labels ---------------------------------------------------------------

def test_a_bare_trade_word_finds_the_one_person_here_with_that_trade(world):
    """fleet6-wenna T13: "Keeper" lines went out with no npc_id."""
    with get_session() as session:
        assert dialogue.resolve_speaker(session, "Keeper", [world["maera"]]).id == world["maera"]
        assert dialogue.resolve_speaker(session, "Innkeeper", [world["maera"]]).id == world["maera"]
        assert dialogue.resolve_speaker(session, "Apprentice", [world["piper"], world["sorrel"]]).id == world["piper"]
        assert dialogue.resolve_speaker(session, "Captain", [world["brann"]]).id == world["brann"]
        assert dialogue.resolve_speaker(session, "Man", [world["sorrel"], world["brann"]]) is None
        # Nobody here keeps an inn: a keeper elsewhere is no one's voice
        assert dialogue.resolve_speaker(session, "Keeper", [world["piper"]]) is None


def test_speech_after_a_mid_reply_move_resolves_among_the_people_there(world):
    from src.web.server import _present_at

    assert _present_at([world["piper"]], world["inn"]) == [world["piper"], world["maera"]]
    assert _present_at([world["piper"]], None) == [world["piper"]]


# --- talk_to ----------------------------------------------------------------------

def _talked(world, key, text, speakers, *, here=True):
    with get_session() as session:
        npc = session.get(NPC, world[key])
        others = [n for n in session.query(NPC).all() if n.id != npc.id]
        return dialogue._talked_to(npc, text, {world[k] for k in speakers}, others, None, here=here)


def test_the_player_may_address_someone_by_their_looks_or_trade(world):
    """fleet6-tamsin T5: "I glance at the straw-haired boy. 'What's your name?
    And when does the stable yard get quietest?'" — Sorrel answered."""
    text = ('I lean back, studying her. "Ten coin and a tincture," I repeat.\n\nI glance at the straw-haired '
            'boy. "What\'s your name? And when does the stable yard get quietest?"')
    assert _talked(world, "sorrel", text, ["sorrel", "piper"])
    assert not _talked(world, "sorrel", text, ["sorrel", "piper"], here=False), "looks need them standing there"
    assert not _talked(world, "sorrel", text, ["piper"]), "nor did he answer"
    assert _talked(world, "piper", 'I turn to the apprentice. "What\'s your name?"', ["piper", "sorrel"])
    # A look that moves on is no address (fleet6-wenna T10)
    assert not _talked(world, "sorrel", 'I glance at Sorrel, then back to the Captain. "Did the rider say '
                                        'anything?"', ["sorrel", "brann"])
    assert _talked(world, "sorrel", 'I look at Sorrel hard. "Did it move?"', ["sorrel", "brann"])


def test_a_goodbye_is_no_conversation(world):
    """fleet6-tamsin T7."""
    text = ('I pocket the vial. "You\'ve got my word," I tell the girl. "I\'ll listen clean."\n\n'
            'I turn toward Sorrel with a nod. "Meet you at the stable yard in a few minutes. I need to grab '
            'something from my room first."')
    assert not _talked(world, "sorrel", text, ["sorrel"])
    # A goodbye at the end of a real talk is still a talk (fleet3-bram T6)
    text = ('"Dyer," I repeat. "I\'ll visit his grave." I meet Coldiron\'s eyes. "I\'ll be back with '
            'answers, Captain."')
    assert _talked(world, "brann", text, ["brann"])
    assert not _talked(world, "sorrel", '"See you at dawn, Sorrel."', ["sorrel"])
    assert _talked(world, "sorrel", '"See you at dawn, Sorrel — what time does the bell ring?"', ["sorrel"])


def test_a_quest_taken_this_reply_ticks_none_of_its_talk_steps(world):
    """fleet6-tamsin T4: the player asked the straw-haired boy about the man
    in ash-gray, then Piper offered the job — the asking came at T5."""
    quest_id = _quest(world, "piper", [{"id": "o1", "text": "Ask Sorrel Tack what the stable yard hears",
                                        "kind": "talk_to", "target_id": world["sorrel"], "done": False}])
    text = 'I turn toward the straw-haired boy. "Did you see his face?"'
    speech = [_speech(world, "sorrel", "Too dark.", "Stable Hand")]
    assert record_turn_dialogue(world["player"], text, speech, started_quest_ids=[quest_id]) == []
    assert _done(quest_id) == {"o1": False}
    lines = record_turn_dialogue(world["player"], text, speech)
    assert lines and _done(quest_id) == {"o1": True}


# --- review: what is said, what is on file, where it was said -----------------------

def test_a_question_or_a_refusal_gives_no_name():
    """A row learns the player's name from these phrases, so an NPC asking
    for it, or the player withholding it, must teach nobody."""
    assert not player_name_said([], "Wenna Cole", set(), '"Will you tell me your name?" she asks.', "I sit.")
    assert not player_name_said([], "Wenna Cole", set(), "You refuse to give your name.", "I sit.")
    assert not player_name_said([], "Wenna Cole", set(), "", "I do not tell him my name. I wait.")
    assert not player_name_said([], "Wenna Cole", set(), "", '"I will tell you my name when you give me yours."')
    # Withheld, then used: still a leak
    spoken = [{"npc_id": "p1", "npc_name": "Piper", "text": "Fine, Wenna, keep your secrets."}]
    warnings = audit_turn("You never give your name.", [], STATE, STATE, spoken=spoken,
                          player_name="Wenna Cole", met_before=set(), player_input="I shrug.")
    assert any(w["check"] == "name_before_introduction" for w in warnings)


def test_a_row_written_by_trust_starts_from_the_names_on_file(world):
    """@trust and a giver's thanks write rows too: the names known before the
    row existed come with it (an exchange filed before the split)."""
    memory_files.append_exchange(world["player"], world["hesk"], "I'm Wenna Cole.", ["Hesk. Hesk Bramble."],
                                 "Day 1 19:00")
    dialogue.remember_heard(world["player"], ["Hesk"])
    assert CommandRunner(world["player"], present_ids=[world["hesk"]]).run("@trust Hesk Bramble +5").ok
    rel = _row(world, "hesk")
    assert rel.name_known is True and rel.knows_player_name is True
    # No row yet: the dossier already says what the row will start from
    memory_files.append_exchange(world["player"], world["sorrel"], "I'm Wenna.", [], "Day 1 19:00")
    with get_session() as session:
        brief = build_scene_brief(session, session.get(Player, world["player"]))
    sorrel_block = brief["text"].split("real name Sorrel Tack")[1].split("\n- ")[0]
    assert _row(world, "sorrel") is None and "Knows the player's name: yes" in sorrel_block


def test_a_reply_that_moved_the_player_teaches_no_bystander(world):
    """'I tell the captain my name and head for the gate': which side of the
    door it was said on is unknowable, so only who spoke learns it."""
    with get_session() as session:
        session.get(Player, world["player"]).current_location_id = world["inn"]
        session.commit()
    said = [{"npc_id": world["piper"], "npc_name": "Piper", "text": "Wenna. Good."}]
    learned = dialogue.note_player_name_heard(world["player"], said, left_from=world["square"])
    assert learned == [world["piper"]]
    assert world["maera"] in dialogue.note_player_name_heard(world["player"], said, left_from=world["inn"])


def test_a_place_word_heard_names_nobody(world):
    """Every round-5 heard-names file holds "Wolf" and "Hollow", heard in
    "Wolf's Hollow": a hunter called Wolf is not met by a walk there."""
    with get_session() as session:
        session.add(Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True))
        hunter = NPC(name="Garrick Wolf", profession="Trapper", current_location_id=world["square"])
        session.add(hunter)
        session.commit()
        hunter = hunter.id
    gate = NameGate.for_turn(world["player"], '"Is the road to Wolf\'s Hollow open?"', present_ids=[hunter])
    assert hunter not in gate.known_ids() and "Wolf" not in gate.known_words()
    dialogue.remember_heard(world["player"], ["Wolf", "Hollow"])     # as an older turn kept them
    with get_session() as session:
        assert not player_has_met(session, world["player"], hunter)
    dialogue.remember_heard(world["player"], ["Garrick"])
    with get_session() as session:
        assert player_has_met(session, world["player"], hunter)


def test_a_quest_restated_as_started_holds_back_no_step(world):
    """Only a quest this reply really started waits; '@quest-start' on one
    already active started nothing."""
    from src.web.server import _quests_started

    active = _quest(world, "piper", _steps(world))
    with get_session() as session:
        fresh = Quest(title="A Letter for a Buried Man", status=QuestStatus.NOT_STARTED,
                      assigned_by_npc_id=world["maera"], objectives=[])
        session.add(fresh)
        session.commit()
        fresh = fresh.id
    r = CommandRunner(world["player"])
    assert r.run("@quest-start Counting the Howls").ok
    assert r.run("@quest-new Counting the Howls | about: again | step: talk_to Piper").ok
    assert r.run("@quest-start A Letter for a Buried Man").ok
    assert _quests_started(r.results) == [fresh] and active not in _quests_started(r.results)


def test_a_report_that_opens_with_later_is_still_a_talk(world):
    assert _talked(world, "piper", 'I turn to Piper. "Later that night it howled three times, then twice."',
                   ["piper"])
    assert _talked(world, "sorrel", '"Take care of the mare tonight, Sorrel."', ["sorrel"])
    assert not _talked(world, "sorrel", '"Later, then. Take care, Sorrel."', ["sorrel"])


# --- the wiring, end to end through /api/play ---------------------------------------

class _FakeAgent:
    def __init__(self, events):
        self.events = events
        self.messages = []

    async def stream_async(self, context):
        for event in self.events:
            yield event


class _FakeDM:
    def __init__(self, events, present):
        self.agent = _FakeAgent(events)
        self.last_brief = {"present": present}
        self._player_name = "Wenna Cole"

    def _build_context(self, player_input, **_kwargs):
        return "THE SCENE: " + ", ".join(self.last_brief["present"].values())


def _play(world, monkeypatch, reply, player_input, present):
    import asyncio
    import json

    from src.web import server
    from src.web.streaming import ToolUsageTracker

    with get_session() as session:
        names = {world[k]: session.get(NPC, world[k]).name for k in present}
    dm = _FakeDM([{"data": reply}], names)
    monkeypatch.setattr(server, "get_or_create_session", lambda pid: {"dm": dm, "tool_tracker": ToolUsageTracker()})
    monkeypatch.setattr(server, "_engine_ambush", lambda *a, **k: None)
    monkeypatch.setattr(server, "maybe_advance_world", lambda: None)
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    server._pending_audit_feedback.pop(world["player"], None)

    async def run():
        response = await server.play_sse(server.GameRequest(player_input=player_input, player_id=world["player"]))
        return [chunk async for chunk in response.body_iterator]

    frames = [json.loads(line[6:]) for chunk in asyncio.run(run()) for line in str(chunk).splitlines()
              if line.startswith("data: ") and line[6:].strip() not in ("", "{}")]
    complete = next(f for f in frames if f.get("type") == "complete")
    return [w["check"] for w in complete["audit"]]


def test_play_records_who_heard_the_players_name(world, monkeypatch):
    """fleet6-wenna T14 -> T15 and fleet4-wenna T2 -> T20, through the stream:
    the girl she told, and the stable hand standing by, both know it after;
    the keeper across town, who never heard it, is caught saying it."""
    checks = _play(world, monkeypatch, 'Piper: "Wenna. Good — I\'m Piper."\nShe smiles.\n',
                   '"I\'m Wenna," I tell the girl.', ("piper", "sorrel"))
    assert "name_before_introduction" not in checks
    rel = _row(world, "piper")
    assert rel.name_known is True and rel.knows_player_name is True
    assert _row(world, "sorrel") is None and dialogue.overheard_player_name(world["player"], world["sorrel"])

    checks = _play(world, monkeypatch, 'Stable Hand: "Evening, Wenna."\nHe tips his cap.\n',
                   "I wait by the well.", ("piper", "sorrel"))
    assert "name_before_introduction" not in checks
    rel = _row(world, "sorrel")
    assert rel.knows_player_name is True and rel.name_known is False      # he never gave his

    with get_session() as session:
        session.get(Player, world["player"]).current_location_id = world["inn"]
        session.commit()
    checks = _play(world, monkeypatch, 'Keeper: "Evening, Wenna. Sit."\nShe wipes the bar.\n',
                   "I take a stool.", ("maera",))
    assert "name_before_introduction" in checks
    rel = _row(world, "maera")
    assert rel is not None and rel.knows_player_name is False, "a leak teaches nobody"

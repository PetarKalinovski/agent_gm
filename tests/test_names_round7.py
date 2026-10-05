"""Names, voice and meta leaks (fleet8 playtest critics, round 7).

  * The pack bypassed the NameGate (fleet8-tamsin): the Gate Pass read
    "Captain Coldiron's seal…" and the found whistle "…the one Captain
    Coldiron never mentions", though nobody had said "Coldiron" aloud.
  * Pronoun and identity drift (all three runs): the stable hand was "she"
    in one reply and "he" in the next; the dead carter was "alive" at T0
    (fleet8-tamsin) beside the cart still due on the road.
  * Name cuts left broken lines (fleet8-wenna T18 "And — was it? Come back
    after…") and kept coming back: Sorrel's memory file, read into his
    dossier, called the player "Wenna" every turn.
  * "Not eight o'clock yet, not by a long count" a quarter hour before the
    20:00 howl (fleet8-tamsin T9).
  * The howl pattern told five times before the player left town
    (fleet8-tamsin T3-T10); the opening priced the reward as a trophy
    (fleet8-wenna T0).
"""

import asyncio

import pytest

from src.models import (
    NPC, Event, Location, LocationType, NPCRelationship, Player, Quest, QuestStatus, WorldClock,
    get_session,
)
from src.services import dialogue, memory_files
from src.services import objectives as obj
from src.services.dialogue import cut_player_name, look_tag, pronoun, without_player_name
from src.services.scene_brief import build_scene_brief


@pytest.fixture()
def world(db, tmp_path, monkeypatch):
    monkeypatch.setattr(memory_files, "memory_root", lambda: tmp_path / "memory")
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    with get_session() as session:
        session.add(WorldClock(day=1, hour=19, minute=45))
        square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True,
                          visited=True)
        yard = Location(name="Stable Yard", type=LocationType.POI, depth=0, discovered=True)
        session.add_all([square, yard])
        session.flush()
        brann = NPC(name="Captain Brann Coldiron", profession="Captain of the Lantern Watch",
                    current_location_id=square.id,
                    description_physical="An old, weathered veteran with a gray beard cropped short.",
                    secrets=["He found a bone whistle near the last raided cart and told no one"])
        piper = NPC(name="Piper Quillow", profession="Herbalist's apprentice", current_location_id=square.id,
                    description_physical="A young woman with bright, emotional eyes and chapped hands.")
        sorrel = NPC(name="Sorrel Tack", profession="Stable hand", current_location_id=yard.id,
                     description_physical="A friendly young man all elbows and straw-colored hair.")
        hesk = NPC(name="Hesk Bramble", profession="Hawker of blades", current_location_id=square.id,
                   description_physical="A stooped, wiry hawker; his stall is a cart.",
                   inventory_notable=[{"id": "watch-knife", "name": "Watch Knife", "type": "weapon",
                                       "description": "Captain Coldiron's old issue blade.", "value": 5}])
        tobin = NPC(name="Tobin", profession="Carter", status="dead", current_location_id=None,
                    description_physical="A Greywater carter, weathered and broad, buried three weeks ago.",
                    description_personality="Worried, by his wife's letter.")
        wolf = NPC(name="Gray-Flank Wolf", profession="wolf", species="wolf", current_location_id=yard.id,
                   description_physical="A rangy gray wolf.")
        session.add_all([brann, piper, sorrel, hesk, tobin, wolf])
        session.add(Event(name="The Evening Howl", description="Howling rises from the hills.",
                          event_type="meso", scheduled_day=1, scheduled_hour=20, player_visible=True))
        session.add(Event(name="The Greywater Cart Arrives Short",
                          description="The overdue supply cart limps through the gate half-empty.",
                          event_type="meso", scheduled_day=2, scheduled_hour=9, player_visible=True))
        player = Player(name="Wenna Cole", current_location_id=square.id, currency=25,
                        inventory=[{"id": "gate-pass", "name": "Gate Pass", "type": "key",
                                    "description": "Captain Coldiron's seal pressed into the wax corner."}])
        session.add(player)
        session.flush()
        session.add(Quest(title="Counting the Howls", status=QuestStatus.NOT_STARTED,
                          assigned_by_npc_id=piper.id, description="Piper swears the howls keep a schedule.",
                          objectives=obj.normalize_objectives([
                              {"text": "Ask Sorrel Tack what the stable yard hears after dark",
                               "kind": "talk_to", "target_id": sorrel.id}], session=session)))
        session.commit()
        return {"player": player.id, "square": square.id, "yard": yard.id, "brann": brann.id,
                "piper": piper.id, "sorrel": sorrel.id, "hesk": hesk.id, "tobin": tobin.id, "wolf": wolf.id}


def _brief(world, addressed=None):
    with get_session() as session:
        return build_scene_brief(session, session.get(Player, world["player"]), addressed)["text"]


def _move(world, where):
    with get_session() as session:
        session.get(Player, world["player"]).current_location_id = world[where]
        session.commit()


# ----------------------------------------------------------------------
# The pack, the counter, the canvas find: names as far as heard
# ----------------------------------------------------------------------

def test_fleet8_tamsin_the_pack_names_people_only_as_heard(world):
    from src.web import server

    state = server.build_player_state(world["player"])
    item = next(i for i in state["inventory"] if i["item_id"] == "gate-pass")
    assert item["description"] == "The Watch Captain's seal pressed into the wax corner."
    assert "Coldiron" not in str(state["inventory"])
    dialogue.remember_heard(world["player"], ["Coldiron"])
    state = server.build_player_state(world["player"])
    item = next(i for i in state["inventory"] if i["item_id"] == "gate-pass")
    assert item["description"] == "Captain Coldiron's seal pressed into the wax corner."
    assert "Brann" not in str(state["inventory"])


def test_the_counter_shelf_and_its_refusals_name_people_as_heard(world, tmp_path, monkeypatch):
    from src.web import server

    with get_session() as session:
        view = server._shop_view(session, session.get(Player, world["player"]), session.get(NPC, world["hesk"]))
    assert "Coldiron" not in str(view["stock"]) and "The Watch Captain's old issue blade." in str(view["stock"])
    assert view["stock"][0]["item_id"] == "watch-knife"            # the id still buys it
    _move(world, "yard")
    with get_session() as session:
        _p, _n, err = server._shop_guard(session, world["player"], world["hesk"], need_stock=True)
    assert err is not None and b"Hawker is not here" in err.body and b"Hesk" not in err.body


def test_a_canvas_find_reads_names_as_heard(world, tmp_path, monkeypatch):
    from src.services.scene_objects import normalize_objects
    from src.web import server

    monkeypatch.setattr(server, "get_active_db_path", lambda: str(tmp_path / "test.db"))
    monkeypatch.setattr(server, "maybe_advance_world", lambda: None)
    with get_session() as session:
        square = session.get(Location, world["square"])
        square.objects = normalize_objects([{"kind": "take", "label": "Bone whistle", "x": 40, "y": 60,
                                             "payload": {"item": {"name": "Coldiron's Whistle", "type": "misc",
                                                                  "description": "Like the one Coldiron keeps."}}}])
        session.commit()
        object_id = square.objects[0]["id"]
    result = asyncio.run(server.canvas_interact(server.InteractRequest(
        player_id=world["player"], location_id=world["square"], object_id=object_id)))
    assert result["success"]
    assert result["result"]["items_gained"][0]["name"] == "The Watch Captain's Whistle"
    assert "Coldiron" not in str(result["result"])
    pack = next(i for i in result["state"]["inventory"] if i["item_id"] != "gate-pass")
    assert pack["name"] == "The Watch Captain's Whistle" and "coldiron" in pack["item_id"]   # the id is untouched
    assert pack["description"] == "Like the one the Watch Captain keeps."
    assert "Coldiron" not in str(result["state"]["inventory"])


# ----------------------------------------------------------------------
# A look and a pronoun on every person line
# ----------------------------------------------------------------------

def test_looks_and_pronouns_come_from_the_authored_row(world):
    with get_session() as session:
        tags = {key: look_tag(session.get(NPC, world[key]))
                for key in ("sorrel", "piper", "brann", "hesk", "tobin", "wolf")}
    assert tags == {"sorrel": "young man, he", "piper": "young woman, she", "brann": "old veteran, he",
                    "hesk": "stooped hawker, he", "tobin": "he", "wolf": ""}
    # Nothing said, nothing guessed
    assert look_tag(NPC(name="Ash", description_physical="A figure in grey.")) == ""
    assert pronoun(NPC(name="Ash", description_personality='Says "she" of everyone; his own counsel.')) == "he"


def test_the_brief_keeps_every_person_line_on_one_look(world):
    text = _brief(world)
    head = next(line for line in text.splitlines() if "real name Piper Quillow" in line)
    assert "Herbalist's apprentice — young woman, she (unrated)" in head
    # fleet8-wenna T3: Piper's job names the stable hand, whose dossier was
    # in no brief yet ("ask the stable hand what she hears")
    who = next(line for line in text.splitlines() if line.startswith("People your quests and offers name"))
    assert "Sorrel Tack [" in who and "young man, he, the Stable Hand to the player, @ Stable Yard" in who
    assert "Coldiron" not in who                                   # he has his dossier above


def test_a_job_kept_for_a_later_act_names_nobody(world, monkeypatch):
    """The dossier leaves a later act's job out (`_quests_offered`); its
    people stay out of the quest-people line too."""
    from src.services import campaign

    with get_session() as session:
        howls = session.query(Quest).filter_by(title="Counting the Howls").one().id
    monkeypatch.setattr(campaign, "later_act_quest_ids", lambda session, player_id: {howls})
    assert "People your quests and offers name" not in _brief(world)


def test_known_people_elsewhere_carry_their_look(world):
    with get_session() as session:
        session.add(NPCRelationship(npc_id=world["sorrel"], player_id=world["player"]))
        session.commit()
    text = _brief(world)
    elsewhere = text.split("People you know elsewhere:")[1].splitlines()[0]
    assert "Sorrel Tack [" in elsewhere and "(young man, he) @ Stable Yard" in elsewhere
    assert "People your quests and offers name" not in text      # listed once


def test_the_dead_carter_is_never_the_cart_still_due(world):
    """fleet8-tamsin T0-T2: "Took a cart three nights back, driver alive",
    then "Driver's name is Tobin. Was. Found him three weeks back"."""
    lost = next(line for line in _brief(world).splitlines() if line.startswith("People the story has lost"))
    assert "Tobin [" in lost and "(Carter, he, dead, name not yet heard by the player)" in lost
    assert "buried three weeks ago" in lost
    assert 'never the people of "The Greywater Cart Arrives Short" (day 2, 09:00, not happened yet)' in lost


def test_a_lost_trade_matches_whole_words_only(world):
    """A dead stable hand is not "a handful of riders"; a trade's small
    words ("with") match nothing."""
    with get_session() as session:
        session.add(NPC(name="Wat Furrow", profession="Stable hand with one eye", status="dead",
                        description_physical="An old man, buried last spring."))
        session.add(Event(name="Riders at Dusk", description="A handful of riders come in with the dusk.",
                          event_type="micro", scheduled_day=2, scheduled_hour=18, player_visible=True))
        session.commit()
    lost = next(line for line in _brief(world).splitlines() if line.startswith("People the story has lost"))
    wat = lost.split("Wat Furrow [")[1].split("; ")[0]
    assert "never the people of" not in wat
    assert 'never the people of "The Greywater Cart Arrives Short"' in lost   # Tobin's still is


# ----------------------------------------------------------------------
# The next event's hour, near it
# ----------------------------------------------------------------------

def test_fleet8_tamsin_t9_the_howl_a_quarter_hour_off_is_said_so(world):
    text = _brief(world)
    assert ("Coming up: The Evening Howl at 20:00 — 15 minutes from now (any minute now; anyone who speaks of "
            "it knows how close it is). It has not happened yet") in text
    assert "write @time 15m" in text
    assert "Greywater Cart Arrives Short at" not in text           # tomorrow, and not near


def test_an_event_just_past_midnight_is_near_too(world):
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.hour, clock.minute = 23, 30
        session.add(Event(name="The Midnight Bell", description="A bell.", event_type="micro",
                          scheduled_day=2, scheduled_hour=0, player_visible=True))
        session.commit()
    text = _brief(world)
    assert "Coming up: The Midnight Bell at 00:00 — 30 minutes from now (well inside the hour" in text
    assert "Coming up: The Evening Howl" not in text               # its hour has passed


# ----------------------------------------------------------------------
# Name cuts that leave a whole line
# ----------------------------------------------------------------------

@pytest.mark.parametrize("before, after", [
    # fleet8-wenna T18 showed "And — was it? Come back after, if you can."
    ("Go on, then. Tell him everything. And — Wenna, was it? Come back after, if you can.",
     "Go on, then. Tell him everything. Come back after, if you can."),
    ("Hey — Wenna, right?", "Hey."),
    ("Wenna, right? Good. Sit.", "Good. Sit."),
    ("Name's Sorrel. And you're — Wenna, yes?", "Name's Sorrel."),
    ("Ah — Wenna, is it? Welcome.", "Welcome."),
    ("Well, Wenna, was it? Sit down.", "Sit down."),
    ("You did it, Wenna, right?", "You did it, right?"),
    ("So — Wenna Cole. The courier everyone's on about.", "The courier everyone's on about."),
    ("Wenna, was it?", "…"),
    # the line goes on past the question; the name set in emphasis
    ("Listen — Wenna, right? — the wolves are close.", "The wolves are close."),
    ("*Wenna*, was it? Sit.", "Sit."),
    # the whole name owning something, and a verb opening the sentence: the
    # cut left "Wenna your satchel" and "Find Wenna you, then"
    ("That's Wenna Cole's satchel.", "That's your satchel."),
    ("Find Wenna Cole, then.", "Find you, then."),
    # what read whole before still does
    ("So. You came back, Wenna.", "So. You came back."),
    ("Listen, Wenna — the wolves are close.", "Listen — the wolves are close."),
    ("You're Wenna, aren't you?", "You're the one, aren't you?"),
])
def test_a_cut_name_takes_its_question_and_lead_in_along(before, after):
    out, cut = cut_player_name(before, "Wenna Cole")
    assert out == after and cut


def test_a_dossier_file_says_you_where_they_never_heard_the_name(world):
    """fleet8-wenna: Sorrel's file read "— Wenna: …" and "Wenna came to the
    yard"; he said "Wenna" in six replies and the engine cut it six times."""
    memory_files.append_exchange(world["player"], world["sorrel"], "I'm here about the howls.",
                                 ["Hey. Ask, then."], "Day 1 19:00")
    memory_files.add_npc_note(world["player"], world["sorrel"], "Wenna came to the yard after dark.")
    with get_session() as session:
        session.add(NPCRelationship(npc_id=world["sorrel"], player_id=world["player"], knows_player_name=False))
        session.commit()
    _move(world, "yard")
    text = _brief(world, addressed=world["sorrel"])
    assert "Knows the player's name: no" in text
    assert "they never heard your name, so it says 'you'" in text
    assert 'Day 1 19:00 · You: "I\'m here about the howls."' in text
    assert "You came to the yard after dark." in text
    assert "Wenna" not in text.split("THE PLAYER IS TALKING TO")[0].split("Sorrel Tack [")[1]
    # Someone who knows it keeps the file as written
    with get_session() as session:
        rel = session.query(NPCRelationship).filter_by(npc_id=world["sorrel"]).one()
        rel.knows_player_name = True
        session.commit()
    assert 'Day 1 19:00 · Wenna: "I\'m here about the howls."' in _brief(world, addressed=world["sorrel"])


def test_without_player_name_reads_you():
    assert without_player_name("- Day 1 19:00 · Wenna: \"Hi.\"", "Wenna Cole") == "- Day 1 19:00 · You: \"Hi.\""
    assert without_player_name("Asked about Wenna Cole's satchel; trusts Wenna.", "Wenna Cole") == \
        "Asked about your satchel; trusts you."
    assert without_player_name("Coleman's cart", "Wenna Cole") == "Coleman's cart"
    # A word of the name inside someone else's is theirs, as the cut reads it
    assert without_player_name("Osric Cole's cart; Wenna asked.", "Wenna Cole") == "Osric Cole's cart; you asked."
    assert without_player_name("Rode with the Grey Company; Tam Grey paid.", "Tam Grey") == \
        "Rode with the Grey Company; you paid."
    assert without_player_name('Sorrel Tack: "Wenna\'s here."', "Wenna Cole") == 'Sorrel Tack: "You\'re here."'


# ----------------------------------------------------------------------
# What the others already told
# ----------------------------------------------------------------------

def test_fleet8_tamsin_what_piper_told_is_in_front_of_the_dm_at_the_yard(world):
    memory_files.append_exchange(world["player"], world["piper"], "What's wrong with the howls?",
                                 ["Three nights running, same gaps between the calls, almost to the minute."],
                                 "Day 1 18:45")
    memory_files.append_exchange(world["player"], world["piper"], "", ["Fine."], "Day 1 18:50")
    memory_files.add_journal_entry(world["player"], "Piper: the howls keep a schedule.")
    _move(world, "yard")
    told = next(line for line in _brief(world).splitlines() if line.startswith("Already told to the player"))
    assert ('Piper Quillow (day 1, 18:45): "Three nights running, same gaps between the calls, almost to the '
            'minute."') in told
    assert "Fine." not in told                                     # no fact in it
    assert 'their journal: "Piper: the howls keep a schedule."' in told
    assert "nobody here tells it again as news" in told
    # Her own lines are in her own dossier: at her side, they are not repeated here
    _move(world, "square")
    assert "Piper Quillow (day 1" not in "".join(
        line for line in _brief(world).splitlines() if line.startswith("Already told"))


# ----------------------------------------------------------------------
# The reward is handed over finished
# ----------------------------------------------------------------------

def test_fleet8_wenna_t0_the_reward_is_no_trophy_to_fetch():
    from src.services import opening
    from src.tools.world_write.quests import reward_words

    words = reward_words({"currency": 60, "items": [{"name": "Wolf-Tooth Charm", "kind": "trinket",
                                                     "about": "A wolf's fang bound in brass wire."}]})
    assert words == ('60 coin and a Wolf-Tooth Charm (say "a Wolf-Tooth Charm"; handed over finished, the '
                     "player's to keep — a wolf's fang bound in brass wire)")
    assert "each item by its own name, as written" in opening._OFFER_AUTHORED
    assert "never a trophy" in opening._OFFER_AUTHORED


def test_a_reload_never_shows_the_planning_the_stream_dropped(world):
    """fleet8-tamsin T13: the stored reply is the DM's raw text; the chat
    history rebuilt from it must drop what the live filter dropped."""
    from src.web.server import _reply_parts

    reply = ("This reply needs to correct that and actually send them through the gate, matching their "
             "stated intent to move north.\n"
             "The gate groans open onto the dark road.\n"
             "@move North Gatehouse\n"
             "I shouldn't have made that second call. Frost crusts the ruts.")
    with get_session():
        parts = _reply_parts(reply, {}, None, "Tamsin Rook")
    assert [(p["kind"], p["text"]) for p in parts] == [
        ("text", "The gate groans open onto the dark road.\nFrost crusts the ruts."),
    ]

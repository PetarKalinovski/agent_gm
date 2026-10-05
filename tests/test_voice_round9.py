"""Voice and placement, round 8 playtests -> round 9.

- fleet9-tamsin T7: "She falls into step beside you" (only "falls in step"
  was read) and "…catches sight of the two of you crossing the yard" (the
  watcher's "he" filtered out the "she" who came) — Piper spoke in the
  yard and stayed in the square for three turns.
- fleet9-bram T12: Sorrel "ducks back off toward the Stable Yard at a trot"
  and stood in the square all evening in the database.
- fleet9-bram T10: the Evening Howl "cleared the stalls — shutters down",
  and the database kept the hawker, the herbalist and a drover there.
- fleet9-bram T12/T14: "…Sorrel's warning is filed away…", "Whatever's next
  — … — is his to choose."
- fleet9-tamsin T11: "She catches herself using your name before
  remembering she was never told it" — the line had lost the name.
- fleet9-tamsin: Piper's basket in 16 of 21 replies, "basket forgotten …
  her hip" twice in four turns; a bolded "**What do you do?**"; a 76-word
  Captain line, a 73-word pitch.
"""

import asyncio
import json

import pytest

from src.models import NPC, Event, Location, Player, Quest, QuestStatus, WorldClock, get_session, reset_engine
from src.models.campaign import Campaign, StoryBeat
from src.services import job_offers, voice_tics, world_tick
from src.web.dialogue_markup import (
    DialogueMarkupFilter, MetaSentenceFilter, SPLIT_SPEECH_WORDS, is_meta_sentence, parse_dialogue_line,
    split_long_speech,
)
from src.web.turn_filters import JunkLineFilter

TAMSIN_T7 = (
    "She falls into step beside you, basket thumping her hip, and doesn't slow down to answer — just talks "
    "faster, like the walking and the words are the same effort.\n\n"
    "The lane opens into the Stable Yard, straw and horse-sweat thick in the cooling air, lantern light spilling "
    "warm from the open barn doors. A young man is forking hay into a stall, sleeves rolled despite the chill, "
    "pausing when he catches sight of the two of you crossing the yard toward him.")
BRAM_T12 = (
    "Sorrel straightens at the clap on his shoulder, color coming back into his face now that someone's taken "
    "him seriously twice in one night.\n\nHe nods once, decisive, and ducks back off toward the Stable Yard at a "
    "trot, already glancing over his shoulder at the dark beyond the fence.")
BRAM_T12_RECAP = ("The pass is in his coat, Sorrel's warning is filed away, and the square around him is still "
                  "worth a look before he calls the night done.")
BRAM_T14_MENU = ("Whatever's next — another word with the Captain, a look at the Drowned Lantern's lamplight down "
                 "the road, or simply calling the night done — is his to choose.")
BRAM7_T17_MENU = ("Dawn and the open gate are still hours off — the night is yours to spend as you choose, whether "
                  "that's resting up before the hollow, or seeing what else this square still has to offer before "
                  "you go.")
TAMSIN_T11_SLIP = ("She catches herself using your name before remembering she was never told it, flustered even "
                   "mid-fright.")


@pytest.fixture()
def emberfall(tmp_path):
    from src.data.playtest_seed import create_playtest_world

    reset_engine()
    create_playtest_world(str(tmp_path / "emberfall.db"))
    job_offers.reset()
    voice_tics.reset()
    with get_session() as session:
        places = {loc.name: loc.id for loc in session.query(Location).all()}
        people = {npc.name: npc.id for npc in session.query(NPC).all()}
        quests = {q.title: q.id for q in session.query(Quest).all()}
        player = Player(name="Tamsin Rook", current_location_id=places["Market Square"], currency=25)
        session.add(player)
        session.commit()
        ids = {"player": player.id, **places, **people, **quests}
    yield ids
    job_offers.reset()
    voice_tics.reset()
    reset_engine()


def _put(ids, where, who="player"):
    with get_session() as session:
        row = session.get(Player, ids["player"]) if who == "player" else session.get(NPC, ids[who])
        row.current_location_id = ids[where]
        session.commit()


def _where(ids, who="player"):
    with get_session() as session:
        row = session.get(Player, ids["player"]) if who == "player" else session.get(NPC, ids[who])
        return session.get(Location, row.current_location_id).name


# ---------------------------------------------------------------------------
# 1. Who walks along goes along (fleet9-tamsin T7)
# ---------------------------------------------------------------------------

def _walked(ids, prose, spoken=None, *, frm="Market Square", to="Stable Yard"):
    from src.web.server import _walked_along

    _put(ids, to)
    return _walked_along(ids["player"], prose, ids[frm], spoken)


def test_she_falls_into_step_and_the_two_of_you_cross_the_yard(emberfall):
    """T7 as it ran: the prose never names her — the one from the square who
    spoke in the reply is the "she"."""
    piper = [{"npc_id": emberfall["Piper Quillow"], "text": "Piper. Piper Quillow.", "_where": emberfall["Stable Yard"],
              "_at": 0}]
    lines = _walked(emberfall, TAMSIN_T7, piper)
    assert lines == ["(engine) Piper Quillow walked along with the player to Stable Yard in your story, so the "
                     "engine moved them there too. If they turned back, write @npc-move Piper Quillow | Market "
                     "Square."]
    assert _where(emberfall, "Piper Quillow") == "Stable Yard"
    assert _where(emberfall, "Captain Brann Coldiron") == "Market Square"


@pytest.mark.parametrize("prose", [
    "Piper falls into step beside you without a word.",
    # the watcher's "he" (the stable hand) never filters out the "she" who came
    "Piper chatters on. A young man looks up when he catches sight of the two of you crossing the yard.",
    "Piper keeps pace at your shoulder, murmuring numbers.",
    "Piper hovers a step behind you, nerve flagging.",
    "Piper hangs back at first, then follows a few steps after.",
])
def test_the_ways_of_going_along(emberfall, prose):
    assert _walked(emberfall, prose)
    assert _where(emberfall, "Piper Quillow") == "Stable Yard"


@pytest.mark.parametrize("prose", [
    "Piper watches the two of you head off, arms folded.",
    "Piper stays by her stall, her eyes following you across the square.",
    "Her gaze follows you to the lane's mouth.",
    "She falls into step beside you.",                  # a "she" nobody named, and nobody spoke
])
def test_who_stays_stays(emberfall, prose):
    assert _walked(emberfall, prose) == []
    assert _where(emberfall, "Piper Quillow") == "Market Square"


def test_a_voice_from_the_square_after_the_yard_is_shown_came_along(emberfall):
    arrived = "The lane opens into the Stable Yard, straw and lantern light."
    line = {"npc_id": emberfall["Piper Quillow"], "text": "That's Sorrel.", "_where": emberfall["Stable Yard"]}
    assert _walked(emberfall, arrived, [{**line, "_at": len(arrived)}])
    assert _where(emberfall, "Piper Quillow") == "Stable Yard"


def test_a_goodbye_said_before_the_new_place_is_shown_stays_behind(emberfall):
    """fleet5-bram T7: "You be careful out there" came after the @move,
    before the prose reached the gate."""
    prose = "Piper waves, already turning back to her stall. The lane opens into the Stable Yard."
    line = {"npc_id": emberfall["Piper Quillow"], "text": "Be careful!", "_where": emberfall["Stable Yard"]}
    assert _walked(emberfall, prose, [{**line, "_at": 0}]) == []
    # ...and one the prose left behind is never carried by a later line
    prose = "Piper waves you off. The lane opens into the Stable Yard."
    assert _walked(emberfall, prose, [{**line, "_at": len(prose)}]) == []
    assert _where(emberfall, "Piper Quillow") == "Market Square"


def test_the_two_of_you_or_an_unnamed_she_never_takes_a_goodbye_along(emberfall):
    """Review: a farewell from the square, then "the two of you" (a
    companion, or someone met on the way) or a "she" nobody named — the one
    who said goodbye before it was no "she" walking along."""
    bye = [{"npc_id": emberfall["Piper Quillow"], "text": "Mind the dark out there.",
            "_where": emberfall["Stable Yard"], "_at": 0}]
    for prose in ("You leave the stalls behind.\nThe lane narrows, and the two of you cross it together.",
                  "You leave the stalls behind.\nShe falls into step beside you as the lane narrows."):
        assert _walked(emberfall, prose, bye) == []
    assert _where(emberfall, "Piper Quillow") == "Market Square"


@pytest.mark.parametrize("prose, action", [
    ("The lane opens into the Stable Yard, straw and lantern light.", "calling after you from the square"),
    ("The lane opens into the Stable Yard. Behind you, from the square, a voice carries.", None),
    ("The lane opens into the Stable Yard. Piper shouts after you, cupping her hands.", None),
])
def test_a_line_called_after_the_player_is_a_goodbye(emberfall, prose, action):
    """Review: voiced once the yard is shown, but called after the player
    from the square — she stayed."""
    line = {"npc_id": emberfall["Piper Quillow"], "text": "Mind the dark!", "_where": emberfall["Stable Yard"],
            "_at": len(prose), "action": action}
    assert _walked(emberfall, prose, [line]) == []
    assert _where(emberfall, "Piper Quillow") == "Market Square"


@pytest.mark.parametrize("prose", [
    "Piper follows you with her eyes as you go.",
    "Piper says she'll walk you to the yard tomorrow.",
    "Piper offers to walk you over, but not tonight.",
])
def test_a_look_or_a_walk_still_to_come_takes_nobody(emberfall, prose):
    assert _walked(emberfall, prose) == []
    assert _where(emberfall, "Piper Quillow") == "Market Square"


# ---------------------------------------------------------------------------
# 2. Who leaves goes (fleet9-bram T12)
# ---------------------------------------------------------------------------

def _off(ids, prose, spoken=None):
    from src.web.server import _walked_off

    return _walked_off(ids["player"], prose, spoken)


def test_sorrel_ducks_off_to_the_stable_yard_and_goes(emberfall):
    _put(emberfall, "Market Square", "Sorrel Tack")
    lines = _off(emberfall, BRAM_T12)
    assert lines == ["(engine) Sorrel Tack left for Stable Yard in your story, so the engine moved them there. "
                     "If they stayed, write @npc-move Sorrel Tack | Market Square."]
    assert _where(emberfall, "Sorrel Tack") == "Stable Yard"


@pytest.mark.parametrize("prose", [
    "Sorrel watches as you head for the Stable Yard.",
    "Sorrel says he'll head back to the Stable Yard before the howl.",
    "Sorrel doesn't go to the Stable Yard; he stays by the well.",
    "Sorrel's eyes go to the Stable Yard.",
    "Sorrel heads for the trough, then glances toward the Stable Yard.",
    "Sorrel walks you to the Stable Yard.",
    "Sorrel heads for the Stable Yard, and you follow.",
    "Sorrel hurries toward the lantern light by the well.",
])
def test_no_leaving_is_no_move(emberfall, prose):
    _put(emberfall, "Market Square", "Sorrel Tack")
    assert _off(emberfall, prose) == []
    assert _where(emberfall, "Sorrel Tack") == "Market Square"


@pytest.mark.parametrize("prose", [
    "Sorrel ducks into the Stable Yard and comes back a minute later with a lantern.",
    "Sorrel jogs off toward the Stable Yard. A minute later he's back, out of breath.",
    "Sorrel, who went to the Stable Yard an hour ago, leans on the well now.",
    "Sorrel went to the Stable Yard earlier, Piper says.",
    "Sorrel hurries to the Stable Yard with you at his heels.",       # the player goes too
])
def test_a_fetch_or_an_errand_told_of_leaves_them_here(emberfall, prose):
    """Review: an errand there and back, or a going told of the past — he
    stands here now."""
    _put(emberfall, "Market Square", "Sorrel Tack")
    assert _off(emberfall, prose) == []
    assert _where(emberfall, "Sorrel Tack") == "Market Square"


def test_standing_beside_you_and_then_leaving_still_goes(emberfall):
    _put(emberfall, "Market Square", "Sorrel Tack")
    assert _off(emberfall, "Sorrel, standing beside you, ducks back toward the Stable Yard.")
    assert _where(emberfall, "Sorrel Tack") == "Stable Yard"


def test_back_to_the_square_in_lower_case_is_the_square(emberfall):
    _put(emberfall, "Stable Yard")
    _put(emberfall, "Stable Yard", "Piper Quillow")
    assert _off(emberfall, "Piper nods fast and hurries back to the square, basket swinging.")
    assert _where(emberfall, "Piper Quillow") == "Market Square"


def test_a_companion_the_villain_and_a_beats_people_are_kept(emberfall):
    _put(emberfall, "Market Square", "Sorrel Tack")
    with get_session() as session:
        session.get(Player, emberfall["player"]).party_members = [emberfall["Sorrel Tack"]]
        session.commit()
    assert _off(emberfall, BRAM_T12) == []                     # the party is the player's
    with get_session() as session:
        session.get(Player, emberfall["player"]).party_members = []
        campaign = Campaign(player_id=emberfall["player"], status="active",
                            villain_npc_id=emberfall["Captain Brann Coldiron"])
        session.add(campaign)
        session.flush()
        # a beat due now that needs Sorrel where he stands
        session.add(StoryBeat(campaign_id=campaign.id, act=1, sequence=1, title="The Man at the Wall",
                              directive="Sorrel Tack tells the player what he saw at the north wall.",
                              status="fired"))
        session.commit()
    assert _off(emberfall, BRAM_T12) == []
    assert _off(emberfall, "The Captain heads back to the North Gatehouse.") == []
    assert _where(emberfall, "Sorrel Tack") == "Market Square"
    assert _where(emberfall, "Captain Brann Coldiron") == "Market Square"


# ---------------------------------------------------------------------------
# 3. A place the world cleared is empty (fleet9-bram T10)
# ---------------------------------------------------------------------------

def _tick(ids, day, hour):
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.day, clock.hour, clock.minute = day, hour, 0
        session.commit()
    return world_tick.run_world_tick(ids["player"])


def test_the_howl_sends_the_squares_everyday_people_in_and_first_light_brings_them_back(emberfall):
    _put(emberfall, "Market Square", "Sorrel Tack")
    result = _tick(emberfall, 1, 20)
    gone = {m["name"]: m["to"] for m in result["cleared"]}
    assert gone == {"Hesk Bramble": "The Drowned Lantern", "Piper Quillow": "The Drowned Lantern",
                    "Sorrel Tack": "Stable Yard"}                            # home, not the inn
    assert _where(emberfall, "Hesk Bramble") == "The Drowned Lantern"
    assert _where(emberfall, "Sorrel Tack") == "Stable Yard"
    assert _where(emberfall, "Captain Brann Coldiron") == "Market Square"    # the Watch holds the square
    text = world_tick.format_world_tick_context(result)
    assert "GONE IN FOR THE NIGHT" in text and "Hesk Bramble: Market Square -> The Drowned Lantern" in text
    # once: a later tick that night moves no one again
    _put(emberfall, "Market Square", "Hesk Bramble")
    assert _tick(emberfall, 1, 21)["cleared"] == []
    _put(emberfall, "The Drowned Lantern", "Hesk Bramble")
    morning = _tick(emberfall, 2, 7)
    assert {m["name"] for m in morning["returned"]} == {"Hesk Bramble", "Piper Quillow"}
    assert _where(emberfall, "Hesk Bramble") == "Market Square"
    assert _where(emberfall, "Sorrel Tack") == "Stable Yard"                 # he was home already
    assert "BACK AT FIRST LIGHT" in world_tick.format_world_tick_context(morning)


@pytest.mark.parametrize("player_at, told", [("Market Square", True), ("Stable Yard", False)])
def test_gone_in_where_the_player_stands_is_shown_going(emberfall, player_at, told):
    """Review: the brief was written before the tick and still lists them;
    with the player in the square the line asks the reply to show them go."""
    _put(emberfall, player_at)
    text = world_tick.format_world_tick_context(_tick(emberfall, 1, 20))
    assert "Hesk Bramble: Market Square -> The Drowned Lantern (The Evening Howl)" in text
    assert ("the player is there: show them hurrying off in this reply" in text) is told


def test_the_people_a_job_points_at_wait_where_they_are(emberfall):
    """Piper's job is running: she waits to be paid, and Sorrel, whom its
    first step names, waits to be asked."""
    _put(emberfall, "Market Square", "Sorrel Tack")
    with get_session() as session:
        session.get(Quest, emberfall["Counting the Howls"]).status = QuestStatus.ACTIVE
        session.commit()
    gone = {m["name"] for m in _tick(emberfall, 1, 20)["cleared"]}
    assert gone == {"Hesk Bramble"}
    assert _where(emberfall, "Piper Quillow") == "Market Square"
    assert _where(emberfall, "Sorrel Tack") == "Market Square"


def test_someone_the_story_moved_since_stays_where_it_put_them(emberfall):
    _tick(emberfall, 1, 20)
    _put(emberfall, "Stable Yard", "Hesk Bramble")
    assert "Hesk Bramble" not in {m["name"] for m in _tick(emberfall, 2, 7)["returned"]}
    assert _where(emberfall, "Hesk Bramble") == "Stable Yard"


@pytest.mark.parametrize("text, clears", [
    ("Townsfolk clear the square", True),
    ("The North Gate is barred behind it and the square empties fast.", True),
    ("The market square stands empty under the lanterns.", True),
    ("The road to the square is clear again", False),
    ("North Gate barred for the night", False),
    ("Flour and lamp oil ration talk begins in the square", False),
])
def test_what_clears_a_place(emberfall, text, clears):
    with get_session() as session:
        square = session.get(Location, emberfall["Market Square"])
        event = Event(name="Test", description=text, consequences=[], locations_involved=[square.id])
        assert world_tick._cleared_by(event, square) is clears


# ---------------------------------------------------------------------------
# 4. Bookkeeping recaps and a slip nobody saw
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sentence", [BRAM_T12_RECAP, BRAM_T14_MENU, BRAM7_T17_MENU])
def test_a_recap_or_a_menu_is_bookkeeping(sentence):
    assert is_meta_sentence(sentence)


@pytest.mark.parametrize("sentence", [
    "He nods toward the dark beyond the lantern posts, like the rest is up to you.",
    "The old rosters are filed away in a drawer behind the bar.",
    "The choice of road is yours to make at first light.",
    "She calls the night watch over with two fingers.",
])
def test_the_fiction_near_them_stays(sentence):
    assert not is_meta_sentence(sentence)


def test_a_menu_naming_the_player_is_dropped_never_turned_into_you():
    meta = MetaSentenceFilter(player_name="Bram Holloway")
    out = meta.feed("Whatever's next — the Captain or the tavern — is Bram's to choose.\nThe square is quiet.\n")
    out += meta.flush()
    assert "Whatever" not in out and "The square is quiet." in out
    assert meta.rewritten == [] and len(meta.dropped) == 1


def test_a_menu_in_the_players_possessive_is_dropped_never_turned_into_you():
    """Review: "Bram's to decide" came out "your to decide" — still the menu."""
    meta = MetaSentenceFilter(player_name="Bram Holloway")
    out = meta.feed("What comes next is Bram's to decide.\nThe night is Bram's to spend as he chooses.\n"
                    "Bram reads the notice twice.\n") + meta.flush()
    assert "decide" not in out and "spend" not in out and "You read the notice twice." in out
    assert len(meta.dropped) == 2 and meta.rewritten == ["Bram reads the notice twice."]


def test_the_slip_told_after_a_cut_goes_with_the_name(emberfall):
    from src.services.dialogue import NameGate

    gate = NameGate.for_turn(emberfall["player"], "I put myself between the wolf and her.",
                             present_ids=[emberfall["Piper Quillow"]])
    assert not gate.comments_on_cut_name(TAMSIN_T11_SLIP), "no cut, nothing to retract"
    assert gate.player_name_in(emberfall["Piper Quillow"], "Wait, Tamsin — that one's alone.",
                               "Herbalist's Apprentice") == "Wait — that one's alone."
    meta = MetaSentenceFilter(player_name="Tamsin Rook", also_drop=gate.comments_on_cut_name)
    out = meta.feed(TAMSIN_T11_SLIP + " The wolf circles the trough.\n") + meta.flush()
    assert "catches herself" not in out and "The wolf circles the trough." in out
    assert meta.retracted == [TAMSIN_T11_SLIP]
    assert not gate.comments_on_cut_name("She never asks your name.")


def test_a_reload_retracts_the_slip_only_in_the_reply_that_cut_the_name(emberfall):
    """Review: the chat history replays the stored reply, slip and all; one
    gate reads every turn, so only a cut in THIS reply retracts its prose."""
    from src.services.dialogue import NameGate
    from src.web.server import _reply_parts

    gate = NameGate.for_turn(emberfall["player"], "", context="")
    earlier = "Easy pickings in a town that's never heard your name.\nThe square hums."
    cut = 'Piper Quillow: "Wait, Tamsin — that one\'s alone."\n' + TAMSIN_T11_SLIP + "\nThe wolf circles the trough."
    later = "The Lantern is a room that's never heard your name.\nThe lantern gutters."
    with get_session():
        shown = ["\n".join(p.get("text") or "" for p in _reply_parts(reply, {}, gate, "Tamsin Rook"))
                 for reply in (earlier, cut, later)]
    assert "never heard your name" in shown[0]
    assert "Tamsin" not in shown[1] and "catches herself" not in shown[1] and "circles the trough" in shown[1]
    assert "never heard your name" in shown[2]          # the cut was a reply ago


# ---------------------------------------------------------------------------
# 5. Tics, the game-show prompt, and lines too long for a bubble
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line, dropped", [
    ("**What do you do?**", True),
    ("*What do you do now?*", True),
    ("What do you do?", True),
    ("Your move.", True),
    ("What do you do with a wolf like that, you wonder, and the answer is nothing good.", False),
])
def test_a_line_that_only_asks_for_the_move_is_dropped(line, dropped):
    junk = JunkLineFilter()
    out = junk.feed(f"The wolf waits.\n\n{line}\n") + junk.flush()
    assert ("do you do" in out or "move" in out) is not dropped
    assert "The wolf waits." in out
    assert junk.dropped == []           # no tool echo: tool_result_in_prose stays quiet
    assert bool(junk.prompts) is dropped


CAPTAIN_76 = (
    "You're not from here. Good eyes, though — I can always tell. Most folk don't look at a door and wonder "
    "what's behind it. There's a notice on that board says the Watch pays for true accounts of the howls, and "
    "I mean to pay it. Sixty coin and a Wolf-Tooth Charm for whoever walks the north road to Wolf's Hollow and "
    "comes back with what's doing this. Capt. B. Coldiron signs for it. Think on it, and come find me at the "
    "gate before the howl.")


def test_a_long_line_goes_out_in_two_at_a_sentence_end():
    parsed = parse_dialogue_line(f'Watch Captain (gruff, arms folded): "{CAPTAIN_76}" He turns back to the road.')
    assert len(parsed["text"].split()) > SPLIT_SPEECH_WORDS
    parts = split_long_speech(parsed)
    assert len(parts) == 2
    assert all(len(p["text"].split()) <= SPLIT_SPEECH_WORDS for p in parts)
    assert " ".join(p["text"] for p in parts) == parsed["text"]
    assert parts[0]["action"] == "arms folded" and parts[1]["action"] is None
    assert parts[1]["continues"] and not parts[0].get("continues")
    assert all(not p["text"].endswith("Capt.") and not p["text"].endswith("B.") for p in parts)
    # through the stream: two speech segments, the narration after the quote once
    markup = DialogueMarkupFilter()
    segments = markup.feed_segments(f'Watch Captain (gruff): "{CAPTAIN_76}" He turns back to the road.\n')
    assert [k for k, _ in segments] == ["speech", "speech", "text"]
    assert segments[2][1].strip() == "He turns back to the road."


def test_a_short_line_or_one_with_no_sentence_end_is_left_whole():
    short = parse_dialogue_line('Piper: "Ten coin and a tincture."')
    assert split_long_speech(short) == [short]
    run_on = parse_dialogue_line('Piper: "' + " ".join(["and the howls go on"] * 16) + '"')
    assert split_long_speech(run_on) == [run_on]


def test_the_audit_still_weighs_the_line_the_dm_wrote():
    from src.web.turn_audit import _too_talky

    parts = split_long_speech(parse_dialogue_line(f'Watch Captain: "{CAPTAIN_76}"'))
    spoken = [{"text": p["text"], "continues": p.get("continues")} for p in parts]
    warning = _too_talky(spoken)
    assert warning and "1 spoken line(s)" in warning


def test_the_continuation_rides_the_speech_event(emberfall):
    from src.web.server import _speech_from_markup

    parts = split_long_speech(parse_dialogue_line(f'Watch Captain: "{CAPTAIN_76}"'))
    notes = [_speech_from_markup(p, [emberfall["Captain Brann Coldiron"]], emberfall["player"]) for p in parts]
    assert notes[1]["continues"] is True and "continues" not in notes[0]


def test_a_name_said_in_the_second_half_shows_on_both_bubbles(emberfall):
    """Review: the gate hears the whole line first, as it did unsplit — never
    the role label on one bubble and her name on the next."""
    from src.services.dialogue import NameGate
    from src.web.server import _speech_from_markup

    pitch = ("Sixty coin, the Captain says, and I believe him, because he never says a number he can't pay. I've "
             "counted every howl since the carter came back dead on his own wagon. Same hour, every night, and "
             "something under it like a whistle nobody else will admit to hearing. Nobody listens to an apprentice "
             "who smells of yarrow. But you look like someone who might. Come find me at the stall before the bell. "
             "I'm Piper, by the way. Piper Quillow.")
    parts = split_long_speech(parse_dialogue_line(f'Herbalist\'s Apprentice (eager): "{pitch}"'))
    assert len(parts) == 2 and "Piper" not in parts[0]["text"]
    gate = NameGate.for_turn(emberfall["player"], "Who are you?", present_ids=[emberfall["Piper Quillow"]])
    shown = [_speech_from_markup(p, [emberfall["Piper Quillow"]], emberfall["player"], names=gate)["npc_name"]
             for p in parts]
    assert shown[0] == shown[1] and "Piper" in shown[0]


PIPER = "pid"


def _person():
    return voice_tics.Person(PIPER, "Piper Quillow", __import__("re").compile(r"\b(?:Piper|Quillow|Apprentice)\b"),
                             "she", {"basket", "sweetroot", "yarrow", "fingertip", "chapped", "stained"},
                             "Herbalist's Apprentice")


def test_the_basket_and_the_forgotten_basket_are_named_and_the_barn_door_is_not():
    """fleet9-tamsin T11/T14: "basket forgotten on/against her hip"."""
    replies = [
        "Piper shifts her basket to the other hip, leaning in. The barn door creaks.",
        "Piper's eyes are fixed on you, waiting, basket forgotten against her hip. The barn door bangs.",
        "Piper nods, her basket bouncing. Somewhere a barn door slams.",
        "Piper edges closer, basket forgotten against her hip, peering at the dead animal.",
    ]
    for prose in replies:
        voice_tics.note("p1", prose, [{"npc_id": PIPER, "text": "Ten coin and a tincture.", "_at": 0}], [_person()])
    found = voice_tics.tics("p1", PIPER)
    assert found[0] == '"basket forgotten against her hip"'
    assert not any("barn" in f or "coin" in f for f in found)
    line = voice_tics.brief_lines("p1", [type("N", (), {"id": PIPER, "name": "Piper Quillow"})])
    assert line and line[0].startswith("Already used — vary:") and "Piper Quillow:" in line[0]


def test_nothing_repeated_is_no_line():
    voice_tics.note("p2", "Piper laughs at that.", [], [_person()])
    voice_tics.note("p2", "Piper counts on her fingers.", [], [_person()])
    assert voice_tics.brief_lines("p2", [type("N", (), {"id": PIPER, "name": "Piper Quillow"})]) == []


def test_the_brief_says_it_for_the_people_here(emberfall):
    from src.services.scene_brief import build_scene_brief

    for prose in ("Piper hugs her basket tighter.", "Piper's basket bumps her hip as she turns.",
                  "Piper shifts the basket, sweetroot stem between her teeth.",
                  "Piper sets her basket down at last."):
        voice_tics.note_reply(emberfall["player"], prose, [])
    with get_session() as session:
        brief = build_scene_brief(session, session.get(Player, emberfall["player"]))["text"]
    assert "Already used — vary:" in brief and "Piper Quillow: basket (4 of the last 4 replies)" in brief


# ---------------------------------------------------------------------------
# Through /api/play: the walk along and the walk off land in the next ledger
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
        return f"THE SCENE\nPlayer says: {player_input}"


def _play(ids, monkeypatch, reply, said):
    from src.services import memory_files
    from src.web import server
    from src.web.streaming import ToolUsageTracker

    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    dm = _FakeDM([{"data": reply}], ids["player"])
    monkeypatch.setattr(server, "get_or_create_session", lambda pid: {"dm": dm, "tool_tracker": ToolUsageTracker()})
    monkeypatch.setattr(server, "_engine_ambush", lambda *a, **k: None)
    monkeypatch.setattr(server, "maybe_advance_world", lambda: None)
    server._pending_command_ledger.pop(ids["player"], None)

    async def run():
        response = await server.play_sse(server.GameRequest(player_input=said, player_id=ids["player"]))
        return [chunk async for chunk in response.body_iterator]

    frames = []
    for chunk in asyncio.run(run()):
        for line in str(chunk).splitlines():
            if line.startswith("data: ") and line[6:].strip() not in ("", "{}"):
                frames.append(json.loads(line[6:]))
    return next(f for f in frames if f.get("type") == "complete"), server._pending_command_ledger.pop(ids["player"],
                                                                                                    [])


def test_the_stream_walks_her_along_and_sends_him_off(emberfall, monkeypatch):
    reply = ("@move Stable Yard\n" + TAMSIN_T7.split("\n\n")[0] + "\n\n"
             'Piper Quillow (breathless): "Piper. Piper Quillow."\n\n' + TAMSIN_T7.split("\n\n")[1] + "\n\n"
             'Piper Quillow (urgent): "That\'s Sorrel. Ask him yourself."\n')
    complete, ledger = _play(emberfall, monkeypatch, reply, "I follow her toward the stables.")
    assert _where(emberfall) == "Stable Yard"
    assert _where(emberfall, "Piper Quillow") == "Stable Yard"
    assert any(line.startswith("(engine) Piper Quillow walked along") for line in complete["engine_walks"])
    assert any("Piper Quillow walked along" in line for line in ledger)
    # T12: Sorrel walked back to the square with him, then trots home
    _put(emberfall, "Market Square")
    _put(emberfall, "Market Square", "Sorrel Tack")
    complete, ledger = _play(emberfall, monkeypatch, BRAM_T12 + "\n", "Get back to those horses.")
    assert _where(emberfall, "Sorrel Tack") == "Stable Yard"
    assert any("Sorrel Tack left for Stable Yard" in line for line in complete["engine_walks"])
    assert "speaker_not_here" not in [w["check"] for w in complete["audit"]]

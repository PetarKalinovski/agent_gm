"""Names, voices and the stream (fleet4 playtest critics, round 3).

  * A real name reaches the player only once it is SAID: "Wolf-Mantled Man"
    became "Aldric Vane" with nobody saying it (bram T4->T5; Piper T12,
    Maera T16, Sorrel and Aldric for tamsin, the Captain for wenna T1), and
    the prose named "Vex Marrow" while his label stayed "Ash-Cloaked Man"
    (wenna T11). `dialogue.NameGate` shows what the player has heard.
  * The DM never voices the player (bram T15: a speech bubble for "Bram
    Holloway").
  * A roll_check seam glued sentences ("tonight.Something shifts") and the
    prose before the dice assumed an outcome.
  * dialogue_without_speak read apostrophes as quotes (bram T14).
  * Invented people the world cannot track ("Garrick", "Corvin Ashe").
"""

import asyncio
import json

import pytest

from src.models import (
    Faction, Location, LocationType, NPC, NPCRelationship, Player, WorldBible, WorldClock, get_session,
)
from src.services import dialogue, memory_files
from src.services.dialogue import NameGate, heard_form, name_words, player_has_met
from src.web.dialogue_markup import DialogueMarkupFilter, is_player_label
from src.web.turn_audit import (
    audit_turn, unintroduced_name_warning, unknown_named_people, unknown_person_warning, voiced_player_warning,
)


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
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True, visited=True)
        square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True)
        session.add_all([hollow, square])
        session.flush()
        vex = NPC(name="Vex Marrow", profession="Ashen Fangs scout", faction_id=fangs.id,
                  danger_tier="dangerous", current_location_id=hollow.id,
                  description_physical="A lean, menacing man in an ash-gray cloak and a bone whistle.")
        aldric = NPC(name="Aldric Vane", profession="Ashen Fangs wolf-caller", faction_id=fangs.id,
                     danger_tier="boss", current_location_id=hollow.id,
                     description_physical="A broad, still man in a wolf-pelt mantle, ash rubbed into his beard.")
        wolf = NPC(name="Hollow Wolf", species="wolf", profession="wolf of the drilled pack",
                   danger_tier="weak", current_location_id=hollow.id,
                   description_physical="A rangy gray wolf, ribs showing.")
        brann = NPC(name="Captain Brann Coldiron", profession="Captain of the Lantern Watch",
                    current_location_id=square.id,
                    description_physical="An old, weathered veteran with a gray beard cropped short.")
        sorrel = NPC(name="Sorrel Tack", profession="Stable hand", current_location_id=square.id,
                     description_physical="A friendly young man all elbows and straw-colored hair.")
        piper = NPC(name="Piper Quillow", profession="Herbalist's apprentice", current_location_id=square.id,
                    description_physical="A small, quick girl with ink on her fingers.")
        session.add_all([vex, aldric, wolf, brann, sorrel, piper])
        player = Player(name="Tamsin Rook", current_location_id=hollow.id, currency=30)
        session.add(player)
        session.commit()
        return {"player": player.id, "hollow": hollow.id, "square": square.id, "vex": vex.id,
                "aldric": aldric.id, "wolf": wolf.id, "brann": brann.id, "sorrel": sorrel.id, "piper": piper.id}


def _gate(world, player_input="", present=("vex", "aldric", "wolf", "brann", "sorrel", "piper"), context=""):
    return NameGate.for_turn(world["player"], player_input, present_ids=[world[k] for k in present],
                             context=context)


def _speaker(gate, world, key, label, text):
    with get_session() as session:
        return gate.speaker(session.get(NPC, world[key]), label, text)


def _met(world, key):
    with get_session() as session:
        return player_has_met(session, world["player"], world[key])


def _meet(world, *keys):
    with get_session() as session:
        for key in keys:
            session.add(NPCRelationship(npc_id=world[key], player_id=world["player"]))
        session.commit()


# ----------------------------------------------------------------------
# Item 1: a label is an introduction only once the name was said
# ----------------------------------------------------------------------

def test_name_words_and_heard_forms():
    assert name_words("Captain Brann Coldiron") == ["Brann", "Coldiron"]
    assert name_words('Jax "The Shadow" Vane') == ["Jax", "Vane"]
    assert name_words("Hilda the Guard") == ["Hilda"]
    assert heard_form("Vex Marrow", {"Vex"}) == "Vex"
    assert heard_form("Captain Brann Coldiron", {"Coldiron"}) == "Captain Coldiron"
    assert heard_form("Vex Marrow", {"Vex", "Marrow"}) == "Vex Marrow"
    assert heard_form("Vex Marrow", set()) == "Vex Marrow"


def test_fleet4_bram_t5_a_label_flip_nobody_said_stays_the_role(world):
    # The villain's label flipped to his real name; nobody had said it
    gate = _gate(world, "Name's Bram Holloway. So what's the play here?")
    verdict = _speaker(gate, world, "aldric", "Aldric Vane", "The play's simple, Holloway.")
    assert verdict == {"shown": "Wolf-Mantled Man", "label": "Wolf-Mantled Man", "introduces": False,
                       "unintroduced": "Aldric Vane"}
    assert gate.unintroduced == [("Aldric Vane", "Wolf-Mantled Man")]
    # The server passes the gate's label and verdict on: he stays a stranger
    dialogue.record_turn_dialogue(world["player"], "So what's the play?", [
        {"npc_id": world["aldric"], "npc_name": verdict["shown"], "label": verdict["label"],
         "introduces": verdict["introduces"], "text": "The play's simple, Holloway."}])
    assert not _met(world, "aldric")


def test_a_name_said_in_the_line_introduces_and_shows_only_that_part(world):
    gate = _gate(world, "Who are you?")
    verdict = _speaker(gate, world, "vex", "Vex Marrow", "Vex. Remember it.")
    assert verdict["shown"] == "Vex" and verdict["introduces"] and verdict["label"] == "Vex Marrow"
    assert gate.unintroduced == []


def test_the_players_own_words_count_as_heard(world):
    gate = _gate(world, "I believe you, Sorrel.")
    verdict = _speaker(gate, world, "sorrel", "Sorrel Tack", "Face — no, hood deep.")
    assert verdict["shown"] == "Sorrel" and verdict["introduces"]


def test_engine_directives_name_nobody(world):
    gate = _gate(world, "[OPENING] Present: Captain Brann Coldiron, Piper Quillow.")
    assert _speaker(gate, world, "brann", "Captain Brann Coldiron", "You're not from here.")["shown"] == "Watch Captain"


def test_earlier_dialogue_and_the_journal_count_as_heard(world):
    pid = world["player"]
    # fleet4-wenna T14: Brann named Sorrel to the player, so T15's "Stable
    # Hand" lines are Sorrel's, and his first own-name label introduces him
    memory_files.append_exchange(pid, world["brann"], "Who saw it?", ["Ask Sorrel at the stables."], "Day 1 18:00")
    memory_files.add_journal_entry(pid, "Piper Quillow keeps charts of the howling.")
    gate = _gate(world)
    role_line = _speaker(gate, world, "sorrel", "Stable Hand", "Few nights back I saw a man at the wall.")
    assert role_line["shown"] == "Sorrel" and not role_line["introduces"]
    assert _speaker(gate, world, "sorrel", "Sorrel Tack", "Hm?")["introduces"]
    assert _speaker(gate, world, "piper", "Piper Quillow", "The charts!")["shown"] == "Piper Quillow"
    # A memory file's labels and title carry the real name; only its quoted words count
    memory_files.append_exchange(pid, world["vex"], "", ["Nobody you know."], "Day 1 19:00")
    assert _speaker(_gate(world), world, "vex", "Vex Marrow", "Move.")["shown"] == "Ash-Cloaked Man"


def test_a_name_heard_where_no_memory_file_keeps_it_stays_heard(world):
    # An ambient voice and a quote in the prose are filed in nobody's memory
    gate = _gate(world)
    gate.hear("Gate Guard says that's Sorrel's horse.")
    gate.mask('"Vex," the scout says, tapping his chest.')
    dialogue.remember_heard(world["player"], gate.heard_this_turn())
    later = _gate(world)
    assert later.mask("Vex grins at Sorrel Tack.") == "Vex grins at Sorrel."
    # Kept once, however often it is heard
    dialogue.remember_heard(world["player"], ["Vex", "Vex"])
    text = memory_files._read(memory_files.Doc(world["player"], memory_files.HEARD_NAMES))
    assert text.count("- Vex") == 1


def test_a_met_person_is_shown_by_what_was_heard_and_a_wolf_by_its_name(world):
    _meet(world, "vex", "brann")
    gate = _gate(world)
    # Met before the name rule, nothing heard: the name they were given
    assert _speaker(gate, world, "vex", "Vex Marrow", "Move.")["shown"] == "Vex Marrow"
    gate.hear("Captain Coldiron wants you.")
    assert _speaker(gate, world, "brann", "Brann", "Well?")["shown"] == "Captain Coldiron"
    assert _speaker(gate, world, "wolf", "Hollow Wolf", "Grr.")["shown"] == "Hollow Wolf"


def test_a_bare_trade_word_is_no_introduction_and_no_warning(world):
    gate = _gate(world)
    verdict = _speaker(gate, world, "brann", "Captain", "Stay off the north road.")
    assert not verdict["introduces"] and verdict["shown"] == "Watch Captain"
    assert gate.unintroduced == []


def test_record_turn_dialogue_gates_an_explicit_label_on_its_own(world):
    # The CLI path passes the DM's label and no verdict: the name must have been said
    pid, vex = world["player"], world["vex"]
    dialogue.record_turn_dialogue(pid, "Who's there?", [{"npc_id": vex, "label": "Vex Marrow", "text": "Move along."}])
    assert not _met(world, "vex")
    dialogue.record_turn_dialogue(pid, "Who's there?", [{"npc_id": vex, "label": "Vex Marrow", "text": "Vex. Move."}])
    assert _met(world, "vex")
    # Heard earlier in the same turn, from someone else
    dialogue.record_turn_dialogue(pid, "", [
        {"npc_id": world["brann"], "label": "Watch Captain", "text": "That's Sorrel's job."},
        {"npc_id": world["sorrel"], "label": "Sorrel Tack", "text": "Mine, sir."}])
    assert _met(world, "sorrel") and not _met(world, "brann")
    # A caller's verdict stands
    dialogue.record_turn_dialogue(pid, "", [{"npc_id": world["piper"], "label": "Piper Quillow",
                                             "introduces": True, "text": "Charts!"}])
    assert _met(world, "piper")


# ----------------------------------------------------------------------
# Item 2: unmet names in the prose
# ----------------------------------------------------------------------

def test_fleet4_wenna_t11_an_unmet_name_in_prose_becomes_the_role(world):
    gate = _gate(world, present=("vex", "aldric"))
    text = "He glances once at Vex Marrow — a look that says *not yet* — and his voice drops.\n"
    assert gate.feed(text) == ("He glances once at the Ash-Cloaked Man — a look that says *not yet* — "
                               "and his voice drops.\n")
    assert gate.masked == [("Vex Marrow", "the Ash-Cloaked Man")]


def test_prose_masks_with_articles_possessives_and_titles(world):
    gate = _gate(world)
    assert gate.mask("Vex's grin doesn't move. Captain Coldiron nods at the door.") == (
        "The Ash-Cloaked Man's grin doesn't move. The Watch Captain nods at the door.")
    assert gate.mask("Aldric Vane watches; Brann does not.") == (
        "The Wolf-Mantled Man watches; the Watch Captain does not.")


def test_an_epithet_after_the_name_is_kept_instead_of_doubled(world):
    # fleet-tamsin T2: "Hesk the hawker" read "The Hawker the hawker"
    gate = _gate(world)
    assert gate.mask("Coldiron the watch captain nods. Later, Piper the herbalist's apprentice waves.") == (
        "The watch captain nods. Later, the herbalist's apprentice waves.")
    # Only the whole role is an epithet: "tell Coldiron the toll stands" (fleet-wenna T7)
    assert gate.mask("You tell Coldiron the toll stands.") == "You tell the Watch Captain the toll stands."
    assert gate.masked[0] == ("Coldiron", "The Watch Captain")


def test_what_was_said_is_heard_and_shown_as_heard(world):
    gate = _gate(world)
    # A quote is speech: heard, never rewritten, and the part said is the name now
    assert gate.mask('"Vex," the scout says. Vex Marrow smiles.') == '"Vex," the scout says. Vex smiles.'
    # The fiction giving a name
    assert gate.mask("A man named Aldric waits. Aldric smiles.") == "A man named Aldric waits. Aldric smiles."


def test_met_and_out_of_scope_people_are_left_alone(world):
    _meet(world, "brann")
    gate = _gate(world, present=("vex",))
    # Brann is met; Piper is neither here nor in the DM's context
    assert gate.mask("Brann Coldiron frowns at Piper Quillow.") == "Brann Coldiron frowns at Piper Quillow."
    in_context = _gate(world, present=("vex",), context="Beat: Piper Quillow brings her charts.")
    assert in_context.mask("Piper Quillow waves.") == "The Herbalist's Apprentice waves."


def test_the_campaign_cast_is_masked_wherever_they_are(world):
    from src.models import Campaign

    # The DM knows the villain's name from every earlier beat, not just this context
    with get_session() as session:
        session.add(Campaign(player_id=world["player"], villain_npc_id=world["piper"], status="active"))
        session.commit()
    gate = _gate(world, present=("vex",))
    assert gate.mask("Somewhere, Piper Quillow is laughing.") == "Somewhere, the Herbalist's Apprentice is laughing."


def test_a_name_that_is_a_word_is_the_word_at_a_sentence_start(world):
    gate = _gate(world)
    assert gate.mask("Tack hangs by the door. Mend the tack.") == "Tack hangs by the door. Mend the tack."
    assert gate.mask("He nods at Tack and leaves.") == "He nods at the Stable Hand and leaves."
    # After an article it is a thing or a place, not the person
    assert gate.mask("He walks to the Vane.") == "He walks to the Vane."


def test_streaming_holds_back_only_a_tail_that_could_become_a_name(world):
    gate = _gate(world, present=("vex",))
    # Ordinary prose is released at once
    assert gate.feed("The wind turns. ") == "The wind turns. "
    out = gate.feed("He glances at V")
    assert out == "He glances at "
    out += gate.feed("ex Mar") + gate.feed("row and nods. Then Vex") + gate.feed("ing thoughts. ")
    out += gate.flush()
    assert out == "He glances at the Ash-Cloaked Man and nods. Then Vexing thoughts. "


# ----------------------------------------------------------------------
# Item 3: the DM never voices the player
# ----------------------------------------------------------------------

@pytest.mark.parametrize("label, player, expected", [
    ("Bram Holloway", "Bram Holloway", True),
    ("Bram", "Bram Holloway", True),
    ("Holloway", "Bram Holloway", True),
    ("Sergeant Holloway", "Bram Holloway", True),
    ("You", "Bram Holloway", True),
    ("Me", None, True),
    ("Brann", "Bram Holloway", False),
    ("Keeper", "Bram Holloway", False),
    ("Bram", None, False),
    ("Eliasz", "Dr. Eliasz Rosen", True),
])
def test_is_player_label(label, player, expected):
    assert is_player_label(label, player) is expected


@pytest.mark.parametrize("mode", ["whole", "chars"])
def test_fleet4_bram_t15_the_players_speaker_line_is_dropped(mode):
    reply = ('Keeper: "Morning. Eat."\n'
             'Bram Holloway: "Anyone been through here lately asking odd questions?"\n'
             "She pours something into a second cup.\n")
    markup = DialogueMarkupFilter(player_name="Bram Holloway")
    chunks = [reply] if mode == "whole" else list(reply)
    segments = [s for c in chunks for s in markup.feed_segments(c)] + markup.flush_segments()
    merged: list[tuple[str, str]] = []
    for kind, value in segments:
        if kind == "text" and merged and merged[-1][0] == "text":
            merged[-1] = ("text", merged[-1][1] + value)
        elif kind != "text" or value:
            merged.append((kind, value["speaker"] if kind == "speech" else value))
    assert merged == [("speech", "Keeper"), ("text", "She pours something into a second cup.\n")]
    assert [p["speaker"] for p in markup.player_lines] == ["Bram Holloway"]
    warning = voiced_player_warning(markup.player_lines)
    assert warning["check"] == "voiced_the_player" and "Anyone been through" in warning["detail"]
    assert voiced_player_warning([]) is None


def test_history_replay_hides_a_dead_strangers_unsaid_name(world):
    from src.web.server import _reply_parts

    with get_session() as session:
        session.get(NPC, world["aldric"]).status = "dead"
        session.commit()
    with get_session():
        parts = _reply_parts('Aldric Vane: "You were never meant to see this."', {}, _gate(world), "Tamsin Rook")
    assert [p["npc_name"] for p in parts] == ["Wolf-Mantled Man"]


def test_the_narration_after_a_dropped_player_quote_stays():
    markup = DialogueMarkupFilter(player_name="Bram Holloway")
    text = markup.feed('Bram: "Anyone been through?" The keeper shrugs.\nShe pours.\n') + markup.flush()
    assert text == "The keeper shrugs.\nShe pours.\n" and markup.speech == []
    assert [p["text"] for p in markup.player_lines] == ["Anyone been through?"]


def test_history_replay_drops_the_players_lines_and_names_as_heard(world):
    from src.web.server import _reply_parts

    gate = _gate(world, present=("vex",))
    reply = ('He glances once at Vex Marrow.\n'
             'Ash-Cloaked Man: "Not yet."\n'
             'Tamsin: "I am leaving."\n'
             'Vex Marrow: "Go, then."\n'
             'You: "Fine." The door bangs.')
    with get_session():
        parts = _reply_parts(reply, {}, gate, "Tamsin Rook")
    assert [(p["kind"], p.get("npc_name") or p.get("text")) for p in parts] == [
        ("text", "He glances once at the Ash-Cloaked Man."),
        ("speech", "Ash-Cloaked Man"),
        ("speech", "Ash-Cloaked Man"),
        ("text", "The door bangs."),
    ]


# ----------------------------------------------------------------------
# Item 5: apostrophes are not quotes
# ----------------------------------------------------------------------

# fleet4-bram T14, verbatim: the audit read "Piper promised, and Wolf's
# Hollow beyond it hasn'" as Piper speaking, and the bogus warning went into
# T15's [REALITY AUDIT]
FLEET4_BRAM_T14 = (
    "The road to the east bend is waiting, cold and wrong exactly as Piper promised, and Wolf's Hollow "
    "beyond it hasn't gone anywhere either. Whatever knife dressed itself as a wolf's tooth three weeks "
    "ago left a mark on that bend that hasn't washed out yet — not in three weeks, maybe not ever, if you "
    "know where to look before anyone else gets there first."
)


def _checks(warnings):
    return [w["check"] for w in warnings]


def test_fleet4_bram_t14_apostrophes_are_not_dialogue():
    state = {"health": "healthy", "inventory": [], "location_id": "l", "currency": 1}
    assert "dialogue_without_speak" not in _checks(audit_turn(FLEET4_BRAM_T14, [], state, state))
    for text in (
        "He says it the way you'd correct a child's grip on a knife, and he's done.",
        "He says it like he's testing the shape of the thought himself, the way a man does when he's tired.",
    ):
        assert "dialogue_without_speak" not in _checks(audit_turn(text, [], state, state)), text


def test_single_quoted_dialogue_still_flags():
    state = {"health": "healthy", "inventory": [], "location_id": "l", "currency": 1}
    for text in (
        "The guard calls out, 'Move along, the lot of you, before the gate shuts.'",
        "Coldiron growls: ‘Not one more cart goes up that road.’",
    ):
        assert "dialogue_without_speak" in _checks(audit_turn(text, [], state, state)), text


# ----------------------------------------------------------------------
# Item 6: people who exist nowhere
# ----------------------------------------------------------------------

KNOWN = {"sorrel", "tack", "brann", "coldiron", "maera", "duskwell", "piper", "quillow", "aldric", "vane",
         "vex", "marrow", "wolf", "hollow", "lantern", "watch", "greywater", "emberfall", "tamsin", "rook",
         "bram", "holloway", "ashen", "fangs", "north", "gate"}
COMMON = {"not", "the", "and", "one", "of", "nobody", "here", "day", "farm", "two", "sheep", "dead", "man",
          "good", "listen", "still", "shift", "night", "mark"}


def test_fleet4_invented_people_are_found():
    lines = [
        # fleet4-wenna T16 (Sorrel)
        "Here's the part nobody believes me on — one of the wall sentries, Garrick, he saw him too. "
        "Didn't raise the alarm.",
        "You believe me? Nobody else did. Not Garrick's shift-mate, not the quartermaster.",
        # fleet4-bram T18 (Maera)
        "Corvin Ashe. I remember him better than most — paid well, spoke less.",
        # fleet4-tamsin T14 (Piper)
        "The second was the mill's wages. Old Harwick practically runs the grain trade out of the square.",
        # fleet4-wenna T17's @journal
        "Sorrel Tack saw an ash-cloaked man at the north wall — and sentry Garrick saw him too.",
    ]
    assert unknown_named_people(lines, KNOWN, COMMON) == ["Garrick", "Corvin Ashe", "Old Harwick"]


def test_known_names_places_words_and_sentence_starts_are_not_invented_people():
    lines = [
        "Garrick's not on tonight — day shift.",              # one word opening a sentence
        "Ask Sorrel Tack. Coldiron knows. Vane's men watch the Lantern Watch.",
        "Two sheep gone from Harrow farm, and the Greywater road is shut.",
        "Wait by Dead Man's Ford until the North Gate opens.",
        "Listen, I'm telling you. Gods know it. The Reeths kept to themselves.",
        "His tools'll be marked, M on the handle.",
        "So. Ask properly, Holloway-who-didn't-give-his-name-till-just-now.",
        "Sir, the Captain wants you, Tamsin.",
    ]
    assert unknown_named_people(lines, KNOWN, COMMON) == []
    # A title says person even at a sentence start
    assert unknown_named_people(["Captain Garrick wants you."], KNOWN, COMMON) == ["Captain Garrick"]
    # One person named twice is one name (fleet2-tamsin T8 listed 'Tomas Reed', 'Tomas')
    assert unknown_named_people(["I saw Tomas Reed at dawn.", "You will find Tomas soon."], KNOWN, COMMON) == ["Tomas Reed"]
    assert unknown_named_people(["You will find Tomas soon.", "I saw Tomas Reed at dawn."], KNOWN, COMMON) == ["Tomas Reed"]


def test_unknown_person_and_unintroduced_warnings_say_what_to_write():
    warning = unknown_person_warning(["Garrick"])
    assert warning["check"] == "unknown_named_person"
    assert "'Garrick'" in warning["detail"] and "@new-npc <name>" in warning["detail"]
    assert "@new-place" in warning["detail"] and "drop the name" in warning["detail"]
    assert unknown_person_warning([]) is None
    warning = unintroduced_name_warning([("Aldric Vane", "Wolf-Mantled Man")],
                                        [("Vex Marrow", "the Ash-Cloaked Man"), ("Vex Marrow", "the Ash-Cloaked Man")])
    assert warning["check"] == "unintroduced_name"
    assert "'Aldric Vane' (the player knows them as 'Wolf-Mantled Man')" in warning["detail"]
    assert warning["detail"].count("Vex Marrow") == 1 and "SAYS it aloud" in warning["detail"]
    assert unintroduced_name_warning([], []) is None


def test_world_words_hold_every_authored_name(world):
    with get_session() as session:
        session.add(WorldBible(name="Emberfall", genre="fantasy", setting_description="Old Harwick runs the mill."))
        session.commit()
        names, words = dialogue.world_words(session)
    assert {"vex", "marrow", "coldiron", "wolf", "hollow", "ashen", "fangs", "harwick", "tamsin"} <= names
    assert {"menacing", "cloak", "runs", "mill"} <= words
    assert "garrick" not in names


# ----------------------------------------------------------------------
# The stream end to end (items 1-4, 6): a scripted DM through /api/play
# ----------------------------------------------------------------------

class _FakeAgent:
    def __init__(self, events):
        self.events = events
        self.messages = []

    async def stream_async(self, context):
        for event in self.events:
            yield event


class _FakeDM:
    def __init__(self, events, present, player_name):
        self.agent = _FakeAgent(events)
        self.last_brief = {"present": present}
        self._player_name = player_name

    def _build_context(self, player_input, **_kwargs):
        return "THE SCENE: " + ", ".join(self.last_brief["present"].values()) + f"\nPlayer says: {player_input}"


def _play(world, monkeypatch, events, player_input="I wait.", present=("vex", "aldric")):
    from src.web import server
    from src.web.streaming import ToolUsageTracker

    with get_session() as session:
        names = {world[k]: session.get(NPC, world[k]).name for k in present}
    dm = _FakeDM(events, names, "Tamsin Rook")
    monkeypatch.setattr(server, "get_or_create_session", lambda pid: {"dm": dm, "tool_tracker": ToolUsageTracker()})
    monkeypatch.setattr(server, "_engine_ambush", lambda *a, **k: None)
    monkeypatch.setattr(server, "maybe_advance_world", lambda: None)
    server._pending_audit_feedback.pop(world["player"], None)

    async def run():
        response = await server.play_sse(server.GameRequest(player_input=player_input, player_id=world["player"]))
        return [chunk async for chunk in response.body_iterator]

    frames = []
    for chunk in asyncio.run(run()):
        for line in str(chunk).splitlines():
            if line.startswith("data: ") and line[6:].strip() not in ("", "{}"):
                frames.append(json.loads(line[6:]))
    text = "".join(f["data"] for f in frames if f.get("type") == "token")
    speech = [f for f in frames if f.get("type") == "speech"]
    complete = next(f for f in frames if f.get("type") == "complete")
    return text, speech, _checks(complete["audit"]), complete


def _roll(name="roll_check"):
    return {"current_tool_use": {"toolUseId": "t1", "name": name, "input": ""}}


def test_the_roll_check_seam_always_gets_a_paragraph_break(world, monkeypatch):
    # fleet4-tamsin T7: the pre-roll sentence sat in the line-buffered junk
    # filter through the tool call and the next model call glued onto it
    text, _, checks, _ = _play(world, monkeypatch, [
        {"data": "He weighs whether honesty costs him anything tonight."},
        _roll(),
        {"data": "Something shifts behind his eyes.\n"},
    ])
    assert "tonight.\n\nSomething shifts" in text
    assert "prose_before_roll" in checks


def test_the_seam_holds_after_speech_too_and_a_clean_roll_is_not_flagged(world, monkeypatch):
    # After a speaker line the deduper holds a sentence back as well
    # (fleet4-tamsin T19 "flickered.She holds")
    text, speech, checks, _ = _play(world, monkeypatch, [
        {"data": 'Ash-Cloaked Man: "Not yet."\nA lesser liar would have flickered.'},
        _roll(),
        {"data": "His gaze holds a beat too long.\n"},
    ])
    assert "flickered.\n\nHis gaze" in text and len(speech) == 1
    assert "prose_before_roll" in checks
    text, _, checks, _ = _play(world, monkeypatch, [_roll(), {"data": "The lock gives.\n"}])
    assert text.strip() == "The lock gives." and "prose_before_roll" not in checks


def test_history_replay_never_glues_two_text_blocks_and_masks_names(world, monkeypatch):
    from src.web import server

    dm = _FakeDM([], {}, "Tamsin Rook")
    dm.agent.messages = [
        {"role": "user", "content": [{"text": "ctx\nPlayer says: I push him."}]},
        {"role": "assistant", "content": [{"text": "He weighs it tonight."},
                                          {"toolUse": {"name": "roll_check", "input": {}}}]},
        # Two text blocks in one message (a stripped reasoning block sat between)
        {"role": "assistant", "content": [{"text": "Something shifts."}, {"text": "He glances at Vex Marrow."}]},
    ]
    monkeypatch.setattr(server, "get_or_create_session", lambda pid: {"dm": dm, "tool_tracker": None})
    history = asyncio.run(server.get_chat_history(world["player"]))
    assert [m["content"] for m in history["messages"] if m["role"] == "assistant"] == [
        "He weighs it tonight.", "Something shifts.\n\nHe glances at the Ash-Cloaked Man."]


def test_names_voices_and_inventions_through_the_stream(world, monkeypatch):
    text, speech, checks, complete = _play(world, monkeypatch, [
        {"data": "He glances once at Vex Marrow — not yet.\n"},
        {"data": 'Aldric Vane (cold): "One of the wall sentries, Garrick, owes me."\n'},
        {"data": 'Tamsin Rook: "I am leaving."\n'},
        {"data": 'Ash-Cloaked Man: "Name\'s Vex."\nVex grins.\n'},
    ])
    assert "Vex Marrow" not in text and "the Ash-Cloaked Man" in text and "Vex grins." in text
    assert "I am leaving" not in text
    assert [s["npc_name"] for s in speech] == ["Wolf-Mantled Man", "Vex"]
    assert all("Aldric" not in json.dumps(s) for s in speech)
    assert {"unintroduced_name", "voiced_the_player", "unknown_named_person"} <= set(checks)
    detail = next(w["detail"] for w in complete["audit"] if w["check"] == "unknown_named_person")
    assert "'Garrick'" in detail
    # Nobody said Aldric's name. Vex's role-labelled line SAID his, and a
    # name heard is a name known (fleet6: who knows whom) — his bubble
    # already read "Vex"; now his row and dossier agree
    assert not _met(world, "aldric") and _met(world, "vex")


def test_a_speech_event_carries_only_the_name_the_player_heard(world, monkeypatch):
    # The DM's raw label used to ride every speech frame ("label": "Vex Marrow")
    _, speech, _, _ = _play(world, monkeypatch, [{"data": 'Vex Marrow: "Vex. Remember it."\n'}])
    assert [s["npc_name"] for s in speech] == ["Vex"] and "Marrow" not in json.dumps(speech)
    assert _met(world, "vex")       # the verdict still reached record_turn_dialogue


def test_the_beat_and_event_checks_read_the_names_the_dm_wrote(world, monkeypatch):
    # Review (round 3): the gate shows the player "the Ash-Cloaked Man", but
    # the campaign's delivery keywords say "Vex Marrow" (fleet4 beats: "The
    # Hollow's Watcher", "The Sergeant's Name"). Matched against the masked
    # text, a delivered beat was nagged as undelivered and left to stall.
    from src.services import world_tick
    from src.web import server

    beat = {"title": "The Hollow's Watcher", "delivery_keywords": ["Vex Marrow", "Aldric Vane", "counting-house",
                                                                   "ledger"]}
    calls = {"tick": 0}

    def last_tick(_pid):
        calls["tick"] += 1      # the first read is "before the turn"
        return None if calls["tick"] == 1 else {"audit": {"beat": beat, "sealed_truths": [], "known_words": []}}

    beats, events = [], []
    monkeypatch.setattr(server, "get_last_tick", last_tick)
    monkeypatch.setattr(server, "mark_beat_delivery",
                        lambda pid, text, speakers=None, spoken=None, **_turn: beats.append((text, speakers)) or {})
    monkeypatch.setattr(world_tick, "mark_event_delivery",
                        lambda text, speakers=None, spoken_text="", **_turn: events.append((text, speakers)) or [])
    text, speech, checks, _ = _play(world, monkeypatch, [
        {"data": "Vex Marrow slips out of the counting-house.\n"},
        {"data": 'Aldric Vane (cold): "Count it twice."\n'},
    ])
    # The player still sees only what they know
    assert "Vex Marrow" not in text and [s["npc_name"] for s in speech] == ["Wolf-Mantled Man"]
    assert "campaign_beat_not_delivered" not in checks
    (beat_text, beat_speakers), = beats
    assert "Vex Marrow" in beat_text and "Aldric Vane" in beat_speakers
    (event_text, event_speakers), = events
    assert "Vex Marrow" in event_text and "Aldric Vane" in event_speakers


def test_journal_entries_are_checked_for_invented_and_unheard_names(world, monkeypatch):
    _, _, checks, complete = _play(world, monkeypatch, [
        {"data": "You note it down.\n@journal The sentry Garrick saw the cloaked man too.\n"
                 "@journal Vex Marrow carries a bone whistle.\n"},
    ])
    assert "unknown_named_person" in checks and "unintroduced_name" in checks
    detail = next(w["detail"] for w in complete["audit"] if w["check"] == "unintroduced_name")
    assert "'Vex Marrow' (the player knows them as 'Ash-Cloaked Man')" in detail

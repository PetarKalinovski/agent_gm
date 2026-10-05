"""Names and voices, round 6 (fleet7).

  * The player's name in a stranger's mouth: Piper said "Straight lines,
    Bram" (bram T16, T18) and "Someone trained them, Tamsin —" (tamsin T11);
    her dossier said "Knows the player's name: no" and the audit caught it a
    turn late. Now the line loses the name before anyone sees it, and prose
    remarking on the name goes with it.
  * A notice read this reply is heard before its quote is masked (bram T18:
    "— Capt. B. Coldiron." went out as "— Capt. B. The Watch Captain.").
  * A trade has many words: "Barkeep" is the keeper (bram T20), "Market
    Vendor" the hawker standing there (tamsin T10).
  * The renamed dead: "Fenwick" made beside the world's dead carter (wenna T2).
  * Audit precision: "Thursdays" was no missing person (wenna T7).
"""

import asyncio
import json
import re
from pathlib import Path

import pytest

from src.models import Location, LocationType, NPC, NPCRelationship, Player, WorldClock, get_session
from src.services import dialogue, memory_files
from src.services.commands import CommandRunner
from src.services.dialogue import NameGate, cut_player_name, player_has_met, resolve_speaker
from src.services.scene_brief import build_scene_brief
from src.web.turn_audit import player_name_cut_warning, unknown_named_people

CORPUS = json.loads((Path(__file__).parent / "data" / "player_name_corpus.json").read_text(encoding="utf-8"))

NOTICE = {"id": "notice_0", "kind": "read", "label": "Notice board", "x": 60, "y": 30,
          "payload": {"text": "TAKEN BY WOLVES — third cart this month. — Capt. B. Coldiron.\n\n"
                              "Beneath it, smaller: 'Sweetroot for nerves. Ask Piper.'"}}


@pytest.fixture()
def world(db):
    with get_session() as session:
        session.add(WorldClock(day=1, hour=19, minute=0))
        square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True, visited=True,
                          objects=[NOTICE])
        inn = Location(name="The Drowned Lantern", type=LocationType.BUILDING, depth=0, discovered=True)
        session.add_all([square, inn])
        session.flush()
        brann = NPC(name="Captain Brann Coldiron", profession="Captain of the Lantern Watch",
                    current_location_id=square.id,
                    description_physical="An old, weathered veteran with a gray beard cropped short.")
        piper = NPC(name="Piper Quillow", profession="Herbalist's apprentice", current_location_id=square.id,
                    description_physical="A young woman with bright, emotional eyes, chapped hands stained green.")
        sorrel = NPC(name="Sorrel Tack", profession="Stable hand", current_location_id=square.id,
                     description_physical="A friendly young man all elbows and straw-colored hair.")
        hesk = NPC(name="Hesk Bramble", profession="Hawker of blades, tools and remedies",
                   current_location_id=square.id,
                   description_physical="A stooped, wiry hawker with a leather apron hung with knives.")
        maera = NPC(name="Maera Duskwell", profession="Keeper of The Drowned Lantern", current_location_id=inn.id,
                    description_physical="A middle-aged woman with a warm, smooth manner and a copper clasp.")
        session.add_all([brann, piper, sorrel, hesk, maera])
        player = Player(name="Wenna Cole", current_location_id=square.id, currency=30)
        session.add(player)
        session.commit()
        return {"player": player.id, "square": square.id, "inn": inn.id, "brann": brann.id, "piper": piper.id,
                "sorrel": sorrel.id, "hesk": hesk.id, "maera": maera.id}


# --- the player's name, cut from a stranger's line ----------------------------

@pytest.mark.parametrize("case", CORPUS, ids=lambda c: f"{c['run']}-T{c['turn']}-{c['speaker'][:12]}")
def test_every_real_line_reads_whole_without_the_players_name(case):
    """Every speaker line of every playtest that said the player's name
    (tests/data/player_name_corpus.json), as it reads cut."""
    out, cut = cut_player_name(case["before"], case["player"])
    assert out == case["after"]
    assert cut == case["cut"] and cut
    first, last = case["player"].split()[0], case["player"].split()[-1]
    assert not re.search(rf"\b(?:{first}|{last})\b", out)
    assert out[:1].isupper() and "  " not in out and not re.search(r"\s[,.!?]", out)


@pytest.mark.parametrize("before, after", [
    ("I saw Bram at the gate.", "I saw you at the gate."),
    ("Bram is here.", "You are here."),
    ("Bram counts the howls every night.", "You count the howls every night."),
    ("Bram's been gone an hour.", "You've been gone an hour."),
    ("Is that Bram's blade?", "Is that your blade?"),
    ("Oh Bram, you didn't.", "Oh, you didn't."),
    ("Go home — Bram.", "Go home."),
    ("Bram?", "You?"),
    ("Thanks, Bram", "Thanks"),
    # The name as what "you" are, or who someone is: never "You're you"
    ("You're Bram, aren't you?", "You're the one, aren't you?"),
    ("You must be Bram.", "You must be the one."),
    ("Are you Bram?", "Are you the one?"),
    ("Who's Bram?", "Who's that?"),
])
def test_other_uses_become_you(before, after):
    assert cut_player_name(before, "Bram Holloway") == (after, ["Bram"])


@pytest.mark.parametrize("line, player", [
    ("Holloway's Farm is north of here.", "Bram Holloway"),
    ("Osric Holloway runs the mill.", "Bram Holloway"),
    ("Cole Street is quiet tonight.", "Wenna Cole"),
    ("Will you come with me?", "Will Ashby"),
    ("Nobody here by that name.", "Bram Holloway"),
])
def test_a_name_inside_another_name_or_a_plain_word_is_left_alone(line, player):
    assert cut_player_name(line, player) == (line, [])


def _gate(world, player_input="I wait by the well."):
    return NameGate.for_turn(world["player"], player_input, present_ids=[world["piper"], world["brann"]])


def test_a_stranger_to_the_name_loses_it_in_stream(world):
    gate = _gate(world)
    out = gate.player_name_in(world["piper"], "That's not an accident, Wenna.", "Herbalist's Apprentice")
    assert out == "That's not an accident."
    assert gate.player_name_cuts == [("Herbalist's Apprentice", "Wenna")]
    # An ambient voice knows nobody
    assert gate.player_name_in(None, "Off with you, Wenna Cole.", "Gate Guard") == "Off with you."
    warning = player_name_cut_warning(gate.player_name_cuts)
    assert warning["check"] == "name_before_introduction"
    assert "Herbalist's Apprentice called the player \"Wenna\"" in warning["detail"]
    assert "cut it from the line" in warning["detail"]


@pytest.mark.parametrize("knows", [True, None])
def test_someone_who_knows_the_name_keeps_it(world, knows):
    """A row that says so, or one from before the split (NULL), knows it."""
    with get_session() as session:
        session.add(NPCRelationship(npc_id=world["piper"], player_id=world["player"], knows_player_name=knows))
        session.commit()
    gate = _gate(world)
    assert gate.player_name_in(world["piper"], "That's not an accident, Wenna.") == "That's not an accident, Wenna."
    assert gate.player_name_cuts == []


def test_a_name_told_before_or_this_turn_is_theirs(world):
    # Told to her in an earlier exchange (no row yet)
    memory_files.append_exchange(world["player"], world["piper"], "I'm Wenna Cole.", ["Oh!"], "Day 1 18:00")
    assert _gate(world).player_name_in(world["piper"], "Careful, Wenna.") == "Careful, Wenna."
    # Said by the player this turn, in their words or "I tell her my name"
    for said in ('"I\'m Wenna," I tell the captain.', "I tell him my name."):
        assert _gate(world, said).player_name_in(world["brann"], "Wenna. Good.") == "Wenna. Good."
    # Given in the prose, or said aloud by someone who knew it: everyone heard
    gate = _gate(world)
    gate.feed("You tell him your name.\n")
    assert gate.player_name_in(world["brann"], "Wenna, then.") == "Wenna, then."
    with get_session() as session:
        session.add(NPCRelationship(npc_id=world["sorrel"], player_id=world["player"], knows_player_name=True))
        session.commit()
    gate = _gate(world)
    gate.player_name_in(world["sorrel"], "Evening, Wenna.")
    assert gate.player_name_in(world["brann"], "Wenna. A word.") == "Wenna. A word."
    assert gate.player_name_cuts == []


def test_prose_remarking_on_a_cut_name_goes_too(world):
    gate = _gate(world)
    remark = "She says your name like she's only just letting herself use it."
    assert not gate.comments_on_cut_name(remark), "no cut, nothing to retract"
    gate.player_name_in(world["piper"], "Someone trained them, Wenna — someone's out there.")
    assert gate.comments_on_cut_name(remark)
    assert gate.comments_on_cut_name("Your name sounds strange in her mouth.")
    assert not gate.comments_on_cut_name("She never asks your name.")
    assert not gate.comments_on_cut_name('"Your name," she says, "is your own business."')
    assert not gate.comments_on_cut_name("The square has gone quiet.")


# --- through /api/play -------------------------------------------------------

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


def _play(world, monkeypatch, reply, player_input, present=("piper", "brann", "sorrel", "hesk"), chunk=None):
    from src.web import server
    from src.web.streaming import ToolUsageTracker

    with get_session() as session:
        names = {world[k]: session.get(NPC, world[k]).name for k in present}
    size = chunk or len(reply)
    dm = _FakeDM([{"data": reply[i:i + size]} for i in range(0, len(reply), size)], names)
    monkeypatch.setattr(server, "get_or_create_session", lambda pid: {"dm": dm, "tool_tracker": ToolUsageTracker()})
    monkeypatch.setattr(server, "_engine_ambush", lambda *a, **k: None)
    monkeypatch.setattr(server, "maybe_advance_world", lambda: None)
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    server._pending_audit_feedback.pop(world["player"], None)

    async def run():
        response = await server.play_sse(server.GameRequest(player_input=player_input, player_id=world["player"]))
        return [chunk async for chunk in response.body_iterator]

    return [json.loads(line[6:]) for chunk in asyncio.run(run()) for line in str(chunk).splitlines()
            if line.startswith("data: ") and line[6:].strip() not in ("", "{}")]


def _prose(frames):
    return "".join(f["data"] for f in frames if f.get("type") == "token")


def _audit(frames):
    return {w["check"]: w["detail"] for w in next(f for f in frames if f.get("type") == "complete")["audit"]}


@pytest.mark.parametrize("chunk", [None, 5])
def test_play_cuts_the_name_everywhere_the_line_goes(world, monkeypatch, chunk):
    """fleet7-tamsin T11, through the stream: the bubble, the voice's text,
    the memory file and the transcript all read the cut line; the prose that
    made a moment of the name is gone; the DM is told why. Streamed whole or
    a few characters at a time."""
    frames = _play(world, monkeypatch,
                   "Herbalist's Apprentice (shaking): \"Someone trained them, Wenna — someone's out there.\"\n"
                   "She says your name like she's only just letting herself use it. The square has gone quiet.\n",
                   '"Trained?" I ask the girl.', chunk=chunk)
    speech = [f for f in frames if f.get("type") == "speech"]
    assert [s["text"] for s in speech] == ["Someone trained them — someone's out there."]
    prose = _prose(frames)
    assert "your name" not in prose and "The square has gone quiet." in prose
    assert "cut it from the line" in _audit(frames)["name_before_introduction"]
    recent = " ".join(memory_files.memory_parts(world["player"], world["piper"])["recent"])
    assert "Someone trained them — someone's out there." in recent and "them, Wenna" not in recent
    rel = next(iter(_rows(world, "piper")), None)
    assert rel is not None and rel.knows_player_name is False, "a cut leak teaches nobody"


def test_history_replay_cuts_the_name_as_the_stream_did(world):
    """A reload replays the DM's stored reply: the line the player saw cut
    must not come back whole — and a speaker who knows the name keeps it."""
    from src.web.server import _reply_parts

    reply = ('Herbalist\'s Apprentice (low): "That\'s not an accident, Wenna."\n'
             'Stable Hand: "Evening, Wenna."')
    with get_session() as session:
        session.add(NPCRelationship(npc_id=world["sorrel"], player_id=world["player"], knows_player_name=True))
        session.commit()
    gate = NameGate.for_turn(world["player"], "", context=reply)
    with get_session():
        parts = _reply_parts(reply, {}, gate, "Wenna Cole")
    assert [p["text"] for p in parts if p["kind"] == "speech"] == ["That's not an accident.", "Evening, Wenna."]


def test_history_replay_hears_a_name_in_its_own_turn_only(world, monkeypatch):
    """The chat history reads every turn through one gate: Sorrel, who knew
    it, saying "Wenna" at one turn gave it to that scene — not to Piper,
    turns later and elsewhere (live, her line was cut)."""
    from src.web import server

    with get_session() as session:
        session.add(NPCRelationship(npc_id=world["sorrel"], player_id=world["player"], knows_player_name=True))
        session.commit()
    dm = _FakeDM([], {})
    dm.agent.messages = [
        {"role": "user", "content": [{"text": "ctx\nPlayer says: I wait by the well."}]},
        {"role": "assistant", "content": [{"text": 'Sorrel Tack: "Evening, Wenna."'}]},
        {"role": "user", "content": [{"text": "ctx\nPlayer says: I find the girl."}]},
        {"role": "assistant", "content": [{"text": 'Piper Quillow: "Straight lines, Wenna."'}]},
    ]
    monkeypatch.setattr(server, "get_or_create_session", lambda pid: {"dm": dm, "tool_tracker": None})
    history = asyncio.run(server.get_chat_history(world["player"]))
    said = [p["text"] for m in history["messages"] for p in m.get("parts") or [] if p["kind"] == "speech"]
    assert said == ["Evening, Wenna.", "Straight lines."]


def test_play_drops_a_re_quote_of_the_line_as_written(world, monkeypatch):
    """The DM re-quoting its own line in the prose quotes it as written, the
    name and all: that copy is stripped like any re-quote of a spoken line,
    never left to say the name the line lost."""
    frames = _play(world, monkeypatch,
                   'Herbalist\'s Apprentice (low): "Lines, Wenna, straight lines."\n'
                   '"Lines, Wenna, straight lines," she says again. The square goes quiet.\n',
                   "I look at the tracks.")
    assert [f["text"] for f in frames if f.get("type") == "speech"] == ["Lines, straight lines."]
    prose = _prose(frames)
    assert "Wenna" not in prose and "The square goes quiet." in prose, prose


def test_play_keeps_the_name_for_someone_told_this_turn(world, monkeypatch):
    frames = _play(world, monkeypatch, 'Herbalist\'s Apprentice: "Wenna — that\'s a nice name."\n',
                   '"I\'m Wenna," I tell the girl.')
    assert [f["text"] for f in frames if f.get("type") == "speech"] == ["Wenna — that's a nice name."]
    assert "name_before_introduction" not in _audit(frames)


def _rows(world, key):
    with get_session() as session:
        rows = session.query(NPCRelationship).filter_by(player_id=world["player"], npc_id=world[key]).all()
        for row in rows:
            session.expunge(row)
        return rows


# --- a document read is heard before its quote is masked -----------------------

@pytest.mark.parametrize("chunk", [None, 5])
def test_a_notice_read_this_reply_is_quoted_whole(world, monkeypatch, chunk):
    """fleet7-bram T18: `@interact Notice board`, then the notice quoted word
    for word as instructed — no mask inside it, no unintroduced_name."""
    frames = _play(world, monkeypatch,
                   "@interact Notice board\n"
                   "The fresh notice is nailed dead center. *TAKEN BY WOLVES — third cart this month. — Capt. B. "
                   "Coldiron.* Beneath it, smaller: *Sweetroot for nerves. Ask Piper.*\n",
                   "I read the notice board.", chunk=chunk)
    prose = _prose(frames)
    assert "Capt. B. Coldiron." in prose and "Ask Piper." in prose
    assert "Watch Captain" not in prose
    assert "unintroduced_name" not in _audit(frames)
    assert {"Coldiron", "Piper"} <= set(dialogue.heard_names(world["player"]))


def test_a_notice_read_on_the_canvas_is_a_name_learned_at_once(world, tmp_path, monkeypatch):
    from src.web import server

    # The handler re-inits the active world's database: keep it on this test's
    monkeypatch.setattr(server, "get_active_db_path", lambda: str(tmp_path / "test.db"))
    monkeypatch.setattr(server, "maybe_advance_world", lambda: None)
    result = asyncio.run(server.canvas_interact(server.InteractRequest(
        player_id=world["player"], location_id=world["square"], object_id="notice_0")))
    assert result["success"]
    assert {"Coldiron", "Piper"} <= set(dialogue.heard_names(world["player"]))
    with get_session() as session:
        assert player_has_met(session, world["player"], world["brann"])
        assert not player_has_met(session, world["player"], world["sorrel"])
    assert dialogue.read_texts({"success": True, "kind": "body", "result": {"panel_text": "Coin x3"}}) == []


# --- a trade has many words -----------------------------------------------------

def test_a_trade_word_finds_the_one_person_of_that_trade_here(world):
    square = [world[k] for k in ("brann", "piper", "sorrel", "hesk")]
    with get_session() as session:
        def who(label, here):
            npc = resolve_speaker(session, label, here)
            return npc.name if npc else None

        for label in ("Barkeep", "Bartender", "Tavern Keeper", "Innkeeper", "Publican"):
            assert who(label, [world["maera"]]) == "Maera Duskwell", label
        for label in ("Market Vendor", "Merchant", "Peddler", "Trader"):
            assert who(label, square) == "Hesk Bramble", label
        for label in ("Stable Boy", "Stablehand", "Groom", "Stable Lad"):
            assert who(label, square) == "Sorrel Tack", label
        # Nobody of that trade here, or a captain who is no guard: a voice
        assert who("Barkeep", square) is None
        assert who("Guard", square) is None and who("Watchman", square) is None
        assert who("Vendor", [world["piper"], world["sorrel"]]) is None
        # A person noun or another trade in the label is someone else
        assert who("Merchant Guard", square) is None
        assert who("Vendor Boy", square) is None
        # Two trades the table knows, and they differ: one word ending the
        # other is no match ("Gatekeeper" is no innkeeper), and "Shopkeeper"
        # beside the hawker is him alone, not him and the keeper
        assert who("Gatekeeper", [world["maera"]]) is None
        assert who("Shopkeeper", [world["hesk"], world["maera"]]) == "Hesk Bramble"
        # ...but their own word still is them: "Keeper" for the Gate Keeper
        gatekeeper = NPC(name="Ansel Ferr", profession="Keeper of the North Gate")
        session.add(gatekeeper)
        session.commit()
        assert who("Keeper", [gatekeeper.id]) == "Ansel Ferr"
        twin = NPC(name="Odo Pell", profession="Peddler", current_location_id=world["square"])
        session.add(twin)
        session.commit()
        assert who("Market Vendor", square + [twin.id]) is None, "two of a trade: nobody"


# --- the dead keep their names ---------------------------------------------------

def _new_dead_carter(world, name="Fenwick"):
    return CommandRunner(world["player"]).run(
        f"@new-npc {name} | trade: carter | looks: never seen | manner: unknown | voice: unknown | status: dead")


def test_a_second_dead_carter_is_refused_and_points_at_the_first(world):
    """fleet7-wenna T2: `@new-npc Fenwick | trade: carter | … | status: dead`."""
    with get_session() as session:
        session.add(NPC(name="Tobin", profession="Carter", status="dead", current_location_id=None))
        session.add(NPC(name="Grey Wolf", profession="wolf", species="wolf", status="dead"))
        session.commit()
    result = _new_dead_carter(world)
    assert not result.ok and "Tobin (Carter, dead)" in result.message and "never a second one" in result.message
    with get_session() as session:
        assert session.query(NPC).filter(NPC.name == "Fenwick").first() is None
        brief = build_scene_brief(session, session.get(Player, world["player"]))["text"]
    lost = next(line for line in brief.splitlines() if line.startswith("People the story has lost"))
    assert "Tobin [" in lost and "(Carter, dead, name not yet heard by the player)" in lost
    assert "Grey Wolf" not in lost


def test_a_dead_carter_is_made_when_the_world_has_none(world):
    with get_session() as session:
        session.add(NPC(name="Hob Wainright", profession="Carter", current_location_id=world["square"]))
        session.commit()
    result = _new_dead_carter(world)
    assert result.ok and result.result.get("created")
    with get_session() as session:
        brief = build_scene_brief(session, session.get(Player, world["player"]))["text"]
    assert "People the story has lost" in brief and "Fenwick [" in brief


@pytest.mark.parametrize("lost, made", [
    ("Captain of the Lantern Watch", "watchman"),         # a captain is no watchman
    ("Keeper of The Drowned Lantern", "Lantern Watch sergeant"),  # "lantern" is no trade
    ("Wagon driver on the north road", "road warden"),
    ("unknown", "unknown"),                               # no trade to share
])
def test_only_the_head_of_a_trade_makes_two_people_one(world, lost, made):
    """The guard reads a trade by its head word, as `dialogue._trade_heads`
    does: every word of it used to count, so a dead captain of the Watch
    refused the story a missing watchman."""
    with get_session() as session:
        session.add(NPC(name="Odo Pell", profession=lost, status="dead"))
        session.commit()
    result = CommandRunner(world["player"]).run(
        f"@new-npc Harl Venn | trade: {made} | looks: never seen | manner: unknown | voice: unknown | status: missing")
    assert result.ok and result.result.get("created"), result.message


def test_a_truth_that_names_the_dead_carter_is_who_the_dm_means(world):
    from src.models.campaign import Campaign

    with get_session() as session:
        session.add(Campaign(player_id=world["player"], title="The Song", status="active", fixed_truths=[
            {"id": "t1", "kind": "the_secret",
             "statement": "The carter Tobin was found dead on the north road after he saw the whistle."}]))
        session.commit()
    result = _new_dead_carter(world)
    assert not result.ok and result.message.startswith("nothing was made — the story already has Tobin")
    assert _new_dead_carter(world, "Tobin").ok, "the name the story uses is made"


# --- audit precision ------------------------------------------------------------

def test_a_weekday_is_nobody():
    """fleet7-wenna T7: Sorrel's "Thursdays" flagged as a missing person."""
    lines = ["Mostly from the Hollow way, yeah — but Thursdays, sometimes there's a thinner one.",
             "Come back Mondays, when the cart's in."]
    assert unknown_named_people(lines, set(), set()) == []


def test_a_word_after_a_quoted_sentence_opens_a_sentence():
    """fleet-bram T10: "'What are we hunting.' Careful with that word" —
    "Careful" opens the sentence after the quote; it is nobody."""
    line = "You ask 'what are we hunting.' Careful with that word, soldier. Ask Garrick."
    assert unknown_named_people([line], set(), {"ask"}) == ["Garrick"]

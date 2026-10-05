"""An authored job its giver pitched and the player took gets started.

fleet7 (all three sessions): Piper pitched "Counting the Howls" with its pay,
the player said yes (-bram T5 "Alright. I'll talk to him") or simply went and
asked Sorrel (-wenna T3-T7), and no @quest-start was ever written — the
witness work earned nothing. src/services/job_offers.py remembers the pitch,
reads the player's answer ([START THIS JOB]), and starts the job itself once
play has done a step of it, crediting what was already done.
"""

from types import SimpleNamespace

import pytest

from src.agents import dm_orchestrator
from src.agents.dm_orchestrator import DMOrchestrator, reset_offer_state
from src.models import NPC, Location, LocationType, Player, Quest, QuestStatus, WorldClock, get_session
from src.services import job_offers, objectives
from src.services.commands import CommandRunner
from src.services.dialogue import record_turn_dialogue

PITCH = ("Ten coin and a tincture if you ask the stable boy what he hears after dark. Nobody with a badge "
         "will hear me out.")
TERMS = {"coin": {10}, "items": {"yarrow", "tincture"}, "step": None, "step_words": {"sorrel", "tack", "stable"}}


@pytest.fixture(autouse=True)
def fresh():
    job_offers.reset()
    reset_offer_state()
    yield
    job_offers.reset()
    reset_offer_state()


@pytest.fixture()
def world(db):
    with get_session() as session:
        session.add(WorldClock(day=1, hour=18, minute=0))
        square = Location(name="Market Square", type=LocationType.DISTRICT, discovered=True, visited=True)
        yard = Location(name="Stable Yard", type=LocationType.POI, discovered=True)
        session.add_all([square, yard])
        session.flush()
        piper = NPC(name="Piper Quillow", profession="Herbalist's apprentice", current_location_id=square.id)
        sorrel = NPC(name="Sorrel Tack", profession="Stable hand", current_location_id=yard.id)
        session.add_all([piper, sorrel])
        session.flush()
        player = Player(name="Bram Holloway", current_location_id=square.id, currency=25)
        session.add(player)
        session.flush()
        howls = Quest(
            title="Counting the Howls", status=QuestStatus.NOT_STARTED, assigned_by_npc_id=piper.id,
            description="Find who else is awake when the howls start, and bring her what you heard.",
            objectives=[
                {"text": "Ask Sorrel Tack what the stable yard hears after dark", "kind": "talk_to",
                 "target_id": sorrel.id},
                {"text": "Stand out the next howl and mark where it rises", "kind": "custom"},
                {"text": "Bring what you heard back to Piper", "kind": "talk_to", "target_id": piper.id},
            ],
            rewards={"currency": 10, "items": [{"name": "Yarrow Tincture", "kind": "potion",
                                                "about": "Piper's own: green, bitter."}]},
        )
        session.add(howls)
        session.commit()
        return {"player": player.id, "piper": piper.id, "sorrel": sorrel.id, "square": square.id,
                "yard": yard.id, "howls": howls.id}


def _pitch(w, text=PITCH):
    return job_offers.note_reply(w["player"], [{"npc_id": w["piper"], "npc_name": "Piper Quillow", "text": text}])


def _talk_to_sorrel(w, words='"Sorrel? Piper sent me. What do you hear after dark?"', started=()):
    with get_session() as session:
        session.get(Player, w["player"]).current_location_id = w["yard"]
        session.commit()
    record_turn_dialogue(w["player"], words, [{"npc_id": w["sorrel"], "npc_name": "Sorrel Tack",
                                               "text": "Four calls, spaced, like someone counting."}],
                         w["sorrel"], list(started))


def _quest(w):
    with get_session() as session:
        quest = session.get(Quest, w["howls"])
        return quest.status, {o["id"]: o["done"] for o in objectives.normalize_objectives(quest.objectives)}


# --- Reading the answer -------------------------------------------------------

@pytest.mark.parametrize("said, verdict", [
    # fleet7-bram T5, fleet7-wenna T3, fleet7-tamsin T5
    ('I nod at Piper. "Alright. I\'ll talk to him. But I\'m taking the pass first — no point asking '
     'questions if I can\'t get through the gate."', "accept"),
    ('"I\'ll find Sorrel at the stable yard tonight and ask him. I\'ll come back and tell you what he says."',
     "accept"),
    ('I nod once. I head toward the stables at a brisk walk.', "accept"),
    ('"Deal."', "accept"),
    ('"No. Find someone else."', "decline"),
    ('"Not now — I have the Captain\'s errand."', "decline"),
    ('"After," I say quietly. "We settle with the Captain first."', "decline"),       # fleet6-tamsin T15
    ('"I\'ll think about it."', "decline"),
    ('"Make it twenty and you have a deal."', "haggle"),
    ('"Fifteen coin and I\'ll do it."', "haggle"),
    ('"Ten coin and a tincture," I repeat. "What does Sorrel hear that matters?"', None),  # fleet7-tamsin T4
    ('I ask Piper about Sorrel.', None),
    ('"What\'s your name? And why does the stable boy matter?"', None),               # fleet7-bram T4
    # Plain refusals close it — missed, play could start a job the player
    # turned down; and the words that only look like a yes are none
    ('"No way."', "decline"),
    ('"Absolutely not."', "decline"),
    ('"Hell no. Ask the Watch."', "decline"),
    ('"Never."', "decline"),
    ('"Not a chance."', "decline"),
    ('"I\'d rather not."', "decline"),
    ('"Yeah, no."', "decline"),
    ('"I\'ll see him later, maybe." I head for the tavern.', None),        # a yes and a no: the DM reads it
    ('"Deal with it yourself."', None),
    ('"Done with you." I walk off.', None),
    ('"No way through the gate tonight anyway."', None),
    ('"Yeah, no problem."', "accept"),
    ('"Fine with me."', "accept"),
    # A question about the step sets out on nothing
    ('"Find Sorrel? Why would I?"', None),
    ('"Where do I find Sorrel?"', None),
    # fleet7-bram T2: a yes with a reason; "for you" is still a no
    ('"I\'ll take a look. I\'m not doing it for the Watch, mind."', "accept"),
    ('"I\'m not doing it for you."', "decline"),
    ('"I\'ll take a look around first."', None),
])
def test_the_answer_to_a_pitch(said, verdict):
    assert job_offers.read_answer(said, TERMS, age=0)[0] == verdict


def test_a_bare_yes_answers_only_a_giver_still_talking():
    yes = '"Alright. I\'ll talk to him."'
    assert job_offers.read_answer(yes, TERMS, age=1, fresh=True)[0] == "accept"     # asked a question first
    assert job_offers.read_answer(yes, TERMS, age=2, fresh=False)[0] is None
    # ...but setting out on the step, or a yes naming the job, holds for a few replies
    assert job_offers.read_answer("I make my way to the stable yard.", TERMS, age=3, fresh=False)[0] == "accept"
    assert job_offers.read_answer('"I\'ll do it."', TERMS, age=3, fresh=False)[0] == "accept"
    assert job_offers.read_answer('"I\'ll do it."', TERMS, age=job_offers.OFFER_WINDOW + 1)[0] is None


def test_taking_the_thing_the_giver_held_out_is_a_yes(world):
    _pitch(world, "Take this tincture now, and ten coin when you're back from the stable yard.")
    assert "I pocket the tincture" in job_offers.start_directive(world["player"], "I pocket the tincture with a nod.")
    job_offers.reset()
    _pitch(world, "Take this tincture now, and ten coin when you're back from the stable yard.")
    assert job_offers.start_directive(world["player"], "I take a long breath and look at the tincture.") == ""


def test_what_names_a_pitch():
    assert job_offers.amounts("Ten coin and a tincture") == {10}
    assert job_offers.amounts("twenty-five silver pieces") == {25} and job_offers.amounts("It pays ten.") == {10}
    assert job_offers.names_offer(PITCH, TERMS)
    assert job_offers.names_offer("I'll give you a tincture for your trouble.", TERMS)
    assert not job_offers.names_offer("I sell tinctures and sweetroot, nothing stronger.", TERMS)
    assert not job_offers.names_offer("Sorrel's out there most nights.", TERMS)


# --- The engine paths ---------------------------------------------------------

def test_a_yes_to_a_pitch_binds_the_start(world):
    assert _pitch(world) == ["Counting the Howls"]
    line = job_offers.start_directive(world["player"], '"Alright. I\'ll talk to him."')
    assert line.startswith("[START THIS JOB: @quest-start Counting the Howls — the player took it last turn")
    assert 'they said "Alright."' in line
    # Ignored, it is said once more, then the engine waits on play
    again = job_offers.start_directive(world["player"], "I look around the square.")
    assert "still not on their quest log" in again
    assert job_offers.start_directive(world["player"], "I look around the square.") == ""


@pytest.mark.parametrize("answer", ['"No. Find someone else."', '"Not now."', '"Make it twenty."'])
def test_a_refusal_or_a_haggle_never_starts_it(world, answer):
    _pitch(world)
    assert job_offers.start_directive(world["player"], answer) == ""
    if "twenty" not in answer:
        # Closed: a later "I'll do it" without a new pitch is not an answer to it,
        # and doing its step does not take it either
        assert job_offers.start_directive(world["player"], '"I\'ll do it."') == ""
        _talk_to_sorrel(world)
        assert job_offers.settle(world["player"]) == []
        assert _quest(world)[0] == QuestStatus.NOT_STARTED
    # A fresh pitch reopens it
    _pitch(world)
    assert "[START THIS JOB" in job_offers.start_directive(world["player"], '"Alright, deal."')


def test_a_haggle_holds_the_job_until_the_giver_answers(world):
    _pitch(world)
    assert job_offers.start_directive(world["player"], '"Make it twenty and I\'ll ask him."') == ""
    _talk_to_sorrel(world)                       # the same reply: the price is still open
    assert job_offers.open_offers(world["player"])[world["howls"]]["met"] == []
    job_offers.note_reply(world["player"], [{"npc_id": world["piper"], "text": "Fifteen. Not a copper more."}])
    assert job_offers.settle(world["player"]) == []
    assert "[START THIS JOB" in job_offers.start_directive(world["player"], '"Fine."')


def test_play_that_does_the_first_step_starts_the_job_and_credits_it(world):
    """fleet7-wenna: pitched T2, Sorrel asked T4-T7, never started."""
    _pitch(world)
    _talk_to_sorrel(world)
    taken = job_offers.settle(world["player"])
    assert [t["title"] for t in taken] == ["Counting the Howls"]
    assert taken[0]["steps"] == ["Ask Sorrel Tack what the stable yard hears after dark"]
    assert "never write @quest-start for it" in taken[0]["note"]
    status, done = _quest(world)
    assert status == QuestStatus.ACTIVE and done == {"o1": True, "o2": False, "o3": False}
    with get_session() as session:
        assert world["howls"] in session.get(Player, world["player"]).active_quests
    events = objectives.pop_pending_events(world["player"])
    assert [e["type"] for e in events] == ["quest_activated", "objective_done"]
    assert job_offers.settle(world["player"]) == []          # once


def test_a_talk_before_the_pitch_is_not_the_job(world):
    """fleet6-tamsin T4: the asking came a reply before Piper's offer."""
    _talk_to_sorrel(world)
    _pitch(world)
    assert job_offers.settle(world["player"]) == []
    assert _quest(world)[0] == QuestStatus.NOT_STARTED


def test_talking_to_the_giver_never_starts_her_job(world):
    """The report back to Piper waits for the steps before it."""
    _pitch(world)
    record_turn_dialogue(world["player"], '"Piper, about those howls."',
                         [{"npc_id": world["piper"], "npc_name": "Piper Quillow", "text": "Go on."}],
                         world["piper"], [])
    assert job_offers.settle(world["player"]) == []


def test_a_job_whose_first_step_is_its_giver_is_not_taken_by_talking_to_her(world):
    """forge_fresh's "Talk to Captain Coldiron at the Watch House", given by
    Coldiron: the offer IS that talk, so a question back to the giver after
    the pitch must not start the job."""
    with get_session() as session:
        quest = session.get(Quest, world["howls"])
        quest.objectives = [{"text": "Talk to Piper about the howls", "kind": "talk_to",
                             "target_id": world["piper"]}] + list(quest.objectives)
        session.commit()
    _pitch(world)
    assert job_offers.start_directive(world["player"], '"What does the tincture do, exactly?"') == ""
    record_turn_dialogue(world["player"], '"What does the tincture do, exactly?"',
                         [{"npc_id": world["piper"], "npc_name": "Piper Quillow", "text": "Steadies the hands."}],
                         world["piper"], [])
    assert job_offers.settle(world["player"]) == []
    assert _quest(world)[0] == QuestStatus.NOT_STARTED


def test_a_bare_yes_to_someone_else_is_not_a_yes_to_this_job(world):
    """"Yes, sir" to the Captain (their talk chip) beside the herbalist who
    just pitched: it answers him. Her own talk chip, or no chip, still counts."""
    _pitch(world)
    other = "some-captain-id"
    assert job_offers.start_directive(world["player"], '"Yes, sir."', addressed_npc_id=other) == ""
    assert job_offers.start_directive(world["player"], '"No."', addressed_npc_id=other) == ""
    assert job_offers.open_offers(world["player"])[world["howls"]]["status"] == "open"     # not closed
    assert "[START THIS JOB" in job_offers.start_directive(world["player"], '"Alright."',
                                                            addressed_npc_id=world["piper"])


def test_quest_start_credits_what_play_already_did(world):
    _pitch(world)
    _talk_to_sorrel(world)
    started = CommandRunner(world["player"], present_ids=[world["sorrel"]]).run("@quest-start Counting the Howls")
    assert started.ok and "already done in play since it was offered" in started.message
    assert "Ask Sorrel Tack" in started.message
    assert _quest(world)[1]["o1"] is True


def test_a_job_started_in_the_reply_after_its_pitch_ticks_that_replys_talk(world):
    """Pitched in an earlier reply, started this one: the talk came after the
    offer, so `note_conversation` does not hold the step back."""
    _pitch(world)
    CommandRunner(world["player"], present_ids=[world["piper"]]).run("@quest-start Counting the Howls")
    _talk_to_sorrel(world, started=[world["howls"]])
    assert _quest(world)[1]["o1"] is True


def test_a_job_started_without_a_pitch_still_holds_that_replys_talk(world):
    CommandRunner(world["player"], present_ids=[world["piper"]]).run("@quest-start Counting the Howls")
    _talk_to_sorrel(world, started=[world["howls"]])
    assert _quest(world)[1]["o1"] is False


# --- The context line ---------------------------------------------------------

def _fake(monkeypatch, offer_line: str = ""):
    monkeypatch.setattr(dm_orchestrator, "get_player", lambda pid: {"name": "Bram", "inventory": []})
    monkeypatch.setattr(dm_orchestrator, "appearance_lines", lambda pid: [])
    monkeypatch.setattr(dm_orchestrator, "run_world_tick", lambda *a, **k: {})
    monkeypatch.setattr(dm_orchestrator, "format_world_tick_context", lambda t: "")
    monkeypatch.setattr(dm_orchestrator, "run_campaign_tick", lambda pid: {})
    monkeypatch.setattr(dm_orchestrator, "format_campaign_context", lambda t: "### CAMPAIGN: spine")
    monkeypatch.setattr(dm_orchestrator, "no_spine_context", lambda pid: "", raising=False)
    monkeypatch.setattr(dm_orchestrator, "offer_directive", lambda *a, **k: offer_line)
    monkeypatch.setattr(job_offers, "start_directive", lambda pid, said, **kw: (
        "[START THIS JOB: @quest-start Counting the Howls — the player took it last turn (they said "
        "\"Alright.\"). Write that line first in this reply; the pay stands as offered. Never a second pitch "
        "of it.]" if "Alright" in said else ""))
    fake = SimpleNamespace(context=SimpleNamespace(player_id="p1"), _player_name="Bram")
    fake._player_block = lambda: ["### THE PLAYER"]
    fake._quest_block = lambda: ["### ACTIVE QUESTS", "- none"]
    return fake


def test_the_start_line_reaches_the_context_and_replaces_a_second_pitch(monkeypatch):
    pitch = '[OFFER THIS REPLY] Piper Quillow has work: "Counting the Howls" (it pays 10 coin).'
    context = DMOrchestrator._build_context(_fake(monkeypatch, pitch), '"Alright. I\'ll talk to him."')
    assert "[START THIS JOB: @quest-start Counting the Howls" in context
    assert "[OFFER THIS REPLY]" not in context and dm_orchestrator.NO_QUEST_ORDER not in context
    assert context.index("[START THIS JOB") < context.index("Player says:")
    other = DMOrchestrator._build_context(_fake(monkeypatch, pitch), "I look around.")
    assert "[OFFER THIS REPLY]" in other and "[START THIS JOB" not in other


def test_engine_turns_get_no_start_line(monkeypatch):
    fake = _fake(monkeypatch)
    for text in ("[COMBAT OUTCOME] Victory. Alright.", "[OPENING] Alright."):
        assert "[START THIS JOB" not in DMOrchestrator._build_context(fake, text)


# --- A pitch, and work nobody offered (fleet9-bram) --------------------------

def test_a_pitch_names_the_pay_or_asks_for_the_work_and_talk_is_neither(world):
    """fleet9-bram T2-T3: Piper talking about her counting, then pointing at
    Sorrel, was taken as the pitch of "Counting the Howls" — never offered."""
    with get_session() as session:
        quest = session.get(Quest, world["howls"])
        assert not job_offers.pitched(session, quest, "I've been counting the howls for a week. They're not "
                                                      "hungry-wrong. They're drilled-wrong.")
        assert not job_offers.pitched(session, quest, "Sorrel at the stable yard hears it worse than anyone. "
                                                      "He'd tell you more than he's told me, if you asked him proper.")
        # The ask and the job's words in one line, priced or not
        assert job_offers.pitched(session, quest, PITCH)
        assert job_offers.pitched(session, quest, "Will you count the howls with me tonight, from the fence?")
        # ...but an ask in one line and the howls in another is her chart, not the job
        assert not job_offers.pitched(session, quest, "I've marked the howls every night for three weeks.\n"
                                                      "Will you take it? Show someone who'll actually look?")
    # And the offer book only remembers a real pitch
    assert _pitch(world, "He'd tell you more than he's told me, if you asked him proper.") == []
    assert world["howls"] not in job_offers.open_offers(world["player"])


def test_work_nobody_offered_is_credit_and_never_a_start(world):
    """fleet9-bram T7: Bram asked Sorrel what the yard hears after dark for a
    job nobody had pitched. Remembered, offered next — not started for him."""
    _talk_to_sorrel(world)
    credit = job_offers.credited(world["player"])
    assert [s["text"] for s in credit[world["howls"]]] == ["Ask Sorrel Tack what the stable yard hears after dark"]
    assert job_offers.settle(world["player"]) == []
    assert _quest(world)[0] == QuestStatus.NOT_STARTED


@pytest.mark.parametrize("words, reply, credited", [
    # fleet6-tamsin T4: the ash-gray man's face; "hears" and "dark" only in
    # the narration around her question and in his answer
    ('I lean in, low enough that only he hears it. "The man in ash-gray — did you see his face?"',
     "I—no. Too dark, too far.", False),
    # fleet2-wenna T13: "hear" alone is no question about the night
    ('"I\'d like to hear what you know about the cloaked man, if you\'re willing."',
     "It was maybe five nights back, by the north wall.", False),
    # fleet9-bram T7: the job's own word, said by either of them
    ('"Piper said your horses know something. What exactly are they reacting to?"',
     "It's not just the howling — a sound under it.", True),
    # fleet9-tamsin T8: the step's own question
    ('"Piper says you hear things out here after dark."', "Then yeah, I've heard it.", True),
])
def test_only_a_talk_about_the_job_is_credit(world, words, reply, credited):
    """Any word with the stable hand was credit for "Ask Sorrel Tack what the
    stable yard hears after dark" — and the giver would have told the player
    they already asked him."""
    with get_session() as session:
        session.get(Player, world["player"]).current_location_id = world["yard"]
        session.commit()
    record_turn_dialogue(world["player"], words, [{"npc_id": world["sorrel"], "npc_name": "Sorrel Tack",
                                                   "text": reply}], world["sorrel"], [])
    assert (world["howls"] in job_offers.credited(world["player"])) is credited


def test_the_report_to_the_giver_is_never_work_nobody_offered(world):
    _talk_to_sorrel(world)
    with get_session() as session:
        session.get(Player, world["player"]).current_location_id = world["square"]
        session.commit()
    record_turn_dialogue(world["player"], '"Piper, Sorrel says the horses hear it first."',
                         [{"npc_id": world["piper"], "npc_name": "Piper Quillow", "text": "Does he now."}],
                         world["piper"], [])
    assert [s["id"] for s in job_offers.credited(world["player"])[world["howls"]]] == ["o1"]


def _howl(w, *, where, hour=20, told=True, visible=True):
    from src.models import Event

    with get_session() as session:
        session.add(Event(name="The Evening Howl", description="Howling rises from the northern hills.",
                          event_type="meso", scheduled_day=1, scheduled_hour=hour, occurred_day=1,
                          occurred_hour=hour, narrated_to_player=told, locations_involved=[where],
                          player_visible=visible))
        clock = session.query(WorldClock).first()
        clock.hour, clock.minute = hour, 15
        session.commit()


def test_a_howl_lived_through_is_the_vigil_step(world):
    """fleet9-bram T9: he stood out the 20:00 howl at the fence — the custom
    step "Stand out the next howl" of the job nobody had offered."""
    with get_session() as session:
        session.get(Player, world["player"]).current_location_id = world["yard"]
        session.commit()
    _howl(world, where=world["yard"])
    credit = job_offers.credited(world["player"])
    assert [s["id"] for s in credit[world["howls"]]] == ["o2"]
    assert job_offers.note_witnessed(world["player"]) == [], "read once"


@pytest.mark.parametrize("case", ["elsewhere", "untold", "long ago", "hidden"])
def test_a_howl_not_lived_through_is_no_vigil(world, case):
    """"hidden": an event the player never hears of is marked narrated the
    moment it fires (world_tick) — nobody stood it out."""
    with get_session() as session:
        session.get(Player, world["player"]).current_location_id = world["yard"]
        session.commit()
    _howl(world, where=world["square"] if case == "elsewhere" else world["yard"], told=case != "untold",
          visible=case != "hidden")
    if case == "long ago":
        with get_session() as session:
            session.query(WorldClock).first().day = 2
            session.commit()
    assert world["howls"] not in job_offers.credited(world["player"])


def test_what_a_vigil_step_is():
    howl = SimpleNamespace(name="The Evening Howl")
    assert job_offers.vigil_matches("Stand out the next howl and mark where it rises", howl)
    assert job_offers.vigil_matches("Listen for the howl from the paddock fence", howl)
    assert not job_offers.vigil_matches("Find out what is organizing the wolves", howl)
    assert not job_offers.vigil_matches("Ask about the howl at the inn", howl), "no witnessing verb"
    assert not job_offers.vigil_matches("Stand out the night at the gate", howl), "'evening' names nothing"
    cart = SimpleNamespace(name="The Greywater Cart Arrives Short")
    assert not job_offers.vigil_matches("Stand out the next howl and mark where it rises", cart)


def test_a_pitch_after_the_work_carries_it_and_the_start_ticks_it(world):
    """Credited work rides the pitch; it never starts the job by itself, and
    the start ticks it — then the report to the giver closes the job."""
    _talk_to_sorrel(world)
    _howl(world, where=world["yard"])
    assert {s["id"] for s in job_offers.credited(world["player"])[world["howls"]]} == {"o1", "o2"}
    with get_session() as session:
        session.get(Player, world["player"]).current_location_id = world["square"]
        session.commit()
    assert _pitch(world) == ["Counting the Howls"]
    assert job_offers.credited(world["player"]) == {}, "offered now: no second credited push"
    assert job_offers.open_offers(world["player"])[world["howls"]]["credit"] == ["o1", "o2"]
    assert job_offers.settle(world["player"]) == [], "credit alone never starts it"
    started = CommandRunner(world["player"], present_ids=[world["piper"]]).run("@quest-start Counting the Howls")
    assert started.ok
    assert _quest(world)[1] == {"o1": True, "o2": True, "o3": False}
    record_turn_dialogue(world["player"], '"Piper. It rose from the north ridge, three times."',
                         [{"npc_id": world["piper"], "npc_name": "Piper Quillow", "text": "Then I was right."}],
                         world["piper"], [world["howls"]])
    assert _quest(world)[1]["o3"] is True


def test_a_start_with_no_noted_pitch_still_ticks_the_credit(world):
    _talk_to_sorrel(world)
    runner = CommandRunner(world["player"], present_ids=[world["piper"]])
    runner.player_input = '"Alright, I\'ll take your job."'
    assert runner.run("@quest-start Counting the Howls").ok
    assert _quest(world)[1]["o1"] is True


def test_a_turned_down_job_earns_no_credit(world):
    _pitch(world)
    job_offers.start_directive(world["player"], '"No. Find someone else."')
    _talk_to_sorrel(world)
    assert job_offers.credited(world["player"]) == {}


def test_the_start_line_says_where_the_proof_lies_when_it_is_here(world):
    """fleet9-bram T18: the reply that wrote @quest-start had the Keeper send
    the player "upstairs, under the bed" for the satchel on her own floor."""
    from src.services.scene_objects import normalize_objects

    with get_session() as session:
        square = session.get(Location, world["square"])
        square.objects = normalize_objects([{
            "kind": "open", "label": "Lost traveler's satchel",
            "payload": {"lock": "ask her first", "item": {"name": "Water-Stained Letter", "type": "quest_item"}}}])
        letter = Quest(title="A Letter for a Buried Man", status=QuestStatus.NOT_STARTED,
                       assigned_by_npc_id=world["piper"], rewards={"currency": 20},
                       objectives=[{"text": "Take the letter from the lost traveler's satchel", "kind": "obtain",
                                    "item_name": "Water-Stained Letter"}])
        session.add(letter)
        session.commit()
        letter_id = letter.id
    _pitch(world, "Twenty coin to take the letter out of that satchel and see it delivered.")
    line = job_offers.start_directive(world["player"], '"Deal."')
    assert "[START THIS JOB: @quest-start A Letter for a Buried Man" in line
    assert ("PROOF HERE: Lost traveler's satchel holds the Water-Stained Letter this job is about" in line
            and "@interact Lost traveler's satchel" in line and "(locked: ask her first)" in line)
    with get_session() as session:
        assert "here, where the player stands" in job_offers.job_steps(session, session.get(Quest, letter_id),
                                                                      session.get(Player, world["player"]))

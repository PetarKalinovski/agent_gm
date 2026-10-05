"""Authored work is offered, by name and price, at the moments that need it.

fleet5-wenna T13-19: seven turns at Maera's bar and "A Letter for a Buried
Man" was never offered. fleet5-bram T19: right after @quest-done the DM
walked Bram away from Piper and her "Counting the Howls". fleet5-tamsin T0:
the job offer named no pay. The dossier line was passive; now the engine
names the moment in one binding line before the player's words.
"""

from types import SimpleNamespace

import pytest

from src.agents import dm_orchestrator
from src.agents.dm_orchestrator import (
    NO_QUEST_ORDER, OFFER_MAX, REPLY_BUDGET, DMOrchestrator, last_reply_text, offer_directive,
    reset_offer_state,
)
from src.models import (
    NPC, Faction, Location, LocationType, NPCRelationship, Player, Quest, QuestStatus, WorldClock, get_session,
)
from src.services.scene_brief import build_scene_brief


@pytest.fixture(autouse=True)
def fresh_offers():
    reset_offer_state()
    yield
    reset_offer_state()


def _world(session):
    """Market Square with Piper (met) and the Drowned Lantern inside it,
    where Maera (never met) keeps the bar."""
    session.add(WorldClock(day=1, hour=21, minute=0))
    square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True)
    session.add(square)
    session.flush()
    lantern = Location(name="The Drowned Lantern", type=LocationType.BUILDING, depth=1, parent_id=square.id,
                       discovered=True)
    watch = Faction(name="Lantern Watch")
    session.add_all([lantern, watch])
    session.flush()
    coldiron = NPC(name="Captain Brann Coldiron", profession="watch captain", current_location_id=square.id)
    piper = NPC(name="Piper Quillow", profession="herbalist's apprentice", current_location_id=square.id)
    hesk = NPC(name="Hesk Bramble", profession="merchant", current_location_id=square.id)
    maera = NPC(name="Maera Duskwell", profession="innkeeper", current_location_id=lantern.id)
    session.add_all([coldiron, piper, hesk, maera])
    session.flush()
    player = Player(name="Wenna Hale", current_location_id=square.id)
    session.add(player)
    session.flush()
    session.add(NPCRelationship(npc_id=piper.id, player_id=player.id, trust_level=50,
                                current_disposition="neutral"))
    teeth = Quest(title="Teeth on the North Road", status=QuestStatus.ACTIVE, assigned_by_npc_id=coldiron.id,
                  rewards={"currency": 60})
    howls = Quest(title="Counting the Howls", status=QuestStatus.NOT_STARTED, assigned_by_npc_id=piper.id,
                  description="Stand out the next howl and bring her what you heard.",
                  rewards={"currency": 10, "items": [{"name": "Yarrow Tincture", "kind": "potion",
                                                      "about": "Piper's own: green, bitter."}]})
    letter = Quest(title="A Letter for a Buried Man", status=QuestStatus.NOT_STARTED,
                   assigned_by_npc_id=maera.id, rewards={"currency": 20, "reputation": {watch.id: 5}})
    session.add_all([teeth, howls, letter])
    session.commit()
    return {"player": player.id, "piper": piper.id, "hesk": hesk.id, "maera": maera.id, "coldiron": coldiron.id,
            "teeth": teeth.id, "howls": howls.id, "letter": letter.id, "lantern": lantern.id}


@pytest.fixture()
def world(db):
    with get_session() as session:
        return _world(session)


def _here(world, *keys):
    names = {"piper": "Piper Quillow", "hesk": "Hesk Bramble", "coldiron": "Captain Brann Coldiron",
             "maera": "Maera Duskwell"}
    return {world[k]: names[k] for k in keys}


def test_a_giver_who_shares_the_scene_two_replies_offers_on_the_third(world):
    present = _here(world, "piper", "hesk")
    assert offer_directive(world["player"], present, "I look at the notice board.") == ""
    assert offer_directive(world["player"], present, "I ask about the howls.") == ""
    line = offer_directive(world["player"], present, "Anything strange lately?")
    assert line.startswith("[OFFER THIS REPLY] Piper Quillow has work: \"Counting the Howls\"")
    assert "it pays 10 coin and a Yarrow Tincture" in line
    assert "write @quest-start Counting the Howls only when the player accepts" in line


def test_the_push_stops_once_the_reply_names_the_job(world):
    present = _here(world, "piper")
    for _ in range(2):
        offer_directive(world["player"], present, "I wait.")
    assert offer_directive(world["player"], present, "I wait.")
    reply = 'Piper Quillow (eager): "Would you help me with Counting the Howls? It pays ten."'
    assert offer_directive(world["player"], present, "Maybe.", reply) == ""
    assert offer_directive(world["player"], present, "Got any work?", reply) == ""


def test_the_push_stops_after_two_unanswered_offers(world):
    present = _here(world, "piper")
    lines = [offer_directive(world["player"], present, "I wait.") for _ in range(5)]
    assert [bool(x) for x in lines] == [False, False, True, True, False]
    assert OFFER_MAX == 2


def test_the_push_stops_once_the_quest_starts(world):
    present = _here(world, "piper")
    for _ in range(2):
        offer_directive(world["player"], present, "I wait.")
    with get_session() as session:
        session.get(Quest, world["howls"]).status = QuestStatus.ACTIVE
        session.commit()
    assert offer_directive(world["player"], present, "I wait.") == ""


def test_asking_for_work_gets_the_offer_at_once(world):
    for asked in ("Got any work for someone like me?", "How can I earn a few coin?",
                  "Is there anything I can do to help?", "What would you want for it?"):
        reset_offer_state()
        assert "Counting the Howls" in offer_directive(world["player"], _here(world, "piper"), asked), asked
    reset_offer_state()
    assert offer_directive(world["player"], _here(world, "piper"), "I watch the gate.") == ""


@pytest.mark.parametrize("said", [
    # Every one of these was a hit of the first pattern in the fleet transcripts
    "The fog works in my favor — less chance of being spotted from a distance.",
    "testing which direction they favor, looking for the gap",
    "This smells like a setup—the Watch asking favors.",
    "I meet her eyes straight, because she's earned honesty.",
    "you need to decide what Vane's name means before we talk",
    "Piper's found something you need to hear about where those wolves are heading.",
    "letting her work while I consider how much truth to trade for her goodwill",
    "What do you want with the town, and why haven't you just taken it?",
    "Let me help you up.",
    "I need a hand here!",
])
def test_words_that_only_sound_like_asking_for_work_offer_nothing(world, said):
    assert offer_directive(world["player"], _here(world, "piper"), said) == ""


def test_more_ways_of_asking_for_work(world):
    for asked in ("I came through looking for work.", "I'm here to help.", "Need a hand with anything?",
                  "Know anyone hiring?", "I'd trade you a favour for it.", "What do you need?"):
        reset_offer_state()
        assert "Counting the Howls" in offer_directive(world["player"], _here(world, "piper"), asked), asked


def test_a_giver_who_spoke_when_told_to_offer_is_not_pushed_again(world):
    """The DM offers in dialogue, never by title: the push after a decline
    would have Piper pitch the job again over the player's 'no'."""
    present = _here(world, "piper")
    assert offer_directive(world["player"], present, "Got any work?")
    reply = ('Piper Quillow (eager): "Count the howls with me tonight? Ten coin and a tincture."\n'
             "She holds out her chart.")
    assert offer_directive(world["player"], present, "Not tonight, sorry.", reply) == ""
    assert offer_directive(world["player"], present, "Got any work, then?", reply) == ""


def test_a_stranger_giver_speaking_under_their_role_label_counts(world):
    with get_session() as session:
        session.query(NPCRelationship).filter_by(npc_id=world["piper"]).delete()
        session.commit()
    present = _here(world, "piper")
    assert offer_directive(world["player"], present, "Got any work?",
                           strangers={world["piper"]: "Herbalist's Apprentice"})
    reply = 'Herbalist\'s Apprentice (nervous): "I could use someone to count the howls."'
    assert offer_directive(world["player"], present, "Maybe.", reply,
                           strangers={world["piper"]: "Herbalist's Apprentice"}) == ""


def test_a_reply_where_the_giver_never_spoke_gets_the_push_again(world):
    present = _here(world, "piper", "hesk")
    for _ in range(2):
        offer_directive(world["player"], present, "I wait.")
    assert offer_directive(world["player"], present, "I wait.")
    reply = 'Hesk Bramble (bored): "Rope, lamp oil, nothing else."'
    assert "Counting the Howls" in offer_directive(world["player"], present, "I wait.", reply)


def test_a_pointer_to_a_giver_away_spends_no_push_and_the_offer_comes_on_arrival(world):
    assert offer_directive(world["player"], {}, "I sit.") == ""
    with get_session() as session:
        session.get(Quest, world["teeth"]).status = QuestStatus.COMPLETED
        session.commit()
    for _ in range(2):      # the payoff window points at Maera, inside the inn
        assert "a step away at The Drowned Lantern" in offer_directive(world["player"], _here(world, "hesk"), "Hm.")
    assert offer_directive(world["player"], _here(world, "hesk"), "Hm.") == "", "the window has closed"
    # The player walks in: she offers on the first reply, no streak to wait out
    line = offer_directive(world["player"], _here(world, "maera"), "I take a stool at the bar.")
    assert "has work: \"A Letter for a Buried Man\"" in line and "a step away" not in line


def test_a_pointer_from_an_empty_square_invents_nobody(world):
    offer_directive(world["player"], {}, "I sit.")
    with get_session() as session:
        session.get(Quest, world["teeth"]).status = QuestStatus.COMPLETED
        session.commit()
    line = offer_directive(world["player"], {}, "What now?")
    assert "a step away at The Drowned Lantern" in line
    assert "someone here mentions" not in line and "never a passer-by you invent" in line


def test_a_payoff_offers_the_next_job_from_someone_present(world):
    present = _here(world, "coldiron", "piper")
    assert offer_directive(world["player"], {}, "I head back to the square.") == ""
    with get_session() as session:
        session.get(Quest, world["teeth"]).status = QuestStatus.COMPLETED
        session.commit()
    line = offer_directive(world["player"], present, "I count my coin.")
    assert "Piper Quillow has work: \"Counting the Howls\"" in line


def test_a_payoff_points_at_a_giver_a_step_inside(world):
    assert offer_directive(world["player"], {}, "I sit.") == ""
    with get_session() as session:
        session.get(Quest, world["teeth"]).status = QuestStatus.COMPLETED
        session.commit()
    # Piper is off at the stalls: nobody standing here has work
    line = offer_directive(world["player"], _here(world, "hesk"), "What now?")
    assert "a step away at The Drowned Lantern" in line and "\"A Letter for a Buried Man\"" in line
    assert "it pays 20 coin and standing with the Lantern Watch" in line
    # Maera never introduced herself: the DM is told to keep her role label
    assert line.startswith("[OFFER THIS REPLY] the Innkeeper (Maera Duskwell — a name the player has not learned")


def test_new_work_closes_the_payoff_window(world):
    offer_directive(world["player"], {}, "I sit.")
    with get_session() as session:
        session.get(Quest, world["teeth"]).status = QuestStatus.COMPLETED
        session.add(Quest(title="Something Else", status=QuestStatus.ACTIVE))
        session.commit()
    assert offer_directive(world["player"], _here(world, "piper"), "I sit.") == ""


def test_a_firing_beat_holds_the_streak_push_but_not_a_request(world):
    present = _here(world, "piper")
    for _ in range(2):
        offer_directive(world["player"], present, "I wait.")
    assert offer_directive(world["player"], present, "I wait.", beat_firing=True) == ""
    assert offer_directive(world["player"], present, "Need a hand with anything?", beat_firing=True)


def test_someone_who_will_not_talk_offers_nothing(world):
    with get_session() as session:
        rel = session.query(NPCRelationship).filter_by(npc_id=world["piper"]).one()
        rel.current_disposition, rel.trust_level = "hostile", 5
        session.commit()
    assert offer_directive(world["player"], _here(world, "piper"), "Got any work?") == ""


def test_the_dossier_names_the_pay(world):
    with get_session() as session:
        brief = build_scene_brief(session, session.get(Player, world["player"]))
    text = brief["text"]
    assert '"Counting the Howls" [' in text
    assert ('(pays 10 coin and a Yarrow Tincture (say "a Yarrow Tincture"; handed over finished, the player\'s to '
            "keep — Piper's own: green, bitter))") in text
    assert "they name the pay when they offer it" in text


def test_last_reply_text_reads_the_previous_reply_only():
    agent = SimpleNamespace(messages=[
        {"role": "user", "content": [{"text": "Player says: hi"}]},
        {"role": "assistant", "content": [{"text": "Old reply."}]},
        {"role": "user", "content": [{"text": "Player says: and?"}]},
        {"role": "assistant", "content": [{"toolUse": {"name": "roll_check"}}]},
        {"role": "user", "content": [{"toolResult": {"content": []}}]},
        {"role": "assistant", "content": [{"text": "Piper offers Counting the Howls."}]},
    ])
    assert last_reply_text(agent) == "Piper offers Counting the Howls."
    assert last_reply_text(None) == ""


# --- Where the line goes ------------------------------------------------------

def _fake(monkeypatch, offers: list):
    monkeypatch.setattr(dm_orchestrator, "get_player", lambda pid: {"name": "Wenna", "inventory": []})
    monkeypatch.setattr(dm_orchestrator, "appearance_lines", lambda pid: [])
    monkeypatch.setattr(dm_orchestrator, "run_world_tick", lambda *a, **k: {})
    monkeypatch.setattr(dm_orchestrator, "format_world_tick_context", lambda t: "")
    monkeypatch.setattr(dm_orchestrator, "run_campaign_tick", lambda pid: {})
    monkeypatch.setattr(dm_orchestrator, "format_campaign_context", lambda t: "### CAMPAIGN: spine")
    monkeypatch.setattr(dm_orchestrator, "no_spine_context", lambda pid: "", raising=False)

    def offer(*args, **kwargs):
        offers.append((args, kwargs))
        return '[OFFER THIS REPLY] Piper Quillow has work: "Counting the Howls".'

    monkeypatch.setattr(dm_orchestrator, "offer_directive", offer)
    fake = SimpleNamespace(context=SimpleNamespace(player_id="p1"), _player_name="Wenna")
    fake._player_block = lambda: ["### THE PLAYER"]
    fake._quest_block = lambda: ["### ACTIVE QUESTS", "- none"]
    return fake


def test_the_offer_sits_just_before_the_reply_budget_and_replaces_the_vague_order(monkeypatch):
    offers: list = []
    context = DMOrchestrator._build_context(_fake(monkeypatch, offers), "Got any work?")
    assert context.index("[OFFER THIS REPLY]") < context.index(REPLY_BUDGET) < context.index("Player says:")
    assert NO_QUEST_ORDER not in context, "the exact offer outranks the general no-quest order"
    assert offers and offers[0][0][2] == "Got any work?"


def test_engine_turns_never_get_an_offer(monkeypatch):
    offers: list = []
    fake = _fake(monkeypatch, offers)
    for text, constrained in (("[OPENING] first moment", False), ("[COMBAT OUTCOME] Victory.", False),
                              ("I ask his name.", True)):
        assert "[OFFER THIS REPLY]" not in DMOrchestrator._build_context(fake, text, constrained=constrained)
    assert offers == []


# --- fleet9: what counts as offered, work done unasked, the steps -------------

@pytest.fixture()
def jobs(world):
    """Piper's job with its steps, Sorrel at the yard, and a fresh offer book."""
    from src.services import job_offers

    job_offers.reset()
    with get_session() as session:
        square = session.get(Player, world["player"]).current_location_id
        yard = Location(name="Stable Yard", type=LocationType.POI, depth=1, parent_id=square, discovered=True)
        session.add(yard)
        session.flush()
        sorrel = NPC(name="Sorrel Tack", profession="Stable hand", current_location_id=yard.id)
        session.add(sorrel)
        session.flush()
        session.get(Quest, world["howls"]).objectives = [
            {"text": "Ask Sorrel Tack what the stable yard hears after dark", "kind": "talk_to",
             "target_id": sorrel.id},
            {"text": "Stand out the next howl and mark where it rises", "kind": "custom"},
            {"text": "Bring what you heard back to Piper", "kind": "talk_to", "target_id": world["piper"]},
        ]
        session.commit()
        world = dict(world, sorrel=sorrel.id, yard=yard.id)
    yield world
    job_offers.reset()


def test_a_giver_who_only_talked_is_pushed_again(jobs):
    """fleet9-bram T2-T3: Piper's "I've been counting the howls for a week"
    read as the title, then she only pointed at Sorrel — counted as the offer,
    and "Counting the Howls" was never pushed again nor ever offered."""
    present = _here(jobs, "piper")
    for _ in range(2):
        offer_directive(jobs["player"], present, "I wait.")
    assert "Counting the Howls" in offer_directive(jobs["player"], present, "I wait.")
    talk = ('Piper Quillow (quick): "I\'ve been counting the howls for a week."\n'
            'Piper Quillow (quick): "Sorrel at the stable yard hears it worse than anyone — he\'d tell you more, '
            'if you asked him proper."')
    assert "Counting the Howls" in offer_directive(jobs["player"], present, "Sorrel. Got it.", talk)


def test_the_offer_line_carries_the_job_as_written(jobs):
    """fleet9-bram T17-T18: a pitch that made up a room and a key and said
    "look, don't take" against its own steps."""
    line = offer_directive(jobs["player"], _here(jobs, "piper"), "Got any work?")
    assert ("The job, as written: 1) Ask Sorrel Tack what the stable yard hears after dark — Sorrel Tack, at "
            "Stable Yard; 2) Stand out the next howl and mark where it rises; 3) Bring what you heard back to "
            "Piper.") in line
    assert "nothing they say contradicts them (no other room, no key, no rule of their own)" in line
    assert "write @quest-start Counting the Howls only when the player accepts" in line


def test_work_done_unasked_is_offered_on_the_givers_next_appearance_crediting_it(jobs):
    """fleet9-bram: Sorrel asked at T7, the howl stood at T9, and Piper —
    in the square from T11 — never offered the job; 0 XP for the work."""
    from src.services import job_offers
    from src.services.dialogue import record_turn_dialogue

    with get_session() as session:
        session.get(Player, jobs["player"]).current_location_id = jobs["yard"]
        session.commit()
    record_turn_dialogue(jobs["player"], '"Sorrel, what does the yard hear after dark?"',
                         [{"npc_id": jobs["sorrel"], "npc_name": "Sorrel Tack", "text": "A whistle, under it."}],
                         jobs["sorrel"], [])
    # Not while she is elsewhere
    assert offer_directive(jobs["player"], {jobs["sorrel"]: "Sorrel Tack"}, "I wait by the fence.") == ""
    # Her first reply back in the scene: no streak to wait out, the work credited
    line = offer_directive(jobs["player"], _here(jobs, "piper"), "I head back to the square.")
    assert line.startswith("[OFFER THIS REPLY] Piper Quillow has work: \"Counting the Howls\" (it pays 10 coin")
    assert 'the player ALREADY did part of it, unasked: "Ask Sorrel Tack what the stable yard hears after dark"' in line
    assert "still to do: 2) Stand out the next howl and mark where it rises; 3) Bring what you heard back" in line
    assert "the engine ticks what they already did" in line
    # A beat firing holds it a reply; a real pitch ends it
    assert offer_directive(jobs["player"], _here(jobs, "piper"), "Hm.", beat_firing=True) == ""
    job_offers.note_reply(jobs["player"], [{"npc_id": jobs["piper"], "npc_name": "Piper Quillow",
                                            "text": "You asked him already? Ten coin and a tincture if you stand "
                                                    "out the howl with me."}])
    pitched = 'Piper Quillow (eager): "You asked him already? Ten coin and a tincture if you stand out the howl."'
    assert offer_directive(jobs["player"], _here(jobs, "piper"), "Maybe.", pitched) == ""


def test_the_credited_offer_stops_after_two_unanswered_pushes(jobs):
    from src.services.dialogue import record_turn_dialogue

    with get_session() as session:
        session.get(Player, jobs["player"]).current_location_id = jobs["yard"]
        session.commit()
    record_turn_dialogue(jobs["player"], '"Sorrel, what does the yard hear after dark?"',
                         [{"npc_id": jobs["sorrel"], "npc_name": "Sorrel Tack", "text": "A whistle, under it."}],
                         jobs["sorrel"], [])
    lines = [offer_directive(jobs["player"], _here(jobs, "piper"), "I wait.") for _ in range(4)]
    assert ["ALREADY did part of it" in x for x in lines[:2]] == [True, True]
    assert all("ALREADY" not in x for x in lines[2:])


def test_a_job_already_worked_is_never_pushed_as_fresh_work(jobs):
    """Past its two credited pushes, the streak push used to offer the same
    job plainly — "1) Ask Sorrel Tack…" to a player who had asked him. Only
    the credited line offers it, and the player asking for work gets it even
    while a beat fires."""
    from src.services.dialogue import record_turn_dialogue

    with get_session() as session:
        session.get(Player, jobs["player"]).current_location_id = jobs["yard"]
        session.commit()
    record_turn_dialogue(jobs["player"], '"Sorrel, what does the yard hear after dark?"',
                         [{"npc_id": jobs["sorrel"], "npc_name": "Sorrel Tack", "text": "The howls, every night."}],
                         jobs["sorrel"], [])
    lines = [offer_directive(jobs["player"], _here(jobs, "piper"), "I wait.") for _ in range(5)]
    assert ["ALREADY did part of it" in x for x in lines[:2]] == [True, True]
    assert lines[2:] == ["", "", ""], "never the plain push of a job the player already worked"
    asked = offer_directive(jobs["player"], _here(jobs, "piper"), "Got any work for me?", beat_firing=True)
    assert "ALREADY did part of it" in asked and "Counting the Howls" in asked


def test_the_dossier_credits_work_already_done(jobs):
    """Past the credited pushes the dossier is all the DM has: its "Work they
    can offer" must not ask the player to ask Sorrel again."""
    from src.services.dialogue import record_turn_dialogue

    with get_session() as session:
        session.get(Player, jobs["player"]).current_location_id = jobs["yard"]
        session.commit()
    record_turn_dialogue(jobs["player"], '"Sorrel, what does the yard hear after dark?"',
                         [{"npc_id": jobs["sorrel"], "npc_name": "Sorrel Tack", "text": "The howls, every night."}],
                         jobs["sorrel"], [])
    with get_session() as session:
        player = session.get(Player, jobs["player"])
        player.current_location_id = session.get(NPC, jobs["piper"]).current_location_id
        session.commit()
        text = build_scene_brief(session, player)["text"]
    line = next(x for x in text.splitlines() if '"Counting the Howls" [' in x)
    assert ('Already done by the player, unasked (credit it; the start ticks it): "Ask Sorrel Tack what the '
            'stable yard hears after dark".') in line
    assert "Steps left: 2) Stand out the next howl" in line and "1) Ask Sorrel" not in line

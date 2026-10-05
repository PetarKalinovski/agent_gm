"""Progress you can feel, kindness that counts, no spoilers (round 9 critics, fleet10).

  * Investigation paid nothing until a quest closed: 21 turns, three of four
    main-quest steps done, and the bar moved only for three weak wolves
    (fleet10-bram; tamsin and wenna ended on level 1). A met step pays a
    slice of its quest's XP, out of the quest's own total.
  * Predator bodies "carry nothing worth taking" (bram T13/T17): a beast
    leaves a fang or a pelt the counter buys — never one of the story's cast.
  * Nothing on screen said what the open job pays: the lead carries it.
  * Kindness earned nothing (fleet10-wenna T5/T12: every trust stayed 50, and
    the stable hand's clue stayed locked though she asked him exactly at T7).
  * The thief persona's want never reached play (fleet10-tamsin).
  * The quest log named the proof from T1 (fleet10-bram T1, -tamsin T2).
"""

import asyncio
import json

import pytest

from src.data.playtest_seed import create_playtest_world
from src.models import (
    NPC, Campaign, Location, LocationType, NPCRelationship, Player, Quest, QuestStatus, StoryBeat, WorldClock,
    get_session, reset_engine,
)
from src.services import memory_files, progression, standing
from src.services import objectives as obj
from src.services.commands import CommandRunner, gift_in_words


@pytest.fixture()
def mem(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_files, "memory_root", lambda: tmp_path / "memory")
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)


@pytest.fixture()
def town(db, mem):
    with get_session() as session:
        session.add(WorldClock(day=1, hour=15, minute=0))
        square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True, visited=True)
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True)
        session.add_all([square, hollow])
        session.flush()
        brann = NPC(name="Captain Brann Coldiron", profession="Captain of the Lantern Watch",
                    current_location_id=square.id)
        sorrel = NPC(name="Sorrel Tack", profession="Stable hand", current_location_id=square.id,
                     secrets=["He saw a man in an ash-gray cloak crouched by the north wall two nights ago, "
                              "and told no one because no one asks Sorrel"])
        session.add_all([brann, sorrel])
        player = Player(name="Wenna Cole", current_location_id=square.id, currency=25)
        session.add(player)
        session.flush()
        quest = Quest(title="Teeth on the North Road", status=QuestStatus.ACTIVE, scale="mission",
                      assigned_by_npc_id=brann.id, description="Walk up to the Hollow and bring back proof.",
                      rewards={"currency": 60, "items": [{"name": "Wolf-Tooth Charm", "kind": "trinket"}]},
                      objectives=obj.normalize_objectives([
                          {"text": "Reach Wolf's Hollow", "kind": "go_to", "target_id": hollow.id},
                          {"text": "Find out what is organizing the wolves", "kind": "custom"},
                          {"text": "Bring proof back to Captain Coldiron", "kind": "deliver",
                           "item_name": "Cracked Bone Whistle", "target_id": brann.id},
                      ], session=session))
        session.add(quest)
        session.flush()
        player.active_quests = [quest.id]
        session.commit()
        return {"player": player.id, "square": square.id, "hollow": hollow.id, "brann": brann.id,
                "sorrel": sorrel.id, "quest": quest.id}


@pytest.fixture()
def seed(tmp_path, mem):
    """The Emberfall seed with a player in the square and the main job taken."""
    from src.tools.world_write.quests import activate_quest

    reset_engine()
    create_playtest_world(str(tmp_path / "emberfall.db"))
    with get_session() as session:
        square = session.query(Location).filter_by(name="Market Square").one()
        hollow = session.query(Location).filter_by(name="Wolf's Hollow").one()
        teeth = session.query(Quest).filter_by(title="Teeth on the North Road").one()
        player = Player(name="Tamsin Rook", current_location_id=square.id, currency=25)
        session.add(player)
        session.commit()
        ids = {"player": player.id, "square": square.id, "hollow": hollow.id, "teeth": teeth.id,
               "brann": session.query(NPC).filter(NPC.name.like("%Coldiron%")).one().id}
    activate_quest(ids["teeth"])
    yield ids
    reset_engine()


def _xp(player_id):
    with get_session() as session:
        return progression.player_xp(session.get(Player, player_id))


# ----------------------------------------------------------------------
# 1. A met step pays a slice of its quest's XP
# ----------------------------------------------------------------------

def test_the_slices_come_out_of_the_quest_s_own_total():
    quest = Quest(title="Teeth", scale="mission")                 # 220 all told, 110 by the steps
    assert progression.step_xp(quest, 4, 1) == 27
    quest.step_xp_paid = 81
    assert progression.step_xp(quest, 4, 1) == 27
    quest.step_xp_paid = 108
    assert progression.step_xp(quest, 4, 1) == 2, "never past the step share"
    assert progression.quest_completion_xp(quest) == 220 - 108
    legacy = Quest(title="Old")                                   # NULL scale, NULL paid: the old game
    assert progression.quest_completion_xp(legacy) == progression.QUEST_XP["job"]
    assert progression.step_xp(legacy, 0, 1) == 0


def test_reaching_a_step_moves_the_bar_and_completion_pays_the_rest(town):
    """fleet10-bram: three of four steps done, 0 XP from them."""
    from src.tools.world_write.quests import update_quest_status

    obj.pop_pending_events(town["player"])
    obj.note_arrival(town["player"], town["hollow"])
    assert _xp(town["player"]) == 36                              # 110 // 3 steps
    events = obj.pop_pending_events(town["player"])
    assert [e["type"] for e in events] == ["objective_done"]
    assert events[0]["progression"]["xp_gained"] == 36 and events[0]["progression"]["source"] == "quest_step"
    with get_session() as session:
        assert session.get(Quest, town["quest"]).step_xp_paid == 36
    done = update_quest_status(town["quest"], QuestStatus.COMPLETED)
    assert done["progression"]["xp_gained"] == 220 - 36
    assert _xp(town["player"]) == 220, "the quest pays its scale all told, never more"


def test_a_custom_step_the_dm_rules_pays_too(town):
    """fleet10-bram T18: @step-done … "Find out what is organizing the wolves" ticked with no xp_gain."""
    r = CommandRunner(town["player"])
    out = r.run("@step-done Teeth on the North Road | Find out what is organizing the wolves")
    assert out.ok, out.message
    assert _xp(town["player"]) == 36
    event = next(e for e in obj.pop_pending_events(town["player"]) if e["type"] == "objective_done")
    assert event["progression"]["xp_gained"] == 36


# ----------------------------------------------------------------------
# 2. A beast's trophy, and what the open job pays
# ----------------------------------------------------------------------

def test_a_beast_leaves_a_trophy_the_counter_buys(town):
    """fleet10-bram T13/T17: the wolves "carry nothing worth taking"."""
    from src.services.loot import _story_cast, beast_trophy, body_payload

    with get_session() as session:
        weak = NPC(name="Torn-Ear Wolf", species="wolf", danger_tier="weak", current_location_id=town["hollow"])
        boar = NPC(name="Old Tusker", species="boar", danger_tier="standard", current_location_id=town["hollow"])
        deer = NPC(name="Red Deer", species="deer", danger_tier="harmless", current_location_id=town["hollow"])
        cast = NPC(name="Hollow Wolf", species="wolf", danger_tier="weak", current_location_id=town["hollow"])
        session.add_all([weak, boar, deer, cast])
        session.flush()
        campaign = Campaign(player_id=town["player"], title="Teeth", status="active")
        session.add(campaign)
        session.flush()
        session.add(StoryBeat(campaign_id=campaign.id, title="The Pack Answers", act=3, status="pending",
                              directive="Hollow Wolf runs the North Gatehouse's perimeter at dusk."))
        session.commit()
        fang = beast_trophy(weak)
        assert fang["name"] == "Wolf Fang" and fang["value"] == progression.BEAST_TROPHY_VALUE["weak"]
        assert standing.sell_price(standing.base_price(fang)) == 4          # what Hesk pays for it
        assert beast_trophy(boar)["name"] == "Boar Tusk"
        assert beast_trophy(deer) is None, "harmless game is no trophy"
        assert [i["name"] for i in body_payload(weak)["items"]] == ["Wolf Fang"]
        # The story's own cast leaves only what it carried
        assert _story_cast(session, cast) and not _story_cast(session, weak)
        assert body_payload(cast, story_cast=True)["items"] == []


def test_the_lead_says_what_the_job_pays(town):
    """fleet10 (all runs): 25 coin start to end, and nothing said what the open job brings."""
    from src.services.lead import compute_lead, format_lead_context, payout_words

    with get_session() as session:
        lead = compute_lead(session, session.get(Player, town["player"]))
    assert lead["text"] == "Reach Wolf's Hollow", "the step itself stays the chip's words"
    assert lead["pays"] == "60 coin and a Wolf-Tooth Charm"
    assert '"Reach Wolf\'s Hollow" — pays 60 coin and a Wolf-Tooth Charm' in format_lead_context(lead)
    # Paid up front, the balance; nothing left, no line
    assert payout_words({"currency": 60, "advanced": {"currency": 20}}) == "40 coin"
    assert payout_words({"reputation": {"x": 5}}) == ""


# ----------------------------------------------------------------------
# 3. Kindness counts
# ----------------------------------------------------------------------

T12 = ("I pull out two coin and press them into Sorrel's hand. \"Thank you for the truth, and for marking "
       "the howls. If I make it back through here, I'll buy you a drink.\"")


def _trust(town, key="sorrel"):
    with get_session() as session:
        rel = session.query(NPCRelationship).filter_by(npc_id=town[key], player_id=town["player"]).first()
        return rel.trust_level if rel else None


def _runner(town, text):
    r = CommandRunner(town["player"], present_ids=[town["brann"], town["sorrel"]])
    r.player_input = text
    return r


def test_gift_words_are_not_a_price():
    assert gift_in_words(T12)                                                   # "buy you a drink" is a treat
    assert gift_in_words("I reach into my coin purse and pull out a copper, pressing it into his palm.")
    assert not gift_in_words("\"Room for the night.\" I drop coin on the counter without haggling.")
    assert not gift_in_words("I slip the guard a coin to look the other way.")
    assert not gift_in_words("I need bandages, needle, thread. I fish out a few coins.")


def test_a_gift_in_her_own_words_raises_his_trust_once_a_day(town):
    """fleet10-wenna T12: `@coin -2 | a thank-you pressed into Sorrel's hand`, trust stayed 50."""
    out = _runner(town, T12).run("@coin -2 | a thank-you pressed into Sorrel's hand")
    assert out.ok and _trust(town) == 50 + standing.GIFT_TRUST
    assert "took the 2 coin as a kindness" in out.message and "never write @trust" in out.message
    assert out.result["gift_trust"]["npc_id"] == town["sorrel"]
    # Once a game-day: the second gift today is coin, not trust
    again = _runner(town, T12).run("@coin -1 | another for Sorrel")
    assert again.ok and "gift_trust" not in again.result and _trust(town) == 55
    with get_session() as session:
        session.query(WorldClock).first().day = 2
        session.commit()
    assert _runner(town, T12).run("@coin -1 | for Sorrel").ok and _trust(town) == 60


def test_the_gift_is_capped_and_a_price_is_no_gift(town):
    with get_session() as session:
        session.add(NPCRelationship(npc_id=town["sorrel"], player_id=town["player"],
                                    trust_level=standing.GIFT_TRUST_CAP))
        session.commit()
    _runner(town, T12).run("@coin -2 | for Sorrel")
    assert _trust(town) == standing.GIFT_TRUST_CAP, "gifts alone never buy more"
    _runner(town, "\"How much for the rope?\" I hand over the coin.").run("@coin -3 | rope from Sorrel")
    assert _trust(town) == standing.GIFT_TRUST_CAP


def test_a_gift_and_the_dm_s_own_trust_for_it_are_one_kindness(town):
    r = _runner(town, T12)
    assert r.run("@coin -2 | pressed into Sorrel's hand").ok
    twice = r.run("@trust Sorrel +5")
    assert not twice.ok and "already raised their trust for the gift" in twice.message
    assert _trust(town) == 55
    # ...and the other way round: the DM's @trust first, the gift adds nothing
    r = _runner(town, T12)
    with get_session() as session:
        session.query(WorldClock).first().day = 2
        session.commit()
    r.run("@trust Sorrel +5")
    out = r.run("@coin -1 | for Sorrel")
    assert "gift_trust" not in out.result and _trust(town) == 60


def test_the_toast_names_only_what_was_heard(town):
    from src.web.server import _kindness_frame

    out = _runner(town, T12).run("@coin -2 | pressed into Sorrel's hand")
    frame = _kindness_frame(town["player"], out)
    event = json.loads(frame[len("data: "):])
    assert event["type"] == "kindness" and event["text"] == "The Stable Hand won't forget that."
    assert "Sorrel" not in frame and "5" not in event["text"], "no unheard name, no number"
    heard = _kindness_frame(town["player"], out, heard_now='"Name\'s Sorrel."')
    assert json.loads(heard[len("data: "):])["text"] == "Sorrel won't forget that."


T7 = ("I write down what he's told me, then look up from my notebook. \"Sorrel, you looked north when you "
      "mentioned the howls. Have you seen something up there, or heard something besides the wolves?\"")


def test_asking_about_what_he_hides_puts_it_within_reach(town):
    """fleet10-wenna T7: asked exactly, trust 50 of 60 — "I haven't seen anything", clue locked."""
    from src.services.scene_brief import asked_about, build_scene_brief

    secret = "He saw a man in an ash-gray cloak crouched by the north wall two nights ago"
    assert asked_about(T7, secret, "Sorrel Tack") == "north, seen"
    # A farewell that says the words but asks nothing; the night alone is every question
    assert asked_about('"You find me or you find the Captain. Stay off the north wall."', secret) == ""
    assert asked_about('"Hear anything at night?"', secret) == ""

    with get_session() as session:
        session.add(NPCRelationship(npc_id=town["sorrel"], player_id=town["player"], trust_level=50))
        session.commit()
        player = session.get(Player, town["player"])
        text = build_scene_brief(session, player, player_input=T7)["text"]
        plain = build_scene_brief(session, player, player_input="\"Nice weather?\"")["text"]
    line = next(x for x in text.splitlines() if "just asked about what they hide" in x)
    assert "(north, seen)" in line and "a kindness brings it closer" in line
    assert "@trust Sorrel Tack +10" in line and "never a flat denial" in line
    assert "ash-gray" not in line, "the secret's words stay out until trust opens it"
    assert "just asked about what they hide" not in plain
    # One gift away: the line says so
    with get_session() as session:
        session.query(NPCRelationship).filter_by(npc_id=town["sorrel"]).one().trust_level = 55
        session.commit()
        text = build_scene_brief(session, session.get(Player, town["player"]), player_input=T7)["text"]
    assert "one more kindness opens it" in text


# ----------------------------------------------------------------------
# 4. What the character wants reaches play
# ----------------------------------------------------------------------

def test_the_want_is_due_by_the_third_reply_whatever_beats_are_pending(seed, monkeypatch):
    """fleet10-tamsin: a pickpocket offered no mark in 21 replies — the hook
    waited out every reply that carried a beat or a job."""
    from src.services import campaign as campaign_mod
    from src.services.scene_brief import PERSONA_EVERY, build_scene_brief

    def want():
        with get_session() as session:
            text = build_scene_brief(session, session.get(Player, seed["player"]))["text"]
        return next(x for x in text.splitlines() if x.startswith("What this character wants"))

    with get_session() as session:
        session.get(Player, seed["player"]).background = "A card-sharp and pickpocket run out of three cities."
        session.commit()
    first, second, third = want(), want(), want()
    assert "THIS REPLY" not in first and "never two running" in first and "never over a fight" in first
    assert "beat" not in first.split("put one")[0], "a beat no longer holds it back"
    assert "THIS REPLY puts one such chance in plain view" in third and "Hawker's wares and till" in third
    assert "a beat or a job is no reason to hold it" in third
    # A MUST-DELIVER beat owned that reply: the next one, once
    monkeypatch.setattr(campaign_mod, "get_last_tick", lambda pid: {"fired_beat": {"title": "SING"}})
    assert "THIS REPLY" in want() and "THIS REPLY" not in want()
    for _ in range(PERSONA_EVERY - 3):
        assert "THIS REPLY" not in want()
    assert "THIS REPLY" in want(), "then every fifth reply"


def test_a_fight_s_aftermath_hands_the_chance_to_the_next_reply():
    from src.services.scene_brief import persona_due

    pid = "persona-held-test"
    assert [persona_due(pid), persona_due(pid)] == [False, False]
    assert persona_due(pid, held=True) is False          # the third reply answers a [COMBAT OUTCOME]
    assert persona_due(pid, held=True) is False          # ...and so does the fourth
    assert persona_due(pid) is True                      # the first free reply carries it
    assert persona_due(pid) is False


def test_a_thing_holding_goods_is_the_thief_s_chance(seed):
    """fleet10-tamsin: the Hollow's crate of Greywater silver went unmentioned for nine replies."""
    from src.services.scene_brief import _persona_here

    with get_session() as session:
        player = session.get(Player, seed["player"])
        hollow = session.get(Location, seed["hollow"])
        here = _persona_here(session, player, "thief", [], hollow)
    assert any("split supply crate" in h and "what, only searching it shows" in h for h in here), here
    assert not any("silver" in h.lower() for h in here), "what it holds stays theirs to find"


# ----------------------------------------------------------------------
# 5. Proof nobody has found is shown as proof
# ----------------------------------------------------------------------

def _old_wording(seed):
    """The seed as round 9 shipped it: the step named the whistle."""
    with get_session() as session:
        quest = session.get(Quest, seed["teeth"])
        steps = obj.normalize_objectives(quest.objectives)
        steps[3]["text"] = "Bring the cracked bone whistle back to Captain Coldiron as proof"
        quest.objectives = steps
        session.commit()


def test_the_seed_never_names_the_proof_in_its_steps(seed):
    with get_session() as session:
        texts = [o["text"] for o in obj.normalize_objectives(session.get(Quest, seed["teeth"]).objectives)]
        assert texts[3] == "Bring proof back to Captain Coldiron"
        assert not any("whistle" in t.lower() for t in texts)


def test_the_quest_log_shows_proof_until_it_is_in_the_pack(seed):
    """fleet10-bram T1: "Bring the cracked bone whistle back…" on screen from activation."""
    from src.web import server

    _old_wording(seed)
    state = server.build_player_state(seed["player"])
    steps = [o["text"] for o in state["quests"][0]["objectives"]]
    assert steps[3] == "Bring proof back to the Watch Captain" and "whistle" not in json.dumps(state).lower()
    assert steps[0].startswith("Get the Watch Captain's gate pass"), "the pass is a means, never hidden"
    assert "whistle" not in json.dumps(asyncio.run(server.get_quests(seed["player"]))).lower()
    with get_session() as session:
        player = session.get(Player, seed["player"])
        player.inventory = list(player.inventory or []) + [
            {"id": "cracked_bone_whistle", "name": "Cracked Bone Whistle", "type": "quest_item", "quantity": 1}]
        session.commit()
    state = server.build_player_state(seed["player"])
    assert state["quests"][0]["objectives"][3]["text"].startswith("Bring the cracked bone whistle back")


def test_the_lead_on_unfound_proof_says_proof_to_screen_and_dm_alike(seed):
    from src.services.lead import compute_lead, format_lead_context

    _old_wording(seed)
    with get_session() as session:
        quest = session.get(Quest, seed["teeth"])
        steps = obj.normalize_objectives(quest.objectives)
        for step in steps[:3]:
            step["done"] = True
        quest.objectives = steps
        session.commit()
        player = session.get(Player, seed["player"])
        lead = compute_lead(session, player)
        assert lead["text"] == "Bring proof back to Captain Brann Coldiron" and lead["item_name"] is None
        assert "whistle" not in format_lead_context(lead).lower()
        # Standing over it, the lead is the Thing the canvas draws
        player.current_location_id = seed["hollow"]
        session.commit()
        assert compute_lead(session, player)["text"] == "Take the cracked bone whistle here"


def test_the_dm_is_told_nobody_names_it_and_the_word_is_not_met(seed):
    from src.services.campaign import _known_vocabulary
    from src.services.dialogue import shown_step_texts
    from src.services.scene_brief import build_scene_brief

    _old_wording(seed)
    with get_session() as session:
        player = session.get(Player, seed["player"])
        brief = build_scene_brief(session, player)["text"]
        quest = session.get(Quest, seed["teeth"])
        assert not any("whistle" in t.lower() for t in shown_step_texts(session, player, quest))
        campaign = Campaign(player_id=player.id, title="Teeth", status="active", acts=[{"premise": ""}])
        vocabulary = " ".join(_known_vocabulary(session, campaign, player)).lower()
    line = next(x for x in brief.splitlines() if x.startswith("NOT FOUND YET"))
    assert "the Cracked Bone Whistle" in line and "nobody names it yet" in line
    assert "whistle" not in vocabulary, "the quest log no longer counts as meeting the word"
    # Standing where it lies, the find is the scene: PROOF HERE, not NOT FOUND
    with get_session() as session:
        player = session.get(Player, seed["player"])
        player.current_location_id = seed["hollow"]
        session.commit()
        brief = build_scene_brief(session, player)["text"]
    assert "NOT FOUND YET" not in brief and "PROOF HERE" in brief


# ----------------------------------------------------------------------
# Review follow-ups
# ----------------------------------------------------------------------

def test_the_unfound_tag_leaves_the_witnesses_their_clues(seed):
    """fleet10-bram T3/T6: Piper's whistle theory and Sorrel's "Like a whistle"
    were the investigation the critics praised — the tag holds the thing back
    as the job's proof, never the clues that point at it."""
    from src.services.scene_brief import build_scene_brief

    _old_wording(seed)
    with get_session() as session:
        brief = build_scene_brief(session, session.get(Player, seed["player"]))["text"]
    line = next(x for x in brief.splitlines() if x.startswith("NOT FOUND YET"))
    assert "as what the job wants" in line and "What a witness saw or heard" in line


def test_a_won_roll_beside_a_gift_still_lands_the_rest(town):
    """The within-reach line names `@trust <name> +10` for a won roll; a gift
    in the same reply must not swallow it (the secret would stay at 55)."""
    r = _runner(town, T12)
    assert r.run("@coin -2 | pressed into Sorrel's hand").ok and _trust(town) == 55
    rolled = r.run("@trust Sorrel Tack +10 | opening up")
    assert rolled.ok and _trust(town) == 60, rolled.message
    assert "the gift already added +5" in rolled.message
    again = r.run("@trust Sorrel Tack +10")
    assert not again.ok and _trust(town) == 60, "the same +10 twice is still one"


def test_a_price_a_loss_or_a_sweet_bought_is_no_gift(town):
    # fleet9-bram T11: a theft, written beside words that hand something over
    stolen = _runner(town, "I hand Sorrel the halter and turn to go.").run(
        "@coin -6 | a quick hand in the press lifts coin from his purse before he feels it")
    assert stolen.ok and "gift_trust" not in stolen.result and _trust(town) is None
    # fleet2-tamsin T7: coin handed over for a sweet
    assert not gift_in_words("I hand over the coin with a genuine smile, taking the sweetroot twist. "
                             "\"Two coin well spent,\" I say warmly.")
    wages = _runner(town, T12).run("@coin -3 | Sorrel's wages for the week")
    assert wages.ok and "gift_trust" not in wages.result


def test_an_ambush_reply_hands_the_due_chance_on(seed):
    """An ambush is one sentence: a chance due then waits for the next reply,
    as a fight's aftermath does (the line itself says only a fight moves it)."""
    from src.services.scene_brief import build_scene_brief

    with get_session() as session:
        session.get(Player, seed["player"]).background = "A card-sharp and pickpocket run out of three cities."
        session.commit()

    def want(**kw):
        with get_session() as session:
            text = build_scene_brief(session, session.get(Player, seed["player"]), **kw)["text"]
        return next(x for x in text.splitlines() if x.startswith("What this character wants"))

    want(), want()
    assert "THIS REPLY" not in want(player_input="I keep walking.", engine_reply=True)
    assert "THIS REPLY" in want(player_input="I look around the square.")

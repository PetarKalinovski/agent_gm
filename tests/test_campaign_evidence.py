"""The story spine against round 5 (fleet6, 2026-10-04): act pacing, beats
told by play, sealed truths.

- fleet6-wenna: act 2 opened on turn 3 off the opening's own quest; an act-3
  quest was pitched in act 2; a letter the DM wrote spilled three sealed
  truths one term each; "The Cart Comes Short" was told while its scheduled
  twin stayed due.
- fleet6-bram: "The Fence's Ledger" was delivered on "Hesk" + "wolf" while
  the Hawker refused to talk after a failed roll; it had fired as
  MUST-DELIVER and the DM wrote "You press him"; a leaked truth delivered
  beat 7 while its predecessor was still armed.

Replays over every fleet transcript (fleet-fleet6, scratch copies of the
round DBs): fired-path deliveries 19 old → 17 new (the two held: the Fence's
Ledger, and "The Cracked Whistle" shown as "a length of bone" until the
whistle was named); play-path deliveries 18 old → 21 new, with "north wall"
read as a phrase and the player's own question seconding a keyword.
"""

import pytest

from src.models import (
    NPC, Campaign, DMState, Event, Location, LocationType, Player, Quest, QuestStatus, StoryBeat, WorldClock,
    get_session,
)
from src.services import campaign as camp
from src.services import memory_files


@pytest.fixture(autouse=True)
def quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(camp, "_spawn_generation", lambda player_id: None)
    monkeypatch.setattr(memory_files, "memory_root", lambda: tmp_path / "memory")
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    camp.reset_state()
    yield
    camp.reset_state()


TRUTHS = [
    {"id": "truth_betrayal", "kind": "the_betrayal", "revealed": False,
     "statement": "Captain Coldiron's roster lists twenty able bodies but only eleven exist.",
     "spoiler_keywords": ["coldiron", "roster", "eleven"]},
    {"id": "truth_secret", "kind": "the_secret", "revealed": False,
     "statement": "Maera Duskwell and Hesk Bramble both buy 'wolf-taken' goods through a fence.",
     "spoiler_keywords": ["maera", "hesk", "fence", "wolf-taken"]},
    {"id": "truth_cost", "kind": "the_cost", "revealed": False,
     "statement": "The toll keeps the road closed and Emberfall starving.",
     "spoiler_keywords": ["toll", "road closed"]},
]
FENCE = ("Have Hesk Bramble, cornered or chatted up at his stall, let slip under pressure or coin that "
         "he and Maera Duskwell both buy 'wolf-taken' goods off the same fence — goods that come off "
         "dead carters.")


@pytest.fixture()
def spine(db):
    """fleet6's Emberfall in miniature, act 1 armed at Day 1 18:00."""
    with get_session() as session:
        session.add(WorldClock(day=1, hour=18))
        session.add(DMState())
        square = Location(name="Market Square", type=LocationType.DISTRICT, visited=True, discovered=True)
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, visited=True, discovered=True)
        session.add_all([square, hollow])
        session.flush()
        people = {key: NPC(name=name, current_location_id=square.id) for key, name in (
            ("coldiron", "Captain Brann Coldiron"), ("hesk", "Hesk Bramble"), ("piper", "Piper Quillow"),
            ("maera", "Maera Duskwell"), ("sorrel", "Sorrel Tack"))}
        session.add_all(people.values())
        teeth = Quest(title="Teeth on the North Road", status=QuestStatus.NOT_STARTED,
                      assigned_by_npc_id=None)
        letter = Quest(title="A Letter for a Buried Man", status=QuestStatus.NOT_STARTED)
        session.add_all([teeth, letter])
        player = Player(name="Bram Holloway", current_location_id=square.id)
        session.add(player)
        session.flush()
        people["coldiron"].current_location_id = square.id
        teeth.assigned_by_npc_id = people["coldiron"].id
        letter.assigned_by_npc_id = people["maera"].id
        campaign = Campaign(player_id=player.id, title="The Toll Road", status="active", current_act=1,
                            premise="Someone drills the wolves.",
                            acts=[{"act": 1, "title": "Teeth", "premise": "The howls keep a schedule."},
                                  {"act": 2, "title": "Grudges", "premise": "Old debts."},
                                  {"act": 3, "title": "The Gate", "premise": "It ends at the gate."}],
                            fixed_truths=[dict(t) for t in TRUTHS])
        session.add(campaign)
        session.commit()
        ids = {k: v.id for k, v in people.items()}
        ids.update(player=player.id, campaign=campaign.id, square=square.id, hollow=hollow.id,
                   teeth=teeth.id, letter=letter.id)
        return ids


def _beat(spine, act, seq, title, **kw):
    with get_session() as session:
        row = StoryBeat(campaign_id=spine["campaign"], act=act, sequence=seq, title=title,
                        beat_type=kw.pop("beat_type", "revelation"), directive=kw.pop("directive", title),
                        trigger_type=kw.pop("trigger_type", "time"),
                        trigger_data=kw.pop("trigger_data", {"delay": "days"}),
                        status=kw.pop("status", "pending"), **kw)
        session.add(row)
        session.commit()
        return row.id


def _row(beat_id):
    with get_session() as session:
        return session.get(StoryBeat, beat_id)


def _campaign(spine):
    with get_session() as session:
        return session.get(Campaign, spine["campaign"])


def _set(model, ident, **fields):
    with get_session() as session:
        row = session.get(model, ident)
        for key, value in fields.items():
            setattr(row, key, value)
        session.commit()


def _truth(spine, truth_id):
    return next(t for t in _campaign(spine).fixed_truths if t["id"] == truth_id)


# --- 1. Pull-ahead counts only what play made true ---------------------------

def _wenna_acts(spine, *, delivered=True):
    """Act 1: one beat told, a day-2 clock left. Act 2: 'The Wolf's Hollow
    Whistle' on Teeth going active. Act 3 to come."""
    _beat(spine, 1, 1, "Piper's Charts", status="delivered" if delivered else "armed",
          trigger_type="location", trigger_data={"location_id": "elsewhere"},
          fallback_day=9, fallback_hour=0)
    _beat(spine, 1, 2, "The Cart Comes Short", status="armed", scheduled_day=3, scheduled_hour=18,
          fallback_day=6, fallback_hour=18)
    whistle = _beat(spine, 2, 3, "The Wolf's Hollow Whistle", trigger_type="quest_status",
                    trigger_data={"quest_id": spine["teeth"], "status": "active"})
    _beat(spine, 3, 4, "The Hollow Answers", beat_type="confrontation", cinematic="finale")
    return whistle


def test_the_openings_own_job_never_pulls_the_next_act_ahead(spine):
    """fleet6-wenna T3: the opening started Teeth at T0; act 2 opened at T3."""
    whistle = _wenna_acts(spine)
    camp.note_opening_offers(spine["player"], [spine["teeth"]])
    assert whistle in (_campaign(spine).pull_held or [])
    camp.run_campaign_tick(spine["player"])           # the [OPENING] turn: Teeth not started yet
    _set(Quest, spine["teeth"], status=QuestStatus.ACTIVE)   # the DM's @quest-start
    for _ in range(3):
        camp.run_campaign_tick(spine["player"])
    assert _campaign(spine).current_act == 1
    assert _row(whistle).status == "pending"


def test_a_trigger_already_true_when_the_act_armed_never_pulls_ahead(spine):
    whistle = _wenna_acts(spine)
    _set(Quest, spine["teeth"], status=QuestStatus.ACTIVE)
    with get_session() as session:
        camp.arm_act(session, session.get(Campaign, spine["campaign"]), 1)
        session.commit()
    assert _campaign(spine).pull_held == [whistle]
    camp.run_campaign_tick(spine["player"])
    assert _campaign(spine).current_act == 1


def test_a_trigger_play_made_true_after_arming_pulls_ahead(spine):
    whistle = _wenna_acts(spine)
    with get_session() as session:
        camp.arm_act(session, session.get(Campaign, spine["campaign"]), 1)
        session.commit()
    assert _campaign(spine).pull_held == []
    _set(Quest, spine["teeth"], status=QuestStatus.ACTIVE)     # the player took the job in play
    camp.run_campaign_tick(spine["player"])
    assert _campaign(spine).current_act == 2 and _row(whistle).status == "fired", "and it lands at once"


def test_an_act_that_told_nothing_is_never_pulled_ahead(spine):
    _wenna_acts(spine, delivered=False)
    with get_session() as session:
        camp.arm_act(session, session.get(Campaign, spine["campaign"]), 1)
        session.commit()
    _set(Quest, spine["teeth"], status=QuestStatus.ACTIVE)
    camp.run_campaign_tick(spine["player"])
    assert _campaign(spine).current_act == 1


def test_a_place_trigger_counts_once_the_player_left_and_came_back(spine):
    """fleet6-bram T14: the square held the act-2 trigger when act 1 armed;
    walking back into it after the Hollow is play."""
    _beat(spine, 1, 1, "A Pattern in the Dark", status="delivered")
    _beat(spine, 1, 2, "The Evening Howl", status="armed", scheduled_day=3, scheduled_hour=20,
          fallback_day=6, fallback_hour=20)
    ledger = _beat(spine, 2, 3, "The Fence's Ledger", trigger_type="location",
                   trigger_data={"location_id": spine["square"]})
    _beat(spine, 3, 4, "The Hollow Answers", beat_type="confrontation", cinematic="finale")
    with get_session() as session:
        camp.arm_act(session, session.get(Campaign, spine["campaign"]), 1)
        session.commit()
    assert _campaign(spine).pull_held == [ledger]
    camp.run_campaign_tick(spine["player"])
    assert _campaign(spine).current_act == 1, "standing where the act began is no news"
    _set(Player, spine["player"], current_location_id=spine["hollow"])
    camp.run_campaign_tick(spine["player"])
    _set(Player, spine["player"], current_location_id=spine["square"])
    camp.run_campaign_tick(spine["player"])
    assert _campaign(spine).current_act == 2


# --- 2. A later act's job waits for its act ----------------------------------

def test_offer_directive_keeps_a_later_acts_quest_for_that_act(spine):
    """fleet6-wenna: act-3 'What Maera Hid' activates 'A Letter for a Buried
    Man'; offer_directive pitched it in act 2."""
    from src.agents.dm_orchestrator import offer_directive, reset_offer_state

    _set(Campaign, spine["campaign"], current_act=2)
    hid = _beat(spine, 3, 9, "What Maera Hid", trigger_type="location",
                trigger_data={"location_id": spine["square"]},
                consequence_met=[{"op": "activate_quest", "quest_id": spine["letter"]}])
    present = {spine["maera"]: "Maera Duskwell"}
    with get_session() as session:
        assert camp.later_act_quest_ids(session, spine["player"]) == {spine["letter"]}
    reset_offer_state()
    assert offer_directive(spine["player"], present, "Got any work for me?") == ""

    _set(Campaign, spine["campaign"], current_act=3)
    _set(StoryBeat, hid, status="armed")
    reset_offer_state()
    assert "A Letter for a Buried Man" in offer_directive(spine["player"], present, "Got any work for me?")


# --- 3. What counts as evidence a beat was told ------------------------------

def _fence_ledger(spine, **kw):
    fields = dict(directive=FENCE, trigger_type="location", trigger_data={"location_id": spine["square"]},
                  delivery_keywords=["wolf-taken", "fence", "dead carters", "Hesk"],
                  reveals_truth_ids=["truth_secret"], lead="Hesk's stall is full of goods he won't explain.",
                  lead_target={"type": "npc", "id": spine["hesk"]},
                  consequence_met=[{"op": "npc_mood", "npc_id": spine["hesk"], "mood": "frightened"}])
    fields.update(kw)
    return _beat(spine, 1, 5, "The Fence's Ledger", status="armed", **fields)


HAWKER = ("That's wolf work. Or — no. No, that's not wolf work, is it. Wolves don't leave a man walking. "
          "That's not something I talk about.")


def _speech(spine, who, text):
    return [{"npc_id": spine[who], "npc_name": who.title(), "text": text}]


def test_a_name_and_the_campaigns_weather_tell_nothing(spine):
    """fleet6-bram T14, word for word: 'Hesk' + 'wolf' delivered the beat."""
    beat = _fence_ledger(spine, directive="Hesk Bramble lets slip that he buys wolf-taken goods off a fence.")
    for i in range(10):     # a campaign about wolves: 'wolf' in a third of its texts
        _beat(spine, 2, 10 + i, f"Wolf beat {i}", directive="The wolves howl; a wolf pack circles.")
    camp.run_campaign_tick(spine["player"])
    assert _row(beat).status == "fired"
    with get_session() as session:
        c = session.get(Campaign, spine["campaign"])
        assert "wolf" in camp.campaign_common_words(c, camp._campaign_beats(session, c))
    result = camp.mark_beat_delivery(spine["player"], HAWKER, speakers=["Hesk Bramble"],
                                     spoken=_speech(spine, "hesk", HAWKER), player_input="What's this?")
    assert not result["delivered"] and _row(beat).status == "fired"
    assert _truth(spine, "truth_secret")["revealed"] is False


def test_a_truth_beat_needs_its_truth_said(spine):
    beat = _fence_ledger(spine, directive="Hesk lets slip where his stock comes from.")
    camp.run_campaign_tick(spine["player"])
    line = "These came off dead carters — the wolf-taken lot. Don't ask me more."
    assert not camp.mark_beat_delivery(spine["player"], line, spoken=_speech(spine, "hesk", line),
                                       player_input="Where's this from?")["delivered"]
    line += " I buy it off a fence, same as Maera."
    assert camp.mark_beat_delivery(spine["player"], line, spoken=_speech(spine, "hesk", line),
                                   player_input="Where's this from?")["delivered"]
    assert _truth(spine, "truth_secret")["revealed"] is True


def test_a_failed_roll_holds_the_beat_back(spine):
    beat = _fence_ledger(spine, directive="Hesk lets slip where his stock comes from.")
    camp.run_campaign_tick(spine["player"])
    line = "Dead carters, a fence — no. No. That's not something I talk about."
    result = camp.mark_beat_delivery(spine["player"], line, spoken=_speech(spine, "hesk", line),
                                     player_input="I lean on him.", roll_outcomes=["failure"])
    assert not result["delivered"] and _row(beat).status == "fired"
    fired = camp.run_campaign_tick(spine["player"])["fired_beat"]
    assert fired["title"] == "The Fence's Ledger" and fired["attempt"] == 2
    assert any("roll failed" in note for note in fired["notes"])


def test_the_audit_copy_carries_the_evidence_rule(spine):
    _fence_ledger(spine, directive="Hesk lets slip where his stock comes from.")
    tick = camp.run_campaign_tick(spine["player"])
    audit_beat = tick["audit"]["beat"]
    assert "Hesk Bramble" in audit_beat["names"]
    assert not camp.beat_keywords_delivered(audit_beat, HAWKER + "\nHesk Bramble")


def test_a_phrase_of_everyday_words_is_still_evidence():
    """Sorrel's 'ash-gray cloak … by the north wall' told his sighting."""
    told, hits = camp.evidence_told(["Sorrel", "ash-gray cloak", "north wall"],
                                    "A man in an ash-gray cloak crouched by the north wall.", ["Sorrel Tack"])
    assert told and hits == ["ash-gray cloak", "north wall"]
    assert camp.evidence_told(["Sorrel", "ash-gray cloak", "north wall"],
                              "A man in an ash-gray cloak stood at the wall.", ["Sorrel Tack"])[1] == ["ash-gray cloak"]


def test_a_named_person_only_seconds_a_fired_beats_evidence():
    """fleet4-wenna T2: Piper showed her notebook — 'notebook' + Piper."""
    keywords = ["Piper", "howl charts", "schedule", "notebook"]
    text = "Piper Quillow thrusts a battered notebook at you."
    assert camp.evidence_told(keywords, text, ["Piper Quillow"], name_support=True)[0]
    assert not camp.evidence_told(keywords, text, ["Piper Quillow"])[0], "play needs two of its own"
    assert not camp.evidence_told(keywords, "Piper Quillow waves.", ["Piper Quillow"], name_support=True)[0]


# --- 4. A truth out before its scene -----------------------------------------

def test_a_leak_ahead_of_its_predecessor_keeps_the_confrontation_owed(spine):
    """fleet6-bram T19: an overheard 'eleven… told the council twenty' told
    the roster lie; 'Coldiron's Lie, Uncovered' was delivered by play while
    'The Sergeant's Name' was still armed."""
    sergeant = _beat(spine, 1, 6, "The Sergeant's Name", status="armed", scheduled_day=9, scheduled_hour=0,
                     fallback_day=12, fallback_hour=0)
    lie = _beat(spine, 1, 7, "Coldiron's Lie, Uncovered", trigger_type="after_beat",
                trigger_data={"beat_id": sergeant, "delay": "soon"}, reveals_truth_ids=["truth_betrayal"],
                directive="Force the moment where Bram confronts Coldiron; Coldiron admits the roster is "
                          "eleven, not twenty.",
                delivery_keywords=["eleven able bodies", "Coldiron", "bluff", "roster"],
                lead_target={"type": "npc", "id": spine["coldiron"]})
    mutter = "Coldiron mutters a headcount under his breath: eleven able, told the council twenty."
    result = camp.mark_beat_delivery(spine["player"], mutter, player_input="Where do I find the Captain?")
    assert "truth_betrayal" in result["truths_revealed"]
    truth = _truth(spine, "truth_betrayal")
    assert truth["revealed"] is True and truth["scene_owed"] == [lie]
    assert _row(lie).status == "pending", "never told by the leak"

    _set(StoryBeat, sergeant, status="delivered")
    camp.run_campaign_tick(spine["player"])
    assert _row(lie).status == "armed", "it arms in order and is not delivered by the leaked truth"
    _set(StoryBeat, lie, scheduled_day=1, scheduled_hour=0)
    _set(Campaign, spine["campaign"], last_beat_day=None)
    fired = camp.run_campaign_tick(spine["player"])["fired_beat"]
    assert fired["title"] == "Coldiron's Lie, Uncovered"
    assert any("already overheard" in n and "still costs a scene" in n for n in fired["notes"])
    assert fired["hook"] is True, "'where Bram confronts' waits on the player"


# --- 5. Leaks spread thin, and the subject's own count -----------------------

LETTER = ("*If this reaches you, I was right about the counting-house silver. Ask who's paying the toll, "
          "and ask why the Watch roster never changes no matter how many go missing.*")


def test_one_term_each_of_two_sealed_truths_gives_both_away():
    """fleet6-wenna T17: the DM's letter — {} before."""
    truths = [dict(t) for t in TRUTHS]
    hits = camp.sealed_truth_hits(truths, LETTER, ["Captain Brann Coldiron"])
    assert set(hits) == {"truth_betrayal", "truth_cost"}


def test_a_unit_telling_one_truth_in_full_spends_no_other():
    """fleet3-tamsin T20: Coldiron's confession and, beside it, a whistle."""
    truths = [dict(t) for t in TRUTHS] + [{"id": "truth_whistle", "statement": "Vane drills the pack.",
                                           "spoiler_keywords": ["whistle"]}]
    line = "Eleven on the roster, not twenty. And there's a bone whistle I never showed anyone."
    assert set(camp.sealed_truth_hits(truths, line, ["Captain Brann Coldiron"])) == {"truth_betrayal"}


def test_spread_thin_words_are_matched_as_written():
    """fleet6-wenna T14: "a good word" is no "wolf-taken goods"; "close
    enough" is no "road closed"; "twenty coin" is no headcount."""
    truths = [{"id": "goods", "spoiler_keywords": ["stolen goods"]},
              {"id": "closed", "spoiler_keywords": ["closed"]},
              {"id": "roster", "spoiler_keywords": ["roster"]},
              {"id": "count", "statement": "Only eleven men answer.", "spoiler_keywords": []}]
    for line in ("I'll put in a stolen good word — the roster's thin.",
                 "Close enough to see the roster on the wall.",
                 "Eleven coin for the roster, then."):
        assert camp.sealed_truth_hits(truths, line) == {}, line
    assert set(camp.sealed_truth_hits(truths, "Stolen goods, and a roster nobody trusts.")) == {"goods", "roster"}


def test_the_players_own_words_are_no_evidence():
    truths = [dict(t) for t in TRUTHS]
    line = "The toll? The roster? I can't say that out loud."
    assert set(camp.sealed_truth_hits(truths, line)) == {"truth_betrayal", "truth_cost"}
    assert camp.sealed_truth_hits(truths, line, player_words="Who pays the toll, and why is the roster thin?") == {}


def test_the_truths_subject_saying_its_count_gives_it_away():
    """fleet-bram T18: "I've been fighting for it with eleven —"."""
    truths = [dict(t, subjects=["coldiron-id"]) for t in TRUTHS]
    said = [("coldiron-id", "I've been fighting for it with eleven — with what I've got.")]
    assert camp.sealed_truth_hits(truths, said[0][1], speech=said) == {"truth_betrayal": ["eleven"]}
    for npc_id, line in (("coldiron-id", "Drove that road eleven years, never lost a wheel."),
                         ("coldiron-id", "A thin roster and no man to spare."),
                         ("piper-id", "I counted eleven howls.")):
        assert camp.sealed_truth_hits(truths, line, speech=[(npc_id, line)]) == {}, line


def test_the_engine_reveals_a_letter_spread_thin(spine):
    result = camp.mark_beat_delivery(spine["player"], LETTER, player_input="I read the letter.")
    assert set(result["truths_revealed"]) == {"truth_betrayal", "truth_cost"}


# --- 6. A beat and its scheduled twin ----------------------------------------

CART_EVENT = ("The overdue supply cart finally limps through the South Gate half-empty, one horse lathered "
              "and lame, the driver repeating that the birches on the north road had eyes in them.")
HOWL_EVENT = ("At the stroke of eight, howling rises from the northern hills — layered, answering itself, "
              "closer than last night. The North Gate is barred early and the square empties fast.")


def _events():
    with get_session() as session:
        cart = Event(name="The Greywater Cart Arrives Short", description=CART_EVENT, event_type="meso",
                     scheduled_day=2, scheduled_hour=9, player_visible=True, narrated_to_player=False,
                     consequences=["Pressure on the Watch to act"])
        howl = Event(name="The Evening Howl", description=HOWL_EVENT, event_type="meso",
                     scheduled_day=1, scheduled_hour=20, player_visible=True, narrated_to_player=False,
                     consequences=["North Gate barred for the night", "Townsfolk clear the square"])
        session.add_all([cart, howl])
        session.commit()
        return cart.id, howl.id


def test_a_delivered_beat_retires_its_scheduled_twin(spine):
    """fleet6-wenna T9: Coldiron told the short cart ('no driver'); the
    scheduled cart with its talking driver stayed due for Day 2 09:00."""
    cart, howl = _events()
    _beat(spine, 1, 4, "The Cart Comes Short", beat_type="setpiece", status="armed",
          directive="The overdue Greywater supply cart limps through the South Gate half-empty, one horse "
                    "lame, the driver repeating that the birches on the north road had eyes.",
          delivery_keywords=["Greywater cart", "half-empty", "lame horse", "birches"],
          lead_target={"type": "npc", "id": spine["coldiron"]})
    line = "Supply cart from Greywater came in half-empty, a lame horse dragging it, and no driver."
    result = camp.mark_beat_delivery(spine["player"], line, spoken=_speech(spine, "coldiron", line),
                                     player_input="Any news from the road?")
    assert result["played"]
    with get_session() as session:
        assert session.get(Event, cart).narrated_to_player is True
        assert session.get(Event, cart).occurred_day == 1
        assert session.get(Event, howl).narrated_to_player is False, "the howl is not the cart"
    context = camp.format_campaign_context(camp.run_campaign_tick(spine["player"]))
    assert 'CONTINUITY: "The Greywater Cart Arrives Short" (due Day 2, 09:00) will not happen' in context
    assert "CONTINUITY" not in camp.format_campaign_context(camp.run_campaign_tick(spine["player"]))


def test_a_beat_about_the_howls_is_no_twin_of_the_evening_howl(db):
    from src.services import world_tick

    _events()
    with get_session() as session:
        twins = world_tick.retire_scheduled_twins(
            session, "A Pattern in the Dark", ["Piper", "charcoal", "three directions", "howls"],
            "Have Piper approach Bram with a scrap of charcoal-marked parchment, insisting the howls repeat "
            "the same three directions every night and that nobody with a badge will listen to her.", (1, 18))
        assert twins == []
        twins = world_tick.retire_scheduled_twins(session, "The Evening Howl", ["evening howl", "North Gate"],
                                                  "", (1, 20))
        assert [e.name for e in twins] == ["The Evening Howl"]

    with get_session() as session:
        twins = world_tick.retire_scheduled_twins(
            session, "The Cart Limps In", ["supply", "wagon"],
            "As already scheduled, the overdue supply cart limps through the South Gate half-empty, one "
            "horse lame, the driver swearing the birches on the north road had eyes in them.", (1, 15))
        assert [e.name for e in twins] == ["The Greywater Cart Arrives Short"]


def test_a_beat_whose_twin_was_already_told_is_its_aftermath(spine):
    """fleet6-wenna T8/T10: the 20:00 howl was narrated, and the 'Evening
    Howl' deadline beat (due at midnight) would have told it again."""
    _cart, howl = _events()
    _set(Event, howl, occurred_day=1, occurred_hour=20, narrated_to_player=True)
    _beat(spine, 1, 2, "The Evening Howl", beat_type="deadline_event", status="armed",
          directive="At the stroke of eight the howling rises from the northern hills; the North Gate bars.",
          delivery_keywords=["Evening Howl", "North Gate", "barred"], scheduled_day=1, scheduled_hour=18,
          fallback_day=1, fallback_hour=18)
    fired = camp.run_campaign_tick(spine["player"])["fired_beat"]
    assert fired["title"] == "The Evening Howl"
    assert any('"The Evening Howl" already happened and was told (Day 1, 20:00)' in n for n in fired["notes"])


def test_the_forge_never_restates_a_scheduled_event_nor_titles_an_act_with_a_truth():
    from src.agents.campaign_forge import CAMPAIGN_FORGE_PROMPT

    assert "ONE TELLING PER EVENT" in CAMPAIGN_FORGE_PROMPT
    assert "ACT TITLES AND PREMISES ARE SHOWN TO THE PLAYER" in CAMPAIGN_FORGE_PROMPT


# --- 7. Hooks wait for the player --------------------------------------------

def test_hook_beats_are_the_ones_that_wait_on_the_player():
    from types import SimpleNamespace as Beat

    assert camp.is_hook_beat(Beat(beat_type="revelation", directive=FENCE))
    assert camp.is_hook_beat(Beat(beat_type="revelation",
                                  directive="Pressed about the Watch, Coldiron shows the roster."))
    # the person comes to the player; pressing is only for more
    assert not camp.is_hook_beat(Beat(beat_type="revelation", directive=(
        "Have Sorrel mention to Wenna that he saw a man by the north wall. He won't say more unless pressed.")))
    assert not camp.is_hook_beat(Beat(beat_type="confrontation", directive="Vane, cornered, makes his offer."))
    assert not camp.is_hook_beat(Beat(beat_type="deadline_event", directive="If pressed, the gate bars."))


def test_a_hook_fires_as_a_tell_and_counts_once_the_player_takes_it_up(spine):
    """fleet6-bram T14: fired as MUST-DELIVER while Bram walked to the
    healer; the DM wrote 'You press him' and rolled for it."""
    from src.services.lead import compute_lead

    beat = _fence_ledger(spine)
    tick = camp.run_campaign_tick(spine["player"])
    fired = tick["fired_beat"]
    assert fired["hook"] is True and tick["audit"]["beat"] is None
    context = camp.format_campaign_context(tick)
    assert "THIS TURN — STORY HOOK" in context and "MUST DELIVER" not in context
    assert "never take it up for them" in context
    assert any(n.startswith("HOOK") and "Hesk Bramble" in n and "Hesk's stall" in n for n in fired["notes"])
    with get_session() as session:
        lead = compute_lead(session, session.get(Player, spine["player"]))
        assert lead["beat_id"] == beat, "its lead is on the tracker while it waits"

    line = "Wolf-taken, off dead carters — I buy it through a fence. Maera too."
    walking = camp.mark_beat_delivery(spine["player"], line, spoken=_speech(spine, "hesk", line),
                                      player_input="I head for the healer's lit window.")
    assert not walking["delivered"] and _row(beat).status == "fired"
    asked = camp.mark_beat_delivery(spine["player"], line, spoken=_speech(spine, "hesk", line),
                                    player_input='"Where does this stock come from?"',
                                    addressed_npc_id=spine["hesk"])
    assert asked["delivered"] and _row(beat).status == "delivered"


def test_a_hook_taken_up_in_the_players_own_words(spine):
    beat = _fence_ledger(spine)
    camp.run_campaign_tick(spine["player"])
    line = "Wolf-taken, off dead carters — I buy it through a fence. Maera too."
    assert camp.mark_beat_delivery(spine["player"], line, spoken=_speech(spine, "hesk", line),
                                   player_input="I ask around about the fence nobody names.")["delivered"]
    assert _row(beat).status == "delivered"


def test_an_engine_turn_takes_no_hook_up(spine):
    beat = _fence_ledger(spine)
    camp.run_campaign_tick(spine["player"])
    line = "Wolf-taken, off dead carters — I buy it through a fence."
    assert not camp.mark_beat_delivery(spine["player"], line, spoken=_speech(spine, "hesk", line),
                                       player_input="[COMBAT OUTCOME] the wolves fled")["delivered"]
    assert _row(beat).status == "fired"


# --- Review of round 5 --------------------------------------------------------

def test_the_cover_story_count_tells_nothing():
    """Every fleet roster truth states the lie AND the truth ("lists twenty
    … only eleven exist"): Coldiron bluffing "twenty" keeps the lie."""
    for statement in (TRUTHS[0]["statement"],
                      "Captain Coldiron's roster of twenty able Watch bodies is a lie; he has eleven.",
                      "The Watch roster is a lie — only eleven able bodies remain, not twenty.",
                      "Coldiron is telling now about having twenty able bodies instead of eleven."):
        assert camp._cover_counts(statement) == {"twenty"}, statement
    assert camp._cover_counts("Captain Coldiron's thin eleven are exposed as a lie.") == set()
    assert camp._cover_counts("There are twenty names on it but only eleven men.") == {"twenty", "eleven"}, \
        "both marked: neither counts alone"

    truths = [dict(t, subjects=["coldiron-id"]) for t in TRUTHS]
    bluff = "Twenty good men on that wall, and every one of them awake."
    assert camp.sealed_truth_hits(truths, bluff, speech=[("coldiron-id", bluff)]) == {}
    assert camp.sealed_truth_hits(truths, "His twenty hold the gate while the toll stands.") == {}
    told = "Eleven. I have eleven, and the town thinks otherwise."
    assert camp.sealed_truth_hits(truths, told, speech=[("coldiron-id", told)]) == {"truth_betrayal": ["eleven"]}


def test_the_audit_reads_the_players_words_and_the_subject_like_the_engine(spine):
    """fleet6-wenna T20: Wenna asked "who's paying the toll, and why your
    Watch roster never changes"; Coldiron's answer repeats both words. The
    engine reveals nothing for that, so the audit must not say it did."""
    from src.web.turn_audit import audit_turn

    tick = camp.run_campaign_tick(spine["player"])
    assert all("subjects" in t for t in tick["audit"]["sealed_truths"])
    line = "The toll — I don't know who's collecting it. But my roster's got more names on it than men."
    spoken = [{"npc_id": spine["coldiron"], "npc_name": "Captain Brann Coldiron", "text": line}]
    asked = "Who's paying the toll, and why does your Watch roster never change?"

    def frontrun(player_input: str, words: str = line) -> bool:
        said = [dict(spoken[0], text=words)]
        warnings = audit_turn(words, [], {}, {}, campaign=tick["audit"], spoken_text=words, spoken=said,
                              player_input=player_input)
        return any(w["check"] == "campaign_truth_frontrun" for w in warnings)

    assert not frontrun(asked)
    assert camp.mark_beat_delivery(spine["player"], line, spoken=spoken, player_input=asked)["truths_revealed"] == []
    assert frontrun("I wait.")
    count = "I've been holding that road with eleven — with what I've got."
    assert frontrun("I wait.", count), "the subject's own count, as the engine reads it"


def test_a_later_nights_howl_is_not_the_aftermath_of_day_ones(spine):
    """fleet-bram's act-3 deadline 'The Evening Howl' ("close at the evening
    howl as always — but this time…") arms days after Day 1's howl."""
    _cart, howl = _events()
    _set(Event, howl, occurred_day=1, occurred_hour=20, narrated_to_player=True)
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.day, clock.hour = 4, 21
        session.commit()
    _beat(spine, 1, 2, "The Evening Howl", beat_type="deadline_event", status="armed",
          directive="Have the gates close at the evening howl as always — but this time the whistle sounds early.",
          delivery_keywords=["evening howl", "whistle", "gates"], armed_day=4, armed_hour=8,
          scheduled_day=4, scheduled_hour=20, fallback_day=4, fallback_hour=20)
    fired = camp.run_campaign_tick(spine["player"])["fired_beat"]
    assert fired["title"] == "The Evening Howl"
    assert not any("already happened" in n for n in fired["notes"])


def test_a_twin_already_due_happened_as_the_beat_told_it(spine):
    _cart, howl = _events()
    _set(Event, howl, occurred_day=1, occurred_hour=20)        # fired, not yet narrated
    beat = _beat(spine, 1, 2, "The Evening Howl", beat_type="deadline_event", status="fired",
                 delivery_keywords=["evening howl", "North Gate"])
    with get_session() as session:
        campaign = session.get(Campaign, spine["campaign"])
        camp._deliver_beat(session, campaign, session.get(StoryBeat, beat), (1, 20), [], [])
        session.commit()
    line = camp._CONTINUITY[spine["player"]][0]
    assert '"The Evening Howl" (due Day 1, 20:00) happened as "The Evening Howl" told it' in line
    assert "will not happen" not in line


def test_a_hook_firing_here_outranks_the_quest_step_on_the_tracker(spine):
    """fleet6-bram T14: Bram had the Teeth job, so the tracker kept its step
    while the Fence's Ledger fired at Hesk's stall."""
    from src.services.lead import compute_lead

    with get_session() as session:
        quest = session.get(Quest, spine["teeth"])
        quest.status = QuestStatus.ACTIVE
        quest.objectives = [{"text": "Find out what drills the wolves", "kind": "custom"}]
        player = session.get(Player, spine["player"])
        player.active_quests = [quest.id]
        session.commit()

    def lead():
        with get_session() as session:
            return compute_lead(session, session.get(Player, spine["player"]))

    beat = _fence_ledger(spine)
    assert lead()["source"] == "quest", "armed, not yet shown: the job leads"
    camp.run_campaign_tick(spine["player"])
    assert _row(beat).status == "fired"
    assert lead()["beat_id"] == beat, "the tell is on screen, and so is what it is about"
    _set(NPC, spine["hesk"], current_location_id=spine["hollow"])
    assert lead()["source"] == "quest", "not where the player stands: the job leads again"


def test_a_hook_hands_over_and_shows_its_title_only_once_told(spine):
    """fleet6-bram's "Greywater Silver" (give_item: a pouch of Greywater
    silver) would have put the pouch in the pack — and "▲ Greywater Silver"
    on screen — over a tell, before anyone pressed Vex."""
    pouch = {"op": "give_item", "item_name": "Pouch of Greywater Silver", "description": "Minted coin."}
    mood = {"op": "npc_mood", "npc_id": spine["hesk"], "mood": "frightened"}
    beat = _fence_ledger(spine, consequence_met=[mood, pouch])

    def pack() -> list[str]:
        with get_session() as session:
            return [e.get("name") for e in session.get(Player, spine["player"]).inventory or []]

    tick = camp.run_campaign_tick(spine["player"])
    assert tick["fired_beat"]["hook"] is True
    assert not [e for e in tick["events"] if e.get("type") == "campaign_beat"], "no title over the tell"
    assert "Pouch of Greywater Silver" not in pack()
    assert not any("Pouch" in line for line in tick["fired_beat"]["applied_effects"])

    line = "Wolf-taken, off dead carters — I buy it through a fence. Here, take the pouch. Maera too."
    told = camp.mark_beat_delivery(spine["player"], line, spoken=_speech(spine, "hesk", line),
                                   player_input='"Where does this stock come from?"', addressed_npc_id=spine["hesk"])
    assert told["delivered"]
    assert [e["title"] for e in told["events"] if e.get("type") == "campaign_beat"] == ["The Fence's Ledger"]
    assert pack().count("Pouch of Greywater Silver") == 1


def test_a_coin_pressed_on_her_is_no_hook_and_an_approach_is_its_tell():
    from types import SimpleNamespace as Beat

    assert not camp.is_hook_beat(Beat(beat_type="revelation", directive=(
        "Tamsin ends up with a coin of that strange silver, pressed on her by a nervous onlooker.")))
    assert camp.is_hook_beat(Beat(beat_type="revelation", directive=(
        "If approached carefully, Vex can be pressed into revealing where his pay comes from.")))
    # campaign_forge rule 8 asks for "a tell, an approach" in a hook beat
    assert camp.is_hook_beat(Beat(beat_type="revelation", directive=(
        "Hesk approaches Bram, jittery; if pressed or paid, he names the fence.")))


def test_a_stalled_predecessor_lets_its_follow_up_play_out(spine):
    """A hook the player never took up stalls; what follows it is no longer
    waiting on it (`_arm_after_beats` armed it), so a leak can tell it."""
    sergeant = _beat(spine, 1, 6, "The Sergeant's Name", status="stalled", fired_day=1, fired_hour=10)
    lie = _beat(spine, 1, 7, "Coldiron's Lie, Uncovered", status="armed", trigger_type="after_beat",
                trigger_data={"beat_id": sergeant, "delay": "soon"}, reveals_truth_ids=["truth_betrayal"],
                directive="Coldiron admits the roster is eleven, not twenty.",
                delivery_keywords=["eleven able bodies", "roster"], scheduled_day=9, scheduled_hour=0,
                fallback_day=12, fallback_hour=0)
    line = "Eleven on the roster. Not twenty. Eleven."
    result = camp.mark_beat_delivery(spine["player"], line, spoken=_speech(spine, "coldiron", line),
                                     player_input='"How many men do you really have, Captain?"')
    assert "truth_betrayal" in result["truths_revealed"]
    assert "scene_owed" not in _truth(spine, "truth_betrayal")
    assert _row(lie).status == "delivered" and lie in result["played"]

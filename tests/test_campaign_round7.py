"""The story spine against round 7 (fleet8, 2026-10-05).

- fleet8-bram: "What Sorrel Saw" told at T18 — the ash-gray cloak by the
  north wall, "He whistled… the howling answered him" — and nagged three
  times ("north" is a word of North Gatehouse; "Watchman" waits for a second
  press); "The Notice Board" read word for word through @interact at T9,
  fired at T10, nagged three times, stalled, then "delivered" at T13 on "A
  board creaks… noticing a draft"; "Tally Marks" fired on the ambush turn;
  the proof hand-over ticked "Find out what is organizing the wolves" and the
  job closed with the act-3 cue on it still waiting; the tracker empty from
  T17.
- Beats that script the player ("Have Bram read the notice board", "If Bram
  presses Sorrel", "before she pockets the whistle").
- fleet8-wenna T3: a beat built on tomorrow's cart and a gate-pass hand-over
  delivered by play when the campaign was saved; a beat as she walked off.
- fleet8-tamsin T17-T19: the unrevealed villain face to face in act 1.

Replayed over every fleet transcript with a campaign DB (fleet-fleet8, 19
runs): see `later_keywords`, `EVIDENCE_WINDOW` and `_read_tells`.
"""

import pytest

from src.models import (
    NPC, Campaign, Connection, DMState, Event, Location, LocationType, Player, Quest, QuestStatus, StoryBeat,
    WorldClock, get_session,
)
from src.services import campaign as camp
from src.services import memory_files
from src.services import objectives as obj


@pytest.fixture(autouse=True)
def quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(camp, "_spawn_generation", lambda player_id: None)
    monkeypatch.setattr(memory_files, "memory_root", lambda: tmp_path / "memory")
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    camp.reset_state()
    yield
    camp.reset_state()


NOTICE = {"id": "notice_board", "kind": "read", "label": "Notice board", "x": 30, "y": 50,
          "payload": {"text": "TAKEN BY WOLVES — the Watch pays for true accounts of the howls. "
                              "Beneath it, smaller: 'Sweetroot for nerves. Ask Piper.'"}}
NAMES = ["Captain Brann Coldiron", "Sorrel Tack", "Piper Quillow", "Aldric Vane", "Gray-Flank Wolf",
         "Market Square", "Stable Yard", "North Gatehouse", "Wolf's Hollow", "Bram Holloway"]
SORREL_SAW = ("If Bram presses Sorrel Tack at the Stable Yard, have the boy admit — quietly, looking around "
              "first — that he saw a man in an ash-gray cloak crouched by the north wall two nights ago, and the "
              "man didn't run from the dark, he whistled something low into it and the howling answered. Sorrel "
              "won't give more than that without being pushed twice; on a second press he adds the man moved "
              "like he knew the wall guard's rounds, like an old Watchman would.")
SORREL_T18 = ("Two nights back. I was out checking the far stall and I saw a man crouched right against the north "
              "wall — ash-gray cloak, still as a post. He whistled. Low, just a few notes — and the howling "
              "answered him.")


@pytest.fixture()
def emberfall(db):
    """fleet8's Emberfall in miniature, act 1 armed at Day 1 18:00."""
    with get_session() as session:
        session.add(WorldClock(day=1, hour=18))
        session.add(DMState())
        square = Location(name="Market Square", type=LocationType.DISTRICT, visited=True, discovered=True,
                          objects=[NOTICE])
        yard = Location(name="Stable Yard", type=LocationType.DISTRICT, visited=True, discovered=True)
        gate = Location(name="North Gatehouse", type=LocationType.BUILDING, visited=True, discovered=True)
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, visited=False, discovered=True)
        session.add_all([square, yard, gate, hollow])
        session.flush()
        session.add_all([Connection(from_location_id=square.id, to_location_id=where.id, travel_type="walk",
                                    travel_time_hours=0.1) for where in (gate, yard)])
        people = {key: NPC(name=name, current_location_id=where.id) for key, name, where in (
            ("coldiron", "Captain Brann Coldiron", square), ("piper", "Piper Quillow", square),
            ("sorrel", "Sorrel Tack", yard), ("vane", "Aldric Vane", hollow))}
        people["vane"].danger_tier = "boss"
        session.add_all(people.values())
        player = Player(name="Bram Holloway", current_location_id=square.id)
        session.add(player)
        session.flush()
        teeth = Quest(title="Teeth on the North Road", status=QuestStatus.ACTIVE,
                      assigned_by_npc_id=people["coldiron"].id,
                      objectives=[{"text": "Get Captain Coldiron's gate pass", "kind": "obtain",
                                   "item_name": "Gate Pass"}])
        howls = Quest(title="Counting the Howls", status=QuestStatus.NOT_STARTED,
                      assigned_by_npc_id=people["piper"].id)
        session.add_all([teeth, howls])
        campaign = Campaign(
            player_id=player.id, title="What Sings in the Hollow", status="active", current_act=1,
            premise="Someone drills the wolves.", villain_npc_id=people["vane"].id, villain_name="Aldric Vane",
            acts=[{"act": 1, "title": "Teeth", "premise": "The howls keep time."},
                  {"act": 2, "title": "Whistles", "premise": "The pack answers."},
                  {"act": 3, "title": "The Howl", "premise": "It ends at the hollow."}],
            fixed_truths=[{"id": "truth_villain", "kind": "villain_identity", "revealed": False,
                           "statement": "Aldric Vane trains the wolves.", "spoiler_keywords": ["vane", "trains"]}])
        session.add(campaign)
        session.commit()
        ids = {k: v.id for k, v in people.items()}
        ids.update(player=player.id, campaign=campaign.id, square=square.id, yard=yard.id, gate=gate.id,
                   hollow=hollow.id, teeth=teeth.id, howls=howls.id)
        return ids


def _beat(w, act, seq, title, **kw):
    with get_session() as session:
        row = StoryBeat(campaign_id=w["campaign"], act=act, sequence=seq, title=title,
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


def _set(model, ident, **fields):
    with get_session() as session:
        row = session.get(model, ident)
        for key, value in fields.items():
            setattr(row, key, value)
        session.commit()


def _speech(w, who, text):
    return [{"npc_id": w[who], "npc_name": who.title(), "text": text}]


# --- 1. What counts as told ----------------------------------------------------

def test_a_word_a_place_name_shares_never_bars_a_phrase():
    """fleet8-bram T18: "north wall" was dropped for North Gatehouse's
    "north" — the sighting told nearly word for word stayed undelivered."""
    keywords = ["ash-gray cloak", "whistled", "north wall"]
    told, hits = camp.evidence_told(keywords, SORREL_T18, NAMES, ["whistle", "wolf"])
    assert told and hits == ["ash-gray cloak", "north wall"]
    # ...a name said whole is still no evidence
    assert camp.evidence_told(["North Gatehouse"], "At the North Gatehouse.", NAMES, fallback=False) == (False, [])


def test_a_second_press_is_not_owed_by_the_first():
    assert camp.later_keywords(SORREL_SAW, ["ash-gray cloak", "whistled", "north wall", "Watchman"]) == ["Watchman"]
    howl = ("Have Sorrel Tack say he saw a man in an ash-gray cloak by the north wall — he won't say more "
            "unless pressed, and even pressed he'll only admit the man was 'listening for something'.")
    assert camp.later_keywords(howl, ["ash-gray cloak", "north wall", "Sorrel", "listening"]) == ["listening"]
    beat = {"delivery_keywords": ["ash-gray cloak", "north wall", "Watchman"], "later_keywords": ["Watchman"],
            "names": NAMES, "common_words": []}
    assert camp.beat_keywords_delivered(beat, "A man in an ash-gray cloak by the north wall.")
    assert not camp.beat_keywords_delivered(dict(beat, later_keywords=[]), "An ash-gray cloak, nothing more.")


def test_a_second_stage_that_is_all_the_news_stays_owed():
    """'Pressing the Captain': the first press is a denial — the roster is
    the scene."""
    directive = ("If Bram confronts Coldiron, have him deny it. If Bram presses a second time, have him admit the "
                 "roster is thin — eleven, not twenty.")
    keywords = ["eleven", "roster", "Coldiron"]
    later = camp.later_keywords(directive, keywords)
    assert set(later) == {"eleven", "roster"}
    assert camp._first_stage(keywords, later, NAMES, []) == keywords


def test_a_keyword_of_several_words_is_told_whole_or_seconds():
    """fleet8-bram T13: "A board creaks somewhere above the stalls … like a
    man noticing a draft" delivered "The Notice Board", Sorrel speaking."""
    beat = {"delivery_keywords": ["notice board", "Coldiron", "sweetroot", "Sorrel"], "names": NAMES,
            "common_words": []}
    creak = ("A board creaks somewhere above the stalls, and a lean figure watches from the loft. Sorrel's face "
             "goes like a man noticing a draft he can't place.\nSorrel")
    assert not camp.beat_keywords_delivered(beat, creak)
    assert camp.beat_keywords_delivered(beat, "The board's nailed notice: 'Sweetroot for nerves. Ask Piper.'")
    # A partial one seconds a whole one: "the howls keep a schedule" told Piper's chart
    assert camp.evidence_told(["charted howls", "schedule"], "The howls keep a schedule. I counted them.",
                              NAMES, fallback=False)[0]
    # ...and stands alone only as a beat's only evidence ("whistle-note", fleet-bram T8)
    assert camp.evidence_told(["Wolf's Hollow", "whistle-note"], "More than the whistle, it chills you.",
                              NAMES, name_support=True)[0]


# --- 2. What the player read is told --------------------------------------------

def _journal(w, *entries):
    for entry in entries:
        memory_files.add_journal_entry(w["player"], entry)


NOTICE_BEAT = dict(directive="Have Bram read the notice board in Market Square: Coldiron is paying for true "
                             "accounts of the howls, and Piper's sweetroot note is pinned beneath it. Sorrel "
                             "Tack watches Bram read it.",
                   delivery_keywords=["notice board", "Coldiron", "sweetroot", "Sorrel"],
                   trigger_type="location", lead="Read the notice board in Market Square.")


def test_a_thing_the_player_read_delivers_its_beat_before_it_fires(emberfall):
    """fleet8-bram T9-T12: read word for word through @interact, then fired
    and nagged three times."""
    beat = _beat(emberfall, 1, 1, "The Notice Board", status="armed", fallback_day=8, fallback_hour=18,
                 trigger_data={"location_id": emberfall["square"]},
                 consequence_met=[{"op": "npc_mood", "npc_id": emberfall["sorrel"], "mood": "nervous"}],
                 **NOTICE_BEAT)
    _journal(emberfall, 'Notice board: "TAKEN BY WOLVES — the Watch pays for true accounts of the howls. '
                        "Beneath it, smaller: 'Sweetroot for nerves. Ask Piper.'\"")
    tick = camp.run_campaign_tick(emberfall["player"])
    assert tick["fired_beat"] is None
    assert _row(beat).status == "delivered"


def test_a_fired_beat_the_player_read_is_told_and_never_nagged(emberfall):
    beat = _beat(emberfall, 1, 1, "The Notice Board", status="fired", fired_day=1, fired_hour=18,
                 delivery_attempts=1, trigger_data={"location_id": emberfall["square"]}, **NOTICE_BEAT)
    _journal(emberfall, 'Notice board: "The Watch pays for true accounts. Sweetroot for nerves. Ask Piper."')
    result = camp.mark_beat_delivery(emberfall["player"], "Piper tucks her basket higher.",
                                     player_input="I read the board again.")
    assert result["delivered"] and _row(beat).status == "delivered"


def test_a_read_hands_over_nothing_a_hook_held_for_its_people(emberfall):
    """A fired hook told by the player's own read is nobody's scene: the job
    its person was to offer is not started behind the player's back (the
    @quest-start-before-yes of fleet8-wenna T3, by another door)."""
    beat = _beat(emberfall, 1, 1, "The Notice Board", status="fired", fired_day=1, fired_hour=18,
                 delivery_attempts=1, trigger_data={"location_id": emberfall["square"]},
                 consequence_met=[{"op": "activate_quest", "quest_id": emberfall["howls"]}], **NOTICE_BEAT)
    with get_session() as session:
        assert camp.is_hook_beat(session.get(StoryBeat, beat)), "'Have Bram read…' waits on the player"
    _journal(emberfall, 'Notice board: "The Watch pays for true accounts. Sweetroot for nerves. Ask Piper."')
    camp.mark_beat_delivery(emberfall["player"], "Piper tucks her basket higher.", player_input="I wait.")
    assert _row(beat).status == "delivered"
    with get_session() as session:
        assert session.get(Quest, emberfall["howls"]).status == QuestStatus.NOT_STARTED


def test_words_that_are_no_thing_of_the_world_deliver_nothing(emberfall):
    """A speech line in the journal ("Piper: …") is no read."""
    beat = _beat(emberfall, 1, 1, "The Notice Board", status="armed", fallback_day=8, fallback_hour=18,
                 trigger_data={"location_id": emberfall["yard"]}, **NOTICE_BEAT)
    _journal(emberfall, 'Piper: "The notice board? Sweetroot for nerves — that note is mine."')
    camp.run_campaign_tick(emberfall["player"])
    assert _row(beat).status == "armed"


# --- 3. Beats that script the player ---------------------------------------------

def test_a_beat_whose_cue_is_the_players_own_act_is_a_hook(emberfall):
    notice = _beat(emberfall, 1, 1, "The Notice Board", **NOTICE_BEAT)
    sorrel = _beat(emberfall, 1, 2, "What Sorrel Saw", directive=SORREL_SAW)
    offer = _beat(emberfall, 1, 3, "A Captain Who Needs Hands",
                  directive="Have Captain Coldiron approach Bram and offer him the job. Coldiron should be curt "
                            "about pay and vague about Watch numbers if Bram presses.")
    with get_session() as session:
        rows = {i: session.get(StoryBeat, i) for i in (notice, sorrel, offer)}
        assert camp.is_hook_beat(rows[notice]) and camp.is_hook_beat(rows[sorrel])
        assert not camp.is_hook_beat(rows[offer]), "a closing 'if Bram presses' only shades the offer"
        assert camp.player_scripted(rows[sorrel])[0] == "If Bram presses"


def test_a_scripted_act_in_passing_is_named_back_as_the_players(emberfall):
    """fleet8-tamsin T15: "…before she pockets the whistle"; the @give went
    round the reach guard the @interact had just met."""
    beat = _beat(emberfall, 1, 1, "The Tally", status="fired", fired_day=1, fired_hour=18, delivery_attempts=1,
                 directive="Have the tally marks found under the word SING, and the cracked whistle in the crate. "
                           "Let her read it as evidence, before she pockets the whistle.")
    with get_session() as session:
        row = session.get(StoryBeat, beat)
        assert not camp.is_hook_beat(row)
        notes = camp._directive_notes(session, row, session.get(Player, emberfall["player"]))
    assert any(n.startswith("PLAYER'S PART") and "before she pockets the whistle" in n for n in notes)


def test_do_not_have_him_confront_her_is_no_hook():
    from types import SimpleNamespace as Beat

    assert not camp.is_hook_beat(Beat(beat_type="revelation", directive=(
        "Have Vex shadow her rather than attack. Do not have him confront her directly.")))


# --- 4. Fired on the ambush turn; leaving as it fires ------------------------------

def test_no_beat_fires_on_an_ambush_turn(emberfall):
    """fleet8-bram T4: the arrival ambush's one sentence was nagged for
    "Tally Marks at the Hollow"; it landed in the aftermath."""
    beat = _beat(emberfall, 1, 1, "Tally Marks", status="armed", scheduled_day=1, scheduled_hour=18,
                 fallback_day=4, fallback_hour=18, trigger_type="time")
    camp.hold_beats_this_reply(emberfall["player"])         # the orchestrator, on an ambush reply
    assert camp.run_campaign_tick(emberfall["player"])["fired_beat"] is None
    assert _row(beat).status == "armed"
    assert camp.run_campaign_tick(emberfall["player"])["fired_beat"]["title"] == "Tally Marks"


def test_the_orchestrator_holds_beats_on_an_ambush_reply(monkeypatch):
    from tests.test_context_directives import _fake
    from src.agents import dm_orchestrator
    from src.agents.dm_orchestrator import DMOrchestrator

    fake = _fake(monkeypatch)
    held: list[bool] = []
    monkeypatch.setattr(dm_orchestrator, "run_campaign_tick", lambda pid: held.append(pid in camp._HOLD_BEATS) or {})
    DMOrchestrator._build_context(fake, "I ask him his name.", extra_blocks=["[AMBUSH]"], constrained=True)
    DMOrchestrator._build_context(fake, "I ask him his name.")
    assert held == [True, True], "the flag is the tick's to read (this fake never pops it)"
    camp.reset_state()
    DMOrchestrator._build_context(fake, "I ask him his name.")
    assert held[-1] is False


def test_a_beat_firing_as_the_player_leaves_goes_with_them(emberfall):
    """fleet8-wenna T3: 'I head toward the North Gatehouse' — Piper caught
    her sleeve and the trip never happened."""
    _beat(emberfall, 1, 1, "The Notice and the Nerves", status="armed", scheduled_day=1, scheduled_hour=18,
          fallback_day=4, fallback_hour=18, delivery_keywords=["tally", "sweetroot"],
          directive="Have Piper Quillow approach Bram near the notice board, sweetroot in hand.",
          lead_target={"type": "npc", "id": emberfall["piper"]})
    tick = camp.run_campaign_tick(emberfall["player"])
    assert tick["fired_beat"] and tick["audit"]["beat"] is not None
    note = camp.departure_note(emberfall["player"], "I head to the North Gatehouse to get that pass.")
    assert "LEAVING for North Gatehouse" in note and "@npc-move Piper Quillow | North Gatehouse" in note
    assert tick["audit"]["beat"] is None, "a beat the player walked away from is not nagged"
    assert camp.departure_note(emberfall["player"], "I ask Piper about her tally.") == ""
    # The place is read as the travel line reads it: the last phrase that IS
    # one ("Piper" is a person — fleet8-wenna T3's "going to remember…")
    note = camp.departure_note(emberfall["player"], "I head to the North Gatehouse. Then I walk over to Piper.")
    assert "LEAVING for North Gatehouse" in note


def test_a_departure_replaces_the_let_it_interrupt_them_order(emberfall):
    """fleet8-wenna T3: "let it interrupt them" is how Piper caught her
    sleeve; the departure NOTE takes that order's place, never sits after it."""
    _beat(emberfall, 1, 1, "The Notice and the Nerves", status="armed", scheduled_day=1, scheduled_hour=18,
          fallback_day=4, fallback_hour=18, delivery_keywords=["tally", "sweetroot"],
          directive="Have Piper Quillow approach Bram near the notice board, sweetroot in hand.",
          lead_target={"type": "npc", "id": emberfall["piper"]})
    briefing = camp.format_campaign_context(camp.run_campaign_tick(emberfall["player"]))
    assert "let it interrupt them" in briefing
    note = camp.departure_note(emberfall["player"], "I head to the North Gatehouse to get that pass.")
    leaving = camp.with_departure(briefing, note)
    assert "let it interrupt them" not in leaving and leaving.endswith(note)
    assert "Have Piper Quillow approach Bram" in leaving
    assert camp.with_departure(briefing, "") == briefing
    again = camp.format_campaign_context(camp.run_campaign_tick(emberfall["player"]))   # re-sent, attempt 2
    assert "You were given this beat before" in again
    note = camp.departure_note(emberfall["player"], "I head to the North Gatehouse to get that pass.")
    assert "You were given this beat before" not in camp.with_departure(again, note)


def test_the_worlds_clock_still_lands_on_the_way(emberfall):
    """A deadline is no scene to wait: with its "deliver it now" close gone,
    the departure NOTE itself says it lands this reply, on the road."""
    _beat(emberfall, 1, 1, "The Evening Howl", beat_type="deadline_event", status="armed", scheduled_day=1,
          scheduled_hour=18, fallback_day=1, fallback_hour=18, delivery_keywords=["howl", "barred"],
          directive="At the stroke of eight the howling rises and the gate bars behind it.")
    briefing = camp.format_campaign_context(camp.run_campaign_tick(emberfall["player"]))
    note = camp.departure_note(emberfall["player"], "I head to the North Gatehouse.")
    assert "deliver it in this reply" in note and "on the way" in note
    assert "let it interrupt them" not in camp.with_departure(briefing, note)


# --- 5. Played already — or not ---------------------------------------------------

def test_a_job_already_taken_is_not_the_scene_lived(emberfall):
    """fleet8-wenna T3: 'The Cart Comes Back Wrong' (built on tomorrow's
    cart; Coldiron hands over the gate pass) was delivered by play because
    the opening had started its job."""
    with get_session() as session:
        session.add(Event(name="The Greywater Cart Arrives Short", description="The overdue cart rolls in.",
                          event_type="meso", scheduled_day=2, scheduled_hour=9, player_visible=True))
        session.commit()
    beat = _beat(emberfall, 1, 2, "The Cart Comes Back Wrong", status="armed", scheduled_day=3,
                 scheduled_hour=18, fallback_day=6, fallback_hour=18,
                 directive="Build on the scheduled arrival of the half-empty Greywater cart: have Captain "
                           "Coldiron question the driver, then offer Bram the gate pass a little too readily.",
                 consequence_met=[{"op": "activate_quest", "quest_id": emberfall["teeth"]}])
    camp.run_campaign_tick(emberfall["player"])
    assert _row(beat).status == "armed"
    with get_session() as session:
        cart = session.query(Event).one()
        cart.occurred_day, cart.occurred_hour = 2, 9
        session.get(Player, emberfall["player"]).inventory = [{"name": "Gate Pass", "type": "key"}]
        session.commit()
    camp.run_campaign_tick(emberfall["player"])
    assert _row(beat).status == "delivered", "the cart came and the pass is held: nothing is left of it"


def test_a_scene_built_on_a_scheduled_event_waits_for_it(emberfall):
    """Freed from the by-play retirement, "The Cart Comes Back Wrong" must not
    fire before the cart it builds on — that tells the cart early."""
    with get_session() as session:
        session.add(Event(name="The Greywater Cart Arrives Short", description="The overdue cart rolls in.",
                          event_type="meso", scheduled_day=2, scheduled_hour=9, player_visible=True))
        session.commit()
    beat = _beat(emberfall, 1, 2, "The Cart Comes Back Wrong", status="armed", scheduled_day=1,
                 scheduled_hour=18, fallback_day=4, fallback_hour=18,
                 directive="Build on the scheduled arrival of the half-empty Greywater cart: have Captain "
                           "Coldiron question the driver in the open market.")
    assert camp.run_campaign_tick(emberfall["player"])["fired_beat"] is None
    with get_session() as session:
        cart = session.query(Event).one()
        cart.occurred_day, cart.occurred_hour = 1, 18
        session.commit()
    assert camp.run_campaign_tick(emberfall["player"])["fired_beat"]["title"] == "The Cart Comes Back Wrong"
    assert _row(beat).status == "fired"


def test_a_cue_that_can_never_come_true_comes_due_soon(emberfall):
    """fleet8-bram: act-3 "Vex Marrow's Ultimatum" waits on Teeth being
    ACTIVE — paid in full at T8."""
    beat = _beat(emberfall, 1, 4, "Vex Marrow's Ultimatum", beat_type="setpiece", status="armed",
                 trigger_type="quest_status", trigger_data={"quest_id": emberfall["teeth"], "status": "active"},
                 fallback_day=8, fallback_hour=18)
    _set(Quest, emberfall["teeth"], status=QuestStatus.COMPLETED)
    camp.run_campaign_tick(emberfall["player"])
    row = _row(beat)
    assert (row.fallback_day, row.fallback_hour) == (2, 0)
    with get_session() as session:
        notes = camp._directive_notes(session, session.get(StoryBeat, beat), session.get(Player, emberfall["player"]))
    assert any('cued by "Teeth on the North Road"' in n and "already finished" in n for n in notes)


def test_a_player_whose_work_is_done_opens_the_next_act(emberfall):
    """fleet8-bram: both jobs paid by T16, act 1 holding only a day-6 clock."""
    _beat(emberfall, 1, 1, "The Notice Board", status="delivered")
    _beat(emberfall, 1, 2, "The Cart Comes in Lame", beat_type="deadline_event", status="armed",
          scheduled_day=6, scheduled_hour=18, fallback_day=6, fallback_hour=18)
    _beat(emberfall, 2, 3, "Hesk's Ledger", trigger_type="time", lead="Ask Hesk where his stock comes from.")
    _beat(emberfall, 3, 4, "The Second Signature", beat_type="confrontation", cinematic="finale")
    camp.run_campaign_tick(emberfall["player"])
    with get_session() as session:
        assert session.get(Campaign, emberfall["campaign"]).current_act == 1, "a job still open: no pull"
    _set(Quest, emberfall["teeth"], status=QuestStatus.COMPLETED)
    camp.run_campaign_tick(emberfall["player"])
    with get_session() as session:
        assert session.get(Campaign, emberfall["campaign"]).current_act == 2


def test_no_finished_work_no_pull(emberfall):
    """The opening, not a lull: nothing finished yet."""
    _beat(emberfall, 1, 1, "The Notice Board", status="delivered")
    _beat(emberfall, 1, 2, "The Cart Comes in Lame", beat_type="deadline_event", status="armed",
          scheduled_day=6, scheduled_hour=18, fallback_day=6, fallback_hour=18)
    _beat(emberfall, 2, 3, "Hesk's Ledger", trigger_type="time")
    _beat(emberfall, 3, 4, "The Second Signature", beat_type="confrontation", cinematic="finale")
    _set(Quest, emberfall["teeth"], status=QuestStatus.NOT_STARTED)
    camp.run_campaign_tick(emberfall["player"])
    with get_session() as session:
        assert session.get(Campaign, emberfall["campaign"]).current_act == 1


# --- 6. The tracker is never empty while the story has a next step ---------------

def test_an_empty_tracker_takes_the_next_acts_lead(emberfall):
    from src.services.lead import compute_lead

    _set(Quest, emberfall["teeth"], status=QuestStatus.COMPLETED)
    _beat(emberfall, 1, 1, "The Cart Comes in Lame", beat_type="deadline_event", status="armed",
          scheduled_day=6, scheduled_hour=18, fallback_day=6, fallback_hour=18)
    scene = _beat(emberfall, 2, 2, "The Man Who Trained the Wolves", beat_type="confrontation")
    _beat(emberfall, 2, 3, "Pressing the Captain", trigger_type="after_beat",
          trigger_data={"beat_id": scene, "delay": "soon"}, lead="Bring what you learned to the Captain.")
    _beat(emberfall, 2, 4, "Hesk's Ledger", lead="Ask Hesk where his stock comes from.",
          lead_target={"type": "npc", "id": emberfall["piper"]})
    with get_session() as session:
        lead = compute_lead(session, session.get(Player, emberfall["player"]))
    assert lead["text"] == "Ask Hesk where his stock comes from.", "never one waiting on a scene unplayed"
    assert lead["target"]["id"] == emberfall["piper"]


def test_a_firing_beats_lead_comes_first_on_an_empty_tracker(emberfall):
    from src.services.lead import compute_lead

    _set(Quest, emberfall["teeth"], status=QuestStatus.COMPLETED)
    _beat(emberfall, 1, 1, "What Sorrel Saw", status="fired", fired_day=1, fired_hour=18,
          directive="Have Sorrel admit he saw a man by the wall.", lead="Ask the stable hand about the wall.")
    _beat(emberfall, 2, 2, "Hesk's Ledger", lead="Ask Hesk where his stock comes from.")
    with get_session() as session:
        assert compute_lead(session, session.get(Player, emberfall["player"]))["text"] == \
            "Ask the stable hand about the wall."


# --- 7. The villain stays offstage until the story brings them on ------------------

def test_the_villain_is_offstage_until_their_scene_fires(emberfall):
    """fleet8-tamsin T17-T19: Aldric Vane stepped out at the Hollow in act 1."""
    scene = _beat(emberfall, 2, 6, "The Man Who Trained the Wolves", beat_type="confrontation",
                  directive="Have Aldric Vane step out to meet Bram at Wolf's Hollow.",
                  reveals_truth_ids=["truth_villain"])
    _beat(emberfall, 2, 7, "The Toll Demand", beat_type="confrontation",
          directive="Have a runner bring Vane's demand, nailed to the gate.")
    with get_session() as session:
        vane = session.get(NPC, emberfall["vane"])
        assert camp.villain_held_line(session, emberfall["player"], vane) == camp.VILLAIN_OFFSTAGE_LINE
    tick = camp.run_campaign_tick(emberfall["player"])
    assert tick["audit"]["offstage_villain"] == {"id": emberfall["vane"], "name": "Aldric Vane"}
    _set(StoryBeat, scene, status="fired")
    with get_session() as session:
        vane = session.get(NPC, emberfall["vane"])
        assert camp.villain_held_line(session, emberfall["player"], vane) == camp.VILLAIN_HELD_LINE


def test_the_offstage_villains_dossier_gives_no_speaker_label(emberfall):
    from src.services.scene_brief import build_scene_brief

    _beat(emberfall, 2, 6, "The Man Who Trained the Wolves", beat_type="confrontation",
          directive="Have Aldric Vane step out to meet Bram at Wolf's Hollow.", reveals_truth_ids=["truth_villain"])
    _set(Player, emberfall["player"], current_location_id=emberfall["hollow"])
    with get_session() as session:
        text = build_scene_brief(session, session.get(Player, emberfall["player"]))["text"]
    assert "OFFSTAGE until the story brings them on" in text
    assert "as the speaker label" not in text
    assert "If glimpsed at all, in prose they are only '" in text, "a glimpse goes by what the player sees"


def test_the_villains_scene_is_wherever_a_beat_stages_them(emberfall):
    """fleet7-tamsin's act-3 setpiece "The Counting at the Hollow" has
    "Aldric Vane overseeing with the whistle at his hip" — two beats before
    the finale the scan resolved to; a beat of any kind that has them step
    out stages them too, and "do not have Vane…" never does."""
    twist = _beat(emberfall, 2, 5, "A Shape on the Road", beat_type="twist",
                  directive="Do not have Vane appear; only his tracks at the wall.")
    setpiece = _beat(emberfall, 3, 11, "The Counting at the Hollow", beat_type="setpiece",
                     directive="Crates stamped Greywater, Aldric Vane overseeing with the whistle at his hip.")
    _beat(emberfall, 3, 13, "What the Road Costs", beat_type="confrontation",
          directive="Have Vane make his last stand at Wolf's Hollow.")

    def scene():
        with get_session() as session:
            campaign, vane = session.get(Campaign, emberfall["campaign"]), session.get(NPC, emberfall["vane"])
            return camp._villain_scene(session, campaign, vane).id

    assert scene() == setpiece
    _set(StoryBeat, twist, directive="Have Aldric Vane step out of the birches and name his price.")
    assert scene() == twist


def test_the_audit_flags_the_villain_speaking_before_their_scene():
    from src.web.turn_audit import audit_turn

    line = {"npc_id": "vane-id", "npc_name": "Wolf-Mantled Man", "text": "I'll have it back."}
    warnings = audit_turn("He steps out of the birches.", [], {}, {}, spoken=[line], spoken_text=line["text"],
                          campaign={"beat": None, "sealed_truths": [], "known_words": [],
                                    "offstage_villain": {"id": "vane-id", "name": "Aldric Vane"}})
    assert [w["check"] for w in warnings if w["check"] == "villain_before_their_scene"]
    quiet = audit_turn("He steps out of the birches.", [], {}, {}, spoken=[line], spoken_text=line["text"],
                       campaign={"beat": None, "sealed_truths": [], "known_words": [], "offstage_villain": None})
    assert not [w for w in quiet if w["check"] == "villain_before_their_scene"]


def test_the_audit_never_nags_a_beat_the_engine_marked_told(emberfall):
    from src.web.turn_audit import audit_turn

    beat = _beat(emberfall, 1, 1, "The Notice Board", status="delivered", **NOTICE_BEAT)
    copy = {"beat_id": beat, "title": "The Notice Board", "delivery_keywords": NOTICE_BEAT["delivery_keywords"],
            "names": NAMES, "common_words": []}
    campaign = {"beat": copy, "sealed_truths": [], "known_words": []}
    assert not [w for w in audit_turn("Piper waves.", [], {}, {}, campaign=campaign)
                if w["check"] == "campaign_beat_not_delivered"]
    _set(StoryBeat, beat, status="fired")
    assert [w for w in audit_turn("Piper waves.", [], {}, {}, campaign=campaign)
            if w["check"] == "campaign_beat_not_delivered"]


# --- 7b. Sealed truths: who knows them, and no invented past -----------------------

def test_a_sealed_truth_is_known_only_to_the_people_it_names(emberfall):
    """fleet8-bram T16: the herbalist hinted at the Captain's sealed roster
    ("lips moving over the roster"); T7: the Captain invented a "twin" whistle
    lost to a man the Watch buried, against the truths."""
    text = camp.format_campaign_context(camp.run_campaign_tick(emberfall["player"]))
    assert camp.SEALED_KNOWERS_LINE in text
    assert "only the people it names" in camp.SEALED_KNOWERS_LINE
    assert "Nobody invents a past" in camp.SEALED_KNOWERS_LINE
    with get_session() as session:
        campaign = session.get(Campaign, emberfall["campaign"])
        campaign.fixed_truths = [dict(t, revealed=True) for t in campaign.fixed_truths]
        session.commit()
    assert camp.SEALED_KNOWERS_LINE not in camp.format_campaign_context(camp.run_campaign_tick(emberfall["player"]))


# --- 8. A question the story answers ----------------------------------------------

def test_a_question_step_is_the_storys_to_answer():
    assert obj.investigates({"kind": "custom", "text": "Find out what is organizing the wolves"})
    assert obj.investigates({"kind": "custom", "text": "Learn who the letter was meant for"})
    assert not obj.investigates({"kind": "custom", "text": "Stand out the next howl and mark where it rises"})
    assert not obj.investigates({"kind": "talk_to", "text": "Who is Sorrel?"})


def test_the_offstage_villain_is_left_off_the_players_screen(emberfall):
    """Round-7 spine review: the seed parks the villain at the act-1
    destination, so his sprite stood there while his dossier said OFFSTAGE.
    The canvas roster and the state's people leave him out until his scene."""
    scene = _beat(emberfall, 2, 6, "The Man Who Trained the Wolves", beat_type="confrontation",
                  directive="Have Aldric Vane step out to meet Bram at Wolf's Hollow.",
                  reveals_truth_ids=["truth_villain"])
    with get_session() as session:
        assert camp.offstage_ids(session, emberfall["player"]) == {emberfall["vane"]}
    _set(StoryBeat, scene, status="fired")
    with get_session() as session:
        assert camp.offstage_ids(session, emberfall["player"]) == set()
        assert camp.offstage_ids(session, None) == set()

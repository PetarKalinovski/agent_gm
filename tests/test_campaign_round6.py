"""The story spine against round 6 (fleet7, 2026-10-05).

- fleet7-tamsin: "SING" ("When Tamsin reaches Wolf's Hollow…") had a
  quest_status trigger and no location lead; it fired in the Stable Yard and
  the DM turned the act-1 expedition into a two-reply prose trek. Its act-2
  reveal came free: Coldiron deflected and confessed in one breath.
- fleet7-wenna: "The Gate Bars at the Howl" ("…the evening howl rises as
  already scheduled") stayed armed for Day 6 after the Day-1 howl fired;
  "A Man Who Knows More Than He Says" (act 1) stayed armed after act 2 had
  played Coldiron past it; the ambush killed the wolves an act-3 setpiece
  stages; a false leak un-sealed the act-3 cost truth on day 1 from "goes
  down … chokes into silence".

Leak replay over every fleet transcript x every campaign's truths (fleet to
fleet7, the production rule with subjects and cross terms): 9 own-campaign
hits before, 8 after — the fleet7-wenna T15 false one gone, the eight true
reveals of earlier rounds kept; cross-campaign probes 148 -> 120, removals
only.
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


@pytest.fixture()
def spine(db):
    """Emberfall in miniature: the square with its Stable Yard, the Hollow
    and its wolves, act 1 current at Day 1 18:00."""
    with get_session() as session:
        session.add(WorldClock(day=1, hour=18))
        session.add(DMState())
        square = Location(name="Market Square", type=LocationType.DISTRICT, visited=True, discovered=True)
        session.add(square)
        session.flush()
        yard = Location(name="Stable Yard", type=LocationType.POI, parent_id=square.id, visited=True,
                        discovered=True)
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, discovered=True)
        session.add_all([yard, hollow])
        session.flush()
        people = {
            "coldiron": NPC(name="Captain Brann Coldiron", current_location_id=square.id),
            "sorrel": NPC(name="Sorrel Tack", current_location_id=yard.id),
            "vane": NPC(name="Aldric Vane", current_location_id=hollow.id, danger_tier="boss"),
            "wolf": NPC(name="Hollow Wolf", species="wolf", danger_tier="weak", current_location_id=hollow.id),
            "flank": NPC(name="Gray-Flank Wolf", species="wolf", danger_tier="weak",
                         current_location_id=hollow.id),
        }
        session.add_all(people.values())
        quest = Quest(title="Teeth on the North Road", status=QuestStatus.NOT_STARTED)
        session.add(quest)
        player = Player(name="Tamsin Rook", current_location_id=yard.id)
        session.add(player)
        session.flush()
        campaign = Campaign(player_id=player.id, title="The Toll Road", status="active", current_act=1,
                            villain_npc_id=people["vane"].id, premise="Someone drills the wolves.",
                            acts=[{"act": 1, "title": "Teeth", "premise": "The howls keep a schedule."},
                                  {"act": 2, "title": "Grudges", "premise": "Old debts."},
                                  {"act": 3, "title": "The Gate", "premise": "It ends at the gate."}],
                            fixed_truths=[{"id": "truth_betrayal", "kind": "the_betrayal", "revealed": False,
                                           "statement": "Coldiron's whistle and Vane's are a matched pair.",
                                           "spoiler_keywords": ["matched", "trained"]}])
        session.add(campaign)
        session.commit()
        ids = {k: v.id for k, v in people.items()}
        ids.update(player=player.id, campaign=campaign.id, square=square.id, yard=yard.id, hollow=hollow.id,
                   quest=quest.id)
        return ids


def _beat(spine, act, seq, title, **kw):
    with get_session() as session:
        row = StoryBeat(campaign_id=spine["campaign"], act=act, sequence=seq, title=title,
                        beat_type=kw.pop("beat_type", "revelation"), directive=kw.pop("directive", title),
                        trigger_type=kw.pop("trigger_type", "time"),
                        trigger_data=kw.pop("trigger_data", {"delay": "days"}),
                        status=kw.pop("status", "armed"), **kw)
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


def _clock(day, hour):
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.day, clock.hour = day, hour
        session.commit()


SING = ("When Tamsin reaches Wolf's Hollow per her quest, have her find the tally marks grouped in threes and "
        "the word SING carved into the doorframe, and the cracked bone whistle lying nearby.")


# --- 1. A scene at a place waits for the player there ------------------------

def test_a_beat_that_waits_on_an_arrival_fires_only_there(spine):
    """fleet7-tamsin T8: "SING" fired in the Stable Yard on its quest trigger."""
    sing = _beat(spine, 1, 5, "SING", directive=SING, trigger_type="quest_status",
                 trigger_data={"quest_id": spine["quest"], "status": "active"},
                 armed_day=1, armed_hour=18, fallback_day=8, fallback_hour=18)
    _set(Quest, spine["quest"], status=QuestStatus.ACTIVE)
    tick = camp.run_campaign_tick(spine["player"])
    assert not tick.get("fired_beat")
    assert _row(sing).status == "armed"

    # ...past its backstop too: the place is never brought to the player
    _clock(8, 20)
    assert not camp.run_campaign_tick(spine["player"]).get("fired_beat")

    _set(Player, spine["player"], current_location_id=spine["hollow"])
    tick = camp.run_campaign_tick(spine["player"])
    assert (tick.get("fired_beat") or {}).get("title") == "SING"


def test_word_reaching_a_place_is_news_not_an_arrival(spine):
    news = _beat(spine, 1, 1, "Word Spreads",
                 directive="If Tamsin has gone north, have word reach Market Square that the cart is lost.")
    sing = _beat(spine, 1, 2, "SING", directive=SING)
    with get_session() as session:
        assert camp._arrival_place_ids(session, session.get(StoryBeat, news)) == set()
        assert camp._arrival_place_ids(session, session.get(StoryBeat, sing)) == {spine["hollow"]}


def test_a_scene_whose_place_the_player_left_waits_there_and_fires_on_return(spine):
    sing = _beat(spine, 1, 5, "SING", directive=SING, status="fired", fired_day=1, fired_hour=18,
                 delivery_attempts=1, armed_day=1, armed_hour=18)
    tick = camp.run_campaign_tick(spine["player"])           # the player is in the yard
    assert not tick.get("fired_beat")
    assert _row(sing).status == "stalled"

    _set(Player, spine["player"], current_location_id=spine["hollow"])
    tick = camp.run_campaign_tick(spine["player"])
    assert (tick.get("fired_beat") or {}).get("title") == "SING"
    assert any("they are where it happens now" in n for n in tick["fired_beat"]["notes"])


def test_a_deadline_away_from_its_place_is_told_as_news_from_there(spine):
    _beat(spine, 1, 3, "The Den Wakes", beat_type="deadline_event",
          directive="The pack leaves the den at Wolf's Hollow in a body.",
          lead_target={"type": "location", "id": spine["hollow"]},
          scheduled_day=1, scheduled_hour=18, fallback_day=1, fallback_hour=18, armed_day=1, armed_hour=12)
    fired = camp.run_campaign_tick(spine["player"])["fired_beat"]
    assert fired["title"] == "The Den Wakes"
    note = next(n for n in fired["notes"] if n.startswith("WHERE IT HAPPENS"))
    assert "Wolf's Hollow" in note and "Stable Yard" in note and "news" in note
    assert "Never take the player there" in note


# --- 2. Act bookkeeping ------------------------------------------------------

def _howl(spine, *, occurred=True):
    with get_session() as session:
        session.add(Event(
            name="The Evening Howl", event_type="meso", scheduled_day=1, scheduled_hour=20,
            occurred_day=1 if occurred else None, occurred_hour=20 if occurred else None,
            narrated_to_player=occurred, player_visible=True,
            description=("At the stroke of eight, howling rises from the northern hills — layered, answering "
                         "itself, closer than last night. The North Gate is barred behind it and the square "
                         "empties fast."),
            consequences=["North Gate barred for the night", "Townsfolk clear the square"],
        ))
        session.commit()


GATE_BARS = ("At the stroke of eight the evening howl rises as already scheduled. Have the gate guards bar the "
             "North Gate hard and fast, and have Sorrel Tack, shaken, tell anyone nearby that he saw a man in an "
             "ash-gray cloak crouched by the north wall two nights ago.")


def test_a_beat_built_on_a_scheduled_event_comes_due_once_it_happened(spine):
    """fleet7-wenna: armed for Day 6 while the Day-1 howl it builds on fired."""
    gate = _beat(spine, 1, 2, "The Gate Bars at the Howl", beat_type="deadline_event", directive=GATE_BARS,
                 delivery_keywords=["ash-gray cloak", "north wall", "Sorrel"],
                 scheduled_day=6, scheduled_hour=18, fallback_day=6, fallback_hour=18, armed_day=1, armed_hour=18)
    _clock(1, 19)
    _howl(spine, occurred=False)
    assert not camp.run_campaign_tick(spine["player"]).get("fired_beat")     # not yet: nothing happened
    assert (_row(gate).scheduled_day, _row(gate).scheduled_hour) == (6, 18)

    with get_session() as session:
        event = session.query(Event).filter(Event.name == "The Evening Howl").one()
        event.occurred_day, event.occurred_hour, event.narrated_to_player = 1, 20, True
        session.commit()
    _clock(1, 21)
    fired = camp.run_campaign_tick(spine["player"])["fired_beat"]
    assert fired["title"] == "The Gate Bars at the Howl"
    assert any('"The Evening Howl" already happened' in n for n in fired["notes"])


def test_a_beat_merely_near_an_event_keeps_its_own_clock(spine):
    gate = _beat(spine, 1, 2, "The Gate Bars", beat_type="deadline_event",
                 directive="Have the gate guards bar the North Gate early and Sorrel Tack shout a warning.",
                 scheduled_day=6, scheduled_hour=18, fallback_day=6, fallback_hour=18, armed_day=1, armed_hour=18)
    _howl(spine)
    _clock(1, 21)
    assert not camp.run_campaign_tick(spine["player"]).get("fired_beat")
    assert _row(gate).status == "armed" and _row(gate).scheduled_day == 6


def test_an_earlier_acts_beat_a_later_act_played_past_is_skipped(spine):
    """fleet7-wenna: "A Man Who Knows More Than He Says" (Coldiron waves the
    pattern off) still armed after act 2's "The Cracked Whistle" had him
    name Vane."""
    _set(Campaign, spine["campaign"], current_act=2)
    coldiron = {"type": "npc", "id": spine["coldiron"]}
    mood = [{"op": "npc_mood", "npc_id": spine["coldiron"], "mood": "guarded"}]
    stale = _beat(spine, 1, 3, "A Man Who Knows More Than He Says", trigger_type="after_beat",
                  directive="Have him wave it off too quickly.", lead_target=coldiron, consequence_met=mood,
                  scheduled_day=2, scheduled_hour=0, armed_day=1, armed_hour=18)
    sealed = _beat(spine, 1, 4, "The Matched Pair", lead_target=coldiron, reveals_truth_ids=["truth_betrayal"],
                   scheduled_day=9, scheduled_hour=0)
    clock = _beat(spine, 1, 5, "The Cart Comes Short", beat_type="deadline_event", lead_target=coldiron,
                  scheduled_day=9, scheduled_hour=0, fallback_day=9, fallback_hour=0)
    _beat(spine, 2, 7, "The Cracked Whistle", status="delivered", lead_target=coldiron, consequence_met=mood)
    camp.run_campaign_tick(spine["player"])
    assert _row(stale).status == "skipped"
    assert _row(sealed).status == "armed", "a sealed truth still needs its telling"
    assert _row(clock).status == "armed", "a deadline is the world's clock"


def test_a_late_beat_and_one_whose_wolves_died_say_so(spine):
    _set(Campaign, spine["campaign"], current_act=2)
    _set(NPC, spine["wolf"], status="dead")
    late = _beat(spine, 1, 3, "The Cart Limps In", directive="Have the driver mutter about herded wolves.")
    den = _beat(spine, 3, 11, "The Cached Cargo", status="pending",
                directive="Have Hollow Wolf and Gray-Flank Wolf prowl the den's edge at Wolf's Hollow.")
    with get_session() as session:
        player = session.get(Player, spine["player"])
        late_notes = camp._directive_notes(session, session.get(StoryBeat, late), player)
        den_notes = camp._directive_notes(session, session.get(StoryBeat, den), player)
    assert any("comes late" in n for n in late_notes)
    dead = next(n for n in den_notes if "dead" in n)
    assert dead.startswith("Hollow Wolf is dead") and "Gray-Flank" not in dead
    assert not any("comes late" in n for n in den_notes)


def test_the_story_cast_is_everyone_a_beat_still_needs(spine):
    _beat(spine, 3, 11, "The Cached Cargo", status="pending",
          directive="Have Hollow Wolf prowl the den's edge at Wolf's Hollow.")
    _beat(spine, 2, 6, "A Warning", status="armed", directive="A scrap pinned at Wolf's Hollow.",
          consequence_met=[{"op": "move_npc", "npc_id": spine["sorrel"], "to": spine["square"]}])
    _beat(spine, 1, 1, "Done Already", status="delivered", directive="Have Gray-Flank Wolf howl.")
    with get_session() as session:
        cast = camp.story_cast_ids(session, spine["player"])
    assert cast == {spine["vane"], spine["wolf"], spine["sorrel"]}


# --- 3. A false leak ---------------------------------------------------------

COST = {"id": "truth_cost", "kind": "the_cost", "revealed": False,
        "statement": ("Stopping Vane's whistle code means someone has to get close enough inside Wolf's Hollow to "
                      "break or steal it, and the pack will not scatter until the whistle itself is silenced or "
                      "Vane is taken down."),
        "spoiler_keywords": ["silence the whistle", "pack obeys", "vane taken down"]}
T15 = ("The second wolf goes down with a final snarl that chokes into silence, and the hollow is suddenly, "
       "horribly quiet again — just your own ragged breathing and the smell of blood on cold leaf-mould.")


def test_a_term_left_with_a_stray_word_after_what_the_player_knows_tells_nothing():
    """fleet7-wenna T15: "silence the whistle" (whistle known) and "vane taken
    down" (a name, an everyday word) left "silence" and "down"."""
    known = ["Aldric Vane", "Bring the cracked bone whistle back to the Captain"]
    assert camp.sealed_truth_hits([COST], T15, known) == {}
    kf = camp._text_forms(" ".join(known))
    assert camp._term_words("silence the whistle", kf) == []
    assert camp._term_words("vane taken down", kf) == []
    # An everyday word never carried the meaning: "roster lie" is "roster"
    assert camp._term_words("roster lie", kf) == ["roster"]
    # ...and the term said whole still gives the truth away
    told = "Silence the whistle, and the pack obeys no one."
    assert set(camp.sealed_truth_hits([dict(COST, spoiler_keywords=["silence the whistle", "pack obeys"])],
                                      told, ["Aldric Vane"])) == {"truth_cost"}


def test_the_roster_confession_still_gives_its_truth_away():
    """fleet3-tamsin T20: the true reveal the rule must keep."""
    roster = {"id": "truth_cost", "revealed": False,
              "statement": "Coldiron's roster of twenty is a lie; only eleven remain.",
              "spoiler_keywords": ["roster lie", "eleven"]}
    line = "That roster's a lie and you know it — eleven men, not twenty."
    assert set(camp.sealed_truth_hits([roster], line, ["Captain Brann Coldiron"])) == {"truth_cost"}


# --- 4. A reveal that costs ----------------------------------------------------

def test_a_beat_where_someone_deflects_carries_its_cost(spine):
    """fleet7-tamsin T16: "If pressed, he deflects…" — deflection and
    confession came in the same breath, no roll, no leverage."""
    same_make = _beat(spine, 2, 6, "The Same Make", directive=(
        "When Tamsin returns the cracked bone whistle to Coldiron, have him go very still. If pressed, he "
        "deflects, claims he's never seen its like, but Tamsin notices an identical whistle under his collar."))
    plain = _beat(spine, 2, 7, "Piper's Charts", directive="Have Piper show her charted howls.")
    with get_session() as session:
        player = session.get(Player, spine["player"])
        notes = camp._directive_notes(session, session.get(StoryBeat, same_make), player)
        plain_notes = camp._directive_notes(session, session.get(StoryBeat, plain), player)
    cost = next(n for n in notes if n.startswith("COST"))
    assert '"deflects"' in cost and "roll_check" in cost and "never a deflection and a confession" in cost
    assert not any(n.startswith("COST") for n in plain_notes)


def test_the_finale_a_dead_villain_forced_still_ends_the_story_anywhere(spine):
    """A place waits for the player — except the story's own end, forced
    by the villain dying with no successor."""
    _beat(spine, 3, 13, "The Whistle Breaks", beat_type="confrontation", cinematic="finale", status="pending",
          directive="Bring Tamsin face to face with Aldric Vane at Wolf's Hollow.",
          lead_target={"type": "location", "id": spine["hollow"]})
    _set(NPC, spine["vane"], status="dead")
    fired = camp.run_campaign_tick(spine["player"])["fired_beat"]      # the player is in the yard
    assert fired["title"] == "The Whistle Breaks"
    assert camp.run_campaign_tick(spine["player"])["fired_beat"]["title"] == "The Whistle Breaks"


# --- Review: the ending waits, walking off is no failure, a scene set on the
# way, the forge's deadline windows --------------------------------------------

def _finale(spine, **kw):
    return _beat(spine, 3, 13, "The Whistle Breaks", beat_type="confrontation", cinematic="finale",
                 directive="Bring Tamsin face to face with Aldric Vane at Wolf's Hollow.",
                 lead_target={"type": "location", "id": spine["hollow"]}, **kw)


def _campaign_status(spine):
    with get_session() as session:
        return camp.get_campaign_row(session, spine["player"]).status


def test_the_finale_waits_at_its_place_and_is_never_skipped_for_it(spine):
    """Skipping the last beat closes act 3 as "won" (`_check_act_transition`):
    a player who never went to the Hollow would have won without facing Vane."""
    _set(Campaign, spine["campaign"], current_act=3)
    side = _beat(spine, 3, 12, "The Cached Cargo", directive="Crates stacked under the ledge.",
                 lead_target={"type": "location", "id": spine["hollow"]},
                 armed_day=1, armed_hour=18, fallback_day=8, fallback_hour=18)
    finale = _finale(spine, armed_day=1, armed_hour=18, fallback_day=8, fallback_hour=18)
    _clock(8 + camp.STALLED_SKIP_DAYS + 2, 12)
    assert not camp.run_campaign_tick(spine["player"]).get("fired_beat")     # the player is in the yard
    assert _row(side).status == "skipped", "a side scene that waited too long still lets the act close"
    assert _row(finale).status == "armed"
    assert _campaign_status(spine) == "active"


def test_a_scene_the_player_walks_off_from_fires_again_on_every_return(spine):
    """Leaving is no failed telling: the once-only re-fire is for a beat the
    DM failed where it happens."""
    sing = _beat(spine, 1, 5, "SING", directive=SING, status="fired", fired_day=1, fired_hour=18,
                 delivery_attempts=1, armed_day=1, armed_hour=18)
    for _ in range(2):
        _set(Player, spine["player"], current_location_id=spine["yard"])
        assert not camp.run_campaign_tick(spine["player"]).get("fired_beat")
        assert _row(sing).status == "stalled"
        _set(Player, spine["player"], current_location_id=spine["hollow"])
        assert (camp.run_campaign_tick(spine["player"]).get("fired_beat") or {}).get("title") == "SING"


def test_a_scene_waiting_where_it_happens_is_not_skipped_at_the_door(spine):
    """The stale-stall skip runs before the re-fire: coming back days later
    must bring the scene, not lose it on arrival."""
    sing = _beat(spine, 1, 5, "SING", directive=SING, status="fired", fired_day=1, fired_hour=18,
                 delivery_attempts=1, armed_day=1, armed_hour=18)
    camp.run_campaign_tick(spine["player"])                   # the player is in the yard
    assert _row(sing).status == "stalled"
    _clock(1 + camp.STALLED_SKIP_DAYS + 2, 12)
    _set(Player, spine["player"], current_location_id=spine["hollow"])
    assert (camp.run_campaign_tick(spine["player"]).get("fired_beat") or {}).get("title") == "SING"


def test_the_ending_the_player_walked_away_from_is_never_skipped(spine):
    _set(Campaign, spine["campaign"], current_act=3)
    finale = _finale(spine, status="fired", fired_day=2, fired_hour=12, delivery_attempts=1,
                     armed_day=1, armed_hour=18)
    _clock(2, 14)
    camp.run_campaign_tick(spine["player"])          # the player ran back to the yard
    assert _row(finale).status == "stalled"
    _clock(2 + camp.STALLED_SKIP_DAYS + 2, 12)
    camp.run_campaign_tick(spine["player"])
    assert _row(finale).status == "stalled" and _campaign_status(spine) == "active"
    _set(Player, spine["player"], current_location_id=spine["hollow"])
    assert camp.run_campaign_tick(spine["player"])["fired_beat"]["title"] == "The Whistle Breaks"


def test_a_scene_set_before_the_arrival_is_not_held_there(spine):
    road = _beat(spine, 1, 3, "On the Road",
                 directive="Before Tamsin reaches Wolf's Hollow, have a scout step onto the road and warn her off.")
    never = _beat(spine, 1, 4, "Word From the Road",
                  directive="If she never gets to Wolf's Hollow, have Sorrel Tack hear the pack moving south.")
    with get_session() as session:
        assert camp._arrival_place_ids(session, session.get(StoryBeat, road)) == set()
        assert camp._arrival_place_ids(session, session.get(StoryBeat, never)) == set()

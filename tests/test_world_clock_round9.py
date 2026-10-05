"""Round 9 (fleet10): the world's own clock.

  * an event firing where the player stands is THIS reply's scene — on a
    fight's aftermath too — and names the job step it satisfies; a reply
    that denies it or tells it long over has not told it (fleet10-bram T18);
  * the waiting clock never contradicts its own fresh news, and pauses while
    the player works the story's own side job (fleet10-wenna T10/T16);
  * a disappearance names somebody the world holds, with a trace to find
    (fleet10-bram T10-T20);
  * a talk-streak summons carries news, and no push lands while the player
    works a step with its person (fleet10-tamsin T7-T10);
  * a watcher belongs to the story's other side (fleet10-bram T7-T10);
  * the brief says distances in words, and the audit catches a spoken
    distance far off the road's time (fleet10-wenna T1/T11, -tamsin T2/T10).
"""

import pytest

from src.models import Event, Location, LocationType, Player, Quest, QuestStatus, WorldClock, get_session
from src.services import world_tick
from src.services.world_tick import format_world_tick_context, mark_event_delivery, run_world_tick

HOWL_TEXT = ("At the stroke of eight, howling rises from the northern hills — layered, answering itself, closer "
             "than last night. The North Gate is barred behind it and the square empties fast.")
# fleet10-bram T18, word for word: the howl fired at his feet and this went by
BRAM_T18 = (
    "The cracked whistle gives up a thin, wrong note — half the sound choked off where the bone splits — but it "
    "carries far enough. For three long heartbeats nothing answers at all. Then, far off along the ridge, "
    "something moves between the birches: a shape too upright to be a wolf, pale ash on a dark mantle, gone the "
    "instant you try to fix your eyes on it, like the tree line swallowed it back down.\n\nNo howl follows. No "
    "whistle answers yours. Just the cold settling in around your split knuckles and the three dead wolves at "
    "your feet, and the sure, crawling certainty that whatever stood on that ridge heard your broken note and "
    "knew exactly what it wasn't.\n\nYou're bleeding, you're spent, and the gate's long since barred behind the "
    "evening howl — nothing north of here is answering you twice tonight."
)
STEP = "Stand out the next howl and mark where it rises"


@pytest.fixture()
def hollow(db):
    """The player at Wolf's Hollow at 20:00, the howl due there, Piper's job active."""
    with get_session() as session:
        square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True)
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True)
        session.add_all([square, hollow])
        session.flush()
        player = Player(name="Bram Holloway", currency=25, current_location_id=hollow.id)
        session.add(player)
        session.add(WorldClock(day=1, hour=20))
        session.add(Event(
            name="The Evening Howl", event_type="meso", player_visible=True, description=HOWL_TEXT,
            scheduled_day=1, scheduled_hour=20, locations_involved=[square.id, hollow.id],
            consequences=["North Gate barred for the night", "Townsfolk clear the square"],
        ))
        session.flush()
        quest = Quest(title="Counting the Howls", status=QuestStatus.ACTIVE, description="Count the howls.",
                      objectives=[{"text": STEP, "kind": "custom"},
                                  {"text": "Bring what you heard back to Piper", "kind": "custom"}])
        session.add(quest)
        session.flush()
        player.active_quests = [quest.id]
        session.commit()
        return {"player": player.id, "square": square.id, "hollow": hollow.id}


def _howl():
    with get_session() as session:
        e = session.query(Event).filter(Event.name == "The Evening Howl").one()
        return {"attempts": e.delivery_attempts, "narrated": e.narrated_to_player}


# --- 1. The howl at the player's feet ------------------------------------------

def test_an_event_at_the_players_feet_is_this_replys_scene_and_names_its_step(hollow):
    tick = run_world_tick(hollow["player"])
    text = format_world_tick_context(tick)
    assert "The Evening Howl — HAPPENING WHERE YOU STAND — this reply's scene" in text
    assert f"@step-done Counting the Howls | {STEP}" in text
    # ...and only the step it is: bringing word back is no vigil
    assert "Bring what you heard back" not in text


def test_a_fights_aftermath_still_carries_it_and_spends_no_attempt(hollow):
    """fleet10-bram T17-T18: the howl around a [COMBAT OUTCOME] reply."""
    tick = run_world_tick(hollow["player"], constrained=True, aftermath=True)
    text = format_world_tick_context(tick)
    assert "HAPPENING WHERE YOU STAND" in text and "the fight is over, and this is what comes next" in text
    assert _howl()["attempts"] == 0
    assert mark_event_delivery("Then it comes: howling rises from the northern hills all around you, "
                               "layered and close.") == ["The Evening Howl"]


def test_an_ambush_one_liner_still_holds_it_back(hollow):
    text = format_world_tick_context(run_world_tick(hollow["player"], constrained=True))
    assert "MUST-DELIVER" not in text


def test_a_reply_that_denies_it_or_tells_it_long_over_has_not_told_it(hollow):
    run_world_tick(hollow["player"])
    assert mark_event_delivery(BRAM_T18) == []
    assert _howl()["narrated"] is False
    # ...so it comes back, still happening (the clock has barely moved)
    text = format_world_tick_context(run_world_tick(hollow["player"]))
    assert "HAPPENING WHERE YOU STAND — this reply's scene — your last reply did not show it" in text


@pytest.mark.parametrize("prose", [
    "No howls tonight — only the ordinary dark, patient and listening right back at you.",
    "The gate's long since barred behind the evening howl, and the hills are quiet.",
    "The howl had already risen and died away an hour ago.",
])
def test_denials_and_long_past_tellings_are_not_the_howl_happening(hollow, prose):
    run_world_tick(hollow["player"])
    assert mark_event_delivery(prose) == []


@pytest.mark.parametrize("prose", [
    # fleet10-tamsin T17: a real telling beside "not a howl at all" about another sound
    "The minutes crawl, and then the first howl breaks — close, ragged, rising from somewhere east of the hut "
    "below. Somewhere far off a third sound threads through the dark — not a howl at all, but a low whistle.",
    "Howling rises from the northern hills, layered, answering itself. No second howl follows it.",
    # ...and a "no howl" about the player's own call, beside the howl itself
    "Howling rises from the northern hills, layered and close. You whistle back, but no howl answers yours.",
    "The howl rises from the northern hills, layered, answering itself — and then nothing, no howl after it.",
])
def test_a_real_telling_still_tells_it(hollow, prose):
    run_world_tick(hollow["player"])
    assert mark_event_delivery(prose) == ["The Evening Howl"]


def test_an_event_long_over_is_told_in_the_past(hollow):
    """Two hours on it is aftermath: the past perfect is how it is told."""
    with get_session() as session:
        session.query(WorldClock).first().hour = 23
        session.commit()
    text = format_world_tick_context(run_world_tick(hollow["player"]))
    assert "it is over now" in text
    # ...and over is no vigil kept: a howl that rose while they slept at its
    # source never ticks "Stand out the next howl"
    assert "@step-done" not in text
    assert mark_event_delivery("The howling had risen from the northern hills hours since, and the North Gate "
                               "has been barred for the night ever since.") == ["The Evening Howl"]


# --- The Emberfall seed, its campaign spine as the fleet played it --------------

@pytest.fixture()
def emberfall(tmp_path):
    from src.data.playtest_seed import create_playtest_world
    from src.models import NPC, reset_engine
    from src.models.campaign import Campaign, StoryBeat
    from src.services import complications, stakes
    from src.services.objectives import normalize_objectives

    reset_engine()
    create_playtest_world(str(tmp_path / "emberfall.db"))
    stakes.reset_state()
    complications.reset_state()
    with get_session() as session:
        places = {loc.name: loc.id for loc in session.query(Location).all()}
        people = {npc.name: npc.id for npc in session.query(NPC).all()}
        teeth = session.query(Quest).filter_by(title="Teeth on the North Road").one()
        steps = normalize_objectives(teeth.objectives)
        steps[0]["done"] = True                                  # the gate pass is theirs
        teeth.objectives, teeth.status = steps, QuestStatus.ACTIVE
        howls = session.query(Quest).filter_by(title="Counting the Howls").one()
        howls.status = QuestStatus.ACTIVE
        player = Player(name="Wenna Cole", current_location_id=places["Market Square"],
                        active_quests=[teeth.id, howls.id],
                        inventory=[{"id": "gate_pass", "name": "Gate Pass", "type": "quest_item"}])
        session.add(player)
        session.flush()
        vex = session.get(NPC, people["Vex Marrow"])
        campaign = Campaign(player_id=player.id, title="The Toll Road", status="active", current_act=1,
                            premise="Someone is using trained wolves to strangle the north road.",
                            villain_npc_id=people["Aldric Vane"], successor_npc_id=vex.id,
                            villain_faction_id=vex.faction_id)
        session.add(campaign)
        session.flush()
        session.add_all([
            StoryBeat(campaign_id=campaign.id, act=1, sequence=1, title="The Gate Pass", status="delivered",
                      consequence_met=[{"op": "activate_quest", "quest_id": teeth.id}]),
            # fleet10-wenna: the act's own beat pitched Piper's job
            StoryBeat(campaign_id=campaign.id, act=1, sequence=2, title="A Pattern Written in Ink",
                      status="delivered", directive="Piper Quillow is at Market Square near the notice board."),
            # fleet10-tamsin: the beat that waited on Sorrel at the yard
            StoryBeat(campaign_id=campaign.id, act=1, sequence=3, title="The Man at the Wall", status="armed",
                      trigger_type="location", trigger_data={"location_id": places["Stable Yard"]},
                      directive="Sorrel Tack keeps glancing toward the north wall."),
        ])
        session.commit()
        ids = {"player": player.id, "teeth": teeth.id, "howls": howls.id, **places, **people}
    yield ids
    reset_engine()


def _clock(day, hour, minute=0):
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.day, clock.hour, clock.minute = day, hour, minute
        session.commit()


def _put(pid, place_id):
    with get_session() as session:
        session.get(Player, pid).current_location_id = place_id
        session.commit()


def _force_kind(monkeypatch, kind):
    from src.services import stakes

    entry = next(c for c in stakes._WAIT_CONSEQUENCES + (stakes._WAIT_FALLBACK,) if c[0] == kind)
    monkeypatch.setattr(stakes, "_pick_consequence", lambda *a, **k: (entry[0], entry[2], entry[3]))


def _wait_out(pid, said="I order another drink and watch the room."):
    from src.services import stakes

    return [stakes.waited_line(pid, player_input=said) for _ in range(stakes.WAIT_TURNS + 1)]


# --- 2. The waiting clock never contradicts itself ---------------------------

def test_the_trail_is_never_cold_the_evening_something_fresh_happened_there(emberfall, monkeypatch):
    """fleet10-wenna T10 "Another Cart Taken" at 17:00, T16 "The Trail Grows
    Cold" at 19:00: "nothing's touched that ground in longer than three weeks"."""
    pid = emberfall["player"]
    _force_kind(monkeypatch, "cold")
    with get_session() as session:
        session.add(Event(name="Another Cart Taken", description="A cart taken past the Hollow.",
                          event_type="meso", occurred_day=1, occurred_hour=15, player_visible=True,
                          narrated_to_player=True, locations_involved=[emberfall["Wolf's Hollow"]]))
        session.commit()
    assert _wait_out(pid)[-1] is None
    assert not [e for e in _events() if e.name == "The Trail Grows Cold"]
    _clock(2, 8)                                            # past COLD_FRESH_HOURS
    line = next(line for line in _wait_out(pid) if line)
    assert "The freshest tracks at Wolf's Hollow" in line
    assert "older" not in line
    assert "contradicts none of it" in line


def _events():
    with get_session() as session:
        return session.query(Event).all()


def test_working_the_storys_own_side_job_pauses_the_clock(emberfall):
    """fleet10-wenna: two consequences while she worked Piper's job, which the
    act-1 beat itself pitched."""
    from src.services import stakes
    from src.services.objectives import normalize_objectives

    pid = emberfall["player"]
    stakes.waited_line(pid, player_input="I look around.")              # the count starts
    for i in range(stakes.WAIT_TURNS + 2):
        with get_session() as session:                                    # a step of it done each reply
            quest = session.get(Quest, emberfall["howls"])
            steps = normalize_objectives(quest.objectives)
            if i < len(steps) - 1:
                steps[i]["done"] = True
                quest.objectives = steps
                session.commit()
        if i < 2:
            assert stakes.waited_line(pid, player_input="I ask what he hears after dark.") is None
            assert stakes._waiting[pid]["turns"] == 0
    # A watch kept for the howl due within the hours is work too (fleet10-wenna T10)
    stakes.reset_state()
    with get_session() as session:
        quest = session.get(Quest, emberfall["howls"])
        quest.objectives = [dict(s, done=False) for s in normalize_objectives(quest.objectives)]
        session.commit()
    _clock(1, 17)
    stakes.waited_line(pid, player_input="I look around.")
    for _ in range(stakes.WAIT_TURNS + 2):
        assert stakes.waited_line(pid, player_input="Fair enough. I'll wait here then — I want to hear it "
                                                    "myself before the night's out.") is None
    assert stakes._waiting[pid]["turns"] == 0


def test_a_step_along_the_road_past_the_hop_starts_the_count_over(emberfall):
    """fleet10-wenna T12 walked the Stable Yard to the North Gatehouse in one move."""
    from src.services import stakes

    pid = emberfall["player"]
    _put(pid, emberfall["Stable Yard"])
    for _ in range(3):
        stakes.waited_line(pid, player_input="I order another drink.")
    assert stakes._waiting[pid]["turns"] == 2
    _put(pid, emberfall["North Gatehouse"])
    stakes.waited_line(pid, player_input="I look at the gate.")
    assert stakes._waiting[pid]["turns"] == 0


# --- 3. Someone missing is someone -------------------------------------------

def test_a_disappearance_registers_the_missing_person_and_a_trace_to_find(emberfall, monkeypatch):
    """fleet10-bram T10: "old Dennet's boy" was the DM's invention, and nothing
    of him could be found at the Hollow (T12-T20)."""
    from src.models import NPC
    from src.services.scene_objects import normalize_objects

    pid = emberfall["player"]
    _force_kind(monkeypatch, "missing")
    with get_session() as session:
        names_before = {n.name for n in session.query(NPC).all()}
    line = _wait_out(pid)[-1]
    with get_session() as session:
        missing = session.query(NPC).filter(NPC.status == "missing").one()
        assert missing.name not in names_before
        assert missing.current_location_id is None
        event = session.query(Event).filter(Event.name == "Someone Else Missing").one()
        assert event.description.startswith(f"{missing.name}, a {missing.profession}, went missing")
        # The Hollow's three authored Things leave no room: the road's near end
        gate = session.get(Location, emberfall["North Gatehouse"])
        trace = [o for o in normalize_objects(gate.objects) if o["id"].startswith("trace_")]
        assert len(trace) == 1 and trace[0]["kind"] == "search" and trace[0]["payload"]["item"]
        # The trace never names them: the player has not heard the name
        assert missing.name not in trace[0]["payload"]["text"]
    assert f"The one missing is {missing.name} ({missing.profession})" in line
    assert f"at North Gatehouse, the engine left a trace to find: {trace[0]['label']}" in line
    assert "invent no other missing person" in line
    # Searched there, it is found, carried, and journaled
    from src.services.memory_files import interaction_journal_entries
    from src.services.scene_objects import resolve_interaction

    _put(pid, emberfall["North Gatehouse"])
    found = resolve_interaction(pid, emberfall["North Gatehouse"], trace[0]["id"])
    assert found["success"] and found["result"]["items_gained"]
    entries = interaction_journal_entries(found, trace[0]["payload"])
    assert entries and entries[0].startswith(f"{trace[0]['label']}: ")


def test_a_spent_thing_at_the_place_gives_way_to_the_trace(emberfall, monkeypatch):
    from src.services.scene_objects import normalize_objects

    pid = emberfall["player"]
    _force_kind(monkeypatch, "missing")
    with get_session() as session:
        hollow = session.get(Location, emberfall["Wolf's Hollow"])
        objects = normalize_objects(hollow.objects)
        objects[-1]["consumed"] = True
        spent = objects[-1]["id"]
        hollow.objects = objects
        session.commit()
    _wait_out(pid)
    with get_session() as session:
        ids = [o["id"] for o in normalize_objects(session.get(Location, emberfall["Wolf's Hollow"]).objects)]
    assert spent not in ids and any(i.startswith("trace_") for i in ids)


# --- 4. A summons carries news; no push while the player works with its person --

def test_no_complication_while_an_armed_beats_person_is_in_the_scene(emberfall):
    """fleet10-tamsin T7: a runner pulled her off Sorrel while "The Man at the
    Wall" waited on him at the yard."""
    from src.services import complications

    pid = emberfall["player"]
    _put(pid, emberfall["Stable Yard"])
    for _ in range(12):
        complications.note_turn(pid, player_input='"What do you hear after dark?"')
    assert complications.directive_line(pid, player_input='"And the north wall?"') is None
    assert complications.quiet_turns(pid) >= 12                       # held, not spent
    # ...nor while the step they work is with someone here: Piper's next step
    with get_session() as session:
        from src.services.objectives import normalize_objectives

        quest = session.get(Quest, emberfall["howls"])
        steps = normalize_objectives(quest.objectives)
        steps[0]["done"] = steps[1]["done"] = True
        quest.objectives = steps
        session.commit()
    _put(pid, emberfall["Market Square"])
    # ...only while they talk with her: standing in the same square all day
    # is no conversation (she has not spoken in the last two replies)
    with get_session() as session:
        assert complications._working_with(session, session.get(Player, pid)) is None
    complications.note_turn(pid, player_input='"Piper — the howls?"',
                            speaker_ids=[emberfall["Piper Quillow"]])
    with get_session() as session:
        assert "Bring what you heard back to Piper" in complications._working_with(
            session, session.get(Player, pid))
        # ...but a companion is "present" everywhere they go: Piper in the
        # party would hold every push for the rest of the session
        session.get(Player, pid).party_members = [emberfall["Piper Quillow"]]
        session.commit()
        assert complications._working_with(session, session.get(Player, pid)) is None


def test_a_summons_with_no_news_is_no_summons(emberfall, monkeypatch):
    from src.services import complications

    pid = emberfall["player"]
    _put(pid, emberfall["The Drowned Lantern"])
    _clock(2, 12)                                  # the howl gone by, the cart due in the morning long past
    with get_session() as session:
        for e in session.query(Event).all():
            e.occurred_day, e.occurred_hour, e.narrated_to_player = 1, 20, True
        session.commit()
        assert complications._summons_news(session, session.get(Player, pid)) is None
    menu = {k: (k, what, bite) for k, _, what, bite in complications._TOWN}
    monkeypatch.setattr(complications, "_pick", lambda *a, **k: menu["summons"])
    for _ in range(12):
        complications.note_turn(pid, player_input="I drink.")
    assert complications.directive_line(pid) is None
    assert complications.open_complication(pid) is None and complications.quiet_turns(pid) == 12


def test_a_summons_carries_whats_coming(emberfall, monkeypatch):
    from src.services import complications

    pid = emberfall["player"]
    _put(pid, emberfall["The Drowned Lantern"])
    _clock(1, 16)
    menu = {k: (k, what, bite) for k, _, what, bite in complications._TOWN}
    monkeypatch.setattr(complications, "_pick", lambda *a, **k: menu["summons"])
    for _ in range(12):
        complications.note_turn(pid, player_input="I drink.")
    line = complications.directive_line(pid)
    assert "The Evening Howl, due at 20:00" in line
    assert "never retell what the player was already told" in line


def test_the_menu_offers_no_summons_without_news():
    from src.services import complications

    kinds = set()
    for i in range(30):
        complications._fired["p"] = i
        kinds.add(complications._pick("p", "town", has_goods=True, has_quest=True, has_news=False)[0])
    complications.reset_state()
    assert "summons" not in kinds and "watcher" in kinds


# --- 5. A watcher of the story's other side ----------------------------------

def test_a_watcher_is_a_lookout_of_the_villains_side_with_a_name_of_this_world(emberfall, monkeypatch):
    """fleet10-bram T7 (and fleet9-bram T19): the same stock laundress, Ilse
    Marrick, who connected to nothing."""
    from src.models import NPC
    from src.services import complications

    pid = emberfall["player"]
    _put(pid, emberfall["The Drowned Lantern"])
    menu = {k: (k, what, bite) for k, _, what, bite in complications._TOWN}
    monkeypatch.setattr(complications, "_pick", lambda *a, **k: menu["watcher"])
    with get_session() as session:
        names_before = {w.lower() for n in session.query(NPC).all() for w in (n.name or "").split()}
    for _ in range(12):
        complications.note_turn(pid, player_input="I drink.")
    line = complications.directive_line(pid)
    opened = complications.open_complication(pid)
    with get_session() as session:
        watcher = session.get(NPC, opened["entity_id"])
        vex = session.get(NPC, emberfall["Vex Marrow"])
        assert watcher.faction_id == vex.faction_id
        assert watcher.current_location_id == emberfall["The Drowned Lantern"]
        assert "reports to Vex Marrow" in watcher.description_personality
        assert "A tell the player can learn" in watcher.description_personality
        assert watcher.name.lower() not in names_before
        assert watcher.name not in {s[0] for s in complications._TOWN_STUBS}
    assert "the engine made them REAL" in line and "report to Vex Marrow" in line
    assert "One tell the player can learn" in line
    # Never dressed as the villain's side: no cloak, nothing the story keeps sealed
    assert "cloak" not in line.split("One tell")[1].split(".")[0].lower()


def test_world_names_follow_the_bible_and_never_repeat_a_name_in_use(emberfall):
    from src.models import NPC
    from src.services.complications import _COMMON_WORDS, world_name

    with get_session() as session:
        used = {w.lower() for n in session.query(NPC).all() for w in (n.name or "").split()}
        names = {world_name(session, f"seed-{i}") for i in range(60)}
    assert len(names) > 20
    assert not {n.lower() for n in names} & used
    assert not {n.lower() for n in names} & _COMMON_WORDS


# --- 6. Distances in words, and said right -------------------------------------

def test_the_brief_gives_each_exit_in_words_never_minutes(emberfall):
    from src.services.scene_brief import _exits, travel_words

    pid = emberfall["player"]
    _put(pid, emberfall["North Gatehouse"])
    with get_session() as session:
        lines = _exits(session, session.get(Player, pid), session.get(Location, emberfall["North Gatehouse"]))
    hollow = next(line for line in lines if "Wolf's Hollow" in line)
    assert "(walk, under an hour)" in hollow and " min" not in " ".join(lines)
    assert [travel_words(h) for h in (0.05, 0.75, 2, 6, 12)] == [
        "a few steps", "under an hour", "a couple of hours", "half a day", "most of a day"]


@pytest.mark.parametrize("said", [
    'Wolf\'s Hollow is a half-day up the north road, past where the trees close in.',       # fleet10-tamsin T2
    "Wolf's Hollow is north past the gate, half a day's walk — it's on the maps, just not the good ones.",  # -wenna T1
    "Wolf's Hollow is straight up the north road past the Gatehouse — maybe two hours' walk.",            # -wenna T11
])
def test_a_spoken_distance_far_off_the_road_is_flagged(emberfall, said):
    from src.web.turn_audit import distance_warning

    warning = distance_warning(["You wait.", said], [emberfall["Market Square"]])
    assert warning and warning["check"] == "distance_misstated"
    assert "under an hour" in warning["detail"]


@pytest.mark.parametrize("said", [
    "Hollow's three-quarters of an hour north once you're past the wall.",            # fleet3-tamsin T2
    "Wolf's Hollow waits ahead, forty-five minutes of road between you and it.",    # fleet4-tamsin T4
    "The gatehouse is three minutes north.",                                          # fleet7-bram T3
    "The Drowned Lantern sits six minutes off, warm windows glowing.",               # fleet4-bram T13
    "The howl is still an hour off, and the Hollow quiet.",                           # a time, no distance
    "You've been in town less than an hour, and the square feels thin.",
    "The gate closes in two hours; the Hollow can wait.",
])
def test_right_distances_and_times_are_not_flagged(emberfall, said):
    from src.web.turn_audit import distance_warning

    assert distance_warning([said], [emberfall["Market Square"]]) is None

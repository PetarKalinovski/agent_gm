"""World tick mechanics: time, event delivery, tension escalation."""

from src.models import DMState, Event, WorldClock, get_session
from src.services.world_tick import (
    enforce_minimum_time_cost,
    format_world_tick_context,
    mark_event_delivery,
    run_world_tick,
    snapshot_clock,
)


def _setup_clock(day=1, hour=8):
    with get_session() as session:
        clock = WorldClock(day=day, hour=hour)
        session.add(clock)
        session.commit()


def test_fractional_time_accumulates(db):
    _setup_clock()
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.advance(0.25)  # 15 min
        clock.advance(0.25)
        clock.advance(0.5)
        session.commit()
        assert clock.hour == 9, "three fractional advances must add up to a full hour"


def test_minimum_time_cost_enforced_when_clock_unchanged(db):
    _setup_clock()
    start = snapshot_clock()
    result = enforce_minimum_time_cost(start, minimum_hours=1.0)
    assert result["enforced"] is True
    assert snapshot_clock() != start


def test_minimum_time_cost_skipped_when_dm_advanced(db):
    _setup_clock()
    start = snapshot_clock()
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.advance(2)
        session.commit()
    result = enforce_minimum_time_cost(start)
    assert result["enforced"] is False


def test_scheduled_event_fires_and_redelivers_until_giveup(db):
    _setup_clock(day=2, hour=10)
    with get_session() as session:
        session.add(Event(
            name="Blockade",
            description="The docks are blockaded",
            event_type="macro",
            scheduled_day=2,
            scheduled_hour=9,
        ))
        session.commit()

    # Fires on the first tick
    tick = run_world_tick()
    assert any(e["name"] == "Blockade" for e in tick["fired_events"])

    # Not narrated → re-surfaced on following ticks
    tick2 = run_world_tick()
    assert any(e["name"] == "Blockade" for e in tick2["undelivered_events"])

    # After the attempt budget it stops nagging and is marked delivered
    run_world_tick()
    run_world_tick()
    tick5 = run_world_tick()
    assert not any(e["name"] == "Blockade" for e in tick5["undelivered_events"])
    with get_session() as session:
        event = session.query(Event).filter(Event.name == "Blockade").first()
        assert event.narrated_to_player is True


def test_tension_escalates_after_quiet_turns(db):
    _setup_clock()
    for _ in range(7):
        tick = run_world_tick()
        assert not tick["auto_escalated"]

    tick = run_world_tick()  # 8th quiet turn
    assert tick["auto_escalated"] is True
    with get_session() as session:
        state = session.query(DMState).first()
        assert state.tension == "rising"


# ----------------------------------------------------------------------
# Delivery: a told event is done; a re-surfaced one is history
# ----------------------------------------------------------------------

def _howl():
    with get_session() as session:
        session.add(Event(
            name="The Evening Howl", event_type="meso", player_visible=True,
            description="Howling rises from the northern hills; the North Gate is barred early.",
            scheduled_day=1, scheduled_hour=20,
        ))
        session.commit()


def _narrated():
    with get_session() as session:
        return session.query(Event).filter(Event.name == "The Evening Howl").one().narrated_to_player


def test_a_turn_that_mentions_the_event_tells_it(db):
    """Playtest 2026-10-04: the Evening Howl was narrated, yet re-surfaced
    and staged as "starting" on four turns — nothing marked it told."""
    _setup_clock(day=1, hour=20)
    _howl()
    assert run_world_tick()["fired_events"]
    assert mark_event_delivery("You buy bread and listen to the fire.") == []
    assert _narrated() is False
    assert mark_event_delivery("Somewhere north, the howling rises from the hills.") == ["The Evening Howl"]
    assert _narrated() is True
    assert run_world_tick()["undelivered_events"] == []


def test_an_unfired_event_is_never_marked_told(db):
    _setup_clock(day=1, hour=8)
    _howl()                                          # not due until 20:00
    assert mark_event_delivery("The howl of the wind.") == []
    run_world_tick()
    assert _narrated() is False


def test_a_resurfaced_event_is_framed_as_already_over(db):
    _setup_clock(day=1, hour=20)
    _howl()
    run_world_tick()                                 # fires
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.day, clock.hour = 2, 7
        session.commit()
    tick = run_world_tick()                          # re-surfaces, a day later
    assert tick["undelivered_events"][0]["hour"] == 20
    text = format_world_tick_context(tick)
    assert ("The Evening Howl (happened at day 1, 20:00 — it is already over; refer to it "
            "in the past tense, do not stage it again)") in text
    # The same day reads as a time alone
    tick["day"] = 1
    assert "(happened at 20:00 — it is already over" in format_world_tick_context(tick)


# ----------------------------------------------------------------------
# Round 3 (fleet4): told by the prose, pushed when it is here, never lost
# ----------------------------------------------------------------------

from src.models import Location, LocationType, Player  # noqa: E402
from src.services import world_tick  # noqa: E402

HOWL_TEXT = ("At the stroke of eight, howling rises from the northern hills — layered, answering "
             "itself. The North Gate is barred early and the square empties fast.")


def _places_and_howl(player_at: str = "square", hour: int = 20):
    """A town square and the Hollow; the howl involves both; the player stands at `player_at`."""
    with get_session() as session:
        square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True)
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True)
        inn = Location(name="The Drowned Lantern", type=LocationType.POI, depth=0, discovered=True)
        elsewhere = Location(name="Mill", type=LocationType.POI, depth=0, discovered=True)
        session.add_all([square, hollow, inn, elsewhere])
        session.flush()
        inn.parent_id = square.id
        places = {"square": square.id, "hollow": hollow.id, "inn": inn.id, "mill": elsewhere.id}
        player = Player(name="Wenna Cole", currency=10, current_location_id=places[player_at])
        session.add(player)
        session.add(WorldClock(day=1, hour=hour))
        session.add(Event(
            name="The Evening Howl", event_type="meso", player_visible=True, description=HOWL_TEXT,
            scheduled_day=1, scheduled_hour=20, locations_involved=[places["square"], places["hollow"]],
            consequences=["North Gate barred for the night", "Townsfolk clear the square"],
        ))
        session.commit()
        return player.id, places


def _howl_row():
    with get_session() as session:
        e = session.query(Event).filter(Event.name == "The Evening Howl").one()
        return {"attempts": e.delivery_attempts, "narrated": e.narrated_to_player}


def test_one_loose_word_in_someone_elses_line_does_not_tell_it(db):
    """fleet4-tamsin T12: Piper's "The howls, they're not —" (about her
    research) closed The Evening Howl though nobody saw or heard it."""
    pid, _ = _places_and_howl(player_at="mill")       # elsewhere: speech may tell it too
    run_world_tick(pid)
    assert mark_event_delivery("Piper waylays you by the well with her basket.",
                               spoken_text="The howls, they're not — they're drilled wrong.") == []
    assert _narrated() is False
    # One of its consequences said outright tells it
    assert mark_event_delivery("", spoken_text="They've barred the gate already, love.") == ["The Evening Howl"]


def test_an_event_where_the_player_stands_is_must_deliver_and_told_by_prose_only(db):
    """fleet4-wenna: the howl fired at 20:00 while she stood at its source; the
    villain's future-tense "The gate closes at the howl" is not it happening."""
    pid, _ = _places_and_howl(player_at="hollow")
    tick = run_world_tick(pid)
    assert tick["fired_events"][0]["must_deliver"] is True
    text = format_world_tick_context(tick)
    assert "MUST-DELIVER THIS REPLY" in text and "HAPPENING WHERE YOU STAND — this reply's scene" in text
    assert "EVENTS THAT JUST FIRED" not in text
    assert mark_event_delivery("He watches the treeline.", spoken_text="The gate closes at the howl.") == []
    assert mark_event_delivery("Howling rises from the hills all around you.") == ["The Evening Howl"]


def test_the_inn_on_the_square_hears_the_square(db):
    pid, _ = _places_and_howl(player_at="inn")
    assert run_world_tick(pid)["fired_events"][0]["must_deliver"] is True


def test_an_event_elsewhere_stays_news_unless_it_was_foreshadowed(db):
    pid, _ = _places_and_howl(player_at="mill", hour=18)
    assert run_world_tick(pid)["fired_events"] == []
    # Brann sold the deadline before it fired (fleet4-wenna T1/T4)
    mark_event_delivery("", spoken_text="Gate's barred after the howl. Be back before it.")
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.hour = 20
        session.commit()
    tick = run_world_tick(pid)
    assert tick["fired_events"][0]["must_deliver"] is True
    assert "they were told to expect it" in format_world_tick_context(tick)


def test_an_engine_turn_spends_no_delivery_attempt(db):
    """fleet4-wenna: the howl's three attempts went to a parley, an AMBUSH
    one-liner and a [COMBAT OUTCOME] aftermath."""
    pid, _ = _places_and_howl(player_at="hollow")
    tick = run_world_tick(pid, constrained=True)                 # fires inside an ambush reply
    assert _howl_row()["attempts"] == 0
    text = format_world_tick_context(tick)
    assert "MUST-DELIVER" not in text and "they keep — they come back next turn" in text
    run_world_tick(pid, constrained=True)                        # the combat aftermath
    assert _howl_row()["attempts"] == 0
    for expected in (1, 2, 3):
        if expected == 3:
            with get_session() as session:                       # two hours on: over now
                session.query(WorldClock).first().hour = 22
                session.commit()
        tick = run_world_tick(pid)
        assert tick["undelivered_events"][0]["must_deliver"] is True
        assert _howl_row()["attempts"] == expected
        if expected == 1:
            # Still within its hour where they stand: it comes now (fleet10-bram T18)
            assert "your last reply did not show it, so it comes now" in format_world_tick_context(tick)
    assert ("The Evening Howl (happened at 20:00, around them — it is over now: show what it left "
            "behind, in the past tense)") in format_world_tick_context(tick)


def test_an_event_out_of_attempts_is_reported_offscreen_and_chronicled(db, tmp_path, monkeypatch):
    from src.services import memory_files

    monkeypatch.setattr(memory_files, "memory_root", lambda: tmp_path / "memory")
    pid, _ = _places_and_howl(player_at="mill")
    for _ in range(3):
        run_world_tick(pid)                                      # fired, then shown twice more
    tick = run_world_tick(pid)
    assert [e["name"] for e in tick["offscreen_events"]] == ["The Evening Howl"]
    assert _howl_row()["narrated"] is True
    text = format_world_tick_context(tick)
    assert "HAPPENED OFFSCREEN" in text and "The Evening Howl (happened at 20:00)" in text
    assert "The Evening Howl happened offscreen" in memory_files.read_chronicle(pid)
    assert run_world_tick(pid)["offscreen_events"] == []         # said once


def test_todays_public_schedule_is_in_the_world_block(db):
    """fleet4-wenna T3: Piper's 'not tonight' for the howl scheduled that night."""
    pid, _ = _places_and_howl(player_at="square", hour=15)
    with get_session() as session:
        session.add(Event(name="Secret Muster", event_type="meso", player_visible=False,
                          description="The pack gathers.", scheduled_day=1, scheduled_hour=22))
        session.add(Event(name="Cart Arrives", event_type="meso", player_visible=True,
                          description="The cart limps in.", scheduled_day=2, scheduled_hour=9))
        session.commit()
    tick = run_world_tick(pid)
    assert [e["name"] for e in tick["todays_events"]] == ["The Evening Howl"]
    text = format_world_tick_context(tick)
    assert "LATER TODAY" in text and "  - 20:00 The Evening Howl: At the stroke of eight" in text
    assert "Secret Muster" not in text and "Cart Arrives" not in text


def test_half_a_consequence_or_a_denial_does_not_tell_it(db):
    """A false 'told' drops the event for good (a miss only re-shows it):
    'Townsfolk drift across the square' is half of 'Townsfolk clear the
    square', and 'no sign yet of the howl' says it has NOT happened."""
    pid, _ = _places_and_howl(player_at="hollow")
    run_world_tick(pid)
    for prose in ("Townsfolk drift across the square toward the inn.",
                  "The square is quiet, no sign yet of the howl Brann promised.",
                  "Not one single howl, not yet, though the gate guard keeps glancing north."):
        assert mark_event_delivery(prose) == [], prose
    assert _narrated() is False
    assert mark_event_delivery("Townsfolk clear out of the square fast, shutters slamming.") == ["The Evening Howl"]


def test_talk_about_the_howls_routine_does_not_tell_it(db):
    """fleet4-tamsin T13 / fleet4-wenna T8: NPCs describing how the howl goes
    are not it happening — speech needs one more detail than prose."""
    pid, _ = _places_and_howl(player_at="mill")       # elsewhere: speech may tell it
    run_world_tick(pid)
    for line in ("The gate closes at the howl, girl.",
                 "See, a real wolf howls when it wants to — spread out, answering each other, messy."):
        assert mark_event_delivery("You listen.", spoken_text=line) == [], line
    assert mark_event_delivery("", spoken_text="Howling's rising off the northern hills — hear it?") == [
        "The Evening Howl"]


def test_an_event_slept_through_is_not_happening_at_dawn(db):
    """A bed before 20:00 moves the clock to 07:00: the howl fires on the
    morning tick, and the reply must not stage an evening howl at dawn."""
    pid, _ = _places_and_howl(player_at="inn", hour=18)
    assert run_world_tick(pid)["fired_events"] == []
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.day, clock.hour = 2, 7
        session.commit()
    tick = run_world_tick(pid)
    assert tick["fired_events"][0]["must_deliver"] is True
    text = format_world_tick_context(tick)
    assert "happening around them right now" not in text
    assert "The Evening Howl (happened at day 1, 20:00, around them — it is over now" in text


# ----------------------------------------------------------------------
# Round 4 (fleet5): an event the reply's own clock crossed, told once
# ----------------------------------------------------------------------

def _clock_to(hour: int, minute: int = 0):
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.hour, clock.minute = hour, minute
        session.commit()


def test_an_event_the_reply_crossed_and_told_is_delivered_that_turn(db):
    """fleet5-tamsin T7: the reply's `@time 45m` crossed the 20:00 howl and its
    prose told it; T8 got it as news and barred the gate a second time."""
    pid, _ = _places_and_howl(player_at="square", hour=19)
    _clock_to(19, 15)
    assert run_world_tick(pid)["fired_events"] == []           # the turn starts before eight
    _clock_to(20, 0)                                            # the reply's @time carried it past
    assert mark_event_delivery("Then it comes: howling rises from the northern hills, "
                               "layered, answering itself.") == ["The Evening Howl"]
    assert _howl_row() == {"attempts": 1, "narrated": True}
    tick = run_world_tick(pid)
    assert tick["fired_events"] == [] and tick["undelivered_events"] == []
    # Never staged again — only said once as over, with what it closed
    # (fleet7-tamsin T9 wrote "the howl hasn't come yet" after T7 told it)
    text = format_world_tick_context(tick)
    assert "MUST-DELIVER" not in text and "EVENTS THAT JUST FIRED" not in text
    assert "The Evening Howl (due at 20:00, told as your clock passed it)" in text
    assert "It means: North Gate barred for the night" in text
    assert "The Evening Howl" not in format_world_tick_context(run_world_tick(pid))


def test_a_crossed_event_only_talked_about_still_fires_next_turn(db):
    """Speech about it ("the gate shuts at the howl") is not it happening."""
    pid, _ = _places_and_howl(player_at="square", hour=19)
    _clock_to(20, 0)
    assert mark_event_delivery("You wait by the well.",
                               spoken_text="Howling's rising off the northern hills — hear it?") == []
    assert _howl_row()["narrated"] is False
    assert [e["name"] for e in run_world_tick(pid)["fired_events"]] == ["The Evening Howl"]


def test_an_event_told_within_the_hour_before_it_happens_then(db):
    """fleet5-wenna T8 told the howl "right on the stroke" at 19:45 and it
    fired again at eight; fleet6-bram T7 (19:30) and fleet6-wenna T8 (19:50)
    the same — two howls in a story whose hook is that it is never early.
    Told within the hour before its hour, it happened then: the clock moves
    on to eight and the tick never fires it a second time."""
    pid, _ = _places_and_howl(player_at="square", hour=19)
    _clock_to(19, 45)
    assert mark_event_delivery("Howling rises from the northern hills, right on the stroke.") == [
        "The Evening Howl"]
    assert _howl_row() == {"attempts": 1, "narrated": True}
    with get_session() as session:
        clock = session.query(WorldClock).first()
        assert (clock.hour, clock.minute) == (20, 0)
    tick = run_world_tick(pid)
    assert tick["fired_events"] == [] and tick["undelivered_events"] == []


def test_an_event_told_hours_early_or_as_a_memory_is_not_delivered(db):
    """Further out than an hour it is someone's story, not the event; and a
    howl "last night" or "every evening" is a habit told, not this one."""
    pid, _ = _places_and_howl(player_at="square", hour=19)
    _clock_to(18, 30)
    assert mark_event_delivery("Howling rises from the northern hills, right on the stroke.") == []
    _clock_to(19, 30)
    assert mark_event_delivery("Last night howling rose from the northern hills, layered, "
                               "answering itself.") == []
    assert _howl_row()["narrated"] is False
    with get_session() as session:
        clock = session.query(WorldClock).first()
        assert (clock.hour, clock.minute) == (19, 30)


# ----------------------------------------------------------------------
# Round 6 (fleet7): told early only when told HAPPENING — and said so
# ----------------------------------------------------------------------

FLEET7_BRAM_T5 = (
    "The gatehouse is squat stone and iron, lantern-lit, the gate itself a black timber wall banded in "
    "rust. A Watch man at the desk inside barely looks up before sliding a folded paper across the counter "
    "— the Captain's word already passed ahead of you. The pass is plain, stamped with a lantern seal, "
    "good for one crossing before the gate bars for the night. Beyond the timbers you can hear nothing yet "
    "but wind combing through bare branches, the hills still quiet, the howl still an hour off.\n\n"
    "You fold the pass into your coat, the paper still warm from someone's hand. The stable yard is three "
    "minutes back the way you came, and the stable boy will still be awake — or you could push on now "
    "while the gate stands open and the hills are still quiet."
)


def _clock_now():
    with get_session() as session:
        clock = session.query(WorldClock).first()
        return clock.day, clock.hour, clock.minute or 0


def _howl_fired():
    with get_session() as session:
        return session.query(Event).filter(Event.name == "The Evening Howl").one().occurred_day is not None


def test_a_countdown_to_the_howl_is_not_the_howl(db):
    """fleet7-bram T5 at 19:00: "the howl still an hour off" and the DM's
    pass "good for one crossing before the gate bars for the night" jumped
    the clock to 20:00, barred the gate unseen and spent the act's set piece
    (its beat never delivered, "Stand out the next howl" impossible)."""
    pid, _ = _places_and_howl(player_at="square", hour=19)
    assert mark_event_delivery(FLEET7_BRAM_T5) == []
    assert _clock_now() == (1, 19, 0)
    assert not _howl_fired() and _howl_row()["narrated"] is False
    # It still happens on its hour, around them
    _clock_to(20, 0)
    tick = run_world_tick(pid)
    assert [e["name"] for e in tick["fired_events"]] == ["The Evening Howl"]
    assert tick["fired_events"][0]["must_deliver"] is True


def test_a_countdown_the_replys_own_clock_crossed_is_not_the_howl_either(db):
    """The same pass line in a reply whose @time carried the clock past eight."""
    pid, _ = _places_and_howl(player_at="square", hour=19)
    _clock_to(20, 5)
    assert mark_event_delivery("The pass is plain, stamped with a lantern seal, good for one crossing "
                               "before the gate bars for the night.") == []
    assert [e["name"] for e in run_world_tick(pid)["fired_events"]] == ["The Evening Howl"]


def test_a_howl_told_happening_early_still_fires_then_and_the_next_context_says_so(db):
    """fleet6-bram T7 (19:30) told the howl with "not ragged, not wild" in the
    sentence and T9 told it again — the denial is that clause's alone. The
    clock moves on to eight, and the next context says what that closed."""
    pid, _ = _places_and_howl(player_at="square", hour=19)
    _clock_to(19, 30)
    assert mark_event_delivery(
        "He points, slow, toward the dark ridge past the rooftops, and you follow his arm just as the "
        "first howl rises — not ragged, not wild, but long and level, like a note held on purpose."
    ) == ["The Evening Howl"]
    assert _clock_now() == (1, 20, 0)
    tick = run_world_tick(pid)
    assert tick["fired_events"] == [] and tick["undelivered_events"] == []
    text = format_world_tick_context(tick)
    assert "HAPPENED — your last reply told it" in text
    assert "The Evening Howl (told just before its hour — the clock moved on to 20:00)" in text
    assert "It means: North Gate barred for the night, Townsfolk clear the square — true now" in text
    assert "HAPPENED —" not in format_world_tick_context(run_world_tick(pid))      # said once


def test_told_happening_needs_the_event_happening_not_its_words():
    """Replayed over every fleet transcript: the countdowns, deadlines and
    states below told the howl to the round-6 detector; the happenings
    (fleet5-wenna T8, fleet6-wenna T8/T10, fleet7-tamsin T7) must still."""
    from src.services.world_tick import told_happening

    howl = {"name": "The Evening Howl", "description": HOWL_TEXT,
            "consequences": ["North Gate barred for the night", "Townsfolk clear the square"]}
    for line in (
        "Beyond the timbers you can hear nothing yet but wind combing through bare branches, the hills "
        "still quiet, the howl still an hour off.",
        "The pass is plain, stamped with a lantern seal, good for one crossing before the gate bars for the night.",
        "The stable yard is a short walk off the square, and the evening howl is still a ways off yet.",
        "You've got maybe half an hour before the gate bars for the night.",
        "The howl comes in an hour, and the gate bars with it.",
        "Tonight's howl rises over the northern hills at eight, as it has every night.",
        "He nods toward the barred gate, dark now against the hill beyond it.",
        "No howl rises from the northern hills.",
        "Piper studies her chart of the howls from the northern hills.",
        "If the howl rises from the northern hills, the gate bars.",
    ):
        assert not told_happening(howl, line), line
    for line in (
        "Then, right on the stroke the sky promised, the first howl climbs out of the northern hills.",
        "Then the howl rises, low and long, and answers itself twice just as Piper's chart promised.",
        "At the North Gate, you can hear the bar drop into its iron cradle, and the square empties around you.",
        "Then it comes: one long howl rolling down off the hills, low and sustained.",
        "Far off, eight slow bells roll up from Market Square — howling rises from the northern slopes.",
    ):
        assert told_happening(howl, line), line


def test_a_howl_that_might_come_is_not_the_howl():
    """A might-be is foreshadowing, the kind of line a reply writes in the
    hour before the howl (fleet7-bram T5's hour): no modal, expectation,
    "any minute", listening for it or question tells it happening."""
    from src.services.world_tick import told_happening

    howl = {"name": "The Evening Howl", "description": HOWL_TEXT,
            "consequences": ["North Gate barred for the night", "Townsfolk clear the square"]}
    for line in (
        "The howl could rise any minute now over the northern hills, layered and answering itself.",
        "The howl should be rising over the northern hills soon enough, layered.",
        "The howl might rise over the northern hills at any moment, layered.",
        "The howl is expected to rise over the northern hills, layered and answering itself.",
        "Any minute now the howl rises over the northern hills, layered.",
        "The howl comes within the hour, rising over the northern hills, layered.",
        "You listen for the howl rising over the northern hills, layered and answering.",
        "Is that the howl rising over the northern hills, layered and answering itself?",
    ):
        assert not told_happening(howl, line), line
    assert told_happening(howl, "You can hear the howl rising over the northern hills, layered and answering itself.")


def test_the_seed_howl_agrees_with_its_hour(tmp_path):
    """fleet5-wenna T10: every NPC says the howl is at eight, the seed's text
    said the gate is barred "early" — and the DM told it shutting "well
    ahead of its usual hour" at 20:15."""
    from src.data.playtest_seed import create_playtest_world
    from src.models import reset_engine

    reset_engine()
    create_playtest_world(str(tmp_path / "emberfall.db"))
    with get_session() as session:
        howl = session.query(Event).filter(Event.name == "The Evening Howl").one()
        assert howl.scheduled_hour == 20 and "eight" in howl.description
        assert "early" not in howl.description.lower()
    reset_engine()

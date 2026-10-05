"""Rest: a bed ends the day, heals by its quality, and charges first."""

from src.models import DMState, Event, Location, LocationType, Player, WorldClock, get_session
from src.services import rest as rest_mod
from src.services.rest import hours_until_morning, normalize_rest, rest_here, rest_now
from src.services.scene_objects import normalize_objects, resolve_interaction
from src.tools.world_write.rest import rest as rest_tool
from src.web import pending_actions


def _world(session, *, hour=20, minute=0, day=1, health="hurt", coin=20, spec=None, bed=True):
    session.add(WorldClock(day=day, hour=hour, minute=minute))
    session.add(DMState())
    objects = [{"kind": "rest", "label": "Upstairs bed", "x": 86, "y": 30,
                "payload": {"text": "A narrow bed under the eaves."}}] if bed else []
    tavern = Location(name="The Drowned Lantern", type=LocationType.BUILDING, depth=0, discovered=True,
                      objects=normalize_objects(objects), rest=spec)
    hollow = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True)
    session.add_all([tavern, hollow])
    session.flush()
    player = Player(name="Rook", current_location_id=tavern.id, health_status=health, currency=coin)
    session.add(player)
    session.commit()
    return {"player": player.id, "tavern": tavern.id, "hollow": hollow.id}


INN = {"kind": "inn", "price": 8, "quality": "good"}


def _snapshot(world):
    with get_session() as session:
        p = session.get(Player, world["player"])
        c = session.query(WorldClock).first()
        return {"health": p.health_status, "coin": p.currency, "day": c.day, "hour": c.hour, "minute": c.minute or 0}


def test_normalizer_clamps_and_rejects_garbage():
    assert normalize_rest({"kind": "inn", "price": "12", "quality": "GOOD"}) == {"kind": "inn", "price": 12, "quality": "good"}
    assert normalize_rest({"kind": "camp"}) == {"kind": "camp", "price": 0, "quality": "fair"}
    assert normalize_rest({"kind": "inn", "price": -5, "quality": "royal"}) == {"kind": "inn", "price": 0, "quality": "fair"}
    assert normalize_rest({"kind": "palace"}) is None
    assert normalize_rest("inn") is None and normalize_rest(None) is None


def test_hours_until_morning():
    assert hours_until_morning(WorldClock(day=1, hour=17, minute=30)) == 13.5
    assert hours_until_morning(WorldClock(day=3, hour=2, minute=0)) == 5.0
    assert hours_until_morning(WorldClock(day=1, hour=7, minute=0)) == 24.0


def test_no_bed_no_spec_refuses_and_touches_nothing(db):
    with get_session() as session:
        world = _world(session, bed=False)
    before = _snapshot(world)
    result = rest_now(world["player"])
    assert result["error"] == "no_rest_here"
    assert _snapshot(world) == before


def test_inn_charges_first_heals_by_quality_and_wakes_at_first_light(db):
    with get_session() as session:
        world = _world(session, hour=17, minute=30, health="badly_hurt", spec=normalize_rest(INN))
    pending_actions.clear(world["player"])
    result = rest_now(world["player"])
    assert result["success"] and result["paid"] == 8
    assert result["health"] == {"before": "badly_hurt", "after": "healthy"}, "good bed = 3 steps"
    after = _snapshot(world)
    assert (after["day"], after["hour"], after["minute"]) == (2, 7, 0)
    assert after["coin"] == 12 and after["health"] == "healthy"
    assert result["cinematic"]["kind"] == "title_card" and result["cinematic"]["title"] == "DAY 2"
    block = pending_actions.format_pending_actions(pending_actions.drain(world["player"]))
    assert "Rested: They slept the night at The Drowned Lantern (paid 8 coin)" in block
    assert "first light on Day 2" in block and "Open on the MORNING" in block
    assert {"advance_time", "update_player_health", "adjust_currency"} <= pending_actions.tool_equivalents(
        [{"kind": "rest", "summary": "x", "tools": []}]
    )


def test_too_poor_is_refused_with_health_and_clock_untouched(db):
    with get_session() as session:
        world = _world(session, coin=3, spec=normalize_rest(INN))
    before = _snapshot(world)
    result = rest_now(world["player"])
    assert result["error"] == "cannot_afford" and result["price"] == 8 and result["currency"] == 3
    assert _snapshot(world) == before


def test_past_midnight_sleeps_to_the_same_morning(db):
    with get_session() as session:
        world = _world(session, hour=2, day=3, health="winded", spec=normalize_rest({"kind": "camp", "quality": "poor"}))
    result = rest_now(world["player"])
    assert result["success"] and result["paid"] == 0
    after = _snapshot(world)
    assert (after["day"], after["hour"]) == (3, 7)
    assert after["health"] == "healthy"


def test_a_bed_with_no_spec_is_a_makeshift_camp(db):
    with get_session() as session:
        world = _world(session, health="hurt")
        assert rest_here(session.get(Location, world["tavern"])) == rest_mod.MAKESHIFT
    result = rest_now(world["player"])
    assert result["success"] and result["kind"] == "camp" and result["paid"] == 0
    assert result["health"]["after"] == "winded", "a poor bed is one step"


def test_the_dead_do_not_sleep(db):
    with get_session() as session:
        world = _world(session, health="dead", spec=normalize_rest(INN))
    assert rest_now(world["player"])["error"] == "dead"


def test_canvas_bed_and_dm_tool_share_the_service(db):
    with get_session() as session:
        world = _world(session, health="hurt", spec=normalize_rest(INN))
        bed_id = normalize_objects(session.get(Location, world["tavern"]).objects)[0]["id"]
    result = resolve_interaction(world["player"], world["tavern"], bed_id)
    assert result["success"] and result["consumed"] is False, "a bed is reusable"
    assert result["result"]["rest"]["success"] and result["result"]["rest"]["day"] == 2
    # And again through the DM's door the next night (coin runs out first)
    with get_session() as session:
        session.get(Player, world["player"]).currency = 2
        session.commit()
    tool_result = rest_tool(world["player"])
    assert tool_result["error"] == "cannot_afford" and "instruction" in tool_result
    refused = resolve_interaction(world["player"], world["tavern"], bed_id)
    assert refused["refused"] is True and "8" in refused["message"]


def test_seed_tavern_sleeps_and_the_hollow_does_not(tmp_path):
    from src.data.playtest_seed import create_playtest_world
    from src.models import reset_engine
    reset_engine()
    create_playtest_world(str(tmp_path / "emberfall.db"))
    with get_session() as session:
        tavern = session.query(Location).filter(Location.name == "The Drowned Lantern").first()
        hollow = session.query(Location).filter(Location.name == "Wolf's Hollow").first()
        assert rest_here(tavern) == {"kind": "inn", "price": 8, "quality": "good"}
        assert any(o["kind"] == "rest" for o in normalize_objects(tavern.objects))
        assert rest_here(hollow) is None


# ----------------------------------------------------------------------
# A night is not a skip button: scheduled events wake the sleeper
# ----------------------------------------------------------------------

def _schedule(name, day, hour, *, places=(), visible=True):
    with get_session() as session:
        event = Event(name=name, description=f"{name} happens.", event_type="meso",
                      scheduled_day=day, scheduled_hour=hour, locations_involved=list(places),
                      consequences=["The North Gate is barred"], player_visible=visible)
        session.add(event)
        session.commit()
        return event.id


def _event(event_id):
    with get_session() as session:
        e = session.get(Event, event_id)
        return {"occurred": (e.occurred_day, e.occurred_hour), "narrated": e.narrated_to_player,
                "witnessed": e.player_witnessed}


def test_the_evening_howl_wakes_the_sleeper_and_the_bed_waits_for_a_whole_night(db):
    """Playtest 2026-10-04: sleeping at 19:30 jumped past the seeded 20:00
    Evening Howl, and the town's set piece never reached the screen."""
    with get_session() as session:
        world = _world(session, hour=19, minute=30, health="badly_hurt", spec=normalize_rest(INN))
    howl = _schedule("The Evening Howl", 1, 20, places=[world["tavern"]])
    pending_actions.clear(world["player"])

    result = rest_now(world["player"])
    assert result["success"] and result["interrupted"] is True
    assert result["woken_by"]["name"] == "The Evening Howl"
    assert (result["day"], result["hour"], result["minute"], result["hours"]) == (1, 20, 0, 0.5)
    assert result["paid"] == 0 and result["cinematic"] is None
    assert result["health"] == {"before": "badly_hurt", "after": "badly_hurt"}, "half an hour heals nothing"
    assert result["summary"].startswith("Woken at 20:00 by The Evening Howl")
    after = _snapshot(world)
    assert (after["day"], after["hour"], after["minute"], after["coin"]) == (1, 20, 0, 20)
    # Fired at the moment it woke them; the ledger line is its narration
    assert _event(howl) == {"occurred": (1, 20), "narrated": True, "witnessed": True}
    block = pending_actions.format_pending_actions(pending_actions.drain(world["player"]))
    assert "woken at 20:00 on Day 1 by The Evening Howl" in block and "happening NOW" in block
    assert "The North Gate is barred" in block and "Open on the MORNING" not in block

    # Back to bed: the howl is spent, so this night runs to first light, paid
    again = rest_now(world["player"])
    assert again["interrupted"] is False and again["woken_by"] is None
    assert (again["day"], again["hour"], again["paid"]) == (2, 7, 8)
    assert again["health"]["after"] == "healthy"


def test_a_broken_night_heals_the_share_that_was_slept(db):
    with get_session() as session:
        world = _world(session, hour=22, health="badly_hurt", spec=normalize_rest(INN))
    _schedule("Wolves at the Wall", 2, 3)                     # no place: the world's own
    result = rest_now(world["player"])
    assert result["interrupted"] and result["hours"] == 5.0
    assert result["health"]["after"] == "hurt", "5 of 9 hours of a 3-step bed is one step"
    assert (result["day"], result["hour"]) == (2, 3)


def test_an_event_already_due_wakes_them_before_sleep_comes(db):
    """Due but unfired: the tick that would fire it has not run yet (the
    reply moved the clock with @time and then wrote @rest)."""
    with get_session() as session:
        world = _world(session, hour=20, minute=15, spec=normalize_rest(INN))
    _schedule("The Evening Howl", 1, 20)
    result = rest_now(world["player"])
    assert result["interrupted"] and result["hours"] == 0 and (result["hour"], result["minute"]) == (20, 15)
    assert "before sleep came" in result["summary"]
    assert _snapshot(world)["coin"] == 20


def test_hidden_distant_and_dawn_events_let_them_sleep(db):
    with get_session() as session:
        world = _world(session, hour=21, spec=normalize_rest(INN))
    hidden = _schedule("Smugglers Meet", 1, 23, visible=False)
    distant = _schedule("Ambush in the Hollow", 2, 1, places=[world["hollow"]])   # another settlement
    dawn = _schedule("Market Bell", 2, rest_mod.MORNING_HOUR)                     # first light itself
    result = rest_now(world["player"])
    assert result["interrupted"] is False and (result["day"], result["hour"]) == (2, 7)
    # Sleep never fires what it did not stop for — the next tick does
    for event_id in (hidden, distant, dawn):
        assert _event(event_id)["occurred"] == (None, None)


def test_a_broken_night_reads_in_minutes_and_a_free_camp_charges_nothing(db):
    with get_session() as session:
        world = _world(session, hour=19, minute=47, spec=normalize_rest({"kind": "camp", "price": 0}))
    _schedule("The Evening Howl", 1, 20)
    pending_actions.clear(world["player"])
    result = rest_now(world["player"])
    assert result["summary"] == (
        "Woken at 20:00 by The Evening Howl, after 13 minutes of sleep at The Drowned Lantern. "
        "The night is broken."
    ), "never '0.22 hours'; and a free camp has no bed to not charge"
    block = pending_actions.format_pending_actions(pending_actions.drain(world["player"]))
    assert "by The Evening Howl after 13 minutes of sleep" in block


def test_a_long_broken_night_rounds_to_hours():
    assert rest_mod._slept(0) == "before sleep came"
    assert rest_mod._slept(1) == "after 1 minute of sleep"
    assert rest_mod._slept(120) == "after 2 hours of sleep"
    assert rest_mod._slept(200) == "after about 3 hours of sleep"

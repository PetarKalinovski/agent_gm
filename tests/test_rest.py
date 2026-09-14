"""Rest: a bed ends the day, heals by its quality, and charges first."""

from src.models import DMState, Location, LocationType, Player, WorldClock, get_session
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

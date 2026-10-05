"""@move spends the road's own travel time; a @time restating it is not paid twice.

fleet5-bram T7: `@move Wolf's Hollow | through` with no @time, prose "Forty-five
minutes of road" — the clock moved only the 15-minute turn floor, the same
road home cost the 45 minutes the DM wrote at T15, and the 20:00 howl was told
early and then again. A canvas door always charged the road (travel_now); now
the DM's @move does too.
"""

import pytest

from src.models import Connection, Location, LocationType, Player, WorldClock, get_session
from src.services.commands import CommandRunner
from src.services.world_tick import enforce_minimum_time_cost, snapshot_clock


@pytest.fixture()
def roads(db):
    with get_session() as session:
        session.add(WorldClock(day=1, hour=19, minute=0))
        town = Location(name="Emberfall", type=LocationType.TOWN, discovered=True)
        session.add(town)
        session.flush()
        square = Location(name="Market Square", type=LocationType.DISTRICT, parent_id=town.id,
                          discovered=True, visited=True)
        session.add(square)
        session.flush()
        inn = Location(name="The Drowned Lantern", type=LocationType.BUILDING, parent_id=square.id,
                       discovered=True)
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, parent_id=town.id, discovered=True)
        cellar = Location(name="Cellar", type=LocationType.ROOM, parent_id=square.id, discovered=True)
        session.add_all([inn, hollow, cellar])
        session.flush()
        session.add_all([
            Connection(from_location_id=square.id, to_location_id=inn.id, travel_type="walk",
                       travel_time_hours=0.1, bidirectional=True, discovered=True),
            Connection(from_location_id=square.id, to_location_id=hollow.id, travel_type="walk",
                       travel_time_hours=0.75, bidirectional=True, discovered=True,
                       requirements=["a gate pass"]),
        ])
        player = Player(name="Bram Holloway", current_location_id=square.id, currency=25)
        session.add(player)
        session.commit()
        return {"player": player.id, "square": square.id, "inn": inn.id, "hollow": hollow.id,
                "cellar": cellar.id}


def _clock():
    day, hour, minute = snapshot_clock()
    return (day, hour, minute)


def _where(w):
    with get_session() as session:
        return session.get(Player, w["player"]).current_location_id


def test_a_move_down_a_long_road_spends_its_time(roads):
    r = CommandRunner(roads["player"])
    res = r.run("@move Wolf's Hollow | through")
    assert res.ok and _where(roads) == roads["hollow"]
    assert res.result["road_minutes"] == 45
    assert _clock() == (1, 19, 45)
    # The clock moved, so the turn floor adds nothing on top
    assert enforce_minimum_time_cost((1, 19, 0))["enforced"] is False
    assert _clock() == (1, 19, 45)


def test_the_road_home_costs_the_same(roads):
    assert CommandRunner(roads["player"]).run("@move Wolf's Hollow | through").ok
    r = CommandRunner(roads["player"])                # the next reply: the road is cleared for them
    assert r.run("@move Market Square").ok          # bidirectional: the same 45 minutes back
    assert _clock() == (1, 20, 30)


def test_a_time_restating_the_trip_is_not_charged_twice(roads):
    r = CommandRunner(roads["player"])
    assert r.run("@move Wolf's Hollow | through").ok
    res = r.run("@time 45m | the walk north")
    assert not res.ok and res.result["skipped"] == "already_charged"
    assert "not charged twice" in res.message and "45-minute road" in res.message
    assert _clock() == (1, 19, 45)
    # Restated once: a second @time is another wait, charged in full
    assert r.run("@time 30m").ok
    assert _clock() == (1, 20, 15)
    assert any("not charged twice" in line for line in r.ledger())


def test_a_longer_time_is_reduced_by_the_road(roads):
    r = CommandRunner(roads["player"])
    assert r.run("@move Wolf's Hollow | through").ok
    res = r.run("@time 1h")
    assert res.ok and "only 15 more minutes passed" in res.message
    assert _clock() == (1, 20, 0)


def test_a_time_written_before_the_move_is_the_journey(roads):
    r = CommandRunner(roads["player"])
    assert r.run("@time 45m").ok
    res = r.run("@move Wolf's Hollow | through")
    assert res.ok and "road_minutes" not in res.result
    assert _clock() == (1, 19, 45)


def test_short_steps_and_hierarchy_hops_cost_nothing_extra(roads):
    """The inn is six minutes off the square and the cellar is a doorway: the
    turn floor (15 minutes) covers both — a move never makes a turn cheaper."""
    r = CommandRunner(roads["player"])
    res = r.run("@move The Drowned Lantern")
    assert res.ok and "road_minutes" not in res.result
    assert _clock() == (1, 19, 0)
    assert r.run("@move Market Square").ok
    assert r.run("@move Cellar").ok
    assert _clock() == (1, 19, 0)
    assert enforce_minimum_time_cost((1, 19, 0))["enforced"] is True
    assert _clock() == (1, 19, 15)


def test_a_blocked_move_spends_nothing(roads):
    r = CommandRunner(roads["player"])
    res = r.run("@move Wolf's Hollow")
    assert not res.ok and res.result.get("blocked")
    assert _clock() == (1, 19, 0)
    # ...and a @time after it is the DM's own wait, in full
    assert r.run("@time 20m").ok
    assert _clock() == (1, 19, 20)


def test_a_room_with_no_road_of_its_own_walks_out_through_its_door(roads):
    """fleet-wenna T4: Cooper's Rooms (made by the DM, a room off the square
    with no Connection) to Wolf's Hollow is the doorway out to the square and
    the 45-minute north road — not the turn floor. And the inn's back room is
    six minutes off the square, the road in, then the same road north."""
    with get_session() as session:
        session.add(Location(name="Cooper's Rooms", type=LocationType.BUILDING,
                             parent_id=roads["square"], discovered=True))
        session.add(Location(name="Back Room", type=LocationType.ROOM, parent_id=roads["inn"], discovered=True))
        session.commit()
    r = CommandRunner(roads["player"])
    assert r.run("@move Cooper's Rooms").ok and _clock() == (1, 19, 0)      # a doorway: the floor's
    res = r.run("@move Wolf's Hollow | through")
    assert res.ok and res.result["road_minutes"] == 51                       # 6 out the door + 45
    assert _clock() == (1, 19, 51)
    r = CommandRunner(roads["player"])
    res = r.run("@move Back Room")                                           # 45 + 6 + 6
    assert res.ok and res.result["road_minutes"] == 57
    assert _clock() == (1, 20, 48)

"""Derived doors: deterministic, collision-free, inside the walkable area."""

from types import SimpleNamespace

from src.services.scene_exits import DOOR_SLOTS, MAX_DOORS, compute_exit_doors


def _location(x=50.0, y=50.0, bounds=None):
    return SimpleNamespace(
        position_x=x,
        position_y=y,
        walkable_bounds=bounds or {"x_min": 10, "x_max": 90, "y_min": 20, "y_max": 80},
    )


def _dest(dest_id, x=50.0, y=50.0, travel_type="walk"):
    return {"id": dest_id, "name": dest_id, "travel_type": travel_type,
            "position": {"x": x, "y": y}}


def test_doors_are_deterministic():
    location = _location()
    destinations = [
        _dest("north_road", 50.0, 10.0),
        _dest("east_road", 90.0, 50.0),
        _dest("cellar", 50.0, 50.0, travel_type="enter"),
    ]

    first = compute_exit_doors(location, destinations)
    second = compute_exit_doors(location, list(destinations))

    assert first == second, "the same scene must lay its doors out identically every load"
    assert set(first) == {"north_road", "east_road", "cellar"}


def test_child_gets_north_parent_gets_south():
    doors = compute_exit_doors(_location(), [
        _dest("tavern", 60.0, 60.0, travel_type="enter"),
        _dest("town", 20.0, 20.0, travel_type="exit"),
    ])

    assert doors["tavern"]["slot"] == "north"
    assert doors["town"]["slot"] == "south"


def test_bearing_picks_the_matching_slot():
    location = _location(x=50.0, y=50.0)
    doors = compute_exit_doors(location, [
        _dest("up", 50.0, 10.0),
        _dest("right", 90.0, 50.0),
        _dest("down", 50.0, 90.0),
        _dest("left", 10.0, 50.0),
    ])

    assert doors["up"]["slot"] == "north"
    assert doors["right"]["slot"] == "east"
    assert doors["down"]["slot"] == "south"
    assert doors["left"]["slot"] == "west"


def test_slots_never_collide():
    # Eight destinations that all want the same bearing
    destinations = [_dest(f"road_{i}", 50.0, 10.0) for i in range(8)]
    doors = compute_exit_doors(_location(), destinations)

    slots = [d["slot"] for d in doors.values()]
    assert len(doors) == 8
    assert len(set(slots)) == 8, "each destination must get its own rim slot"
    assert set(slots) == {name for name, _x, _y in DOOR_SLOTS}


def test_destinations_past_max_doors_get_none():
    destinations = [_dest(f"road_{i}", 50.0, 10.0) for i in range(MAX_DOORS + 3)]
    doors = compute_exit_doors(_location(), destinations)

    assert len(doors) == MAX_DOORS


def test_doors_clamped_into_walkable_bounds():
    bounds = {"x_min": 20, "x_max": 80, "y_min": 30, "y_max": 70}
    destinations = [_dest(f"road_{i}", 50.0, 10.0) for i in range(8)]

    doors = compute_exit_doors(_location(bounds=bounds), destinations)

    for door in doors.values():
        assert 22.0 <= door["x"] <= 78.0
        assert 32.0 <= door["y"] <= 68.0


def test_garbage_destinations_are_ignored():
    doors = compute_exit_doors(_location(), [
        None, "not a dict", {}, {"id": ""},
        {"id": "dupe", "travel_type": "walk", "position": {"x": 90, "y": 50}},
        {"id": "dupe", "travel_type": "walk", "position": {"x": 10, "y": 50}},
        {"id": "no_position"},
    ])

    assert set(doors) == {"dupe", "no_position"}
    assert doors["dupe"]["slot"] == "east"


def test_missing_bounds_and_position_still_place_doors():
    location = SimpleNamespace(position_x=None, position_y=None, walkable_bounds=None)
    doors = compute_exit_doors(location, [_dest("somewhere", 80.0, 50.0)])

    assert doors["somewhere"]["slot"] == "east"
    assert 0.0 <= doors["somewhere"]["x"] <= 100.0


# ---------------------------------------------------------------------------
# Doors stand in open floor
# ---------------------------------------------------------------------------

def _open_dest(id_="d1", **kw):
    row = {"id": id_, "name": "Out", "travel_type": "walk", "position": {"x": 90, "y": 50}}
    row.update(kw)
    return row


def test_door_slides_off_an_obstacle():
    """The live case: the east slot (90,50 after the margin) sits between a
    bar footprint and a barrel stack. The door slides along the east wall to
    the nearest clear spot instead of the notch."""
    loc = SimpleNamespace(
        position_x=50, position_y=50,
        walkable_bounds={"x_min": 10, "x_max": 90, "y_min": 20, "y_max": 80},
        obstacles=[
            [[71, 48], [86, 48], [86, 54], [71, 54]],
            [[84, 54], [95, 54], [95, 60], [84, 60]],
        ],
    )
    doors = compute_exit_doors(loc, [_open_dest()])
    door = doors["d1"]
    assert door["slot"] == "east"
    assert door["x"] == 88.0                # still on the east wall
    assert door["y"] != 50.0                # moved off the notch
    assert door["y"] <= 42.0 or door["y"] >= 66.0  # clear of both footprints
    assert 22.0 <= door["y"] <= 78.0        # inside the walkable band


def test_door_stays_put_on_open_floor():
    loc = SimpleNamespace(
        position_x=50, position_y=50,
        walkable_bounds={"x_min": 10, "x_max": 90, "y_min": 20, "y_max": 80},
        obstacles=[[[20, 20], [30, 20], [30, 30], [20, 30]]],
    )
    door = compute_exit_doors(loc, [_open_dest()])["d1"]
    assert (door["x"], door["y"]) == (88.0, 50.0)


def test_door_placement_is_deterministic_with_obstacles():
    loc = SimpleNamespace(
        position_x=50, position_y=50,
        walkable_bounds={"x_min": 10, "x_max": 90, "y_min": 20, "y_max": 80},
        obstacles=[[[80, 40], [95, 40], [95, 60], [80, 60]]],
    )
    a = compute_exit_doors(loc, [_open_dest()])
    b = compute_exit_doors(loc, [_open_dest()])
    assert a == b


def test_garbage_obstacles_are_ignored():
    loc = SimpleNamespace(
        position_x=50, position_y=50,
        walkable_bounds={"x_min": 10, "x_max": 90, "y_min": 20, "y_max": 80},
        obstacles=["nope", [[1]], [[None, None], [2, 2], [3, 3]], 42],
    )
    door = compute_exit_doors(loc, [_open_dest()])["d1"]
    assert (door["x"], door["y"]) == (88.0, 50.0)

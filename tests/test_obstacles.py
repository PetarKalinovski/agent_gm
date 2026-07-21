"""Obstacle detection plumbing: box→footprint conversion and persistence."""

from src.services.image_generator import (
    MAX_OBSTACLES,
    boxes_to_footprint_polygons,
)


def test_box_converts_to_bottom_slice_footprint():
    # A box spanning y 200-600, x 100-500 (0-1000 scale)
    polys = boxes_to_footprint_polygons([{"label": "well", "box_2d": [200, 100, 600, 500]}])
    assert len(polys) == 1
    poly = polys[0]
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    assert min(xs) == 10.0 and max(xs) == 50.0  # x passes through, scaled to 0-100
    # Footprint keeps only the bottom 45%: top = 20 + (60-20)*0.55 = 42
    assert min(ys) == 42.0 and max(ys) == 60.0


def test_tiny_and_malformed_boxes_are_dropped():
    polys = boxes_to_footprint_polygons([
        {"label": "speck", "box_2d": [500, 500, 510, 510]},   # ~0.01% area
        {"label": "no-box"},
        {"box_2d": [1, 2, 3]},                                # wrong arity
        {"box_2d": [600, 500, 400, 300]},                     # inverted
        "not-a-dict",
    ])
    assert polys == []


def test_caps_at_max_obstacles_and_clamps():
    boxes = [{"box_2d": [-100, -100, 1200, 1200]} for _ in range(MAX_OBSTACLES + 5)]
    polys = boxes_to_footprint_polygons(boxes)
    assert len(polys) == MAX_OBSTACLES
    for poly in polys:
        for x, y in poly:
            assert 0.0 <= x <= 100.0 and 0.0 <= y <= 100.0


def test_location_obstacles_column_roundtrip(db):
    from src.models import get_session
    from src.models.location import Location, LocationType

    polys = [[[10.0, 20.0], [30.0, 20.0], [30.0, 40.0], [10.0, 40.0]]]
    with get_session() as session:
        loc = Location(name="Test Square", type=LocationType.POI, obstacles=polys)
        session.add(loc)
        session.commit()
        loc_id = loc.id

    with get_session() as session:
        loaded = session.get(Location, loc_id)
        assert loaded.obstacles == polys

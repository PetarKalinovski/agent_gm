"""Derived door positions for the canvas.

Doors are NOT stored — they are computed from the destination's bearing on
the world map every time a scene loads, so every existing world gets doors
for free. Deterministic and stateless: the same inputs always produce the
same coordinates, which is what stops a door from wandering when the player
walks back into a room.
"""

import math
from typing import Any


DOOR_SLOTS = [                       # normalized scene coords, on the rim
    ("north",      50.0,  8.0), ("northeast", 84.0, 14.0),
    ("east",       92.0, 50.0), ("southeast", 84.0, 86.0),
    ("south",      50.0, 92.0), ("southwest", 16.0, 86.0),
    ("west",        8.0, 50.0), ("northwest", 16.0, 14.0),
]
MAX_DOORS = 8

# Slot name -> index in DOOR_SLOTS, which is ordered clockwise from north
SLOT_INDEX = {name: i for i, (name, _x, _y) in enumerate(DOOR_SLOTS)}

# How far inside the walkable area a door must sit
EDGE_MARGIN = 2.0
DEFAULT_BOUNDS = {"x_min": 10.0, "x_max": 90.0, "y_min": 20.0, "y_max": 80.0}

# World-map deltas smaller than this carry no direction worth reading
BEARING_EPSILON = 1.0

# A door wants this much open floor around it (normalized units). Playtesting
# found the tavern's east door wedged in a two-unit notch between the bar's
# footprint and a barrel stack — reachable only by a detour nobody finds.
DOOR_CLEARANCE = 6.0
DOOR_SLIDE_STEP = 2.0
DOOR_SLIDE_MAX = 30.0


def _point_in_polygon(x: float, y: float, poly: list) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _segment_distance(x: float, y: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2))
    px, py = ax + t * dx, ay + t * dy
    return math.hypot(x - px, y - py)


def _clean_polygons(obstacles: Any) -> list[list[tuple[float, float]]]:
    polygons = []
    for poly in obstacles if isinstance(obstacles, list) else []:
        points = []
        for point in poly if isinstance(poly, (list, tuple)) else []:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                px, py = _number(point[0], float("nan")), _number(point[1], float("nan"))
                if not (math.isnan(px) or math.isnan(py)):
                    points.append((px, py))
        if len(points) >= 3:
            polygons.append(points)
    return polygons


def _clearance(x: float, y: float, polygons: list) -> float:
    """Distance from a point to the nearest obstacle edge; 0 inside one."""
    best = float("inf")
    for poly in polygons:
        if _point_in_polygon(x, y, poly):
            return 0.0
        n = len(poly)
        for i in range(n):
            ax, ay = poly[i]
            bx, by = poly[(i + 1) % n]
            best = min(best, _segment_distance(x, y, ax, ay, bx, by))
    return best


def _slide_clear(
    x: float, y: float, slot: str, polygons: list,
    x_min: float, x_max: float, y_min: float, y_max: float,
) -> tuple[float, float]:
    """Move a door along its rim until it stands in open floor.

    Candidates walk outward from the slot's default position along the
    scene edge the slot sits on (side slots slide vertically, top/bottom
    slots horizontally, corners try both), and the nearest candidate with
    DOOR_CLEARANCE wins. With no clear spot the least-blocked one is used,
    so a cluttered rim still gets a door.
    """
    if not polygons or _clearance(x, y, polygons) >= DOOR_CLEARANCE:
        return x, y

    axes: list[str] = []
    if slot in ("east", "west"):
        axes = ["y"]
    elif slot in ("north", "south"):
        axes = ["x"]
    else:
        axes = ["x", "y"]

    candidates: list[tuple[float, float, float]] = []  # (offset, x, y)
    offset = DOOR_SLIDE_STEP
    while offset <= DOOR_SLIDE_MAX:
        for axis in axes:
            for sign in (-1.0, 1.0):
                cx = x + sign * offset if axis == "x" else x
                cy = y + sign * offset if axis == "y" else y
                if x_min <= cx <= x_max and y_min <= cy <= y_max:
                    candidates.append((offset, cx, cy))
        offset += DOOR_SLIDE_STEP

    best_xy, best_score = (x, y), _clearance(x, y, polygons)
    for offset, cx, cy in candidates:
        clear = _clearance(cx, cy, polygons)
        if clear >= DOOR_CLEARANCE:
            return cx, cy
        if clear > best_score:
            best_xy, best_score = (cx, cy), clear
    return best_xy


def _number(value: Any, fallback: float) -> float:
    """Coerce to float, falling back on anything unusable (None, "", NaN)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if math.isnan(number) or math.isinf(number):
        return fallback
    return number


def _bearing_slot(dx: float, dy: float) -> int:
    """Bucket a world-map delta into one of the eight slots.

    y grows southward on the world map, so north is -y. Returns the index of
    "north" when the two places sit effectively on top of each other.
    """
    if abs(dx) < BEARING_EPSILON and abs(dy) < BEARING_EPSILON:
        return SLOT_INDEX["north"]
    # 0 = north, +pi/2 = east — matches the clockwise DOOR_SLOTS order
    angle = math.atan2(dx, -dy)
    return int(round(angle / (math.pi / 4))) % len(DOOR_SLOTS)


def _clamp_bounds(walkable_bounds: Any) -> tuple[float, float, float, float]:
    """Inset walkable bounds by EDGE_MARGIN, tolerating garbage."""
    bounds = walkable_bounds if isinstance(walkable_bounds, dict) else {}
    x_min = _number(bounds.get("x_min"), DEFAULT_BOUNDS["x_min"]) + EDGE_MARGIN
    x_max = _number(bounds.get("x_max"), DEFAULT_BOUNDS["x_max"]) - EDGE_MARGIN
    y_min = _number(bounds.get("y_min"), DEFAULT_BOUNDS["y_min"]) + EDGE_MARGIN
    y_max = _number(bounds.get("y_max"), DEFAULT_BOUNDS["y_max"]) - EDGE_MARGIN
    # A degenerate box collapses to its centre rather than inverting
    if x_min > x_max:
        x_min = x_max = (x_min + x_max) / 2
    if y_min > y_max:
        y_min = y_max = (y_min + y_max) / 2
    return x_min, x_max, y_min, y_max


def compute_exit_doors(location, destinations: list[dict]) -> dict[str, dict]:
    """Assign each destination a door on the rim of the scene.

    Preference, in order: a child location ("enter") gets north, the parent
    ("exit") gets south, everything else follows the compass bearing between
    the two world-map positions. Ties are broken by walking DOOR_SLOTS
    forward from the preferred slot, and destinations are processed in a
    stable (preference, id) order so the layout survives a reload.

    Args:
        location: The Location the player is standing in (may be None).
        destinations: `get_available_destinations` rows.

    Returns:
        exit_id -> {"x": float, "y": float, "slot": str}. Destinations past
        MAX_DOORS get no door — the edge-push chooser still reaches them.
    """
    origin_x = _number(getattr(location, "position_x", None), 50.0)
    origin_y = _number(getattr(location, "position_y", None), 50.0)
    x_min, x_max, y_min, y_max = _clamp_bounds(getattr(location, "walkable_bounds", None))
    polygons = _clean_polygons(getattr(location, "obstacles", None))

    # Resolve each destination's preferred slot, deduped by id
    preferences: list[tuple[int, str]] = []
    seen: set[str] = set()
    for dest in destinations or []:
        if not isinstance(dest, dict):
            continue
        dest_id = dest.get("id")
        if not dest_id or dest_id in seen:
            continue
        seen.add(dest_id)

        travel_type = str(dest.get("travel_type") or "").lower()
        if travel_type == "enter":
            preferred = SLOT_INDEX["north"]
        elif travel_type == "exit":
            preferred = SLOT_INDEX["south"]
        else:
            position = dest.get("position") if isinstance(dest.get("position"), dict) else {}
            dx = _number(position.get("x"), origin_x) - origin_x
            dy = _number(position.get("y"), origin_y) - origin_y
            preferred = _bearing_slot(dx, dy)
        preferences.append((preferred, str(dest_id)))

    preferences.sort()

    doors: dict[str, dict] = {}
    taken: set[int] = set()
    for preferred, dest_id in preferences:
        if len(doors) >= MAX_DOORS:
            break
        for step in range(len(DOOR_SLOTS)):
            index = (preferred + step) % len(DOOR_SLOTS)
            if index in taken:
                continue
            taken.add(index)
            slot, x, y = DOOR_SLOTS[index]
            x = max(x_min, min(x_max, x))
            y = max(y_min, min(y_max, y))
            x, y = _slide_clear(x, y, slot, polygons, x_min, x_max, y_min, y_max)
            doors[dest_id] = {"x": x, "y": y, "slot": slot}
            break

    return doors

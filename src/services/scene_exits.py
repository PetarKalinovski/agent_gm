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
            doors[dest_id] = {
                "x": max(x_min, min(x_max, x)),
                "y": max(y_min, min(y_max, y)),
                "slot": slot,
            }
            break

    return doors

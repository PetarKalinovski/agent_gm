"""Canvas interactables: normalize what the model wrote, resolve touches.

An object placed on the canvas is a thing nobody minds you touching, so
code owns the whole interaction — no LLM, no DM turn. That only stays safe
because `normalize_objects` strips every number and effect an LLM might
invent: the creator agent picks ONE word from `take / read / open / search`
and writes flavour text, and the engine owns everything else. Item payloads
are cut down to {name, type, description, value, quantity} — scene objects
never carry `slot`, `tier`, or `effect_tag`; tiered gear enters the world
only through merchants and quest rewards.

HTTP mapping for `resolve_interaction` results (the endpoint owns the
status codes): `locked` / `spent` / `error == "not here"` -> 409,
`error == "no such object"` / `"no player"` -> 404, success -> 200.
"""

import math
import re
from typing import Any

from src.models import Location, Player, get_session


OBJECT_KINDS = ("take", "read", "open", "search")   # FIXED MENU. Never extend at runtime.

# Objects are consumed by take/open/search; `read` is re-readable forever.
CONSUMING_KINDS = ("take", "open", "search")

MAX_OBJECTS_PER_LOCATION = 3
LABEL_MAX = 48
TEXT_MAX = 400

# The ONLY keys an authored item payload may carry
ITEM_KEYS = ("name", "type", "description", "value", "quantity")
ITEM_TYPES = ("consumable", "weapon", "armor", "quest_item", "misc")
MAX_ITEM_QUANTITY = 5

COORD_MIN, COORD_MAX = 10.0, 90.0
DEFAULT_X, DEFAULT_Y = 50.0, 65.0


def _slug(text: str) -> str:
    """Lowercase identifier fragment, safe to concatenate."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return slug or "object"


def _clamp_coord(value: Any, fallback: float) -> float:
    """Coerce a scene coordinate into the placeable band."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if math.isnan(number) or math.isinf(number):
        return fallback
    return max(COORD_MIN, min(COORD_MAX, number))


def _normalize_item(raw: Any) -> dict[str, Any] | None:
    """Strip an authored item down to the five keys the engine allows."""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None

    try:
        value = int(raw.get("value") or 0)
    except (TypeError, ValueError):
        value = 0
    try:
        quantity = int(raw.get("quantity") or 1)
    except (TypeError, ValueError):
        quantity = 1

    item_type = str(raw.get("type") or "").strip().lower()
    return {
        "name": name,
        "type": item_type if item_type in ITEM_TYPES else "misc",
        "description": str(raw.get("description") or "")[:TEXT_MAX],
        "value": max(0, value),
        "quantity": min(MAX_ITEM_QUANTITY, max(1, quantity)),
    }


def normalize_objects(raw: Any) -> list[dict[str, Any]]:
    """Coerce whatever an LLM produced into valid object rows. Never raises.

    Drops entries that are not dicts or carry an unknown `kind`, assigns
    missing ids, clamps coordinates, truncates text, strips effects and
    unknown keys from item payloads, drops `lock` on anything but `open`,
    and caps the list at MAX_OBJECTS_PER_LOCATION.
    """
    normalized: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    for index, entry in enumerate(raw or []):
        if len(normalized) >= MAX_OBJECTS_PER_LOCATION:
            break
        if not isinstance(entry, dict):
            continue

        kind = str(entry.get("kind") or "").strip().lower()
        if kind not in OBJECT_KINDS:
            continue

        label = str(entry.get("label") or "").strip()[:LABEL_MAX] or kind.capitalize()

        base_id = str(entry.get("id") or "").strip() or f"{_slug(label)}_{index}"
        object_id = base_id
        suffix = 2
        while object_id in used_ids:
            object_id = f"{base_id}_{suffix}"
            suffix += 1
        used_ids.add(object_id)

        raw_payload = entry.get("payload")
        raw_payload = raw_payload if isinstance(raw_payload, dict) else {}
        payload: dict[str, Any] = {}

        text = raw_payload.get("text")
        if isinstance(text, str) and text.strip():
            payload["text"] = text.strip()[:TEXT_MAX]

        item = _normalize_item(raw_payload.get("item"))
        if item:
            payload["item"] = item

        # A lock is the hand-off to the DM — meaningless on anything but a container
        if kind == "open" and raw_payload.get("lock"):
            payload["lock"] = str(raw_payload["lock"])[:LABEL_MAX * 2]

        normalized.append({
            "id": object_id,
            "kind": kind,
            "label": label,
            "x": _clamp_coord(entry.get("x"), DEFAULT_X),
            "y": _clamp_coord(entry.get("y"), DEFAULT_Y),
            "payload": payload,
            "consumed": bool(entry.get("consumed", False)),
        })

    return normalized


def public_objects(raw: Any) -> list[dict[str, Any]]:
    """What the client is allowed to see before it touches anything.

    Payload text, items, and the lock reason are NEVER sent up front — the
    reveal is the whole point of the interaction. `locked` ships as a bare
    boolean so the renderer can draw a keyhole.
    """
    return [
        {
            "id": obj["id"],
            "kind": obj["kind"],
            "label": obj["label"],
            "x": obj["x"],
            "y": obj["y"],
            "consumed": obj["consumed"],
            "locked": bool(obj["kind"] == "open" and obj["payload"].get("lock")),
        }
        for obj in normalize_objects(raw)
    ]


def _mark_consumed(location_id: str, object_id: str) -> None:
    """Flip `consumed` and write the list back.

    The JSON column must be REASSIGNED, not mutated in place, or SQLAlchemy
    sees old == new and skips the UPDATE (same trap `_normalize_inventory`
    documents). `normalize_objects` already returns fresh dicts.
    """
    with get_session() as session:
        location = session.get(Location, location_id)
        if not location:
            return
        objects = normalize_objects(location.objects)
        changed = False
        for obj in objects:
            if obj["id"] == object_id and not obj["consumed"]:
                obj["consumed"] = True
                changed = True
        if changed:
            location.objects = objects
            session.commit()


def resolve_interaction(player_id: str, location_id: str, object_id: str) -> dict[str, Any]:
    """Apply a canvas interaction in code. Returns the /api/interact body.

    Args:
        player_id: The player's ID.
        location_id: The scene the client thinks it is in.
        object_id: Which interactable was touched.

    Returns:
        On success `{"success": True, "object_id", "kind", "label",
        "consumed", "result": {"floater", "panel_title", "panel_text",
        "items_gained"}}`; otherwise a `locked` / `spent` / `error` body.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"success": False, "error": "no player"}
        if player.current_location_id != location_id:
            return {"success": False, "error": "not here"}

        location = session.get(Location, location_id)
        if not location:
            return {"success": False, "error": "no such object"}

        match = next(
            (obj for obj in normalize_objects(location.objects) if obj["id"] == object_id),
            None,
        )
        if not match:
            return {"success": False, "error": "no such object"}

        kind = match["kind"]
        label = match["label"]
        payload = match["payload"]
        already_consumed = match["consumed"]

    # A locked container is fiction, not code — hand it to the DM untouched
    if kind == "open" and payload.get("lock"):
        return {
            "success": False,
            "locked": True,
            "label": label,
            "lock_hint": payload["lock"],
            "chat_prompt": f"I try to open the {label.lower()}.",
        }

    if kind in CONSUMING_KINDS and already_consumed:
        return {"success": False, "spent": True, "label": label}

    # Imported here so the tools layer can import this module freely
    from src.tools.world_write.player import add_to_inventory

    item = payload.get("item")
    text = payload.get("text")
    items_gained: list[dict[str, Any]] = []
    floater = None

    if kind != "read" and item:
        added = add_to_inventory(
            player_id,
            item["name"],
            item_type=item["type"],
            description=item["description"],
            value=item["value"],
            quantity=item["quantity"],
        )
        if added.get("success"):
            items_gained.append({"name": item["name"], "quantity": item["quantity"]})
            floater = f"+ {item['name']}"

    consumed = kind in CONSUMING_KINDS
    if consumed:
        _mark_consumed(location_id, object_id)

    show_panel = bool(text) and kind in ("read", "open", "search")
    return {
        "success": True,
        "object_id": object_id,
        "kind": kind,
        "label": label,
        "consumed": consumed,
        "result": {
            "floater": floater,
            "panel_title": label if show_panel else None,
            "panel_text": text if show_panel else None,
            "items_gained": items_gained,
        },
    }

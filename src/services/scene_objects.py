"""Canvas interactables: normalize what the model wrote, resolve touches.

An object placed on the canvas is a thing nobody minds you touching, so
code owns the whole interaction — no LLM, no DM turn. That only stays safe
because `normalize_objects` strips every number and effect an LLM might
invent: the creator agent picks ONE word from `take / read / open / search`
and writes flavour text, and the engine owns everything else. Item payloads
are cut down to {name, type, description, value, quantity} (and a document's
own words, `reads`) — scene objects never carry `slot`, `tier`, or
`effect_tag`; tiered gear enters the world only through merchants and quest
rewards.

HTTP mapping for `resolve_interaction` results (the endpoint owns the
status codes): `locked` / `spent` / `error == "not here"` -> 409,
`error == "no such object"` / `"no player"` -> 404, success -> 200.
"""

import math
import re
from typing import Any

from src.models import Location, Player, get_session
from src.models.item import Item


OBJECT_KINDS = ("take", "read", "open", "search", "rest", "body")   # FIXED MENU. Never extend at runtime.

# What an author (creator agent, world forge, editor) may place; a body is
# placed by the engine only, when someone dies (src/services/loot.py)
AUTHORED_KINDS = ("take", "read", "open", "search", "rest")
ENGINE_SRC = "engine"

# Objects are consumed by take/open/search/body; `read` and `rest` are reusable.
CONSUMING_KINDS = ("take", "open", "search", "body")

MAX_OBJECTS_PER_LOCATION = 3     # the authored pool (take/read/open/search)
MAX_RESTS_PER_LOCATION = 1       # one bed or campfire per scene
MAX_BODIES_PER_LOCATION = 4      # the engine pool; oldest evicted first
LABEL_MAX = 48
TEXT_MAX = 400

# The ONLY keys an authored item payload may carry. `reads` is a document's
# own words (a letter, a ledger page), optional and never mechanics: fleet6-
# wenna T17 opened the satchel, found a letter whose payload said only who it
# was addressed to, and the DM wrote its contents itself — three sealed
# campaign truths in one paragraph
ITEM_KEYS = ("name", "type", "description", "value", "quantity", "reads")
ITEM_TYPES = ("consumable", "weapon", "armor", "quest_item", "misc")
MAX_ITEM_QUANTITY = 5
READS_MAX = 600

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
    item = {
        "name": name,
        "type": item_type if item_type in ITEM_TYPES else "misc",
        "description": str(raw.get("description") or "")[:TEXT_MAX],
        "value": max(0, value),
        "quantity": min(MAX_ITEM_QUANTITY, max(1, quantity)),
    }
    reads = raw.get("reads")
    if isinstance(reads, str) and reads.strip():
        item["reads"] = reads.strip()[:READS_MAX]
    return item


def _normalize_loot_item(raw: Any) -> dict[str, Any] | None:
    """A body's item keeps its mechanics: round-trip through the Item model
    so slot / tier / effect_tag / visual_desc / attire survive the grave."""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    data = dict(raw)
    data.setdefault("id", _slug(name))
    data.setdefault("type", "misc")
    data.setdefault("value", 0)
    data.setdefault("description", "")
    try:
        item = Item.from_dict(data).to_dict()
    except Exception:
        return None
    try:
        item["quantity"] = max(1, int(item.get("quantity") or 1))
    except (TypeError, ValueError):
        item["quantity"] = 1
    return item


def normalize_objects(raw: Any, *, trusted: bool = True) -> list[dict[str, Any]]:
    """Coerce whatever an LLM produced into valid object rows. Never raises.

    Drops entries that are not dicts or carry an unknown `kind`, assigns
    missing ids, clamps coordinates, truncates text, strips effects and
    unknown keys from item payloads, drops `lock` on anything but `open`,
    and caps each pool: MAX_OBJECTS_PER_LOCATION authored objects, one
    `rest`, MAX_BODIES_PER_LOCATION bodies (oldest evicted).

    `trusted=False` is the LLM/editor authoring boundary: a `body` (and its
    `items`/`coin`) can only enter through the engine (`src == "engine"` on
    a row that came out of the database), never through a model's JSON.
    """
    authored: list[dict[str, Any]] = []
    rests: list[dict[str, Any]] = []
    bodies: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    for index, entry in enumerate(raw or []):
        if not isinstance(entry, dict):
            continue

        kind = str(entry.get("kind") or "").strip().lower()
        if kind not in OBJECT_KINDS:
            continue
        engine_entry = bool(trusted and entry.get("src") == ENGINE_SRC)
        if kind == "body" and not engine_entry:
            continue        # nobody authors a corpse with loot on it

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

        # Engine-only: what a body carries (full item dicts + coin)
        if kind == "body" and engine_entry:
            payload["items"] = [
                i for i in (_normalize_loot_item(x) for x in (raw_payload.get("items") or []))
                if i is not None
            ]
            try:
                payload["coin"] = max(0, int(raw_payload.get("coin") or 0))
            except (TypeError, ValueError):
                payload["coin"] = 0
            if raw_payload.get("npc_id"):
                payload["npc_id"] = str(raw_payload["npc_id"])

        row: dict[str, Any] = {
            "id": object_id,
            "kind": kind,
            "label": label,
            "x": _clamp_coord(entry.get("x"), DEFAULT_X),
            "y": _clamp_coord(entry.get("y"), DEFAULT_Y),
            "payload": payload,
            "consumed": bool(entry.get("consumed", False)),
        }
        if engine_entry:
            row["src"] = ENGINE_SRC
        if kind == "body":
            bodies.append(row)
        elif kind == "rest":
            rests.append(row)
        else:
            authored.append(row)

    return (
        authored[:MAX_OBJECTS_PER_LOCATION]
        + rests[:MAX_RESTS_PER_LOCATION]
        + bodies[-MAX_BODIES_PER_LOCATION:]
    )


def public_row(obj: dict[str, Any]) -> dict[str, Any]:
    """One object as the client may see it (no payload)."""
    return {
        "id": obj["id"],
        "kind": obj["kind"],
        "label": obj["label"],
        "x": obj["x"],
        "y": obj["y"],
        "consumed": obj["consumed"],
        "locked": bool(obj["kind"] == "open" and obj["payload"].get("lock")),
    }


def public_objects(raw: Any) -> list[dict[str, Any]]:
    """What the client is allowed to see before it touches anything.

    Payload text, items, and the lock reason are NEVER sent up front — the
    reveal is the whole point of the interaction. `locked` ships as a bare
    boolean so the renderer can draw a keyhole.
    """
    return [public_row(obj) for obj in normalize_objects(raw)]


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


def resolve_interaction(player_id: str, location_id: str, object_id: str,
                        unlocked: bool = False) -> dict[str, Any]:
    """Apply a canvas interaction in code. Returns the /api/interact body.

    Args:
        player_id: The player's ID.
        location_id: The scene the client thinks it is in.
        object_id: Which interactable was touched.
        unlocked: The DM's fiction opened a locked container (`@interact
            <thing> | through`: the owner's leave, the key, a good roll).
            The canvas never passes it.

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

    # A locked container is fiction, not code — hand it to the DM untouched.
    # Once the DM says the fiction opened it, the engine hands over what is
    # inside like any open: before this, Maera's satchel letter could reach
    # the pack only by a @give that left the satchel "holding" it forever
    if kind == "open" and payload.get("lock") and not unlocked:
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
    coin_gained = 0

    if kind == "body":
        # The dead keep their mechanics: spawn the full item, not a flavour copy
        from src.tools.world_write.items import adjust_currency, spawn_item_to_user
        for loot in payload.get("items") or []:
            quantity = max(1, int(loot.get("quantity") or 1))
            spawned = spawn_item_to_user(player_id, loot, quantity, user_type="player")
            if "error" not in spawned:
                items_gained.append({"name": loot["name"], "quantity": quantity})
        coin = int(payload.get("coin") or 0)
        if coin > 0:
            paid = adjust_currency(player_id, coin)
            if "error" not in paid:
                coin_gained = coin
        found = [f"{i['name']}" + (f" x{i['quantity']}" if i["quantity"] > 1 else "") for i in items_gained]
        if coin_gained:
            found.append(f"{coin_gained} coin")
        text = (f"{text} " if text else "") + (f"You find: {', '.join(found)}." if found else "Nothing worth taking.")
        if coin_gained and not items_gained:
            floater = f"+ {coin_gained} coin"
        elif items_gained:
            floater = f"+ {items_gained[0]['name']}"

    rest_result = None
    if kind == "rest":
        from src.services.rest import rest_now
        rest_result = rest_now(player_id)
        if "error" in rest_result:
            reason = rest_result["error"]
            message = {
                "cannot_afford": f"The bed costs {rest_result.get('price')} and you carry {rest_result.get('currency')}.",
                "no_rest_here": "There is nowhere safe to sleep here.",
                "dead": "The dead do not sleep.",
            }.get(reason, "You cannot rest now.")
            return {"success": False, "refused": True, "error": reason, "label": label, "message": message}

    document = None
    if kind not in ("read", "body", "rest") and item:
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
            # A document found is read on the spot, in its own words only
            if item.get("reads"):
                document = {"item": item["name"], "text": item["reads"]}

    consumed = kind in CONSUMING_KINDS
    if consumed:
        _mark_consumed(location_id, object_id)

    show_panel = bool(text) and kind in ("read", "open", "search", "body")
    panel_title = label if show_panel else None
    panel_text = text if show_panel else None
    if document:
        panel_title = panel_title or document["item"]
        panel_text = f"{panel_text}\n\n{document['text']}" if panel_text else document["text"]
    body = {
        "success": True,
        "object_id": object_id,
        "kind": kind,
        "label": label,
        "consumed": consumed,
        "result": {
            "floater": floater,
            "panel_title": panel_title,
            "panel_text": panel_text,
            "items_gained": items_gained,
            "coin_gained": coin_gained,
            "rest": rest_result,
        },
    }
    if document:
        body["result"]["document"] = document
    return body

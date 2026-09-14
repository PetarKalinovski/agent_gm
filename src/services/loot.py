"""Loot — what the dead carried stays on them, where they fell.

Neither `kill_npc` nor a canvas fight ever moved a dead NPC's shelf to the
player; the DM had to remember, and mostly didn't. Now a death leaves a
`body` scene object on the location: the sparkle pulses, the prompt says
"[E] Search", and `/api/interact` hands over the gear WITH its mechanics
(slot, tier, attire — a hood that stops concealing when looted is a bug)
plus a code-owned coin drop by danger tier. A body is a choice on the
canvas, persists across reloads, and never fires mid-fight against a
snapshotted HUD.
"""

from __future__ import annotations

import logging
from typing import Any

from src.models import Location, NPC
from src.services import progression, scene_objects
from src.services.combat import resolve_danger_tier

logger = logging.getLogger(__name__)


def body_label(npc: NPC) -> str:
    name = (npc.name or "someone").strip()
    return f"{name}'s body"[:scene_objects.LABEL_MAX]


def body_payload(npc: NPC) -> dict[str, Any]:
    """{"items": [...full item dicts...], "coin": int, "npc_id": ...}"""
    items: list[dict[str, Any]] = []
    for raw in (npc.inventory_notable or []):
        item = scene_objects._normalize_loot_item(raw)
        if item:
            items.append(item)
    tier = resolve_danger_tier(npc)
    coin = progression.coin_drop(tier)
    return {"items": items, "coin": coin, "npc_id": npc.id}


def leave_body(session, npc: NPC, *, x: float | None = None, y: float | None = None) -> dict[str, Any] | None:
    """Move the NPC's notables (+ tier coin) onto their location as a body.

    Clears the NPC's shelf (a corpse is not a shop). Returns the object row,
    or None when there is nothing to leave. Mutates inside the caller's
    transaction; the caller commits.
    """
    payload = body_payload(npc)
    if not payload["items"] and not payload["coin"]:
        return None
    if not npc.current_location_id:
        return None
    location = session.get(Location, npc.current_location_id)
    if location is None:
        return None

    objects = [
        o for o in scene_objects.normalize_objects(location.objects)
        if not (o["kind"] == "body" and (o.get("payload") or {}).get("npc_id") == npc.id)
    ]
    body = {
        "id": f"body_{npc.id[:8]}",
        "kind": "body",
        "label": body_label(npc),
        "x": float(x) if x is not None else float(npc.position_x or 50.0),
        "y": float(y) if y is not None else float(npc.position_y or 50.0),
        "payload": {**payload, "text": f"{npc.name} lies where they fell."},
        "src": scene_objects.ENGINE_SRC,
        "consumed": False,
    }
    objects.append(body)
    normalized = scene_objects.normalize_objects(objects)   # applies the body cap
    location.objects = normalized                            # reassign: JSON column
    npc.inventory_notable = []
    kept = next((o for o in normalized if o["id"] == body["id"]), None)
    if kept is None:
        logger.warning(f"body for {npc.name} did not survive normalization")
        return None
    logger.info(
        f"Body left: {body['label']} at {location.name} "
        f"({len(payload['items'])} item(s), {payload['coin']} coin)"
    )
    return kept

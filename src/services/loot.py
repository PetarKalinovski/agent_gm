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
import re
from typing import Any

from src.models import Location, NPC
from src.services import progression, scene_objects
from src.services.combat import resolve_danger_tier

logger = logging.getLogger(__name__)


def body_name(npc: NPC, known: bool = True) -> str:
    """Who the body is, as the player may read it: their name, or — for
    someone nobody learned the name of — what they looked like (killing a
    stranger must not hand the player their name). Beasts keep theirs."""
    name = (npc.name or "someone").strip()
    if not known:
        from src.services.dialogue import is_beast, role_label

        if not is_beast(npc):
            name = f"the {role_label(npc).lower()}"
    return name


def body_label(npc: NPC, known: bool = True) -> str:
    return f"{body_name(npc, known)}'s body"[:scene_objects.LABEL_MAX]


def body_payload(npc: NPC, *, story_cast: bool = False) -> dict[str, Any]:
    """{"items": [...full item dicts...], "coin": int, "npc_id": ...}

    A beast carries no purse: fleet6-bram's two dead wolves held 2 and 1
    coin, and the aftermath told of "whatever they carried still on them".
    What it really had on it (a drilled wolf's collar) still drops, and so
    does its trophy (`beast_trophy`) — unless it is one of the story's own
    cast (`story_cast`), who leaves only what it carried."""
    from src.services.dialogue import is_beast

    items: list[dict[str, Any]] = []
    for raw in (npc.inventory_notable or []):
        item = scene_objects._normalize_loot_item(raw)
        if item:
            items.append(item)
    tier = resolve_danger_tier(npc)
    beast = is_beast(npc)
    coin = 0 if beast else progression.coin_drop(tier)
    if beast and not story_cast:
        trophy = scene_objects._normalize_loot_item(beast_trophy(npc, tier))
        if trophy:
            items.append(trophy)
    return {"items": items, "coin": coin, "npc_id": npc.id}


# fleet10-bram T13/T17, -tamsin T14/T19: every predator "carried nothing
# worth taking", so a wolf fight paid 5 XP and nothing a merchant would buy.
# A beast leaves its fang (a weak one) or its hide — plain goods with a
# catalog value by tier (progression.BEAST_TROPHY_VALUE), sold at the
# counter like anything else, never worn and never a clue.
_SCALED = frozenset({"snake", "serpent", "viper", "lizard", "crocodile", "drake", "wyrm", "wyvern", "basilisk"})
_HORNED = frozenset({"boar", "hog", "pig", "sow", "bull", "ox", "goat", "ram", "stag", "deer", "elk"})


def _animal_word(npc: NPC) -> str:
    """"wolf" for a Torn-Ear Wolf or a species-"wolf" row; "beast" when no
    animal word is written anywhere."""
    try:
        from src.services.image_generator import ImageGenerator
        animals = ImageGenerator._BEAST_WORDS
    except Exception:       # an asset import never decides a trophy's name
        animals = frozenset({"wolf"})
    for text in (npc.species, npc.name):
        for word in reversed(re.findall(r"[a-z]+", str(text or "").lower())):
            if word in animals:
                return "wolf" if word == "wolves" else word
    return "beast"


def beast_trophy(npc: NPC, tier: str | None = None) -> dict[str, Any] | None:
    """The trophy a beast's body leaves, as an item dict, or None when its
    tier leaves none (harmless game, a boss)."""
    tier = tier or resolve_danger_tier(npc)
    value = progression.beast_trophy_value(tier)
    if value <= 0:
        return None
    animal = _animal_word(npc)
    if animal in _SCALED:
        part, what = "Hide", "a scaled hide"
    elif animal in _HORNED:
        part, what = ("Tusk", "a curved tusk") if animal in ("boar", "hog", "pig", "sow") else ("Horn", "a horn")
    elif tier == "weak":
        part, what = "Fang", "a long fang"
    else:
        part, what = "Pelt", "a thick pelt"
    name = f"{animal.title()} {part}"
    return {
        "id": f"{animal}_{part.lower()}",
        "name": name,
        "type": "misc",
        "value": value,
        "description": f"{what[:1].upper()}{what[1:]} off a dead {animal}. A trader will give coin for it.",
    }


def _story_cast(session, npc: NPC) -> bool:
    """Is this NPC one of the story's own cast — the villain, the successor,
    or someone a beat not yet played needs (campaign.story_cast_ids, read
    for one row that may already be dead)? True on any trouble: the cast is
    never a pelt by accident."""
    try:
        from src.models import Campaign
        from src.services.campaign import _beat_cast, _campaign_beats

        for campaign in session.query(Campaign).filter(Campaign.status == "active").all():
            if npc.id in (campaign.villain_npc_id, campaign.successor_npc_id):
                return True
            for beat in _campaign_beats(session, campaign):
                if beat.status in ("pending", "armed", "fired", "stalled") and \
                        _beat_cast(session, beat, alive_only=False, npcs=[npc]):
                    return True
        return False
    except Exception as e:
        logger.warning(f"story cast unread for {npc.id}, no trophy: {e}")
        return True


def leave_body(session, npc: NPC, *, x: float | None = None, y: float | None = None) -> dict[str, Any] | None:
    """Move the NPC's notables (+ tier coin) onto their location as a body.

    Clears the NPC's shelf (a corpse is not a shop). Returns the object row,
    or None when there is nothing to leave. Mutates inside the caller's
    transaction; the caller commits.
    """
    from src.services.dialogue import is_beast

    payload = body_payload(npc, story_cast=is_beast(npc) and _story_cast(session, npc))
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
    from src.models import NPCRelationship
    from src.services.dialogue import name_known_clause

    # Someone whose NAME was learned — a row alone is a talk, not a name
    known = session.query(NPCRelationship).filter(
        NPCRelationship.npc_id == npc.id, name_known_clause()).first() is not None
    who = body_name(npc, known)
    body = {
        "id": f"body_{npc.id[:8]}",
        "kind": "body",
        "label": f"{who}'s body"[:scene_objects.LABEL_MAX],
        "x": float(x) if x is not None else float(npc.position_x or 50.0),
        "y": float(y) if y is not None else float(npc.position_y or 50.0),
        "payload": {**payload, "text": f"{who[:1].upper()}{who[1:]} lies where they fell."},
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

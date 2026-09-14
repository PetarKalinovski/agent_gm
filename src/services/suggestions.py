"""Suggested actions — two to four chips under the chat input, derived in code.

A blank text box is a blank page. The chips name what is actually possible
right now, in this order: the lead (the one thing the tracker shows), the
people standing here, the ways out, the things on the floor, and "look
around" to pad to two. Nothing is authored at runtime; nothing costs a
model call. Chips that resolve on the canvas (a door, an object) are
routed by the client through the same REST paths the canvas uses — no DM
turn — and the rest just fill the input.

Shape: {"label", "text", "kind": lead|talk|travel|interact|look,
        "npc_id"?, "location_id"?, "object_id"?, "locked"?}
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.models import Location, NPC
from src.services import scene_objects
from src.services.travel import check_route
from src.tools.world_read.locations import get_available_destinations

logger = logging.getLogger(__name__)

MAX_CHIPS = 4
MIN_CHIPS = 2

_VERB_LABELS = {"take": "Take", "read": "Read", "open": "Open", "search": "Search",
                "body": "Search", "rest": "Rest at"}
_LEADING_VERB = re.compile(
    r"^(?:find(?: out)?|ask|talk to|speak (?:to|with)|go to|reach|get to|bring|deliver|"
    r"kill|obtain|get|take|search|seek|visit|meet)\s+", re.I,
)


def _first_name(name: str) -> str:
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    if not parts:
        return "them"
    # "Captain Brann Coldiron" -> "Brann"; "Maera" -> "Maera"
    honorifics = {"captain", "sir", "lady", "lord", "master", "old", "the", "mister", "miss", "mother", "father"}
    for part in parts:
        if part.lower().strip(".") not in honorifics:
            return part
    return parts[-1]


def _topic(lead: dict[str, Any]) -> str:
    """'Find out what is organizing the wolves' -> 'what is organizing the wolves'."""
    text = str(lead.get("text") or "").strip().rstrip(".")
    stripped = _LEADING_VERB.sub("", text)
    if stripped and stripped != text:
        return stripped
    return str(lead.get("quest_title") or text)


def _lead_chip(lead: dict[str, Any] | None, exits_by_id: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not lead or not lead.get("text"):
        return None
    target = lead.get("target") or {}
    hop = lead.get("route_next_hop_location_id")
    if target.get("type") == "npc" and target.get("here"):
        first = _first_name(target.get("name") or "")
        return {
            "kind": "talk", "npc_id": target.get("id"),
            "label": f"Ask {first} about {_topic(lead)}"[:60],
            "text": f"I go to {target.get('name')} and ask about {_topic(lead)}.",
        }
    if hop and hop in exits_by_id:
        ex = exits_by_id[hop]
        return {
            "kind": "travel", "location_id": hop, "locked": bool(ex.get("locked")),
            "label": f"Head to {ex.get('name')}"[:60],
            "text": f"I head to {ex.get('name')}.",
        }
    return {"kind": "lead", "label": str(lead["text"])[:60], "text": f"I {_lower_first(lead['text'])}"}


def _lower_first(text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    body = text[0].lower() + text[1:]
    return body if body.endswith((".", "!", "?")) else body + "."


def suggest_actions(session, player, lead: dict[str, Any] | None,
                    location_id: str | None) -> list[dict[str, Any]]:
    """The chips for this moment. Never raises."""
    try:
        return _suggest(session, player, lead, location_id)
    except Exception as e:
        logger.warning(f"suggestions failed: {e}")
        return []


def _suggest(session, player, lead, location_id) -> list[dict[str, Any]]:
    chips: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(chip: dict[str, Any] | None) -> None:
        if not chip or len(chips) >= MAX_CHIPS:
            return
        key = (chip["kind"], str(chip.get("npc_id") or chip.get("location_id") or chip.get("object_id") or chip["label"]))
        if key in seen:
            return
        seen.add(key)
        chips.append(chip)

    exits_by_id: dict[str, dict[str, Any]] = {}
    location = session.get(Location, location_id) if location_id else None
    if location is not None:
        try:
            for d in get_available_destinations(location.id) or []:
                if not isinstance(d, dict) or not d.get("id"):
                    continue
                try:
                    route = check_route(session, location.id, d["id"], player)
                    locked = bool(route.get("blocked"))
                except Exception:
                    locked = bool(d.get("requirements"))
                exits_by_id[d["id"]] = {"id": d["id"], "name": d.get("name") or "?", "locked": locked}
        except Exception as e:
            logger.debug(f"exits for suggestions unavailable: {e}")

    # 1. The lead
    add(_lead_chip(lead, exits_by_id))

    # 2. People here
    if location is not None:
        npcs = session.query(NPC).filter(
            NPC.current_location_id == location.id, NPC.status == "alive"
        ).all()
        for npc in npcs:
            add({
                "kind": "talk", "npc_id": npc.id,
                "label": f"Talk to {_first_name(npc.name)}"[:60],
                "text": f"I approach {npc.name} and start talking.",
            })

    # 2b. A counter to trade at
    if location is not None:
        for npc in npcs:
            if npc.inventory_notable:
                add({
                    "kind": "trade", "npc_id": npc.id,
                    "label": f"Trade with {_first_name(npc.name)}"[:60],
                    "text": f"I look over what {npc.name} has for sale.",
                })

    # 3. Ways out (unlocked first)
    for ex in sorted(exits_by_id.values(), key=lambda e: (e["locked"], e["name"])):
        add({
            "kind": "travel", "location_id": ex["id"], "locked": ex["locked"],
            "label": f"Head to {ex['name']}"[:60],
            "text": f"I head to {ex['name']}." if not ex["locked"] else f"I try to get through to {ex['name']}.",
        })

    # 4. Things here
    if location is not None:
        for obj in scene_objects.public_objects(location.objects):
            if obj.get("consumed"):
                continue
            verb = _VERB_LABELS.get(obj.get("kind"), "Use")
            add({
                "kind": "interact", "object_id": obj["id"],
                "label": f"{verb} the {obj.get('label') or 'thing'}"[:60],
                "text": f"I {verb.lower()} the {obj.get('label') or 'thing'}.",
            })

    # 5. Never fewer than two
    if len(chips) < MIN_CHIPS:
        add({"kind": "look", "label": "Look around", "text": "I take a careful look around."})
    return chips[:MAX_CHIPS]

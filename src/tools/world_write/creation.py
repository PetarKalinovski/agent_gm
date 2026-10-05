"""Growing the world mid-play: one call per new place or person.

These replace `prompt_creator_agent`, a sub-agent that took ~30 tool calls on
a slow model to add anything — and on the 2026-10-04 playtest, asked to
"populate" an empty town node during the opening, re-created the Market
Square and the tavern under new names, moved two existing NPCs into the
copies, and authored a duplicate of an existing quest. These tools cannot do
any of that: they refuse to make a second copy of a place or person that
exists (and hand back the original), they always attach a new place to where
the player stands, and they never touch anything that already exists.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from strands import tool

from src.models import NPC, Connection, Location, LocationType, NPCTier, Player, get_session
from src.services.combat import ARCHETYPES, DANGER_TIERS
from src.services.rest import normalize_rest
from src.services.scene_objects import normalize_objects

_KIND_TO_TYPE = {
    "room": LocationType.INTERIOR,
    "interior": LocationType.INTERIOR,
    "building": LocationType.BUILDING,
    "shop": LocationType.BUILDING,
    "street": LocationType.DISTRICT,
    "district": LocationType.DISTRICT,
    "outdoors": LocationType.POI,
    "wilds": LocationType.POI,
    "landmark": LocationType.POI,
}
_FILLER = {"the", "a", "an", "of", "old", "ye"}


def _key(name: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9']+", (name or "").lower()) if w not in _FILLER]
    return " ".join(words)


def _same_name(a: str, b: str) -> bool:
    ka, kb = _key(a), _key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    return difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.86


@tool
def create_location(
    player_id: str,
    name: str,
    description: str,
    kind: str = "room",
    inside_current: bool = True,
    travel_minutes: int = 5,
    atmosphere_tags: list[str] | None = None,
    objects: list[dict] | None = None,
    rest: dict | None = None,
) -> dict[str, Any]:
    """Create a NEW place next to (or inside) where the player stands.

    Only for somewhere that does not exist yet — the scene lists "Ways out"
    and "Known places"; use those first. If a place with this name (or nearly
    this name) already exists, nothing is created and you get the existing
    one back to move_player into.

    Args:
        player_id: The player's ID.
        name: The place's proper name ("The Tannery Loft", "Cellar").
        description: 2-3 sentences: what it looks like, smells like, who uses it.
        kind: room | building | shop | street | district | outdoors | wilds | landmark.
        inside_current: True when it is part of the current place (a back room,
            a cellar, a stall in this market); False when it is a separate place
            reached from here (a path out of town, the next street).
        travel_minutes: How long the walk from here takes.
        atmosphere_tags: A few mood words.
        objects: 0-3 canvas interactables, same shape as the world editor uses.
        rest: {"kind": "inn"|"camp", "price": coin, "quality": "poor"|"fair"|"good"}
            only if someone could actually sleep here.

    Returns:
        {"id", "name", "created": True} or {"id", "name", "exists": True}.
    """
    with get_session() as session:
        player = session.get(Player, player_id)
        if player is None or not player.current_location_id:
            return {"error": "player_not_placed"}
        here = session.get(Location, player.current_location_id)
        if here is None:
            return {"error": "player_not_placed"}
        for loc in session.query(Location).all():
            if _same_name(loc.name, name):
                return {
                    "id": loc.id, "name": loc.name, "exists": True,
                    "instruction": f"{loc.name} already exists — use it (move_player to {loc.id}); nothing was created.",
                }
        # Inside: a room of where they stand. Outside: a sibling, under the
        # same parent. Depth follows the parent — 2026-10-04's Cooper's Rooms
        # sat under Emberfall at depth 0, level with the town that held it
        parent = here
        if not inside_current and here.parent_id:
            parent = session.get(Location, here.parent_id) or here
        parent_id = parent.id
        location = Location(
            name=name.strip(),
            description=description.strip(),
            type=_KIND_TO_TYPE.get((kind or "").strip().lower(), LocationType.POI),
            parent_id=parent_id,
            depth=(parent.depth or 0) + 1,
            atmosphere_tags=list(atmosphere_tags or [])[:6],
            visited=False,
            discovered=True,
            objects=normalize_objects(objects, trusted=False) or None,
            rest=normalize_rest(rest),
        )
        session.add(location)
        session.flush()
        hours = max(1, min(int(travel_minutes or 5), 24 * 60)) / 60.0
        if parent_id != here.id:
            # A separate place: a road from here, not a door inside it
            session.add(Connection(
                from_location_id=here.id, to_location_id=location.id,
                travel_time_hours=hours, bidirectional=True, travel_type="walk", discovered=True,
            ))
        else:
            # Hierarchy already links parent and child; give the child its
            # own discovered door so the canvas and the brief list it
            session.add(Connection(
                from_location_id=here.id, to_location_id=location.id,
                travel_time_hours=hours, bidirectional=True, travel_type="door", discovered=True,
            ))
        session.commit()
        return {
            "id": location.id, "name": location.name, "created": True,
            "note": "It is a way out of the current scene now. move_player takes them there; its art paints on arrival.",
        }


@tool
def create_npc(
    player_id: str,
    name: str,
    profession: str,
    appearance: str,
    personality: str,
    voice: str,
    goals: list[str] | None = None,
    secret: str | None = None,
    mood: str = "neutral",
    danger_tier: str | None = None,
    combat_archetype: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Create a NEW named person, standing where the player is.

    For someone the scene needs who does not exist yet — the barkeep nobody
    authored, the guard who becomes more than a voice. If anyone with this
    name (or nearly) already exists, nothing is created and you get them
    back instead. You voice them yourself from then on.

    Someone only talked about is made `status` "dead" or "missing": on
    record (a name the story can use, a memory file, a note), standing
    nowhere. fleet5-wenna T3: the dead carter was made alive in the square
    and stood in the brief, and on the canvas, for eight turns.

    Args:
        player_id: The player's ID.
        name: Full name.
        profession: Their trade or role.
        appearance: One or two sentences — clothes, build, a detail (drives their sprite).
        personality: One or two sentences.
        voice: How they talk (rhythm, vocabulary, a verbal tic).
        goals: 1-2 things they want.
        secret: Optional — something they will not say until they trust the player.
        mood: Their mood right now.
        danger_tier: harmless | weak | standard | dangerous | deadly (never boss).
        combat_archetype: brute | skirmisher | stalker | ranged, if it matters.
        status: "dead" or "missing" for someone who is not here to be met
            (no location, no body, nothing to loot); anything else is alive.

    Returns:
        {"id", "name", "created": True} or {"id", "name", "exists": True}.
    """
    tier = (danger_tier or "").strip().lower() or None
    if tier not in DANGER_TIERS or tier == "boss":
        tier = None
    archetype = (combat_archetype or "").strip().lower() or None
    if archetype not in ARCHETYPES:
        archetype = None
    absent = (status or "").strip().lower()
    absent = absent if absent in ("dead", "missing") else None
    with get_session() as session:
        player = session.get(Player, player_id)
        if player is None or not player.current_location_id:
            return {"error": "player_not_placed"}
        for npc in session.query(NPC).all():
            if _same_name(npc.name, name):
                where = npc.current_location.name if npc.current_location is not None else "unknown"
                return {
                    "id": npc.id, "name": npc.name, "exists": True, "status": npc.status,
                    "instruction": f"{npc.name} already exists ({npc.status}, at {where}) — nothing was created.",
                }
        npc = NPC(
            name=name.strip(),
            tier=NPCTier.MINOR,
            profession=(profession or "").strip(),
            status=absent or "alive",
            # The dead lie nowhere (a body is the engine's, left by a death in
            # play); the missing still belong where they were last seen
            current_location_id=None if absent else player.current_location_id,
            home_location_id=None if absent == "dead" else player.current_location_id,
            description_physical=(appearance or "").strip(),
            description_personality=(personality or "").strip(),
            voice_pattern=(voice or "").strip(),
            goals=[g for g in (goals or []) if g][:3],
            secrets=[secret.strip()] if secret and secret.strip() else [],
            current_mood=(mood or "neutral").strip(),
            danger_tier=tier,
            combat_archetype=archetype,
            position_x=55.0,
            position_y=62.0,
        )
        session.add(npc)
        session.commit()
        if absent:
            return {"id": npc.id, "name": npc.name, "created": True, "status": absent,
                    "note": f"They are {absent} — on record, nowhere to be met."}
        return {"id": npc.id, "name": npc.name, "created": True,
                "note": "They are in the scene now; voice them with a speaker line."}

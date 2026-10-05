"""Where a new character first stands.

The server used to spawn into the first SETTLEMENT/TOWN-typed row. In a
generated world that row is the town's parent node — a map container — and
containers hold no people: the 2026-10-04 Emberfall playtest put a fresh
character in "Emberfall" with zero NPCs while Market Square (three of them)
and The Drowned Lantern sat one door away. Handed an empty town, the DM
invented a population on the spot and corrupted the world.

A first scene needs someone to talk to, and nobody who wants the player
dead. So the spawn is, in order:

  1. a place with no threat in it at all — one boss among three wolves is
     still a lair (forge_fresh's Wolf's Hollow out-counted the market);
  2. a place the world already marks as known (discovered/visited) — that
     is how WorldForge flags the starting area, and an undiscovered place
     has no discovered roads or children, so no exits and no lead route;
  3. the most living, named, unthreatening PEOPLE (an animal is not a
     conversation);
  4. fewest threats, then a leaf (district, building, room) over a
     container, then the name.

A region-level node is never chosen while anything else has people. Pure
over the session — no writes, no model.
"""

from __future__ import annotations

from collections import Counter
from typing import Callable

from src.models import NPC, Location, LocationType
from src.services import standing

# Map containers. A player standing "in the Kingdom" has no scene.
NEVER_SPAWN_TYPES = frozenset({
    LocationType.ROOT, LocationType.WORLD,
    LocationType.REGION_1, LocationType.REGION_2, LocationType.REGION_3,
    LocationType.CONTINENT, LocationType.KINGDOM, LocationType.PROVINCE,
    LocationType.GALAXY, LocationType.SECTOR, LocationType.SYSTEM, LocationType.PLANET,
})

# Lower is better. A settlement-level node is usually the parent of the
# places where the people actually are, so it only wins when it alone has them.
_TYPE_RANK = {
    LocationType.DISTRICT: 0,
    LocationType.BUILDING: 1,
    LocationType.POI: 1,
    LocationType.INTERIOR: 2,
    LocationType.ROOM: 2,
    LocationType.SETTLEMENT: 3,
    LocationType.CITY: 3,
    LocationType.TOWN: 3,
    LocationType.VILLAGE: 3,
    LocationType.STATION: 3,
}
_UNRANKED = 4

# Authored danger, not keyword inference: a "legendary brewer" is not a threat
THREAT_TIERS = frozenset({"deadly", "boss"})


def _type_rank(location: Location) -> int:
    if location.type in NEVER_SPAWN_TYPES:
        return _UNRANKED + 1
    return _TYPE_RANK.get(location.type, _UNRANKED)


def _known(location: Location) -> bool:
    return bool(location.discovered or location.visited)


def _beast_check() -> Callable[[NPC], bool]:
    """The sprite pipeline's animal test (name, species, physical words) —
    the one definition of "this is a wolf, not a person" the game has."""
    try:
        from src.services.image_generator import ImageGenerator
        return ImageGenerator._is_beast
    except Exception:  # never let an asset import decide whether the game starts
        return lambda npc: False


def _threatening(npc: NPC, player) -> bool:
    """Someone a brand-new character should not open the game beside."""
    if (npc.danger_tier or "").strip().lower() in THREAT_TIERS:
        return True
    if (npc.current_mood or "").strip().lower() in standing.HOSTILE_DISPOSITIONS:
        return True
    return bool(player is not None and npc.faction_id
                and standing.standing_for(player, npc.faction_id) == "hostile")


def _refuses(location: Location, player) -> bool:
    """A place whose faction hates the player would turn them out at the
    door (travel.check_route, reason "standing") — not a first scene."""
    return bool(player is not None and location.controlling_faction_id
                and standing.standing_for(player, location.controlling_faction_id) == "hostile")


def pick_spawn_location(session, *, player=None) -> Location | None:
    """The best place for a character's first scene, or None in an empty world.

    `player` is optional (the placeholder player does not exist yet when the
    server picks); when given, NPCs of factions that hate them and places
    those factions hold are passed over.

    See the module docstring for the order. Fallbacks: any place with anyone
    in it at all, then the most leaf-ish place (a region only when nothing
    else exists).
    """
    locations = session.query(Location).all()
    if not locations:
        return None

    is_beast = _beast_check()
    welcoming: Counter[str] = Counter()
    threats: Counter[str] = Counter()
    for npc in session.query(NPC).filter(
        NPC.status == "alive", NPC.current_location_id.isnot(None)
    ).all():
        if not (npc.name or "").strip():
            continue
        if _threatening(npc, player):
            threats[npc.current_location_id] += 1
        elif not is_beast(npc):
            welcoming[npc.current_location_id] += 1

    def name_key(loc: Location) -> tuple[str, str]:
        return ((loc.name or "").casefold(), loc.id)

    candidates = [
        loc for loc in locations
        if loc.type not in NEVER_SPAWN_TYPES and welcoming[loc.id] > 0 and not _refuses(loc, player)
    ]
    if candidates:
        return min(candidates, key=lambda loc: (
            threats[loc.id] > 0, not _known(loc), -welcoming[loc.id], threats[loc.id],
            _type_rank(loc), *name_key(loc)))

    peopled = [loc for loc in locations if welcoming[loc.id] or threats[loc.id]]
    if peopled:
        return min(peopled, key=lambda loc: (
            threats[loc.id] > 0, _refuses(loc, player), not _known(loc), -welcoming[loc.id],
            threats[loc.id], _type_rank(loc), *name_key(loc)))

    return min(locations, key=lambda loc: (
        _refuses(loc, player), not _known(loc), _type_rank(loc), *name_key(loc)))


# ---------------------------------------------------------------------------
# A beast at the edge of town
# ---------------------------------------------------------------------------
# fleet6-wenna stood at the north paddock fence three night turns (T7-T9)
# while the howl "walked the fence line in", and nothing came; fleet6-tamsin
# played 18 turns in two rooms with no danger at all. The complication
# director makes ONE weak scout of the story's own pack, real in the
# database (a body on the canvas, a roster @fight can build), at the rim of
# the scene on the side its wild comes from.

SCOUT_LOOKS = ("Torn-Ear", "Rib-Thin", "Pale-Muzzle", "Burr-Coat", "Lame-Foot", "Scar-Snout")
SCOUT_INSET = 12.0            # how far in from the rim it stands (0-100 scene units)


def _scout_position(location: Location, toward: Location | None) -> tuple[float, float]:
    """Just inside the door its wild lies behind; the north rim without one."""
    x, y = 50.0, 14.0
    if toward is not None:
        try:
            from src.services.scene_exits import compute_exit_doors

            door = compute_exit_doors(location, [{
                "id": toward.id,
                "position": {"x": toward.position_x, "y": toward.position_y},
            }]).get(toward.id)
            if door:
                x, y = float(door["x"]), float(door["y"])
        except Exception:      # a door the layout cannot place is the north rim
            pass
    # Pulled in toward the middle so it stands in the scene, not in the doorway
    x += SCOUT_INSET if x < 50 else -SCOUT_INSET if x > 50 else 0.0
    y += SCOUT_INSET if y < 50 else -SCOUT_INSET if y > 50 else 0.0
    return round(x, 1), round(y, 1)


def spawn_beast_scout(session, location: Location, *, species: str = "wolf", faction_id: str | None = None,
                      toward: Location | None = None) -> NPC:
    """Make one weak, hungry beast scout at `location` and return it (added
    and flushed; the caller commits). Its name is new in the world, its home
    is the wild it came from, and it belongs to the pack it scouts for."""
    from src.models import NPCTier

    species = (species or "wolf").strip().lower()
    taken = {(n.name or "").strip().lower() for n in session.query(NPC).all()}
    name = next((f"{look} {species.title()}" for look in SCOUT_LOOKS
                 if f"{look} {species.title()}".lower() not in taken),
                f"Lone {species.title()} {len(taken) + 1}")
    x, y = _scout_position(location, toward)
    npc = NPC(
        name=name,
        tier=NPCTier.AMBIENT,
        species=species,
        profession=f"lone {species} scout",
        faction_id=faction_id,
        current_location_id=location.id,
        home_location_id=toward.id if toward is not None else location.id,
        position_x=x,
        position_y=y,
        description_physical=(f"A lone {species}, thin and wary, ribs showing — hungry enough to test the "
                              "edge of town after dark."),
        description_personality="Hungry, bold at the edges, quick to bolt from fire or a raised voice.",
        goals=["Find food", "Test how close it can come"],
        current_mood="hungry",
        danger_tier="weak",
        combat_archetype="skirmisher",
    )
    session.add(npc)
    session.flush()
    return npc

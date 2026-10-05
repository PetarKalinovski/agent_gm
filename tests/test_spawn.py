"""Spawn: a first scene has people in it, never an empty container."""

from src.models import Faction, Location, LocationType, NPC, Player, get_session
from src.services.spawn import pick_spawn_location


def _loc(session, name, type_, parent=None):
    loc = Location(name=name, type=type_, parent_id=parent.id if parent else None,
                   depth=(parent.depth + 1) if parent else 0)
    session.add(loc)
    session.flush()
    return loc


def _npcs(session, loc, *names, **fields):
    for name in names:
        session.add(NPC(name=name, current_location_id=loc.id, **fields))
    session.flush()


def _pick(player=None):
    with get_session() as session:
        if player is not None:
            player = session.get(Player, player)
        loc = pick_spawn_location(session, player=player)
        return loc.name if loc is not None else None


def test_empty_town_parent_loses_to_its_populated_square(db):
    """The Emberfall playtest: the TOWN row had nobody, its children had everyone."""
    with get_session() as session:
        region = _loc(session, "The Ashen March", LocationType.REGION_2)
        town = _loc(session, "Emberfall", LocationType.TOWN, region)
        square = _loc(session, "Market Square", LocationType.DISTRICT, town)
        lantern = _loc(session, "The Drowned Lantern", LocationType.BUILDING, town)
        _npcs(session, square, "Captain Brann Coldiron", "Piper Vance", "Maera Lisk")
        _npcs(session, lantern, "Old Tobin")
        session.commit()
    assert _pick() == "Market Square"


def test_deadly_only_lair_is_not_preferred(db):
    with get_session() as session:
        town = _loc(session, "Emberfall", LocationType.TOWN)
        lair = _loc(session, "Wolf's Hollow", LocationType.POI, town)
        inn = _loc(session, "The Drowned Lantern", LocationType.BUILDING, town)
        _npcs(session, lair, "The Grey Alpha", "Ash Wraith", danger_tier="deadly")
        _npcs(session, lair, "The Pack Mother", danger_tier="boss")
        _npcs(session, inn, "Old Tobin")
        session.commit()
    assert _pick() == "The Drowned Lantern"


def test_threats_break_a_tie_between_equally_busy_places(db):
    with get_session() as session:
        a = _loc(session, "Arena Row", LocationType.DISTRICT)
        b = _loc(session, "Baker Street", LocationType.DISTRICT)
        _npcs(session, a, "Vendor One", "Vendor Two")
        _npcs(session, a, "The Champion", danger_tier="deadly")
        _npcs(session, b, "Baker", "Miller")
        session.commit()
    assert _pick() == "Baker Street"


def test_dead_and_hostile_npcs_do_not_count(db):
    with get_session() as session:
        graveyard = _loc(session, "Graveyard", LocationType.DISTRICT)
        camp = _loc(session, "Raider Camp", LocationType.POI)
        well = _loc(session, "Well Street", LocationType.DISTRICT)
        _npcs(session, graveyard, "Corpse A", "Corpse B", "Corpse C", status="dead")
        _npcs(session, camp, "Raider A", "Raider B", current_mood="murderous")
        _npcs(session, well, "Water Carrier")
        session.commit()
    assert _pick() == "Well Street"


def test_a_settlement_with_its_own_people_still_counts(db):
    with get_session() as session:
        village = _loc(session, "Thornwick", LocationType.VILLAGE)
        empty_square = _loc(session, "Thornwick Green", LocationType.DISTRICT, village)
        assert empty_square.id
        _npcs(session, village, "Reeve Hallam", "Goodwife Pell")
        session.commit()
    assert _pick() == "Thornwick"


def test_on_a_tie_the_leaf_beats_the_settlement_then_the_name_decides(db):
    with get_session() as session:
        town = _loc(session, "Emberfall", LocationType.TOWN)
        square = _loc(session, "Market Square", LocationType.DISTRICT, town)
        inn = _loc(session, "A Quiet Inn", LocationType.BUILDING, town)
        _npcs(session, town, "Town Crier")
        _npcs(session, square, "Stallholder")
        _npcs(session, inn, "Innkeeper")
        session.commit()
    assert _pick() == "Market Square"

    with get_session() as session:
        square = session.query(Location).filter(Location.name == "Market Square").one()
        session.add(Location(name="Cobbler Lane", type=LocationType.DISTRICT))
        session.flush()
        lane = session.query(Location).filter(Location.name == "Cobbler Lane").one()
        session.add(NPC(name="Cobbler", current_location_id=lane.id))
        session.commit()
    assert _pick() == "Cobbler Lane"


def test_a_region_is_never_chosen_while_a_leaf_has_people(db):
    with get_session() as session:
        kingdom = _loc(session, "The Kingdom", LocationType.KINGDOM)
        hut = _loc(session, "Hermit's Hut", LocationType.BUILDING, kingdom)
        _npcs(session, kingdom, "Wanderer A", "Wanderer B", "Wanderer C")
        _npcs(session, hut, "The Hermit")
        session.commit()
    assert _pick() == "Hermit's Hut"


def test_falls_back_to_any_place_with_people(db):
    with get_session() as session:
        root = _loc(session, "The World", LocationType.ROOT)
        _loc(session, "Empty Ruin", LocationType.POI, root)
        _npcs(session, root, "Lost Soul")
        session.commit()
    assert _pick() == "The World"


def test_nobody_anywhere_picks_the_most_leaf_like_place(db):
    with get_session() as session:
        root = _loc(session, "The World", LocationType.ROOT)
        town = _loc(session, "Emberfall", LocationType.TOWN, root)
        _loc(session, "Market Square", LocationType.DISTRICT, town)
        session.commit()
    assert _pick() == "Market Square"


def test_single_region_world_still_spawns_somewhere(db):
    with get_session() as session:
        _loc(session, "The World", LocationType.WORLD)
        session.commit()
    assert _pick() == "The World"


def test_empty_world_returns_none(db):
    assert _pick() is None


def test_a_player_is_not_spawned_among_a_faction_that_hates_them(db):
    with get_session() as session:
        watch = Faction(name="The Watch")
        session.add(watch)
        session.flush()
        barracks = _loc(session, "Barracks", LocationType.BUILDING)
        held = _loc(session, "Watch Square", LocationType.DISTRICT)
        held.controlling_faction_id = watch.id
        alley = _loc(session, "Back Alley", LocationType.DISTRICT)
        _npcs(session, barracks, "Watchman A", "Watchman B", "Watchman C", faction_id=watch.id)
        _npcs(session, held, "Fishwife", "Tinker")
        _npcs(session, alley, "Fence")
        player = Player(name="Rook", reputation={watch.id: 5})
        session.add(player)
        session.commit()
        player_id = player.id
    assert _pick() == "Barracks", "without a player nothing is known about standing"
    assert _pick(player_id) == "Back Alley"


def test_one_boss_makes_a_lair_however_many_others_stand_there(db):
    """forge_fresh.db: Wolf's Hollow held a boss, a scout and two wolves — three
    "welcoming" heads out-counted Market Square's two, and a new character
    opened the game beside the wolf-caller."""
    with get_session() as session:
        town = _loc(session, "Emberfall", LocationType.TOWN)
        hollow = _loc(session, "Wolf's Hollow", LocationType.POI, town)
        square = _loc(session, "Market Square", LocationType.DISTRICT, town)
        _npcs(session, hollow, "Aldric Vane", danger_tier="boss")
        _npcs(session, hollow, "Vex Marrow", danger_tier="dangerous")
        _npcs(session, hollow, "Brother Thorn", "Sister Ash", danger_tier="weak")
        _npcs(session, square, "Piper Quillow", "Sorrel Tack", danger_tier="harmless")
        session.commit()
    assert _pick() == "Market Square"


def test_a_known_place_beats_a_busier_unknown_one(db):
    """WorldForge marks the starting area discovered; an undiscovered place has
    no discovered roads or children — no exits, no lead route."""
    with get_session() as session:
        capital = _loc(session, "Grand Plaza", LocationType.DISTRICT)
        tavern = _loc(session, "The Rusty Cog", LocationType.BUILDING)
        tavern.discovered = True
        _npcs(session, capital, "Senator A", "Senator B", "Senator C", "Senator D")
        _npcs(session, tavern, "Barkeep")
        session.commit()
    assert _pick() == "The Rusty Cog"


def test_a_visited_place_counts_as_known(db):
    with get_session() as session:
        a = _loc(session, "Alpha Street", LocationType.DISTRICT)
        b = _loc(session, "Zulu Street", LocationType.DISTRICT)
        b.visited = True
        _npcs(session, a, "One", "Two")
        _npcs(session, b, "Three")
        session.commit()
    assert _pick() == "Zulu Street"


def test_a_known_lair_still_loses_to_an_unknown_safe_place(db):
    with get_session() as session:
        lair = _loc(session, "Wolf's Hollow", LocationType.POI)
        lair.discovered = True
        inn = _loc(session, "Quiet Inn", LocationType.BUILDING)
        _npcs(session, lair, "The Pack Mother", danger_tier="boss")
        _npcs(session, lair, "Scout A", "Scout B")
        _npcs(session, inn, "Innkeeper")
        session.commit()
    assert _pick() == "Quiet Inn"


def test_animals_are_not_people_to_talk_to(db):
    with get_session() as session:
        den = _loc(session, "Bramble Den", LocationType.POI)
        hut = _loc(session, "Hermit's Hut", LocationType.BUILDING)
        _npcs(session, den, "Hollow Wolf", "Gray-Flank Wolf", "Old Boar")
        _npcs(session, hut, "The Hermit")
        session.commit()
    assert _pick() == "Hermit's Hut"

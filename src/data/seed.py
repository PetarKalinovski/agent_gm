"""Seed data for testing the game."""

from src.models import (
    Connection,
    Faction,
    Location,
    LocationType,
    NPC,
    NPCTier,
    WorldClock,
    get_session,
    init_db,
)


def create_test_world(db_path: str = "data/game.db") -> None:
    """Create a small test world for development.

    Args:
        db_path: Path to the database file.
    """
    init_db(db_path)

    with get_session() as session:
        # Check if world already exists
        existing = session.query(Location).first()
        if existing:
            print("World already exists. Skipping seed.")
            return

        # Create faction
        merchants_guild = Faction(
            name="The Merchants Guild",
            ideology="Profit through fair trade and mutual benefit",
            methods=["trade agreements", "market control", "information brokering"],
            aesthetic="Rich fabrics, golden emblems, well-maintained establishments",
            power_level=65,
            resources={"economic": 80, "influence": 60, "military": 20},
            goals_short=["Expand trade routes to the eastern provinces"],
            goals_long=["Become the primary trading power in the realm"],
            leadership={"leader_name": "Aldric Goldweaver", "structure_type": "council"},
            secrets=["The guild is secretly funding both sides of the border conflict"],
        )
        session.add(merchants_guild)
        session.flush()  # Get the ID

        # Create locations
        town = Location(
            name="Millbrook",
            type=LocationType.TOWN,
            depth=0,
            description="A modest trading town at the crossroads of two major highways. "
                       "The cobblestone streets are well-worn from merchant carts, and the "
                       "air carries the mingled scents of fresh bread and horse manure.",
            atmosphere_tags=["busy", "mercantile", "safe"],
            economic_function="trade_hub",
            population_level="moderate",
            current_state="peaceful",
            controlling_faction_id=merchants_guild.id,
            genre_type="fantasy",
        )
        session.add(town)
        session.flush()

        town_square = Location(
            name="Town Square",
            type=LocationType.DISTRICT,
            parent_id=town.id,
            depth=1,
            position_x=50,
            position_y=50,
            description="The heart of Millbrook. A weathered stone fountain depicts a "
                       "merchant king from ages past. Market stalls line the edges, and "
                       "townfolk bustle about their daily business.",
            atmosphere_tags=["busy", "public", "central"],
            current_state="peaceful",
            controlling_faction_id=merchants_guild.id,
            visited=True,
            discovered=True,
        )
        session.add(town_square)
        session.flush()

        tavern = Location(
            name="The Gilded Flagon",
            type=LocationType.BUILDING,
            parent_id=town_square.id,
            depth=2,
            position_x=30,
            position_y=60,
            description="A well-kept tavern with a sign depicting a golden mug overflowing "
                       "with foam. Inside, the warmth of a large fireplace mingles with the "
                       "sounds of conversation and clinking glasses. The wooden floors are "
                       "worn smooth by countless patrons.",
            atmosphere_tags=["warm", "friendly", "social"],
            economic_function="entertainment",
            current_state="peaceful",
            discovered=True,
        )
        session.add(tavern)
        session.flush()

        market = Location(
            name="The Grand Market",
            type=LocationType.BUILDING,
            parent_id=town_square.id,
            depth=2,
            position_x=70,
            position_y=40,
            description="A large covered marketplace where merchants from across the region "
                       "sell their wares. Colorful awnings shade tables laden with goods, "
                       "from exotic spices to practical tools. The air is thick with the "
                       "calls of vendors and the haggling of customers.",
            atmosphere_tags=["busy", "commercial", "diverse"],
            economic_function="trade",
            current_state="peaceful",
            discovered=True,
        )
        session.add(market)
        session.flush()

        # Create connections
        square_to_tavern = Connection(
            from_location_id=town_square.id,
            to_location_id=tavern.id,
            travel_type="walk",
            travel_time_hours=0.1,
            bidirectional=True,
            discovered=True,
        )
        session.add(square_to_tavern)

        square_to_market = Connection(
            from_location_id=town_square.id,
            to_location_id=market.id,
            travel_type="walk",
            travel_time_hours=0.1,
            bidirectional=True,
            discovered=True,
        )
        session.add(square_to_market)

        # Create NPCs
        bartender = NPC(
            name="Mira Hearthstone",
            tier=NPCTier.MINOR,
            species="human",
            age=42,
            profession="Tavern Owner",
            faction_id=merchants_guild.id,
            current_location_id=tavern.id,
            home_location_id=tavern.id,
            description_physical="A sturdy woman with graying auburn hair tied back in a "
                                "practical bun. Her hands are calloused from years of work, "
                                "and her apron is perpetually stained with ale.",
            description_personality="Practical and no-nonsense, but with a warm heart for "
                                   "regulars. She has a keen ear for gossip and knows "
                                   "everyone's business in town.",
            voice_pattern="Direct and efficient. Uses short sentences. Occasionally "
                         "drops in local idioms. 'Well now, what'll it be?'",
            goals=["Keep the tavern profitable", "Find out who's been stealing from the cellar"],
            secrets=["She was once a scout for the king's army before settling down"],
            current_mood="neutral",
        )
        session.add(bartender)

        guild_master = NPC(
            name="Aldric Goldweaver",
            tier=NPCTier.MAJOR,
            species="human",
            age=58,
            profession="Guild Master",
            faction_id=merchants_guild.id,
            current_location_id=market.id,
            home_location_id=market.id,
            description_physical="A portly man with a meticulously groomed silver beard and "
                                "shrewd blue eyes. His clothes are of the finest quality, "
                                "adorned with subtle golden thread that catches the light.",
            description_personality="Calculating and politically savvy. He speaks in measured "
                                   "tones and rarely reveals his true thoughts. Despite his "
                                   "wealth, he remembers his humble origins.",
            voice_pattern="Formal and measured. Uses flowery language when flattering, "
                         "but can be cutting when crossed. 'Ah, a pleasure to make your "
                         "acquaintance. I do hope we can find... mutual benefit.'",
            goals=[
                "Expand the guild's influence to the eastern provinces",
                "Discover who is undermining guild trade routes",
                "Find a worthy successor",
            ],
            secrets=[
                "He secretly funds both sides of the border conflict to profit from war",
                "His son, whom he disowned, has joined a band of highway robbers",
                "He knows the location of an ancient treasure but lacks the means to retrieve it",
            ],
            current_mood="contemplative",
        )
        session.add(guild_master)

        town_guard = NPC(
            name="Captain Theron Ironheart",
            tier=NPCTier.MINOR,
            species="human",
            age=35,
            profession="Town Guard Captain",
            current_location_id=town_square.id,
            home_location_id=town_square.id,
            description_physical="A tall, broad-shouldered man with a prominent scar across "
                                "his left cheek. His armor is well-maintained, and he carries "
                                "himself with military precision.",
            description_personality="Honorable to a fault. He takes his duty seriously and "
                                   "has little patience for those who break the peace. "
                                   "Secretly struggling with past decisions made during the war.",
            voice_pattern="Clipped and military. 'State your business.' Softens only when "
                         "speaking of his daughter.",
            goals=["Maintain order in Millbrook", "Track down the bandits plaguing the roads"],
            secrets=["He let a war criminal escape years ago in exchange for information"],
            current_mood="vigilant",
        )
        session.add(town_guard)

        # Create world clock
        clock = WorldClock(day=1, hour=10)
        session.add(clock)

        session.commit()

        print("Test world created successfully!")
        print(f"- 1 faction: {merchants_guild.name}")
        print(f"- 4 locations: {town.name} (with {town_square.name}, {tavern.name}, {market.name})")
        print(f"- 3 NPCs: {bartender.name}, {guild_master.name}, {town_guard.name}")


def clear_world(db_path: str = "data/game.db") -> None:
    """Clear all world data (for testing).

    Args:
        db_path: Path to the database file.
    """
    from src.models import Base, get_engine

    init_db(db_path)
    engine = get_engine()

    # Drop all tables
    Base.metadata.drop_all(engine)
    # Recreate them
    Base.metadata.create_all(engine)

    print("World cleared.")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "clear":
        clear_world()
    else:
        create_test_world()

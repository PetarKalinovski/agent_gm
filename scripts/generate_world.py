#!/usr/bin/env python3
"""Script to generate a new game world using WorldForge.

Usage:
    python scripts/generate_world.py

Or with custom parameters:
    python scripts/generate_world.py --premise "Star Wars galaxy" --genre scifi --pc "Bounty hunter"
"""

import argparse
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.agents.base import setup_api_keys
from src.agents.world_forge import WorldForge
from src.models import init_db, get_session, WorldBible
from src.tools import world_read

# Import needed tools
get_all_factions = world_read.get_all_factions
get_all_locations = world_read.get_all_locations
get_all_npcs = world_read.get_all_npcs
get_historical_events = world_read.get_historical_events
get_world_bible = world_read.get_world_bible


def clear_existing_world(db_path: str) -> None:
    """Clear existing world data."""
    from src.models import Base, get_engine

    init_db(db_path)
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    print("Cleared existing world data.")


def print_summary() -> None:
    """Print summary of generated world."""
    print("\n" + "=" * 60)
    print("WORLD GENERATION COMPLETE")
    print("=" * 60)

    # World Bible
    bible = get_world_bible()
    if "error" not in bible:
        print(f"\n📖 World: {bible['name']}")
        print(f"   Genre: {bible['genre']}")
        print(f"   Tone: {bible['tone'][:100]}...")

    # Factions
    factions = get_all_factions()
    print(f"\n⚔️  Factions: {len(factions)}")
    for f in factions:
        print(f"   - {f['name']} (Power: {f['power_level']})")

    # Locations
    locations = get_all_locations()
    print(f"\n🗺️  Locations: {len(locations)}")
    root_locs = [l for l in locations if l['parent_id'] is None]
    for loc in root_locs[:5]:
        print(f"   - {loc['name']} ({loc['type']})")
    if len(locations) > 5:
        print(f"   ... and {len(locations) - 5} more")

    # NPCs
    npcs = get_all_npcs()
    major_npcs = [n for n in npcs if n['tier'] == 'major']
    minor_npcs = [n for n in npcs if n['tier'] == 'minor']
    print(f"\n👥 NPCs: {len(npcs)} total ({len(major_npcs)} major, {len(minor_npcs)} minor)")
    for npc in major_npcs[:5]:
        print(f"   - {npc['name']} ({npc['profession']})")
    if len(major_npcs) > 5:
        print(f"   ... and {len(major_npcs) - 5} more major NPCs")

    # Historical Events
    events = get_historical_events()
    print(f"\n📜 Historical Events: {len(events)}")
    for e in events[:3]:
        print(f"   - {e['name']} ({e['time_ago']})")

    print("\n" + "=" * 60)
    print("Run 'streamlit run scripts/world_viewer.py' to explore visually!")
    print("=" * 60)


def get_user_input():
    """Interactive prompts for world generation."""
    print("\n" + "=" * 60)
    print("WORLD GENERATION WIZARD")
    print("=" * 60)

    # Genre first (affects defaults)
    print("\n1. Select Genre:")
    print("   1. Fantasy")
    print("   2. Sci-Fi")
    print("   3. Modern")
    print("   4. Post-Apocalyptic")
    genre_choice = input("\nEnter number (default: 1): ").strip() or "1"
    genre_map = {"1": "fantasy", "2": "scifi", "3": "modern", "4": "post-apocalyptic"}
    genre = genre_map.get(genre_choice, "fantasy")

    # Premise
    print(f"\n2. World Premise ({genre}):")
    print("   Describe your world in 1-3 sentences.")

    default_premises = {
        "fantasy": "A dark fantasy world where an ancient evil is awakening. The old kingdoms are fractured and corrupt. Magic is feared and persecuted.",
        "scifi": "The Star Wars galaxy, 19 years after Order 66. The Empire rules with an iron fist. Jedi are hunted. The Rebellion is growing in secret.",
        "modern": "Modern day, but a secret supernatural underworld exists beneath society. Vampires, werewolves, and mages vie for power in the shadows.",
        "post-apocalyptic": "100 years after nuclear war. Scattered settlements struggle to survive. Ancient technology is both treasure and curse."
    }

    print(f"\n   Default: {default_premises[genre][:80]}...")
    premise = input("\nEnter premise (or press Enter for default): ").strip()
    if not premise:
        premise = default_premises[genre]

    # Player Character
    print("\n3. Player Character Concept:")
    print("   Who is the player character?")

    default_pcs = {
        "fantasy": "A wandering sellsword with a mysterious past, seeking redemption for past sins.",
        "scifi": "A former Jedi padawan hiding as a bounty hunter, haunted by survivor's guilt.",
        "modern": "A detective who recently discovered the supernatural world and is caught between sides.",
        "post-apocalyptic": "A scavenger from a small settlement, searching for a lost family member."
    }

    print(f"\n   Default: {default_pcs[genre]}")
    pc = input("\nEnter PC concept (or press Enter for default): ").strip()
    if not pc:
        pc = default_pcs[genre]

    # Numbers
    print("\n4. World Size:")
    factions = input("   Number of factions (default: 6): ").strip()
    factions = int(factions) if factions else 6

    major_npcs = input("   Number of major NPCs (default: 12): ").strip()
    major_npcs = int(major_npcs) if major_npcs else 12

    minor_npcs = input("   Number of minor NPCs (default: 40): ").strip()
    minor_npcs = int(minor_npcs) if minor_npcs else 40

    return {
        "genre": genre,
        "premise": premise,
        "pc": pc,
        "factions": factions,
        "major_npcs": major_npcs,
        "minor_npcs": minor_npcs,
    }


def main():
    parser = argparse.ArgumentParser(description="Generate a new game world")
    parser.add_argument(
        "--db",
        type=str,
        default="data/game.db",
        help="Database path"
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear existing world before generating"
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Use defaults without prompts"
    )

    args = parser.parse_args()

    # Setup
    setup_api_keys()

    if args.clear:
        clear_existing_world(args.db)
    else:
        init_db(args.db)

    # Check if world already exists
    with get_session() as session:
        existing = session.query(WorldBible).first()
        if existing:
            print(f"\n⚠️  World '{existing.name}' already exists!")
            print("Use --clear to start fresh, or run the viewer to explore.")
            return

    # Get parameters
    if args.quick:
        # Use defaults
        params = {
            "genre": "fantasy",
            "premise": "A dark fantasy world where an ancient evil is awakening. The old kingdoms are fractured and corrupt. Magic is feared and persecuted.",
            "pc": "A wandering sellsword with a mysterious past, seeking redemption for past sins.",
            "factions": 6,
            "major_npcs": 12,
            "minor_npcs": 40,
        }
        print("\n🚀 Quick mode: Using default fantasy world")
    else:
        # Interactive
        params = get_user_input()

    print(f"\n🌍 Generating world...")
    print(f"   Premise: {params['premise'][:80]}...")
    print(f"   Genre: {params['genre']}")
    print(f"   PC: {params['pc'][:60]}...")
    print(f"   Factions: {params['factions']}")
    print(f"   NPCs: {params['major_npcs']} major, {params['minor_npcs']} minor")
    print("\nThis may take a few minutes...\n")

    # Generate
    forge = WorldForge()
    result = forge.generate_world(
        premise=params['premise'],
        genre=params['genre'],
        pc_concept=params['pc'],
        num_factions=params['factions'],
        num_major_npcs=params['major_npcs'],
        num_minor_npcs=params['minor_npcs'],
    )

    print("\n--- Agent Output ---")
    print(str(result)[:2000])
    if len(str(result)) > 2000:
        print("... (truncated)")

    # Summary
    print_summary()


if __name__ == "__main__":
    main()

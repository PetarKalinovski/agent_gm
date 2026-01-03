"""Agent GM - Multi-agent text game dungeon master."""

import argparse
import sys


def main():
    """Main entry point for Agent GM."""
    parser = argparse.ArgumentParser(
        description="Agent GM - A multi-agent text adventure game"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="play",
        choices=["play", "seed", "clear"],
        help="Command to run: play (default), seed (create test world), clear (reset world)"
    )
    parser.add_argument(
        "--db",
        default="data/game.db",
        help="Path to database file (default: data/game.db)"
    )

    args = parser.parse_args()

    if args.command == "seed":
        from src.data.seed import create_test_world
        create_test_world(args.db)
    elif args.command == "clear":
        from src.data.seed import clear_world
        clear_world(args.db)
    elif args.command == "play":
        # Check if world exists
        from pathlib import Path
        if not Path(args.db).exists():
            print("No world found. Creating test world...")
            from src.data.seed import create_test_world
            create_test_world(args.db)
            print()

        # Check for API key
        import os
        if not os.environ.get("OPENROUTER_API_KEY"):
            print("Warning: OPENROUTER_API_KEY not set.")
            print("Set it with: export OPENROUTER_API_KEY=your_key")
            print()
            response = input("Continue anyway? (y/n): ")
            if response.lower() != "y":
                sys.exit(1)

        # Run the game
        from src.game.session import run_game
        run_game()


if __name__ == "__main__":
    main()

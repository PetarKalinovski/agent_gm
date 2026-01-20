"""Forge - Multi-agent text game dungeon master."""

import argparse
import sys
from dotenv import load_dotenv
from src.config import load_settings

load_dotenv()

def main():
    """Main entry point for Forge."""
    parser = argparse.ArgumentParser(
        description="Forge - A multi-agent text adventure game"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="play",
        choices=["play", "seed", "clear", "web"],
        help="Command to run: play (default), seed (create test world), clear (reset world), web (start web frontend)"
    )
    parser.add_argument(
        "--db",
        default=f"{load_settings().database.path}",
        help="Path to database file (default: data/game.db)"
    )
    parser.add_argument(
        "--new_player",
        default =False
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
        from src.game import session
        if args.new_player:
            session.create_player()
        else:
            session.run_game()
    elif args.command == "web":
        # Run the web frontend
        from src.web import run_server
        print("Starting Forge Web Frontend...")
        print("Open http://localhost:8000 in your browser")
        run_server(host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()

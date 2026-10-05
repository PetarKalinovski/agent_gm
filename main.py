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
        choices=["play", "seed", "playtest", "clear", "web", "memory-export", "memory-import"],
        help="Command to run: play (default), seed (create test world), playtest (create Emberfall playtest world), clear (reset world), web (start web frontend), "
             "memory-export (write the world's NPC memories, journals and chronicles out as markdown files), "
             "memory-import (read those files back into the world, replacing what it holds)"
    )
    parser.add_argument(
        "--dir",
        default=None,
        help="Folder for memory-export/memory-import (default: data/memory/<world>)"
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
    elif args.command == "playtest":
        from src.data.playtest_seed import PLAYTEST_DB_PATH, create_playtest_world
        # Unless the user pointed --db somewhere explicitly, the playtest world
        # gets its own file so it never clobbers a real world.
        db = args.db if args.db != load_settings().database.path else PLAYTEST_DB_PATH
        create_playtest_world(db)
    elif args.command == "clear":
        from src.data.seed import clear_world
        clear_world(args.db)
    elif args.command in ("memory-export", "memory-import"):
        # Memory lives inside the world's .db; these files are a copy to read or fix
        from pathlib import Path
        if not Path(args.db).exists():
            print(f"No world at {args.db}")
            sys.exit(1)
        from src.config import set_runtime_db_path
        from src.models import init_db
        from src.services import memory_files
        set_runtime_db_path(args.db)
        init_db(args.db)
        folder = Path(args.dir) if args.dir else memory_files.memory_root()
        if args.command == "memory-export":
            count = memory_files.export_memory(folder)
            print(f"Wrote {count} memory file(s) to {folder}")
        else:
            count = memory_files.import_memory(folder)
            print(f"Stored {count} memory document(s) from {folder} in {args.db}")
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
        print("Open http://localhost:12000 in your browser")
        run_server(host="0.0.0.0", port=12000)


if __name__ == "__main__":
    main()

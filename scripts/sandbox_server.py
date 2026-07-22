"""Run an isolated Forge server for AI self-playtesting.

Boots the full web server on a separate port with its own database, so an
agent can play real DM turns through the same API the browser uses without
touching the human player's world or session.

Usage:
    uv run python scripts/sandbox_server.py [--db data/claude_sandbox.db] [--port 12100] [--seed]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/claude_sandbox.db")
    parser.add_argument("--port", type=int, default=12100)
    parser.add_argument("--seed", action="store_true",
                        help="Seed the Emberfall playtest world if the db is new")
    args = parser.parse_args()

    if args.seed and not Path(args.db).exists():
        from src.data.playtest_seed import create_playtest_world
        create_playtest_world(args.db)

    from src.config import set_runtime_db_path
    set_runtime_db_path(args.db)

    from src.web import run_server
    print(f"Sandbox server: http://127.0.0.1:{args.port} (db={args.db})")
    run_server(host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()

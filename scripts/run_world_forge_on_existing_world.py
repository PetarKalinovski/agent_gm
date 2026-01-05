from pathlib import Path
import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.agents.world_forge import WorldForge
from src.models import init_db, get_session, WorldBible


def main() -> None:
    """Main function to run the script."""
    db_path = "data/sw.db"  # Path to your existing world database
    init_db(db_path)
    session = get_session()
    world_forge = WorldForge()
    world_bible = session.query(WorldBible).first()

    if not world_bible:
        print("No existing world found in the database.")
        return

    print(f"Loaded existing world: {world_bible.name}")


    while True:
        query = input("\nEnter your world query (or 'exit' to quit): ")
        if query.lower() == 'exit':
            break

        world_forge.agent(query)

if __name__ == "__main__":
    main()
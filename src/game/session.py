"""Game session management and main loop."""

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from src.agents.base import setup_api_keys
from src.agents.dm_orchestrator import DMOrchestrator
from src.config import load_settings
from src.game.context import assemble_context
from src.models import (
    Location,
    NPC,
    Player,
    WorldClock,
    get_session,
    init_db,
)
from src.tools.narration import set_console


class GameSession:
    """Manages a game session."""

    def __init__(self, player_id: str | None = None, starting_location_id: str | None = None) -> None:
        """Initialize a game session.

        Args:
            player_id: Optional player ID to resume. If None, creates new player.
        """
        self.settings = load_settings()
        self.console = Console()
        set_console(self.console)

        # Initialize API keys
        setup_api_keys()

        # Initialize database
        init_db(self.settings.database.path)

        # Get or create player
        self.player_id = self.create_player() if player_id == "new" else self._get_or_create_player()

        self.starting_location_id = starting_location_id

        # Initialize DM
        self.dm = DMOrchestrator(self.player_id)
    def create_player(self) -> str:
        """Create a new player character.

        Returns:
            The new player's ID.
        """
        with get_session() as session:
            self.console.print(Panel(
                "Welcome, adventurer! Let's create your new character.",
                title="New Game",
                border_style="green"
            ))

            name = Prompt.ask("What is your name?", default="Adventurer")
            description = Prompt.ask("Describe yourself briefly", default="A mysterious traveler")
            traits = Prompt.ask("List some traits (comma separated)", default="curious, brave")

            player = Player(
                name=name,
                description=description,
                traits=[trait.strip() for trait in traits.split(",") if trait.strip()]
            )
            session.add(player)

            # Set starting location (first location in DB)
            starting_location = session.query(Location).first()
            if starting_location:
                player.current_location_id = starting_location.id
                starting_location.visited = True
                starting_location.discovered = True

            session.commit()

            return player.id

    def _get_or_create_player(self) -> str:
        """Get existing player or create a new one.

        Returns:
            The player's ID.
        """
        with get_session() as session:
            # Check for existing player
            players = session.query(Player).all()

            if len(players) > 0:
                self.console.print(Panel(
                    "Welcome back, adventurer! Select which character to continue.",
                    title="Resume Game",
                    border_style="green"
                ))
                for idx, p in enumerate(players, start=1):
                    self.console.print(f"[{idx}] {p.name} - {p.description}")
                choice = Prompt.ask("Enter the number of your character", choices=[str(i) for i in range(1, len(players)+1)], default="1")
                player = players[int(choice)-1]

            elif len(players) == 1:
                player = players[0]

            else:
                player = None
            if player:
                return player.id

            # Create new player
            self.console.print(Panel(
                "Welcome, adventurer! Let's create your character.",
                title="New Game",
                border_style="green"
            ))

            name = Prompt.ask("What is your name?", default="Adventurer")

            player = Player(
                name=name,
                description="A mysterious traveler",
                traits=["curious", "brave"],
            )
            session.add(player)

            # Set starting location (first location in DB)
            starting_location = session.query(Location).first()
            if starting_location:
                player.current_location_id = starting_location.id
                starting_location.visited = True
                starting_location.discovered = True

            session.commit()

            return player.id

    def _ensure_world_clock(self) -> None:
        """Ensure the world clock exists."""
        with get_session() as session:
            clock = session.query(WorldClock).first()
            if not clock:
                clock = WorldClock(day=1, hour=8)
                session.add(clock)
                session.commit()

    def start(self) -> None:
        """Start the game session."""
        self._ensure_world_clock()

        # Show title
        self.console.print()
        self.console.print(Panel(
            Text("AGENT GM", style="bold magenta", justify="center"),
            subtitle="A Multi-Agent Text Adventure",
            border_style="magenta"
        ))
        self.console.print()

        # Get player name
        with get_session() as session:
            player = session.get(Player, self.player_id)
            player_name = player.name if player else "Adventurer"

        self.console.print(f"Welcome, [bold]{player_name}[/bold]!")
        self.console.print()

        # Show initial scene
        self.dm.describe_scene()
        self.console.print()

        # Main game loop
        self.run()

    def run(self) -> None:
        """Run the main game loop."""
        while True:
            try:
                # Get player input
                player_input = Prompt.ask("\n[bold green]>[/bold green]")

                # Handle special commands
                if player_input.lower() in ["quit", "exit", "q"]:
                    self.console.print("\nFarewell, adventurer!")
                    break
                elif player_input.lower() == "help":
                    self._show_help()
                    continue
                elif player_input.lower() == "status":
                    self._show_status()
                    continue
                elif player_input.lower() in ["look", "l"]:
                    self.dm.describe_scene()
                    continue

                # Process input through DM
                response = self.dm.process_input(player_input)
                # Response is already printed by tools (narrate, describe_location, etc.)

            except KeyboardInterrupt:
                self.console.print("\n\nFarewell, adventurer!")
                break
            except Exception as e:
                self.console.print(f"\n[red]An error occurred: {e}[/red]")
                if self.settings.database.echo:
                    import traceback
                    traceback.print_exc()

    def _show_help(self) -> None:
        """Show help information."""
        help_text = """
**Commands:**
- `look` or `l` - Look around
- `status` - Show your status
- `help` - Show this help
- `quit` or `exit` - Leave the game

**Gameplay:**
- Type what you want to do in natural language
- e.g., "go to the tavern", "talk to the bartender", "ask about rumors"
- The DM will respond to your actions and narrate the world
"""
        self.console.print(Panel(help_text, title="Help", border_style="blue"))

    def _show_status(self) -> None:
        """Show player status."""
        context = assemble_context(self.player_id)
        player = context.get("player", {})
        clock = context.get("clock", {})

        status_text = f"""
**{player.get('name', 'Unknown')}**
Health: {player.get('health_status', 'healthy')}
Traits: {', '.join(player.get('traits', []))}

**Time:** Day {clock.get('day', 1)}, {clock.get('hour', 8)}:00 ({clock.get('time_of_day', 'day')})

**Inventory:** {', '.join(player.get('inventory', [])) or 'Empty'}

**Active Quests:** {len(player.get('active_quests', []))}
"""
        self.console.print(Panel(status_text, title="Status", border_style="cyan"))


def run_game(player_id: str | None = None) -> None:
    """Run the game.

    Args:
        player_id: Optional player ID to resume.
    """
    session = GameSession(player_id)
    session.start()

def create_player() -> str:
    """Create a new player character.

    Returns:
        The new player's ID.
    """
    session = GameSession(player_id="new")
    session.start()
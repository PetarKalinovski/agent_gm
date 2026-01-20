"""Creator Agent - World creation and management sub-agent."""

from typing import Any, Callable

from src.agents.core.base_agent import BaseGameAgent
from src.core.types import AgentContext
from src.tools.world_read import (
    get_current_location,
    get_npcs_at_location,
    get_available_destinations,
    get_npc,
    get_npc_relationship,
    get_world_clock,
    get_player,
)
from src.tools.world_write import (
    add_location,
    add_npc,
    move_npc,
    update_npc_relationship,
    update_npc,
    create_event,
    create_item_template,
)


CREATOR_SYSTEM_PROMPT = """You are a world creator and manager for a text-based RPG. You are a sub-agent focused on building and maintaining the game world. When the DM Orchestrator needs to create or update locations, NPCs, or world events, it will delegate those tasks to you.

Your responsibilities include:
1. Creating new locations with detailed descriptions, types, and connections to other locations.
2. Adding NPCs to the world with rich backstories, personalities, and roles within the game.
3. Moving NPCs between locations as part of world simulation or story events.
4. Updating existing NPCs when they evolve - changing their goals, physical appearance, or gaining new secrets.
5. Managing relationships between NPCs and the player, including trust levels and key moments.
6. Creating world events that can impact the game environment and NPC behaviors.
7. Defining item templates for the game world (weapons, armor, quest items, consumables).

When you receive a request, use the appropriate tools to perform the task and return the results to the DM Orchestrator.
"""


# Collect Creator tools
CREATOR_TOOLS: list[Callable] = [
    get_current_location,
    get_npcs_at_location,
    get_available_destinations,
    get_npc,
    get_npc_relationship,
    get_world_clock,
    get_player,
    add_location,
    add_npc,
    move_npc,
    update_npc_relationship,
    update_npc,
    create_event,
    create_item_template,
]


class CREATORAgent(BaseGameAgent):
    """A sub-agent for world creation and management.

    Inherits from BaseGameAgent for standardized initialization:
    - Automatic FileSessionManager setup
    - Automatic SemanticSummarizingConversationManager
    - Automatic SemanticMemoryHook
    """

    AGENT_NAME = "creator_agent"
    DEFAULT_TOOLS = CREATOR_TOOLS

    def __init__(self, player_id: str, callback_handler: Any = None):
        """Initialize the agent.

        Args:
            player_id: The player's ID in the database.
            callback_handler: Optional callback handler for tool tracking.
        """
        context = AgentContext(
            player_id=player_id,
            session_id=f"{player_id}_creator",
            callback_handler=callback_handler,
        )

        super().__init__(context)

        # Store for backward compatibility
        self.player_id = player_id

    def _get_session_id(self) -> str:
        """Creator agent uses player_id + 'creator' for session."""
        return f"{self.context.player_id}_creator"

    def _build_system_prompt(self) -> str:
        """Build the creator system prompt."""
        return CREATOR_SYSTEM_PROMPT + f"\n\nThe current player_id is: {self.context.player_id}"

    def _build_context(self, player_input: str) -> str:
        """Build context with current location and time."""
        location = get_current_location(self.context.player_id)
        clock = get_world_clock()

        npc_names = ', '.join(n['name'] for n in location.get('npcs_present', [])) or 'None'

        return f"""Current context:
- Location: {location.get('name', 'Unknown')} ({location.get('type', 'unknown')})
- Time: Day {clock.get('day', 1)}, {clock.get('hour', 8)}:00 ({clock.get('time_of_day', 'day')})
- NPCs here: {npc_names}

Request: {player_input}"""

    def process_input(self, player_input: str) -> str:
        """Process a creation request.

        Args:
            player_input: The creation instruction text.

        Returns:
            Agent response as string.
        """
        return self.process(player_input)

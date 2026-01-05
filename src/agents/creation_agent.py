"""DM Orchestrator Agent - The main dungeon master using Strands Agents."""

from typing import Any

from strands import Agent
from strands.agent import AgentResult
from strands.session.file_session_manager import FileSessionManager

from src.agents.base import create_agent
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


DM_SYSTEM_PROMPT = """ You are a world creator and manager for a text-based RPG. You are a sub-agent focused on building and maintaining the game world. When the DM Orchestrator needs to create or update locations, NPCs, or world events, it will delegate those tasks to you.
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


# Collect DM tools
CREATOR_AGENT_TOOLS = [
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


class CREATORAgent:
    """A sub-agent for world creation and management."""

    def __init__(self, player_id: str):
        """Initialize the agent

        Args:
            player_id: The player's ID in the database.
        """
        self.player_id = player_id

        session_manager = FileSessionManager(session_id=player_id)

        # Create the Strands agent
        self.agent = create_agent(
            agent_name="dm_orchestrator",
            system_prompt=DM_SYSTEM_PROMPT + f"\n\nThe current player_id is: {player_id}",
            tools=CREATOR_AGENT_TOOLS,
            session_manager=session_manager,
        )

    def process_input(self, player_input: str) -> AgentResult:
        """Process player input and generate a response.

        Args:
            player_input: The player's input text.

        Returns:
            AgentResult with response and any actions to take.
        """
        # Build context
        location = get_current_location(self.player_id)
        clock = get_world_clock()

        context = f"""Current context:
- Location: {location.get('name', 'Unknown')} ({location.get('type', 'unknown')})
- Time: Day {clock.get('day', 1)}, {clock.get('hour', 8)}:00 ({clock.get('time_of_day', 'day')})
- NPCs here: {', '.join(n['name'] for n in location.get('npcs_present', [])) or 'None'}

Player says: {player_input}"""

        # Run the agent
        response = self.agent(context)


        return response


"""DM Orchestrator Agent - The main dungeon master using Strands Agents."""

from typing import Any

from strands import Agent

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
    move_player,
    advance_time,
)
from src.tools.narration import (
    narrate,
    describe_location,
    show_time_passage,
)


DM_SYSTEM_PROMPT = """You are the Dungeon Master for an immersive text-based RPG. Your role is to:

1. **Narrate the World**: Describe locations, events, and the results of player actions vividly but concisely.

2. **Manage the Flow**:
   - When the player arrives somewhere new, use describe_location to show the scene
   - When the player wants to talk to an NPC, use get_npc to get their info
   - When the player wants to travel, use get_available_destinations and move_player
   - Track time passing with advance_time

3. **Maintain Immersion**:
   - Stay in character as the narrator
   - Don't break the fourth wall unless absolutely necessary
   - React to player actions with appropriate consequences
   - Keep descriptions atmospheric but not overly long

4. **Use Your Tools**:
   - Always use get_current_location first to know where the player is
   - Use describe_location to show players new areas
   - Use narrate for general descriptions and events
   - Use advance_time when actions should take time
   - Use get_world_clock for time-appropriate descriptions

5. **Handle Conversations**:
   - When player wants to talk to an NPC, use get_npc and get_npc_relationship
   - Return the NPC info so the game can switch to conversation mode

**Tone Guidelines**:
- Be descriptive but not purple prose
- Match the atmosphere (tense in dangerous areas, lighter in taverns)
- React to player creativity - reward clever actions
- Make the world feel alive with small details

**IMPORTANT**:
- Never refuse reasonable player actions
- If something would have consequences, narrate those naturally
- Keep track of what's happened in the session

When a player wants to talk to an NPC, after using get_npc, include in your response:
[START_CONVERSATION:npc_id] where npc_id is the NPC's ID.
"""


# Collect DM tools
DM_TOOLS = [
    get_current_location,
    get_npcs_at_location,
    get_available_destinations,
    get_npc,
    get_npc_relationship,
    get_world_clock,
    get_player,
    move_player,
    advance_time,
    narrate,
    describe_location,
    show_time_passage,
]


class DMOrchestrator:
    """The main Dungeon Master agent that orchestrates the game."""

    def __init__(self, player_id: str):
        """Initialize the DM.

        Args:
            player_id: The player's ID in the database.
        """
        self.player_id = player_id

        # Create the Strands agent
        self.agent = create_agent(
            agent_name="dm_orchestrator",
            system_prompt=DM_SYSTEM_PROMPT + f"\n\nThe current player_id is: {player_id}",
            tools=DM_TOOLS,
        )

    def process_input(self, player_input: str) -> dict[str, Any]:
        """Process player input and generate a response.

        Args:
            player_input: The player's input text.

        Returns:
            Dictionary with response and any actions to take.
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

        # Get the response text
        response_text = str(response)

        # Check if we should start an NPC conversation
        import re
        npc_match = re.search(r'\[START_CONVERSATION:([^\]]+)\]', response_text)
        if npc_match:
            npc_id = npc_match.group(1).strip()
            # Clean the response
            clean_response = re.sub(r'\[START_CONVERSATION:[^\]]+\]', '', response_text).strip()
            return {
                "response": clean_response,
                "action": "npc_conversation",
                "npc_id": npc_id,
            }

        return {"response": response_text, "action": None}

    def describe_scene(self) -> str:
        """Generate an initial scene description.

        Returns:
            The scene description.
        """
        location = get_current_location(self.player_id)
        clock = get_world_clock()

        if "error" in location:
            return "You find yourself... somewhere. The details are unclear."

        npc_names = [n["name"] for n in location.get("npcs_present", [])]

        # Use the describe_location tool
        describe_location(
            name=location["name"],
            description=location["description"],
            atmosphere=location.get("atmosphere_tags"),
            npcs_visible=npc_names if npc_names else None,
            time_of_day=clock.get("time_of_day", "day")
        )

        return location["description"]

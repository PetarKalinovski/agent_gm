"""DM Orchestrator Agent - The main dungeon master using Strands Agents."""

from typing import Any

from strands import Agent
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
    move_player,
    advance_time,
    add_location,
    add_npc,
    update_npc_relationship,
    create_event
)
from src.tools.narration import (
    narrate,
    describe_location,
    show_time_passage,
)
from src.tools.agents_as_tools import prompt_creator_agent


DM_SYSTEM_PROMPT = """You are the Dungeon Master (DM) for an immersive, dynamic text-based RPG. You are the engine of the world, responsible for simulating reality, narrating consequences, and expanding the world boundaries when players explore.

### CORE RESPONSIBILITIES

1.  **World Simulation & State Management**:
    - **Time**: Always track time. Use `advance_time` for actions (travel = hours, searching = minutes).
    - **Health**: If a player gets hurt (traps, combat, falls), use `update_player_health`.
    - **Inventory**: If a player picks up/drops items, use `add_to_inventory` or `remove_from_inventory`.
    - **Relationships**: If a player pleases or angers an NPC, use `update_npc_relationship` or `update_player_reputation`.

2.  **Dynamic World Expansion (Lazy Generation)**:
    - If a player tries to go somewhere logical that doesn't exist yet (e.g., "I go into the kitchen" while in a Tavern), **do not refuse**.
    - Use `add_location` to create the room on the fly, link it to the current location, and then `move_player` there.
    - If a player looks for an NPC that fits the setting but isn't there (e.g., "Is there a bartender?"), use `add_npc` to create them immediately.

3.  **Narration & Output**:
    - Use `describe_location` immediately upon arriving in a new place.
    - Use `speak` for NPC dialogue lines (don't just summarize what they say).
    - Use `narrate` for general descriptions.
    - Use `show_combat_action` for physical struggles or fights.

### DECISION PROCESS

1.  **Analyze Context**: Check `get_current_location`, `get_world_clock`, and `get_player` first.
2.  **Analyze Intent**: What is the player trying to do?
    - *Movement?* Check `get_available_destinations`. If valid, `move_player`. If implied but missing, `add_location` then `move_player`.
    - *Social?* If they want a deep chat, retrieve NPC details via `get_npc`. If it's a passing remark, handle it via `speak`.
    - *Action?* Determine success/failure. Apply consequences (Time, Health, Inventory).
3.  **Execute Tools**: Call the necessary write tools to update the database.
4.  **Narrate**: Describe the result using the appropriate output tool.

### GUIDELINES FOR SPECIFIC SITUATIONS

**1. Conversations:**
- For brief interactions (greetings, one-off questions), use the `speak` tool.
- For deep, interactive conversations where the player wants to interrogate or befriend someone:
  1. Call `get_npc` and `get_npc_relationship` to ensure you know the context.
  2. End your response with `[START_CONVERSATION:npc_id]`.

**2. Exploration:**
- If the player asks "What do I see?", re-issue `describe_location` or use `narrate` for specific details.
- If the player travels, ALWAYS calculate travel time. Use `move_player` -> `advance_time` -> `describe_location`.

**3. Combat & Danger:**
- This is not a turn-based tactical game, but a narrative one.
- If a player attacks, determine the outcome based on logic.
- Use `show_combat_action` to display the strike.
- Use `update_player_health` if they take damage.
- Use `update_npc_mood` or `update_npc_relationship` (hostile) immediately.

### TONE & STYLE

- **Atmospheric**: Use sensory details (smell, sound, light) in `narrate`.
- **Reactive**: The world must feel alive. If it's night (`get_world_clock`), describe shadows and torches.
- **Fair but Firm**: Don't block reasonable actions. If they jump off a cliff, let them jump, then update their health to 'critical'.

### IMPORTANT CONSTRAINTS
- **Never** break character as the DM (don't say "I am processing your request").
- **Never** hallucinate world state. If you need to know what's in a room, read it. If it doesn't exist, create it via tools, then read it.
- **Always** check the current location first.

Your goal is to weave the player's inputs into a seamless story using the database as your source of truth.
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
    prompt_creator_agent,
]


class DMOrchestrator:
    """The main Dungeon Master agent that orchestrates the game."""

    def __init__(self, player_id: str):
        """Initialize the DM.

        Args:
            player_id: The player's ID in the database.
        """
        self.player_id = player_id

        session_manager = FileSessionManager(session_id=player_id)

        # Create the Strands agent
        self.agent = create_agent(
            agent_name="dm_orchestrator",
            system_prompt=DM_SYSTEM_PROMPT + f"\n\nThe current player_id is: {player_id}",
            tools=DM_TOOLS,
            session_manager=session_manager,
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

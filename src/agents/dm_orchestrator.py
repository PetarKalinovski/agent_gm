"""DM Orchestrator Agent - The main dungeon master using Strands Agents."""

from typing import Any, Callable

from strands_tools import journal

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
    get_world_state_summary,
    get_active_quests,
    get_world_bible_for_dm,
    get_all_quests,
)
from src.tools.world_write import (
    move_player,
    move_npc,
    advance_time,
    add_location,
    add_npc,
    kill_npc,
    update_npc_relationship,
    update_npc,
    create_event,
    update_quest_status,
    update_quest_objectives,
)
from src.tools.narration import (
    narrate,
    describe_location,
    show_time_passage,
    show_quest_update,
)
from src.tools.agents_as_tools import prompt_creator_agent, prompt_npc_agent, prompt_economy_agent


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
    - NPCs can evolve: use `update_npc` when events change them physically (injuries, aging), their goals shift, or they learn new secrets.

3.  **Narration & Output**:
    - Use `describe_location` immediately upon arriving in a new place.
    - Use `prompt_npc_agent` for named NPC dialogue (bartender, quest giver, etc.).
    - Use `narrate` for general descriptions and unnamed NPC dialogue (guards, crowd reactions).
    - Use `show_combat_action` for physical struggles or fights.

### DECISION PROCESS

1.  **Analyze Context**: Check `get_current_location`, `get_world_clock`, and `get_player` first.
2.  **Analyze Intent**: What is the player trying to do?
    - *Movement?* Check `get_available_destinations`. If valid, `move_player`. If implied but missing, `add_location` then `move_player`.
    - *Social?* Use `prompt_npc_agent` for named NPC interactions.
    - *Economic?* Use `prompt_economy_agent` for buying, selling, using items, or checking inventory.
    - *World Creation?* Use `prompt_creator_agent` for creating new locations, NPCs, events, or items.
    - *Action?* Determine success/failure. Apply consequences (Time, Health).
3.  **Execute Tools**: Call the necessary tools or delegate to sub-agents.
4.  **Narrate**: Describe the result using the appropriate output tool.

### GUIDELINES FOR SPECIFIC SITUATIONS

**1. NPC Interactions:**
- For **named NPCs** (characters with personalities, backstories, or ongoing relationships):
  - Use `prompt_npc_agent(player_id, npc_id, player_input, is_first_interaction, context)`
  - Set `is_first_interaction=True` the first time the player talks to this NPC in the current session
  - **IMPORTANT**: Always pass `context` to sync the NPC with the current narrative situation:
    - Include what you just narrated (events, atmosphere, things the NPC would see/know)
    - Include relevant quest context if the NPC is involved in the player's active quests
    - Include any world events or environmental details the NPC should react to
  - Example: Player asks about the holocron you just described → Call `prompt_npc_agent(player_id, npc_id, "What is this?", False, "A glowing holocron has just appeared on the table. The NPC witnessed it materialize.")`
  - The NPC agent handles personality, memory, and relationship dynamics
  - You remain in control - narrate around the NPC's response, inject world events, etc.
- For **unnamed/ambient NPCs** (guards, shoppers, background characters):
  - Use `narrate` to include their brief dialogue as part of the scene
  - Example: `narrate("A guard calls out: 'Halt! State your business!'")`

**2. Exploration:**
- If the player asks "What do I see?", re-issue `describe_location` or use `narrate` for specific details.
- If the player travels, ALWAYS calculate travel time. Use `move_player` -> `advance_time` -> `describe_location`.
- If NPCs are traveling with the player (party members, companions), use `move_npc` to move them to the same destination.
- NPCs can also move independently for world simulation purposes.

**3. Combat & Danger:**
- This is not a turn-based tactical game, but a narrative one.
- If a player attacks, determine the outcome based on logic.
- Use `show_combat_action` to display the strike.
- Use `update_player_health` if they take damage.
- Use `kill_npc` when an NPC dies (combat, assassination, accident, etc.) - this triggers a death animation.
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

### JOURNAL USAGE
Use the `journal` tool to log important narrative developments, world changes, and player progress. Track significant events, NPC relationship shifts, new locations discovered, and major plot developments. Keep journal entries concise but meaningful for future reference and continuity.

### QUEST TRACKING
- When NPCs offer tasks or the player takes on objectives, create quests.
- Track quest progress naturally through play - update objectives as they're completed.
- Remind the player of relevant active quests when appropriate (e.g., when they encounter a quest-related NPC or location).
- Don't spam quest updates - weave them into narration.
"""


# Collect DM tools
DM_TOOLS: list[Callable] = [
    # Read tools
    get_current_location,
    get_npcs_at_location,
    get_available_destinations,
    get_npc,
    get_npc_relationship,
    get_world_clock,
    get_player,
    get_active_quests,
    get_world_state_summary,
    get_all_quests,
    # Write tools
    move_player,
    move_npc,
    kill_npc,
    advance_time,
    update_quest_status,
    update_quest_objectives,
    # Narration tools
    narrate,
    describe_location,
    show_time_passage,
    show_quest_update,
    # Sub-agent delegation
    prompt_creator_agent,
    prompt_npc_agent,
    prompt_economy_agent,
    # Journal tool
    journal,
]


class DMOrchestrator(BaseGameAgent):
    """The main Dungeon Master agent that orchestrates the game.

    Inherits from BaseGameAgent for standardized initialization:
    - Automatic FileSessionManager setup
    - Automatic SemanticSummarizingConversationManager
    - Automatic SemanticMemoryHook
    - Callback handler propagation
    """

    AGENT_NAME = "dm_orchestrator"
    DEFAULT_TOOLS = DM_TOOLS

    def __init__(self, context_or_player_id: AgentContext | str, callback_handler: Any = None):
        """Initialize the DM.

        Args:
            context_or_player_id: Either an AgentContext or player_id string.
                                  String is supported for backward compatibility.
            callback_handler: Optional callback handler for tool tracking.
                             Only used if context_or_player_id is a string.
        """
        # Support both new AgentContext and old player_id string for backward compatibility
        if isinstance(context_or_player_id, str):
            context = AgentContext(
                player_id=context_or_player_id,
                session_id=context_or_player_id,
                callback_handler=callback_handler,
            )
        else:
            context = context_or_player_id

        # Load world context before calling super().__init__
        self._world_context = self._load_world_context()
        self._player_name = self._load_player_name(context.player_id)

        super().__init__(context)

        # Store for backward compatibility
        self.player_id = context.player_id
        self.callback_handler = context.callback_handler

    def _load_world_context(self) -> str:
        """Load world context for the system prompt."""
        world_context = get_world_bible_for_dm()
        if not world_context or "No World Bible" in world_context:
            return ""
        return f"\n\n### WORLD CONTEXT\n{world_context}"

    def _load_player_name(self, player_id: str) -> str:
        """Load player name for the system prompt."""
        player_data = get_player(player_id)
        return player_data.get('name', 'Unknown') if player_data else 'Unknown'

    def _get_session_id(self) -> str:
        """DM uses player_id as session ID (maintains history across sessions)."""
        return self.context.player_id

    def _build_system_prompt(self) -> str:
        """Build the DM system prompt with world context."""
        prompt = DM_SYSTEM_PROMPT
        prompt += self._world_context
        prompt += f"\n\nThe current player_id is: {self.context.player_id}, with name: {self._player_name}."
        return prompt

    def _build_context(self, player_input: str) -> str:
        """Build rich context for DM processing."""
        location = get_current_location(self.context.player_id)
        clock = get_world_clock()

        npc_names = ', '.join(n['name'] for n in location.get('npcs_present', [])) or 'None'

        return f"""Current context:
- Location: {location.get('name', 'Unknown')} ({location.get('type', 'unknown')})
- Time: Day {clock.get('day', 1)}, {clock.get('hour', 8)}:00 ({clock.get('time_of_day', 'day')})
- NPCs here: {npc_names}

Player says: {player_input}"""

    def process_input(self, player_input: str) -> str:
        """Process player input and generate a response.

        Args:
            player_input: The player's input text.

        Returns:
            The DM's response text.
        """
        return self.process(player_input)

    def get_conv_state(self) -> dict[str, Any]:
        """Get the current conversation state.

        Returns:
            The conversation state as a dictionary.
        """
        if self.agent.conversation_manager:
            return self.agent.conversation_manager.get_state()
        return {}

    def describe_scene(self) -> str:
        """Generate an initial scene description.

        Returns:
            The scene description.
        """
        location = get_current_location(self.context.player_id)
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

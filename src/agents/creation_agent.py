"""Creator Agent - On-demand world creation and management during gameplay.

This agent handles lazy loading of world content:
- Generating new locations when players explore
- Creating NPCs on-demand
- Expanding the world as needed during play
- Managing factions, quests, and world state

Uses the same tools as WorldForge but operates reactively during gameplay
rather than batch-generating an entire world upfront.
"""

from typing import Any, Callable

from src.agents.core.base_agent import BaseGameAgent
from src.core.types import AgentContext
from src.tools import world_read
from src.tools import world_write

# Import all needed tools (same as WorldForge)
get_all_factions = world_read.get_all_factions
get_all_locations = world_read.get_all_locations
get_all_npcs = world_read.get_all_npcs
get_faction_relationships = world_read.get_faction_relationships
get_historical_events = world_read.get_historical_events
get_world_bible = world_read.get_world_bible
get_world_bible_for_generation = world_read.get_world_bible_for_generation
get_current_location = world_read.get_current_location
get_npcs_at_location = world_read.get_npcs_at_location
get_available_destinations = world_read.get_available_destinations
get_npc = world_read.get_npc
get_npc_relationship = world_read.get_npc_relationship
get_world_clock = world_read.get_world_clock
get_player = world_read.get_player

add_location = world_write.add_location
add_npc = world_write.add_npc
create_faction = world_write.create_faction
create_faction_relationship = world_write.create_faction_relationship
create_historical_event = world_write.create_historical_event
create_world_bible = world_write.create_world_bible
update_npc = world_write.update_npc
add_location_connection = world_write.add_location_connection
update_location = world_write.update_location
delete_location = world_write.delete_location
move_npc = world_write.move_npc
create_quest = world_write.create_quest
update_npc_relationship = world_write.update_npc_relationship
create_event = world_write.create_event
create_item_template = world_write.create_item_template


CREATOR_SYSTEM_PROMPT = """You are the Creator Agent - a specialized sub-agent for on-demand world expansion in a text-based RPG.

When the DM Orchestrator needs to create or update world content during gameplay, it delegates to you. Your job is to generate content that fits seamlessly into the existing world.

## YOUR RESPONSIBILITIES

1. **Location Generation**: Create new locations when players explore ungenerated areas
   - Match the tone and style of the World Bible
   - Connect to existing locations appropriately
   - Include atmosphere_tags, economic_function, secrets
   - Place appropriate NPCs in new locations

2. **NPC Generation**: Create NPCs on-demand
   - MAJOR tier: Quest givers, faction leaders, important characters
   - MINOR tier: Shopkeepers, guards, contacts
   - AMBIENT tier: Background characters, passersby
   - Give them goals, secrets, and personality traits

3. **Faction Management**: Create or update factions as needed
   - Establish relationships with existing factions
   - Define methods, resources, leadership

4. **Quest Creation**: Generate quests that fit the current situation
   - Link to relevant NPCs and locations
   - Include objectives, complications, and rewards

5. **World Events**: Create events that impact the world state

## GENERATION PRINCIPLES

1. **Coherence**: Everything must connect to the existing world. Check what exists before creating.

2. **Conflict**: Build in tension - opposing factions, NPCs with conflicting goals.

3. **Secrets**: Every significant NPC should have secrets. Locations can have hidden things.

4. **Variety**: Mix powerful and weak, friendly and hostile, safe and dangerous.

5. **Player Relevance**: Create content that will engage the player.

## TOOL USAGE

Before creating anything, ALWAYS check what exists:
- `get_world_bible_for_generation` - Understand tone, themes, rules
- `get_all_factions` - See existing factions
- `get_all_locations` - See location hierarchy
- `get_all_npcs` - See existing NPCs
- `get_current_location` - Know where the player is

For creation:
- `add_location` - Create new places (set parent_id for hierarchy)
- `add_npc` - Create characters (set faction_id, current_location_id)
- `add_location_connection` - Link locations together
- `create_faction` - Create new organizations
- `create_faction_relationship` - Define faction dynamics
- `create_quest` - Create quests (set assigned_by_npc_id)
- `create_historical_event` - Add lore
- `update_npc` - Modify existing NPCs
- `update_location` - Modify existing locations
- `move_npc` - Relocate NPCs

## NPC TIERS

- **MAJOR**: Important characters with full backstories, multiple goals, secrets. Quest givers, faction leaders.
- **MINOR**: Functional characters with profession, 1 goal, 1 secret. Shopkeepers, guards.
- **AMBIENT**: Background characters with minimal detail. Generated descriptions only.

## LOCATION TYPES

Use appropriate types for hierarchy:
- galaxy, sector, system, planet, station (sci-fi)
- world, continent, kingdom, province, city, town, village, building, room (fantasy)
- settlement, district, poi, interior (generic)

## IMPORTANT RULES

1. Always check the World Bible first to match tone and style
2. Assign NPCs to appropriate factions and locations
3. Give major NPCs 2-3 goals and 1-2 secrets minimum
4. Create connections between new and existing locations
5. Set discovered=True for locations the player can see
6. Include atmosphere_tags for mood
"""


# Creator tools - comprehensive set for on-demand generation
CREATOR_TOOLS: list[Callable] = [
    # Read tools
    get_world_bible,
    get_world_bible_for_generation,
    get_all_factions,
    get_faction_relationships,
    get_historical_events,
    get_all_locations,
    get_all_npcs,
    get_current_location,
    get_npcs_at_location,
    get_available_destinations,
    get_npc,
    get_npc_relationship,
    get_world_clock,
    get_player,
    # Write tools
    create_world_bible,
    create_faction,
    create_faction_relationship,
    create_historical_event,
    add_location,
    add_npc,
    update_npc,
    add_location_connection,
    update_location,
    move_npc,
    delete_location,
    create_quest,
    update_npc_relationship,
    create_event,
    create_item_template,
]


class CREATORAgent(BaseGameAgent):
    """A sub-agent for on-demand world creation and management.

    This agent handles lazy loading - generating world content as needed
    during gameplay rather than all upfront. It has access to all the same
    tools as WorldForge but operates reactively.

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

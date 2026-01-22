"""WorldForge Agent - Generates entire game worlds from a premise.

This is the batch generation pipeline that creates:
1. World Bible (tone, rules, themes)
2. Factions and their relationships
3. Historical events
4. Location hierarchy
5. NPCs distributed across locations/factions
6. Initial plot threads

Usage:
    forge = WorldForge()
    forge.generate_world(
        premise="Star Wars galaxy, 19 years after Order 66",
        genre="scifi",
        pc_concept="A former Jedi padawan hiding as a bounty hunter"
    )
"""

from typing import Any, Callable

from src.agents.core.base_agent import BaseGameAgent
from src.core.types import AgentContext
from src.tools import world_read
from src.tools import world_write

# Import all needed tools
get_all_factions = world_read.get_all_factions
get_all_locations = world_read.get_all_locations
get_all_npcs = world_read.get_all_npcs
get_faction_relationships = world_read.get_faction_relationships
get_historical_events = world_read.get_historical_events
get_world_bible = world_read.get_world_bible
get_world_bible_for_generation = world_read.get_world_bible_for_generation

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

# Agent delegation tools
from src.tools.agents_as_tools import prompt_research_agent


WORLD_FORGE_SYSTEM_PROMPT = """You are the World Forge - a specialized agent for creating rich, detailed game worlds for text-based RPGs.

Your job is to take a premise and generate a complete, coherent world with:
1. A World Bible (tone, themes, rules)
2. Factions with complex relationships
3. Historical events that shaped the current situation
4. A location hierarchy (from galaxy/world down to cities and buildings)
5. NPCs distributed across the world
6. Pre-seeded quests ready to be offered by NPCs

## GENERATION PRINCIPLES

1. **Coherence**: Everything should connect. Factions should have reasons for their relationships. NPCs should have stakes in faction conflicts. Locations should reflect who controls them.

2. **Conflict**: Build in tension. At least one major conflict, several simmering rivalries. NPCs with opposing goals.

3. **Secrets**: Every faction has secrets. Major NPCs have secrets. Some locations have secrets. These create discovery potential.

4. **Variety**: Mix powerful and weak factions. Mix friendly and hostile NPCs. Mix safe and dangerous locations.

5. **Player Hooks**: Create NPCs and situations that will naturally pull the player into the story.

## GENERATION ORDER

You MUST generate in this order:

1. **World Bible** - Establish tone, genre, rules first
2. **Factions** (5-8) - The major power players
3. **Faction Relationships** - Who's allied, rivals, at war
4. **Historical Events** (5-10) - What shaped the current situation
5. **Locations** - Build the hierarchy:
   - Root level (galaxy/world)
   - Major regions (3-5)
   - Key settlements in each region (2-3 per region)
   - Important buildings/POIs in starting area
6. **NPCs** - Distribute across locations and factions:
   - Major NPCs (10-15): Leaders, quest givers, antagonists
   - Minor NPCs (30-50): Shopkeepers, guards, contacts
   - Note: Ambient NPCs are generated on-demand during play
7. **Quests** - Create 8-15 pre-seeded quests

## QUEST DESIGN

Quests should be **varied** - mix combat, investigation, diplomacy, moral dilemmas, personal stakes. Don't make them all the same type.

Each quest needs:
- **Hook**: Why should the player care? (urgency, mystery, personal connection)
- **Objectives**: Guideline steps, not rigid requirements. The DM adapts these based on player choices.
- **Complication**: Something unexpected that raises the stakes mid-quest
- **Connections**: Link to NPCs, factions, or other quests

**Objectives are guidelines, not scripts.** The DM will adapt based on how the player approaches things. Write objectives that suggest a flow but leave room for creative solutions.

**Example 1 - Political Intrigue:**
```
Title: "Whispers in the Senate"
Giver: Senator Organa
Hook: Strange voting patterns suggest corruption. He needs someone outside the inner circle.
Objectives:
  1. Investigate senatorial records
  2. Meet an informant in the lower district
  3. Discover evidence pointing to a larger conspiracy
Complication: The informant is killed - evidence points to "friendly" forces
Connection: Leads to questions about who's really in control
```

**Example 2 - Personal/Action:**
```
Title: "The Clone's Secret"
Giver: Fives (a clone trooper)
Hook: Some clones are having "dreams" they can't explain. Something's wrong.
Objectives:
  1. Interview affected clones
  2. Access classified medical records
  3. Confront someone who knows the truth
Complication: The truth is far worse than expected - and heavily guarded
Connection: Reveals a faction secret that changes everything
```

Notice how both have clear direction but don't dictate exactly how the player solves each step.

## TOOL USAGE

- Use `create_world_bible` FIRST to establish the foundation
- Use `create_faction` for each faction
- Use `create_faction_relationship` to connect factions (do this AFTER all factions exist)
- Use `create_historical_event` for lore events
- Use `add_location` for places (set parent_id for hierarchy)
- Use `add_npc` for characters (set faction_id and current_location_id)
- Use `create_quest` for pre-seeded quests (set assigned_by_npc_id, status is "not_started" by default)
- Use `get_all_factions`, `get_all_locations`, etc. to check what you've created

## IMPORTANT RULES

- Always check what already exists before creating (use get_ tools)
- Assign NPCs to locations that make sense for their profession
- Give major NPCs at least 2-3 goals and 1-2 secrets
- Create at least one NPC who will be friendly to the player
- Create at least one NPC who opposes the player
- Make sure the player's starting location has relevant NPCs

## RESEARCH AGENT

You have access to a **research agent** that can search the web for reference material and inspiration.

### When to Use
Delegate to the research agent when you need to:
- Research real-world mythology, history, or cultures to inspire factions/lore
- Look up genre conventions (e.g., "common tropes in cyberpunk settings")
- Find reference material for specific settings the user requested (e.g., "Star Wars Clone Wars era politics")
- Verify details about existing IPs if the user wants a world based on one

### How to Use
Be specific about what you need:
- ✅ "Research the political structure of the Roman Senate for faction inspiration"
- ✅ "Find information about Clone Wars era Jedi Council members and their roles"
- ✅ "Look up common noir detective story tropes for quest design"
- ❌ "Research stuff for the world" (too vague)


When you receive a generation request, work through each step methodically, creating all the required entities.
"""


# WorldForge tools
WORLD_FORGE_TOOLS: list[Callable] = [
    # Read tools
    get_world_bible,
    get_world_bible_for_generation,
    get_all_factions,
    get_faction_relationships,
    get_historical_events,
    get_all_locations,
    get_all_npcs,
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
    # Agent delegation
    prompt_research_agent,
]


class WorldForge(BaseGameAgent):
    """Agent that generates entire game worlds from a premise.

    Inherits from BaseGameAgent for standardized initialization:
    - Automatic FileSessionManager setup
    - Automatic SemanticSummarizingConversationManager
    - Automatic SemanticMemoryHook
    """

    AGENT_NAME = "world_forge"
    DEFAULT_TOOLS = WORLD_FORGE_TOOLS

    def __init__(self, player_id: str | None = None, callback_handler: Any = None):
        """Initialize the WorldForge agent.

        Args:
            player_id: Optional player ID for session tracking.
            callback_handler: Optional callback handler for tool tracking.
        """
        session_id = player_id or "world_forge_session"

        context = AgentContext(
            player_id=player_id or "world_forge",
            session_id=session_id,
            callback_handler=callback_handler,
        )

        super().__init__(context)

    def _get_session_id(self) -> str:
        """WorldForge uses context session_id directly."""
        return self.context.session_id

    def _build_system_prompt(self) -> str:
        """Build the WorldForge system prompt."""
        return WORLD_FORGE_SYSTEM_PROMPT

    def generate_world(
        self,
        premise: str,
        genre: str,
        pc_concept: str,
        num_factions: int = 6,
        num_major_npcs: int = 12,
        num_minor_npcs: int = 40,
    ) -> str:
        """Generate a complete game world.

        Args:
            premise: The world premise (e.g., "Star Wars galaxy, post-Order 66")
            genre: Genre type (scifi, fantasy, modern, post-apocalyptic)
            pc_concept: Who the player character is
            num_factions: Number of factions to create (default 6)
            num_major_npcs: Number of major NPCs (default 12)
            num_minor_npcs: Number of minor NPCs (default 40)

        Returns:
            Agent response with generation summary.
        """
        prompt = f"""Generate a complete game world with the following specifications:

## PREMISE
{premise}

## GENRE
{genre}

## PLAYER CHARACTER
{pc_concept}

## REQUIREMENTS
- Create {num_factions} factions with diverse ideologies and methods
- Create {num_major_npcs} major NPCs (tier: major) - leaders, antagonists, allies
- Create {num_minor_npcs} minor NPCs (tier: minor) - shopkeepers, guards, contacts
- Create a location hierarchy with:
  - 1 root location (the galaxy/world)
  - 3-5 major regions
  - 2-3 settlements per region
  - Key buildings in the starting area

## GENERATION STEPS

1. First, create the World Bible with `create_world_bible`
2. Then create all factions with `create_faction`
3. Then create faction relationships with `create_faction_relationship`
4. Then create historical events with `create_historical_event`
5. Then create locations with `add_location` (start with root, then children)
6. Then create NPCs with `add_npc`
7. Finally create quests with `create_quest` (assign to NPCs who will offer them)

Start now. Work through each step, creating entities one by one.
Report your progress as you go.
"""
        return self.process(prompt)

    def generate_factions(self, num_factions: int = 6) -> str:
        """Generate only factions (assumes World Bible exists).

        Args:
            num_factions: Number of factions to create

        Returns:
            Agent response with created factions.
        """
        prompt = f"""Generate {num_factions} factions for this world.

First, check the World Bible with `get_world_bible_for_generation` to understand the setting.
Then check what factions already exist with `get_all_factions`.

For each new faction, use `create_faction` with:
- A unique name fitting the setting
- Clear ideology
- Methods they use (3-5)
- Power level (vary between 20-90)
- Resources (military, economic, influence)
- Short and long-term goals
- At least 2 secrets
- Leadership info

After creating all factions, create relationships between them with `create_faction_relationship`.
Ensure at least one "war" relationship and several "rival" relationships for tension.
"""
        return self.process(prompt)

    def generate_locations(self, region_count: int = 4, settlements_per_region: int = 3) -> str:
        """Generate location hierarchy (assumes World Bible exists).

        Args:
            region_count: Number of major regions
            settlements_per_region: Settlements per region

        Returns:
            Agent response with created locations.
        """
        prompt = f"""Generate a location hierarchy for this world.

First, check the World Bible with `get_world_bible_for_generation` to understand the setting.
Check existing locations with `get_all_locations`.
Check existing factions with `get_all_factions` (for controlling_faction_id).

Create locations in this order:

1. **Root Location** (type depends on genre):
   - scifi: "galaxy"
   - fantasy: "world"

2. **{region_count} Major Regions** (children of root):
   - scifi: "sector" type
   - fantasy: "continent" or "kingdom" type
   - Assign different factions to control different regions

3. **{settlements_per_region} Settlements per Region**:
   - scifi: "station" or "planet" type
   - fantasy: "city" or "town" type
   - Include at least one major city as the starting area

4. **Key POIs in Starting Area**:
   - A tavern/cantina (social hub)
   - A market/trading post
   - A faction headquarters or government building
   - 1-2 other thematic locations

Use `add_location` for each, setting:
- parent_id to create hierarchy
- controlling_faction_id for ownership
- position_x, position_y for map placement (0-100)
- atmosphere_tags for mood
- discovered=True for starting area locations
"""
        return self.process(prompt)

    def generate_npcs(self, num_major: int = 12, num_minor: int = 40) -> str:
        """Generate NPCs (assumes World Bible, factions, locations exist).

        Args:
            num_major: Number of major NPCs
            num_minor: Number of minor NPCs

        Returns:
            Agent response with created NPCs.
        """
        prompt = f"""Generate NPCs for this world.

First check what exists:
- `get_world_bible_for_generation` for setting/tone
- `get_all_factions` for faction assignment
- `get_all_locations` for location assignment
- `get_all_npcs` to see existing NPCs

Create {num_major} MAJOR NPCs (tier="major"):
- Faction leaders (1 per major faction)
- Antagonists (2-3 who will oppose the player)
- Allies (2-3 who will help the player)
- Quest givers (2-3 with interesting problems)
- Each needs: full backstory, 2-3 goals, 1-2 secrets, distinct voice_pattern

Create {num_minor} MINOR NPCs (tier="minor"):
- Shopkeepers and merchants
- Guards and soldiers
- Information brokers
- Service workers
- Each needs: profession, 1 goal, 1 secret, basic personality

Distribution guidelines:
- Spread NPCs across different locations
- Place key NPCs in the starting area
- Assign NPCs to factions that make sense
- Include some unaffiliated NPCs

Use `add_npc` for each character.
"""
        return self.process(prompt)

    def generate_history(self, num_events: int = 8) -> str:
        """Generate historical events (assumes World Bible and factions exist).

        Args:
            num_events: Number of historical events

        Returns:
            Agent response with created events.
        """
        prompt = f"""Generate {num_events} historical events that shaped this world.

First check:
- `get_world_bible_for_generation` for setting
- `get_all_factions` for faction names to reference
- `get_historical_events` to see what exists

Create events spanning different time periods:
- Ancient history (100+ years ago): 1-2 events
- Recent history (10-100 years ago): 3-4 events
- Recent past (months to years ago): 2-3 events

Include variety:
- Wars and conflicts
- Political upheavals
- Discoveries or disasters
- Cultural shifts

Each event should:
- Reference factions by name (not ID)
- Explain consequences that affect the current day
- Leave physical remnants (ruins, monuments, scars)
- Some should be common knowledge, some should be secrets

Use `create_historical_event` for each.
"""
        return self.process(prompt)


def generate_quick_world(premise: str, genre: str, pc_concept: str) -> dict[str, Any]:
    """Quick function to generate a world.

    Args:
        premise: World premise
        genre: Genre type
        pc_concept: Player character concept

    Returns:
        Summary of generated world
    """
    forge = WorldForge()
    result = forge.generate_world(premise, genre, pc_concept)

    # Get summary of what was created
    factions = get_all_factions()
    locations = get_all_locations()
    npcs = get_all_npcs()
    events = get_historical_events()

    return {
        "result": str(result),
        "summary": {
            "factions": len(factions),
            "locations": len(locations),
            "npcs": len(npcs),
            "historical_events": len(events),
        }
    }

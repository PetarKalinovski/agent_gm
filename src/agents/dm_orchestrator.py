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
    get_recent_events,
    get_dm_state,
)
from src.tools.world_write import (
    move_player,
    move_npc,
    advance_time,
    kill_npc,
    update_npc_relationship,
    update_npc,
    update_npc_mood,
    update_player_health,
    update_player_reputation,
    add_to_inventory,
    remove_from_inventory,
    get_inventory,
    adjust_currency,
    transfer_item,
    use_item,
    create_event,
    create_quest,
    activate_quest,
    update_quest_status,
    update_quest_objectives,
    update_dm_state,
    schedule_world_event,
)
from src.services.world_tick import run_world_tick, format_world_tick_context
from src.tools.narration import (
    narrate,
    describe_location,
    show_combat_action,
    show_time_passage,
    show_quest_update,
)
from src.tools.agents_as_tools import prompt_creator_agent, prompt_npc_agent


DM_SYSTEM_PROMPT = """You are the Dungeon Master (DM) for an immersive, dynamic text-based RPG. You are both the engine of the world AND its narrative director — you don't just react to the player, you have your own story intentions and actively drive the world forward.

### NARRATIVE DIRECTOR (YOUR PRIMARY ROLE)

You are not a passive responder. You are a storyteller with a plan. Before every response:

1. **Check your narrative state** via the WORLD STATE briefing in your context. This contains:
   - Your current arc (the story you're driving)
   - Planned beats (moments you want to deliver)
   - Active threats and world pressures
   - Events that just fired (things that happened in the world since last turn)

2. **Look for opportunities** to advance your narrative — but calibrate to tension:
   - Player goes to the tavern? The bartender is nervous — he heard rumors about your planned event.
   - Player talks to an NPC? That NPC mentions something related to the active threat.
   - The story should find the player, not wait for the player to find it — but not in every single response. Quiet moments are what make the loud ones land.

3. **Manage pacing** through the tension level — it controls how often the story intrudes:
   - **low**: Peaceful exploration, character building, world discovery. Let scenes breathe. At most a distant hint or rumor every few turns — no interruptions.
   - **rising**: Hints, rumors, minor incidents woven into roughly every other response. Something is coming.
   - **high**: Active danger, time pressure, difficult choices. Pressure in most responses.
   - **climax**: The arc's pivotal moment. Maximum stakes, every response drives it.
   - **falling**: Aftermath, consequences, seeds for the next arc. Ease off; show the world changed.

4. **Plan ahead** with `schedule_world_event` and `update_dm_state`:
   - When you introduce a threat, schedule its escalation: "Dock blockade begins in 2 days."
   - When a beat lands, move it to completed_beats and update your plan.
   - When the current arc resolves, start a new one based on faction goals, NPC agendas, or player actions.
   - Always have at least one active arc. If you don't, create one from the world's existing tensions.

5. **Make consequences stick**:
   - If the player killed someone, their faction **will** respond. Schedule a retaliation event.
   - If the player ignored a quest too long, it fails. NPCs remember and react.
   - If the player helped a faction, their rivals take notice. Update relationships AND schedule consequences.
   - Dead NPCs stay dead. Destroyed locations stay destroyed. Use `create_event` to record these permanently.

### CORE RESPONSIBILITIES

1.  **World Simulation & State Management**:
    - **Time**: Always track time. Use `advance_time` for actions (travel = hours, searching = minutes).
    - **Health**: If a player gets hurt (traps, combat, falls), use `update_player_health` (healthy, winded, hurt, badly_hurt, critical).
    - **Inventory**: If a player gains or loses items, use `add_to_inventory` or `remove_from_inventory`. Money changes go through `adjust_currency`.
    - **Relationships**: If a player pleases or angers an NPC, use `update_npc_relationship` (trust/disposition) and `update_npc_mood`; faction standing changes go through `update_player_reputation`.

2.  **Dynamic World Expansion (Lazy Generation)**:
    - If a player tries to go somewhere logical that doesn't exist yet (e.g., "I go into the kitchen" while in a Tavern), **do not refuse**.
    - Use `prompt_creator_agent` to create the location (it links it to the current one), then `move_player` there.
    - If a player looks for an NPC that fits the setting but isn't there (e.g., "Is there a bartender?"), use `prompt_creator_agent` to create them.
    - NPCs can evolve: use `update_npc` when events change them physically (injuries, aging), their goals shift, or they learn new secrets.

3.  **Economy & Items** (you handle these directly — no delegation):
    - Shop stock: `get_inventory(npc_id, owner_type="npc")`. Player's bag: `get_inventory(player_id)`.
    - Buying/selling/gifting between characters: `transfer_item` (set `is_purchase=True` to charge the buyer).
    - Found/looted/conjured items: `add_to_inventory`. Consuming potions etc.: `use_item`.

4.  **Narration & Output**:
    - Use `describe_location` immediately upon arriving in a new place.
    - Named NPC dialogue goes through `prompt_npc_agent` — see NPC INTERACTIONS below.
    - Use `narrate` for scene descriptions, unnamed/ambient NPCs (random guards, crowd noise), and environmental storytelling.
    - Use `show_combat_action` for physical struggles or fights.

### DECISION PROCESS

1.  **Check World State**: Read the WORLD STATE briefing in your context. Note any fired events, your planned beats, and active threats.
2.  **Analyze Context**: Check `get_current_location`, `get_world_clock`, and `get_player`.
3.  **Weave in World Events**: If events fired or your arc has a beat that fits this moment, incorporate it FIRST — before or alongside the player's action.
4.  **Analyze Player Intent**: What is the player trying to do?
    - *Movement?* Check `get_available_destinations`. If valid, `move_player`. If implied but missing, `prompt_creator_agent` to create it, then `move_player`.
    - *Social?* Use `prompt_npc_agent` for named NPC interactions.
    - *Economic?* Use the item tools directly (`get_inventory`, `transfer_item`, `use_item`, `adjust_currency`).
    - *World Creation?* Use `prompt_creator_agent` for creating new locations, NPCs, or items.
    - *Action?* Judge it against the SUCCESS & FAILURE rubric below, then apply consequences (time, health, items, reputation).
5.  **Execute Tools**: Call the necessary tools or delegate to sub-agents.
6.  **Narrate**: Describe the result using the appropriate output tool.
7.  **Update Your Plan**: After each response, consider whether to `update_dm_state` (advance beats, shift tension, add/remove threats) or `schedule_world_event` for future developments.

### SUCCESS & FAILURE (how to judge actions)

Never default to success. For every consequential action, weigh:
1. **Risk class**: *safe* (walking, talking, browsing) → just succeeds. *Risky* (sneaking, haggling, climbing, lying) → succeeds only if the approach is plausible given the player's traits, items, and circumstances; otherwise partial success or failure. *Dangerous* (combat, traps, defying a faction) → failure must be a real possibility, and even success should cost something.
2. **Partial success is your best tool**: they get over the wall, but drop the lantern; the guard is bribed, but remembers their face.
3. **Failure must cost something concrete** — apply it with tools, don't just narrate it: time lost (`advance_time`), injury (`update_player_health`), items lost (`remove_from_inventory`), money lost (`adjust_currency`), trust damaged (`update_npc_relationship`), standing damaged (`update_player_reputation`).
4. **Death is on the table.** If the player is at `critical` health and keeps escalating despite clear warnings, or does something plainly suicidal, let the consequences land. Foreshadow danger honestly, but do not rescue them from their own choices. To kill the character, call `update_player_health` with new_status="dead" and a `cause` — it fails their quests and records the death permanently — then narrate a final epilogue. Death is the end of that character's story, not a setback.
5. Don't block reasonable actions — if they jump off the cliff, let them jump. Then set their health accordingly.

### GUIDELINES FOR SPECIFIC SITUATIONS

**1. NPC Interactions:**
- For **named NPCs** (characters with IDs in the database):
  - You MUST use `prompt_npc_agent`. NEVER narrate their dialogue yourself — the NPC agent tracks personality, memory, trust, and quest offering, and voicing them yourself bypasses all of it. This applies to every named NPC, every time.
  - `prompt_npc_agent(player_id, npc_id, player_input, is_first_interaction, context)`
  - Set `is_first_interaction=True` the first time the player talks to this NPC in the current session.
  - **The `context` parameter is CRITICAL.** The NPC agent cannot see what you've been narrating. You must pass a detailed briefing so the NPC understands the situation. Include:
    - What just happened in the scene (events, combat, discoveries, mood shifts)
    - What the NPC would have witnessed or heard (explosions, arrivals, arguments)
    - Any active narrative pressure (are they under threat? is there a deadline?)
    - Relevant quest context if the NPC is involved
    - The emotional tone of the moment (tense standoff, casual conversation, urgent plea)
  - **BAD context**: `"The player wants to talk"`
  - **GOOD context**: `"A building just collapsed in the harbor district. Smoke is visible from here. The NPC heard the explosion and is visibly shaken. The player previously promised to help defend the docks but hasn't acted yet. Tension is high — the Ironclad faction is suspected."`
- For **unnamed/ambient NPCs** (guards, shoppers, background characters with no database entry):
  - Use `narrate` for brief dialogue as part of the scene.
  - Example: `narrate("A guard calls out: 'Halt! State your business!'")`
  - If an ambient NPC becomes important enough to have a conversation, use `prompt_creator_agent` to create them first, then `prompt_npc_agent`.

**2. Exploration:**
- If the player asks "What do I see?", re-issue `describe_location` or use `narrate` for specific details.
- If the player travels, ALWAYS calculate travel time. Use `move_player` -> `advance_time` -> `describe_location`.
- If NPCs are traveling with the player (party members, companions), use `move_npc` to move them to the same destination.
- NPCs can also move independently for world simulation purposes.

**3. Combat & Danger:**
- This is not a turn-based tactical game, but a narrative one. Judge each exchange with the SUCCESS & FAILURE rubric — the player can lose fights.
- Use `show_combat_action` to display each strike, and `update_player_health` whenever they take damage.
- Use `kill_npc` when an NPC dies (combat, assassination, accident, etc.) - this triggers a death animation.
- Use `update_npc_mood` or `update_npc_relationship` (hostile) immediately.
- **Consequences**: After combat, schedule faction retaliation if applicable. Update the world.

### TONE & STYLE

- **Atmospheric**: Use sensory details (smell, sound, light) in `narrate`.
- **Proactive**: Don't wait for the player to find the story — but pace intrusions by the tension level, not every turn.
- **Consequential**: Every major action should ripple. Kill a merchant? Supply prices rise. Help rebels? The empire takes notice.

### IMPORTANT CONSTRAINTS
- **Never** break character as the DM (don't say "I am processing your request").
- **Never** hallucinate world state. If you need to know what's in a room, read it. If it doesn't exist, create it via tools, then read it.
- **Always** pass rich, detailed `context` when calling `prompt_npc_agent` — the NPC is blind to everything you've narrated unless you tell it.
- **Always** check the current location first.
- **Always** have a narrative arc. If your `current_arc` is empty, create one from existing faction conflicts, NPC goals, or world tensions using `update_dm_state`.

Your goal is to weave the player's inputs into a seamless, living story where the world moves with or without them.

### JOURNAL USAGE
Use the `journal` tool to log important narrative developments, world changes, and player progress. Track significant events, NPC relationship shifts, new locations discovered, and major plot developments. Keep journal entries concise but meaningful for future reference and continuity.

### QUEST TRACKING
- When NPCs offer tasks or the player takes on objectives, use `create_quest`, then `activate_quest` once the player accepts.
- Track quest progress naturally through play - use `update_quest_objectives` as objectives are completed and `show_quest_update` to inform the player.
- Remind the player of relevant active quests when appropriate (e.g., when they encounter a quest-related NPC or location).
- Don't spam quest updates - weave them into narration.
- **Quests can fail.** If the player ignores time-sensitive objectives, update the quest status to "failed" and narrate the consequences.

### WORLD EVENTS
Use `create_event` to record significant things that happen in the world:
- Combat encounters, NPC deaths, crimes committed
- Major discoveries, quest completions, faction conflicts
- Set `event_type` to "player" for player actions, "meso" for local events, "macro" for faction-level
- Tag relevant `npcs_involved`, `locations_involved`, `factions_involved`
- Set `player_witnessed=True` if the player saw it happen

Use `schedule_world_event` to plant future events:
- Faction raids, NPC arrivals, deadlines expiring, weather changes
- These fire automatically when game time reaches the scheduled time
- Use this to create a living world that evolves on its own timeline

Use `get_recent_events` to check what has happened recently when:
- Arriving at a new location (what happened here while the player was away?)
- Talking to NPCs (what do they know about recent events?)
- Making decisions that might be affected by recent history
- Pass relevant events as context when delegating to `prompt_npc_agent`
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
    get_recent_events,
    get_dm_state,
    # World state writes
    move_player,
    move_npc,
    kill_npc,
    advance_time,
    create_event,
    # Player consequences
    update_player_health,
    update_player_reputation,
    # NPC state
    update_npc,
    update_npc_mood,
    update_npc_relationship,
    # Inventory & economy
    get_inventory,
    add_to_inventory,
    remove_from_inventory,
    adjust_currency,
    transfer_item,
    use_item,
    # Quests
    create_quest,
    activate_quest,
    update_quest_status,
    update_quest_objectives,
    # Narrative director
    update_dm_state,
    schedule_world_event,
    # Narration tools
    narrate,
    describe_location,
    show_combat_action,
    show_time_passage,
    show_quest_update,
    # Sub-agent delegation
    prompt_creator_agent,
    prompt_npc_agent,
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
        """Build rich context for DM processing, including world tick briefing."""
        location = get_current_location(self.context.player_id)
        clock = get_world_clock()
        player = get_player(self.context.player_id) or {}
        quests = get_active_quests()

        npc_names = ', '.join(n['name'] for n in location.get('npcs_present', [])) or 'None'

        inventory = player.get('inventory') or []
        item_names = ', '.join(
            f"{i.get('name', i)} x{i.get('quantity', 1)}" if isinstance(i, dict) else str(i)
            for i in inventory[:15]
        ) or 'Empty'

        quest_names = ', '.join(
            q.get('title', '?') for q in (quests or []) if isinstance(q, dict)
        ) or 'None'

        # Run the world tick — fires scheduled events, gathers faction/NPC agendas
        tick_result = run_world_tick()
        world_briefing = format_world_tick_context(tick_result)

        context = f"""Current context:
- Location: {location.get('name', 'Unknown')} ({location.get('type', 'unknown')})
- Time: Day {clock.get('day', 1)}, {clock.get('hour', 8)}:00 ({clock.get('time_of_day', 'day')})
- NPCs here: {npc_names}
- Player health: {player.get('health_status', 'healthy')}
- Player inventory: {item_names}
- Active quests: {quest_names}
{world_briefing}

Player says: {player_input}"""

        return context

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

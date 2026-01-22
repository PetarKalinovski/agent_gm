"""Agent delegation tools for the DM orchestrator.

These tools allow the DM to delegate to specialized sub-agents.
"""

import os
import tempfile

from strands import tool
from strands.session.file_session_manager import FileSessionManager
from strands_semantic_memory.message_utils import extract_text_content


def get_recent_dm_context(player_id: str, num_messages: int = 6) -> str:
    """Get recent conversation context from the DM agent's session.

    Extracts the last N messages that have text content (ignoring tool-only messages)
    from the DM's conversation history to provide context to NPC agents.

    Args:
        player_id: The player's ID (used as session_id for DM).
        num_messages: Number of recent text messages to retrieve.

    Returns:
        Formatted string with recent conversation context.
    """
    try:
        # DM uses player_id as session_id and "default" as agent_id
        storage_dir = os.path.join(tempfile.gettempdir(), "strands/sessions")
        session_manager = FileSessionManager(session_id=player_id, storage_dir=storage_dir)

        # Get all messages
        all_messages = session_manager.list_messages(
            session_id=player_id,
            agent_id="default",
        )

        if not all_messages:
            return ""

        # Filter to messages that have text content, then get last N
        messages_with_text = []
        for session_msg in all_messages:
            message = session_msg.to_message()
            text = extract_text_content(message)
            if text.strip():
                messages_with_text.append((message, text.strip()))

        # Get the last N messages with text
        recent_messages = messages_with_text[-num_messages:] if len(messages_with_text) > num_messages else messages_with_text

        # Format messages
        context_parts = []
        for message, text in recent_messages:
            role = message.get("role", "unknown")
            if role == "user":
                context_parts.append(f"Player: {text}")
            else:
                context_parts.append(f"Narrator: {text}")

        if not context_parts:
            return ""

        return "Recent conversation:\n" + "\n".join(context_parts)

    except Exception:
        # If we can't read the session, return empty context
        return ""


@tool
def prompt_creator_agent(player_id: str, instruction: str) -> dict[str, str]:
    """Delegate world creation tasks to the Creator Agent.

    Use this when you need to:
    - Generate new locations when players explore ungenerated areas
    - Create NPCs on-demand (any tier: major, minor, ambient)
    - Add new factions or update faction relationships
    - Create quests dynamically during gameplay
    - Expand the world as the player explores

    Args:
        player_id: The player's ID in the database.
        instruction: Detailed instruction for what to create/update. Be specific about:
            - What type of content to create (location, NPC, faction, quest)
            - How it should connect to existing content
            - Any specific requirements (tier, faction affiliation, etc.)

    Returns:
        Dictionary with the agent's response describing what was created.
    """
    # Import inside function to avoid circular imports
    from src.agents.creation_agent import CREATORAgent

    agent = CREATORAgent(player_id)
    result = agent.process_input(instruction)

    return {"text_response": str(result)}


@tool
def prompt_npc_agent(player_id: str, npc_id: str, player_input: str, is_first_interaction: bool = False, context: str = "") -> dict[str, str]:
    """Get a response from an NPC agent for player interaction.

    Use this when the player wants to have a conversation with a named NPC.
    The NPC agent maintains conversation history across multiple calls.

    Args:
        player_id: The player's ID in the database.
        npc_id: The NPC's ID.
        player_input: What the player said or did toward the NPC.
        is_first_interaction: True if this is the first time the player is interacting with this NPC in this session.
        context: Additional context about the interaction. It is encouraged to provide context on the first interaction. The NPC should sometimes know the context (Is the NPC expecting the player? Is the player a stranger?).

    Returns:
        Dictionary with the NPC's response text.
    """
    # Import inside function to avoid circular imports
    from src.repositories.unit_of_work import unit_of_work
    from src.agents.npc_agent import NPCAgent

    # Validate NPC exists before creating agent (fixes "nothing to say" bug)
    with unit_of_work() as uow:
        npc_result = uow.npcs.validate_for_conversation(npc_id)
        if not npc_result.success:
            return {"text_response": npc_result.error, "error": npc_result.error_code}

        npc_data = uow.npcs.to_dict(npc_result.data)

        # Get relationship data
        rel_result = uow.npcs.get_with_relationship(npc_id, player_id)
        _, relationship = rel_result.data if rel_result.success else (None, None)

        relationship_dict = {
            "summary": relationship.summary if relationship else "You have not met this person before.",
            "trust_level": relationship.trust_level if relationship else 50,
            "current_disposition": relationship.current_disposition if relationship else "neutral",
            "key_moments": relationship.key_moments if relationship else [],
            "recent_messages": (relationship.recent_messages or [])[-10:] if relationship else [],
            "revealed_secrets": relationship.revealed_secrets if relationship else [],
        }

    # Auto-fetch recent DM conversation context
    dm_context = get_recent_dm_context(player_id, num_messages=6)

    # Combine auto-context with any explicit context from the DM
    combined_context_parts = []
    if dm_context:
        combined_context_parts.append(dm_context)
    if context:
        combined_context_parts.append(f"Additional context: {context}")

    combined_context = "\n\n".join(combined_context_parts)

    # Create NPC agent and start conversation with validated data
    agent = NPCAgent(player_id, npc_id)
    response = agent.start_conversation(npc=npc_data, relationship=relationship_dict, context=combined_context)

    return {"text_response": str(response)}


@tool
def prompt_economy_agent(player_id: str, instruction: str) -> dict[str, str]:
    """Handle economy, inventory, items, and transactions.

    Use this for:
    - Player buying/selling items from merchants
    - Giving items as loot or quest rewards
    - Using consumable items (potions, food, etc.)
    - Checking inventory contents
    - Managing currency

    Args:
        player_id: The player's ID in the database.
        instruction: Instruction for the Economy agent (e.g., "Player wants to buy health potion from merchant_abc").

    Returns:
        Dictionary with the agent's response.
    """
    # Import inside function to avoid circular imports
    from src.agents.economy_agent import EconomyAgent

    agent = EconomyAgent(player_id)
    result = agent.process_input(instruction)

    return {"text_response": str(result)}


@tool
def prompt_research_agent(session_id: str, query: str) -> dict[str, str]:
    """Delegate research tasks to the Research Agent for gathering reference material.

    Use this when you need to:
    - Research real-world history, mythology, or cultures for inspiration
    - Look up details about existing fictional universes (Star Wars, D&D, etc.)
    - Gather reference material for specific settings or time periods
    - Find naming conventions, political structures, cultural details

    The Research Agent will search the web (primarily Wikipedia and fan wikis)
    and return comprehensive material you can mine for world-building ideas.

    Args:
        session_id: Session identifier for the research agent.
        query: What to research. Be specific about what aspects you need.
            Examples:
            - "Research Roman Senate political structure and notable senators"
            - "Research Clone Wars era Jedi Council members and their fates"
            - "Research feudal Japan daimyo system and samurai culture"
            - "Research deep sea bioluminescent creatures and coral reef ecosystems"

    Returns:
        Dictionary with comprehensive research findings organized by source.
    """
    from src.agents.research_agent import ResearchAgent

    agent = ResearchAgent(session_id)
    result = agent.research(query)

    return {"text_response": str(result)}

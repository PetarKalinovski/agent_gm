import os
import tempfile

from strands import tool
from strands.session.file_session_manager import FileSessionManager

from src.agents.creation_agent import CREATORAgent
from src.agents.npc_agent import NPCAgent
from src.agents.economy_agent import EconomyAgent
from src.agents.world_forge import WorldForge
from strands_semantic_memory.message_utils import extract_text_content

# Global cache for NPC agents to maintain conversation history
_npc_agent_cache: dict[str, NPCAgent] = {}


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
    """Create and return a CREATORAgent instance for the given player ID.

    Args:
        player_id: The player's ID in the database.
        instruction: Instruction for the CREATOR agent.

    Returns:
        Dictionary with the agent's response.
    """
    agent = WorldForge(player_id)

    result = agent.agent(instruction)

    return {"text_response": result}


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
    # Auto-fetch recent DM conversation context
    dm_context = get_recent_dm_context(player_id, num_messages=6)

    # Combine auto-context with any explicit context from the DM
    combined_context_parts = []
    if dm_context:
        combined_context_parts.append(dm_context)
    if context:
        combined_context_parts.append(f"Additional context: {context}")

    combined_context = "\n\n".join(combined_context_parts)

    # Create NPC agent and start conversation
    agent = NPCAgent(player_id, npc_id)
    response = agent.start_conversation(npc=agent.npc, context=combined_context)

    return {"text_response": response}

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
    agent = EconomyAgent(player_id)

    result = agent.process_input(instruction)
    return {"text_response": result}
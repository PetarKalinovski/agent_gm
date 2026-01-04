from src.agents.creation_agent import CREATORAgent
from src.agents.npc_agent import NPCAgent
from src.agents.economy_agent import EconomyAgent
from strands import tool

# Global cache for NPC agents to maintain conversation history
_npc_agent_cache: dict[str, NPCAgent] = {}


@tool
def prompt_creator_agent(player_id: str, instruction: str) -> dict[str, str]:
    """Create and return a CREATORAgent instance for the given player ID.

    Args:
        player_id: The player's ID in the database.
        instruction: Instruction for the CREATOR agent.

    Returns:
        Dictionary with the agent's response.
    """
    agent = CREATORAgent(player_id)

    result = agent.process_input(instruction)

    agent_response = result.messages[-1]["content"][0]["text"]

    return {"text_response": agent_response}


@tool
def prompt_npc_agent(player_id: str, npc_id: str, player_input: str, is_first_interaction: bool = False) -> dict[str, str]:
    """Get a response from an NPC agent for player interaction.

    Use this when the player wants to have a conversation with a named NPC.
    The NPC agent maintains conversation history across multiple calls.

    Args:
        player_id: The player's ID in the database.
        npc_id: The NPC's ID.
        player_input: What the player said or did toward the NPC.
        is_first_interaction: True if this is the first time the player is interacting with this NPC in this session.

    Returns:
        Dictionary with the NPC's response text.
    """
    # Create cache key
    cache_key = f"{player_id}_{npc_id}"

    # Get or create NPC agent
    if cache_key not in _npc_agent_cache or is_first_interaction:
        agent = NPCAgent(player_id, npc_id)
        _npc_agent_cache[cache_key] = agent

        # Start conversation if first interaction
        if is_first_interaction:
            greeting = agent.start_conversation()
            return {"npc_response": greeting}

    agent = _npc_agent_cache[cache_key]

    # Get response
    result = agent.respond(player_input)

    # Clean up cache if conversation ended
    if result.get("conversation_ended"):
        agent.end_conversation()
        del _npc_agent_cache[cache_key]

    return {"npc_response": result["response"]}


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

    agent_response = result.messages[-1]["content"][0]["text"]

    return {"text_response": agent_response}
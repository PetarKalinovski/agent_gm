"""Compound tools for conversation handling.

These tools combine validation, entity lookup, and agent delegation
into single atomic operations to fix issues like the "nothing to say" bug.
"""

from typing import Any

from strands import tool

from src.repositories.unit_of_work import unit_of_work
from src.core.results import Result, ErrorCodes


@tool
def start_npc_conversation(
    player_id: str,
    npc_id: str,
    approach_type: str = "neutral",
    context: str = ""
) -> dict[str, Any]:
    """Start a conversation with an NPC after validating they exist and can talk.

    This is the proper way to initiate NPC conversations. It:
    1. Validates the NPC exists in the database
    2. Checks the NPC's status allows conversation (alive, not unconscious, etc.)
    3. Fetches relationship data
    4. Creates the NPC agent with proper context
    5. Returns the NPC's greeting or a clear error message

    This fixes the "nothing to say" bug by validating BEFORE creating the agent.

    Args:
        player_id: The player's ID.
        npc_id: The NPC's ID to talk to.
        approach_type: How the player approaches (friendly, neutral, aggressive, cautious).
        context: Additional context about the situation.

    Returns:
        Dictionary with NPC response or error information.
        On success: {"success": True, "response": "...", "npc_name": "...", "mood": "..."}
        On error: {"success": False, "error": "...", "error_code": "..."}
    """
    with unit_of_work() as uow:
        # Step 1: Validate NPC exists and can talk
        npc_result = uow.npcs.validate_for_conversation(npc_id)
        if not npc_result.success:
            return npc_result.to_tool_response()

        npc = npc_result.data

        # Step 2: Get relationship data
        rel_result = uow.npcs.get_with_relationship(npc_id, player_id)
        if not rel_result.success:
            return rel_result.to_tool_response()

        _, relationship = rel_result.data

        # Step 3: Import and create agent (inside function to avoid circular imports)
        from src.agents.npc_agent import NPCAgent
        from src.core.types import AgentContext

        # Create agent context
        agent_context = AgentContext(
            player_id=player_id,
            session_id=f"{player_id}_{npc_id}",
        )

        # Create NPC agent - it will use the validated NPC data
        npc_agent = NPCAgent(player_id, npc_id)

        # Build conversation context
        conversation_context = f"The player approaches with a {approach_type} demeanor."
        if context:
            conversation_context += f" {context}"

        # Step 4: Start the conversation
        try:
            response = npc_agent.start_conversation(
                npc=uow.npcs.to_dict(npc),
                relationship=_relationship_to_dict(relationship),
                context=conversation_context
            )

            return {
                "success": True,
                "response": str(response),
                "npc_name": npc.name,
                "npc_id": npc.id,
                "mood": npc.current_mood,
                "tier": npc.tier.value,
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to start conversation: {str(e)}",
                "error_code": ErrorCodes.AGENT_ERROR,
            }


@tool
def continue_npc_conversation(
    player_id: str,
    npc_id: str,
    player_input: str,
    context: str = ""
) -> dict[str, Any]:
    """Continue an existing NPC conversation.

    Args:
        player_id: The player's ID.
        npc_id: The NPC's ID.
        player_input: What the player says.
        context: Additional context about the situation.

    Returns:
        Dictionary with NPC response or conversation end status.
    """
    with unit_of_work() as uow:
        # Validate NPC still exists and can talk
        npc_result = uow.npcs.validate_for_conversation(npc_id)
        if not npc_result.success:
            return npc_result.to_tool_response()

        npc = npc_result.data

        # Get or create NPC agent
        from src.agents.npc_agent import NPCAgent

        npc_agent = NPCAgent(player_id, npc_id)

        try:
            result = npc_agent.respond(player_input, context)

            return {
                "success": True,
                "response": result.get("response", ""),
                "conversation_ended": result.get("conversation_ended", False),
                "npc_name": npc.name,
                "mood": npc.current_mood,
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to get response: {str(e)}",
                "error_code": ErrorCodes.AGENT_ERROR,
            }


@tool
def end_npc_conversation(
    player_id: str,
    npc_id: str,
    reason: str = "normal"
) -> dict[str, Any]:
    """End a conversation with an NPC.

    Args:
        player_id: The player's ID.
        npc_id: The NPC's ID.
        reason: Why the conversation ended (normal, fled, interrupted, hostile).

    Returns:
        Dictionary confirming the conversation end.
    """
    with unit_of_work() as uow:
        npc_result = uow.npcs.get_by_id(npc_id)
        if not npc_result.success:
            return npc_result.to_tool_response()

        npc = npc_result.data

        # Get NPC agent and end conversation
        from src.agents.npc_agent import NPCAgent

        try:
            npc_agent = NPCAgent(player_id, npc_id)
            npc_agent.end_conversation()

            return {
                "success": True,
                "message": f"Conversation with {npc.name} ended.",
                "reason": reason,
            }

        except Exception as e:
            # Conversation might already be ended, that's OK
            return {
                "success": True,
                "message": f"Conversation with {npc.name} ended.",
                "reason": reason,
            }


def _relationship_to_dict(relationship) -> dict[str, Any]:
    """Convert relationship model to dict for NPC agent.

    Args:
        relationship: NPCRelationship model or None.

    Returns:
        Relationship data dictionary.
    """
    if relationship is None:
        return {
            "summary": "You have not met this person before.",
            "trust_level": 50,
            "current_disposition": "neutral",
            "key_moments": [],
            "recent_messages": [],
            "revealed_secrets": [],
        }

    return {
        "summary": relationship.summary or "You have met before.",
        "trust_level": relationship.trust_level or 50,
        "current_disposition": relationship.current_disposition or "neutral",
        "key_moments": relationship.key_moments or [],
        "recent_messages": (relationship.recent_messages or [])[-10:],
        "revealed_secrets": relationship.revealed_secrets or [],
    }

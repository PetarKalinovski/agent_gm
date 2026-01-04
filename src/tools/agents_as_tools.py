from src.agents.creation_agent import CREATORAgent
from strands import tool

@tool
def prompt_creator_agent(player_id: str, instruction:str) -> dict[str, str]:
    """Create and return a CREATORAgent instance for the given player ID.

    Args:
        player_id: The player's ID in the database.
        instruction: Instruction for the CREATOR agent.

    Returns:
        Dictionary with the agent's response.
    """
    agent = CREATORAgent(player_id)

    result = agent(instruction)

    agent_response = result.messages[-1]["content"][0]["text"]

    return {"text_response": agent_response}
"""Agent definitions for the game."""

from src.agents.base import (
    setup_api_keys,
    create_model,
    create_agent,
    get_available_agents,
)
from src.agents.dm_orchestrator import DMOrchestrator
from src.agents.npc_agent import NPCAgent
from src.agents.economy_agent import EconomyAgent
from src.agents.creation_agent import CREATORAgent
from src.agents.world_forge import WorldForge, generate_quick_world

__all__ = [
    "setup_api_keys",
    "create_model",
    "create_agent",
    "get_available_agents",
    "DMOrchestrator",
    "NPCAgent",
    "EconomyAgent",
    "CREATORAgent",
    "WorldForge",
    "generate_quick_world",
]

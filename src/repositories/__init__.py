"""Repository pattern for database access.

Only the NPC repository is retained — it backs NPC validation in
prompt_npc_agent. All other world mutation goes through src/tools/world_write.
"""

from src.repositories.base import BaseRepository
from src.repositories.unit_of_work import UnitOfWork, unit_of_work
from src.repositories.npc_repository import NPCRepository

__all__ = [
    "BaseRepository",
    "UnitOfWork",
    "unit_of_work",
    "NPCRepository",
]

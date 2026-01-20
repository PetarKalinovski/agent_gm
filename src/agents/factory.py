"""Agent factory for creating agents with proper context propagation."""

from typing import Any
from datetime import datetime, timedelta

from src.core.types import AgentContext


class AgentFactory:
    """Factory for creating agents with proper context propagation.

    Handles:
    - Agent caching with TTL
    - Callback handler propagation to sub-agents
    - Session cleanup

    Example:
        dm = AgentFactory.create_dm(player_id, callback_handler=tracker)
        npc = AgentFactory.create_npc_agent(player_id, npc_id, callback_handler=tracker)
    """

    # Agent cache with timestamps
    _agent_cache: dict[str, dict[str, Any]] = {}

    # Cache TTL in minutes
    CACHE_TTL_MINUTES = 60

    @classmethod
    def _get_cache_key(cls, agent_type: str, *identifiers: str) -> str:
        """Build a cache key for an agent."""
        return f"{agent_type}_{'_'.join(identifiers)}"

    @classmethod
    def _is_cache_valid(cls, cache_key: str) -> bool:
        """Check if a cached agent is still valid."""
        if cache_key not in cls._agent_cache:
            return False

        entry = cls._agent_cache[cache_key]
        created_at = entry.get("created_at", datetime.min)
        return datetime.utcnow() - created_at < timedelta(minutes=cls.CACHE_TTL_MINUTES)

    @classmethod
    def _cache_agent(cls, cache_key: str, agent: Any) -> None:
        """Cache an agent with timestamp."""
        cls._agent_cache[cache_key] = {
            "agent": agent,
            "created_at": datetime.utcnow(),
        }

    @classmethod
    def _get_cached_agent(cls, cache_key: str) -> Any | None:
        """Get a cached agent if valid."""
        if cls._is_cache_valid(cache_key):
            return cls._agent_cache[cache_key]["agent"]
        return None

    @classmethod
    def create_dm(
        cls,
        player_id: str,
        callback_handler: Any = None,
        use_cache: bool = True,
    ):
        """Create or get cached DM orchestrator.

        Args:
            player_id: The player's ID.
            callback_handler: Optional callback handler for tool tracking.
            use_cache: Whether to use caching.

        Returns:
            DMOrchestrator instance.
        """
        cache_key = cls._get_cache_key("dm", player_id)

        if use_cache:
            cached = cls._get_cached_agent(cache_key)
            if cached:
                # Update callback handler if provided
                if callback_handler and hasattr(cached, 'context'):
                    cached.context.callback_handler = callback_handler
                return cached

        # Import here to avoid circular imports
        from src.agents.dm_orchestrator import DMOrchestrator

        context = AgentContext(
            player_id=player_id,
            session_id=player_id,
            callback_handler=callback_handler,
        )

        dm = DMOrchestrator(context)

        if use_cache:
            cls._cache_agent(cache_key, dm)

        return dm

    @classmethod
    def create_npc_agent(
        cls,
        player_id: str,
        npc_id: str,
        callback_handler: Any = None,
        use_cache: bool = True,
    ):
        """Create NPC agent with callback propagation.

        Args:
            player_id: The player's ID.
            npc_id: The NPC's ID.
            callback_handler: Optional callback handler for tool tracking.
            use_cache: Whether to use caching.

        Returns:
            NPCAgent instance.
        """
        cache_key = cls._get_cache_key("npc", player_id, npc_id)

        if use_cache:
            cached = cls._get_cached_agent(cache_key)
            if cached:
                return cached

        from src.agents.npc_agent import NPCAgent

        agent = NPCAgent(player_id, npc_id)

        if use_cache:
            cls._cache_agent(cache_key, agent)

        return agent

    @classmethod
    def create_economy_agent(
        cls,
        player_id: str,
        callback_handler: Any = None,
        use_cache: bool = True,
    ):
        """Create economy agent.

        Args:
            player_id: The player's ID.
            callback_handler: Optional callback handler.
            use_cache: Whether to use caching.

        Returns:
            EconomyAgent instance.
        """
        cache_key = cls._get_cache_key("economy", player_id)

        if use_cache:
            cached = cls._get_cached_agent(cache_key)
            if cached:
                return cached

        from src.agents.economy_agent import EconomyAgent

        agent = EconomyAgent(player_id)

        if use_cache:
            cls._cache_agent(cache_key, agent)

        return agent

    @classmethod
    def create_creator_agent(
        cls,
        player_id: str,
        callback_handler: Any = None,
        use_cache: bool = False,  # Creator agent usually doesn't need caching
    ):
        """Create world creator agent.

        Args:
            player_id: The player's ID.
            callback_handler: Optional callback handler.
            use_cache: Whether to use caching.

        Returns:
            CREATORAgent instance.
        """
        from src.agents.creation_agent import CREATORAgent

        return CREATORAgent(player_id)

    @classmethod
    def create_world_forge(
        cls,
        player_id: str | None = None,
        callback_handler: Any = None,
    ):
        """Create world forge agent for world generation.

        Args:
            player_id: Optional player ID for session.
            callback_handler: Optional callback handler.

        Returns:
            WorldForge instance.
        """
        from src.agents.world_forge import WorldForge

        return WorldForge(player_id)

    @classmethod
    def clear_cache(cls, player_id: str | None = None) -> int:
        """Clear agent cache.

        Args:
            player_id: If provided, only clear agents for this player.
                      If None, clear all agents.

        Returns:
            Number of agents cleared.
        """
        if player_id:
            keys_to_remove = [
                k for k in cls._agent_cache
                if player_id in k
            ]
            for key in keys_to_remove:
                del cls._agent_cache[key]
            return len(keys_to_remove)
        else:
            count = len(cls._agent_cache)
            cls._agent_cache.clear()
            return count

    @classmethod
    def cleanup_expired(cls) -> int:
        """Remove expired agents from cache.

        Returns:
            Number of agents removed.
        """
        expired_keys = [
            key for key in cls._agent_cache
            if not cls._is_cache_valid(key)
        ]
        for key in expired_keys:
            del cls._agent_cache[key]
        return len(expired_keys)

    @classmethod
    def get_cache_stats(cls) -> dict[str, Any]:
        """Get cache statistics.

        Returns:
            Dictionary with cache statistics.
        """
        valid_count = sum(
            1 for key in cls._agent_cache
            if cls._is_cache_valid(key)
        )
        return {
            "total_cached": len(cls._agent_cache),
            "valid_cached": valid_count,
            "expired_cached": len(cls._agent_cache) - valid_count,
            "ttl_minutes": cls.CACHE_TTL_MINUTES,
        }

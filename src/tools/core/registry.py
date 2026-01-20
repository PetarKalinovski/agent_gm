"""Tool registry for centralized tool management."""

from enum import Enum
from typing import Callable, Any


class ToolCategory(Enum):
    """Categories for organizing tools."""
    WORLD_READ = "world_read"
    WORLD_WRITE = "world_write"
    NARRATION = "narration"
    COMPOUND = "compound"
    AGENT = "agent"
    PARTY = "party"
    COMBAT = "combat"


class ToolRegistry:
    """Central registry for all tools.

    Allows tools to be registered with metadata and retrieved by category.
    Useful for dynamic tool assignment to agents.

    Example:
        # Register a tool
        @ToolRegistry.register(ToolCategory.WORLD_READ)
        @tool
        def get_location(location_id: str) -> dict:
            ...

        # Get tools for an agent
        tools = ToolRegistry.get_tools_for_agent("dm_orchestrator")
    """

    _tools: dict[str, dict[str, Any]] = {}

    # Agent tool configurations
    AGENT_TOOL_SETS: dict[str, list[ToolCategory]] = {
        "dm_orchestrator": [
            ToolCategory.WORLD_READ,
            ToolCategory.NARRATION,
            ToolCategory.AGENT,
            ToolCategory.COMPOUND,
        ],
        "npc_agent": [
            ToolCategory.NARRATION,
        ],
        "economy_agent": [
            ToolCategory.WORLD_READ,
            ToolCategory.WORLD_WRITE,
        ],
        "creator_agent": [
            ToolCategory.WORLD_READ,
            ToolCategory.WORLD_WRITE,
        ],
        "combat_agent": [
            ToolCategory.COMBAT,
            ToolCategory.NARRATION,
        ],
        "world_forge": [
            ToolCategory.WORLD_READ,
            ToolCategory.WORLD_WRITE,
        ],
    }

    @classmethod
    def register(
        cls,
        category: ToolCategory,
        requires_player: bool = False,
        requires_transaction: bool = False,
        description: str | None = None,
    ) -> Callable:
        """Decorator to register a tool in the registry.

        Args:
            category: The tool's category.
            requires_player: Whether the tool requires a player context.
            requires_transaction: Whether the tool modifies database state.
            description: Optional description override.

        Returns:
            Decorator function.
        """
        def decorator(func: Callable) -> Callable:
            cls._tools[func.__name__] = {
                "func": func,
                "category": category,
                "requires_player": requires_player,
                "requires_transaction": requires_transaction,
                "description": description or func.__doc__,
            }
            return func
        return decorator

    @classmethod
    def get_tool(cls, name: str) -> Callable | None:
        """Get a tool by name.

        Args:
            name: The tool's name.

        Returns:
            The tool function or None if not found.
        """
        tool_info = cls._tools.get(name)
        return tool_info["func"] if tool_info else None

    @classmethod
    def get_tools_by_category(cls, category: ToolCategory) -> list[Callable]:
        """Get all tools in a category.

        Args:
            category: The category to filter by.

        Returns:
            List of tool functions.
        """
        return [
            info["func"]
            for info in cls._tools.values()
            if info["category"] == category
        ]

    @classmethod
    def get_tools_for_agent(cls, agent_type: str) -> list[Callable]:
        """Get appropriate tools for an agent type.

        Args:
            agent_type: The agent type (e.g., "dm_orchestrator").

        Returns:
            List of tool functions appropriate for the agent.
        """
        categories = cls.AGENT_TOOL_SETS.get(agent_type, [])
        tools = []
        for category in categories:
            tools.extend(cls.get_tools_by_category(category))
        return tools

    @classmethod
    def list_tools(cls) -> list[dict[str, Any]]:
        """List all registered tools with their metadata.

        Returns:
            List of tool info dictionaries.
        """
        return [
            {
                "name": name,
                "category": info["category"].value,
                "requires_player": info["requires_player"],
                "requires_transaction": info["requires_transaction"],
                "description": info["description"],
            }
            for name, info in cls._tools.items()
        ]

    @classmethod
    def clear(cls):
        """Clear all registered tools. Useful for testing."""
        cls._tools.clear()

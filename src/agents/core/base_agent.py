"""Base agent class with standardized initialization."""

from abc import ABC, abstractmethod
from typing import Any, Callable

from strands import Agent
from strands.session.file_session_manager import FileSessionManager
from strands_semantic_memory import (
    SemanticSummarizingConversationManager,
    SemanticMemoryHook,
)

from src.agents.base import create_agent
from src.core.types import AgentContext


class BaseGameAgent(ABC):
    """Base class for all game agents.

    Provides standardized initialization for:
    - FileSessionManager (with configurable session ID)
    - SemanticSummarizingConversationManager
    - SemanticMemoryHook
    - Callback handler propagation

    Subclasses must implement:
    - AGENT_NAME: str - The agent's name for config lookup
    - _build_system_prompt() - Returns the system prompt

    Subclasses may override:
    - DEFAULT_TOOLS: list - Default tools for the agent
    - _get_session_id() - Custom session ID logic
    - _get_tools() - Custom tool selection
    - _build_context() - Custom context building

    Example:
        class MyAgent(BaseGameAgent):
            AGENT_NAME = "my_agent"
            DEFAULT_TOOLS = [tool1, tool2]

            def _build_system_prompt(self) -> str:
                return "You are a helpful agent..."
    """

    # Override in subclasses
    AGENT_NAME: str = "base_agent"
    DEFAULT_TOOLS: list[Callable] = []

    def __init__(self, context: AgentContext):
        """Initialize the agent with context.

        Args:
            context: AgentContext containing player_id, callback_handler, etc.
        """
        self.context = context
        self._agent: Agent | None = None

    @property
    def agent(self) -> Agent:
        """Lazily create the underlying Strands agent."""
        if self._agent is None:
            self._agent = self._create_agent()
        return self._agent

    def _create_agent(self) -> Agent:
        """Create the underlying Strands agent with all setup."""
        session_id = self._get_session_id()
        session_manager = FileSessionManager(session_id=session_id)

        conv_manager = SemanticSummarizingConversationManager(
            embedding_model="all-MiniLM-L12-v2"
        )
        semantic_hook = SemanticMemoryHook()

        return create_agent(
            agent_name=self.AGENT_NAME,
            system_prompt=self._build_system_prompt(),
            tools=self._get_tools(),
            session_manager=session_manager,
            conversation_manager=conv_manager,
            hooks=[semantic_hook],
            callback_handler=self.context.callback_handler,
        )

    def _get_session_id(self) -> str:
        """Build session ID for FileSessionManager.

        Override in subclasses for custom session ID patterns.

        Returns:
            Session ID string.
        """
        return f"{self.context.player_id}_{self.AGENT_NAME}"

    @abstractmethod
    def _build_system_prompt(self) -> str:
        """Build the system prompt for this agent.

        Must be implemented by subclasses.

        Returns:
            System prompt string.
        """
        pass

    def _get_tools(self) -> list[Callable]:
        """Get tools for this agent.

        Override in subclasses to customize tool selection.

        Returns:
            List of tool functions.
        """
        return self.DEFAULT_TOOLS

    def _build_context(self, input_text: str) -> str:
        """Build context string for agent invocation.

        Override in subclasses for custom context building.

        Args:
            input_text: The input to process.

        Returns:
            Context string to pass to agent.
        """
        return input_text

    def process(self, input_text: str) -> str:
        """Standard processing method.

        Args:
            input_text: Input to process.

        Returns:
            Agent response as string.
        """
        context = self._build_context(input_text)
        response = self.agent(context)
        return str(response)

    def __call__(self, input_text: str) -> str:
        """Make the agent callable.

        Args:
            input_text: Input to process.

        Returns:
            Agent response as string.
        """
        return self.process(input_text)


class SimpleAgent(BaseGameAgent):
    """A simple agent that doesn't use session management.

    Useful for one-off tasks that don't need conversation history.
    """

    def _create_agent(self) -> Agent:
        """Create agent without session management."""
        return create_agent(
            agent_name=self.AGENT_NAME,
            system_prompt=self._build_system_prompt(),
            tools=self._get_tools(),
            callback_handler=self.context.callback_handler,
        )

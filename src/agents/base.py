"""Base agent setup using Strands Agents SDK with LiteLLM."""

import os
import logging
from typing import Any

from strands import Agent
from strands.models.litellm import LiteLLMModel

from src.config import get_agent_config, get_api_key, load_agents_config

# Suppress verbose LiteLLM warnings
logging.getLogger("LiteLLM").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.WARNING)


def setup_api_keys() -> None:
    """Set up API keys in environment for LiteLLM."""
    openrouter_key = get_api_key("openrouter")
    if openrouter_key:
        os.environ["OPENROUTER_API_KEY"] = openrouter_key

    anthropic_key = get_api_key("anthropic")
    if anthropic_key:
        os.environ["ANTHROPIC_API_KEY"] = anthropic_key

    openai_key = get_api_key("openai")
    if openai_key:
        os.environ["OPENAI_API_KEY"] = openai_key


def _resolve_api_key(model: str) -> str | None:
    """Resolve the API key for a model based on its provider prefix.

    Supports:
        - openrouter/... → OPENROUTER_API_KEY
        - anthropic/...  → ANTHROPIC_API_KEY (works with both API keys and setup-tokens)
        - openai/...     → OPENAI_API_KEY

    Args:
        model: Model ID string in LiteLLM format (provider/model-name).

    Returns:
        API key string or None if not found.
    """
    model_lower = model.lower()
    if model_lower.startswith("openrouter/"):
        return os.environ.get("OPENROUTER_API_KEY")
    elif model_lower.startswith("anthropic/"):
        return os.environ.get("ANTHROPIC_API_KEY")
    elif model_lower.startswith("openai/"):
        return os.environ.get("OPENAI_API_KEY")
    return None


def create_model(agent_name: str) -> LiteLLMModel:
    """Create a LiteLLM model from agent config.

    The provider is detected from the model string prefix:
        - openrouter/provider/model → routes through OpenRouter
        - anthropic/model           → direct Anthropic API
        - openai/model              → direct OpenAI API

    Args:
        agent_name: Name of the agent in agents.yaml.

    Returns:
        Configured LiteLLMModel.
    """
    config = get_agent_config(agent_name)

    # Resolve API key based on provider prefix
    client_args = {}
    api_key = _resolve_api_key(config.model)
    if api_key:
        client_args["api_key"] = api_key

    return LiteLLMModel(
        model_id=config.model,
        client_args=client_args,
        params={
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "drop_params": True,  # Drop unsupported params like reasoningContent
        }
    )


def create_agent(
    agent_name: str,
    system_prompt: str,
    tools: list | None = None,
    session_manager: Any = None,
    conversation_manager: Any = None,
    hooks: list | None = None,
    callback_handler: Any = None,
) -> Agent:
    """Create a Strands Agent with configuration from agents.yaml.

    Args:
        agent_name: Name of the agent (must exist in agents.yaml).
        system_prompt: The system prompt for the agent.
        tools: List of tools the agent can use.
        session_manager: Optional session manager for conversation history.
        conversation_manager: Optional conversation manager.
        hooks: Optional list of hooks.
        callback_handler: Optional callback handler for tool tracking.
            If None, will try to use the context callback handler.

    Returns:
        Configured Strands Agent.
    """
    from src.agents.callback_context import get_callback_handler

    model = create_model(agent_name)

    # Use provided callback_handler, or fall back to context callback
    handler = callback_handler
    if handler is None:
        # return Agent(
        #     model=model,
        #     system_prompt=system_prompt,
        #     tools=tools or [],
        #     session_manager=session_manager,
        #     conversation_manager=conversation_manager,
        #     hooks=hooks or [],
        # )
        handler = get_callback_handler()

    return Agent(
        model=model,
        system_prompt=system_prompt,
        tools=tools or [],
        session_manager=session_manager,
        conversation_manager=conversation_manager,
        hooks=hooks or [],
        callback_handler=handler,
    )


def get_available_agents() -> list[str]:
    """Get list of available agent names from config.

    Returns:
        List of agent names.
    """
    configs = load_agents_config()
    return list(configs.keys())

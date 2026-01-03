"""Base agent setup using Strands Agents SDK with LiteLLM."""

import os
from typing import Any

from strands import Agent
from strands.models.litellm import LiteLLMModel

from src.config import get_agent_config, get_api_key, load_agents_config


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


def create_model(agent_name: str) -> LiteLLMModel:
    """Create a LiteLLM model from agent config.

    Args:
        agent_name: Name of the agent in agents.yaml.

    Returns:
        Configured LiteLLMModel.
    """
    config = get_agent_config(agent_name)

    # Build client args
    client_args = {}

    # Check for OpenRouter API key
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    if openrouter_key and "openrouter" in config.model.lower():
        client_args["api_key"] = openrouter_key

    return LiteLLMModel(
        model_id=config.model,
        client_args=client_args,
        params={
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        }
    )


def create_agent(
    agent_name: str,
    system_prompt: str,
    tools: list | None = None,
) -> Agent:
    """Create a Strands Agent with configuration from agents.yaml.

    Args:
        agent_name: Name of the agent (must exist in agents.yaml).
        system_prompt: The system prompt for the agent.
        tools: List of tools the agent can use.

    Returns:
        Configured Strands Agent.
    """
    model = create_model(agent_name)

    return Agent(
        model=model,
        system_prompt=system_prompt,
        tools=tools or [],
    )


def get_available_agents() -> list[str]:
    """Get list of available agent names from config.

    Returns:
        List of agent names.
    """
    configs = load_agents_config()
    return list(configs.keys())

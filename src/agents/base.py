"""Base agent setup using Strands Agents SDK with LiteLLM and native Anthropic."""

import os
import logging
from typing import Any

from strands import Agent
from strands.models.litellm import LiteLLMModel
from strands.models.anthropic import AnthropicModel

from src.config import get_agent_config, get_api_key, load_agents_config

# Suppress verbose LiteLLM warnings
logging.getLogger("LiteLLM").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# OAuth token prefix used by Anthropic setup-tokens
_ANTHROPIC_OAUTH_PREFIX = "sk-ant-oat"


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


def _resolve_anthropic_client_args() -> dict[str, Any]:
    """Resolve Anthropic client args, handling both API keys and OAuth tokens.

    Checks ANTHROPIC_AUTH_TOKEN first (explicit OAuth), then ANTHROPIC_API_KEY.
    Auto-detects OAuth tokens by their sk-ant-oat prefix and routes them
    as auth_token (Bearer) instead of api_key (x-api-key).

    Returns:
        Client args dict for anthropic.AsyncAnthropic.
    """
    # Check explicit OAuth token first
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if auth_token:
        logger.info("Using Anthropic OAuth auth_token (Bearer)")
        return {"auth_token": auth_token}

    # Fall back to API key, auto-detecting OAuth tokens by prefix
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        if api_key.startswith(_ANTHROPIC_OAUTH_PREFIX):
            logger.info("Detected OAuth token in ANTHROPIC_API_KEY, using as auth_token (Bearer)")
            return {"auth_token": api_key}
        else:
            return {"api_key": api_key}

    raise ValueError(
        "No Anthropic credentials found. Set ANTHROPIC_API_KEY (for API keys) "
        "or ANTHROPIC_AUTH_TOKEN (for OAuth setup-tokens from `claude setup-token`)."
    )


def _create_anthropic_model(agent_name: str) -> AnthropicModel:
    """Create a native Anthropic model (bypasses LiteLLM).

    Uses the Anthropic SDK directly, which properly handles both
    API keys (x-api-key) and OAuth tokens (Authorization: Bearer).

    Args:
        agent_name: Name of the agent in agents.yaml.

    Returns:
        Configured AnthropicModel.
    """
    config = get_agent_config(agent_name)
    # Strip the "anthropic/" prefix to get the bare model ID
    model_id = config.model.split("/", 1)[1] if "/" in config.model else config.model

    client_args = _resolve_anthropic_client_args()

    model = AnthropicModel(
        model_id=model_id,
        max_tokens=config.max_tokens,
        client_args=client_args,
        params={"temperature": config.temperature},
    )

    # When using OAuth (auth_token), the SDK auto-reads ANTHROPIC_API_KEY
    # from env and sets both X-Api-Key and Authorization headers.
    # Anthropic rejects OAuth tokens in X-Api-Key, so we must nullify it.
    if "auth_token" in client_args:
        model.client.api_key = None

    return model


def _create_litellm_model(agent_name: str) -> LiteLLMModel:
    """Create a LiteLLM model for OpenRouter/OpenAI/other providers.

    Args:
        agent_name: Name of the agent in agents.yaml.

    Returns:
        Configured LiteLLMModel.
    """
    config = get_agent_config(agent_name)

    client_args = {}
    model_lower = config.model.lower()
    if model_lower.startswith("openrouter/"):
        key = os.environ.get("OPENROUTER_API_KEY")
        if key:
            client_args["api_key"] = key
    elif model_lower.startswith("openai/"):
        key = os.environ.get("OPENAI_API_KEY")
        if key:
            client_args["api_key"] = key

    return LiteLLMModel(
        model_id=config.model,
        client_args=client_args,
        params={
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "drop_params": True,
        }
    )


def create_model(agent_name: str) -> AnthropicModel | LiteLLMModel:
    """Create a model from agent config, routing to the correct provider.

    Provider routing (detected from model string prefix):
        - anthropic/model           -> native Anthropic SDK (supports OAuth)
        - openrouter/provider/model -> LiteLLM via OpenRouter
        - openai/model              -> LiteLLM via OpenAI
        - other                     -> LiteLLM (generic)

    Args:
        agent_name: Name of the agent in agents.yaml.

    Returns:
        Configured model instance.
    """
    config = get_agent_config(agent_name)

    if config.model.lower().startswith("anthropic/"):
        logger.info("agent=%s | using native Anthropic provider for %s", agent_name, config.model)
        return _create_anthropic_model(agent_name)
    else:
        return _create_litellm_model(agent_name)


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

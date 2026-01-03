"""Configuration loading and management."""

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class AgentConfig(BaseModel):
    """Configuration for a single agent."""
    model: str
    temperature: float = 0.7
    max_tokens: int = 2048
    description: str = ""


class TimeCosts(BaseModel):
    """Time costs for various actions in hours."""
    conversation: float = 0.5
    short_travel: float = 1.0
    long_travel: float = 8.0
    rest: float = 8.0
    combat: float = 0.25


class GameConfig(BaseModel):
    """Game-specific configuration."""
    time_costs: TimeCosts = Field(default_factory=TimeCosts)
    recent_messages_limit: int = 10
    summary_trigger_threshold: int = 20


class DatabaseConfig(BaseModel):
    """Database configuration."""
    path: str = "data/game.db"
    echo: bool = False


class ApiConfig(BaseModel):
    """API configuration."""
    openrouter_key_env: str = "OPENROUTER_API_KEY"
    anthropic_key_env: str = "ANTHROPIC_API_KEY"
    openai_key_env: str = "OPENAI_API_KEY"


class DisplayConfig(BaseModel):
    """Display configuration."""
    use_rich: bool = True
    theme: str = "monokai"


class Settings(BaseModel):
    """Full application settings."""
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    game: GameConfig = Field(default_factory=GameConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)


# Global config instances
_settings: Settings | None = None
_agents_config: dict[str, AgentConfig] | None = None


def get_config_path() -> Path:
    """Get the path to the config directory."""
    # Check if we're in the project root or somewhere else
    cwd = Path.cwd()
    if (cwd / "config").exists():
        return cwd / "config"
    # Check parent directories
    for parent in cwd.parents:
        if (parent / "config").exists():
            return parent / "config"
    # Default to creating in cwd
    return cwd / "config"


def load_settings(config_path: Path | None = None) -> Settings:
    """Load application settings from settings.yaml.

    Args:
        config_path: Optional path to config directory.

    Returns:
        Settings object.
    """
    global _settings

    if _settings is not None:
        return _settings

    if config_path is None:
        config_path = get_config_path()

    settings_file = config_path / "settings.yaml"

    if settings_file.exists():
        with open(settings_file) as f:
            data = yaml.safe_load(f) or {}
        _settings = Settings(**data)
    else:
        _settings = Settings()

    return _settings


def load_agents_config(config_path: Path | None = None) -> dict[str, AgentConfig]:
    """Load agent configurations from agents.yaml.

    Args:
        config_path: Optional path to config directory.

    Returns:
        Dictionary mapping agent names to their configs.
    """
    global _agents_config

    if _agents_config is not None:
        return _agents_config

    if config_path is None:
        config_path = get_config_path()

    agents_file = config_path / "agents.yaml"

    _agents_config = {}
    if agents_file.exists():
        with open(agents_file) as f:
            data = yaml.safe_load(f) or {}
        for name, config in data.get("agents", {}).items():
            _agents_config[name] = AgentConfig(**config)

    return _agents_config


def get_agent_config(agent_name: str) -> AgentConfig:
    """Get configuration for a specific agent.

    Args:
        agent_name: Name of the agent.

    Returns:
        AgentConfig for the agent.

    Raises:
        KeyError: If agent not found in config.
    """
    configs = load_agents_config()
    if agent_name not in configs:
        raise KeyError(f"Agent '{agent_name}' not found in config. Available: {list(configs.keys())}")
    return configs[agent_name]


def get_api_key(provider: str) -> str | None:
    """Get API key for a provider from environment.

    Args:
        provider: Provider name (openrouter, anthropic, openai).

    Returns:
        API key or None if not set.
    """
    settings = load_settings()
    env_var_map = {
        "openrouter": settings.api.openrouter_key_env,
        "anthropic": settings.api.anthropic_key_env,
        "openai": settings.api.openai_key_env,
    }
    env_var = env_var_map.get(provider.lower())
    if env_var:
        return os.environ.get(env_var)
    return None


def reload_config() -> None:
    """Force reload of all configuration."""
    global _settings, _agents_config
    _settings = None
    _agents_config = None

"""Per-agent model configuration knobs."""

import os

from src.agents.base import _create_litellm_model
from src.config import AgentConfig, load_agents_config


def test_campaign_forge_caps_its_reasoning_budget():
    cfg = load_agents_config()["campaign_forge"]
    assert cfg.reasoning == {"enabled": False}
    assert cfg.max_tokens >= 12000


def test_reasoning_block_rides_as_extra_body(monkeypatch):
    monkeypatch.setattr("src.agents.base.get_agent_config",
                        lambda name: AgentConfig(model="openrouter/anthropic/claude-sonnet-5",
                                                 max_tokens=1000, reasoning={"enabled": False}))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    model = _create_litellm_model("anything")
    params = model.get_config()["params"]
    assert params["extra_body"] == {"reasoning": {"enabled": False}}
    assert params["max_tokens"] == 1000


def test_no_reasoning_block_means_no_extra_body(monkeypatch):
    monkeypatch.setattr("src.agents.base.get_agent_config",
                        lambda name: AgentConfig(model="openrouter/deepseek/deepseek-v3.2"))
    model = _create_litellm_model("anything")
    assert "extra_body" not in model.get_config()["params"]


def test_env_override_swaps_one_model(monkeypatch):
    from src import config as config_module

    monkeypatch.setattr(config_module, "_agents_config", None)
    monkeypatch.setenv("FORGE_MODEL_NPC_AGENT", "openrouter/test/model")
    cfg = config_module.load_agents_config()
    assert cfg["npc_agent"].model == "openrouter/test/model"
    assert cfg["dm_orchestrator"].model != "openrouter/test/model"
    monkeypatch.setattr(config_module, "_agents_config", None)

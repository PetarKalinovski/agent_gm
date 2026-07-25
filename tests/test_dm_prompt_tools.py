"""The DM prompt must never command tools the DM doesn't have.

This was the single biggest gameplay bug: the prompt instructed
`update_player_health`, `add_to_inventory`, etc., but none were registered,
so the game could never impose consequences.
"""

import inspect
import re
from types import SimpleNamespace

from src.agents import dm_orchestrator
from src.agents.dm_orchestrator import DM_SYSTEM_PROMPT, DM_TOOLS, DMOrchestrator

TOOL_PREFIXES = (
    "get_", "add_", "update_", "move_", "kill_", "create_", "schedule_",
    "advance_", "describe_", "show_", "prompt_", "transfer_", "use_",
    "adjust_", "remove_", "activate_", "narrate", "speak", "journal",
    "roll_", "stock_", "play_",
)


def registered_tool_names() -> set[str]:
    names = set()
    for t in DM_TOOLS:
        name = getattr(t, "tool_name", None) or getattr(t, "__name__", str(t))
        # Module-provided tools (strands_tools.journal) expose a module path
        names.add(name.split(".")[-1])
    return names


def test_every_prompt_tool_reference_is_registered():
    referenced = set(re.findall(r"`([a-z_]+)`", DM_SYSTEM_PROMPT))
    tool_like = {
        r for r in referenced
        if any(r.startswith(p) or r == p.rstrip("_") for p in TOOL_PREFIXES)
    }
    missing = tool_like - registered_tool_names()
    assert not missing, (
        f"DM prompt references tools that are not in DM_TOOLS: {sorted(missing)}. "
        "Either register them or remove the prompt references."
    )


def test_new_overhaul_tools_are_registered():
    """The tools the rewritten prompt sections lean on must actually exist."""
    registered = registered_tool_names()
    for name in ("roll_check", "stock_merchant", "play_cinematic", "get_campaign"):
        assert name in registered, f"{name} is missing from DM_TOOLS"


def test_add_npc_is_not_backticked_in_dm_prompt():
    """`add_npc` lives in CREATOR_TOOLS, not DM_TOOLS.

    The DM creates NPCs through `prompt_creator_agent`; backticking `add_npc`
    would both break the sync check and push someone to register a tool the
    design deliberately keeps out of the DM's hands.
    """
    assert "`add_npc`" not in DM_SYSTEM_PROMPT


def _stub_context_sources(monkeypatch, *, canvas="", campaign=""):
    """No DB, no LLM — just the seams `_build_context` reads."""
    monkeypatch.setattr(dm_orchestrator, "get_current_location",
                        lambda pid: {"name": "The Drowned Lantern", "type": "building",
                                     "npcs_present": []})
    monkeypatch.setattr(dm_orchestrator, "get_world_clock",
                        lambda: {"day": 3, "hour": 21, "time_of_day": "night"})
    monkeypatch.setattr(dm_orchestrator, "get_player",
                        lambda pid: {"health_status": "winded", "inventory": [],
                                     "level": 4,
                                     "equipped": {"weapon": "Iron Sword (fine)"}})
    monkeypatch.setattr(dm_orchestrator, "get_active_quests", lambda: [])
    monkeypatch.setattr(dm_orchestrator, "run_world_tick", lambda: {})
    monkeypatch.setattr(dm_orchestrator, "format_world_tick_context",
                        lambda tick: "### WORLD STATE (pre-turn briefing)")
    monkeypatch.setattr(dm_orchestrator, "run_campaign_tick", lambda pid: {})
    monkeypatch.setattr(dm_orchestrator, "format_campaign_context", lambda tick: campaign)
    return SimpleNamespace(context=SimpleNamespace(player_id="p1"))


def test_build_context_accepts_canvas_notes_keyword():
    """FROZEN SEAM: server.py passes `canvas_notes=` by name."""
    sig = inspect.signature(DMOrchestrator._build_context)
    param = sig.parameters["canvas_notes"]
    assert param.default == ""


def test_build_context_orders_briefings_and_shows_level_and_gear(monkeypatch):
    fake = _stub_context_sources(
        monkeypatch,
        canvas="[SINCE YOUR LAST TURN the player acted on the canvas.]",
        campaign="### CAMPAIGN: The Long Howl",
    )
    context = DMOrchestrator._build_context(
        fake, "I look around", canvas_notes="[SINCE YOUR LAST TURN the player acted on the canvas.]"
    )

    assert "- Player level: 4" in context
    assert "- Player carries: weapon: Iron Sword (fine)" in context

    # R11: world tick → canvas ledger → campaign → player input
    assert (context.index("### WORLD STATE")
            < context.index("[SINCE YOUR LAST TURN")
            < context.index("### CAMPAIGN:")
            < context.index("Player says:"))


def test_build_context_omits_empty_briefings(monkeypatch):
    fake = _stub_context_sources(monkeypatch)
    context = DMOrchestrator._build_context(fake, "I look around")

    assert "SINCE YOUR LAST TURN" not in context
    assert "### CAMPAIGN" not in context
    assert "- Player carries: nothing notable" not in context  # stub equips a weapon
    assert context.rstrip().endswith("Player says: I look around")

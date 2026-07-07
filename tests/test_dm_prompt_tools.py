"""The DM prompt must never command tools the DM doesn't have.

This was the single biggest gameplay bug: the prompt instructed
`update_player_health`, `add_to_inventory`, etc., but none were registered,
so the game could never impose consequences.
"""

import re

from src.agents.dm_orchestrator import DM_SYSTEM_PROMPT, DM_TOOLS

TOOL_PREFIXES = (
    "get_", "add_", "update_", "move_", "kill_", "create_", "schedule_",
    "advance_", "describe_", "show_", "prompt_", "transfer_", "use_",
    "adjust_", "remove_", "activate_", "narrate", "speak", "journal",
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

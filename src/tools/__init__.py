"""Tools for agents to interact with the world."""

from src.tools.world_read import (
    get_current_location,
    get_location,
    get_npcs_at_location,
    get_npc,
    get_npc_relationship,
    get_available_destinations,
    get_faction,
    get_player_reputation,
    get_world_clock,
    get_player,
    get_recent_events,
)

from src.tools.world_write import (
    move_player,
    advance_time,
    update_npc_relationship,
    update_npc_mood,
    reveal_secret,
    update_player_reputation,
    update_player_health,
    add_to_inventory,
    remove_from_inventory,
    create_event,
)

from src.tools.narration import (
    get_console,
    set_console,
    narrate,
    speak,
    describe_location,
    show_combat_action,
    show_status_change,
    show_time_passage,
    prompt_player,
)

__all__ = [
    # world_read
    "get_current_location",
    "get_location",
    "get_npcs_at_location",
    "get_npc",
    "get_npc_relationship",
    "get_available_destinations",
    "get_faction",
    "get_player_reputation",
    "get_world_clock",
    "get_player",
    "get_recent_events",
    # world_write
    "move_player",
    "advance_time",
    "update_npc_relationship",
    "update_npc_mood",
    "reveal_secret",
    "update_player_reputation",
    "update_player_health",
    "add_to_inventory",
    "remove_from_inventory",
    "create_event",
    # narration
    "get_console",
    "set_console",
    "narrate",
    "speak",
    "describe_location",
    "show_combat_action",
    "show_status_change",
    "show_time_passage",
    "prompt_player",
]

"""Tools for agents to interact with the world."""

# Import world_read and world_write modules
from src.tools import world_read
from src.tools import world_write

# Import specific tools from world_read for backward compatibility
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

# Import specific tools from world_write for backward compatibility
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

# Import narration tools
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
    # Modules
    "world_read",
    "world_write",
    # world_read tools
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
    # world_write tools
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

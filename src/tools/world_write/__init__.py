"""World write tools - organized by entity type."""

# Location tools
from src.tools.world_write.locations import (
    add_location,
    add_location_connection,
    delete_connection,
    delete_location,
    update_location,
    update_connection,
)

# NPC tools
from src.tools.world_write.npcs import (
    add_npc,
    delete_npc,
    kill_npc,
    move_npc,
    reveal_secret,
    update_npc,
    update_npc_mood,
    update_npc_relationship,
)

# Faction tools
from src.tools.world_write.factions import (
    create_faction,
    create_faction_relationship,
    delete_faction,
    update_faction,
)

# Player tools
from src.tools.world_write.player import (
    add_to_inventory,
    move_player,
    remove_from_inventory,
    update_player_health,
    update_player_reputation,
)

# Item tools
from src.tools.world_write.items import (
    adjust_currency,
    create_item_template,
    get_inventory,
    transfer_item,
    use_item,
    spawn_item_to_user,
)

# Event tools
from src.tools.world_write.events import create_event

# World Bible tools
from src.tools.world_write.world_bible import (
    create_historical_event,
    create_world_bible,
)

# Time tools
from src.tools.world_write.time import advance_time

# Quest tools
from src.tools.world_write.quests import (
    activate_quest,
    create_quest,
    update_quest_objectives,
    update_quest_status,
)

__all__ = [
    # Locations
    "add_location",
    "update_location",
    "delete_location",
    "add_location_connection",
    "delete_connection",
    # NPCs
    "add_npc",
    "update_npc",
    "delete_npc",
    "kill_npc",
    "move_npc",
    "update_npc_mood",
    "update_npc_relationship",
    "reveal_secret",
    # Factions
    "create_faction",
    "create_faction_relationship",
    "update_faction",
    "delete_faction",
    # Player
    "move_player",
    "update_player_reputation",
    "update_player_health",
    "add_to_inventory",
    "remove_from_inventory",
    # Items
    "create_item_template",
    "get_inventory",
    "adjust_currency",
    "transfer_item",
    "use_item",
    "spawn_item_to_user",
    # Events
    "create_event",
    # World Bible
    "create_world_bible",
    "create_historical_event",
    # Time
    "advance_time",
    # Quests
    "create_quest",
    "activate_quest",
    "update_quest_status",
    "update_quest_objectives",
]

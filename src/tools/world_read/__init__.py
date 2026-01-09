"""World read tools - organized by entity type."""

# Location tools
from src.tools.world_read.locations import (
    get_all_locations,
    get_available_destinations,
    get_current_location,
    get_location,
    get_location_children,
    get_location_hierarchy,
    get_npcs_at_location,
)

# NPC tools
from src.tools.world_read.npcs import (
    get_all_npcs,
    get_npc,
    get_npc_relationship,
)

# Faction tools
from src.tools.world_read.factions import (
    get_all_factions,
    get_faction,
    get_faction_full,
    get_faction_relationships,
    get_player_reputation,
)

# Connection tools
from src.tools.world_read.connections import get_all_connections

# Event tools
from src.tools.world_read.events import get_recent_events

# World Bible tools
from src.tools.world_read.world_bible import (
    get_historical_event,
    get_historical_events,
    get_world_bible,
    get_world_bible_for_dm,
    get_world_bible_for_generation,
)

# Player tools
from src.tools.world_read.player import (
    get_player,
    get_world_clock,
)

# World State tools
from src.tools.world_read.world_state import get_world_state_summary

# Quest tools
from src.tools.world_read.quests import (
    get_active_quests,
    get_all_quests,
    get_available_quests_for_npc,
    get_quest,
)

__all__ = [
    # Locations
    "get_current_location",
    "get_location",
    "get_all_locations",
    "get_location_children",
    "get_location_hierarchy",
    "get_available_destinations",
    "get_npcs_at_location",
    # NPCs
    "get_npc",
    "get_all_npcs",
    "get_npc_relationship",
    # Factions
    "get_faction",
    "get_all_factions",
    "get_faction_full",
    "get_faction_relationships",
    "get_player_reputation",
    # Connections
    "get_all_connections",
    # Events
    "get_recent_events",
    # World Bible
    "get_world_bible",
    "get_world_bible_for_generation",
    "get_world_bible_for_dm",
    "get_historical_events",
    "get_historical_event",
    # Player
    "get_player",
    "get_world_clock",
    # World State
    "get_world_state_summary",
    # Quests
    "get_active_quests",
    "get_all_quests",
    "get_available_quests_for_npc",
    "get_quest",
]

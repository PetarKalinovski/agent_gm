"""Core type definitions for Agent GM."""

from dataclasses import dataclass, field
from typing import Any, Callable
from enum import Enum


class AgentType(Enum):
    """Types of agents in the system."""
    DM_ORCHESTRATOR = "dm_orchestrator"
    NPC_AGENT = "npc_agent"
    ECONOMY_AGENT = "economy_agent"
    CREATOR_AGENT = "creator_agent"
    COMBAT_AGENT = "combat_agent"
    WORLD_FORGE = "world_forge"


@dataclass
class EntityRef:
    """Reference to any entity with ID and name.

    Used for lightweight entity references without loading full data.
    """
    id: str
    name: str
    type: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "type": self.type}


@dataclass
class AgentContext:
    """Context passed to agent instances.

    Contains all information an agent needs to operate, including
    player context and callback handlers for tool tracking.
    """
    player_id: str
    session_id: str | None = None
    callback_handler: Any | None = None
    parent_agent: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.session_id is None:
            self.session_id = self.player_id

    def with_callback(self, callback_handler: Any) -> "AgentContext":
        """Create a new context with a different callback handler."""
        return AgentContext(
            player_id=self.player_id,
            session_id=self.session_id,
            callback_handler=callback_handler,
            parent_agent=self.parent_agent,
            extra=self.extra.copy(),
        )

    def for_subagent(self, agent_type: str, session_suffix: str | None = None) -> "AgentContext":
        """Create a context for a sub-agent, propagating callback handler."""
        new_session_id = f"{self.player_id}_{session_suffix}" if session_suffix else self.session_id
        return AgentContext(
            player_id=self.player_id,
            session_id=new_session_id,
            callback_handler=self.callback_handler,
            parent_agent=agent_type,
            extra=self.extra.copy(),
        )


@dataclass
class ToolContext:
    """Context passed to tool executions.

    Provides access to player information and database session
    for tool operations.
    """
    player_id: str
    uow: Any = None  # UnitOfWork instance
    extra: dict[str, Any] = field(default_factory=dict)


# Typed context data classes for game state

@dataclass
class PlayerContext:
    """Typed player context for agent prompts."""
    id: str
    name: str
    health_status: str
    traits: list[str]
    inventory: list[dict[str, Any]]
    active_quests: list[str]
    party_members: list[str]
    currency: int
    position_x: float = 0.0
    position_y: float = 0.0
    direction: str = "down"

    @classmethod
    def from_model(cls, player) -> "PlayerContext":
        """Create from SQLAlchemy Player model."""
        return cls(
            id=player.id,
            name=player.name,
            health_status=player.health_status or "healthy",
            traits=player.traits or [],
            inventory=player.inventory or [],
            active_quests=[q for q in (player.active_quests or [])],
            party_members=player.party_members if hasattr(player, 'party_members') else [],
            currency=player.currency if hasattr(player, 'currency') else 0,
            position_x=player.position_x or 0.0,
            position_y=player.position_y or 0.0,
            direction=player.direction or "down",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "health_status": self.health_status,
            "traits": self.traits,
            "inventory": self.inventory,
            "active_quests": self.active_quests,
            "party_members": self.party_members,
            "currency": self.currency,
        }


@dataclass
class LocationContext:
    """Typed location context for agent prompts."""
    id: str
    name: str
    type: str
    description: str
    atmosphere_tags: list[str]
    current_state: str
    visited_before: bool
    controlling_faction: str | None
    parent_id: str | None = None

    @classmethod
    def from_model(cls, location, visited_before: bool = False) -> "LocationContext":
        """Create from SQLAlchemy Location model."""
        return cls(
            id=location.id,
            name=location.name,
            type=location.type,
            description=location.description or "",
            atmosphere_tags=location.atmosphere_tags or [],
            current_state=location.current_state or "normal",
            visited_before=visited_before,
            controlling_faction=location.controlling_faction_id,
            parent_id=location.parent_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "atmosphere_tags": self.atmosphere_tags,
            "current_state": self.current_state,
            "visited_before": self.visited_before,
            "controlling_faction": self.controlling_faction,
        }


@dataclass
class ClockContext:
    """Typed time context for agent prompts."""
    day: int
    hour: int
    time_of_day: str

    @classmethod
    def from_model(cls, clock) -> "ClockContext":
        """Create from SQLAlchemy WorldClock model."""
        return cls(
            day=clock.day,
            hour=clock.hour,
            time_of_day=clock.time_of_day,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "hour": self.hour,
            "time_of_day": self.time_of_day,
        }


@dataclass
class NPCPresenceContext:
    """Minimal NPC info for location presence."""
    id: str
    name: str
    tier: str
    profession: str
    mood: str
    faction_id: str | None = None

    @classmethod
    def from_model(cls, npc) -> "NPCPresenceContext":
        """Create from SQLAlchemy NPC model."""
        return cls(
            id=npc.id,
            name=npc.name,
            tier=npc.tier.value if hasattr(npc.tier, 'value') else npc.tier,
            profession=npc.profession or "unknown",
            mood=npc.current_mood or "neutral",
            faction_id=npc.faction_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "tier": self.tier,
            "profession": self.profession,
            "mood": self.mood,
            "faction_id": self.faction_id,
        }


@dataclass
class GameContext:
    """Fully typed game context for DM prompts."""
    player: PlayerContext
    location: LocationContext | None
    clock: ClockContext
    npcs_present: list[NPCPresenceContext]
    recent_events: list[dict[str, Any]]
    faction_control: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "player": self.player.to_dict(),
            "location": self.location.to_dict() if self.location else None,
            "clock": self.clock.to_dict(),
            "npcs_present": [npc.to_dict() for npc in self.npcs_present],
            "recent_events": self.recent_events,
            "faction_control": self.faction_control,
        }

    def to_prompt_string(self) -> str:
        """Convert to formatted string for agent prompts."""
        lines = [
            f"Player: {self.player.name}",
            f"Health: {self.player.health_status}",
        ]

        if self.location:
            lines.append(f"Location: {self.location.name} ({self.location.type})")
            if self.location.atmosphere_tags:
                lines.append(f"Atmosphere: {', '.join(self.location.atmosphere_tags)}")

        lines.append(f"Time: Day {self.clock.day}, {self.clock.hour}:00 ({self.clock.time_of_day})")

        if self.npcs_present:
            npc_names = [f"{npc.name} ({npc.profession})" for npc in self.npcs_present]
            lines.append(f"NPCs present: {', '.join(npc_names)}")

        return "\n".join(lines)

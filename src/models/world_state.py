"""World state models: clock, events, relationships."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class WorldClock(Base):
    """The in-game world clock."""
    __tablename__ = "world_clock"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    day: Mapped[int] = mapped_column(Integer, default=1)
    hour: Mapped[int] = mapped_column(Integer, default=8)  # 0-23

    def advance(self, hours: float) -> None:
        """Advance the clock by the given number of hours."""
        total_hours = self.hour + hours
        self.day += int(total_hours // 24)
        self.hour = int(total_hours % 24)

    def get_time_of_day(self) -> str:
        """Get a description of the time of day."""
        if 6 <= self.hour < 12:
            return "morning"
        elif 12 <= self.hour < 17:
            return "afternoon"
        elif 17 <= self.hour < 21:
            return "evening"
        else:
            return "night"

    def __repr__(self) -> str:
        return f"<WorldClock(day={self.day}, hour={self.hour:02d}:00)>"


class NPCRelationship(Base):
    """Tracks the relationship between an NPC and the player."""
    __tablename__ = "npc_relationships"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    npc_id: Mapped[str] = mapped_column(String(36), ForeignKey("npcs.id"), nullable=False)
    player_id: Mapped[str] = mapped_column(String(36), ForeignKey("players.id"), nullable=False)

    # Relationship state
    summary: Mapped[str] = mapped_column(Text, default="")  # Compressed history
    trust_level: Mapped[int] = mapped_column(Integer, default=50)  # 0-100
    current_disposition: Mapped[str] = mapped_column(String(50), default="neutral")

    # Key moments in the relationship
    key_moments: Mapped[list] = mapped_column(JSON, default=list)

    # Recent conversation messages (last N)
    recent_messages: Mapped[list] = mapped_column(JSON, default=list)
    # Format: [{role: "player"|"npc", content: str, timestamp: str}]

    # Secrets known to player about this NPC
    revealed_secrets: Mapped[list] = mapped_column(JSON, default=list)  # Indices into NPC.secrets

    # Last interaction
    last_interaction_day: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"<NPCRelationship(npc={self.npc_id}, trust={self.trust_level})>"


class Event(Base):
    """A world event that has occurred or is scheduled."""
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # Event type
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # "macro" (faction-level), "meso" (NPC/location), "player" (involving player)

    # Timing
    occurred_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurred_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scheduled_day: Mapped[int | None] = mapped_column(Integer, nullable=True)  # For future events
    scheduled_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Entities involved
    factions_involved: Mapped[list] = mapped_column(JSON, default=list)
    locations_involved: Mapped[list] = mapped_column(JSON, default=list)
    npcs_involved: Mapped[list] = mapped_column(JSON, default=list)

    # Effects
    consequences: Mapped[list] = mapped_column(JSON, default=list)

    # Visibility
    player_visible: Mapped[bool] = mapped_column(default=True)  # Will player hear about this?
    player_witnessed: Mapped[bool] = mapped_column(default=False)  # Did player see it?
    narrated_to_player: Mapped[bool] = mapped_column(default=False)  # Has it been told?

    # Metadata
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<Event(id={self.id}, name={self.name}, type={self.event_type})>"


class Message(Base):
    """A message in the conversation history."""
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Who said it
    role: Mapped[str] = mapped_column(String(50), nullable=False)  # "player", "dm", "npc:{id}"

    # Content
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Context
    location_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    npc_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # If talking to NPC

    # Timing
    game_day: Mapped[int] = mapped_column(Integer, nullable=False)
    game_hour: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<Message(role={self.role}, content={self.content[:50]}...)>"

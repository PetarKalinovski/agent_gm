"""Faction model for organizations and groups."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Integer, String, Text
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.location import Location
    from src.models.npc import NPC


class Faction(Base):
    """A faction or organization in the game world."""
    __tablename__ = "factions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Core identity
    ideology: Mapped[str] = mapped_column(Text, nullable=False, default="")
    methods: Mapped[list] = mapped_column(JSON, default=list)  # How they operate
    aesthetic: Mapped[str] = mapped_column(Text, default="")  # Visual style description

    # Power and resources
    power_level: Mapped[int] = mapped_column(Integer, default=50)  # 1-100
    resources: Mapped[dict] = mapped_column(JSON, default=dict)  # {military: n, economic: n, influence: n}

    # Goals
    goals_short: Mapped[list] = mapped_column(JSON, default=list)
    goals_long: Mapped[list] = mapped_column(JSON, default=list)

    # Structure
    leadership: Mapped[dict] = mapped_column(JSON, default=dict)  # {leader_name, structure_type}

    # Secrets
    secrets: Mapped[list] = mapped_column(JSON, default=list)  # Hidden faction truths

    # History
    history_notes: Mapped[list] = mapped_column(JSON, default=list)

    # Relationships
    controlled_locations: Mapped[list["Location"]] = relationship(
        "Location",
        back_populates="controlling_faction"
    )
    members: Mapped[list["NPC"]] = relationship(
        "NPC",
        back_populates="faction"
    )

    def __repr__(self) -> str:
        return f"<Faction(id={self.id}, name={self.name}, power={self.power_level})>"


class FactionRelationship(Base):
    """Relationship between two factions."""
    __tablename__ = "faction_relationships"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    faction_a_id: Mapped[str] = mapped_column(String(36), nullable=False)
    faction_b_id: Mapped[str] = mapped_column(String(36), nullable=False)

    # Relationship type: allied, neutral, rival, war, vassal
    relationship_type: Mapped[str] = mapped_column(String(50), nullable=False, default="neutral")
    public_reason: Mapped[str] = mapped_column(Text, default="")  # What everyone knows
    secret_reason: Mapped[str] = mapped_column(Text, default="")  # Hidden motivation
    stability: Mapped[int] = mapped_column(Integer, default=50)  # 1-100, how likely to change

    def __repr__(self) -> str:
        return f"<FactionRelationship({self.faction_a_id} <-> {self.faction_b_id}: {self.relationship_type})>"

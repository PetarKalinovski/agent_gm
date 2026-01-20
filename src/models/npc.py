"""NPC model for non-player characters."""

import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.faction import Faction
    from src.models.location import Location


class NPCTier(enum.Enum):
    """Importance tier of NPCs."""
    MAJOR = "major"  # Fully fleshed out with goals, secrets, relationships
    MINOR = "minor"  # Role, faction, one goal, one secret
    AMBIENT = "ambient"  # Name + profession + one quirk


class NPC(Base):
    """A non-player character in the game world."""
    __tablename__ = "npcs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    tier: Mapped[NPCTier] = mapped_column(Enum(NPCTier), nullable=False, default=NPCTier.MINOR)

    # Basic info
    species: Mapped[str] = mapped_column(String(100), default="human")
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profession: Mapped[str] = mapped_column(String(100), default="")

    # Affiliation
    faction_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("factions.id"), nullable=True)

    # Location
    home_location_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    current_location_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("locations.id"), nullable=True)

    # Position within the current location (for map display)
    position_x: Mapped[float] = mapped_column(Float, default=50.0)  # 0-100 normalized
    position_y: Mapped[float] = mapped_column(Float, default=50.0)  # 0-100 normalized
    scale: Mapped[float] = mapped_column(Float, default=1.0)  # Visual scale multiplier

    # Visual assets (generated)
    sprite_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # Base sprite path
    portrait_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # Portrait for dialogue

    # Appearance and personality
    description_physical: Mapped[str] = mapped_column(Text, default="")
    description_personality: Mapped[str] = mapped_column(Text, default="")
    voice_pattern: Mapped[str] = mapped_column(Text, default="")  # Speech style notes

    # Goals and secrets
    goals: Mapped[list] = mapped_column(JSON, default=list)
    secrets: Mapped[list] = mapped_column(JSON, default=list)

    # Abilities
    skills: Mapped[list] = mapped_column(JSON, default=list)
    inventory_notable: Mapped[list] = mapped_column(JSON, default=list)  # Important items

    # State
    status: Mapped[str] = mapped_column(String(50), default="alive")  # alive, dead, missing, imprisoned
    current_mood: Mapped[str] = mapped_column(String(50), default="neutral")

    # Relationships with other NPCs
    npc_relationships: Mapped[list] = mapped_column(JSON, default=list)
    # Format: [{npc_id, type, details}]

    # Relationships
    faction: Mapped["Faction | None"] = relationship("Faction", back_populates="members")
    current_location: Mapped["Location | None"] = relationship(
        "Location",
        back_populates="npcs_here",
        foreign_keys=[current_location_id]
    )

    def __repr__(self) -> str:
        return f"<NPC(id={self.id}, name={self.name}, tier={self.tier.value})>"

    def get_full_description(self) -> str:
        """Get a full description of the NPC for context."""
        parts = [f"Name: {self.name}"]
        if self.profession:
            parts.append(f"Profession: {self.profession}")
        if self.description_physical:
            parts.append(f"Appearance: {self.description_physical}")
        if self.description_personality:
            parts.append(f"Personality: {self.description_personality}")
        if self.voice_pattern:
            parts.append(f"Speech style: {self.voice_pattern}")
        return "\n".join(parts)

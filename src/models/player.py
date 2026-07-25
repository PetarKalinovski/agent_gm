"""Player model for the player character state."""

import uuid

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base
from src.models.location import Location


class Player(Base):
    """The player character state."""
    __tablename__ = "players"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Location
    current_location_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("locations.id"), nullable=True)

    # Position within current location (0-100 normalized)
    position_x: Mapped[float] = mapped_column(Float, default=50.0)
    position_y: Mapped[float] = mapped_column(Float, default=50.0)
    facing_direction: Mapped[str] = mapped_column(String(10), default="front")  # front, back, left, right
    scale: Mapped[float] = mapped_column(Float, default=1.0)  # Visual scale multiplier

    # Visual assets
    sprite_base_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    portrait_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Character details
    description: Mapped[str] = mapped_column(Text, default="")
    background: Mapped[str] = mapped_column(Text, default="")

    # Stats (narrative, not numeric)
    traits: Mapped[list] = mapped_column(JSON, default=list)  # ["quick", "cunning", "tough"]

    # Inventory
    inventory: Mapped[list] = mapped_column(JSON, default=list)
    currency: Mapped[int] = mapped_column(Integer, default=0)

    # Reputation with factions: {faction_id: score}
    reputation: Mapped[dict] = mapped_column(JSON, default=dict)

    # Status effects
    status_effects: Mapped[list] = mapped_column(JSON, default=list)

    # Health (narrative)
    health_status: Mapped[str] = mapped_column(String(50), default="healthy")
    # "healthy", "winded", "hurt", "badly_hurt", "critical", "dead"

    # Party members (NPC IDs)
    party_members: Mapped[list] = mapped_column(JSON, default=list)

    # Active quests
    active_quests: Mapped[list] = mapped_column(JSON, default=list)
    completed_quests: Mapped[list] = mapped_column(JSON, default=list)

    # --- Progression (nullable: existing rows read as level 1 / 0 xp) ---
    level: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)
    xp: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)

    # Equipped item ids, pointing INTO Player.inventory (items are never moved
    # out of the bag — every existing inventory code path keeps working).
    # {"weapon": "iron_sword" | null, "armor": ..., "trinket": ...}
    equipment: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)

    # Item id bound to the Q hotkey (canvas quick-use)
    quick_slot_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Relationships
    current_location: Mapped["Location | None"] = relationship("Location")

    def __repr__(self) -> str:
        return f"<Player(id={self.id}, name={self.name})>"

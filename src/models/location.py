"""Location model for world geography."""

import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.faction import Faction
    from src.models.npc import NPC


class LocationType(enum.Enum):
    """Types of locations in the hierarchy."""
    # Generic hierarchy levels
    ROOT = "root"
    REGION_1 = "region_1"  # Continent / Sector
    REGION_2 = "region_2"  # Kingdom / System
    REGION_3 = "region_3"  # Province / Planet
    SETTLEMENT = "settlement"  # City / Station
    DISTRICT = "district"
    POI = "poi"  # Point of interest / Building
    INTERIOR = "interior"  # Room

    # Fantasy-specific (for display)
    WORLD = "world"
    CONTINENT = "continent"
    KINGDOM = "kingdom"
    PROVINCE = "province"
    CITY = "city"
    TOWN = "town"
    VILLAGE = "village"
    BUILDING = "building"
    ROOM = "room"

    # Sci-fi specific (for display)
    GALAXY = "galaxy"
    SECTOR = "sector"
    SYSTEM = "system"
    PLANET = "planet"
    STATION = "station"


class Location(Base):
    """A location in the game world.

    Locations form a hierarchy: root -> regions -> settlements -> districts -> POIs -> interiors.
    """
    __tablename__ = "locations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[LocationType] = mapped_column(Enum(LocationType), nullable=False)

    # Hierarchy
    parent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("locations.id"), nullable=True)
    depth: Mapped[int] = mapped_column(Integer, default=0)  # 0 = root
    children_generated: Mapped[bool] = mapped_column(Boolean, default=False)

    # Positioning (relative to parent, 0-100 normalized)
    position_x: Mapped[float] = mapped_column(Float, default=50.0)
    position_y: Mapped[float] = mapped_column(Float, default=50.0)
    position_z: Mapped[float | None] = mapped_column(Float, nullable=True)  # For 3D (space)

    # Content
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    atmosphere_tags: Mapped[list] = mapped_column(JSON, default=list)  # ["dangerous", "wealthy", "lawless"]
    economic_function: Mapped[str | None] = mapped_column(String(100), nullable=True)  # trade_hub, military, etc.
    population_level: Mapped[str | None] = mapped_column(String(50), nullable=True)  # sparse, moderate, dense
    secrets: Mapped[list] = mapped_column(JSON, default=list)  # Hidden things here

    # Control
    controlling_faction_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("factions.id"), nullable=True)

    # State
    current_state: Mapped[str] = mapped_column(String(50), default="peaceful")  # peaceful, under_siege, destroyed
    visited: Mapped[bool] = mapped_column(Boolean, default=False)
    discovered: Mapped[bool] = mapped_column(Boolean, default=False)  # Known to exist but not visited
    last_visited_day: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Generation metadata
    genre_type: Mapped[str] = mapped_column(String(50), default="fantasy")  # fantasy, scifi, modern

    # Map display settings
    display_type: Mapped[str] = mapped_column(String(20), default="pin")  # "pin" (marker) or "area" (zone/region)
    is_map_container: Mapped[bool] = mapped_column(Boolean, default=False)  # Shows a map when entered (has navigable children)
    map_image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # Path to map background image
    map_width: Mapped[int] = mapped_column(Integer, default=1000)  # Map image width in pixels
    map_height: Mapped[int] = mapped_column(Integer, default=1000)  # Map image height in pixels
    pin_icon: Mapped[str] = mapped_column(String(100), default="circle")  # Icon type: circle, star, square, custom path
    pin_color: Mapped[str] = mapped_column(String(20), default="#3388ff")  # Hex color for the pin
    pin_size: Mapped[float] = mapped_column(Float, default=15.0)  # Size of the pin marker

    # Relationships
    parent: Mapped["Location | None"] = relationship(
        "Location",
        back_populates="children",
        remote_side=[id],
        foreign_keys=[parent_id]
    )
    children: Mapped[list["Location"]] = relationship(
        "Location",
        back_populates="parent",
        foreign_keys=[parent_id]
    )
    controlling_faction: Mapped["Faction | None"] = relationship(
        "Faction",
        back_populates="controlled_locations"
    )
    npcs_here: Mapped[list["NPC"]] = relationship(
        "NPC",
        back_populates="current_location",
        foreign_keys="NPC.current_location_id"
    )

    def __repr__(self) -> str:
        return f"<Location(id={self.id}, name={self.name}, type={self.type.value})>"


class Connection(Base):
    """A travel route between two locations."""
    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    from_location_id: Mapped[str] = mapped_column(String(36), ForeignKey("locations.id"), nullable=False)
    to_location_id: Mapped[str] = mapped_column(String(36), ForeignKey("locations.id"), nullable=False)

    travel_type: Mapped[str] = mapped_column(String(50), nullable=False)  # road, hyperspace, stairs, etc.
    travel_time_hours: Mapped[float] = mapped_column(Float, nullable=False)
    difficulty: Mapped[int] = mapped_column(Integer, default=0)  # 0-100, how hard to traverse
    description: Mapped[str] = mapped_column(Text, default="")  # Description of the route
    requirements: Mapped[list] = mapped_column(JSON, default=list)  # Items/abilities needed
    bidirectional: Mapped[bool] = mapped_column(Boolean, default=True)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)  # Must be discovered
    discovered: Mapped[bool] = mapped_column(Boolean, default=False)

    # Relationships
    from_location: Mapped["Location"] = relationship(
        "Location",
        foreign_keys=[from_location_id]
    )
    to_location: Mapped["Location"] = relationship(
        "Location",
        foreign_keys=[to_location_id]
    )

    def __repr__(self) -> str:
        return f"<Connection(from={self.from_location_id}, to={self.to_location_id}, type={self.travel_type})>"

"""Item model for game items (Pydantic schema, stored as JSON)."""

from typing import Any
from pydantic import BaseModel, Field


class Item(BaseModel):
    """An item in the game world.

    Items are defined as templates and stored as JSON in inventories.
    This provides validation while keeping the flexible JSON storage approach.
    """
    id: str = Field(..., description="Unique identifier for this item type (e.g., 'health_potion')")
    name: str = Field(..., description="Display name of the item")
    type: str = Field(..., description="Type of item (consumable, weapon, armor, quest_item, misc)")
    value: int = Field(0, description="Base value in currency", ge=0)
    description: str = Field("", description="Description of the item")
    effects: dict[str, Any] = Field(default_factory=dict, description="Effects when used (e.g., {'heal': 30})")
    stackable: bool = Field(True, description="Whether multiple can stack in one inventory slot")
    quantity: int = Field(1, description="Quantity in inventory", ge=0)

    class Config:
        """Pydantic config."""
        json_schema_extra = {
            "example": {
                "id": "health_potion",
                "name": "Health Potion",
                "type": "consumable",
                "value": 50,
                "description": "Restores health when consumed",
                "effects": {"heal": 30},
                "stackable": True,
                "quantity": 1
            }
        }

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON storage."""
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Item":
        """Create Item from dictionary."""
        return cls(**data)

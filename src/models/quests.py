import uuid

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base
from src.models.location import Location

class QuestStatus:
    """Status of a quest."""
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    NOT_STARTED = "not_started"

class Quest(Base):
    """A quest assigned to the player."""
    __tablename__ = "quests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(50), default=QuestStatus.ACTIVE)
    objectives: Mapped[list] = mapped_column(JSON, default=list)  # List of objectives
    rewards: Mapped[dict] = mapped_column(JSON, default=dict)  # e
    assigned_by_npc_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("npcs.id"), nullable=True)

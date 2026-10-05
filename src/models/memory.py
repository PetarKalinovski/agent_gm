"""Markdown memory documents, kept inside the world's database."""

from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class MemoryDoc(Base):
    """One markdown document of one player character's memory.

    `doc` names it: "npc:<npc id>" (what that NPC remembers of the
    character), "chronicle", "dm_notes", "journal", "heard_names". The text
    is the same markdown the files under data/memory/ used to hold; it lives
    here so the .db is the whole save — a backup, a copy or a restore
    carries every NPC's memory along with the world it belongs to.
    """
    __tablename__ = "memory_docs"

    player_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    doc: Mapped[str] = mapped_column(String(80), primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True,
                                                           default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

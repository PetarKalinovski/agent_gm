"""Campaign spine: the authored story a character is living inside.

Code enforces, the LLM narrates: the campaign is generated once (at character
creation), stored as fact, and from then on the DM may only READ it. There is
no tool that writes a fixed truth — the DM cannot rewrite the story it was
handed, only deliver it.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


# --- Fixed menus. The generator picks LABELS from these; code owns behavior. ---

BEAT_TYPES = ("revelation", "confrontation", "deadline_event", "setpiece", "twist")

TRUTH_KINDS = ("villain_identity", "the_secret", "the_betrayal", "the_location", "the_cost")

TRIGGER_TYPES = ("time", "location", "quest_status", "milestone", "after_beat")

CINEMATIC_LABELS = (
    "none", "act_open", "revelation", "betrayal", "arrival",
    "countdown_expired", "finale",
)

BEAT_STATUSES = ("pending", "armed", "fired", "delivered", "skipped", "stalled")

CAMPAIGN_STATUSES = ("pending", "active", "won", "lost", "abandoned", "failed")


class Campaign(Base):
    """The authored story for ONE player character in ONE world."""
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    player_id: Mapped[str] = mapped_column(String(36), ForeignKey("players.id"), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    premise: Mapped[str] = mapped_column(Text, default="")          # 2-3 sentences, the pitch
    stakes_personal: Mapped[str] = mapped_column(Text, default="")  # why THIS character

    # Casting (existing world entities preferred; name kept denormalized so a
    # deleted/renamed NPC never breaks the briefing)
    villain_npc_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    villain_name: Mapped[str] = mapped_column(String(255), default="")
    villain_faction_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    successor_npc_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    successor_name: Mapped[str] = mapped_column(String(255), default="")
    ally_npc_ids: Mapped[list] = mapped_column(JSON, default=list)

    # [{"act": 1, "title": "...", "premise": "..."}, x3]
    acts: Mapped[list] = mapped_column(JSON, default=list)
    current_act: Mapped[int] = mapped_column(Integer, default=1)

    # [{"id","kind","statement","spoiler_keywords":[...],"revealed":false,
    #   "revealed_day":null}]
    fixed_truths: Mapped[list] = mapped_column(JSON, default=list)

    status: Mapped[str] = mapped_column(String(50), default="pending")
    # "pending" (generation in flight) | "active" | "won" | "lost"
    # | "abandoned" (character died) | "failed" (generation failed)
    generation_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Pacing guard: no two beats fire within CAMPAIGN_BEAT_SPACING_HOURS
    last_beat_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_beat_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)

    generated_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<Campaign(title={self.title!r}, act={self.current_act}, status={self.status})>"


class StoryBeat(Base):
    """One authored moment. Fires in code; the DM narrates it."""
    __tablename__ = "story_beats"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id: Mapped[str] = mapped_column(String(36), ForeignKey("campaigns.id"), nullable=False, index=True)

    act: Mapped[int] = mapped_column(Integer, default=1)
    sequence: Mapped[int] = mapped_column(Integer, default=0)      # order within the act
    beat_type: Mapped[str] = mapped_column(String(50), default="revelation")
    title: Mapped[str] = mapped_column(String(255), nullable=False)

    # What the DM MUST deliver, in second person, 2-4 sentences.
    directive: Mapped[str] = mapped_column(Text, default="")
    # Deadline beats only: what the DM must deliver if the deadline was missed.
    directive_missed: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Immutable facts this beat establishes (plain statements, shown to the DM
    # when the beat fires; they may never be contradicted afterwards)
    fixed_truths: Mapped[list] = mapped_column(JSON, default=list)
    # Campaign-level truth ids this beat un-seals on delivery
    reveals_truth_ids: Mapped[list] = mapped_column(JSON, default=list)
    # 3-6 distinctive tokens; code checks the turn text for them to decide
    # whether the beat was actually delivered (no LLM in the loop)
    delivery_keywords: Mapped[list] = mapped_column(JSON, default=list)

    trigger_type: Mapped[str] = mapped_column(String(50), default="time")
    trigger_data: Mapped[dict] = mapped_column(JSON, default=dict)

    # Resolved absolute fire time for `time` triggers and deadline beats
    scheduled_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scheduled_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Anti-stall: every armed beat gets a hard backstop. If the world never
    # satisfies the trigger, the beat fires anyway. The story always moves.
    fallback_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fallback_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Deadline beats: code-checkable "did the player make it?" + both outcomes
    met_condition: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    consequence_met: Mapped[list] = mapped_column(JSON, default=list)     # op list
    consequence_missed: Mapped[list] = mapped_column(JSON, default=list)  # op list

    # Short HUD countdown label, deadline beats only ("The Howl returns")
    clock_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    cinematic: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # The player-facing half of an unfired beat: one spoiler-safe line telling
    # them what to DO ("Ask around the Drowned Lantern about the howling") and
    # who/where it points at. Shown on the tracker while the beat is armed;
    # the directive above stays sealed. NULL = the beat gives no direction.
    lead: Mapped[str | None] = mapped_column(Text, nullable=True)
    lead_target: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # {"type": "npc"|"location", "id"}

    status: Mapped[str] = mapped_column(String(50), default="pending")
    variant: Mapped[str | None] = mapped_column(String(20), nullable=True)  # "met" | "missed"
    fired_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fired_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivered_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    armed_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    armed_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Plain-language record of what the engine already changed (shown to the DM
    # as ALREADY APPLIED, so it narrates instead of re-applying)
    applied_effects: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<StoryBeat(act={self.act}.{self.sequence} {self.title!r} {self.status})>"

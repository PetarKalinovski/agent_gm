"""WorldBible model for static world configuration.

The WorldBible stores the unchanging aspects of a world:
- Genre and tone
- Major lore and themes
- Rules for how things work (magic, technology, etc.)
- Style guidelines for narration

This is the "style guide" that World Forge uses to generate content
and that the DM uses to maintain consistency.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class WorldBible(Base):
    """Static world configuration and lore.

    There should be exactly one WorldBible per game world.
    """
    __tablename__ = "world_bible"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Basic info
    name: Mapped[str] = mapped_column(String(255), nullable=False)  # "The Star Wars Galaxy"
    tagline: Mapped[str] = mapped_column(Text, default="")  # "A galaxy far, far away..."

    # Genre and tone
    genre: Mapped[str] = mapped_column(String(100), nullable=False)  # "scifi", "fantasy", "modern", "post-apocalyptic"
    sub_genres: Mapped[list] = mapped_column(JSON, default=list)  # ["space opera", "military"]
    tone: Mapped[str] = mapped_column(Text, default="")  # "Dark and gritty with moments of hope"
    themes: Mapped[list] = mapped_column(JSON, default=list)  # ["redemption", "power corrupts", "found family"]

    # Setting details
    time_period: Mapped[str] = mapped_column(Text, default="")  # "19 years after the fall of the Republic"
    setting_description: Mapped[str] = mapped_column(Text, default="")  # Long description of the world

    # World rules
    technology_level: Mapped[str] = mapped_column(Text, default="")  # "Faster-than-light travel, energy weapons, droids"
    magic_system: Mapped[str] = mapped_column(Text, default="")  # "The Force - mystical energy field" or None for non-magic
    rules: Mapped[list] = mapped_column(JSON, default=list)  # ["Jedi are hunted", "Droids are common", "Credits are currency"]

    # Major lore
    major_events_history: Mapped[list] = mapped_column(JSON, default=list)  # Past events that shaped the world
    current_situation: Mapped[str] = mapped_column(Text, default="")  # What's happening RIGHT NOW
    major_conflicts: Mapped[list] = mapped_column(JSON, default=list)  # Ongoing big-picture conflicts

    # Key factions overview (high level, detailed factions are in Faction table)
    faction_overview: Mapped[str] = mapped_column(Text, default="")  # "The Empire controls most systems..."

    # Style guidelines
    narration_style: Mapped[str] = mapped_column(Text, default="")  # "Third person, cinematic, atmospheric"
    dialogue_style: Mapped[str] = mapped_column(Text, default="")  # "Terse and practical for soldiers, flowery for nobles"
    violence_level: Mapped[str] = mapped_column(String(50), default="moderate")  # "none", "mild", "moderate", "graphic"
    mature_themes: Mapped[list] = mapped_column(JSON, default=list)  # ["war", "loss"] - themes to handle carefully

    # What NOT to include
    excluded_elements: Mapped[list] = mapped_column(JSON, default=list)  # ["time travel", "multiverse"]

    # Naming conventions
    naming_conventions: Mapped[dict] = mapped_column(JSON, default=dict)
    # {"humans": "Western European style", "aliens": "Guttural consonants", "places": "Descriptive or mythological"}

    # Visual style (for image generation)
    visual_style: Mapped[str] = mapped_column(Text, default="")  # "Used future aesthetic, worn technology"
    color_palette: Mapped[list] = mapped_column(JSON, default=list)  # ["steel gray", "imperial white", "rebel orange"]

    # Player character guidelines
    pc_guidelines: Mapped[str] = mapped_column(Text, default="")  # "PC is a bounty hunter with a hidden past"
    pc_starting_situation: Mapped[str] = mapped_column(Text, default="")  # "Starting on Tatooine, owes money to Jabba"

    # Metadata
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    version: Mapped[int] = mapped_column(default=1)

    def __repr__(self) -> str:
        return f"<WorldBible(name={self.name}, genre={self.genre})>"

    def get_generation_prompt(self) -> str:
        """Generate a prompt for world content generation."""
        return f"""World: {self.name}
Genre: {self.genre} ({', '.join(self.sub_genres)})
Tone: {self.tone}
Themes: {', '.join(self.themes)}

Setting: {self.setting_description}

Current Situation: {self.current_situation}

Technology: {self.technology_level}
{f'Magic/Powers: {self.magic_system}' if self.magic_system else ''}

Rules to follow:
{chr(10).join(f'- {r}' for r in self.rules)}

Style: {self.narration_style}
Dialogue: {self.dialogue_style}

DO NOT include: {', '.join(self.excluded_elements) if self.excluded_elements else 'N/A'}

Naming: {self.naming_conventions}
"""

    def get_dm_context(self) -> str:
        """Generate context for the DM agent."""
        return f"""## World Bible: {self.name}

**Genre:** {self.genre}
**Tone:** {self.tone}
**Themes:** {', '.join(self.themes)}

**Current Situation:** {self.current_situation}

**Key Rules:**
{chr(10).join(f'- {r}' for r in self.rules[:5])}

**Narration Style:** {self.narration_style}
**Violence Level:** {self.violence_level}
"""


class HistoricalEvent(Base):
    """A historical event that shaped the world.

    Different from Event (which tracks runtime events).
    These are lore events that happened before the game started.
    """
    __tablename__ = "historical_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # When (relative to now)
    time_ago: Mapped[str] = mapped_column(String(100), nullable=False)  # "200 years ago", "last month"

    # What kind of event
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)  # war, disaster, discovery, political, cultural

    # Who was involved (can reference faction/NPC names, not IDs since those might not exist yet)
    involved_parties: Mapped[list] = mapped_column(JSON, default=list)  # ["The Empire", "The Jedi Order"]
    key_figures: Mapped[list] = mapped_column(JSON, default=list)  # ["Emperor Palpatine", "Anakin Skywalker"]

    # Where
    locations_affected: Mapped[list] = mapped_column(JSON, default=list)  # ["Coruscant", "The Outer Rim"]

    # Impact
    consequences: Mapped[list] = mapped_column(JSON, default=list)  # How it changed things

    # Visibility
    common_knowledge: Mapped[bool] = mapped_column(default=True)  # Do regular people know?

    # Physical remnants
    artifacts_left: Mapped[list] = mapped_column(JSON, default=list)  # ["Ruins of the Jedi Temple", "Memorial on Alderaan"]

    def __repr__(self) -> str:
        return f"<HistoricalEvent(name={self.name}, time_ago={self.time_ago})>"

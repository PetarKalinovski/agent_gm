"""Tools for world bible write operations."""

from typing import Any

from strands import tool

from src.models import (
    HistoricalEvent,
    WorldBible,
    WorldClock,
    get_session,
)


@tool
def create_world_bible(
    name: str,
    genre: str,
    tone: str,
    setting_description: str,
    current_situation: str,
    sub_genres: list[str] | None = None,
    themes: list[str] | None = None,
    time_period: str = "",
    technology_level: str = "",
    magic_system: str = "",
    rules: list[str] | None = None,
    major_events_history: list[str] | None = None,
    major_conflicts: list[str] | None = None,
    faction_overview: str = "",
    narration_style: str = "",
    dialogue_style: str = "",
    violence_level: str = "moderate",
    mature_themes: list[str] | None = None,
    excluded_elements: list[str] | None = None,
    naming_conventions: dict[str, str] | None = None,
    visual_style: str = "",
    color_palette: list[str] | None = None,
    pc_guidelines: str = "",
    pc_starting_situation: str = "",
) -> dict[str, Any]:
    """Create the World Bible - the static configuration for the game world.

    This should be created ONCE when setting up a new world.
    It defines the tone, rules, and style that guide all content generation.

    Args:
        name: Name of the world (e.g., "The Star Wars Galaxy").
        genre: Primary genre (scifi, fantasy, modern, post-apocalyptic).
        tone: Tone description (e.g., "Dark and gritty with moments of hope").
        setting_description: Long description of the world setting.
        current_situation: What's happening right now in the world.
        sub_genres: List of sub-genres (e.g., ["space opera", "military"]).
        themes: Major themes (e.g., ["redemption", "power corrupts"]).
        time_period: When the story takes place (e.g., "19 years after the fall").
        technology_level: Description of technology available.
        magic_system: Description of magic/powers if any.
        rules: List of world rules to follow (e.g., ["Jedi are hunted"]).
        major_events_history: List of major past events (brief summaries).
        major_conflicts: Ongoing big-picture conflicts.
        faction_overview: High-level overview of factions.
        narration_style: Style for narration (e.g., "Third person, cinematic").
        dialogue_style: Style for dialogue.
        violence_level: Level of violence (none, mild, moderate, graphic).
        mature_themes: Themes to handle carefully.
        excluded_elements: Things to NOT include.
        naming_conventions: Dict of naming rules by category.
        visual_style: Visual aesthetic for image generation.
        color_palette: List of colors for the world.
        pc_guidelines: Guidelines for player character.
        pc_starting_situation: Where/how the PC starts.

    Returns:
        Dictionary with the created World Bible's details.
    """
    with get_session() as session:
        # Check if one already exists
        existing = session.query(WorldBible).first()
        if existing:
            return {"error": "World Bible already exists. Use update_world_bible to modify."}

        bible = WorldBible(
            name=name,
            genre=genre,
            tone=tone,
            setting_description=setting_description,
            current_situation=current_situation,
            sub_genres=sub_genres or [],
            themes=themes or [],
            time_period=time_period,
            technology_level=technology_level,
            magic_system=magic_system,
            rules=rules or [],
            major_events_history=major_events_history or [],
            major_conflicts=major_conflicts or [],
            faction_overview=faction_overview,
            narration_style=narration_style,
            dialogue_style=dialogue_style,
            violence_level=violence_level,
            mature_themes=mature_themes or [],
            excluded_elements=excluded_elements or [],
            naming_conventions=naming_conventions or {},
            visual_style=visual_style,
            color_palette=color_palette or [],
            pc_guidelines=pc_guidelines,
            pc_starting_situation=pc_starting_situation,
        )
        session.add(bible)

        # Also create the world clock if it doesn't exist
        clock = session.query(WorldClock).first()
        if not clock:
            clock = WorldClock(day=1, hour=8)
            session.add(clock)

        session.commit()

        return {
            "id": bible.id,
            "name": bible.name,
            "genre": bible.genre,
            "created": True,
        }


@tool
def create_historical_event(
    name: str,
    description: str,
    time_ago: str,
    event_type: str,
    involved_parties: list[str] | None = None,
    key_figures: list[str] | None = None,
    locations_affected: list[str] | None = None,
    consequences: list[str] | None = None,
    common_knowledge: bool = True,
    artifacts_left: list[str] | None = None,
) -> dict[str, Any]:
    """Create a historical event that shaped the world.

    Historical events are lore - they happened before the game started.
    Different from runtime Events which track what happens during play.

    Args:
        name: Name of the event (e.g., "The Fall of the Republic").
        description: Full description of what happened.
        time_ago: When it happened relative to now (e.g., "200 years ago", "last month").
        event_type: Type of event (war, disaster, discovery, political, cultural).
        involved_parties: Groups/factions involved (names, not IDs).
        key_figures: Important people in the event (names).
        locations_affected: Places affected (names).
        consequences: How it changed things.
        common_knowledge: Do regular people know about this?
        artifacts_left: Physical remnants (ruins, monuments, etc.).

    Returns:
        Dictionary with the created historical event.
    """
    valid_types = ["war", "disaster", "discovery", "political", "cultural", "religious", "economic"]
    if event_type not in valid_types:
        event_type = "political"  # Default

    with get_session() as session:
        event = HistoricalEvent(
            name=name,
            description=description,
            time_ago=time_ago,
            event_type=event_type,
            involved_parties=involved_parties or [],
            key_figures=key_figures or [],
            locations_affected=locations_affected or [],
            consequences=consequences or [],
            common_knowledge=common_knowledge,
            artifacts_left=artifacts_left or [],
        )
        session.add(event)
        session.commit()

        return {
            "id": event.id,
            "name": event.name,
            "time_ago": event.time_ago,
            "event_type": event.event_type,
        }

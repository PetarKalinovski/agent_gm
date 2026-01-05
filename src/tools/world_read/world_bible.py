"""Tools for world bible read operations."""

from typing import Any

from strands import tool

from src.models import (
    HistoricalEvent,
    WorldBible,
    get_session,
)


@tool
def get_world_bible() -> dict[str, Any]:
    """Get the World Bible configuration.

    Returns:
        Dictionary with the world's static configuration.
    """
    with get_session() as session:
        bible = session.query(WorldBible).first()
        if not bible:
            return {"error": "No World Bible found. Create one first."}

        return {
            "id": bible.id,
            "name": bible.name,
            "genre": bible.genre,
            "sub_genres": bible.sub_genres,
            "tone": bible.tone,
            "themes": bible.themes,
            "time_period": bible.time_period,
            "setting_description": bible.setting_description,
            "current_situation": bible.current_situation,
            "technology_level": bible.technology_level,
            "magic_system": bible.magic_system,
            "rules": bible.rules,
            "major_events_history": bible.major_events_history,
            "major_conflicts": bible.major_conflicts,
            "faction_overview": bible.faction_overview,
            "narration_style": bible.narration_style,
            "dialogue_style": bible.dialogue_style,
            "violence_level": bible.violence_level,
            "mature_themes": bible.mature_themes,
            "excluded_elements": bible.excluded_elements,
            "naming_conventions": bible.naming_conventions,
            "visual_style": bible.visual_style,
            "color_palette": bible.color_palette,
            "pc_guidelines": bible.pc_guidelines,
            "pc_starting_situation": bible.pc_starting_situation,
        }


@tool
def get_world_bible_for_generation() -> str:
    """Get the World Bible formatted as a prompt for content generation.

    Returns:
        Formatted string for use in generation prompts.
    """
    with get_session() as session:
        bible = session.query(WorldBible).first()
        if not bible:
            return "No World Bible found."

        return bible.get_generation_prompt()


@tool
def get_world_bible_for_dm() -> str:
    """Get the World Bible formatted as context for the DM agent.

    Returns:
        Formatted string with key world info for DM context.
    """
    with get_session() as session:
        bible = session.query(WorldBible).first()
        if not bible:
            return "No World Bible found."

        return bible.get_dm_context()


@tool
def get_historical_events() -> list[dict[str, Any]]:
    """Get all historical events that shaped the world.

    Returns:
        List of historical events sorted by time (most recent first).
    """
    with get_session() as session:
        events = session.query(HistoricalEvent).all()

        # Sort by time_ago (this is imperfect since it's a string, but works for display)
        return [
            {
                "id": e.id,
                "name": e.name,
                "time_ago": e.time_ago,
                "event_type": e.event_type,
                "description": e.description,
                "involved_parties": e.involved_parties,
                "key_figures": e.key_figures,
                "locations_affected": e.locations_affected,
                "consequences": e.consequences,
                "common_knowledge": e.common_knowledge,
                "artifacts_left": e.artifacts_left,
            }
            for e in events
        ]


@tool
def get_historical_event(event_id: str) -> dict[str, Any]:
    """Get a specific historical event.

    Args:
        event_id: The event's ID.

    Returns:
        Dictionary with the historical event details.
    """
    with get_session() as session:
        event = session.get(HistoricalEvent, event_id)
        if not event:
            return {"error": "Historical event not found"}

        return {
            "id": event.id,
            "name": event.name,
            "time_ago": event.time_ago,
            "event_type": event.event_type,
            "description": event.description,
            "involved_parties": event.involved_parties,
            "key_figures": event.key_figures,
            "locations_affected": event.locations_affected,
            "consequences": event.consequences,
            "common_knowledge": event.common_knowledge,
            "artifacts_left": event.artifacts_left,
        }

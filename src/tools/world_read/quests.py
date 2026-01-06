"""Quest read tools."""

from strands import tool

from src.models.base import get_session
from src.models.quests import Quest, QuestStatus


@tool
def get_active_quests() -> list[dict]:
    """Get all active quests the player is currently working on.

    Returns:
        List of active quest dictionaries with id, title, description, objectives, rewards.
    """
    with get_session() as session:
        quests = session.query(Quest).filter(
            Quest.status == QuestStatus.ACTIVE
        ).all()

        return [
            {
                "id": q.id,
                "title": q.title,
                "description": q.description,
                "objectives": q.objectives,
                "rewards": q.rewards,
                "assigned_by_npc_id": q.assigned_by_npc_id,
            }
            for q in quests
        ]


@tool
def get_available_quests_for_npc(npc_id: str) -> list[dict]:
    """Get quests that a specific NPC can offer to the player (not yet started).

    Use this when an NPC might have a task to give the player.

    Args:
        npc_id: The NPC's ID.

    Returns:
        List of available quest dictionaries that this NPC can offer.
    """
    with get_session() as session:
        quests = session.query(Quest).filter(
            Quest.status == QuestStatus.NOT_STARTED,
            Quest.assigned_by_npc_id == npc_id
        ).all()

        return [
            {
                "id": q.id,
                "title": q.title,
                "description": q.description,
                "objectives": q.objectives,
                "rewards": q.rewards,
            }
            for q in quests
        ]


@tool
def get_quest(quest_id: str) -> dict:
    """Get a specific quest by ID.

    Args:
        quest_id: The quest's ID.

    Returns:
        Quest dictionary or error.
    """
    with get_session() as session:
        quest = session.query(Quest).filter(Quest.id == quest_id).first()

        if not quest:
            return {"error": f"Quest {quest_id} not found"}

        return {
            "id": quest.id,
            "title": quest.title,
            "description": quest.description,
            "status": quest.status,
            "objectives": quest.objectives,
            "rewards": quest.rewards,
            "assigned_by_npc_id": quest.assigned_by_npc_id,
        }


@tool
def get_all_quests() -> list[dict]:
    """Get all quests regardless of status (for DM reference).

    Returns:
        List of all quest dictionaries.
    """
    with get_session() as session:
        quests = session.query(Quest).all()

        return [
            {
                "id": q.id,
                "title": q.title,
                "description": q.description,
                "status": q.status,
                "objectives": q.objectives,
                "rewards": q.rewards,
                "assigned_by_npc_id": q.assigned_by_npc_id,
            }
            for q in quests
        ]

"""Quest write tools."""

from strands import tool

from src.models.base import get_session
from src.models.quests import Quest, QuestStatus


@tool
def create_quest(
    title: str,
    description: str,
    objectives: list[str],
    rewards: dict | None = None,
    assigned_by_npc_id: str | None = None,
    start_active: bool = False,
) -> dict:
    """Create a new quest.

    Args:
        title: Quest title (e.g., "Find the Lost Artifact").
        description: Full description of what needs to be done.
        objectives: List of objectives to complete.
        rewards: Optional dict of rewards (e.g., {"gold": 100, "items": ["sword"]}).
        assigned_by_npc_id: Optional NPC who can assign this quest.
        start_active: If True, quest starts active. If False (default), quest is
                      not_started and must be activated when NPC offers it.

    Returns:
        The created quest data.
    """
    with get_session() as session:
        quest = Quest(
            title=title,
            description=description,
            objectives=objectives,
            rewards=rewards or {},
            assigned_by_npc_id=assigned_by_npc_id,
            status=QuestStatus.ACTIVE if start_active else QuestStatus.NOT_STARTED,
        )
        session.add(quest)
        session.commit()

        return {
            "id": quest.id,
            "title": quest.title,
            "status": quest.status,
            "message": f"Quest '{title}' created successfully",
        }


@tool
def update_quest_status(quest_id: str, status: str) -> dict:
    """Update a quest's status.

    Args:
        quest_id: The quest's ID.
        status: New status - "active", "completed", "failed", or "not_started".

    Returns:
        Updated quest data or error.
    """
    valid_statuses = [QuestStatus.ACTIVE, QuestStatus.COMPLETED, QuestStatus.FAILED, QuestStatus.NOT_STARTED]
    if status not in valid_statuses:
        return {"error": f"Invalid status. Must be one of: {valid_statuses}"}

    with get_session() as session:
        quest = session.query(Quest).filter(Quest.id == quest_id).first()

        if not quest:
            return {"error": f"Quest {quest_id} not found"}

        quest.status = status
        session.commit()

        return {
            "id": quest.id,
            "title": quest.title,
            "status": quest.status,
            "message": f"Quest '{quest.title}' marked as {status}",
        }


@tool
def activate_quest(quest_id: str) -> dict:
    """Activate a quest when an NPC offers it to the player.

    Use this when an NPC reveals or offers a pre-seeded quest to the player.
    Changes status from not_started to active.

    Args:
        quest_id: The quest's ID.

    Returns:
        Activated quest data or error.
    """
    with get_session() as session:
        quest = session.query(Quest).filter(Quest.id == quest_id).first()

        if not quest:
            return {"error": f"Quest {quest_id} not found"}

        if quest.status != QuestStatus.NOT_STARTED:
            return {"error": f"Quest '{quest.title}' is already {quest.status}"}

        quest.status = QuestStatus.ACTIVE
        session.commit()

        return {
            "id": quest.id,
            "title": quest.title,
            "description": quest.description,
            "objectives": quest.objectives,
            "rewards": quest.rewards,
            "status": quest.status,
            "message": f"Quest '{quest.title}' is now active!",
        }


@tool
def update_quest_objectives(quest_id: str, objectives: list[str]) -> dict:
    """Update a quest's objectives (e.g., mark some as done).

    Args:
        quest_id: The quest's ID.
        objectives: Updated list of objectives (use strikethrough or [DONE] prefix for completed ones).

    Returns:
        Updated quest data or error.
    """
    with get_session() as session:
        quest = session.query(Quest).filter(Quest.id == quest_id).first()

        if not quest:
            return {"error": f"Quest {quest_id} not found"}

        quest.objectives = objectives
        session.commit()

        return {
            "id": quest.id,
            "title": quest.title,
            "objectives": quest.objectives,
            "message": f"Quest '{quest.title}' objectives updated",
        }

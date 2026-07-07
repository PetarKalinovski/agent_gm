"""Quest write tools."""

from strands import tool

from src.models.base import get_session
from src.models.quests import Quest, QuestStatus
from src.models.player import Player
from src.models.item import Item
from src.tools.world_write.player import _normalize_inventory


def _get_player(session, player_id: str | None = None):
    """Resolve the player: by id, or the sole player in a single-player world."""
    if player_id:
        return session.get(Player, player_id)
    players = session.query(Player).all()
    return players[0] if len(players) == 1 else None


def _sync_player_quest_lists(player, quest_id: str, status: str) -> None:
    """Keep Player.active_quests/completed_quests in sync with quest status."""
    active = [q for q in (player.active_quests or []) if q != quest_id]
    completed = [q for q in (player.completed_quests or []) if q != quest_id]

    if status == QuestStatus.ACTIVE:
        active.append(quest_id)
    elif status == QuestStatus.COMPLETED:
        completed.append(quest_id)
    # failed/not_started: present in neither list

    player.active_quests = active
    player.completed_quests = completed


def _apply_rewards(session, player, rewards: dict) -> list[str]:
    """Apply quest rewards to the player. Returns human-readable summary lines."""
    applied = []
    if not rewards or not player:
        return applied

    # Currency: accept "currency" or "gold"
    amount = rewards.get("currency", rewards.get("gold"))
    if isinstance(amount, (int, float)) and amount:
        player.currency = max(0, (player.currency or 0) + int(amount))
        applied.append(f"+{int(amount)} currency (now {player.currency})")

    # Items: list of names or item dicts
    items = rewards.get("items") or []
    if isinstance(items, list) and items:
        inventory = _normalize_inventory(player.inventory)
        for entry in items:
            try:
                if isinstance(entry, str):
                    item = Item(id=entry.lower().replace(" ", "_"), name=entry, type="misc")
                elif isinstance(entry, dict):
                    entry.setdefault("id", str(entry.get("name", "item")).lower().replace(" ", "_"))
                    entry.setdefault("name", entry["id"])
                    entry.setdefault("type", "misc")
                    item = Item.from_dict(entry)
                else:
                    continue
            except Exception:
                continue
            for existing in inventory:
                if existing.get("id") == item.id:
                    existing["quantity"] = existing.get("quantity", 1) + item.quantity
                    break
            else:
                inventory.append(item.to_dict())
            applied.append(f"received {item.name}")
        player.inventory = inventory

    # Reputation: {faction_id: delta}
    reputation = rewards.get("reputation") or {}
    if isinstance(reputation, dict) and reputation:
        rep = player.reputation.copy() if player.reputation else {}
        for faction_id, delta in reputation.items():
            if isinstance(delta, (int, float)):
                rep[faction_id] = max(0, min(100, rep.get(faction_id, 50) + int(delta)))
                applied.append(f"reputation with {faction_id}: {rep[faction_id]}")
        player.reputation = rep

    return applied


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

        if start_active:
            player = _get_player(session)
            if player:
                _sync_player_quest_lists(player, quest.id, QuestStatus.ACTIVE)

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

        # Keep the player's quest lists in sync, and apply rewards on
        # completion — in the same transaction
        rewards_applied = []
        player = _get_player(session)
        if player:
            _sync_player_quest_lists(player, quest.id, status)
            if status == QuestStatus.COMPLETED:
                rewards_applied = _apply_rewards(session, player, quest.rewards or {})

        session.commit()

        result = {
            "id": quest.id,
            "title": quest.title,
            "status": quest.status,
            "message": f"Quest '{quest.title}' marked as {status}",
        }
        if rewards_applied:
            result["rewards_applied"] = rewards_applied
            result["message"] += f". Rewards applied: {', '.join(rewards_applied)} — narrate this to the player."
        return result


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

        player = _get_player(session)
        if player:
            _sync_player_quest_lists(player, quest.id, QuestStatus.ACTIVE)

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

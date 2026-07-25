"""Quest write tools."""

from strands import tool

from src.models.base import get_session
from src.models.quests import Quest, QuestStatus
from src.models.player import Player
from src.models.item import Item
from src.services.progression import (
    auto_equip,
    catalog_price,
    grant_xp,
    strip_authored_xp,
    xp_for_quest,
)
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
    # The DM authors quest rewards. XP is NOT theirs to author — strip and log.
    rewards = strip_authored_xp(rewards)
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
        gear_ids = []
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
            # Reward gear is real gear: the catalog owns its price, and it
            # goes on the character if the slot is free (same as pickups)
            price = catalog_price(item.to_dict())
            if price is not None:
                item.value = price
                gear_ids.append(item.id)
            for existing in inventory:
                if existing.get("id") == item.id:
                    existing["quantity"] = existing.get("quantity", 1) + item.quantity
                    break
            else:
                inventory.append(item.to_dict())
            applied.append(f"received {item.name}")
        player.inventory = inventory
        for item_id in gear_ids:
            try:
                equipped = auto_equip(session, player, item_id)
                if equipped:
                    applied.append(f"equipped {item_id.replace('_', ' ')}")
            except Exception:
                pass

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


def _complete_quest(session, quest, player) -> dict:
    """Complete a quest exactly once: status flip, list sync, rewards, XP.

    The ONE completion path. Both the DM tool (`update_quest_status`) and the
    campaign engine's `complete_quest` beat op call it, so an engine-driven
    completion pays identical rewards and identical XP (integration doc R12).

    Mutates inside the CALLER'S open transaction and never commits — rewards
    and the XP grant must land or roll back together.

    Returns {"already_completed", "rewards_applied", "progression"}.
    """
    if quest.status == QuestStatus.COMPLETED:
        # Idempotence: a retried completion must not pay twice
        return {"already_completed": True, "rewards_applied": [], "progression": {}}

    quest.status = QuestStatus.COMPLETED

    rewards_applied: list[str] = []
    progression: dict = {}
    if player:
        _sync_player_quest_lists(player, quest.id, QuestStatus.COMPLETED)
        rewards_applied = _apply_rewards(session, player, quest.rewards or {})
        progression = grant_xp(session, player, xp_for_quest(quest), "quest")

    return {
        "already_completed": False,
        "rewards_applied": rewards_applied,
        "progression": progression,
    }


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

        result = {
            "id": quest.id,
            "title": quest.title,
            "status": quest.status,
            "message": f"Quest '{title}' created successfully",
        }
        if start_active:
            result["event"] = "quest_activated"
        return result


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

        # Keep the player's quest lists in sync, and apply rewards + XP on
        # completion — in the same transaction
        rewards_applied = []
        progression = {}
        player = _get_player(session)
        if status == QuestStatus.COMPLETED:
            outcome = _complete_quest(session, quest, player)
            rewards_applied = outcome["rewards_applied"]
            progression = outcome["progression"]
        else:
            quest.status = status
            if player:
                _sync_player_quest_lists(player, quest.id, status)

        session.commit()

        result = {
            "id": quest.id,
            "title": quest.title,
            "status": quest.status,
            "message": f"Quest '{quest.title}' marked as {status}",
        }
        # Structured frontend events: quest completion plays a triumph
        # stinger + banner; failure shows a banner
        if status == QuestStatus.COMPLETED:
            result["event"] = "quest_completed"
        elif status == QuestStatus.FAILED:
            result["event"] = "quest_failed"
        if rewards_applied:
            result["rewards_applied"] = rewards_applied
            result["message"] += f". Rewards applied: {', '.join(rewards_applied)} — narrate this to the player."
        # Rides to the frontend as xp_gain / level_up — never narrated as numbers
        if progression:
            result["progression"] = progression
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
            "event": "quest_activated",
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

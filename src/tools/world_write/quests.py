"""Quest write tools."""

from typing import Any

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
from src.services import objectives as objectives_mod


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
    objectives: list[Any],
    rewards: dict | None = None,
    assigned_by_npc_id: str | None = None,
    start_active: bool = False,
    scale: str | None = None,
) -> dict:
    """Create a new quest.

    Args:
        title: Quest title (e.g., "Find the Lost Artifact").
        description: Full description of what needs to be done.
        objectives: List of objective objects. Each is
            {"text": "Find Captain Coldiron at the Watch House",
             "kind": "talk_to", "target_id": "<the real npc id>"}
            kind is one of "go_to" (target_id = a location id), "talk_to"
            (an npc id), "obtain" (item_name), "kill" (an npc id), "deliver"
            (item_name + an npc id), "custom" (no target; you mark it done).
            The engine completes every kind but "custom" when it actually
            happens, and the first objective's target puts a marker on the
            player's screen — give it a real id. Plain strings still work
            (they become "custom").
        rewards: Optional dict of rewards (e.g., {"gold": 100, "items": ["sword"]}).
        assigned_by_npc_id: Optional NPC who can assign this quest.
        start_active: If True, quest starts active. If False (default), quest is
                      not_started and must be activated when NPC offers it.
        scale: How big this is — "errand", "job", "mission" or "saga". The
            engine pays growth from the word; leave it out for an ordinary job.

    Returns:
        The created quest data.
    """
    with get_session() as session:
        scale_label = str(scale or "").strip().lower() or None
        if scale_label not in (None, "errand", "job", "mission", "saga"):
            scale_label = None
        quest = Quest(
            title=title,
            description=description,
            objectives=objectives_mod.normalize_objectives(objectives, session=session),
            rewards=rewards or {},
            assigned_by_npc_id=assigned_by_npc_id,
            status=QuestStatus.ACTIVE if start_active else QuestStatus.NOT_STARTED,
            scale=scale_label,
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
            "objectives": objectives_mod.objectives_public(quest),
            "message": f"Quest '{title}' created successfully",
        }
        if start_active:
            result["event"] = "quest_activated"
        untargeted = [o["text"] for o in result["objectives"]
                      if o["kind"] != "custom" and not o["target_id"]]
        if untargeted:
            result["warning"] = (
                "These objectives name no real target, so the player's screen cannot "
                f"point at them: {untargeted}. Pass a real npc/location id next time."
            )
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
        # Entering play repairs a legacy seed: strings become objects, names
        # become ids, so the tracker has something to point at
        quest.objectives = objectives_mod.normalize_objectives(
            quest.objectives, previous=quest.objectives, session=session
        )

        player = _get_player(session)
        if player:
            _sync_player_quest_lists(player, quest.id, QuestStatus.ACTIVE)

        session.commit()

        return {
            "id": quest.id,
            "title": quest.title,
            "description": quest.description,
            "objectives": objectives_mod.objectives_public(quest),
            "rewards": quest.rewards,
            "status": quest.status,
            "event": "quest_activated",
            "message": f"Quest '{quest.title}' is now active!",
        }


@tool
def update_quest_objectives(quest_id: str, objectives: list[Any]) -> dict:
    """Add, reword, or mark a "custom" objective done. The engine completes
    every other kind itself when it happens — you cannot un-complete one.

    Args:
        quest_id: The quest's ID.
        objectives: The full list, same shape as create_quest: objects with
            "text", "kind", "target_id"/"item_name", and "done": true for a
            custom objective the player has just satisfied. Plain strings
            still work.

    Returns:
        The merged objectives and whether every one is now met.
    """
    with get_session() as session:
        quest = session.query(Quest).filter(Quest.id == quest_id).first()

        if not quest:
            return {"error": f"Quest {quest_id} not found"}

        merged = objectives_mod.normalize_objectives(
            objectives, previous=quest.objectives, session=session
        )
        quest.objectives = merged
        session.commit()

        result = {
            "id": quest.id,
            "title": quest.title,
            "objectives": objectives_mod.objectives_public(quest),
            "all_done": objectives_mod.all_done(merged),
            "message": f"Quest '{quest.title}' objectives updated",
        }
        if result["all_done"] and quest.status == QuestStatus.ACTIVE:
            result["message"] += (
                ". Every objective is met — if the quest is finished, call "
                "update_quest_status(status=\"completed\")."
            )
        return result

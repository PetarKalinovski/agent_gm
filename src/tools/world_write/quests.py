"""Quest write tools."""

import re
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
    quest_completion_xp,
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


# A reward's `kind` in @give's words: (Item.type, gear slot)
_REWARD_KINDS = {
    "weapon": ("weapon", "weapon"), "armor": ("armor", "armor"), "trinket": ("misc", "trinket"),
    "potion": ("consumable", None), "key": ("quest_item", None), "clue": ("quest_item", None),
    "misc": ("misc", None),
}


def reward_item(entry: Any) -> Item | None:
    """One `rewards["items"]` entry as an Item.

    A plain string is plain stuff, as it always was. A dict may be a full
    Item, or say it the way @give does — {name, kind, tier, about} — so an
    authored reward is real gear. fleet4 (all three sessions): the seed's
    'Wolf-Tooth Charm' arrived as an inert misc, the DM tried to fix it with
    a second @give, and the pack ended on two useless charms. The quest's own
    dict is never mutated (it is the quest row's JSON)."""
    if isinstance(entry, str):
        name = entry.strip()
        return Item(id=name.lower().replace(" ", "_"), name=name, type="misc") if name else None
    if not isinstance(entry, dict):
        return None
    data = dict(entry)
    kind = str(data.pop("kind", "") or "").strip().lower()
    about = data.pop("about", None)
    potion = data.pop("potion", None)
    if kind in _REWARD_KINDS:
        item_type, slot = _REWARD_KINDS[kind]
        data.setdefault("type", item_type)
        if slot:
            data.setdefault("slot", slot)
            data["tier"] = data.get("tier") or "common"
        if kind == "potion":
            data["effect_tag"] = data.get("effect_tag") or potion or "heal_light"
    if about and not data.get("description"):
        data["description"] = str(about)
    data.setdefault("id", str(data.get("name", "item")).lower().replace(" ", "_"))
    data.setdefault("name", data["id"])
    data.setdefault("type", "misc")
    return Item.from_dict(data)


def reward_coin(rewards: Any) -> int:
    """The coin a quest's completion pays (`_apply_rewards` reads the same keys)."""
    if not isinstance(rewards, dict):
        return 0
    for key in ("currency", "gold", "coin", "coins"):
        value = rewards.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return 0


def reward_advanced(rewards: Any) -> dict[str, Any]:
    """{"currency": N, "items": [names]}: the part of a quest's reward the
    giver already paid up front (`commands._note_advance` records it as
    rewards["advanced"]). fleet6-tamsin T6 was paid 10 coin and the tincture
    before the job; T11's completion paid both again."""
    raw = rewards.get("advanced") if isinstance(rewards, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    try:
        coin = max(0, int(raw.get("currency") or 0))
    except (TypeError, ValueError):
        coin = 0
    items = [" ".join(str(i).split()) for i in raw.get("items") or [] if isinstance(i, str) and i.strip()]
    return {"currency": coin, "items": items}


def reward_balance(rewards: Any) -> dict:
    """What completing the quest still pays: the reward minus the advance
    (and minus the advance's own record). A quest never paid up front gets
    its reward back as written."""
    if not isinstance(rewards, dict):
        return {}
    out = {k: v for k, v in rewards.items() if k != "advanced"}
    advanced = reward_advanced(rewards)
    if advanced["currency"]:
        for key in ("currency", "gold", "coin", "coins"):
            value = out.get(key)
            if isinstance(value, (int, float)) and value > 0:
                out[key] = max(0, int(value) - advanced["currency"])
                break
    if advanced["items"]:
        owed = [name.lower() for name in advanced["items"]]
        kept = []
        for entry in out.get("items") or []:
            name = " ".join(str((entry.get("name") if isinstance(entry, dict) else entry) or "").split()).lower()
            if name in owed:
                owed.remove(name)       # one advanced item settles one entry
                continue
            kept.append(entry)
        out["items"] = kept
    return out


def reward_factions(session, rewards: Any) -> dict[str, str]:
    """{faction id: name} for `reward_words`, read only when the reward pays standing."""
    from src.models.faction import Faction

    if not (isinstance(rewards, dict) and isinstance(rewards.get("reputation"), dict) and rewards["reputation"]):
        return {}
    return {f.id: f.name for f in session.query(Faction).all()}


def _gloss(note: str) -> str:
    """What a reward item is, short: its description's first sentence
    ("A wolf's fang bound in brass wire on a Watch cord." -> "a wolf's fang
    bound in brass wire on a Watch cord"), at most about 80 letters."""
    first = re.split(r"(?<=[.!?])\s", note.strip(), maxsplit=1)[0].rstrip(".!?")
    if len(first) > 80:
        first = first[:80].rsplit(" ", 1)[0].rstrip(",;:—–- ")
    words = first.split()
    if words and words[0] in ("A", "An", "The"):
        first = first[0].lower() + first[1:]
    return first


def _with_article(name: str) -> str:
    if not name or name.split()[0].lower() in ("a", "an", "the", "some") or name[0].isdigit():
        return name
    return f"{'an' if name[0].lower() in 'aeiou' else 'a'} {name}"


def reward_words(rewards: Any, *, about: bool = False, factions: dict[str, str] | None = None) -> str:
    """'60 coin and a Wolf-Tooth Charm' — what completing a quest pays, in the
    words a quest-giver names a price with. fleet5-tamsin T0: the thief was
    offered the job with no word of pay (the quest carried 60 coin and a
    charm), so a money-hungry persona had nothing to bargain over.

    `about` adds each item's authored description: fleet5-bram T18 narrated
    the Wolf-Tooth Charm as "a charm strung on sinew" while the pack held "a
    wolf's fang bound in brass wire". `factions` ({id: name}) names whose
    goodwill the reputation reward buys. Experience is the engine's and is
    never named. Empty when the quest pays nothing.

    Part of it paid up front, the words are the balance and then the
    advance: "a Yarrow Tincture (reward already paid up front: 10 coin)".

    Without `about`, an item that has a description still says it is the
    player's to keep, and what it is: fleet7-bram T0/T1 (and fleet6-bram T0)
    priced the job as "sixty coin, a tooth off whatever's doing this … a
    tooth for proof" from a bare "a Wolf-Tooth Charm" — the charm is handed
    TO the player; the proof they bring back is another thing. "The
    player's", not "theirs": the dossier's "Work they can offer" makes
    "they" the giver, and "theirs to keep" the giver's charm. fleet8-wenna
    T0 still heard "a wolf's fang bound in brass wire" as a bounty ("a tooth
    off the thing that's doing this bound up proper"): the item is said to be
    handed over finished, by its own name — since fleet9-bram T0 that name in
    quotes, as the words to say ('say "a Wolf-Tooth Charm"'), and the audit
    flags a pitch that says otherwise (turn_audit.reward_misnamed)."""
    if not isinstance(rewards, dict):
        return ""
    advanced = reward_advanced(rewards)
    if advanced["currency"] or advanced["items"]:
        rest = reward_words(reward_balance(rewards), about=about, factions=factions)
        paid = reward_words({"currency": advanced["currency"], "items": advanced["items"]})
        return f"{rest or 'nothing more'} (reward already paid up front: {paid})"
    parts: list[str] = []
    coin = reward_coin(rewards)
    if coin:
        parts.append(f"{coin} coin")
    for entry in rewards.get("items") or []:
        if isinstance(entry, dict):
            name = " ".join(str(entry.get("name") or "").split())
            qty = entry.get("quantity")
            what = f"{qty} {name}" if isinstance(qty, int) and qty > 1 else _with_article(name)
            note = " ".join(str(entry.get("about") or entry.get("description") or "").split())
            # The name in quotes, the words to say (fleet9-bram T0, the third
            # round running: "a tooth off one of the brutes strung on a Watch
            # cord" for the Wolf-Tooth Charm — the gloss alone read as a bounty)
            say = f'say "{what}"; handed over finished, the player\'s to keep'
            if name:
                parts.append(f"{what} ({note})" if about and note else
                             f"{what} ({say} — {_gloss(note)})" if note
                             else f"{what} ({say})")
        elif str(entry or "").strip():
            parts.append(_with_article(" ".join(str(entry).split())))
    reputation = rewards.get("reputation")
    if isinstance(reputation, dict) and reputation:
        named = [factions[f] if factions[f].lower().startswith("the ") else f"the {factions[f]}"
                 for f, delta in reputation.items()
                 if factions and factions.get(f) and isinstance(delta, (int, float)) and delta > 0]
        parts.append(f"standing with {' and '.join(dict.fromkeys(named))}" if named
                     else "standing with the quest-giver's faction")
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _apply_rewards(session, player, rewards: dict) -> list[str]:
    """Apply quest rewards to the player. Returns human-readable summary lines."""
    applied = []
    # The DM authors quest rewards. XP is NOT theirs to author — strip and log.
    rewards = strip_authored_xp(rewards)
    if not rewards or not player:
        return applied
    # What the giver paid up front is never paid again: only the balance
    # (fleet6-tamsin T6 advance, T11 completion: 45 coin, two tinctures)
    advanced = reward_advanced(rewards)
    rewards = reward_balance(rewards)
    if advanced["currency"] or advanced["items"]:
        applied.append(f"already paid up front, not again: "
                       f"{reward_words({'currency': advanced['currency'], 'items': advanced['items']})}")

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
                item = reward_item(entry)
            except Exception:
                continue
            if item is None:
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


# A job done for someone is trust earned with them. fleet5-bram: Coldiron sat
# at trust 55, 'measuring', after Bram finished his mission and handed over
# the proof — below the 60 his trust-gated secret needs (standing.SECRET_TRUST),
# so the clue he was holding never came out.
QUEST_GIVER_TRUST = 10


def _thank_giver(session, quest, player) -> dict | None:
    """The giver trusts the player more: +QUEST_GIVER_TRUST, capped at 100,
    and a talk_to step aimed at them closes if they are there to pay out
    (`objectives.close_giver_talk`).

    The row is the relationship, not an introduction (dialogue: who knows
    whom), so a giver without one gets it here — as a stranger whose name the
    player still has not heard, if so. fleet6 (all three runs): Coldiron,
    Piper and Maera each paid out with no row, and the thanks did nothing.
    In the caller's transaction, like the rest of the completion —
    `update_npc_relationship` opens and commits its own session, which would
    lock against this one. Returns {npc_id, trust} or None."""
    if not player or not quest.assigned_by_npc_id:
        return None
    from src.models import NPC
    from src.models.world_state import WorldClock
    from src.services import dialogue

    if session.get(NPC, quest.assigned_by_npc_id) is None:
        return None
    try:
        objectives_mod.close_giver_talk(session, quest, player)
    except Exception as e:      # the thanks never waits on the tracker
        import logging

        logging.getLogger(__name__).warning(f"giver's talk_to step not closed for {quest.id}: {e}")
    rel = dialogue.ensure_relationship(session, player.id, quest.assigned_by_npc_id)
    rel.trust_level = max(0, min(100, (rel.trust_level or 0) + QUEST_GIVER_TRUST))
    clock = session.query(WorldClock).first()
    if clock:
        rel.last_interaction_day = clock.day
    return {"npc_id": rel.npc_id, "trust": rel.trust_level}


def _complete_quest(session, quest, player) -> dict:
    """Complete a quest exactly once: status flip, list sync, rewards, XP,
    and the giver's trust (`_thank_giver`).

    The ONE completion path. Both the DM tool (`update_quest_status`) and the
    campaign engine's `complete_quest` beat op call it, so an engine-driven
    completion pays identical rewards and identical XP (integration doc R12).

    Mutates inside the CALLER'S open transaction and never commits — rewards
    and the XP grant must land or roll back together.

    Returns {"already_completed", "rewards_applied", "progression", "giver_trust"}.
    """
    if quest.status == QuestStatus.COMPLETED:
        # Idempotence: a retried completion must not pay twice
        return {"already_completed": True, "rewards_applied": [], "progression": {}, "giver_trust": None}

    quest.status = QuestStatus.COMPLETED

    rewards_applied: list[str] = []
    progression: dict = {}
    giver_trust = None
    if player:
        _sync_player_quest_lists(player, quest.id, QuestStatus.COMPLETED)
        rewards_applied = _apply_rewards(session, player, quest.rewards or {})
        # The scale's total, less what its met steps already paid
        # (progression.STEP_XP_SHARE) — never more than the scale all told
        progression = grant_xp(session, player, quest_completion_xp(quest), "quest")
        giver_trust = _thank_giver(session, quest, player)

    return {
        "already_completed": False,
        "rewards_applied": rewards_applied,
        "progression": progression,
        "giver_trust": giver_trust,
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
        giver_trust = None
        player = _get_player(session)
        if status == QuestStatus.COMPLETED:
            outcome = _complete_quest(session, quest, player)
            rewards_applied = outcome["rewards_applied"]
            progression = outcome["progression"]
            giver_trust = outcome["giver_trust"]
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
        # The giver's thanks, already in their trust (@quest-done's runner
        # refuses a @trust that would pay it twice)
        if giver_trust:
            result["giver_trust"] = giver_trust
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
                ". Every objective is met — if the quest is finished, write "
                "`@quest-done <quest>`."
            )
        return result

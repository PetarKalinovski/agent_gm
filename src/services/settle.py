"""Lines that settle the ledger before the story moves on (round 7).

Two kinds of binding context line, put in by
`src/agents/dm_orchestrator.py`:

[SETTLE THIS FIRST] — fleet8-wenna T13-T20: the DM put the Hollow's authored
proof on a dead stand-in wolf. The engine refused `@give` twice and
`@interact` twice; the prose had her holding it every time, then took it back
twice ("it must still be lying in the mud", "you never actually lifted it"),
each retcon planting it somewhere new — seven of 21 turns, and the player
sent on three pointless trips. A refusal told the DM what NOT to write and
left it to invent where the thing "lies". Now a refused @give / @interact /
@hand of an authored item lying elsewhere puts one line at the TOP of the
next context: where it really is, and one sentence of what the player
really has. Once per item; again only when a later reply claims it again
(refused_but_narrated / carried_not_held name it).

[PAY THIS REPLY] — fleet8-wenna T16/T17 and fleet8-tamsin T11/T12: the only
payoffs happened off screen ("find it already there among your things… Piper
must have counted it out"). When every step of an active quest is done and
its giver stands with the player, this reply pays, in the scene. A payout
made while the giver was elsewhere — or paid and never shown, the giver gone
since — reaches the player as a message or an arrival. Never mid-danger
(round 8, fleet9-tamsin T12: Piper paid with the wolf at the trough): with
a beast or a hostile here, a fight waiting or a risky act under way, the
line waits, its push unspent, for the first calm reply.

Per player, in memory: a restart forgets at most one line.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

logger = logging.getLogger(__name__)

PAY_MAX = 2          # PAY THIS REPLY pushes per quest; then the quest log alone
# A roll-first act that is a danger of its own (never the haggling or the
# word with the giver the payment is about)
_RISKY_FAMILIES = ("stealth", "theft", "force", "athletics", "intimidation", "facing a beast")

# player id -> item slug -> {"item": name, "due": bool}
_SETTLE: dict[str, dict[str, dict[str, Any]]] = {}
# player id -> quest id -> pushes
_PAY_PUSHED: dict[str, dict[str, int]] = {}
# player id -> [{"quest_id", "title", "giver_id", "away": bool, "unshown": bool}]
_PAID_APART: dict[str, list[dict[str, Any]]] = {}
# player id -> the item names the last reply's @give/@interact/@hand were
# refused (carried_not_held reads "it" as them a reply on)
_LAST_REFUSED: dict[str, list[str]] = {}
# player id -> this reply's danger, noted before the context is built
# (`note_turn`): {"risky": family | None, "fight_pending": bool}
_TURN: dict[str, dict[str, Any]] = {}

_CLAIM_CHECKS = ("refused_but_narrated", "carried_not_held")
_ITEM_VERBS = ("give", "interact", "hand")


def reset(player_id: str | None = None) -> None:
    """Forget the bookkeeping (tests, world switches)."""
    for store in (_SETTLE, _PAY_PUSHED, _PAID_APART, _LAST_REFUSED, _TURN):
        if player_id is None:
            store.clear()
        else:
            store.pop(player_id, None)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")


def _get(row: Any, key: str) -> Any:
    return row.get(key) if isinstance(row, dict) else getattr(row, key, None)


# ---------------------------------------------------------------------------
# After a reply
# ---------------------------------------------------------------------------

def note_reply(player_id: str, results: Iterable[Any], warnings: Iterable[dict[str, Any]] = (),
               paid_apart: Iterable[dict[str, Any]] = ()) -> None:
    """Book what the reply just did. `results`: its CommandResults (or
    transcript rows); `warnings`: the audit's; `paid_apart`: the runner's
    `paid_away` (@quest-done with the giver elsewhere). Never raises."""
    try:
        _note(player_id, list(results or []), list(warnings or []), list(paid_apart or []))
    except Exception as e:      # bookkeeping never costs the turn
        logger.warning(f"settle bookkeeping failed: {e}")


def refused_items(results: Iterable[Any]) -> list[str]:
    """The item each refused @give / @interact / @hand line named."""
    from src.services import commands as cmds

    names = []
    for row in results or []:
        if _get(row, "ok"):
            continue
        parsed = cmds.parse_command(str(_get(row, "line") or ""))
        if parsed is None or cmds._ALIASES.get(parsed.verb, parsed.verb) not in _ITEM_VERBS:
            continue
        item = re.sub(r"\s+(?:to\s+.+|x\d+)$", "", (parsed.main or "").strip(), flags=re.IGNORECASE)
        if item:
            names.append(item)
    return names


def recent_refusals(player_id: str) -> list[str]:
    """What the last reply's item lines were refused for."""
    return list(_LAST_REFUSED.get(player_id) or [])


def _note(player_id: str, results: list[Any], warnings: list[dict[str, Any]],
          paid_apart: list[dict[str, Any]]) -> None:
    _LAST_REFUSED[player_id] = refused_items(results)
    items = _SETTLE.setdefault(player_id, {})
    for row in results:
        result = _get(row, "result") or {}
        if _get(row, "ok") or not isinstance(result, dict) or result.get("refused") != "authored_elsewhere":
            continue
        name = str(result.get("item") or "").strip()
        # Once per item: a second refusal the prose respected says nothing new
        if name and _slug(name) not in items:
            items[_slug(name)] = {"item": name, "due": True}
    # ...and again when a reply claims it again
    for w in warnings:
        if w.get("check") not in _CLAIM_CHECKS:
            continue
        detail = str(w.get("detail") or "").lower()
        for entry in items.values():
            if entry["item"].lower() in detail:
                entry["due"] = True
    # A payout away from its giver, or one nobody showed
    apart = _PAID_APART.setdefault(player_id, [])
    seen = {row["quest_id"] for row in apart}
    for row in paid_apart:
        if row.get("quest_id") and row["quest_id"] not in seen:
            apart.append({**row, "away": True, "unshown": False})
            seen.add(row["quest_id"])
    if any(w.get("check") == "reward_unnarrated" for w in warnings):
        for row in results:
            result = _get(row, "result") or {}
            if not (_get(row, "ok") and _get(row, "verb") == "quest-done" and isinstance(result, dict)):
                continue
            quest_id = result.get("id")
            if not quest_id or result.get("already"):
                continue
            known = next((r for r in apart if r["quest_id"] == quest_id), None)
            if known is not None:
                known["unshown"] = True
            else:
                apart.append({"quest_id": quest_id, "title": result.get("title") or "", "giver_id": None,
                              "away": False, "unshown": True})


# ---------------------------------------------------------------------------
# Before the next one
# ---------------------------------------------------------------------------

def settle_line(player_id: str) -> str:
    """The [SETTLE THIS FIRST] line for the items due, or "". Spends them."""
    items = _SETTLE.get(player_id) or {}
    due = [entry for entry in items.values() if entry.get("due")]
    if not due:
        return ""
    try:
        parts = [p for p in (_settle_part(player_id, entry) for entry in due) if p]
    except Exception as e:      # the world unreadable: say it next time
        logger.warning(f"settle line unavailable: {e}")
        return ""
    for entry in due:
        entry["due"] = False
    if not parts:
        return ""
    return "[SETTLE THIS FIRST: " + " ".join(parts) + "]"


def _settle_part(player_id: str, entry: dict[str, Any]) -> str:
    """One item's sentence, read fresh: where it really is. "" when play has
    since settled it (it is in the pack, or it lies right where they stand)."""
    from src.models import NPC, Location, Player, get_session
    from src.services.commands import _plain_head
    from src.services.lead import holding_things, same_item

    item = entry["item"]
    with get_session() as session:
        player = session.get(Player, player_id)
        if player is None:
            return ""
        pack = [str(e.get("name") or "") for e in (player.inventory or []) if isinstance(e, dict)]
        if any(same_item(item, name) or same_item(name, item) for name in pack if name):
            return ""
        where = ""
        for location in session.query(Location).all():
            if any(same_item(item, t["payload"]["item"]["name"]) for t in holding_things(location)):
                if location.id == player.current_location_id:
                    return ""
                where = f"it lies at {location.name}"
                break
        if not where:
            holder = next((n for n in session.query(NPC).filter(NPC.status == "alive").all()
                           if any(same_item(item, str((e.get("name") if isinstance(e, dict) else e) or ""))
                                  for e in (n.inventory_notable or []))), None)
            where = f"{holder.name} has it" if holder is not None else "nobody has it"
    return (f"the {item} is not here and never was — {where}. In one sentence say what the player really "
            f"has (an empty cord, a plain toggle, nothing at all — a lesser find is @give'n under its own "
            f"name, never another {_plain_head(item)}) and never place the {item} anywhere else.")


def note_turn(player_id: str, player_input: str = "", *, fight_pending: bool = False) -> None:
    """Before the context is built (server): what this reply's player is
    doing — a risky act the roll decides, or a fight still waiting to be
    fought — so [PAY THIS REPLY] waits it out. Never raises."""
    try:
        from src.services.roll_intent import risky_act

        act = risky_act(player_input or "")
        _TURN[player_id] = {"risky": act[0] if act and act[0] in _RISKY_FAMILIES else None,
                            "fight_pending": bool(fight_pending)}
    except Exception as e:      # the hold is a courtesy, never a cost to the turn
        logger.debug(f"pay hold note skipped: {e}")
        _TURN.pop(player_id, None)


def _danger_now(session, player, player_id: str, giver_id: str | None = None) -> str:
    """Why a payout waits this reply, or "" when the scene is calm: someone
    here who would fight the player (a beast at the trough, a held scout, a
    thief in sight), a fight still waiting, or a risky act under way.
    fleet9-tamsin T12: Piper paid "even with the wolf still prowling the
    trough", and the stand-off the player was answering got no roll. The
    giver is never the danger to their own payout (a cross word with them
    would hold it for ever)."""
    from src.services import encounters

    turn = _TURN.get(player_id) or {}
    if turn.get("fight_pending"):
        return "a fight is waiting to be fought"
    if any(n.id != giver_id for n in encounters.enemies_here(session, player, beasts_only=False, limit=None)):
        return "a danger stands in the scene"
    if turn.get("risky"):
        return f"the player is mid-risk ({turn['risky']})"
    return ""


def pay_lines(player_id: str, present: dict[str, str], strangers: dict[str, str] | None = None) -> list[str]:
    """[PAY THIS REPLY] for each finished job whose giver is here, then a
    line for each payout made apart from its giver. Advances the push count
    and spends the apart notes. Never raises."""
    try:
        return _pay_lines(player_id, present or {}, strangers or {})
    except Exception as e:
        logger.warning(f"pay lines unavailable: {e}")
        return []


def _who(session, player_id: str, npc, strangers: dict[str, str], present: dict[str, str]) -> str:
    """The giver as the DM must say them: a stranger's role label, with the
    name it must not use. The brief's `strangers` decides for the people
    here; someone elsewhere is looked up."""
    from src.services.dialogue import player_has_met, stranger_label

    role = strangers.get(npc.id)
    if role is None and npc.id not in present:
        role = stranger_label(npc, met=player_has_met(session, player_id, npc.id))
    if not role:
        return npc.name
    the = role if role.lower().startswith("the ") else f"the {role}"
    return f"{the} ({npc.name} — a name the player has not learned: keep the role label)"


def _pay_lines(player_id: str, present: dict[str, str], strangers: dict[str, str]) -> list[str]:
    from src.models import NPC, Player, Quest, QuestStatus, get_session
    from src.services import objectives as objectives_mod
    from src.tools.world_write.quests import reward_balance, reward_coin, reward_words

    lines: list[str] = []
    if not present and not _PAID_APART.get(player_id):
        return lines            # nobody here to pay, nothing paid apart
    pushed = _PAY_PUSHED.setdefault(player_id, {})
    with get_session() as session:
        player = session.get(Player, player_id)
        if player is None:
            return []
        dangers: dict[str, str] = {}        # by giver, read only when a payout is due
        for quest in objectives_mod.active_quests_for(session, player):
            giver_id = quest.assigned_by_npc_id
            if not giver_id or giver_id not in present or pushed.get(quest.id, 0) >= PAY_MAX:
                continue
            if not objectives_mod.all_done(objectives_mod.normalize_objectives(quest.objectives)):
                continue
            giver = session.get(NPC, giver_id)
            if giver is None or giver.status != "alive":
                continue
            # Never mid-danger: the line waits, unspent, for the first calm
            # reply (the giver is not going anywhere)
            if giver_id not in dangers:
                dangers[giver_id] = _danger_now(session, player, player_id, giver_id)
            danger = dangers[giver_id]
            if danger:
                logger.info(f"[PAY THIS REPLY] for {quest.title!r} waits: {danger}")
                continue
            pushed[quest.id] = pushed.get(quest.id, 0) + 1
            balance = reward_balance(quest.rewards if isinstance(quest.rewards, dict) else {})
            pay = reward_words(balance)
            shown = ("show the coin counted into their hand" if reward_coin(balance)
                     else f"show {pay} handed over" if pay else "show their thanks")
            lines.append(f"[PAY THIS REPLY: {_who(session, player_id, giver, strangers, present)} pays for "
                         f"\"{quest.title}\"" + (f" ({pay})" if pay else "") + f" — every step is done. Write "
                         f"@quest-done {quest.title} and {shown}, in this scene, before anyone walks off.]")
        apart = _PAID_APART.pop(player_id, [])
        for row in apart:
            quest = session.get(Quest, row["quest_id"])
            if quest is None or quest.status != QuestStatus.COMPLETED:
                continue
            giver_id = row.get("giver_id") or quest.assigned_by_npc_id
            giver = session.get(NPC, giver_id) if giver_id else None
            here = giver is not None and giver.id in present
            if here and not row.get("away"):
                continue    # unshown with the giver right here: the [REALITY AUDIT] has them hand it over
            pay = reward_words(reward_balance(quest.rewards if isinstance(quest.rewards, dict) else {}))
            who = (_who(session, player_id, giver, strangers, present) if giver is not None
                   else "whoever set the job")
            paid = f" ({pay})" if pay else ""
            if here:
                lines.append(f"[PAID WHILE APART: \"{quest.title}\"{paid} was paid last reply while {who} was not "
                             f"with the player; {who} is here now — unless your last reply showed it reach them, "
                             "show it handed over in one sentence. Never @coin or @give it again.]")
            else:
                lines.append(f"[PAID WHILE APART: \"{quest.title}\"{paid} is already paid — the engine put it in "
                             f"their purse while {who} was not with the player. Unless your last reply showed it "
                             f"reach them, it reaches them THIS reply as a message or an arrival ({who} comes to "
                             "find them, or sends someone with it) — one sentence, never 'already among your "
                             "things' — and never @coin or @give it again.]")
    return lines

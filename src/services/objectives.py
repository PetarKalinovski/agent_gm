"""Structured quest objectives — the engine ticks them, the DM narrates them.

A quest objective used to be a sentence. Nothing could check a sentence, so
"Find Captain Coldiron" stayed unchecked after the player had spent an hour
talking to him, and the only way to point the player anywhere was prose.

An objective is now an object:

    {"id": "o1", "text": "Find Captain Coldiron at the Watch House",
     "kind": "talk_to", "target_id": "<npc id>", "target_type": "npc",
     "target_name": "Captain Brann Coldiron", "item_name": None,
     "done": False, "done_day": None}

`kind` is a fixed menu. Everything but `custom` is completed by CODE when the
thing actually happens — the player arrives (`go_to`), talks (`talk_to`),
picks up (`obtain`), hands over (`deliver`), or the target dies (`kill`).
The hooks below are called from the engine paths that make those true.

Backward compatibility is the normalizer's whole job: plain strings still
load (as `custom`, with a legacy "[DONE]" prefix honoured), every stored
list converges on dicts as it is rewritten, and `done` is STICKY — a model
that rewrites the list can add, reword, or mark a custom objective done, but
it can never un-complete work the engine has already credited.

Completion never completes the quest. The DM is told every objective is met
and decides — rewards and XP still flow through `_complete_quest` only.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable, Iterable

from src.models import Location, NPC, Player, Quest, QuestStatus, WorldClock, get_session

logger = logging.getLogger(__name__)

OBJECTIVE_KINDS = ("go_to", "talk_to", "obtain", "kill", "deliver", "custom")

# Kinds whose target is a person vs. a place
_NPC_KINDS = ("talk_to", "kill", "deliver")
_LOCATION_KINDS = ("go_to",)

# Legacy done-markers the old prompt taught the DM to write
_DONE_PREFIX = re.compile(r"^\s*(?:\[(?:done|x|complete|completed)\]|✓|✔|~~)\s*", re.I)
_DONE_SUFFIX = re.compile(r"\s*(?:~~|\((?:done|completed?)\))\s*$", re.I)

TEXT_MAX = 200
_ITEM_MATCH_MIN = 3

# player_id -> queued {"type": "objective_done", ...} frames for the client
_pending_events: dict[str, list[dict[str, Any]]] = {}
# player_id -> tool names the engine effectively ran this turn (audit exemption)
_audit_tools: dict[str, set[str]] = {}
_lock = threading.Lock()


# ----------------------------------------------------------------------
# Shape
# ----------------------------------------------------------------------

def _text_of(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        for key in ("text", "description", "objective", "title", "name"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _strip_done_markers(text: str) -> tuple[str, bool]:
    stripped = _DONE_PREFIX.sub("", text)
    done = stripped != text
    stripped2 = _DONE_SUFFIX.sub("", stripped)
    done = done or stripped2 != stripped
    return stripped2.strip(), done


def _resolve_target(session, kind: str, ident: Any) -> tuple[str | None, str | None, str | None]:
    """(target_id, target_type, target_name) for an id or a name; Nones when unresolvable."""
    if not ident or not isinstance(ident, str):
        return None, None, None
    ident = ident.strip()
    if not ident:
        return None, None, None
    if session is None:
        # No way to verify — keep what we were given and type it by kind
        target_type = "npc" if kind in _NPC_KINDS else "location" if kind in _LOCATION_KINDS else None
        return ident, target_type, None

    if kind in _NPC_KINDS:
        from src.web.tool_rescue import resolve_npc
        npc = resolve_npc(session, ident, None)
        if npc is not None:
            return npc.id, "npc", npc.name
        return None, None, None

    if kind in _LOCATION_KINDS:
        location = session.get(Location, ident)
        if location is None:
            wanted = ident.lower()
            rows = session.query(Location).all()
            exact = [r for r in rows if (r.name or "").lower() == wanted]
            partial = [r for r in rows if wanted in (r.name or "").lower()]
            candidates = exact or partial
            location = min(candidates, key=lambda r: len(r.name or "")) if candidates else None
        if location is not None:
            return location.id, "location", location.name
        return None, None, None

    return None, None, None


def normalize_objective(raw: Any, index: int, session=None) -> dict[str, Any] | None:
    """One objective in canonical shape, or None when it is unusable."""
    text = _text_of(raw)
    text, legacy_done = _strip_done_markers(text)
    if not text:
        return None
    text = text[:TEXT_MAX]

    if isinstance(raw, dict):
        kind = str(raw.get("kind") or "custom").strip().lower()
        if kind not in OBJECTIVE_KINDS:
            kind = "custom"
        ident = raw.get("target_id") or raw.get("npc_id") or raw.get("location_id") or raw.get("target")
        item_name = raw.get("item_name") or raw.get("item")
        done = bool(raw.get("done")) or legacy_done
        obj_id = str(raw.get("id") or "").strip()
        done_day = raw.get("done_day")
        given_name = raw.get("target_name")
    else:
        kind, ident, item_name, done, obj_id, done_day, given_name = "custom", None, None, legacy_done, "", None, None

    target_id, target_type, target_name = _resolve_target(session, kind, ident)
    if target_id is None and ident:
        logger.warning(f"objective target could not be resolved, keeping text only: {ident!r} ({kind})")
    if target_name is None and isinstance(given_name, str) and given_name.strip():
        target_name = given_name.strip()

    if isinstance(item_name, str):
        item_name = item_name.strip() or None
    else:
        item_name = None

    try:
        done_day = int(done_day) if done and done_day is not None else None
    except (TypeError, ValueError):
        done_day = None

    return {
        "id": obj_id or f"o{index + 1}",
        "text": text,
        "kind": kind,
        "target_id": target_id,
        "target_type": target_type,
        "target_name": target_name,
        "item_name": item_name,
        "done": done,
        "done_day": done_day,
    }


def normalize_objectives(raw: Any, previous: Any = None, session=None) -> list[dict[str, Any]]:
    """The canonical list. `previous` (the stored list) makes `done` sticky
    and keeps a marker the rewrite dropped. Never raises."""
    if raw is None:
        raw = []
    elif isinstance(raw, (str, dict)):
        raw = [raw]
    elif not isinstance(raw, list):
        logger.warning(f"objectives ignored, not a list: {type(raw).__name__}")
        raw = []

    prior = normalize_objectives(previous) if previous else []
    by_id = {p["id"]: p for p in prior}
    by_text = {p["text"].lower(): p for p in prior}

    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(raw):
        try:
            obj = normalize_objective(entry, index, session)
        except Exception as e:  # never let a bad entry cost the turn
            logger.warning(f"objective dropped: {e}")
            obj = None
        if obj is None:
            continue

        match = by_id.get(obj["id"]) or by_text.get(obj["text"].lower())
        if match is None and len(prior) == len(raw) and index < len(prior):
            match = prior[index]
        if match is not None:
            if match["done"]:
                obj["done"] = True
                obj["done_day"] = match.get("done_day")
            if obj["target_id"] is None and match.get("target_id") and obj["kind"] == match.get("kind"):
                obj["target_id"] = match["target_id"]
                obj["target_type"] = match.get("target_type")
                obj["target_name"] = match.get("target_name")
            if obj["item_name"] is None and match.get("item_name"):
                obj["item_name"] = match["item_name"]

        if obj["id"] in seen_ids:
            obj["id"] = f"o{index + 1}"
            if obj["id"] in seen_ids:
                obj["id"] = f"o{index + 1}_{len(seen_ids)}"
        seen_ids.add(obj["id"])
        result.append(obj)
    return result


def objective_text(raw: Any) -> str:
    """The sentence, for a console panel or an editor row."""
    text, _ = _strip_done_markers(_text_of(raw))
    return text


def first_undone(objectives: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    for obj in objectives or []:
        if isinstance(obj, dict) and not obj.get("done"):
            return obj
    return None


def all_done(objectives: Iterable[dict[str, Any]]) -> bool:
    objectives = list(objectives or [])
    return bool(objectives) and all(bool(o.get("done")) for o in objectives if isinstance(o, dict))


_PUBLIC_KEYS = ("id", "text", "kind", "target_id", "target_type", "target_name", "item_name", "done")


def objectives_public(quest: Any) -> list[dict[str, Any]]:
    """Normalized objectives for a Quest row or a quest dict — what the HUD,
    the DM's reads, and the editor all agree on."""
    raw = quest.get("objectives") if isinstance(quest, dict) else getattr(quest, "objectives", None)
    return [{k: o.get(k) for k in _PUBLIC_KEYS} for o in normalize_objectives(raw)]


# ----------------------------------------------------------------------
# Engine completion
# ----------------------------------------------------------------------

def _player(session, player_id: str | None):
    if player_id:
        return session.get(Player, player_id)
    players = session.query(Player).all()
    return players[0] if len(players) == 1 else None


def active_quests_for(session, player) -> list:
    """Every ACTIVE quest, the player's most recently activated first."""
    rows = session.query(Quest).filter(Quest.status == QuestStatus.ACTIVE).all()
    by_id = {q.id: q for q in rows}
    ordered: list = []
    seen: set[str] = set()
    for quest_id in reversed([q for q in (getattr(player, "active_quests", None) or []) if isinstance(q, str)]):
        quest = by_id.get(quest_id)
        if quest is not None and quest.id not in seen:
            ordered.append(quest)
            seen.add(quest.id)
    for quest in rows:
        if quest.id not in seen:
            ordered.append(quest)
            seen.add(quest.id)
    return ordered


def _item_matches(wanted: str | None, names: Iterable[str]) -> bool:
    if not wanted:
        return False
    w = wanted.lower().strip()
    if len(w) < _ITEM_MATCH_MIN:
        return False
    for name in names:
        n = str(name or "").lower().strip()
        if not n:
            continue
        if w in n or (len(n) >= _ITEM_MATCH_MIN and n in w):
            return True
    return False


def tick_in_session(session, player, matcher: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
    """Mark every matching undone objective on the player's ACTIVE quests.

    Runs inside the caller's transaction and never commits. Returns one
    record per objective completed; call `publish` once the caller has
    committed so the client and the ledger hear about it.
    """
    if player is None:
        return []
    # Quests are world-global (one story per database); the player's own list
    # is an ordering hint, not a filter — a quest an NPC agent activated for
    # "the" player is still theirs even when the list never caught it
    quests = active_quests_for(session, player)
    if not quests:
        return []
    clock = session.query(WorldClock).first()
    day = clock.day if clock else 1

    records: list[dict[str, Any]] = []
    for quest in quests:
        objectives = normalize_objectives(quest.objectives)
        hits: list[dict[str, Any]] = []
        for obj in objectives:
            if obj["done"]:
                continue
            try:
                if not matcher(obj):
                    continue
            except Exception as e:
                logger.warning(f"objective matcher failed: {e}")
                continue
            obj["done"] = True
            obj["done_day"] = day
            hits.append(obj)
        if not hits:
            continue
        quest.objectives = objectives      # reassign: in-place JSON edits are not persisted
        finished = all_done(objectives)
        for obj in hits:
            records.append({
                "quest_id": quest.id,
                "quest_title": quest.title,
                "objective_id": obj["id"],
                "text": obj["text"],
                "kind": obj["kind"],
                "all_done": finished,
            })
    return records


def publish(player_id: str | None, records: list[dict[str, Any]]) -> None:
    """Queue the client events, the ledger lines, and the audit exemption."""
    if not records:
        return
    from src.web import pending_actions

    pid = player_id or ""
    with _lock:
        queue = _pending_events.setdefault(pid, [])
        for r in records:
            queue.append({
                "type": "objective_done",
                "quest_id": r["quest_id"],
                "quest_title": r["quest_title"],
                "objective_id": r["objective_id"],
                "text": r["text"],
                "kind": r["kind"],
                "all_done": bool(r.get("all_done")),
            })
        _audit_tools.setdefault(pid, set()).add("update_quest_objectives")
    for r in records:
        try:
            pending_actions.record_action(
                pid, "objective", f'"{r["text"]}" ({r["quest_title"]})',
                tools=("update_quest_objectives",),
                all_done=bool(r.get("all_done")), quest_title=r["quest_title"],
            )
        except Exception as e:
            logger.debug(f"objective ledger line skipped: {e}")
        logger.info(f"Objective met: {r['text']!r} ({r['quest_title']}, all_done={r.get('all_done')})")


def _tick(player_id: str | None, matcher: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
    """Own-session variant for callers that have already committed."""
    try:
        with get_session() as session:
            player = _player(session, player_id)
            records = tick_in_session(session, player, matcher)
            if records:
                session.commit()
            resolved_id = player.id if player else player_id
    except Exception as e:
        logger.warning(f"objective tick failed: {e}")
        return []
    publish(resolved_id, records)
    return records


def note_arrival(player_id: str | None, location_id: str | None) -> list[dict[str, Any]]:
    if not location_id:
        return []
    return _tick(player_id, lambda o: o["kind"] == "go_to" and o["target_id"] == location_id)


def note_conversation(player_id: str | None, npc_id: str | None) -> list[dict[str, Any]]:
    if not npc_id:
        return []
    return _tick(player_id, lambda o: o["kind"] == "talk_to" and o["target_id"] == npc_id)


def note_items_gained(player_id: str | None, item_names: Iterable[str]) -> list[dict[str, Any]]:
    names = [n for n in (item_names or []) if n]
    if not names:
        return []
    return _tick(player_id, lambda o: o["kind"] == "obtain" and _item_matches(o["item_name"], names))


def note_item_given(player_id: str | None, npc_id: str | None, item_name: str | None) -> list[dict[str, Any]]:
    if not npc_id or not item_name:
        return []
    return _tick(
        player_id,
        lambda o: o["kind"] == "deliver" and o["target_id"] == npc_id
        and _item_matches(o["item_name"], [item_name]),
    )


def note_npc_deaths(player_id: str | None, npc_ids: Iterable[str]) -> list[dict[str, Any]]:
    dead = {n for n in (npc_ids or []) if n}
    if not dead:
        return []
    return _tick(player_id, lambda o: o["kind"] == "kill" and o["target_id"] in dead)


def deaths_matcher(npc_ids: Iterable[str]) -> Callable[[dict[str, Any]], bool]:
    """For callers inside their own transaction (campaign ops)."""
    dead = {n for n in (npc_ids or []) if n}
    return lambda o: o["kind"] == "kill" and o["target_id"] in dead


def note_custom_done(player_id: str | None, quest_id: str, objective_id: str) -> list[dict[str, Any]]:
    """The DM's path: mark one custom objective on one quest."""
    if not quest_id or not objective_id:
        return []
    wanted = objective_id.strip().lower()

    def matcher(o: dict[str, Any]) -> bool:
        return o["id"].lower() == wanted or o["text"].lower() == wanted

    try:
        with get_session() as session:
            player = _player(session, player_id)
            quest = session.get(Quest, quest_id)
            if player is None or quest is None or quest.status != QuestStatus.ACTIVE:
                return []
            # Scope the tick to this one quest
            keep = list(player.active_quests or [])
            player.active_quests = [quest_id]
            records = tick_in_session(session, player, matcher)
            player.active_quests = keep
            if records:
                session.commit()
            resolved_id = player.id
    except Exception as e:
        logger.warning(f"custom objective tick failed: {e}")
        return []
    publish(resolved_id, records)
    return records


# ----------------------------------------------------------------------
# Drains
# ----------------------------------------------------------------------

def pop_pending_events(player_id: str | None) -> list[dict[str, Any]]:
    with _lock:
        return _pending_events.pop(player_id or "", [])


def pop_audit_tools(player_id: str | None) -> set[str]:
    with _lock:
        return _audit_tools.pop(player_id or "", set())


def clear(player_id: str | None) -> None:
    with _lock:
        _pending_events.pop(player_id or "", None)
        _audit_tools.pop(player_id or "", None)


def done_lines(records: list[dict[str, Any]]) -> list[str]:
    """Plain lines for a tool result, so the DM sees what just ticked."""
    return [f'Objective met: "{r["text"]}" ({r["quest_title"]})'
            + (" — every objective on this quest is now met; decide whether it is finished."
               if r.get("all_done") else "")
            for r in records or []]

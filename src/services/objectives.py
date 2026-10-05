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


_RETURN_KINDS = ("talk_to", "deliver")

# A custom step that asks a question ("Find out what is organizing the
# wolves", "Learn who the letter was meant for") is answered by the story,
# not by the proof changing hands. fleet8-bram T7: the whistle in the
# Captain's hand ticked it while he said "You want the whole of it, you'll
# have to give me more than one night in the Hollow. Go back." — the job was
# paid in full at T8, and the act-3 beat waiting on it being under way could
# never come. The DM rules it (@step-done) when the story answers it.
_INVESTIGATES = re.compile(
    r"^\s*(?:find\s+out|learn|discover|uncover|work\s+out|figure\s+out|determine|identify|investigate"
    r"|who|why|what|how|where|whose)\b",
    re.IGNORECASE,
)


def investigates(obj: dict[str, Any]) -> bool:
    """Is this a custom step that asks a question (see above)?"""
    return obj.get("kind") == "custom" and bool(_INVESTIGATES.match(str(obj.get("text") or "")))


def _is_return_step(obj: dict[str, Any], earlier: list[dict[str, Any]], giver_id: str | None) -> bool:
    """A step that closes a loop, so it waits for the steps before it.

    Two shapes: a target an earlier objective already named ("Report back to
    Coldiron" after "Talk to Coldiron"), and a talk/hand-over aimed at the
    quest's GIVER. The giver one is the playtest 2026-10-04 bug: "Bring proof
    back to Captain Coldiron" ticked on turn 0 because Coldiron speaks while
    offering the job, and the quest paid out before any proof existed.

    A `kill` step never waits: a death happens once, so a held-back kill
    ("Kill Vex" after an unmet "Talk to Vex") could never tick again.
    """
    target = obj.get("target_id")
    if not target or obj.get("kind") == "kill":
        return False
    if any(prev.get("target_id") == target for prev in earlier):
        return True
    return bool(giver_id) and target == giver_id and obj.get("kind") in _RETURN_KINDS


def tick_in_session(session, player, matcher: Callable[[dict[str, Any]], bool],
                    *, quest_id: str | None = None, skip_quest_ids: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Mark every matching undone objective on the player's ACTIVE quests
    (only on `quest_id` when given).

    Runs inside the caller's transaction and never commits. Returns one
    record per objective completed; call `publish` once the caller has
    committed so the client and the ledger hear about it.

    A return step (`_is_return_step`) completes only when every objective
    before it was done BEFORE this event — one conversation never both
    opens and closes a loop.
    """
    if player is None:
        return []
    # Quests are world-global (one story per database); the player's own list
    # is an ordering hint, not a filter — a quest an NPC agent activated for
    # "the" player is still theirs even when the list never caught it
    skip = set(skip_quest_ids or ())
    quests = [q for q in active_quests_for(session, player)
              if (quest_id is None or q.id == quest_id) and q.id not in skip]
    if not quests:
        return []
    clock = session.query(WorldClock).first()
    day = clock.day if clock else 1

    records: list[dict[str, Any]] = []
    for quest in quests:
        objectives = normalize_objectives(quest.objectives)
        done_before = [bool(o["done"]) for o in objectives]
        giver_id = getattr(quest, "assigned_by_npc_id", None)
        hits: list[dict[str, Any]] = []
        for index, obj in enumerate(objectives):
            if obj["done"]:
                continue
            # The real item in the giver's hand closes the loop by itself: a
            # custom step only the DM can tick never holds back a delivery
            # (fleet5-tamsin: "Find out what is organizing the wolves" was
            # never ticked, and the lead sends them on to the proof — see
            # lead._proof_lead)
            returning = _is_return_step(obj, objectives[:index], giver_id)
            waiting = [done or (obj["kind"] == "deliver" and objectives[i]["kind"] == "custom")
                       for i, done in enumerate(done_before[:index])]
            if returning and not all(waiting):
                continue
            try:
                if not matcher(obj):
                    continue
            except Exception as e:
                logger.warning(f"objective matcher failed: {e}")
                continue
            if returning and obj["kind"] == "deliver":
                # ...and settles them, or the tracker falls back onto the
                # frozen step the moment the proof is handed over (a hand-over
                # to someone else mid-quest closes no loop: its customs stay)
                # — but never a question the story has not answered yet
                # (`investigates`, fleet8-bram T7)
                for prev in objectives[:index]:
                    if not prev["done"] and prev["kind"] == "custom" and not investigates(prev):
                        prev["done"] = True
                        prev["done_day"] = day
                        hits.append(prev)
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
        pay_step_xp(session, player, quest, len(objectives), records[-len(hits):])
    return records


def pay_step_xp(session, player, quest, steps: int, records: list[dict[str, Any]]) -> dict[str, Any]:
    """The quest's XP slice for the steps just met (`records`, all of this
    quest), granted in the caller's transaction; its progression block
    rides the last record, so the toast moves the bar (fleet10: a whole
    session of investigation moved it 0). {} when nothing was paid. Never
    raises — a step met is never undone by its XP."""
    if player is None or not records:
        return {}
    try:
        from src.services import progression

        amount = progression.step_xp(quest, steps, len(records))
        if amount <= 0:
            return {}
        quest.step_xp_paid = progression.step_xp_paid(quest) + amount
        block = progression.grant_xp(session, player, amount, "quest_step")
        if block:
            records[-1]["progression"] = block
        return block
    except Exception as e:
        logger.warning(f"step XP not paid for {getattr(quest, 'id', '?')}: {e}")
        return {}


def publish(player_id: str | None, records: list[dict[str, Any]]) -> None:
    """Queue the client events, the ledger lines, and the audit exemption."""
    if not records:
        return
    from src.web import pending_actions

    pid = player_id or ""
    with _lock:
        queue = _pending_events.setdefault(pid, [])
        for r in records:
            event = {
                "type": "objective_done",
                "quest_id": r["quest_id"],
                "quest_title": r["quest_title"],
                "objective_id": r["objective_id"],
                "text": r["text"],
                "kind": r["kind"],
                "all_done": bool(r.get("all_done")),
            }
            # The step's XP rides its toast: the bar moves, never a number in chat
            if r.get("progression"):
                event["progression"] = r["progression"]
            queue.append(event)
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
        # A level a step earned is acknowledged once, obliquely, as a fight's is
        if (r.get("progression") or {}).get("leveled_up"):
            try:
                pending_actions.record_action(pid, "levelup", "")
            except Exception as e:
                logger.debug(f"level-up ledger line skipped: {e}")


def _tick(player_id: str | None, matcher: Callable[[dict[str, Any]], bool],
          skip_quest_ids: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Own-session variant for callers that have already committed."""
    try:
        with get_session() as session:
            player = _player(session, player_id)
            records = tick_in_session(session, player, matcher, skip_quest_ids=skip_quest_ids)
            if records:
                session.commit()
            resolved_id = player.id if player else player_id
    except Exception as e:
        logger.warning(f"objective tick failed: {e}")
        return []
    publish(resolved_id, records)
    _note_offered(resolved_id, matcher)
    return records


def _note_offered(player_id: str | None, matcher: Callable[[dict[str, Any]], bool]) -> None:
    """The same event, for a job its giver offered that nobody has started
    yet: remembered, so the start credits it (src/services/job_offers.py;
    fleet7-wenna T4-T7 asked Sorrel for four turns of a job never started)."""
    if not player_id:
        return
    try:
        from src.services import job_offers

        job_offers.note_play(player_id, matcher)
    except Exception as e:     # the tick itself already landed
        logger.debug(f"offered job's step not noted: {e}")


def note_arrival(player_id: str | None, location_id: str | None) -> list[dict[str, Any]]:
    if not location_id:
        return []
    return _tick(player_id, lambda o: o["kind"] == "go_to" and o["target_id"] == location_id)


def note_conversation(player_id: str | None, npc_id: str | None,
                      started_quest_ids: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The player talked to `npc_id`: their talk_to steps tick.

    Not on a quest taken in this same reply (`started_quest_ids`): the
    player's words came before the job. fleet6-tamsin T4: "the straw-haired
    boy" was asked about the ash-gray man, then Piper offered "Ask Sorrel
    Tack what the stable yard hears after dark" — the asking was T5.

    A job offered in an EARLIER reply and started in this one is not held
    back: the words came after the offer (fleet7-wenna: Piper pitched at
    T2, Sorrel was asked at T4)."""
    if not npc_id:
        return []
    held = list(started_quest_ids or ())
    if held:
        try:
            from src.services import job_offers

            held = [q for q in held if not job_offers.offered_before(player_id, q)]
        except Exception as e:      # unreadable offers: hold them all, as before
            logger.debug(f"offer bookkeeping unreadable: {e}")
    return _tick(player_id, lambda o: o["kind"] == "talk_to" and o["target_id"] == npc_id,
                 skip_quest_ids=held)


def close_giver_talk(session, quest, player) -> list[dict[str, Any]]:
    """At a quest's completion, the open talk_to steps aimed at its giver
    close when the giver is there to pay out: that payday IS the report.

    fleet6-tamsin T11: the player laid it all out for Piper, "@quest-done
    Counting the Howls" paid, and "Bring what you heard back to Piper" stayed
    open on a finished quest — the player's words that turn were to Sorrel.
    A giver who is elsewhere (fleet6-wenna T20: Maera's coin arrived at the
    gatehouse) closes nothing. In the caller's transaction; returns the steps
    closed (no toast: the completion banner says it)."""
    giver_id = getattr(quest, "assigned_by_npc_id", None)
    if player is None or not giver_id:
        return []
    giver = session.get(NPC, giver_id)
    here = getattr(player, "current_location_id", None)
    party = player.party_members if isinstance(getattr(player, "party_members", None), list) else []
    if giver is None or giver.status != "alive":
        return []
    if giver_id not in party and not (here and giver.current_location_id == here):
        return []
    objectives = normalize_objectives(quest.objectives)
    clock = session.query(WorldClock).first()
    closed = []
    for obj in objectives:
        if not obj["done"] and obj["kind"] == "talk_to" and obj.get("target_id") == giver_id:
            obj["done"] = True
            obj["done_day"] = clock.day if clock else 1
            closed.append(obj)
    if closed:
        quest.objectives = objectives      # reassign: in-place JSON edits are not persisted
    return closed


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
            # Scoped by id: narrowing `player.active_quests` (the old way)
            # scoped nothing, since that list only orders the quests — "o2"
            # ticked the second step of every active quest at once
            records = tick_in_session(session, player, matcher, quest_id=quest_id)
            if records:
                session.commit()
            resolved_id = player.id
    except Exception as e:
        logger.warning(f"custom objective tick failed: {e}")
        return []
    publish(resolved_id, records)
    return records


# ----------------------------------------------------------------------
# Work only told
# ----------------------------------------------------------------------
# fleet7-tamsin T9-T14: the trip to Wolf's Hollow was prose (the DB had her
# at the North Gatehouse), `@give Cracked Bone Whistle` was refused, `@hand`
# of it failed — and `@quest-done Teeth on the North Road` paid 60 coin, the
# charm and 220 XP with "Reach Wolf's Hollow" and the whistle undone. Act 2
# then opened on a prop she never held.

def _stood_in(session, player, location_id: str) -> bool:
    """Has the player ever stood at this place (or somewhere inside it)?"""
    location = session.get(Location, location_id)
    if location is None or location.visited:
        return True                    # an unknown place proves nothing either way
    here = session.get(Location, player.current_location_id) if player.current_location_id else None
    hops = 0
    while here is not None and hops < 12:
        if here.id == location_id:
            return True
        here = session.get(Location, here.parent_id) if here.parent_id else None
        hops += 1
    inside = [location_id]
    for _ in range(6):
        inside = [row[0] for row in session.query(Location.id).filter(Location.parent_id.in_(inside)).all()]
        if not inside:
            return False
        if session.query(Location.id).filter(Location.id.in_(inside), Location.visited == True).first():  # noqa: E712
            return True
    return False


_PLAIN_HEADS = frozenset({"piece", "pieces", "coin", "coins", "bundle", "bit", "scrap", "item", "thing", "set"})


def _head_noun(name: str) -> str:
    """"North Road Pass" -> "pass"; "" for a head that names nothing ("piece")."""
    words = re.findall(r"[a-z]+", re.split(r"\s+of\s+", str(name or "").lower())[0])
    head = words[-1] if words else ""
    return head if len(head) >= 3 and head not in _PLAIN_HEADS else ""


def _where_item_is(session, item_name: str) -> tuple[str, bool]:
    """("it still lies at Wolf's Hollow (Cracked bone whistle)", True) for an
    item an authored Thing still holds; ("Captain Coldiron has it", False) for
    one a person holds; ("", False) when it is nowhere. The flag: a Thing
    still holding it means nobody ever took it."""
    from src.services.lead import holding_things, same_item

    for location in session.query(Location).all():
        for thing in holding_things(location):
            if same_item(item_name, thing["payload"]["item"]["name"]):
                return f"it still lies at {location.name} ({thing['label']})", True
    for npc in session.query(NPC).filter(NPC.status == "alive").all():
        names = [str((e.get("name") if isinstance(e, dict) else e) or "") for e in (npc.inventory_notable or [])]
        if any(same_item(item_name, n) for n in names if n):
            return f"{npc.name} has it", False
    return "", False


def undone_in_play(session, quest, player) -> list[dict[str, Any]]:
    """The quest's place and item steps play never did and could not have:
    a go_to place the player has never stood in, an obtain or deliver item
    they have never held. Rows {"id", "text", "kind", "why", "where"} in
    step order; [] when each such step is done or was possible — they stood
    there, or hold the item (a tick the name matching missed is not this).
    Custom, talk_to and kill steps are the DM's or the engine's own.

    "Never held": not in the pack, named by no done step of the quest, not
    in the deliver target's hands, and either an authored Thing still
    holds it or nobody has it at all."""
    from src.services.lead import same_item

    if quest is None or player is None:
        return []
    objectives = normalize_objectives(quest.objectives)
    pack = [str(e.get("name") or "") for e in (player.inventory or []) if isinstance(e, dict)]
    # The same thing by another name is held: fleet-bram T13 carried the
    # "North Road Pass" the DM gave for the step's "Gate Pass"
    pack_heads = {_head_noun(n) for n in pack} - {""}
    done_items = [o["item_name"] for o in objectives if o["done"] and o.get("item_name")]
    rows: list[dict[str, Any]] = []
    for obj in objectives:
        if obj["done"]:
            continue
        why = where = ""
        if obj["kind"] == "go_to" and obj.get("target_id"):
            if not _stood_in(session, player, obj["target_id"]):
                place = session.get(Location, obj["target_id"])
                why = f"the player has never stood at {place.name if place else 'that place'}"
        elif obj["kind"] in ("obtain", "deliver") and obj.get("item_name"):
            item = obj["item_name"]
            held = any(same_item(item, n) for n in pack if n) or any(same_item(item, d) for d in done_items) \
                or _head_noun(item) in pack_heads
            target = session.get(NPC, obj["target_id"]) if obj["kind"] == "deliver" and obj.get("target_id") else None
            if not held and target is not None:
                names = [str((e.get("name") if isinstance(e, dict) else e) or "")
                         for e in (target.inventory_notable or [])]
                held = any(same_item(item, n) for n in names if n)
            if not held:
                where, untouched = _where_item_is(session, item)
                if untouched or not where:
                    why = f"they have never held the {item}"
                else:
                    where = ""
        if why:
            rows.append({"id": obj["id"], "text": obj["text"], "kind": obj["kind"], "why": why, "where": where})
    return rows


# ----------------------------------------------------------------------
# Drains
# ----------------------------------------------------------------------

def queue_event(player_id: str | None, event: dict[str, Any]) -> None:
    """One more client frame for the next drain (a quest the engine started)."""
    with _lock:
        _pending_events.setdefault(player_id or "", []).append(event)


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

"""The Lead — the one thing the player should do next, chosen by code.

The game had a spine (campaign beats fire on their own) but no compass:
nothing on the screen ever said "go here" or "talk to them". The lead is
that compass. It is derived, never authored at runtime:

  1. the first undone objective of the most recently activated quest
     (`Player.active_quests` is append-on-activate, so the last id is the
     newest — see `_sync_player_quest_lists`);
  2. else the player-facing `lead` on the earliest armed (or stalled)
     campaign beat — the spoiler-safe half of a beat the DM has not
     delivered yet — or, when none carries one, the beat firing now or
     the next act's first (`_next_story_lead`);
  3. else nothing, and the DM is told so in plain words.

`state.lead` rides the state event; the tracker under the location chip
shows its text, the canvas marks its target, the chips offer it first, and
`format_lead_context` puts the same sentence in front of the DM so prose and
screen never disagree.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from typing import Any

from src.models import Connection, Location, NPC, Quest, QuestStatus
from src.services import objectives as objectives_mod
from src.services.travel import check_route

logger = logging.getLogger(__name__)

NO_LEAD_CONTEXT = (
    "[PLAYER'S LEAD: none. The player has NOTHING to do and can see that on their "
    "screen. Fix it THIS TURN: offer work through someone present, activate an "
    "authored quest (get_available_quests_for_npc), or give them a reason to go "
    "somewhere — a quest with one objective that names a real npc or location id.]"
)


# ----------------------------------------------------------------------
# Selection
# ----------------------------------------------------------------------

def compute_lead(session, player) -> dict[str, Any] | None:
    """The player's current lead, or None."""
    if player is None:
        return None
    try:
        lead = _quest_lead(session, player)
        try:
            story = _beat_lead(session, player)
        except Exception as e:  # the spine unreadable: the quest step still stands
            logger.warning(f"story lead computation failed: {e}")
            story = None
        # A story hook firing where the player stands outranks the quest
        # step while they stand there: the person shows a tell this reply,
        # and the tracker says what it is about (fleet6-bram T14: Bram had
        # a job, so the Fence's Ledger lead never reached the screen while
        # the DM pressed Hesk for him). Walk on and the quest step is back.
        if story is not None and (story.get("target") or {}).get("here") and _fired_hook(session, story):
            return story
        return lead if lead is not None else story
    except Exception as e:
        logger.warning(f"lead computation failed: {e}")
        return None


def _fired_hook(session, lead: dict[str, Any]) -> bool:
    from src.models import StoryBeat
    from src.services.campaign import is_hook_beat

    beat = session.get(StoryBeat, lead.get("beat_id")) if lead.get("beat_id") else None
    return beat is not None and beat.status == "fired" and is_hook_beat(beat)


def _quest_lead(session, player) -> dict[str, Any] | None:
    for quest in objectives_mod.active_quests_for(session, player):
        objectives = objectives_mod.normalize_objectives(quest.objectives)
        current = objectives_mod.first_undone(objectives)
        if current is None:
            continue
        pointed = _proof_lead(session, player, quest, objectives, current)
        if pointed is not None:
            return _with_pay(pointed, quest)
        lead = {
            "text": current["text"],
            "kind": current["kind"],
            "source": "quest",
            "quest_id": quest.id,
            "quest_title": quest.title,
            "objective_id": current["id"],
            "item_name": current.get("item_name"),
        }
        lead.update(_target_fields(session, player, current.get("target_type"), current.get("target_id")))
        # Proof nobody has found is "proof" on their screen, so the line the
        # DM reads as on it says the same (dialogue.unfound_steps)
        try:
            from src.services.dialogue import hide_unfound, unfound_steps

            lead = hide_unfound(lead, unfound_steps(session, player, [quest]))
        except Exception as e:
            logger.debug(f"unfound proof lead not reworded: {e}")
        return _with_pay(lead, quest)
    return None


def payout_words(rewards: Any) -> str:
    """"60 coin and a Wolf-Tooth Charm": what completing a quest still pays
    (the balance after any advance), in the plain words a tracker line can
    carry — coin and items by name, nothing else (standing is no prize to
    point at, experience is never named). "" when it pays nothing more."""
    from src.tools.world_write.quests import reward_balance, reward_coin

    balance = reward_balance(rewards)
    parts: list[str] = []
    coin = reward_coin(balance)
    if coin:
        parts.append(f"{coin} coin")
    for entry in balance.get("items") or []:
        name = " ".join(str((entry.get("name") if isinstance(entry, dict) else entry) or "").split())
        if not name:
            continue
        qty = entry.get("quantity") if isinstance(entry, dict) else None
        if isinstance(qty, int) and qty > 1:
            parts.append(f"{qty} {name}")
        elif name.split()[0].lower() in ("a", "an", "the", "some"):
            parts.append(name)
        else:
            parts.append(f"{'an' if name[0].lower() in 'aeiou' else 'a'} {name}")
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _with_pay(lead: dict[str, Any], quest) -> dict[str, Any]:
    """The lead with what its quest pays (`pays`): fleet10 (all three runs)
    ended 21 turns on the 25 coin they began with and nothing on screen said
    what the open job would bring. The tracker shows it after the step
    ("— pays 60 coin and a Wolf-Tooth Charm"); the step text stays as it is,
    since the chips build "I <lead>" from it."""
    try:
        pays = payout_words(getattr(quest, "rewards", None))
    except Exception as e:      # an odd reward never costs the compass
        logger.debug(f"payout words skipped for {getattr(quest, 'id', '?')}: {e}")
        pays = ""
    if pays:
        lead["pays"] = pays
    return lead


def _beat_lead(session, player) -> dict[str, Any] | None:
    from src.services.campaign import _campaign_beats, get_campaign_row, is_hook_beat

    campaign = get_campaign_row(session, player.id)
    if campaign is None or campaign.status != "active":
        return None
    # A hook that fired waits on the player to take it up, so its lead is
    # the one to show (fleet6-bram T14: "press him on it" never reached the
    # screen while the DM pressed Hesk for him)
    beats = _campaign_beats(session, campaign)
    candidates = [
        b for b in beats
        if (b.status in ("armed", "stalled") or (b.status == "fired" and is_hook_beat(b)))
        and (getattr(b, "lead", None) or "").strip()
    ]
    if not candidates:
        # Nothing waiting carries a lead: the story's next step does
        # (`_next_story_lead`) rather than an empty tracker
        candidates = _next_story_lead(session, campaign, beats)
    if not candidates:
        return None
    candidates.sort(key=lambda b: (b.status != "fired", b.act, b.sequence))
    # A beat whose happening is still ahead on the clock yields to one that
    # stands now; alone, it says when and points nowhere yet (fleet7-tamsin
    # T15-T20: "The cart that limped in short is being unloaded" at 23:40,
    # its cart due at 09:00 — the DM narrated the wreckage twice)
    now = _clock_now(session)
    ahead = {b.id: due for b in candidates if (due := beat_due_later(session, b, now)) is not None}
    beat = next((b for b in candidates if b.id not in ahead), candidates[0])
    lead = {
        "text": beat.lead.strip(),
        "kind": "story",
        "source": "beat",
        "beat_id": beat.id,
        "quest_id": None,
        "quest_title": campaign.title,
        "objective_id": None,
        "item_name": None,
    }
    if beat.id in ahead:
        day, hour = ahead[beat.id]
        when = when_phrase(now, (day, hour))
        text = lead["text"]
        if text.split(" ", 1)[0] in ("The", "A", "An"):          # a name keeps its capital
            text = text[:1].lower() + text[1:]
        lead["text"] = f"{when[:1].upper()}{when[1:]}: {text}"
        lead["due"] = {"day": day, "hour": hour, "when": when}
        lead.update({"target": None, "route_next_hop_location_id": None})
        return lead
    target = getattr(beat, "lead_target", None) or {}
    lead.update(_target_fields(session, player, target.get("type"), target.get("id")))
    return lead


def _next_story_lead(session, campaign, beats: list) -> list:
    """The beat whose lead goes on an otherwise empty tracker: the live
    act's beat firing now, else the next act's first beat that carries a
    lead and waits on nothing unplayed (never a deadline). Round 7,
    fleet8-bram: both jobs paid by T16 and act 1 holding only a day-6 clock,
    `compute_lead` returned nothing from T17 on, and the DM filled the gap
    with a drover in the loft and an "old quarry track" that exists nowhere.
    A list of at most one beat."""
    from src.services.campaign import _predecessor_pending

    act = campaign.current_act or 1
    live = [b for b in beats if b.act == act and b.status == "fired" and (b.lead or "").strip()]
    if live:
        return live[:1]
    for beat in beats:
        if beat.act != act + 1 or beat.status not in ("pending", "armed") or not (beat.lead or "").strip():
            continue
        if beat.beat_type == "deadline_event" or _predecessor_pending(session, beat):
            continue
        return [beat]
    return []


def _clock_now(session) -> tuple[int, int]:
    from src.models import WorldClock

    clock = session.query(WorldClock).first()
    return (clock.day, clock.hour) if clock is not None else (1, 0)


def beat_due_later(session, beat, now: tuple[int, int]) -> tuple[int, int] | None:
    """(day, hour) when an armed beat's own happening is due, if that is
    still ahead of `now`; else None. Its scheduled twin event decides when
    there is one ("The Cart Limps In" is the Greywater cart, due at 09:00 —
    fired already, the beat's lead is true now); else a time-triggered beat
    whose lead tells the beat itself ("the cart that limped in short").

    The lead must speak of the happening: a twin's lead that names none of
    its event is a hook that stands now (fleet7-bram's spine titled a beat
    "The Evening Howl" with the lead "Sorrel Tack keeps looking toward the
    north wall"), and a lead that only talks of the event or says it has
    not come ("The cart from Greywater still hasn't come", "Everyone is
    talking about the Greywater cart") tells no happening (`told_happening`).
    Replayed over the round 1-6 spines: five such leads were dated ahead
    and stripped of their target; the five that tell the cart or the howl
    still are."""
    if getattr(beat, "status", None) != "armed":
        return None
    from src.models import Event
    from src.services.campaign import _distinctive, _text_forms, _word_forms
    from src.services.world_tick import is_scheduled_twin, told_happening

    lead = beat.lead or ""
    told = _text_forms(lead)

    def names_it(event) -> bool:
        return any(_word_forms(w) & told for w in _distinctive(event.name or ""))

    twins = [e for e in session.query(Event).filter(Event.scheduled_day.isnot(None)).all()
             if (is_scheduled_twin(e, beat.title or "", beat.delivery_keywords or [], beat.directive or "")
                 and names_it(e))
             or told_happening(e, lead)]
    if twins:
        if any(e.occurred_day is not None for e in twins):
            return None
        due = min((e.scheduled_day, e.scheduled_hour or 0) for e in twins)
        return due if due > tuple(now) else None
    if getattr(beat, "trigger_type", None) == "time" and beat.scheduled_day is not None:
        due = (beat.scheduled_day, beat.scheduled_hour or 0)
        # "Ask around whether anyone's seen something near the north wall"
        # under "The Watcher at the Wall" is an errand for now
        if due > tuple(now) and told_happening(
                {"name": beat.title or "", "description": beat.directive or ""}, lead):
            return due
    return None


def when_phrase(now: tuple[int, int], due: tuple[int, int]) -> str:
    """"tomorrow morning", "tonight", "at first light", "in 3 days"."""
    (day, hour), (due_day, due_hour) = now, due
    # Midnight to four is the night before first light, not first light
    part = ("night" if due_hour >= 20 or due_hour < 4 else "evening" if due_hour >= 17
            else "afternoon" if due_hour >= 12 else "morning" if due_hour > 6 else "first light")
    if due_day == day:
        if part == "first light":
            return "at first light"
        if part == "night":
            return "tonight"
        return f"in the {part}" if hour < 6 else f"this {part}"
    if due_day == day + 1:
        if part == "night" and due_hour < 4:
            return "tonight, after midnight"
        return "at first light tomorrow" if part == "first light" else f"tomorrow {part}"
    return f"in {due_day - day} days"


def _target_fields(session, player, target_type: str | None, target_id: str | None) -> dict[str, Any]:
    """`target` + `route_next_hop_location_id` for a resolved npc/location."""
    fields: dict[str, Any] = {"target": None, "route_next_hop_location_id": None}
    if not target_type or not target_id:
        return fields
    here_id = player.current_location_id

    if target_type == "npc":
        npc = session.get(NPC, target_id)
        if npc is None or npc.status == "dead":
            return fields
        location = session.get(Location, npc.current_location_id) if npc.current_location_id else None
        # An NPC standing somewhere the player has never found is a rumour,
        # not a waypoint: name them, point nowhere
        known = bool(location and location.discovered)
        fields["target"] = {
            "type": "npc",
            "id": npc.id,
            "name": npc.name,
            "location_id": location.id if known else None,
            "location_name": location.name if known else None,
            "here": bool(location and location.id == here_id),
        }
        if known and location.id != here_id:
            fields["route_next_hop_location_id"] = next_hop(session, player, location.id)
        return fields

    if target_type == "location":
        location = session.get(Location, target_id)
        if location is None:
            return fields
        fields["target"] = {
            "type": "location",
            "id": location.id,
            "name": location.name,
            "location_id": location.id,
            "location_name": location.name,
            "here": location.id == here_id,
        }
        if location.id != here_id:
            fields["route_next_hop_location_id"] = next_hop(session, player, location.id)
        return fields

    return fields


# ----------------------------------------------------------------------
# Proof in view
# ----------------------------------------------------------------------
# fleet5-tamsin T13-T18: the quest's proof (the Hollow's Cracked bone
# whistle) lay at her feet for six turns. The DM never put it in view, the
# lead sat on "Find out what is organizing the wolves" — a custom step only
# the DM can tick, never ticked — and she walked home without it, to a
# Coldiron asking "Did you see proof?". The engine knows where the proof
# lies: the brief says so (PROOF HERE / LEFT BEHIND, scene_brief.py) and the
# lead points at the thing itself while the player stands beside it.

_PROOF_KINDS = ("obtain", "deliver")
_HOLDING_KINDS = ("take", "open", "search")      # authored Things that hold an item
_THING_VERBS = {"take": "Take", "open": "Open", "search": "Search"}
_ITEM_STOP = {"the", "a", "an", "of", "some"}
# Money is one noun whatever the DM calls it: its "Silver Coin" WAS the
# crate's "Greywater Silver Piece" (fleet5-tamsin T12)
_MONEY_NOUNS = {"coin", "coins", "piece", "pieces", "penny", "pennies"}
MAX_LEFT_BEHIND = 3


def item_words(name: Any) -> set[str]:
    """An item name's words, articles dropped, money nouns as "coin"."""
    raw = str(name or "").lower().replace("'", "").replace("’", "")
    return {"coin" if w in _MONEY_NOUNS else w for w in re.findall(r"[a-z0-9]+", raw) if w not in _ITEM_STOP}


def same_item(wanted: Any, held: Any) -> bool:
    """Does `wanted` (a quest step's item) name the item `held`? The engine's
    own tick test (picking `held` up would tick the step), or every word of
    `wanted` in `held` ("the whistle" is the Cracked Bone Whistle)."""
    if not str(wanted or "").strip() or not str(held or "").strip():
        return False
    if objectives_mod._item_matches(str(wanted), [str(held)]):
        return True
    words = item_words(wanted)
    return bool(words) and words <= item_words(held)


def holding_things(location) -> list[dict[str, Any]]:
    """The authored Things at a place still holding an item (never a body or
    a bed: the dead's gear is loot, not a clue)."""
    if location is None:
        return []
    from src.services.scene_objects import normalize_objects

    try:
        objects = normalize_objects(location.objects)
    except Exception as e:
        logger.debug(f"objects unreadable at {getattr(location, 'id', '?')}: {e}")
        return []
    return [o for o in objects
            if o["kind"] in _HOLDING_KINDS and not o["consumed"] and (o["payload"].get("item") or {}).get("name")]


def _pack_names(player) -> list[str]:
    return [str(e.get("name") or "") for e in (getattr(player, "inventory", None) or []) if isinstance(e, dict)]


def proof_things(session, player, location) -> list[dict[str, Any]]:
    """The quest proof lying at `location`: one row per Thing holding the
    item of an undone obtain/deliver step of an active quest — unless the
    player already carries it. Rows: {"thing", "item", "quest_title",
    "objective_id", "locked"}; each Thing once."""
    things = holding_things(location)
    if not things or player is None:
        return []
    pack = _pack_names(player)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for quest in objectives_mod.active_quests_for(session, player):
        for step in objectives_mod.normalize_objectives(quest.objectives):
            item = step.get("item_name")
            if step.get("done") or step.get("kind") not in _PROOF_KINDS or not item:
                continue
            if any(same_item(item, name) for name in pack):
                continue
            for thing in things:
                held = thing["payload"]["item"]["name"]
                if thing["id"] not in seen and same_item(item, held):
                    seen.add(thing["id"])
                    rows.append({"thing": thing, "item": held, "quest_title": quest.title,
                                 "objective_id": step.get("id"), "locked": bool(thing["payload"].get("lock"))})
                    break
    return rows


def left_behind(session, player) -> list[dict[str, Any]]:
    """Proof the player walked away from: `proof_things` rows (plus
    "place") at the places they have stood in, other than where they are."""
    if player is None:
        return []
    here = getattr(player, "current_location_id", None)
    rows: list[dict[str, Any]] = []
    for location in session.query(Location).filter(Location.visited == True).all():  # noqa: E712
        if location.id == here or not location.objects:
            continue
        for row in proof_things(session, player, location):
            rows.append({**row, "place": location.name})
            if len(rows) >= MAX_LEFT_BEHIND:
                return rows
    return rows


def _thing_phrase(label: str) -> str:
    """"Cracked bone whistle" -> "the cracked bone whistle"; a proper name
    ("Maera's Satchel") keeps its capital."""
    words = str(label or "").split()
    if words and words[0].lower() in ("the", "a", "an"):
        words = words[1:]
    if words and not any(c.isupper() for c in words[0][1:]) and "'" not in words[0] and "’" not in words[0]:
        words[0] = words[0][:1].lower() + words[0][1:]
    return "the " + " ".join(words) if words else "it"


def _proof_lead(session, player, quest, objectives: list[dict[str, Any]],
                current: dict[str, Any]) -> dict[str, Any] | None:
    """The lead when a quest's proof is within reach, else None.

    The current step is obtain/deliver and a Thing HERE holds its item: the
    lead is that Thing ("Take the cracked bone whistle here"). The current
    step is custom (only the DM ticks it — fleet5-tamsin's "Find out what is
    organizing the wolves" froze the tracker for eight turns): a later
    obtain/deliver step whose item is here, or already in the pack, leads
    instead. A locked Thing never leads — its step's own words say how in.
    """
    if current.get("kind") in _PROOF_KINDS:
        steps = [current]
    elif current.get("kind") == "custom":
        at = next((i for i, o in enumerate(objectives) if o is current), len(objectives))
        steps = [o for o in objectives[at + 1:]
                 if not o.get("done") and o.get("kind") in _PROOF_KINDS and o.get("item_name")]
    else:
        return None
    here = session.get(Location, player.current_location_id) if player.current_location_id else None
    things = [t for t in holding_things(here) if not t["payload"].get("lock")]
    pack = _pack_names(player)
    for step in steps:
        item = step.get("item_name")
        if not item:
            continue
        lead = {
            "text": step["text"],
            "kind": step["kind"],
            "source": "quest",
            "quest_id": quest.id,
            "quest_title": quest.title,
            "objective_id": step["id"],
            "item_name": item,
        }
        if any(same_item(item, name) for name in pack):
            if step is current:
                return None                 # the ordinary lead: whoever it goes to
            if step["kind"] == "obtain":
                continue                    # carried already: "Get the pass" is no lead
            lead.update(_target_fields(session, player, step.get("target_type"), step.get("target_id")))
            return lead
        thing = next((t for t in things if same_item(item, t["payload"]["item"]["name"])), None)
        if thing is not None:
            # The Thing's label is public (the canvas draws it); what it
            # holds is not — a crate is searched, never "take the silver"
            lead["text"] = f"{_THING_VERBS.get(thing['kind'], 'Search')} {_thing_phrase(thing['label'])} here"
            lead["target"] = {"type": "object", "id": thing["id"], "name": thing["label"],
                              "location_id": here.id, "location_name": here.name, "here": True}
            lead["route_next_hop_location_id"] = None
            return lead
    return None


# ----------------------------------------------------------------------
# Routing
# ----------------------------------------------------------------------

def _adjacency(session, roads: set[tuple[str, str]] | None = None) -> dict[str, set[str]]:
    """Discovered roads (both ways where bidirectional) plus the hierarchy:
    a discovered child is reachable from its parent and vice versa — the
    same edges `get_available_destinations` offers the canvas. `roads`, when
    given, collects the (u, v) pairs that are authored roads."""
    adjacency: dict[str, set[str]] = {}

    def add(u: str, v: str) -> None:
        if u and v and u != v:
            adjacency.setdefault(u, set()).add(v)

    for conn in session.query(Connection).filter(Connection.discovered == True).all():  # noqa: E712
        add(conn.from_location_id, conn.to_location_id)
        if roads is not None:
            roads.add((conn.from_location_id, conn.to_location_id))
        if conn.bidirectional:
            add(conn.to_location_id, conn.from_location_id)
            if roads is not None:
                roads.add((conn.to_location_id, conn.from_location_id))
    for loc in session.query(Location).all():
        if loc.parent_id and loc.discovered:
            add(loc.parent_id, loc.id)
            add(loc.id, loc.parent_id)
    return adjacency


def next_hop(session, player, target_location_id: str | None) -> str | None:
    """The first location on the shortest known route to the target.

    Roads the player cannot walk (requirements, gates, dress codes) are
    pruned — EXCEPT when the locked destination IS the target: a lead that
    points at the far side of a gate should light the gate, not go dark.
    None when the target is here, undiscovered, or unreachable.
    """
    start = getattr(player, "current_location_id", None)
    if not start or not target_location_id or start == target_location_id:
        return None
    target = session.get(Location, target_location_id)
    if target is None or not target.discovered:
        return None

    roads: set[tuple[str, str]] = set()
    adjacency = _adjacency(session, roads)
    blocked_cache: dict[tuple[str, str], bool] = {}

    def blocked(u: str, v: str) -> bool:
        key = (u, v)
        if key not in blocked_cache:
            try:
                blocked_cache[key] = bool(check_route(session, u, v, player).get("blocked"))
            except Exception as e:
                logger.debug(f"route check failed for {u}->{v}: {e}")
                blocked_cache[key] = True
        return blocked_cache[key]

    previous: dict[str, str | None] = {start: None}
    queue: deque[str] = deque([start])
    while queue:
        u = queue.popleft()
        # An authored road before a hop through a parent: the seed's Hollow
        # is two steps from the square both by the gatehouse's north road and
        # out through the town node, and the tracker must light the gatehouse
        for v in sorted(adjacency.get(u, ()), key=lambda v: ((u, v) not in roads, v)):
            if v in previous:
                continue
            if v != target_location_id and blocked(u, v):
                continue
            previous[v] = u
            if v == target_location_id:
                # Walk back to the hop right after `start`
                node = v
                while previous[node] != start:
                    node = previous[node]
                    if node is None:
                        return None
                return node
            queue.append(v)
    return None


def way_in_blocked(session, player, target_location_id: str | None) -> bool:
    """Does every known way into the target refuse this player (a
    requirement they hold nothing for, the gate guard, a dress code, their
    standing)? `next_hop` still lights the gate, but a road shut to them is
    the world's wall, not a road left untaken (fleet7-bram T13-T19)."""
    if not target_location_id:
        return False
    ways = sorted(u for u, vs in _adjacency(session).items() if target_location_id in vs)
    for u in ways:
        try:
            if not check_route(session, u, target_location_id, player).get("blocked"):
                return False
        except Exception as e:
            logger.debug(f"route check failed for {u}->{target_location_id}: {e}")
    return bool(ways)


# ----------------------------------------------------------------------
# DM context
# ----------------------------------------------------------------------

def format_lead_context(lead: dict[str, Any] | None) -> str:
    """The block the DM reads. Empty lead → an order to create one."""
    if not lead:
        return NO_LEAD_CONTEXT
    lines = ["[PLAYER'S LEAD — this exact line is on the player's screen right now]"]
    origin = f'  "{lead.get("text", "")}"'
    if lead.get("pays"):
        origin += f' — pays {lead["pays"]}'
    if lead.get("source") == "quest" and lead.get("quest_title"):
        origin += f"  (quest: {lead['quest_title']})"
    elif lead.get("source") == "beat":
        origin += "  (from the story spine)"
    lines.append(origin)

    target = lead.get("target") or {}
    if target:
        name = target.get("name") or "the target"
        if target.get("here"):
            where = target.get("location_name")
            lines.append(f"  {name} is here" + (f" in {where}." if where else "."))
        elif target.get("location_name"):
            hop = lead.get("route_next_hop_location_id")
            if hop and hop != target.get("location_id"):
                lines.append(f"  {name} is at {target['location_name']}; the way there from here is known.")
            else:
                lines.append(f"  {name} is at {target['location_name']}.")
        else:
            lines.append(f"  {name} — the player does not yet know where.")
        if target.get("type") == "object":
            lines.append(f"  It is one of the Things here: put it in plain view this reply; what it holds "
                         f"is theirs only through @interact {name}.")
    due = lead.get("due") or {}
    if due:
        # fleet7-tamsin T19-T20: a present-tense lead about tomorrow's cart,
        # and the DM showed its wreckage being picked over at midnight
        lines.append(
            f"  It has NOT happened yet: it is due {due.get('when')} (day {due.get('day')}, "
            f"{int(due.get('hour') or 0):02d}:00). Until then nothing of it is in the world — nobody\n"
            "  has seen it and nobody speaks of it as done; never narrate it early. If the player\n"
            "  waits for it, `@time` up to its hour and tell it then."
        )
    else:
        lines.append(
            "  Write as if the player knows this is what they are doing. Never contradict it,\n"
            "  never tell them to do something else instead, and if they follow it, let it pay off."
        )
    # fleet5: the screen used to show these names verbatim, and every player
    # asked "Coldiron?" before anyone had said it. It no longer does.
    lines.append(
        "  On their screen a person appears only by the part of their name they have heard said,\n"
        "  or by their role until then — never assume they read a name here."
    )
    if lead.get("source") == "beat":
        lines.append(
            "  This line came from the story spine. It is the ONLY thing you know about any\n"
            "  beat that has not fired — infer nothing else from it and reveal nothing more."
        )
    return "\n".join(lines)

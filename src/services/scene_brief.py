"""The scene brief: everything the DM needs this turn, assembled by code.

The DM used to spend its first four or five model round-trips on read
tools — get_current_location, get_npcs_at_location, get_available_
destinations, get_player, get_world_state_summary — for facts the engine
already had (measured 2026-10-04: five reads before the opening turn's first
word). Then it called an NPC sub-agent per conversation, which re-read half
of them again.

The brief is that whole read pass done in code, once, before the model
starts: where the player stands, the ways out (with what blocks them), every
named person in the room with a dossier — who they are, how they talk, what
they want, what they remember of the player, what work they could offer,
what they would confess — and the places and people the player already
knows. The DM voices NPCs itself from these dossiers. Knowledge is gated in
code: a secret's words are in the brief only once trust is high enough to
say them.

Since the DM has no read tools at all, every answer the engine would give
is written here before it is asked: whether someone would join the party
(the same gate `join_party` runs), what a merchant sells and at what price
for THIS player (the same numbers the counter charges), and the price list
the catalog owns. What an NPC remembers comes from their memory file
(`memory_files`); trust and disposition come from the database.

Names are gated too (playtest 2026-10-04: the DM labelled an unseen voice
"Vex Marrow:" and NPCs called the player by a name nobody had said). A
person the player has not met leads their dossier with a role label
(`dialogue.role_label`) the DM must use until they introduce themselves,
and every dossier says whether they know the player's name. The place's
authored objects are listed as "Things here", because a DM that never
saw them invented substitute clues for the real ones.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

from src.models import (
    NPC,
    Connection,
    DMState,
    Location,
    NPCRelationship,
    Player,
    Quest,
    QuestStatus,
    WorldClock,
)

logger = logging.getLogger(__name__)

RECENT_EXCHANGES_PRESENT = 3     # remembered utterances per bystander
MAX_KNOWN_PEOPLE = 14
MAX_KNOWN_PLACES = 40
MAX_LOST_PEOPLE = 8
MAX_STOCK_LINES = 10
DESCRIPTION_CHARS = 420

# A used-up scene object, as the DM should read it
_SPENT = {"take": "taken", "open": "opened", "search": "searched", "body": "searched"}


def _clip(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _as_list(value: Any) -> list:
    """A JSON list column as a list. Authored data is not always the shape
    the model declares — a bare string is one entry, not one per character."""
    if isinstance(value, list):
        return [v for v in value if v not in (None, "")]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _indices(value: Any) -> set[int]:
    """revealed_secrets as ints; anything else in the list is ignored."""
    out: set[int] = set()
    for v in _as_list(value):
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            out.add(v)
        elif isinstance(v, str) and v.strip().isdigit():
            out.add(int(v.strip()))
    return out


def _relationship(session, player_id: str, npc_id: str) -> NPCRelationship | None:
    return session.query(NPCRelationship).filter(
        NPCRelationship.npc_id == npc_id, NPCRelationship.player_id == player_id
    ).first()


def _whole_town(location_id: str) -> bool:
    try:
        from src.services.commands import whole_town

        return bool(whole_town(location_id))
    except Exception:
        return False


# A road's time in the words a person would say, never raw minutes:
# fleet10-tamsin T10 "The North Gatehouse waits three minutes off" (the brief
# said "walk, 3 min"), and with no words to repeat, people invented their own
# scale — fleet10-wenna T1 "half a day's walk", T11 "maybe two hours' walk",
# fleet10-tamsin T2 "a half-day up the north road", all for a 45-minute road.
# (upper bound in hours, words); turn_audit's distance check reads the same scale
_TRAVEL_WORDS = (
    (0.08, "a few steps"),
    (0.25, "a short walk"),
    (0.5, "under half an hour"),
    (0.99, "under an hour"),
    (1.5, "about an hour"),
    (3.0, "a couple of hours"),
    (5.0, "a few hours"),
    (9.0, "half a day"),
    (15.0, "most of a day"),
    (30.0, "about a day"),
)


def travel_words(hours: float) -> str:
    """'a few steps', 'under an hour', 'most of a day' … for a road's hours."""
    for bound, words in _TRAVEL_WORDS:
        if hours <= bound:
            return words
    return f"about {max(2, round(hours / 24))} days"


def _exits(session, player: Player, location: Location) -> list[str]:
    """One line per way out, with what would stop the player — its time in
    words (`travel_words`)."""
    from src.services.travel import check_route

    seen: set[str] = set()
    rows: list[tuple[Location, str]] = []
    conns = session.query(Connection).filter(
        ((Connection.from_location_id == location.id)
         | ((Connection.to_location_id == location.id) & (Connection.bidirectional == True)))  # noqa: E712
        & (Connection.discovered == True)  # noqa: E712
    ).all()
    for conn in conns:
        other_id = conn.to_location_id if conn.from_location_id == location.id else conn.from_location_id
        other = session.get(Location, other_id)
        if other and other.id not in seen:
            seen.add(other.id)
            rows.append((other, conn.travel_type or "walk"))
    for child in session.query(Location).filter(Location.parent_id == location.id).all():
        if child.id not in seen and (child.discovered or child.visited):
            seen.add(child.id)
            rows.append((child, "inside"))
    if location.parent_id and location.parent_id not in seen:
        parent = session.get(Location, location.parent_id)
        # The whole town is not a place to stand (@move refuses it) — listing
        # it as a way out only invited the refusal
        if parent and not _whole_town(parent.id):
            rows.append((parent, "out"))

    lines = []
    for dest, how in rows:
        try:
            route = check_route(session, location.id, dest.id, player)
        except Exception:
            route = {"blocked": False, "travel_time_hours": 0.1}
        hours = float(route.get("travel_time_hours") or 0.1)
        span = travel_words(hours)
        note = ""
        if route.get("blocked"):
            reason = route.get("reason") or "route"
            needs = ", ".join(str(r) for r in route.get("requirements") or [])
            note = f" — BLOCKED ({reason}{': ' + needs if needs else ''})"
        lines.append(f"  · {dest.name} [{dest.id}] ({how}, {span}){note}")
    return lines


def _night_ways(session, player: Player, location: Location) -> list[str]:
    """The ways still open when the night bars the road the player's quests
    point down (or the one at their feet). All three round-8 sessions met the
    20:00 bar as a dead end ("The north road isn't going anywhere before
    morning" — fleet9-bram T12) and the evening had no danger left: a price,
    a climb, a bed till first light are each a choice."""
    from collections import deque

    from src.services import objectives as objectives_mod
    from src.services.complications import _place_setting
    from src.services.rest import rest_here
    from src.services.travel import barred_tonight

    targets: set[str] = set()
    for quest in objectives_mod.active_quests_for(session, player):
        for step in objectives_mod.normalize_objectives(quest.objectives):
            if not step.get("done") and step.get("kind") == "go_to" and step.get("target_id"):
                targets.add(step["target_id"])
    roads, barred = [], set()
    conns = session.query(Connection).filter(Connection.discovered == True).all()  # noqa: E712
    for conn in conns:
        if not barred_tonight(conn):
            continue
        barred.add(conn.id)
        ends = (conn.from_location_id, conn.to_location_id)
        if targets & set(ends):
            far_id = next(e for e in ends if e in targets)
        elif location.id in ends and _place_setting(session, location) != "wild":
            # the gate at their feet — never the way back in from outside
            far_id = ends[1] if ends[0] == location.id else ends[0]
        else:
            continue
        near_id = ends[0] if far_id == ends[1] else ends[1]
        far, near = session.get(Location, far_id), session.get(Location, near_id)
        if far is not None and near is not None and far.id != location.id:
            roads.append((near, far))
    if not roads:
        return []
    # A bed: here, else the nearest inn on this side of the bar
    inn, seen, queue = None, {location.id}, deque([location])
    adjacency: dict[str, set[str]] = {}
    for conn in conns:
        if conn.id not in barred:
            adjacency.setdefault(conn.from_location_id, set()).add(conn.to_location_id)
            adjacency.setdefault(conn.to_location_id, set()).add(conn.from_location_id)
    while queue and inn is None:
        place = queue.popleft()
        spec = rest_here(place)
        if spec and spec.get("kind") == "inn":
            inn = (place, spec)
            break
        for other_id in sorted(adjacency.get(place.id, ())):
            if other_id not in seen:
                seen.add(other_id)
                other = session.get(Location, other_id)
                if other is not None:
                    queue.append(other)
    from src.services.dialogue import player_has_met, stranger_label

    lines = []
    for near, far in roads[:2]:
        # ...a keeper the player never met by the label they know (the line
        # is the DM's, but a name in it is a name the DM may say)
        keepers = []
        for n in session.query(NPC).filter(NPC.current_location_id == near.id, NPC.status == "alive").all():
            if _is_beast_row(n):
                continue
            label = stranger_label(n, met=player_has_met(session, player.id, n.id))
            the = label if (label or "").lower().startswith("the ") else f"the {label}"
            keepers.append(f"{the} ({n.name}, a name the player has not learned)" if label else n.name)
        who = f"{', '.join(keepers[:2])} at {near.name}" if keepers else f"the watch on it at {near.name}"
        bed = ""
        if inn is not None:
            price = int(inn[1].get("price") or 0)
            bed = (f";\n    · the night — a bed till first light: @rest at {inn[0].name} "
                   f"({'free' if not price else str(price) + ' coin'}) wakes them at dawn with the way open")
        lines.append(
            f"Tonight the way to {far.name} is barred at {near.name} until first light — shut, not a dead end. "
            "When you say it is shut, name at least one way still open, and let the player choose:\n"
            f"    · a price — whoever keeps it ({who}) can be bought: a bribe (@coin -N, the reason naming the "
            f"gate), then @move {far.name} | through;\n"
            "    · the wall — a hard climb in the dark: roll_check FIRST (athletics, hard); a win is "
            f"@move {far.name} | through, a loss is a fall or a lantern turned their way — and the dark past "
            "the wall has teeth (something comes to the noise)" + bed + "."
        )
    return lines


def _is_beast_row(npc: NPC) -> bool:
    try:
        from src.services.dialogue import is_beast

        return bool(is_beast(npc))
    except Exception:
        return False


def _holds(obj: dict[str, Any], payload: dict[str, Any]) -> str:
    """' — reads: "…"; holds: Bone Whistle (…)' for one object, DM eyes only."""
    if obj.get("kind") in ("body", "rest"):
        return ""
    parts = []
    if payload.get("text"):
        parts.append(f"reads/shows: \"{_clip(payload['text'], 220)}\"")
    item = payload.get("item") or {}
    if item.get("name"):
        about = f" ({_clip(item.get('description'), 160)})" if item.get("description") else ""
        # A document's own words, whole: the reply that writes @interact tells
        # them, and fleet6-wenna T17's DM, shown only who the satchel letter
        # was addressed to, wrote the letter itself (three sealed truths)
        reads = (f", which reads, word for word: \"{item['reads']}\" (quote it exactly; it says nothing more)"
                 if item.get("reads") else "")
        parts.append(f"holds: {item['name']}{about}{reads}")
    if payload.get("lock"):
        parts.append(f"lock: {_clip(payload['lock'], 120)}")
    # Marked on every line, not once above: the unread carving's "SING" and
    # the whistle's "same make" reached the prose of replies that never
    # touched them (fleet4-tamsin T9, fleet4-wenna T12)
    return (" — UNSEEN by the player until @interact: " + "; ".join(parts)) if parts else ""


def _coming_up(session, clock: WorldClock | None) -> list[str]:
    """One line per public event still scheduled later today (or just past
    midnight), with how long until it. fleet6-bram T6/T7 and fleet6-wenna T8:
    the 20:00 howl was told at 19:30 and 19:50, then fired on the hour and
    was told again — two howls in a mystery whose hook is that it is never
    early."""
    from src.models import Event

    if clock is None:
        return []
    now = clock.hour * 60 + (clock.minute or 0)
    try:
        rows = session.query(Event).filter(
            Event.scheduled_day.in_((clock.day, clock.day + 1)),
            Event.occurred_day.is_(None),
            Event.player_visible == True,  # noqa: E712
        ).all()
    except Exception as e:
        logger.debug(f"scheduled events unavailable: {e}")
        return []

    def minutes_to(event) -> int:
        return (event.scheduled_day - clock.day) * 24 * 60 + (event.scheduled_hour or 0) * 60 - now

    lines = []
    for event in sorted(rows, key=minutes_to):
        hour = event.scheduled_hour or 0
        minutes = minutes_to(event)
        if minutes <= 0:
            continue          # due now: the world tick fires it this turn
        if event.scheduled_day != clock.day and minutes > SOON_MINUTES:
            continue          # tomorrow's, and not near yet
        if minutes <= SOON_MINUTES:
            # fleet8-tamsin T9: "not eight o'clock yet, not by a long count"
            # a quarter hour before the 20:00 howl. Near its hour, the hour
            # and the minutes left in plain words, and how close that is
            lines.append(
                f"Coming up: {event.name} at {hour:02d}:00 — {minutes} minutes from now ({_how_soon(minutes)}; "
                "anyone who speaks of it knows how close it is). It has not happened yet; never "
                "narrate it, or anyone hearing or seeing it, before then; if the player waits for it, "
                f"write @time {minutes}m and it happens in that reply."
            )
            continue
        until = f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d} min"
        lines.append(
            f"Coming up: {event.name} at {hour:02d}:00 (in {until}) — it has not happened yet; never "
            "narrate it, or anyone hearing or seeing it, before then; if the player waits for it, "
            f"write @time {minutes}m and it happens in that reply."
        )
    return lines


# Within this many minutes a scheduled event is "near": the brief spells out
# the hour and the minutes left
SOON_MINUTES = 120


def _how_soon(minutes: int) -> str:
    if minutes <= 20:
        return "any minute now"
    if minutes <= 45:
        return "well inside the hour"
    if minutes <= 90:
        return "about an hour off"
    return "under two hours off"


def _things_here(location: Location) -> list[str]:
    """The place's authored objects by label and kind — never what they hold.

    Playtest 2026-10-04: the Hollow's whistle, crate and carved word were
    never found because the DM never saw them; it invented a notched branch
    and handed it over instead. `public_objects` carries no payload, so an
    item inside or a page's text cannot reach the brief.
    """
    from src.services import scene_objects

    try:
        objects = scene_objects.public_objects(location.objects)
    except Exception as e:
        logger.debug(f"objects unavailable for {location.id}: {e}")
        return []
    usable = [o for o in objects if not o["consumed"]]
    spent = [o for o in objects if o["consumed"]]
    # What each one holds, for the DM only. Round-2 autoplay: @interact and
    # its narration share one reply, so a DM that could not see the notice's
    # words narrated different ones, and an invented clue replaced the
    # authored one. Bodies stay sealed (what is on the dead is found, never told).
    try:
        held = {o["id"]: o.get("payload") or {} for o in scene_objects.normalize_objects(location.objects)}
    except Exception:
        held = {}
    lines = []
    if usable:
        lines.append(
            "Things here (authored — this place's real clues and goods. The player uses them on the "
            "canvas, or you resolve a typed attempt with @interact <thing>. When they search or look "
            "for proof here, it is one of these — never invent a substitute. What each holds is for "
            "YOU: reveal it only when they actually use the thing, and then say exactly that. Until "
            "the reply that writes @interact <thing>, no word of it — not a quoted word, not a "
            "detail, not a memory of it — goes in prose or anyone's mouth):"
        )
        for obj in usable:
            lock = ", locked" if obj["locked"] else ""
            lines.append(f"  · {obj['label']} ({obj['kind']}{lock}){_holds(obj, held.get(obj['id']) or {})}")
    if spent:
        lines.append("Already used up here: " + "; ".join(
            f"{o['label']} ({_SPENT.get(o['kind'], 'used')})" for o in spent))
    return lines


def _proof_lines(session, player: Player, location: Location) -> list[str]:
    """The quest's proof, where it lies — here, or where they walked away from it.

    fleet5-tamsin T13-T18: the Hollow's Cracked bone whistle (the act-1
    proof) went unmentioned for six turns, she left without it, and at T20
    Coldiron asked "Did you see proof?". A Things-here line among three was
    not enough to make the DM show it; this line is the order to."""
    from src.services.lead import _thing_phrase, left_behind, proof_things, same_item

    lines: list[str] = []
    try:
        for row in proof_things(session, player, location):
            label = row["thing"]["label"]
            how = (f"once the fiction opens it (locked: {_clip(row['thing']['payload'].get('lock'), 100)}), "
                   if row["locked"] else "")
            if same_item(label, row["item"]):
                # The whistle IS the proof: "Cracked bone whistle holds Cracked
                # Bone Whistle … not what it holds" read as a riddle
                lines.append(
                    f"PROOF HERE: {label} — the {row['item']} itself (for \"{row['quest_title']}\") — put "
                    f"{_thing_phrase(label)} in plain view in this reply, where it lies; it becomes theirs "
                    f"{how}through @interact {label}."
                )
                continue
            lines.append(
                f"PROOF HERE: {label} holds {row['item']} (for \"{row['quest_title']}\") — put "
                f"{_thing_phrase(label)} in plain view in this reply (the thing itself, not what it holds); "
                f"it becomes theirs {how}through @interact {label}."
            )
        for row in left_behind(session, player):
            lines.append(
                f"LEFT BEHIND: they left {_thing_phrase(row['thing']['label'])} behind at {row['place']} — "
                f"{row['item']}, the proof for \"{row['quest_title']}\", is still there and they do NOT "
                "carry it; anyone who asks for it hears they have not got it."
            )
    except Exception as e:  # the proof is never worth the turn
        logger.debug(f"proof lines unavailable at {location.id}: {e}")
    return lines


def _unfound_proof(session, player: Player, location: Location) -> list[str]:
    """The quests' proof the player has never held, named for the DM only
    (`dialogue.unfound_steps`; their quest log says "proof"). fleet10-bram
    T1: "Find the whistle, or the collar" from the quest-giver, fifteen
    turns before anyone found it — the step named it. Proof lying HERE has
    its own PROOF HERE line: the find is this scene. Never raises."""
    from src.services.dialogue import unfound_steps
    from src.services.lead import proof_things, same_item

    try:
        rows = unfound_steps(session, player, named_only=False)
        if not rows:
            return []
        here = [r["item"] for r in proof_things(session, player, location)]
        named = []
        for row in rows.values():
            if any(same_item(row["item"], item) for item in here):
                continue
            entry = f"the {row['item']} (\"{row['quest_title']}\": \"{_clip(row['step_text'], 90)}\")"
            if entry not in named:
                named.append(entry)
    except Exception as e:
        logger.debug(f"unfound proof line skipped: {e}")
        return []
    if not named:
        return []
    # ...the thing itself, as what the job wants — never the clues that point
    # at it: a witness's "a sound like a whistle", a beat's own words or a
    # secret their dossier lets them tell are the investigation (fleet10-bram
    # T3/T6 were praised for exactly those, before the T15 find)
    return ["NOT FOUND YET — the player has not found this, so nobody names it yet as what the job wants, in "
            "speech or prose (their quest log says only \"proof\"): " + "; ".join(named) + ". Whoever speaks of "
            "what the job needs says \"proof\", \"whatever is behind it\" — never the thing itself — until the "
            "player holds it or stands where it lies. What a witness saw or heard, a beat's words or a secret "
            "their dossier lets them tell still say what they say."]


def _quests_offered(session, npc_id: str, player_id: str | None = None, *, player: Player | None = None) -> list[str]:
    """One line per authored job this person can hand out, its pay in plain
    words: fleet5-tamsin T0 offered a thief the main job with no word of pay
    (it paid 60 coin and a charm), so nothing was there to haggle over. A job
    the story keeps for a later act is left out, as the offer directive
    leaves it out (round 5: pitched from the dossier, it spent an act-3
    confession's premise in act 2). Its steps as written, the first with
    where its work is (`job_offers.job_steps`): fleet9-bram T17-T18, the
    Keeper's pitch put the satchel "upstairs" behind a key and said "look,
    don't take", while the step said take the letter from it."""
    from src.services.job_offers import job_steps
    from src.services.objectives import objectives_public
    from src.tools.world_write.quests import reward_factions, reward_words

    try:
        from src.services.campaign import later_act_quest_ids

        kept = later_act_quest_ids(session, player_id) if player_id else set()
    except Exception:  # no campaign, or an odd one: every job shows
        kept = set()
    out = []
    worked: dict[str, list[dict[str, str]]] | None = None
    for q in session.query(Quest).filter(
        Quest.status == QuestStatus.NOT_STARTED, Quest.assigned_by_npc_id == npc_id
    ).all():
        if q.id in kept:
            continue
        if worked is None:
            worked = _worked_unstarted(session, player_id)
        done = worked.get(q.id) or []
        try:
            steps = job_steps(session, q, player, skip=[d["id"] for d in done])
        except Exception as e:  # the first step's words alone still say it
            logger.debug(f"job steps unavailable for {q.id}: {e}")
            first = next((o.get("text") for o in objectives_public(q) if isinstance(o, dict)), "")
            steps = f"1) {first}" if first else ""
        try:
            pay = reward_words(q.rewards, factions=reward_factions(session, q.rewards))
        except Exception:  # an odd reward shape never costs the dossier
            pay = ""
        # Work the player already did on it is no step to ask for again
        # (job_offers: credited on the start)
        did = ("; ".join(f'"{_clip(d["text"], 90)}"' for d in done))
        out.append(f'"{q.title}" [{q.id}]' + (f" (pays {pay})" if pay else "")
                   + f" — {_clip(q.description, 160)}"
                   + (f" Already done by the player, unasked (credit it; the start ticks it): {did}." if did else "")
                   + (f" Steps{' left' if did else ''}: {steps}" if steps else ""))
    return out


def _worked_unstarted(session, player_id: str | None) -> dict[str, list[dict[str, str]]]:
    """{quest id: [{"id", "text"}]} — the steps play already did on each job
    not yet started: credit on a job nobody offered (`job_offers.credited`),
    and an open offer's credit and steps met since its pitch. {} when
    unreadable."""
    if not player_id:
        return {}
    try:
        from src.services import job_offers
        from src.services.objectives import normalize_objectives

        out = job_offers.credited(player_id)
        offers = job_offers.open_offers(player_id)
        for quest_id, offer in offers.items():
            ids = set(offer.get("credit") or []) | set(offer.get("met") or [])
            quest = session.get(Quest, quest_id) if ids and offer.get("status") != "started" else None
            if quest is not None and quest.status == QuestStatus.NOT_STARTED:
                out[quest_id] = [{"id": o["id"], "text": o["text"]}
                                 for o in normalize_objectives(quest.objectives) if o["id"] in ids]
        return out
    except Exception as e:  # the dossier never waits on the offer book
        logger.debug(f"worked steps unreadable for {player_id}: {e}")
        return {}


def _recognition(session, player: Player, npc: NPC) -> str:
    """How this NPC reads the player — known on sight, or only by clothes."""
    try:
        from src.services import perception

        verdict = perception.recognition(session, player, npc)
        profile = perception.profile_for(session, player)
        if verdict.get("verdict") == "contested":
            check = verdict.get("check") or {}
            ask = ""
            if check.get("skill") and check.get("difficulty"):
                ask = (f' — before they name you, roll_check skill "{check["skill"]}", difficulty '
                       f'"{check["difficulty"]}", about_npc_id "{npc.id}"')
            return f"has met you but your face is covered ({profile['read_as']}) — not placed you yet{ask}"
        if verdict.get("identity_known"):
            return "knows you on sight"
        faction = profile.get("uniform_faction_name")
        seen = profile.get("read_as") or "a stranger"
        return f"does not know you — sees {seen}" + (f", treats you as {faction}" if faction else "")
    except Exception as e:  # never cost the turn
        logger.debug(f"recognition unavailable for {npc.id}: {e}")
        return ""


def _memory_lines(player: Player, npc: NPC, *, addressed: bool,
                  unnamed: bool = False) -> tuple[list[str], bool]:
    """(dossier lines, have they ever spoken) from the NPC's memory file.

    The one being addressed brings their whole file; a bystander brings
    what they know of the player and the last few lines between them.
    `unnamed`: they do not know the player's name, so the file's copy of it
    reads "you" (`dialogue.without_player_name`).
    """
    from src.services import memory_files
    from src.services.dialogue import without_player_name

    def shown(text: str) -> str:
        return without_player_name(text, player.name) if unnamed else text

    try:
        parts = memory_files.memory_parts(player.id, npc.id)
    except Exception as e:  # memory is never worth the turn
        logger.debug(f"memory unavailable for {npc.id}: {e}")
        return [], False
    spoken = bool(parts["recent"] or parts["earlier"].strip())
    lines: list[str] = []
    if addressed:
        text = memory_files.read_npc_memory(player.id, npc.id)
        body = [line for line in text.splitlines() if line.strip() and not line.startswith("# ")]
        # An empty section is just a heading: leave it out of the brief
        body = [line for i, line in enumerate(body)
                if not line.startswith("## ") or (i + 1 < len(body) and not body[i + 1].startswith("## "))]
        if body:
            lines.append("    Their memory of you (their file — what they know, what was said"
                         + ("; they never heard your name, so it says 'you'" if unnamed else "") + "):")
            lines.extend(f"      {shown(line)}" for line in body)
        return lines, spoken
    if parts["notes"]:
        lines.append("    Knows of you: " + "; ".join(_clip(shown(n), 140) for n in parts["notes"][-6:]))
    recent = parts["recent"][-RECENT_EXCHANGES_PRESENT:]
    if recent:
        lines.append("    Last words between you: " + " | ".join(_clip(shown(r), 180) for r in recent))
    return lines, spoken


def _join_line(session, player: Player, npc: NPC) -> str:
    """The answer join_party would give right now — the same gate."""
    from src.services import party as party_mod

    try:
        why = party_mod.can_join(session, player, npc)
    except Exception as e:
        logger.debug(f"join gate unavailable for {npc.id}: {e}")
        return ""
    if why is None:
        return "    Would come along if asked: yes"
    return f"    Would come along if asked: no — {_clip(why.get('message') or why.get('error'), 160)}"


def _kind_label(entry: dict[str, Any]) -> str:
    from src.services import progression

    mech = progression.mechanize_item(entry) or {}
    if mech.get("slot"):
        return f"{progression.tier_label(mech.get('tier')) or 'common'} {mech['slot']}"
    if mech.get("effect_tag"):
        return f"potion: {mech['effect_tag']}"
    return str(entry.get("type") or "misc").replace("_", " ")


def _upgrade_note(player: Player, entry: dict[str, Any], price: int) -> str:
    """" — they can afford this, better than their Worn Knife" for gear their
    purse reaches that beats the best they carry for that slot (or fills an
    empty one). fleet4-bram/-wenna ended on 72-85 unspent coin, the crude
    starting knife and bare armor and trinket slots, while Hesk sold a
    hatchet two stalls away: coin only feels spendable when the DM can see
    what it would buy.

    Crude gear adds nothing in any slot (every tier table starts at 0), so
    an empty slot counts as crude. A faction's uniform or a concealing hood
    is never pushed: what it costs is who the wearer becomes, not coin.

    Gear no better than what they already carry for that slot says so, at
    any price: fleet9-tamsin T4-T5, Hesk sold her a crude Gutting Knife for
    10 of her 25 coin beside the crude Worn Knife she wore; it sat in the
    bag and the fight used the old one."""
    from src.services import progression

    mech = progression.mechanize_item(entry) or {}
    slot, tier = mech.get("slot"), mech.get("tier")
    if not slot or tier is None:
        return ""
    attire = entry.get("attire") if isinstance(entry.get("attire"), dict) else {}
    if attire.get("uniform_faction_id") or attire.get("concealing"):
        return ""
    best: tuple[int, str | None] = (0, None)
    for item in progression._inventory(player):
        held = progression.mechanize_item(item) or {}
        if held.get("slot") == slot and held.get("tier") is not None and (best[1] is None or held["tier"] > best[0]):
            best = (held["tier"], str(item.get("name") or "?"))
    if best[1] and tier <= best[0]:
        return (f" — no better than the {best[1]} they carry (if they ask for it, the seller says so, or "
                "steers them to something that would help)")
    if price > int(player.currency or 0) or tier <= best[0]:
        return ""
    if not progression.can_equip(player, entry)[0]:
        return ""                    # beyond their level: buying it changes nothing yet
    return f" — they can afford this, better than their {best[1]}" if best[1] \
        else f" — they can afford this, and they have no {slot}"


def _stock_line(session, player: Player, npc: NPC) -> str:
    """Their shelf priced for THIS player — the numbers the counter charges."""
    from src.services import standing
    from src.tools.world_write.items import _validate_inventory

    try:
        mul, reason = standing.price_multiplier(session, player, npc)
        goods = []
        for entry in _validate_inventory(_as_list(npc.inventory_notable))[:MAX_STOCK_LINES]:
            if str(entry.get("type") or "") == "quest_item":
                continue        # a key or clue they carry is never for sale (the seed's Gate Pass)
            price = standing.buy_price(standing.base_price(entry), mul)
            qty = int(entry.get("quantity", 1) or 1)
            goods.append(
                f"{entry.get('name', '?')}{f' x{qty}' if qty > 1 else ''} ({_kind_label(entry)}) "
                + (f"{price} coin" if price else "free")
                + _upgrade_note(player, entry, price)
            )
    except Exception as e:
        logger.debug(f"stock unavailable for {npc.id}: {e}")
        return ""
    if not goods:
        return ""
    return (
        f"    Sells (prices for you{'; ' + reason if reason else ''} — a sale happens only by writing "
        f"@buy <item> | from: {npc.name}): " + "; ".join(goods)
    )


def _price_list(location: Location) -> list[str]:
    """The catalog's numbers, so a quoted price is never invented."""
    from src.services import progression
    from src.services.rest import rest_here

    sold = progression.TIER_LABELS[: progression.LEGENDARY_INDEX]
    lines = ["Price list (the engine's base prices — a merchant's feelings shift them; their Sells line is exact):"]
    for slot in progression.SLOTS:
        row = " · ".join(f"{tier} {progression.TIER_PRICE[slot][i]}" for i, tier in enumerate(sold))
        lines.append(f"  {slot.capitalize()}: {row} coin")
    potions = " · ".join(f"{tag} {spec['price']}" for tag, spec in progression.CONSUMABLES.items())
    lines.append(f"  Potions: {potions} coin. Legendary gear is never sold — it is won.")
    try:
        spec = rest_here(location)
    except Exception:
        spec = None
    if spec:
        price = int(spec.get("price") or 0)
        lines.append(
            f"  A night's sleep here: {'free' if not price else str(price) + ' coin'} "
            f"({spec.get('kind')}, {spec.get('quality')} bed) — write @rest."
        )
    return lines


# A report back to whoever gave the job is answered with something NEW.
# fleet9-tamsin T16-T19: Piper and the player retold the howl relay and the
# wolf to the Captain, and he added only the pass; his hidden weakness
# ("hiding how bad it really is") stayed hidden, and nothing moved
MAX_NEWS = 2


def _stamp(m) -> tuple[int, int, int]:
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


_FOUND = re.compile(r"^Found\s+(.+?)(?:\s+in\s+the\s+.*|:\s.*)?$")


def _news_since(player: Player, npc: NPC, items: Iterable[str] = ()) -> list[str]:
    """The clues the player learned after they last spoke with `npc` — the
    journal's entries (what the engine keeps of reads, finds and the story's
    beats; chatter is not news), newest first. [] when the two never spoke.
    A find is news only when it is one of the job's own `items`: fleet9-bram's
    report to the Captain led with a coil of rope out of a market crate."""
    from src.services import memory_files
    from src.services.lead import same_item

    text = memory_files.npc_memory_texts(player.id).get(npc.id) or ""
    stamps = [_stamp(m) for line in text.splitlines() for m in [_EXCHANGE.match(line.strip())] if m]
    if not stamps:
        return []
    last = max(stamps)
    items = [i for i in items if i]
    news = []
    for entry in memory_files.read_journal(player.id):
        m = _JOURNAL_STAMP.match(entry)
        if m and _stamp(m) > last:
            found = _FOUND.match(m.group(4).strip())
            if found and not any(same_item(found.group(1), item) for item in items):
                continue
            news.append((_stamp(m), f'"{_clip(m.group(4), 110)}"'))
    news.sort(key=lambda n: n[0], reverse=True)
    return [line for _, line in news]


def _report_back(session, player: Player, npc: NPC, rel: NPCRelationship | None, unmet: str | None) -> str:
    """The dossier line for the giver of an active job when the player
    comes back to them: the next step IS the report (a talk_to/deliver to
    them), or the player learned something since they last spoke. Names
    what the giver can add — a pressure, the next lead, an admission as far
    as their trust allows. "" otherwise; never raises."""
    from src.services import standing
    from src.services.job_offers import job_steps
    from src.services.objectives import active_quests_for, normalize_objectives

    try:
        quest = next((q for q in active_quests_for(session, player) if q.assigned_by_npc_id == npc.id), None)
        if quest is None:
            return ""
        nxt = next((o for o in normalize_objectives(quest.objectives) if not o["done"]), None)
        reporting = (nxt is not None and nxt.get("target_id") == npc.id
                     and nxt.get("kind") in ("talk_to", "deliver"))
        news = [] if reporting else _news_since(
            player, npc, [o.get("item_name") for o in normalize_objectives(quest.objectives)])
        if not (reporting or news):
            return ""
        if reporting:
            why = f"their next step is this report (\"{_clip(nxt.get('text'), 100)}\")"
            ahead = job_steps(session, quest, player, skip=[nxt["id"]])
        else:
            why = "they learned this since you last spoke — " + "; ".join(news[:MAX_NEWS])
            ahead = job_steps(session, quest, player)
        ahead = re.sub(r"^\d+\)\s*", "", ahead.split("; ")[0]) if ahead else ""
        raw = npc.secrets if isinstance(npc.secrets, list) else _as_list(npc.secrets)
        revealed = _indices(rel.revealed_secrets) if rel is not None else set()
        hidden = [s for i, s in enumerate(raw) if s and i not in revealed]
        if hidden and standing.secret_refusal(npc, rel) is None:
            admission = "an admission from their 'Would now confess' line"
        elif hidden:
            admission = "a tell that they are holding something back (never the secret itself)"
        else:
            admission = "a worry of their own they had not said"
        who = f"the {unmet}" if unmet else npc.name
        return (f"    Reporting back on \"{quest.title}\": {why}. When the player reports, {who} answers with "
                "something NEW, never a recap of what the player just told them: a pressure (what it costs "
                "them if this fails, or what changed since), the next lead"
                + (f" ({ahead})" if ahead else "") + f", or {admission}.")
    except Exception as e:      # the dossier never waits on this
        logger.debug(f"report-back line skipped for {npc.id}: {e}")
        return ""


# What the character wants, read from their own background: a temptation the
# scene can hold out. fleet9-tamsin, a card-sharp and pickpocket who wanted
# money above everything, was offered no mark, purse, fence or con in 21
# replies and ended on the 25 coin she began with — the brief was built
# around the quest and the lead alone. (kind, their words, the want, its shapes)
_PERSONAS = (
    ("thief", re.compile(r"\b(?:thie(?:f|ves|ving)|steal(?:s|ing)?|stole|stolen|pickpocket\w*|cutpurse\w*"
                         r"|card[- ]?sharp\w*|grift\w*|con[- ](?:man|woman|artist)|swindl\w*|smuggl\w*"
                         r"|burglar\w*|forger\w*|gambler\w*)\b", re.I),
     "money that is not nailed down",
     "a mark — a fat purse on a careless belt, a till left open, a gullible buyer, a card game with real stakes, "
     "a whisper of who fences goods"),
    ("fighter", re.compile(r"\b(?:soldier\w*|mercenar\w*|sell-?swords?|veteran|(?:border|free|mercenary)\s+company"
                           r"|brawl\w*|fighter|pit[- ]?fight\w*|warrior|disbanded|bruiser|fists?)\b", re.I),
     "paid work and a fight worth having",
     "someone hiring a strong arm, a bully worth facing down, a wager on a bout, an officer who has it coming"),
    ("collector", re.compile(r"\b(?:folk[- ]?tales?|collector|scholar|historian|scribe|chronicler|archivist"
                             r"|bookish|notebooks?|bard|ballads?|storyteller)\b", re.I),
     "a story nobody has written down",
     "an old-timer who remembers, a song with a missing verse, a written record (a ledger, a carved name), a "
     "rumour told two ways"),
    ("healer", re.compile(r"\b(?:healer|herbalist|apothecar\w*|physician|surgeon|midwife)\b", re.I),
     "someone to mend and a remedy worth finding",
     "someone hurt who hides it, a rare plant, a remedy sold as a fake"),
    ("trader", re.compile(r"\b(?:merchant|trader|peddler|pedlar|haggl\w*|shopkeep\w*)\b", re.I),
     "a bargain",
     "goods priced wrong, a buyer who needs what they carry, a debt to collect"),
    ("hunter", re.compile(r"\b(?:hunter|tracker|ranger|trapper|poacher)\b", re.I),
     "a trail worth following",
     "fresh tracks, a beast's sign, a bounty on a pelt"),
)
# Replayed over the 1507 secrets, 461 looks and 432 trades of every world on
# disk: "fence" only as a trade ("buys from the same fence", never "closer to
# the fence"), no smuggling of a child "to safety" or of food
_FENCE = re.compile(r"\b(?:(?:buys?|sells?|deals?|goods)\b[^.]{0,40}\bfence[sd]?|fence[sd]?\s+(?:for|goods|stolen))\b"
                    r"|\bstolen\b|\bblack\s+market\b|\boff\s+dead\b"
                    r"|\bsmuggl\w*\b(?![^.]{0,40}\b(?:to\s+safety|food)\b)", re.I)
# Old enough to remember: the age column, or looks that say so ("grey" alone
# was cloaks and eyes, "old" alone old scars)
OLD_AGE = 55
_OLD = re.compile(r"\b(?:old\s+(?:man|woman|veteran|soldier|knight|timer)|elderly|(?<!middle-)aged|ancient|white-haired"
                  r"|gr[ae]y-(?:haired|bearded)|gr[ae]y\s+(?:beard|stubble)|stooped|wrinkl\w*|bent\s+with\s+age)\b",
                  re.I)
_OFFICER = re.compile(r"\b(?:captain|sergeant|officer|lieutenant|commander)\b", re.I)
PERSONA_CHARS = 600          # a long written backstory's opening says who they are; its middle is history


def _persona_here(session, player: Player, kind: str, npcs: list[NPC], location: Location) -> list[str]:
    """What here could carry the want: a stall and its till, a secret in the
    thief's own line of work (never what it is), a written record, someone
    old enough to remember, an officer, someone hurt."""
    from src.services.dialogue import player_has_met, stranger_label

    def who(npc: NPC) -> str:
        label = stranger_label(npc, met=player_has_met(session, player.id, npc.id))
        return f"the {label}" if label else npc.name

    people = [n for n in npcs if n.status == "alive" and not _is_beast_row(n)]
    out: list[str] = []
    if kind in ("thief", "trader"):
        # A stall is more than one thing for sale (the Captain's one coat is no till)
        # — a key or clue someone carries is no ware (the seed's Captain holds
        # the Gate Pass beside his coat, and is still no stall)
        def wares(npc: NPC) -> int:
            return sum(1 for e in _as_list(npc.inventory_notable)
                       if not (isinstance(e, dict) and str(e.get("type") or "") == "quest_item"))

        stalls = sorted((n for n in people if wares(n) >= 2), key=lambda n: -wares(n))
        out += [f"{who(n)}'s wares and till" for n in stalls][:1]
        # ...and a Thing here that holds coin or goods. fleet10-tamsin: the
        # Hollow's crate held Greywater silver and went unmentioned for nine
        # replies of a thief's visit. Its label only — what it holds is
        # theirs to find through @interact
        try:
            from src.services.lead import _thing_phrase, holding_things

            # a container (searched or opened) — a thing lying out to be
            # taken is itself, and the proof among them has its own line
            out += [f"{_thing_phrase(t['label'])} (it holds something worth having — what, only searching it "
                    "shows)" for t in holding_things(location) if t["kind"] in ("search", "open")][:1]
        except Exception as e:
            logger.debug(f"persona things skipped: {e}")
    if kind == "thief":
        for npc in people:
            rel = _relationship(session, player.id, npc.id)
            revealed = _indices(rel.revealed_secrets) if rel is not None else set()
            raw = npc.secrets if isinstance(npc.secrets, list) else _as_list(npc.secrets)
            if any(s and i not in revealed and _FENCE.search(str(s)) for i, s in enumerate(raw)):
                out.append(f"{who(npc)} keeps a secret in this very line of work (a rumour may point at them; "
                           "what it is stays theirs until their dossier says they would confess)")
                break
    if kind == "collector":
        try:
            from src.services import scene_objects

            reads = [o for o in scene_objects.public_objects(location.objects)
                     if o["kind"] == "read" and not o["consumed"]]
        except Exception:
            reads = []
        out += [f"the {o['label']} (a written record)" for o in reads[:1]]
        out += [f"{who(n)}, old enough to remember" for n in people
                if (n.age or 0) >= OLD_AGE or _OLD.search(str(n.description_physical or ""))][:1]
    if kind == "fighter":
        out += [f"{who(n)}, an officer" for n in people if _OFFICER.search(str(n.profession or ""))][:1]
    if kind == "healer":
        out += [f"{who(n)} ({n.condition})" for n in people if n.condition][:1]
    return out[:3]


# The hook must reach play. fleet10-tamsin (a pickpocket who wants money
# above everything) was offered no mark, purse, fence or game in 21 replies:
# "never over a fight or a beat" held it back on every town reply, since each
# carried a beat or a job. Now it is due — on the third reply (the opening is
# the first), then every fifth — whatever beats are pending; an engine reply
# (a [COMBAT OUTCOME]) hands it to the next one, and so, once, does a reply a
# MUST-DELIVER beat owned.
PERSONA_FIRST = 3
PERSONA_EVERY = 5
_PERSONA_REPLIES: dict[str, int] = {}             # player -> briefs built (one a reply)
_PERSONA_DUE: dict[str, tuple[int, bool]] = {}    # player -> (reply it was due, carried over already)
_PERSONA_OWED: set[str] = set()                   # due on an engine reply: the next one


def _beat_owned_last_reply(player_id: str) -> bool:
    """Did a MUST-DELIVER beat own the last reply? (This reply's tick has
    not run yet: the last one is the reply before.)"""
    try:
        from src.services.campaign import get_last_tick

        fired = get_last_tick(player_id).get("fired_beat")
        return bool(fired) and not (isinstance(fired, dict) and fired.get("hook"))
    except Exception:
        return False


def persona_due(player_id: str, *, held: bool = False) -> bool:
    """Is the character's want due in view this reply (see above)? Counts
    the reply: call once per brief. `held`: this reply is the engine's (a
    fight's aftermath) — a due chance waits for the next."""
    n = _PERSONA_REPLIES[player_id] = _PERSONA_REPLIES.get(player_id, 0) + 1
    due_at, carried = _PERSONA_DUE.get(player_id, (0, True))
    if n >= PERSONA_FIRST and (n - PERSONA_FIRST) % PERSONA_EVERY == 0:
        carry = False
    elif player_id in _PERSONA_OWED:
        carry = False
    elif due_at == n - 1 and not carried and _beat_owned_last_reply(player_id):
        carry = True
    else:
        return False
    if held:
        _PERSONA_OWED.add(player_id)
        return False
    _PERSONA_OWED.discard(player_id)
    _PERSONA_DUE[player_id] = (n, carry)
    return True


def _persona_hook(session, player: Player, npcs: list[NPC], location: Location, *,
                  held: bool = False) -> list[str]:
    """One line: what this character would want, from their own background
    and looks, and what here could carry it — for the DM to put in view now
    and then, and THIS reply when it is due (`persona_due`; `held`: an
    engine reply). [] when their words name no want this knows; never raises."""
    try:
        own = " ".join(str(x or "")[:PERSONA_CHARS] for x in (player.background, player.description)
                       if str(x or "").strip().lower() != "a mysterious figure awaiting their story.")
        own += " " + " ".join(str(t) for t in _as_list(player.traits))
        # The want they are introduced by: the earliest one their words name
        hits = [(m.start(), kind, want, shapes) for kind, pattern, want, shapes in _PERSONAS
                for m in [pattern.search(own)] if m]
        if not hits:
            return []
        _, kind, want, shapes = min(hits, key=lambda h: h[0])
        here = _persona_here(session, player, kind, npcs, location)
        due = persona_due(player.id, held=held)
    except Exception as e:
        logger.debug(f"persona hook skipped: {e}")
        return []
    head = f"What this character wants (their own background — never said to them as a hook): {want}."
    if due:
        where = (f"here: {'; '.join(here)}" if here else
                 f"nothing here carries it, so through someone passing under a role name, never a new named "
                 f"person: {shapes}")
        return [f"{head} THIS REPLY puts one such chance in plain view for them to take or leave — {where}. "
                "It goes in whatever else the reply carries (a beat or a job is no reason to hold it); only a "
                "fight, an ambush or a MUST-DELIVER beat moves it to the next reply. Show it in the scene — "
                "never a menu, never said to be for them."]
    return [f"{head} Now and then — every few replies, never two running, never over a fight — put one such "
            f"chance in view for them to take or leave: {shapes}."
            + (f" Here: {'; '.join(here)}." if here else "")]


# What the player just asked a person about, against what that person hides.
# fleet10-wenna T7 asked the stable hand exactly about his secret ("you
# looked north… have you seen something up there?") at trust 50 of the 60
# a secret needs; the dossier only said "play them guarded", so he answered
# "I haven't seen anything" and the authored clue never came. The DM never
# sees a gated secret's words, so code says when the question touches it.
# Typed with no speech, the asking is said in so many words ("I ask her
# about the dead carters"); a "who" or "what" in narration asks nobody
_ASKING = re.compile(r"\?|\b(?:ask|asks|asked|asking|tell\s+me|wonder)\b", re.I)
# One spelling, one word: "grey" is "gray", "saw" is "seen". What the secret
# DID (saw, found, heard) is half its topic: "seen anything up north?" is
# the stable hand's "saw a man by the north wall"; "seen anything?" alone is not
_SAME_WORD = {"grey": "gray", "saw": "seen", "see": "seen", "sees": "seen", "seeing": "seen",
              "spotted": "seen", "find": "found", "finds": "found", "finding": "found", "hear": "heard",
              "hears": "heard", "hearing": "heard"}
# A question shares at least this many topic words with the secret. One
# alone was any talk of the night or the howls (replayed over the 561 typed
# turns of every fleet transcript, against the seed's secrets, for the person
# who answered: 211 hits, most of them the night). Two, from what the player
# SAYS with a "?": 33 hits, 29 of them asking about that very secret's subject
# (fleet10-wenna T7 among them); the misses are a barn "north of the track",
# a cloak fetched from a satchel, a question to someone else
MIN_ASKED_WORDS = 2
# Words a secret is made of that name no topic: verbs of knowing and telling,
# people in general, time words, and what every line here says
_NO_TOPIC = frozenset({
    "know", "knows", "knew", "told", "tell", "tells", "said", "says", "night", "nights", "tonight",
    "asks", "asked", "been", "have", "that", "this", "with", "from", "they", "them", "their",
    "there", "when", "where", "what", "which", "would", "could", "about", "into", "only", "never",
    "ever", "than", "then", "your", "will", "just", "because", "before", "after", "nobody", "anyone",
    "someone", "something", "anything", "everyone", "people", "once", "last", "same", "every", "other",
    "ago", "days", "weeks", "years", "since", "still", "here", "much", "more", "most", "some", "near",
    "come", "comes", "came", "going", "goes", "back",
})


def _topic_words(text: str, *, small_only: bool = False) -> set[str]:
    """A text's topic words: four letters or more, one spelling each,
    a plural or tense ending off. `small_only`: words written in lowercase
    only — in a secret a capitalised word is a name ("The Watch roster",
    "the Ashen Fangs"), and every question about the town says those."""
    out: set[str] = set()
    for raw in re.findall(r"[A-Za-z]+", str(text or "")):
        if small_only and raw[:1].isupper():
            continue
        word = _SAME_WORD.get(raw.lower(), raw.lower())
        if len(word) < 4 or word in _NO_TOPIC:
            continue
        if len(word) > 5:
            word = re.sub(r"(?:ing|ed|es|s)$", "", word)
        out.add(word[:6])
    return out


_SAID = re.compile(r"[\"“]([^\"“”]{2,})[\"”]")


def asked_about(player_input: str, secret: str, npc_name: str = "") -> str:
    """The topic words a question in `player_input` shares with `secret`
    (", "-joined), or "" when the player asked nothing about it. Typed with
    speech, the question is what they SAY, and it asks ("?"): the words of
    "you find me or you find the Captain" in a farewell asked nothing. The
    person's own name is no topic (the player says it to ask them anything)."""
    if not player_input or not secret:
        return ""
    said = " ".join(_SAID.findall(player_input))
    if said:
        if "?" not in said:
            return ""
        asked = said
    elif _ASKING.search(player_input):
        asked = player_input
    else:
        return ""
    shared = (_topic_words(secret, small_only=True) - _topic_words(npc_name)) & _topic_words(asked)
    return ", ".join(sorted(shared)) if len(shared) >= MIN_ASKED_WORDS else ""


def _within_reach(session, player: Player, npc: NPC, rel: NPCRelationship | None, hidden: list[tuple[int, str]],
                  *, player_input: str, addressed: bool) -> list[str]:
    """The dossier line for a trust-gated secret the player just asked this
    person about (`asked_about`): what opens it — one more kindness (a gift,
    `commands._gift_trust`) or a won roll — and how to hold it meanwhile.
    [] when they asked nothing of it, never spoke with them (no trust to
    build on), or a story truth keeps it sealed whatever the trust. Never raises."""
    try:
        from src.services import standing

        if rel is None or not (player_input or "").strip() or player_input.lstrip().startswith("["):
            return []
        if not addressed:
            from src.services.commands import _named_in

            if not _named_in(npc, " ".join(_SAID.findall(player_input)) or player_input):
                return []
        asked = next((a for _, s in hidden if (a := asked_about(player_input, str(s), npc.name or ""))), "")
        gap = standing.SECRET_TRUST - standing.trust_of(rel)
        if not asked or gap <= 0:
            return []
        from src.services.campaign import sealed_holder_line

        if sealed_holder_line(session, player.id, npc):
            return []                  # the story's own truth: its scene opens it, never trust
        clock = session.query(WorldClock).first()
        gifted = rel.last_gift_day is not None and rel.last_gift_day == (clock.day if clock else 1)
        kindness = ("" if gifted else
                    "one more kindness opens it" if gap <= standing.GIFT_TRUST else "a kindness brings it closer")
        roll = f"a won roll when the player presses (persuasion; on a success write @trust {npc.name} +{gap})"
        return [f"    The player just asked about what they hide ({asked}). It is within reach: "
                + (f"{kindness} (a gift the player gives in their own words — the engine adds the trust, once a "
                   f"day), or " if kindness else "")
                + f"{roll}; they tell it in the reply after. Until then they deflect with a tell — a glance, a "
                "pause, a half-answer — never a flat denial that there is anything to tell."]
    except Exception as e:
        logger.debug(f"within-reach line skipped for {npc.id}: {e}")
        return []


def _dossier(session, player: Player, npc: NPC, *, addressed: bool, in_party: bool,
             player_input: str = "", alone: bool = False) -> list[str]:
    from src.services import standing
    from src.services.dialogue import _names_on_file, knows_player_name, player_has_met, stranger_label

    rel = _relationship(session, player.id, npc.id)
    band = standing.disposition_band(rel)
    tier = npc.danger_tier or "unrated"
    # The row is the relationship; whether the player knows their NAME is
    # its own fact (dialogue: who knows whom)
    unmet = stranger_label(npc, met=player_has_met(session, player.id, npc.id))
    if unmet:
        head = (f"- {unmet} — real name {npc.name} [{npc.id}], NOT known to the player — "
                f"{npc.profession or 'no trade given'}")
    else:
        head = f"- {npc.name} [{npc.id}] — {npc.profession or 'no trade given'}"
    # "young man, he": fleet8 (all runs) the stable hand flipped she/he
    look = _look(npc)
    if look:
        head += f" — {look}"
    tags = [tier]
    if in_party:
        tags.append("IN YOUR PARTY")
    if npc.condition:
        tags.append(npc.condition)
    head += f" ({', '.join(tags)}). Mood: {npc.current_mood or 'neutral'}."
    lines = [head]
    if unmet:
        lines.append(
            f"    The player has not learned this person's name. Call them '{unmet}' in prose and as the "
            "speaker label until they introduce themselves (they decide if/when — the first line "
            "labelled with their name must SAY it, or the engine keeps showing the role label). "
            "@commands still take the real name."
        )
    # The story's villain before their reveal: menace, never the why
    # (fleet4-wenna T11 improvised a grievance the sealed truth contradicts)
    held = None
    try:
        from src.services.campaign import CAST_OFFSTAGE_LINE, VILLAIN_OFFSTAGE_LINE, villain_held_line

        held = villain_held_line(session, player.id, npc)
        if held:
            # Offstage they get no speaker label to keep (fleet8-tamsin T17:
            # the "Wolf-Mantled Man" stepped out and made his demand; round
            # 9, the villain's people held for their entrance alike)
            if held in (VILLAIN_OFFSTAGE_LINE, CAST_OFFSTAGE_LINE):
                lines = lines[:1]
                # ...but a glimpse in prose still goes by what the player
                # can see, never the name the NameGate would cut
                if unmet:
                    lines.append(f"    If glimpsed at all, in prose they are only '{unmet}' — never their name.")
            lines.append(held)
    except Exception as e:
        logger.debug(f"villain line skipped for {npc.id}: {e}")

    # Looks: what a stranger is known by, and what keeps a recurring
    # character from drifting (a playtest DM wrote the ash-cloaked man as
    # "a figure in dark leather")
    looks = _clip(npc.description_physical, 300 if addressed else 160)
    if looks:
        lines.append(f"    Looks: {looks}")
    personality = _clip(npc.description_personality, 600 if addressed else 220)
    if personality:
        lines.append(f"    Who: {personality}")
    if npc.voice_pattern:
        lines.append(f"    Voice: {_clip(npc.voice_pattern, 200 if addressed else 120)}")
    goals = _as_list(npc.goals)
    if goals:
        lines.append("    Wants: " + "; ".join(_clip(g, 140) for g in goals[: 4 if addressed else 2]))

    knows = _recognition(session, player, npc)
    # fleet6-bram T19 / fleet6-tamsin T15: Piper said "Bram" and "Tamsin",
    # which neither player ever gave, because a row printed "yes" here
    # No row yet: what is on file (told before the split, or overheard) — the
    # same the row will start from, and the audit already accepts
    knows_you = knows_player_name(rel) or (rel is None and _names_on_file(session, player.id, npc.id)[1])
    memory, met = _memory_lines(player, npc, addressed=addressed, unnamed=not knows_you)
    # No row yet: every gate (join, secrets) reads that as no trust at all,
    # so the brief must not print the 50 a first conversation will create
    standing_words = (f"trust {standing.trust_of(rel)}/100, {band}" if rel is not None
                      else "a stranger, no trust built yet")
    lines.append(
        f"    With you: {standing_words}"
        + ("" if met else ", never spoken with you")
        + (f"; {knows}" if knows else "")
    )
    lines.append(f"    Knows the player's name: {'yes' if knows_you else 'no'}"
                 + ("" if knows_you else " — until the player says it, they call them by what they see"))
    lines.extend(memory)
    if not in_party:
        join = _join_line(session, player, npc)
        if join:
            lines.append(join)

    offered = _quests_offered(session, npc.id, player.id, player=player)
    if offered:
        lines.append("    Work they can offer (authored — they name the pay when they offer it, and what they ask "
                     "for is its steps as written, never another room, key or rule; @quest-start it when the "
                     "player accepts, never invent a copy): " + " ; ".join(offered))
    if not held:
        report = _report_back(session, player, npc, rel, unmet)
        if report:
            lines.append(report)

    # Indices are reveal_secret's: positions in the RAW list, so an empty
    # entry is skipped without shifting the ones after it
    raw_secrets = npc.secrets if isinstance(npc.secrets, list) else _as_list(npc.secrets)
    secrets = [(i, s) for i, s in enumerate(raw_secrets) if s]
    if secrets:
        revealed = _indices(rel.revealed_secrets) if rel is not None else set()
        hidden = [(i, s) for i, s in secrets if i not in revealed]
        told = [s for i, s in secrets if i in revealed]
        if told:
            lines.append("    Already told you: " + "; ".join(_clip(s, 160) for s in told))
        if hidden:
            # A held villain confesses nothing, whatever the trust: his
            # secrets are the story's reveal, not a parley's
            if standing.secret_refusal(npc, rel) is None and not held:
                lines.append(
                    f"    Would now confess if pressed (write @reveal {npc.name} <index> FIRST, "
                    "then let them say it): "
                    + "; ".join(f"[{i}] {_clip(s, 200)}" for i, s in hidden)
                )
            else:
                lines.append(
                    f"    Hides {len(hidden)} secret(s) they will not share with you yet — play them "
                    "guarded about it; you do not get to know what it is."
                )
                if not held:
                    lines.extend(_within_reach(session, player, npc, rel, hidden, player_input=player_input,
                                               addressed=addressed or alone))
    # A story truth about them stays theirs until its scene, whatever the
    # dice say (fleet9-bram T1: a critical success spent the act-2 roster)
    if not held:
        try:
            from src.services.campaign import sealed_holder_line

            sealed_line = sealed_holder_line(session, player.id, npc)
            if sealed_line:
                lines.append(sealed_line)
        except Exception as e:
            logger.debug(f"sealed holder line skipped for {npc.id}: {e}")
        lines.extend(_knows_the_lost(session, npc))

    refusal = standing.conversation_refusal(session, player, npc, rel)
    if _as_list(npc.inventory_notable) and not refusal:
        stock = _stock_line(session, player, npc)
        if stock:
            lines.append(stock)
    if refusal:
        lines.append("    WILL NOT TALK TO YOU (hostile, no trust left) — nor trade. "
                     "Give them no lines beyond a cold refusal.")
    return lines


# What the player's hurt looks and feels like, for the prose. fleet8-bram was
# "winded" from T4 to T20 and it showed once, as flavour; roll_check now makes
# a hurt climb or sneak harder (src/tools/world_write/progression.py), and
# the DM is told so in words, never numbers
_HURT = {
    "winded": "short of breath, ribs sore from the last blow",
    "hurt": "a real wound that pulls when they move",
    "badly_hurt": "badly hurt, bleeding through a binding, every move costs them",
    "critical": "barely on their feet",
}


def _wound_line(player: Player) -> list[str]:
    """One line when the player is hurt (winded or worse), else none."""
    health = str(player.health_status or "healthy")
    if health not in _HURT:
        return []
    return [f"The player is {health.replace('_', ' ')} — {_HURT[health]}. Let it show in a beat whenever they "
            "move, fight or strain, and their body knows it: climbing, running and sneaking go harder until "
            "they rest, drink a draught or get it dressed."]


# fleet8-tamsin: the howl pattern was told five times before the player left
# town — Piper (T3, T4), Sorrel (T6), Piper again (T9, T10). Every channel fed
# it fresh (Piper's beat and job, the step "ask what the stable yard hears",
# the summons), and nothing told the DM voicing Sorrel what Piper had just
# said: each memory file holds only its own NPC's lines (the people here have
# theirs in their dossiers). What the others said last, and the newest clues
MAX_TOLD_LINES = 3
MAX_TOLD_JOURNAL = 2
MIN_TOLD_WORDS = 5          # "Twenty minutes, maybe less." is no fact to keep from repeating
_EXCHANGE = re.compile(r'^-\s*Day\s+(\d+)\s+(\d{1,2}):(\d{2})\s*[·—–-]\s*([^:]{1,60}):\s*"?(.*?)"?\s*$')
_JOURNAL_STAMP = re.compile(r"^\s*-?\s*\(Day\s+(\d+)\s+(\d{1,2}):(\d{2})\)\s*(.*)$")


def _already_told(session, player: Player, npcs: list[NPC]) -> list[str]:
    """One line: the newest things people who are not here told the player,
    and the journal's newest entries. Never raises."""
    from src.services import memory_files

    try:
        ids = {n.id for n in npcs}
        you = memory_files.speaker_label(player.name or "", first_word=True)
        heard: list[tuple[tuple[int, int, int], str, str]] = []
        for npc_id, text in memory_files.npc_memory_texts(player.id).items():
            if npc_id in ids:
                continue
            for line in text.splitlines():
                m = _EXCHANGE.match(line.strip())
                if (m and m.group(4).strip() != you
                        and len(memory_files._journal_words(m.group(5))) >= MIN_TOLD_WORDS):
                    heard.append(((int(m.group(1)), int(m.group(2)), int(m.group(3))),
                                  m.group(4).strip(), m.group(5)))
        heard.sort(key=lambda h: h[0], reverse=True)
        notes = []
        for entry in reversed(memory_files.read_journal(player.id)[-MAX_TOLD_JOURNAL:]):
            m = _JOURNAL_STAMP.match(entry)
            notes.append(m.group(4) if m else entry)
    except Exception as e:
        logger.debug(f"already-told line skipped: {e}")
        return []
    if not heard and not notes:
        return []
    shown = [f'{who} (day {d}, {h:02d}:{mi:02d}): "{_clip(words, 220)}"'
             for (d, h, mi), who, words in heard[:MAX_TOLD_LINES]]
    shown += [f'their journal: "{_clip(n, 160)}"' for n in notes]
    return ["Already told to the player (they know it — nobody here tells it again as news; whoever raises "
            "it adds ONE new detail, or points them onward): " + "; ".join(shown)]


def _tics_line(player: Player, npcs: list[NPC]) -> list[str]:
    """The details of the people here that recent replies kept coming back
    to ("Already used — vary"), else none. fleet9-tamsin: Piper's basket in
    16 of 21 replies, "basket forgotten on/against her hip" twice in four
    turns — the dossier's looks are a palette, not a checklist
    (src/services/voice_tics.py). Never raises."""
    try:
        from src.services import voice_tics

        return voice_tics.brief_lines(player.id, npcs)
    except Exception as e:
        logger.debug(f"tic line skipped: {e}")
        return []


def _look(npc: NPC) -> str:
    """'young man, he' (`dialogue.look_tag`); '' on any trouble."""
    try:
        from src.services.dialogue import look_tag

        return look_tag(npc)
    except Exception:
        return ""


# The small words a trade is written with ("hawker of blades, tools and
# remedies", "smith with a limp"): none of them is the trade
_NOT_A_TRADE = frozenset({"with", "from", "that", "this", "their", "into", "onto", "over", "under", "upon",
                          "once", "also", "some", "more", "most", "much", "very", "been", "were", "have",
                          "what", "when", "where", "which", "while", "former", "retired", "sometime", "part",
                          "time", "town", "local", "north", "south", "east", "west"})


def _not_them(session, npc: NPC) -> str:
    """' — not the people of <event> …' when a public event still to come is
    about the lost person's own trade (a carter dead three weeks, a supply cart
    due tomorrow): the story's dead are never its living."""
    from src.models import Event

    stems = set()
    for word in re.findall(r"[a-z]{4,}", str(npc.profession or "").lower()):
        if word in _NOT_A_TRADE:
            continue
        stems.add(word)
        if word.endswith("er") and len(word) > 5:
            stems.add(word[:-2])                         # carter -> cart
    if not stems:
        return ""
    # Whole words: a dead stable hand is no "handful of riders", nor a carter
    # the "cartographer"
    pattern = re.compile(rf"\b(?:{'|'.join(sorted(stems))})(?:s|es)?(?:['’]s)?\b", re.I)
    try:
        events = session.query(Event).filter(Event.occurred_day.is_(None), Event.scheduled_day.isnot(None),
                                             Event.player_visible == True).all()  # noqa: E712
    except Exception:
        return ""
    for event in sorted(events, key=lambda e: (e.scheduled_day or 0, e.scheduled_hour or 0)):
        if pattern.search(f"{event.name} {event.description or ''}"):
            return (f" — never the people of \"{event.name}\" (day {event.scheduled_day}, "
                    f"{(event.scheduled_hour or 0):02d}:00, not happened yet): they are others, alive")
    return ""


def _quest_people(session, player: Player, present: set[str], listed: set[str]) -> list[str]:
    """The people the player's quests and the offers here name — giver,
    step targets, anyone named in a step — who have no line above, each with
    their look and pronoun. fleet8-wenna T3: Piper said "ask the stable hand
    what she hears"; the step named Sorrel, whose dossier (a young man) was
    in no brief until the player walked into the stable yard."""
    from src.services.dialogue import is_beast, player_has_met, stranger_label
    from src.services.objectives import objectives_public

    try:
        quests = session.query(Quest).filter(Quest.status == QuestStatus.ACTIVE).all()
        if present:          # what the people here can offer (their dossiers' work lines)
            try:             # ...never a job the story keeps for a later act (`_quests_offered`)
                from src.services.campaign import later_act_quest_ids

                kept = later_act_quest_ids(session, player.id)
            except Exception:
                kept = set()
            quests += [q for q in session.query(Quest).filter(
                Quest.status == QuestStatus.NOT_STARTED, Quest.assigned_by_npc_id.in_(list(present))).all()
                if q.id not in kept]
        people = {n.id: n for n in session.query(NPC).filter(NPC.status == "alive").all() if not is_beast(n)}
        named: list[str] = []
        for quest in quests:
            ids = [quest.assigned_by_npc_id]
            text = f"{quest.title} {quest.description or ''}"
            for step in objectives_public(quest):
                if isinstance(step, dict):
                    ids.append(step.get("target_id"))
                    text += f" {step.get('text') or ''}"
            ids += [n.id for n in people.values() if n.name and n.name in text]
            for i in ids:
                if i and i in people and i not in listed and i not in named:
                    named.append(i)
        lines = []
        for npc_id in named:
            npc = people[npc_id]
            look = _look(npc)
            if not look:
                continue
            label = stranger_label(npc, met=player_has_met(session, player.id, npc.id))
            shown = f", the {label} to the player" if label else ""
            where = npc.current_location.name if npc.current_location is not None else "unknown"
            lines.append(f"{npc.name} [{npc.id}] — {look}{shown}, @ {where}")
    except Exception as e:
        logger.debug(f"quest people line skipped: {e}")
        return []
    if not lines:
        return []
    return ["People your quests and offers name (keep this look and pronoun whoever speaks of them; never "
            "guess a sex): " + "; ".join(lines[:MAX_KNOWN_PEOPLE])]


def _lost_people(session, player: Player) -> list[str]:
    """The dead and the missing the world already has, with their trade, so a
    story about "the dead carter" uses his name. fleet7-wenna T1-T2: the
    Captain named him "Fenwick" and the DM made a second dead carter beside
    the world's own (round 5: "Hollis"). Beasts are no one's loss here.
    Never raises: the scene matters more than this line."""
    from src.services.dialogue import is_beast, player_has_met

    lost = []
    try:
        for npc in session.query(NPC).filter(NPC.status.in_(("dead", "missing"))).order_by(NPC.name).all():
            if is_beast(npc):
                continue
            heard = "" if player_has_met(session, player.id, npc.id) else ", name not yet heard by the player"
            # fleet8-tamsin T0-T2: "Took a cart three nights back, driver
            # alive", then "Driver's name is Tobin. Was. Found him three weeks
            # back" — the dead carter and the cart still due blurred. Who he
            # was and when, and who the scheduled cart's people are not
            look = _look(npc)
            when = _clip(npc.description_physical, 100)
            lost.append(f"{npc.name} [{npc.id}] ({npc.profession or 'no trade given'}, "
                        f"{look + ', ' if look else ''}{npc.status}{heard})"
                        + (f" — {when}" if when else "") + _not_them(session, npc))
    except Exception as e:
        logger.warning(f"lost people line skipped: {e}")
        return []
    if not lost:
        return []
    return ["People the story has lost (when the story speaks of one, it is them — by this name; never make "
            "another): " + "; ".join(lost[:MAX_LOST_PEOPLE])]


# fleet9-tamsin T2 (and fleet8-tamsin T0-T2): the Captain told the arc's dead
# carter as a survivor — "Driver survived — cart didn't… hasn't said three
# words straight since" — with Tobin under "People the story has lost" a
# screen away. A separate list is someone else's fact; what a person in the
# room knows is in their own dossier. Who would know: anyone of the same
# town as the lost one (the top of the place tree), or anyone at all when
# the lost one has no place (Tobin, buried). Beasts know nothing.
MAX_KNOWN_LOST = 3


def _settlement(session, location_id: str | None) -> str | None:
    """The top of the place tree above `location_id` (the town), or None."""
    seen: set[str] = set()
    while location_id and location_id not in seen:
        seen.add(location_id)
        place = session.get(Location, location_id)
        if place is None or not place.parent_id:
            return location_id
        location_id = place.parent_id
    return location_id


def _knows_the_lost(session, npc: NPC) -> list[str]:
    """One "Knows:" dossier line: the lost people this person would know
    of, each dead (or missing) as the world has them. Never raises."""
    from src.services.dialogue import is_beast

    try:
        if is_beast(npc):
            return []
        home = _settlement(session, npc.current_location_id)
        known = []
        for person in session.query(NPC).filter(NPC.status.in_(("dead", "missing"))).order_by(NPC.name).all():
            if person.id == npc.id or is_beast(person):
                continue
            if person.current_location_id and _settlement(session, person.current_location_id) != home:
                continue
            when = _clip(person.description_physical, 100)
            known.append(f"{person.name} ({person.profession or 'no trade given'}) is {person.status}"
                         + (f" — {when}" if when else ""))
    except Exception as e:
        logger.debug(f"lost people unknown to {npc.id}: {e}")
        return []
    if not known:
        return []
    return ["    Knows: " + "; ".join(known[:MAX_KNOWN_LOST])
            + ". Spoken of, they are as written — the dead are dead: never a survivor, never someone who came back."]


def _names_in_use(session, player: Player) -> list[str]:
    """fleet9-wenna T12: "Old Marrow worked nights before me" gave the
    successor villain's surname to a stable hand the DM invented. The
    story's people and the world's dead keep their names (`campaign.
    names_in_use`). Never raises."""
    try:
        from src.services.campaign import names_in_use

        names = names_in_use(session, player.id)
    except Exception as e:
        logger.debug(f"names in use line skipped: {e}")
        return []
    if not names:
        return []
    return ["Names in use (each is ONE person of this story — never give any of these names, or a word of one, to "
            "anyone you invent: a new person gets a new name): " + "; ".join(names)]


def build_scene_brief(session, player: Player, addressed_npc_id: str | None = None,
                      player_input: str = "", engine_reply: bool = False) -> dict[str, Any]:
    """The brief text plus what the server needs to resolve speakers.

    Returns {"text", "present": {npc_id: name}, "strangers": {npc_id:
    role label} (the present people the player has not met — what their
    speech should be shown as), "refusal": {...} | None}. `player_input` is
    what they typed this turn (a question touching a gated secret,
    `_within_reach`); "" reads as a turn with nothing asked. `engine_reply`:
    the server bound this reply to an engine directive (an ambush's one
    sentence) — a due persona chance waits for the next reply.
    """
    location = session.get(Location, player.current_location_id) if player.current_location_id else None
    clock = session.query(WorldClock).first()
    dm_state = session.query(DMState).first()
    from src.services import party as party_mod
    from src.services.dialogue import player_has_met, stranger_label

    party_ids = set(party_mod.member_ids(player))
    out: list[str] = ["### THE SCENE (assembled by the engine — current, and the only facts you need)"]
    present: dict[str, str] = {}
    strangers: dict[str, str] = {}
    refusal = None

    if location is None:
        out.append("The player is nowhere yet.")
        return {"text": "\n".join(out), "present": present, "strangers": strangers, "refusal": None}

    when = (f"Day {clock.day}, {clock.hour:02d}:{(clock.minute or 0):02d} ({clock.get_time_of_day()})"
            if clock else "unknown time")
    weather = (dm_state.weather if dm_state and dm_state.weather else "clear")
    out.append(f"Where: {location.name} [{location.id}] ({location.type.value}) — {when}, weather {weather}.")
    if location.description:
        out.append(f"  {_clip(location.description, DESCRIPTION_CHARS)}")
    if location.current_state:
        out.append(f"  Right now: {_clip(location.current_state, 200)}")
    if location.controlling_faction is not None:
        out.append(f"  Held by: {location.controlling_faction.name}")
    out.extend(_wound_line(player))
    out.extend(_coming_up(session, clock))

    exits = _exits(session, player, location)
    out.append("Ways out:" if exits else "Ways out: none known.")
    out.extend(exits)
    try:
        out.extend(_night_ways(session, player, location))
    except Exception as e:      # the night's ways are a nudge, never a cost to the brief
        logger.warning(f"night ways line skipped: {e}")
    out.extend(_things_here(location))
    out.extend(_proof_lines(session, player, location))
    out.extend(_unfound_proof(session, player, location))

    npcs = session.query(NPC).filter(
        NPC.current_location_id == location.id, NPC.status == "alive"
    ).all()
    if npcs:
        out.append("People here (you voice them; their dossiers):")
        # The one being addressed first, then party, then the rest
        npcs.sort(key=lambda n: (n.id != addressed_npc_id, n.id not in party_ids, n.name))
        # Alone with one person, whatever the player says is said to them
        alone = sum(1 for n in npcs if not _is_beast_row(n)) == 1
        for npc in npcs:
            present[npc.id] = npc.name
            try:
                label = stranger_label(npc, met=player_has_met(session, player.id, npc.id))
                if label:
                    strangers[npc.id] = label
            except Exception as e:
                logger.debug(f"role label failed for {npc.id}: {e}")
            try:
                out.extend(_dossier(session, player, npc, addressed=npc.id == addressed_npc_id,
                                    in_party=npc.id in party_ids, player_input=player_input, alone=alone))
            except Exception as e:
                # One malformed row must not cost the DM the whole scene —
                # nor a stranger their anonymity
                logger.warning(f"dossier failed for {npc.id}: {e}", exc_info=True)
                who = (f"{strangers[npc.id]} — real name {npc.name} [{npc.id}], NOT known to the player "
                       f"(call them '{strangers[npc.id]}')" if npc.id in strangers
                       else f"{npc.name} [{npc.id}]")
                out.append(f"- {who} — {npc.profession or 'no trade given'} "
                           "(details unavailable this turn; play them guarded).")
    else:
        out.append("People here: no one named. Ambient folk may speak under a role name.")
    out.extend(_already_told(session, player, npcs))
    out.extend(_tics_line(player, npcs))
    # The mystery figure the story already wrote, before the DM makes one up
    # (fleet10-wenna T7: an invented drover took the ash-gray cloak's place;
    # src/services/suspects.py)
    from src.services import suspects

    out.extend(suspects.brief_line(session, player, npcs))
    out.extend(_persona_hook(session, player, npcs, location,
                             held=engine_reply or (player_input or "").lstrip().startswith("[")))

    if addressed_npc_id:
        target = session.get(NPC, addressed_npc_id)
        if target is not None:
            from src.services import standing

            ref = standing.conversation_refusal(session, player, target, _relationship(session, player.id, target.id))
            if ref:
                refusal = {"npc_id": target.id, "npc_name": target.name, **ref}
            here = target.current_location_id == location.id
            unmet = strangers.get(target.id) or stranger_label(
                target, met=player_has_met(session, player.id, target.id))
            who = (f"the {unmet} ({target.name} [{target.id}] — a name the player has not learned)"
                   if unmet else f"{target.name} [{target.id}]")
            out.append(
                f"THE PLAYER IS TALKING TO: {who}"
                + ("" if here else " — who is NOT here (they called out, or misremember where they are)")
                + ("" if not refusal else " — who REFUSES them (the engine already spoke their one cold line)")
            )
            if not here:
                # No dossier above for someone elsewhere: their memory still counts
                from src.services.dialogue import _names_on_file, knows_player_name

                rel = _relationship(session, player.id, target.id)
                knows_you = knows_player_name(rel) or (rel is None and _names_on_file(session, player.id, target.id)[1])
                out.extend(_memory_lines(player, target, addressed=True, unnamed=not knows_you)[0])

    out.extend(_price_list(location))

    # People the player has met who are elsewhere
    met_rows = session.query(NPCRelationship).filter(NPCRelationship.player_id == player.id).all()
    elsewhere = []
    listed: set[str] = set(present)
    for rel in met_rows:
        npc = session.get(NPC, rel.npc_id)
        if npc is None or npc.id in present:
            continue
        where = npc.current_location.name if npc.current_location is not None else "unknown"
        state = "" if npc.status == "alive" else f", {npc.status}"
        # Someone they have spoken with but whose name they never heard
        # stays a role here too (the row is no introduction)
        label = stranger_label(npc, met=player_has_met(session, player.id, npc.id))
        who = f"the {label} ({npc.name} — name not known to the player)" if label else npc.name
        look = _look(npc)
        elsewhere.append(f"{who} [{npc.id}]{f' ({look})' if look else ''} @ {where}{state}")
        if len(elsewhere) <= MAX_KNOWN_PEOPLE:
            listed.add(npc.id)
    if elsewhere:
        out.append("People you know elsewhere: " + "; ".join(elsewhere[:MAX_KNOWN_PEOPLE]))
    out.extend(_quest_people(session, player, set(present), listed))
    out.extend(_lost_people(session, player))
    out.extend(_names_in_use(session, player))

    # The map the player knows — reuse these before inventing a place
    places = session.query(Location).filter(
        (Location.visited == True) | (Location.discovered == True)  # noqa: E712
    ).all()
    if places:
        names = [f"{p.name} [{p.id}]" for p in places if p.id != location.id][:MAX_KNOWN_PLACES]
        if names:
            out.append("Known places (use these — never create a second copy of one): " + "; ".join(names))

    return {"text": "\n".join(out), "present": present, "strangers": strangers, "refusal": refusal}

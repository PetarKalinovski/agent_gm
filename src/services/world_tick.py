"""World Tick - Pre-turn world simulation.

Runs before each DM response to check what happened in the world since
the last player action. Surfaces scheduled events, faction movements,
and NPC goal progress so the DM can weave them into the narrative.
"""

import logging
import math
import re
from typing import Any

from src.models import (
    DMState,
    Event,
    Faction,
    Location,
    NPC,
    NPCTier,
    Player,
    WorldClock,
    get_session,
)
# The same word model campaign beats are matched with (stems, possessive
# heads, the generic-word list), so "howling" meets "howl" in both places
from src.services.campaign import (
    _GENERIC_WORDS,
    _distinctive,
    _text_forms,
    _word_forms,
    _words,
    mentioned_keywords,
)

logger = logging.getLogger(__name__)

# How many turns a fired event is shown before it is recorded as having
# happened offscreen
MAX_DELIVERY_ATTEMPTS = 3

# Every turn costs at least this much game time (enforce_minimum_time_cost);
# @move spends only the part of a road's time this floor would not cover
MIN_TURN_HOURS = 0.25

# Scheduled events a reply spoke of BEFORE they fired (fleet4-wenna T1/T4:
# Brann sold "the gate's barred after the howl" twice) — the player was told
# to expect them, so they must land. In memory: a restart only costs the
# MUST-DELIVER push for an event happening elsewhere.
_foreshadowed: set[str] = set()
# The events this turn's context ordered into the reply's prose because they
# happen where the player stands. The post-turn marker reads only the prose
# for them: a line about the howl in someone's mouth ("The gate closes at the
# howl", fleet4-wenna T8) is not the howl rising around the player.
_must_deliver: set[str] = set()
# ...those of them still HAPPENING (due under two hours ago): told only as
# now, never denied or long over (`told_at_hand`) — the rest are told as
# what they left behind, in the past tense
_happening_now: set[str] = set()
# Fired events someone has spoken of since — a reply in passing, or the
# player's own words ("The howl echoes in my ears", fleet9-wenna T11). One
# that runs out of attempts is never chronicled as unseen (`_offscreen_line`)
_referred: set[str] = set()
# A fired event that clears a place ("Townsfolk clear the square", "the
# square empties fast") sends that place's everyday people in for the night
# (`clear_places`). fleet9-bram T10: the howl cleared the stalls — "shutters
# down, the Captain's board standing alone" — and the database kept the
# hawker, the herbalist's girl and a drover there; the canvas would have
# shown five people in the square the story called empty. In memory, like
# `_foreshadowed`: the events already applied, and who went in where (back
# at first light). A restart forgets: they stay in, and the DM can move them.
CLEAR_FRESH_HOURS = 12      # an event older than this clears nothing (a restart replays no old night)
FIRST_LIGHT_HOUR = 6
_cleared: set[str] = set()
_gone_in: dict[str, tuple[str, str, int]] = {}     # npc id -> (home id, where they went, first-light day)


def run_world_tick(player_id: str | None = None, *, constrained: bool = False,
                   aftermath: bool = False) -> dict[str, Any]:
    """Run the world tick and return everything the DM needs to know.

    Checks:
    1. Scheduled events whose time has arrived
    2. Faction goals that should produce visible effects
    3. Major NPC goals that should advance
    4. The DM's own narrative state

    Args:
        player_id: Whose turn it is — where they stand decides which fired
            events happen AROUND them (MUST-DELIVER), and an event that runs
            out of attempts is written into their chronicle.
        constrained: The engine owns this reply ([OPENING], [COMBAT
            OUTCOME], an engine ambush's one sentence). Events are still
            shown, but no delivery attempt is spent on it: fleet4-wenna's
            Evening Howl used up its three on a parley, an ambush and a
            combat aftermath, and was never told.
        aftermath: This constrained reply is a [COMBAT OUTCOME]: an event
            happening where the player stands is still THIS reply's scene
            (fleet10-bram T17-T18: the howl Piper's job was built around
            fired at the player's feet around a fight's aftermath, and went
            by in half a sentence). Still no attempt is spent on it.

    Returns a context dict the DM can use to inform its response.
    """
    with get_session() as session:
        clock = session.query(WorldClock).first()
        if not clock:
            return {"tick": "no_clock"}

        dm_state = session.query(DMState).first()
        if not dm_state:
            dm_state = DMState()
            session.add(dm_state)
            session.commit()

        current_day = clock.day
        current_hour = clock.hour

        # 1. Fire scheduled events whose time has come
        fired_events = _check_scheduled_events(session, current_day, current_hour, constrained=constrained)

        # 1b. Re-surface fired-but-undelivered events so they can't silently
        #     vanish; the ones out of attempts happened offscreen
        just_fired_ids = {e["id"] for e in fired_events}
        undelivered_events, offscreen_events = _check_undelivered_events(
            session, skip_ids=just_fired_ids, constrained=constrained,
        )

        # 1c. An event at the player's place (or one they were told to
        #     expect) is not news to weave in some day — it is happening
        #     to them. fleet4-wenna: the howl fired at 20:00 while she stood
        #     at its source and the reply never mentioned it.
        here = _player_places(session, player_id)
        _must_deliver.clear()
        _happening_now.clear()
        for e in fired_events + undelivered_events:
            if not e.get("player_visible", True):
                continue
            at_hand = bool(here & set(e.get("locations_involved") or []))
            e["must_deliver"] = at_hand or e["id"] in _foreshadowed
            e["where"] = "here" if at_hand else ("expected" if e["must_deliver"] else "")
            # Told by the prose alone only when it happens where they stand —
            # news from elsewhere arrives in someone's mouth
            if at_hand and (not constrained or aftermath):
                _must_deliver.add(e["id"])
                # Still happening: a reply that skipped or denied it brings
                # it back as now, not as "it is over" (fleet10-bram T18)
                if not _overdue(e, {"day": current_day, "hour": current_hour}):
                    e["now"] = True
                    _happening_now.add(e["id"])
                    # ...and only then is it the step's vigil: a howl that
                    # rose while they slept at the Hollow is no howl stood out
                    e["steps"] = _steps_it_answers(session, player_id, e)

        # 1d. What the world will do later today, so nobody's improvised
        #     schedule contradicts it (fleet4-wenna T3: Piper's "not tonight"
        #     for the howl scheduled that very night)
        todays_events = _todays_public_events(session, current_day, current_hour)

        # 2. Check faction pressures (goals that should be visible)
        faction_pressures = _check_faction_goals(session)

        # 3. Check major NPC goals
        npc_agendas = _check_npc_goals(session)

        # 4. Quiet-turn tracking → mechanical tension escalation.
        #    The DM is asked to manage tension, but if the world stays quiet
        #    too long we escalate for it.
        auto_escalated = False
        if fired_events:
            dm_state.turns_since_event = 0
        else:
            dm_state.turns_since_event = (dm_state.turns_since_event or 0) + 1

        quiet_turns = dm_state.turns_since_event or 0
        if dm_state.tension == "low" and quiet_turns >= 8:
            dm_state.tension = "rising"
            dm_state.turns_since_event = 0
            auto_escalated = True

        # 5. Load the DM's narrative state
        narrative_state = {
            "current_arc": dm_state.current_arc,
            "planned_beats": dm_state.planned_beats or [],
            "completed_beats": dm_state.completed_beats or [],
            "tension": dm_state.tension,
            "active_threats": dm_state.active_threats or [],
            "world_pressures": dm_state.world_pressures or [],
        }

        # 6. Update last tick time
        dm_state.last_tick_day = current_day
        dm_state.last_tick_hour = current_hour
        session.commit()

        # What the last reply's prose fired, said once (`_note_happened`) —
        # of this world: the server switches worlds in one process
        happened_events = [e for e in _happened_news if session.get(Event, e["id"]) is not None]
        _happened_news.clear()

        # An event that cleared a place sends its everyday people in for the
        # night, and first light brings them back (`clear_places`)
        cleared, returned = [], []
        try:
            cleared = clear_places(session, player_id, clock)
            returned = back_at_first_light(session, clock)
        except Exception as e:      # who stands where never costs the turn
            logger.warning(f"clearing a place failed: {e}")
            session.rollback()

    if offscreen_events and player_id:
        _chronicle_offscreen(player_id, offscreen_events)

    return {
        "game_time": f"Day {current_day}, {current_hour:02d}:00",
        "day": current_day,
        "hour": current_hour,
        "constrained": constrained,
        "aftermath": bool(constrained and aftermath),
        "fired_events": fired_events,
        "undelivered_events": undelivered_events,
        "offscreen_events": offscreen_events,
        "happened_events": happened_events,
        "todays_events": todays_events,
        "faction_pressures": faction_pressures,
        "npc_agendas": npc_agendas,
        "narrative_state": narrative_state,
        "auto_escalated": auto_escalated,
        "quiet_turns": quiet_turns,
        "cleared": cleared,
        "returned": returned,
    }


def _player_places(session, player_id: str | None) -> set[str]:
    """The player's location and every place it sits inside (the square's
    howl is heard in the inn on the square)."""
    if not player_id:
        return set()
    player = session.get(Player, player_id)
    places: set[str] = set()
    location_id = player.current_location_id if player is not None else None
    while location_id and location_id not in places:
        places.add(location_id)
        location = session.get(Location, location_id)
        location_id = location.parent_id if location is not None else None
    return places


def _steps_it_answers(session, player_id: str | None, event: dict[str, Any]) -> list[tuple[str, str]]:
    """(quest title, step text) of each open custom step of the player's
    active quests that living through this event satisfies — "Stand out the
    next howl and mark where it rises" for The Evening Howl (job_offers'
    `vigil_matches`). fleet10-bram T18 stood at the howl's source on the
    night Piper asked him to, and the MUST-DELIVER line never said what it
    was for. Never raises."""
    if not player_id:
        return []
    try:
        from types import SimpleNamespace

        from src.services import objectives as objectives_mod
        from src.services.job_offers import vigil_matches

        player = session.get(Player, player_id)
        if player is None:
            return []
        it = SimpleNamespace(name=str(event.get("name") or ""))
        return [(quest.title, step["text"])
                for quest in objectives_mod.active_quests_for(session, player)
                for step in objectives_mod.normalize_objectives(quest.objectives)
                if step.get("kind") == "custom" and not step.get("done") and step.get("text")
                and vigil_matches(step["text"], it)]
    except Exception as e:      # a missed tie-in only costs the old line
        logger.debug(f"steps an event answers unavailable: {e}")
        return []


def _todays_public_events(session, day: int, hour: int) -> list[dict]:
    """Public events still scheduled for later today, earliest first."""
    rows = session.query(Event).filter(
        Event.scheduled_day == day,
        Event.occurred_day.is_(None),
        Event.player_visible == True,  # noqa: E712
    ).all()
    upcoming = [e for e in rows if (e.scheduled_hour or 0) > hour]
    upcoming.sort(key=lambda e: e.scheduled_hour or 0)
    return [{"id": e.id, "name": e.name, "description": e.description, "hour": e.scheduled_hour or 0}
            for e in upcoming]


def _chronicle_offscreen(player_id: str, events: list[dict]) -> None:
    """An event nobody told still happened: the chronicle keeps it after the
    one context that reports it is compacted away."""
    try:
        from src.services import memory_files

        lines = [
            f"- Day {e.get('day')}, {int(e.get('hour') or 0):02d}:00 — {e['name']} {_offscreen_line(e)}: "
            f"{e.get('description') or ''}".rstrip(": ")
            for e in events
        ]
        story = memory_files.read_chronicle(player_id).rstrip()
        memory_files.write_chronicle(player_id, (story + "\n" if story else "") + "\n".join(lines))
    except Exception as e:  # the chronicle is a nicety, never a crash
        logger.warning(f"offscreen events not chronicled: {e}")


def _offscreen_line(event: dict) -> str:
    """How the chronicle files an event that ran out of attempts. "The player
    did not see it" only when nobody — no reply, not the player — spoke of
    it since it fired: fleet9-wenna's Evening Howl was filed unseen though
    the player wrote "The howl echoes in my ears" (T11)."""
    if event.get("id") in _referred:
        return "happened (never staged in full, but the player knows of it — they or someone spoke of it)"
    return "happened offscreen (the player did not see it)"


def _check_scheduled_events(session, current_day: int, current_hour: int, *,
                            constrained: bool = False) -> list[dict]:
    """Find and fire scheduled events whose time has passed."""
    pending = session.query(Event).filter(
        Event.scheduled_day.isnot(None),
        Event.occurred_day.is_(None),  # Not yet fired
    ).all()

    fired = []
    for event in pending:
        event_time = (event.scheduled_day, event.scheduled_hour or 0)
        current_time = (current_day, current_hour)

        if event_time <= current_time:
            # Fire this event — mark it as occurred
            event.occurred_day = current_day
            event.occurred_hour = current_hour
            if event.player_visible:
                # Shown this turn — but a reply the engine owns is no attempt
                event.delivery_attempts = 0 if constrained else 1
            else:
                # Nothing to narrate — mark delivered immediately
                event.narrated_to_player = True

            fired.append({
                "id": event.id,
                "name": event.name,
                "description": event.description,
                "event_type": event.event_type,
                "consequences": event.consequences or [],
                "factions_involved": event.factions_involved or [],
                "locations_involved": event.locations_involved or [],
                "npcs_involved": event.npcs_involved or [],
                "player_visible": event.player_visible,
                # When it was due: a night's rest fires the 20:00 howl on the
                # 07:00 tick, and that is no howl "right now"
                "day": event.scheduled_day,
                "hour": event.scheduled_hour or 0,
            })

    if fired:
        session.commit()

    return fired


def _check_undelivered_events(session, skip_ids: set[str] | None = None, *,
                              constrained: bool = False) -> tuple[list[dict], list[dict]]:
    """Re-surface fired events that were never woven into narration.

    An event gets surfaced to the DM for up to MAX_DELIVERY_ATTEMPTS turns
    after firing; after that it is marked delivered so stale events don't
    clutter the briefing forever — and returned as OFFSCREEN, so the next
    context says it happened instead of it silently vanishing. A reply the
    engine owns (`constrained`) spends no attempt and expires nothing.

    Returns (undelivered, offscreen).
    """
    skip_ids = skip_ids or set()
    pending = session.query(Event).filter(
        Event.occurred_day.isnot(None),
        Event.scheduled_day.isnot(None),  # Only scheduled events need delivery tracking
        Event.player_visible == True,  # noqa: E712
        Event.narrated_to_player == False,  # noqa: E712
    ).all()

    undelivered = []
    offscreen = []
    changed = False
    for event in pending:
        if event.id in skip_ids:
            continue
        row = {
            "id": event.id,
            "name": event.name,
            "description": event.description,
            "consequences": event.consequences or [],
            "locations_involved": event.locations_involved or [],
            "day": event.scheduled_day,
            "hour": event.scheduled_hour or 0,
        }
        attempts = event.delivery_attempts or 0
        if attempts >= MAX_DELIVERY_ATTEMPTS and not constrained:
            event.narrated_to_player = True
            _foreshadowed.discard(event.id)
            offscreen.append(row)
            changed = True
            continue
        undelivered.append(row)
        if not constrained:
            event.delivery_attempts = attempts + 1
            changed = True

    if changed:
        session.commit()

    return undelivered, offscreen


def mark_events_narrated(event_ids: list[str]) -> None:
    """Mark events as delivered to the player (called after a successful turn)."""
    if not event_ids:
        return
    with get_session() as session:
        events = session.query(Event).filter(Event.id.in_(event_ids)).all()
        for event in events:
            event.narrated_to_player = True
        session.commit()


def mark_event_delivery(turn_text: str, speakers: list[str] | None = None, *,
                        spoken_text: str = "", player_input: str = "") -> list[str]:
    """Post-turn: mark every fired-but-unnarrated event this turn told.

    Nothing marked delivery once, so a told event was re-surfaced for three
    turns and staged as new each time (the Evening Howl "started" on four
    turns). Then one loose word told it: Piper's "The howls, they're not —"
    about her research closed The Evening Howl (fleet4-tamsin T12) though
    nobody saw or heard it happen. Now it takes two distinctive words of the
    event close together, one of them from its name ("howling rises from
    the hills"; a long, everyday name needs three), or one of its
    consequences said outright ("the gate's barred"). An event happening
    where the player stands (MUST-DELIVER) is read from `turn_text` alone —
    speech is someone talking about it, often in the future tense.

    `turn_text` is the prose; `spoken_text` the speaker lines. Scheduled
    events the turn spoke of before they fired are remembered as
    foreshadowed; fired ones the turn or `player_input` (the player's own
    words) spoke of, as referred (`_offscreen_line`). Returns the names of
    the events marked.

    An event the reply's own clock carried past (its @time, its @move, the
    turn floor — run before this) has not fired yet: the next tick would
    fire it as news. When the PROSE already told it, it fired in this reply
    and is delivered here (fleet5-tamsin T7: `@time 45m` crossed the 20:00
    howl the reply narrated, and T8 barred the gate again; fleet5-wenna
    T8-T10 told the howl three times).
    """
    prose = turn_text or ""
    speech = "\n".join([spoken_text or ""] + [str(s) for s in (speakers or []) if s])
    everything = f"{prose}\n{speech}"
    if not everything.strip():
        return []
    with get_session() as session:
        pending = session.query(Event).filter(
            Event.occurred_day.isnot(None),
            Event.scheduled_day.isnot(None),
            Event.player_visible == True,  # noqa: E712
            Event.narrated_to_player == False,  # noqa: E712
        ).all()
        heard = f"{everything}\n{player_input or ''}"
        _referred.update(e.id for e in pending if mentioned_keywords([e.name], heard))
        # ...and one happening where they stand only as now, never denied
        # or long over (`told_at_hand`, fleet10-bram T18)
        told = [(e.id, e.name) for e in pending
                if (told_at_hand(e, prose) if e.id in _happening_now else event_told(e, prose))
                or (e.id not in _must_deliver and event_told(e, speech, spoken=True))]
        upcoming = session.query(Event).filter(
            Event.scheduled_day.isnot(None),
            Event.occurred_day.is_(None),
            Event.player_visible == True,  # noqa: E712
        ).all()
        crossed = _crossed_and_told(session, upcoming, prose)
        crossed += _told_early(session, [e for e in upcoming if e not in crossed], prose)
        told += [(e.id, e.name) for e in crossed]
        _foreshadowed.difference_update(e.id for e in crossed)
        _foreshadowed.update(e.id for e in upcoming
                             if e not in crossed and mentioned_keywords([e.name], everything))
    mark_events_narrated([event_id for event_id, _ in told])
    return [name for _, name in told]


def _crossed_and_told(session, upcoming: list, prose: str) -> list:
    """The unfired events now due (this reply's clock passed their hour)
    whose happening the prose told — fired here, as delivered, so the next
    tick neither fires nor re-surfaces them. Speech never counts: "the gate
    shuts at the howl" in someone's mouth is not the howl. Told means told
    happening (`told_happening`): "before the gate bars for the night" in a
    reply whose @time crossed eight is no howl (fleet7-bram T5's line)."""
    clock = session.query(WorldClock).first()
    if clock is None or not prose.strip():
        return []
    now = (clock.day, clock.hour)
    crossed = [e for e in upcoming
               if (e.scheduled_day, e.scheduled_hour or 0) <= now and told_happening(e, prose)]
    for event in crossed:
        event.occurred_day, event.occurred_hour = clock.day, clock.hour
        event.delivery_attempts = 1
        event.narrated_to_player = True
    if crossed:
        _note_happened(crossed, "crossed")
        session.commit()
        logger.info(f"Told as it happened this reply: {', '.join(e.name for e in crossed)}")
    return crossed


# A scheduled event the prose told up to an hour before its hour happened
# then: fleet6-bram T7 told the 20:00 howl at 19:30 and T9 told it again on
# the stroke of eight; fleet6-wenna T8 at 19:50 and T10 at 20:15. The world
# moves its clock on to the event's hour and records it told, so the tick
# never fires it a second time. A memory or a habit ("the howl that came
# last night", "every evening at eight") is no happening.
EARLY_TELLING_MINUTES = 60
_HABIT_OR_PAST = re.compile(
    r"\b(?:last\s+night|yesterday|every\s+(?:night|evening|dusk)|each\s+(?:night|evening|dusk)"
    r"|nights?\s+running|three\s+nights|always|used\s+to|before\s+tonight)\b",
    re.IGNORECASE,
)


def _told_early(session, upcoming: list, prose: str) -> list:
    """The unfired events due within EARLY_TELLING_MINUTES whose happening
    the prose told (`told_happening`): the clock jumps to the latest one's
    hour and each is fired as delivered (see `_crossed_and_told`). Speech
    never counts. The next context says so (`_happened_news`): fleet7-bram
    T5's jump barred the gate and the DM, never told, kept it open."""
    clock = session.query(WorldClock).first()
    if clock is None or not prose.strip():
        return []
    now = clock.day * 1440 + clock.hour * 60 + (clock.minute or 0)
    early = [e for e in upcoming
             if 0 < e.scheduled_day * 1440 + (e.scheduled_hour or 0) * 60 - now <= EARLY_TELLING_MINUTES
             and told_happening(e, prose)]
    if not early:
        return []
    due = max(e.scheduled_day * 1440 + (e.scheduled_hour or 0) * 60 for e in early)
    clock.advance((due - now) / 60.0)
    for event in early:
        event.occurred_day, event.occurred_hour = event.scheduled_day, event.scheduled_hour or 0
        event.delivery_attempts = 1
        event.narrated_to_player = True
    _note_happened(early, "early")
    session.commit()
    logger.info(f"Told early, clock moved to its hour: {', '.join(e.name for e in early)}")
    return early


# The events a reply's prose fired (told a little early, or as its own clock
# crossed their hour), said once in the next context as HAPPENED with their
# consequences: a clock the prose moved must never close a road silently
# (fleet7-bram T5-T12). In memory, like `_foreshadowed`.
_happened_news: list[dict] = []


def _note_happened(events: list, how: str) -> None:
    for event in events:
        _happened_news.append({
            "id": event.id, "name": event.name, "description": event.description,
            "consequences": list(event.consequences or []), "how": how,
            "day": event.scheduled_day, "hour": event.scheduled_hour or 0,
        })


def retire_scheduled_twins(session, title: str, keywords: list[str], directive: str,
                           now: tuple[int, int]) -> list:
    """The unnarrated scheduled events a just-delivered campaign beat already
    told, marked told (occurred now if they had not fired). Round 5,
    fleet6-wenna T9: "The Cart Comes Short" was told in Coldiron's words
    ("cart overturned, no driver") and "The Greywater Cart Arrives Short"
    stayed due at Day 2 09:00 with its talking driver.

    Runs in the caller's transaction (the campaign's) and never commits.
    Returns the events retired (see `is_scheduled_twin`). A beat built on
    the schedule ("as already scheduled") retires the event its directive
    tells, whatever its title: fleet7-tamsin's "The Cart Limps In" told
    the Greywater cart and left it due (the campaign's `_event_twins` reads
    the same rule the other way round)."""
    from src.services.campaign import _BUILT_ON_SCHEDULE

    rows = session.query(Event).filter(
        Event.scheduled_day.isnot(None),
        Event.player_visible == True,  # noqa: E712
        Event.narrated_to_player == False,  # noqa: E712
    ).all()
    built_on = bool(_BUILT_ON_SCHEDULE.search(directive or ""))
    twins = [event for event in rows if is_scheduled_twin(event, title, keywords, directive)
             or (built_on and event_told(event, directive or ""))]
    for event in twins:
        if event.occurred_day is None:
            event.occurred_day, event.occurred_hour = now
        event.narrated_to_player = True
        event.delivery_attempts = max(1, event.delivery_attempts or 0)
        _foreshadowed.discard(event.id)
    if twins:
        logger.info(f"Told by a campaign beat ({title}): {', '.join(e.name for e in twins)}")
    return twins


def is_scheduled_twin(event: Any, title: str, keywords: list[str], directive: str) -> bool:
    """Does a campaign beat restate this scheduled event? The event's whole
    name as the beat's title ("The Evening Howl"); three quarters of its
    name's words across the title and keywords ("Greywater", "cart",
    "short" of "The Greywater Cart Arrives Short" in "The Cart Comes Short"
    + "Greywater cart"); or the directive telling it (`event_told`) under a
    title naming two of its words (fleet6-bram's "The Cart Comes Short":
    "Deliver the scheduled arrival of the half-empty Greywater cart").
    Replayed over every fleet campaign's 172 beats x the seed's two events:
    nine pairs, each a beat restating its event — a directive alone paired
    Piper's howl charts with the Evening Howl and a fence's cart-marked
    crates with the Greywater cart, and "evening howl" as a keyword is
    only when a deadline falls, not the howl itself."""
    name = str((event.get("name") if isinstance(event, dict) else getattr(event, "name", "")) or "")
    phrase = " ".join(w for w in _words(name) if w not in ("the", "a", "an"))
    if phrase and f" {phrase} " in f" {' '.join(_words(title or ''))} ":
        return True
    name_words = _distinctive(name)
    if len(name_words) < 2:
        return False
    named = sum(1 for w in name_words
                if _word_forms(w) & _text_forms(" ".join([title or ""] + [str(k) for k in keywords or []])))
    if named >= 2 and named * 4 >= 3 * len(name_words):
        return True
    in_title = sum(1 for w in name_words if _word_forms(w) & _text_forms(title or ""))
    return in_title >= 2 and event_told(event, directive or "")


# Words an event description is full of that tell nothing about WHICH event
_EVENT_FILLER = frozenset({
    "itself", "himself", "herself", "themselves", "than", "then", "there", "here", "when",
    "where", "while", "after", "before", "again", "once", "only", "even", "also", "like",
    "because", "through", "across", "around", "against", "between", "without", "within",
    "every", "each", "both", "fast", "early", "late", "closer", "stroke", "begins", "starts",
    "town", "people", "someone", "something", "everyone", "anyone", "word",
})
_SENTENCE_BREAK = re.compile(r"(?<=[.!?…])\s+|\n+")
_EVENT_WINDOW = 10      # words: an event's tells sit together
# A sentence that denies the event or puts it ahead tells nothing: "no sign
# yet of the howl in the square" (fleet4-wenna T6: "not one single howl, not
# yet"), "the gate closes at the howl — you'll want to be inside"
_NOT_YET = re.compile(
    r"\b(?:no|not|never|without|until|unless|if|will|won['’]t|going\s+to|soon)\b|n['’]t\b|['’]ll\b",
    re.IGNORECASE,
)


def _content_words(text: str) -> list[str]:
    out: list[str] = []
    for word in _words(text):
        if len(word) >= 4 and word not in _GENERIC_WORDS and word not in _EVENT_FILLER and word not in out:
            out.append(word)
    return out


def event_told(event: Any, text: str, *, spoken: bool = False, screened: bool = False) -> bool:
    """Did `text` tell this event? `event` is an Event row or a dict with
    name / description / consequences. See `mark_event_delivery`.

    `spoken`: the text is people talking. Talk about an event is mostly its
    routine ("The gate closes at the howl", "a real wolf howls… answering
    each other" — fleet4-tamsin T13, fleet4-wenna T8), so the name needs one
    more detail; a consequence said outright ("They've barred the gate
    already") still tells it.

    `screened`: the caller already dropped what denies it or puts it
    ahead, clause by clause (`told_happening`)."""
    def field(key: str) -> Any:
        return event.get(key) if isinstance(event, dict) else getattr(event, key, None)

    name_words = _distinctive(str(field("name") or ""))
    name_forms = set().union(*(_word_forms(w) for w in name_words)) if name_words else set()
    details = [w for w in _content_words(" ".join(
        [str(field("description") or "")] + [str(c) for c in (field("consequences") or [])]
    )) if not (_word_forms(w) & name_forms)]
    consequences = [_content_words(str(c)) for c in (field("consequences") or [])]
    # A long name's words are everyday words too ("The Greywater Cart Arrives
    # Short": every cart in a carting town) — one of them needs two details
    need = (2 if len(name_words) <= 1 else 3) + (1 if spoken else 0)
    for sentence in _SENTENCE_BREAK.split(text or ""):
        if not sentence.strip() or (not screened and _NOT_YET.search(sentence)):
            continue
        # Close together, not merely in one long sentence: "the gate… finally
        # rises… a short worried line" is no cart arriving short
        tokens = [_word_forms(t) for t in _words(sentence)]
        for start in range(max(1, len(tokens) - _EVENT_WINDOW + 1)):
            forms = set().union(*tokens[start:start + _EVENT_WINDOW]) if tokens else set()
            named = sum(1 for w in name_words if _word_forms(w) & forms)
            detailed = sum(1 for w in details if _word_forms(w) & forms)
            if len(name_words) >= 2 and named == len(name_words):
                return True                              # the whole name, said
            if (named and named + detailed >= need) or (not name_words and detailed >= need):
                return True
            # A short consequence said whole: half of "Townsfolk clear the
            # square" is any evening ("townsfolk drift across the square"),
            # and a false "told" drops the event for good while a miss only
            # re-shows it
            for words in consequences:
                hits = sum(1 for w in words if _word_forms(w) & forms)
                if len(words) >= 2 and hits >= (len(words) if len(words) <= 3
                                                else math.ceil(len(words) * 2 / 3)):
                    return True
    return False


# Before its hour an event is told only by a sentence that has it HAPPENING.
# fleet7-bram T5 at 19:00: "you can hear nothing yet ... the hills still
# quiet, the howl still an hour off", and the DM's own pass "good for one
# crossing before the gate bars for the night", each held the howl or a
# consequence beside a detail word: the clock jumped to 20:00, the gate
# barred unseen and the act's set piece was spent. A sentence that counts
# down to it, puts it ahead or supposes it never tells it; a clause that
# denies it is dropped (fleet6-bram T7's "the first howl rises — not
# ragged, not wild" still tells it); and what is left needs a verb of the
# happening beside the event's name or a consequence ("the howl rises",
# "rose", "breaks", "comes", "a bar drops across the North Gate"). A
# might-be is no happening either: "the howl could rise any minute now",
# "is expected to", "you listen for it", a question.
_AHEAD = re.compile(
    r"\b(?:yet|still|before|due|until|till|by\s+the\s+time|soon|tomorrow|tonight['’]s"
    r"|will|would|shall|going\s+to|about\s+to|unless|whether"
    r"|could|might|may|should|must|expected\s+to|any\s+(?:minute|moment|second)|within\s+the\s+hour"
    r"|listen(?:s|ed|ing)?\s+for"
    r"|wait(?:s|ed|ing)?\s+(?:for|on)|a\s+ways?\s+(?:off|away)"
    r"|in\s+(?:an?|one|two|three|half\s+an?|a\s+few|\d+)\s+(?:\w+\s+)?(?:hours?|minutes?|mins?|bells?)"
    r"|(?:an?|one|two|three|half\s+an?|a\s+few|few|\d+)\s+(?:\w+\s+)?(?:hours?|minutes?|mins?|bells?)"
    r"\s+(?:off|away|out|left|from\s+now|to\s+go))\b"
    r"|(?<!as\s)\bif\b|['’]ll\b|won['’]t\b|\?[\"”’']?\s*$",
    re.IGNORECASE,
)
_DENIED = re.compile(r"\b(?:no|not|never|without|nothing|nobody|none)\b|n['’]t\b", re.IGNORECASE)
_CLAUSE_BREAK = re.compile(r"\s*(?:[,;:()]|—|–|\s-\s)\s*")
_HAPPENING_REACH = 6    # words between the event (or a consequence) and its verb
_HAPPENING = frozenset("""
    rise rises rising rose risen  break breaks breaking broke broken  come comes coming came
    roll rolls rolling rolled  start starts starting started  begin begins beginning began begun
    sound sounds sounding sounded  ring rings ringing rang rung  carry carries carrying carried
    fill fills filling filled  echo echoes echoing echoed  swell swells swelling swelled
    climb climbs climbing climbed  lift lifts lifting lifted  spill spills spilling spilled
    pour pours pouring poured  split splits splitting  tear tears tearing tore torn
    burst bursts bursting  erupt erupts erupting erupted  howl howls howling howled
    answer answers answering answered  wail wails wailing wailed  cry cries crying cried
    call calls calling called  arrive arrives arriving arrived  limp limps limping limped
    creak creaks creaking creaked  rumble rumbles rumbling rumbled  clatter clatters clattered
    trundle trundles trundling trundled  lurch lurches lurching lurched
    bar bars barring barred  shut shuts shutting  close closes closing closed
    slam slams slamming slammed  drop drops dropping dropped  fall falls falling fell fallen
    empty empties emptying emptied  clear clears clearing cleared  bang bangs banging banged
    groan groans groaning groaned  boom booms booming boomed  cut cuts cutting  hit hits hitting
    reach reaches reaching reached  fade fades fading faded  pierce pierces piercing pierced
    sweep sweeps sweeping swept  goes went
""".split())


def told_happening(event: Any, text: str) -> bool:
    """Did this prose tell an event that has not fired yet HAPPENING? The
    test `_told_early` and `_crossed_and_told` fire it on (see `_AHEAD`)."""
    def field(key: str) -> Any:
        return event.get(key) if isinstance(event, dict) else getattr(event, key, None)

    name_words = _distinctive(str(field("name") or ""))
    verbish = [w for w in name_words if w in _HAPPENING and w.endswith(("s", "ed"))]
    consequence_words = [w for c in (field("consequences") or []) for w in _content_words(str(c))]
    anchors = set().union(set(), *(_word_forms(w) for w in name_words + consequence_words))
    # Neither the name's nouns ("the howls" is talk about howls) nor a
    # consequence's state ("the barred gate", fleet5-tamsin T0, before any
    # howl) is the verb; a name that is a clause lends its own ("Cart
    # ARRIVES Short")
    no_verb = anchors - set().union(set(), *(_word_forms(w) for w in verbish))
    for sentence in _SENTENCE_BREAK.split(text or ""):
        if not sentence.strip() or _AHEAD.search(sentence) or _HABIT_OR_PAST.search(sentence):
            continue
        said = " ".join(c for c in _CLAUSE_BREAK.split(sentence) if c.strip() and not _DENIED.search(c))
        words = _words(said)
        at = [i for i, w in enumerate(words) if _word_forms(w) & anchors]
        does = [i for i, w in enumerate(words) if w in _HAPPENING and not (_word_forms(w) & no_verb)]
        if any(0 < abs(i - j) <= _HAPPENING_REACH for i in at for j in does) \
                and event_told(event, said, screened=True):
            return True
    return False


# An event happening AROUND the player is told as now — never as long over,
# and never in a reply that denies it. fleet10-bram T18: the 20:00 howl fired
# with him standing at its source, and the reply wrote "No howl follows. No
# whistle answers yours … the gate's long since barred behind the evening
# howl": the past-perfect clause passed as the telling, the howl was marked
# told, and the step it was for ("Stand out the next howl") stayed open.
_LONG_PAST = re.compile(
    r"\b(?:long\s+since|long\s+ago|(?:an|one|two|three|some|a\s+few|\d+)\s+hours?\s+(?:ago|since|back)"
    r"|hours\s+(?:ago|since|back)|already\s+over|had\s+(?:long\s+|already\s+|just\s+)?(?:been\s+)?\w+(?:ed|en)\b)",
    re.IGNORECASE,
)
# "No howl follows", "no howls tonight", "the night passes without a howl" —
# never "not": "not a howl at all" / "not quite a howl" names ANOTHER sound,
# and on the replayed corpus it sat beside real tellings (fleet10-tamsin T17
# "the first howl breaks", fleet5-tamsin T7, fleet6-bram T9)
_DENIAL_WORDS = frozenset({"no", "nor", "never", "without"})
# "No second howl follows" says one came; "no more howling" that it stopped
_AGAIN = frozenset({"again", "more", "second", "another", "further", "other", "else", "twice", "longer", "third"})
_DENIAL_REACH = 3       # words between the "no" and the thing it denies
# An event name's words that name no thing ("Another Cart Taken" is the cart):
# "nothing taken" from a thief is no denial of a cart
_NOT_A_THING = frozenset({"another", "taken", "missing", "someone", "else", "short", "late", "early", "first",
                          "last", "second", "third", "great", "little", "grows", "cold", "death", "warning"})


def _denies(event: Any, text: str) -> bool:
    """Does `text` say the event did not come — "No howl follows", "not a
    howl this time"? A "no" within _DENIAL_REACH words before the thing the
    event's name names, in one clause, and no "again"/"second" there."""
    name = str((event.get("name") if isinstance(event, dict) else getattr(event, "name", "")) or "")
    # (a name that is a clause lends no thing: "Cart ARRIVES Short")
    heads = [w for w in _distinctive(name)
             if w not in _NOT_A_THING and not (w in _HAPPENING and w.endswith(("s", "ed")))]
    forms = set().union(set(), *(_word_forms(w) for w in heads))
    if not forms:
        return False
    for sentence in _SENTENCE_BREAK.split(text or ""):
        for clause in _CLAUSE_BREAK.split(sentence):
            words = _words(clause)
            if set(words) & _AGAIN:
                continue
            for i, word in enumerate(words):
                before = words[max(0, i - _DENIAL_REACH):i]
                # ("howl or no howl" is a shrug, not a denial)
                if _word_forms(word) & forms and set(before) & _DENIAL_WORDS and "or" not in before:
                    return True
    return False


def told_at_hand(event: Any, text: str) -> bool:
    """Did this prose tell an event happening where the player stands? Told
    (`event_told`) by what is left once clauses that put it long past are
    dropped — and a reply that denies it (`_denies`) tells it only by a
    sentence of its own that has it HAPPENING outright (`told_happening`):
    "Howling rises from the northern hills… you whistle back, but no howl
    answers yours" is the howl, "No howl follows… long since barred behind
    the evening howl" is not. A miss only brings it back next reply, a false
    "told" loses the scene for good."""
    # (the clauses kept apart: `told_happening` drops each that denies it)
    kept = [", ".join(c for c in _CLAUSE_BREAK.split(sentence) if c.strip() and not _LONG_PAST.search(c))
            for sentence in _SENTENCE_BREAK.split(text or "")]
    kept = "\n".join(s for s in kept if s.strip())
    return told_happening(event, kept) if _denies(event, text) else event_told(event, kept)


def _check_faction_goals(session) -> list[dict]:
    """Summarize active faction goals that could drive world events."""
    factions = session.query(Faction).all()

    pressures = []
    for faction in factions:
        short_term = faction.goals_short if hasattr(faction, 'goals_short') and faction.goals_short else []
        long_term = faction.goals_long if hasattr(faction, 'goals_long') and faction.goals_long else []

        if short_term or long_term:
            pressures.append({
                "faction": faction.name,
                "faction_id": faction.id,
                "short_term_goals": short_term[:2],  # Top 2 only
                "long_term_goals": long_term[:1],     # Top 1 only
                "power_level": getattr(faction, 'power_level', None),
            })

    return pressures


def _check_npc_goals(session) -> list[dict]:
    """Summarize major NPC goals that could drive narrative."""
    major_npcs = session.query(NPC).filter(
        NPC.tier == NPCTier.MAJOR,
        NPC.status == "alive",
    ).all()

    agendas = []
    for npc in major_npcs:
        goals = npc.goals if hasattr(npc, 'goals') and npc.goals else []
        if goals:
            agendas.append({
                "npc": npc.name,
                "npc_id": npc.id,
                "location": npc.current_location_id,
                "goals": goals[:2],  # Top 2 goals
                "mood": npc.current_mood,
            })

    return agendas


# --- a place cleared, and who goes in ------------------------------------

# Place words that name their place in lower case ("clear the square")
_PLACE_WORDS = frozenset({"square", "yard", "market", "plaza", "street", "streets", "docks", "harbour", "harbor",
                          "bazaar", "commons", "green", "courtyard", "camp", "quarter", "district"})
# ...said of the place itself: "the square empties", never "the road to the
# square is clear"
_EMPTIES = (r"(?:clears|cleared|empties|emptied|emptying|is\s+(?:emptied|empty|deserted|cleared)|stands?\s+empty"
            r"|goes\s+(?:\w+\s+)?(?:empty|quiet|still|dark)|falls?\s+(?:empty|silent|still))")


def _cleared_by(event: Any, place: Any) -> bool:
    """Does this event's text clear this place? "Townsfolk clear the square",
    "the square empties fast": a clearing verb on the place itself — never
    "the road is clear" (no verb on a place) or a gate "barred"."""
    words = re.findall(r"[a-z][a-z'’-]*", str(getattr(place, "name", "") or "").lower().replace("’", "'"))
    if not words:
        return False
    forms = [" ".join(words)]
    if len(words) > 1 and words[-1] in _PLACE_WORDS:
        forms.append(words[-1])
    texts = [str(c) for c in (getattr(event, "consequences", None) or [])] + [str(getattr(event, "description", "")
                                                                               or "")]
    place = "(?:" + "|".join(re.escape(f) for f in forms) + ")"
    clear_it = re.compile(
        rf"\b(?:clears?|cleared|clearing|empt(?:y|ies|ied|ying))\s+(?:out\s+)?(?:of\s+)?(?:the\s+)?{place}\b"
        rf"|\b{place}\s+(?:\w+\s+)?{_EMPTIES}\b", re.IGNORECASE)
    return any(clear_it.search(t.replace("’", "'")) for t in texts)


def kept_by_story(session, player_id: str | None, location_id: str | None) -> set[str]:
    """Who the story needs where they stand: the campaign's villain and
    successor (it stages them), and everyone a beat due now (fired or
    stalled) or armed to happen at `location_id` needs (`_beat_cast`). Shared
    with the server's prose-made moves (`_walked_off`). Never raises."""
    try:
        from src.services.campaign import _beat_cast, _beat_place_ids, _campaign_beats, get_campaign_row

        campaign = get_campaign_row(session, player_id) if player_id else None
        if campaign is None or campaign.status not in ("pending", "active"):
            return set()
        ids = {i for i in (campaign.villain_npc_id, campaign.successor_npc_id) if i}
        if campaign.status != "active":
            return ids
        npcs = session.query(NPC).filter(NPC.status == "alive").all()
        for beat in _campaign_beats(session, campaign):
            if beat.status in ("fired", "stalled") or (
                    beat.status == "armed" and location_id and location_id in _beat_place_ids(beat)):
                ids |= {npc.id for npc in _beat_cast(session, beat, npcs=npcs)}
        return ids
    except Exception as e:
        logger.debug(f"story keep-list unavailable: {e}")
        return set()


def _wanted_here(session, player) -> set[str]:
    """The people the player's jobs and lead point at: the givers of active
    quests, the people their undone steps name, and the lead's person —
    they wait where the tracker sends the player."""
    from src.services import lead as lead_mod
    from src.services import objectives as objectives_mod

    ids: set[str] = set()
    try:
        for quest in objectives_mod.active_quests_for(session, player):
            if quest.assigned_by_npc_id:
                ids.add(quest.assigned_by_npc_id)
            for step in objectives_mod.normalize_objectives(quest.objectives):
                if isinstance(step, dict) and not step.get("done") and step.get("target_id"):
                    ids.add(str(step["target_id"]))
        target = (lead_mod.compute_lead(session, player) or {}).get("target") or {}
        if target.get("type") == "npc" and target.get("id"):
            ids.add(str(target["id"]))
    except Exception as e:
        logger.debug(f"quest people for a cleared place unavailable: {e}")
    return ids


def _shelter(session, place: Location) -> Location | None:
    """Where a cleared place's people wait out the night: a building in it
    or beside it, an inn first."""
    from src.models import Connection

    near = {row[0] for row in session.query(Location.id).filter(Location.parent_id == place.id).all()}
    for conn in session.query(Connection).filter(
            (Connection.from_location_id == place.id) | (Connection.to_location_id == place.id)).all():
        near.add(conn.to_location_id if conn.from_location_id == place.id else conn.from_location_id)
    near.discard(place.id)
    rooms = [loc for loc in session.query(Location).filter(Location.id.in_(near)).all()
             if str(getattr(loc.type, "value", loc.type)).lower() in ("building", "interior")]
    rooms.sort(key=lambda loc: (not (isinstance(loc.rest, dict) and loc.rest.get("kind") == "inn"), loc.name or ""))
    return rooms[0] if rooms else None


def clear_places(session, player_id: str | None, clock: WorldClock) -> list[dict[str, str]]:
    """Send the everyday people of a place a fired event cleared indoors.

    For each event fired in the last CLEAR_FRESH_HOURS whose text clears one
    of its places (`_cleared_by`), once: the minor and ambient people there
    go home (their own place, if it is another) or into its shelter (an inn
    beside it). Never a major figure, a beast, a companion, anyone the story
    keeps there (`kept_by_story`), nor anyone the player's jobs or lead
    point at (`_wanted_here`) — the herbalist whose job is running stays to
    be paid. Runs in the caller's session and commits. Returns
    [{"event", "name", "from", "to"}] for the next context."""
    if not player_id:
        return []
    player = session.get(Player, player_id)
    if player is None:
        return []
    from src.services import party as party_mod
    from src.services.dialogue import is_beast

    now = clock.day * 24 + clock.hour
    moved: list[dict[str, str]] = []
    for event in session.query(Event).filter(Event.occurred_day.isnot(None)).all():
        if event.id in _cleared:
            continue
        age = now - (event.occurred_day * 24 + (event.occurred_hour or 0))
        if age < 0 or age > CLEAR_FRESH_HOURS:
            continue
        places = [session.get(Location, i) for i in (event.locations_involved or []) if i]
        places = [p for p in places if p is not None and _cleared_by(event, p)]
        _cleared.add(event.id)
        if not places:
            continue
        wanted = set(party_mod.member_ids(player)) | _wanted_here(session, player)
        for place in places:
            keep = wanted | kept_by_story(session, player_id, place.id)
            shelter = _shelter(session, place)
            for npc in session.query(NPC).filter(NPC.current_location_id == place.id, NPC.status == "alive").all():
                if npc.tier == NPCTier.MAJOR or npc.id in keep or is_beast(npc):
                    continue
                home = session.get(Location, npc.home_location_id) if npc.home_location_id else None
                to = home if home is not None and home.id != place.id else shelter
                if to is None or to.id == place.id:
                    continue
                npc.current_location_id = to.id
                if to is shelter:
                    first_light = event.occurred_day + (1 if (event.occurred_hour or 0) >= FIRST_LIGHT_HOUR else 0)
                    _gone_in[npc.id] = (place.id, to.id, first_light)
                moved.append({"event": event.name, "name": npc.name, "from": place.name, "to": to.name,
                              "here": place.id == player.current_location_id})
    if moved:
        session.commit()
        logger.info("Gone in for the night: " + "; ".join(f"{m['name']} -> {m['to']}" for m in moved))
    return moved


def back_at_first_light(session, clock: WorldClock) -> list[dict[str, str]]:
    """The people a cleared place sent into a shelter go back at first
    light — unless they moved since (the story took them somewhere: theirs
    to keep). Commits. Returns [{"name", "from", "to"}]."""
    back: list[dict[str, str]] = []
    for npc_id, (home_id, shelter_id, day) in list(_gone_in.items()):
        if (clock.day, clock.hour) < (day, FIRST_LIGHT_HOUR):
            continue
        del _gone_in[npc_id]
        npc = session.get(NPC, npc_id)
        home = session.get(Location, home_id)
        if npc is None or home is None or (npc.status or "alive") != "alive" or npc.current_location_id != shelter_id:
            continue
        shelter = session.get(Location, shelter_id)
        npc.current_location_id = home_id
        back.append({"name": npc.name, "from": shelter.name if shelter else "", "to": home.name})
    if back:
        session.commit()
    return back


def format_world_tick_context(tick_result: dict[str, Any]) -> str:
    """Format the world tick result into a readable context block for the DM prompt.

    Args:
        tick_result: Output of run_world_tick().

    Returns:
        Formatted string to inject into the DM's context.
    """
    parts = []

    # Narrative state (always show)
    ns = tick_result.get("narrative_state", {})
    if ns.get("current_arc"):
        parts.append(f"**YOUR NARRATIVE ARC**: {ns['current_arc']}")
        # DMState.planned_beats is world-global and was often authored for
        # someone else — the seed's are for its suggested courier, and a
        # custom apothecary was handed "the letter in your pocket" (playtest
        # 2026-10-04). The campaign owns the arc now and each character's
        # own threads live in their DM notes (@plan), so the list stays out.
        parts.append(f"  Tension: {ns.get('tension', 'low')}")

    if ns.get("active_threats"):
        threats = ", ".join(ns["active_threats"])
        parts.append(f"  Active threats: {threats}")

    if ns.get("world_pressures"):
        pressures = ", ".join(ns["world_pressures"])
        parts.append(f"  World pressures: {pressures}")

    # Auto-escalation notice
    if tick_result.get("auto_escalated"):
        parts.append(
            "\n**TENSION AUTO-ESCALATED TO 'rising'** — the world has been quiet too long. "
            "Introduce a complication, rumor, or incident THIS turn (`@tension rising`, `@plan` the thread)."
        )

    # An event at the player's place, or one they were told to expect, is
    # this reply's — "weave it in" let the howl fire at 20:00 while Wenna
    # stood at its source and three replies pass it by (fleet4-wenna)
    constrained = bool(tick_result.get("constrained"))
    fired = tick_result.get("fired_events", [])
    undelivered = tick_result.get("undelivered_events", [])
    fired_ids = {e["id"] for e in fired}
    # A fight's aftermath still owns what happens where the player stands
    # (fleet10-bram T17-T18); the other engine replies keep theirs
    aftermath = bool(tick_result.get("aftermath"))
    must = [e for e in fired + undelivered if e.get("must_deliver")
            and (not constrained or (aftermath and e.get("where") == "here"))]
    must_ids = {e["id"] for e in must}
    if must:
        parts.append(
            "\n**MUST-DELIVER THIS REPLY — world events reaching the player** (the engine fired them; "
            "they are real. Put each in this reply's PROSE as what the player sees or hears, at the "
            "scale written — not as a rumour, not in someone's future tense):"
        )
        for e in must:
            if e.get("where") == "here" and e.get("now"):
                # fleet10-bram T18: "No howl follows … the gate's long since
                # barred behind the evening howl", with him at its source
                again = "" if e["id"] in fired_ids else " — your last reply did not show it, so it comes now"
                parts.append(
                    f"  - {e['name']} — HAPPENING WHERE YOU STAND — this reply's scene{again}"
                    + (" (the fight is over, and this is what comes next)" if aftermath else "")
                    + f": {e['description']}")
                parts.append("    Show it reaching the player, as it happens, in this reply's prose. Never write that "
                             "it does not come, or tell it as long over — a reply that does has not told it, and it "
                             "comes back.")
            else:
                if e["id"] in fired_ids and not _overdue(e, tick_result):
                    how = "happening now — they were told to expect it; let it reach them"
                else:
                    when = _past_label(e, tick_result.get("day")).split(" — ")[0]
                    how = (f"{when}, around them — it is over now: show what it left behind, in the past tense"
                           if e.get("where") == "here"
                           else f"{when} — they were told to expect it: let the news reach them")
                parts.append(f"  - {e['name']} ({how}): {e['description']}")
            if e.get("consequences"):
                parts.append(f"    It means: {', '.join(e['consequences'])} — true now; show it.")
            # What the player is standing there for: the job's own step
            for title, step in e.get("steps") or []:
                parts.append(f"    This is what '{title}' asks them to live through ('{step}'), and they are in "
                             f"it: show them taking it in, and write on its own line: @step-done {title} | {step}")

    # Fired events (important — these just happened)
    fired = [e for e in fired if e["id"] not in must_ids]
    if fired:
        parts.append("\n**EVENTS THAT JUST FIRED** (weave these into your narration):")
        for e in fired:
            vis = " [player can learn about this]" if e["player_visible"] else " [hidden from player]"
            parts.append(f"  - {e['name']}: {e['description']}{vis}")
            if e["consequences"]:
                parts.append(f"    Consequences: {', '.join(e['consequences'])} — apply these with tools, don't just mention them.")

    # Fired earlier but never narrated — these MUST reach the player, as
    # something that already happened: re-surfaced as if fresh, the DM
    # staged the same howl "starting" on four turns (playtest 2026-10-04)
    undelivered = [e for e in undelivered if e["id"] not in must_ids]
    if undelivered:
        parts.append("\n**EVENTS THE PLAYER STILL HASN'T HEARD ABOUT** (they already happened — let the player learn of them as news, aftermath or a remark):")
        for e in undelivered:
            parts.append(f"  - {e['name']} ({_past_label(e, tick_result.get('day'))}): {e['description']}")
    if constrained and (fired or undelivered):
        parts.append("  (This reply belongs to the engine's directive. If it has no room for these, "
                     "they keep — they come back next turn.)")

    # Out of attempts: still true, just never seen
    offscreen = tick_result.get("offscreen_events", [])
    if offscreen:
        parts.append(
            "\n**HAPPENED OFFSCREEN** (never told in time — the player did not see these, but they "
            "are true now: the world shows their aftermath; nobody stages them again):"
        )
        for e in offscreen:
            when = _past_label(e, tick_result.get("day")).split(" — ")[0]
            # ...unless it was spoken of: then the player knows (fleet9-wenna T11)
            known = " — the player knows of it (it was spoken of), so nobody treats it as news" \
                if e.get("id") in _referred else ""
            parts.append(f"  - {e['name']} ({when}){known}: {e['description']}")

    # Fired by the last reply's own prose: the clock may have moved, and
    # what the event closes is closed (fleet7-bram T5: the gate barred at a
    # jump nobody was told of, and T10 still walked through it "open")
    happened = tick_result.get("happened_events", [])
    if happened:
        parts.append(
            "\n**HAPPENED — your last reply told it, so the world fired it** (over and told; never "
            "stage it again, and nobody speaks of it as still to come):"
        )
        for e in happened:
            hour = f"{int(e.get('hour') or 0):02d}:00"
            how = (f"told just before its hour — the clock moved on to {hour}" if e.get("how") == "early"
                   else f"due at {hour}, told as your clock passed it")
            parts.append(f"  - {e['name']} ({how}): {e.get('description') or ''}".rstrip(": "))
            if e.get("consequences"):
                parts.append(f"    It means: {', '.join(e['consequences'])} — true now; hold to it, "
                             "and say so if the player acts as if it were not.")

    # The people an event's clearing sent in, and back at first light
    # (`clear_places`): the canvas shows them where these lines say
    cleared = tick_result.get("cleared") or []
    if cleared:
        parts.append("\n**GONE IN FOR THE NIGHT** (the event cleared their place, so the engine moved them; they "
                     "are not there now — if your story keeps one of them out, write @npc-move to put them back):")
        for m in cleared:
            # The brief was written before the tick: the player stands there
            # and may still see them listed as present — this reply shows
            # them going, so the canvas and the story agree
            seen = (" — the player is there: show them hurrying off in this reply" if m.get("here") else "")
            parts.append(f"  - {m['name']}: {m['from']} -> {m['to']} ({m['event']}){seen}")
    returned = tick_result.get("returned") or []
    if returned:
        parts.append("\n**BACK AT FIRST LIGHT** (out again where they were before the night cleared it): "
                     + "; ".join(f"{m['name']} -> {m['to']}" for m in returned))

    # The world's own clock for the rest of today, so an NPC's improvised
    # schedule cannot contradict it (fleet4-wenna T3: "not tonight")
    todays = tick_result.get("todays_events", [])
    if todays:
        parts.append(
            "\n**LATER TODAY** (public, on the world's clock — anyone who speaks of these agrees with "
            "the hour; nothing of it happens early):"
        )
        for e in todays:
            about = str(e.get("description") or "")
            about = about if len(about) <= 160 else about[:159].rstrip() + "…"
            parts.append(f"  - {int(e.get('hour') or 0):02d}:00 {e['name']}: {about}")

    # Faction pressures (background)
    factions = tick_result.get("faction_pressures", [])
    if factions:
        parts.append("\n**FACTION AGENDAS** (use to drive background tension):")
        for f in factions:
            goals = "; ".join(f["short_term_goals"])
            parts.append(f"  - {f['faction']}: {goals}")

    # NPC agendas (major NPCs with their own plans)
    npcs = tick_result.get("npc_agendas", [])
    if npcs:
        parts.append("\n**MAJOR NPC AGENDAS** (NPCs pursuing their own goals):")
        for n in npcs:
            goals = "; ".join(n["goals"])
            parts.append(f"  - {n['npc']} ({n['mood']}): {goals}")

    if not parts:
        return ""

    return "\n### WORLD STATE (pre-turn briefing)\n" + "\n".join(parts)


def _overdue(event: dict[str, Any], tick_result: dict[str, Any]) -> bool:
    """Fired two or more hours after it was due: a night's rest (or a long
    @time) carried the clock past it, so it happened while the player slept
    — never "right now" at dawn."""
    if event.get("day") is None or tick_result.get("day") is None or tick_result.get("hour") is None:
        return False
    due = int(event["day"]) * 24 + int(event.get("hour") or 0)
    return int(tick_result["day"]) * 24 + int(tick_result["hour"]) - due >= 2


def _past_label(event: dict[str, Any], today: int | None) -> str:
    """'happened at 20:00 — it is already over; ...' for a re-surfaced event."""
    when = f"{int(event.get('hour') or 0):02d}:00"
    if event.get("day") is not None and today is not None and event["day"] != today:
        when = f"day {event['day']}, {when}"
    return (f"happened at {when} — it is already over; refer to it in the past tense, "
            "do not stage it again")


def snapshot_clock() -> tuple[int, int, int]:
    """Return the current (day, hour, minute) for time-cost enforcement."""
    with get_session() as session:
        clock = session.query(WorldClock).first()
        if not clock:
            return (1, 8, 0)
        return (clock.day, clock.hour, clock.minute or 0)


def enforce_minimum_time_cost(start_clock: tuple[int, int, int],
                              minimum_hours: float = MIN_TURN_HOURS) -> dict[str, Any]:
    """Ensure game time advanced during a turn.

    The DM is instructed to call `advance_time`, but if it forgot, the world
    clock — and everything scheduled against it — freezes. This applies a
    minimum cost per turn so scheduled events always eventually fire.

    Args:
        start_clock: (day, hour, minute) when the turn started.
        minimum_hours: Time to apply if the DM didn't advance the clock.

    Returns:
        Dict with whether time was enforced and the current clock.
    """
    with get_session() as session:
        clock = session.query(WorldClock).first()
        if not clock:
            return {"enforced": False}

        if (clock.day, clock.hour, clock.minute or 0) != tuple(start_clock):
            return {"enforced": False, "day": clock.day, "hour": clock.hour}

        clock.advance(minimum_hours)
        session.commit()
        return {"enforced": True, "day": clock.day, "hour": clock.hour}

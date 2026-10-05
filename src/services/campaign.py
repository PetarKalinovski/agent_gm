"""Campaign spine — a story set in stone, fired by code.

The campaign is authored ONCE (a single fast-model call at character creation)
and from then on belongs to the engine: `run_campaign_tick` decides when a beat
happens, `execute_ops` applies its consequences through a fixed op vocabulary,
and the DM is handed a MUST-DELIVER directive plus a ledger of what already
changed. The LLM never writes a campaign fact again — it reads and narrates.

Three rules this module exists to enforce:

- **Code owns every number.** The generator picks LABELS ("weakened", "short",
  "soon"); every magnitude lives in the constants below.
- **Unfired beats are never shown to the DM.** Front-running is the observed
  failure mode, so the context block is starved rather than prompted harder.
- **The story moves without the player.** Every armed beat carries a hard
  fallback time; if the world never satisfies its trigger, it fires anyway.

Nothing here touches the network on the turn path — a tick is a handful of
indexed SQLite queries. Generation (`ensure_campaign`) runs on a background
thread and degrades to "the game exactly as it plays today" on failure.
"""

import re
import threading
from datetime import datetime, timedelta
import uuid
from typing import Any, Iterable

from loguru import logger

from src.config import load_settings
from src.models import (
    NPC,
    Campaign,
    DMState,
    Event,
    Faction,
    FactionRelationship,
    Location,
    NPCRelationship,
    Player,
    Quest,
    QuestStatus,
    StoryBeat,
    WorldClock,
    get_session,
)
from src.models.campaign import BEAT_TYPES, CINEMATIC_LABELS, TRIGGER_TYPES, TRUTH_KINDS

# --- Code-owned numbers. The generator never writes one of these. -----------

DELAY_HOURS = {"immediate": 0, "soon": 6, "days": 48, "week": 168, "fortnight": 336}
DEADLINE_DAYS = {"short": 2, "medium": 5, "long": 9}
FALLBACK_DAYS = 7                # backstop for non-time triggers
TIME_FALLBACK_DAYS = 3           # extra slack past a `time` beat's schedule
CAMPAIGN_BEAT_SPACING_HOURS = 2  # min in-game gap between non-deadline beats
BEAT_DELIVERY_ATTEMPTS = 3       # mirrors Event delivery re-surfacing
# A beat the DM failed to narrate three times is parked as `stalled`: it stops
# blocking the story but nothing is claimed told — its lead goes on the
# player's tracker instead, and it is delivered when its keywords or its
# met_condition finally show up. Past this many in-game days it is skipped
# (skipped un-seals nothing) so an act can still close.
STALLED_SKIP_DAYS = 3
FACTION_POWER_SHIFT = {"weakened": -15, "strengthened": +15, "shattered": -40, "ascendant": +30}
# (player_level_at_least, completed_quests_at_least) — either arm carries it
MILESTONE_THRESHOLDS = {"early": (2, 1), "midway": (4, 3), "late": (6, 5)}
BEAT_PRIORITY = {"deadline_event": 0, "twist": 1, "confrontation": 1, "revelation": 2, "setpiece": 3}

ACTS_TOTAL = 3
MIN_BEATS = 6    # a campaign with fewer surviving beats is a failed generation
MAX_BEATS = 20

FACTION_POWER_MIN = 1
FACTION_POWER_MAX = 100

# Clock urgency bands, in hours until the deadline fires
URGENCY_IMMINENT_HOURS = 24
URGENCY_SOON_HOURS = 72

# --- Fixed label menus (validated at persist; unknown labels are dropped) ---

OP_NAMES = (
    "move_npc", "npc_status", "npc_mood", "activate_quest", "fail_quest",
    "complete_quest", "give_item", "faction_power", "faction_relationship",
    "world_event", "tension_floor",
)
NPC_STATUSES = ("alive", "dead", "missing", "imprisoned")
RELATIONSHIP_TYPES = ("allied", "neutral", "rival", "war", "vassal")
EVENT_SCOPES = ("macro", "meso", "player")
TENSION_LEVELS = ("rising", "high", "climax")
MET_CHECKS = ("quest_status", "truth_revealed", "npc_status", "player_visited", "item_held")
QUEST_STATUSES = ("active", "completed", "failed")

# Per-turn caches the web layer reads after `_build_context` has run.
_LAST_TICK: dict[str, dict[str, Any]] = {}
_PENDING_EVENTS: dict[str, list[dict[str, Any]]] = {}
# What the next briefing tells the DM about the last turn, per player: the
# beats a failed roll held back (`mark_beat_delivery`) and the scheduled
# events a delivered beat already told (`_deliver_beat`). In memory: a
# restart only costs a note.
_ROLL_HELD: dict[str, set[str]] = {}
_CONTINUITY: dict[str, list[str]] = {}
# Players whose next tick is an engine-owned reply (an ambush): it fires and
# re-sends no beat (`hold_beats_this_reply`, `run_campaign_tick`)
_HOLD_BEATS: set[str] = set()
# Beats a find of the player's brought due, per player: {beat id: the
# people stepping on} (round 9, `_due_by_find`) — read by the spacing rule
# and the beat's notes; the beat's own backstop carries it past a restart
_FOUND_DUE: dict[str, dict[str, list[str]]] = {}


# ----------------------------------------------------------------------
# Config (read defensively — the campaign block may predate this build)
# ----------------------------------------------------------------------

def _campaign_setting(name: str, default: Any) -> Any:
    """Read one `campaign.*` setting, falling back when it isn't configured."""
    try:
        block = getattr(load_settings(), "campaign", None)
    except Exception:  # pragma: no cover - settings.yaml unreadable
        return default
    if block is None:
        return default
    value = getattr(block, name, None)
    return default if value is None else value


def campaign_enabled() -> bool:
    return bool(_campaign_setting("enabled", True))


def _auto_generate_enabled() -> bool:
    return bool(_campaign_setting("auto_generate_on_first_turn", True))


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")


def _get(session, model, ident):
    """`session.get` with a None-safe guard — unresolvable ids are routine."""
    if not ident:
        return None
    try:
        return session.get(model, ident)
    except Exception:  # pragma: no cover - malformed id type
        return None


def _now(session) -> tuple[int, int]:
    """(day, hour) from the world clock. A clockless world starts at Day 1."""
    clock = session.query(WorldClock).first()
    if not clock:
        return (1, 8)
    return (clock.day, clock.hour)


def _add_hours(when: tuple[int, int], hours: int) -> tuple[int, int]:
    total = when[1] + int(hours)
    return (when[0] + total // 24, total % 24)


def _hours_between(start: tuple[int, int], end: tuple[int, int]) -> int:
    """Signed in-game hours from `start` to `end`."""
    return (end[0] - start[0]) * 24 + (end[1] - start[1])


def _stamp(row, prefix: str) -> tuple[int, int] | None:
    """Read a `<prefix>_day`/`<prefix>_hour` pair off a beat or campaign row."""
    day = getattr(row, f"{prefix}_day", None)
    if day is None:
        return None
    return (day, getattr(row, f"{prefix}_hour", None) or 0)


def get_campaign_row(session, player_id: str | None) -> Campaign | None:
    """The (most recent) campaign for this character, or None."""
    if not player_id:
        return None
    return (
        session.query(Campaign)
        .filter(Campaign.player_id == player_id)
        .order_by(Campaign.created_at.desc())
        .first()
    )


def _act_entry(campaign: Campaign, act: int) -> dict[str, Any]:
    """The {act,title,premise} record for an act, defaulted."""
    for entry in (campaign.acts or []):
        if isinstance(entry, dict) and int(entry.get("act", 0) or 0) == act:
            return entry
    return {"act": act, "title": f"Act {act}", "premise": ""}


def _campaign_beats(session, campaign: Campaign) -> list[StoryBeat]:
    return (
        session.query(StoryBeat)
        .filter(StoryBeat.campaign_id == campaign.id)
        .order_by(StoryBeat.act, StoryBeat.sequence)
        .all()
    )


def reset_state() -> None:
    """Drop the per-player tick caches (tests, world switches)."""
    _LAST_TICK.clear()
    _PENDING_EVENTS.clear()
    _ROLL_HELD.clear()
    _CONTINUITY.clear()
    _HOLD_BEATS.clear()
    _FOUND_DUE.clear()
    _SEAL_CUTS.clear()
    _SEAL_SEEN.clear()
    _SEAL_FORMS.clear()
    _SEAL_COLD.clear()


def hold_beats_this_reply(player_id: str | None) -> None:
    """The next tick for this player is an engine-owned reply (an ambush's
    one sentence): it fires and re-sends no beat. Read once, by that tick."""
    if player_id:
        _HOLD_BEATS.add(player_id)


def get_last_tick(player_id: str) -> dict[str, Any]:
    """The payload from this player's most recent `run_campaign_tick`."""
    return _LAST_TICK.get(player_id) or {}


def pop_pending_events(player_id: str) -> list[dict[str, Any]]:
    """Drain queued campaign SSE payloads (beat / act / resolved)."""
    return _PENDING_EVENTS.pop(player_id, [])


def _queue_events(player_id: str, events: list[dict[str, Any]]) -> None:
    if events:
        _PENDING_EVENTS.setdefault(player_id, []).extend(events)


# ----------------------------------------------------------------------
# Cinematics hand-off (integration doc R9)
# ----------------------------------------------------------------------

def _cinematic_request(label: str | None, campaign: Campaign, act: int,
                       clock_label: str | None, variant: str | None) -> tuple | None:
    """Turn a campaign cinematic LABEL into a deferred `request_cinematic` call.

    The label -> kind map lives in `src/services/cinematics.py`; we only ever
    emit labels and the strings that go on the card. Returns the argument
    tuple (dispatched AFTER the tick's transaction commits, so the cinematics
    ledger never contends with our write lock), or None when the label renders
    nothing (revelation/betrayal/arrival are carried by the toast + prose).
    """
    if not label or label == "none":
        return None
    entry = _act_entry(campaign, act)
    if label == "countdown_expired":
        title = clock_label or entry.get("title") or campaign.title
        subtitle = "Too late"
    elif label == "finale":
        title = campaign.title or entry.get("title") or "Finale"
        subtitle = entry.get("premise") or ""
    else:  # act_open
        title = f"Act {act}" if not entry.get("title") else entry["title"]
        subtitle = entry.get("premise") or ""
    if variant == "missed" and label != "countdown_expired":
        subtitle = subtitle or "Too late"
    return (label, title, subtitle)


def _dispatch_cinematics(player_id: str, requests: list[tuple]) -> None:
    """Queue the tick's cinematics. Import is at call time on purpose — the
    cinematics module is a sibling system and campaign play must survive it
    being absent."""
    if not requests:
        return
    try:
        from src.services.cinematics import CAMPAIGN_CINEMATIC_MAP, request_cinematic
    except Exception as e:  # pragma: no cover - cinematics not installed
        logger.debug(f"campaign: cinematics unavailable ({e}) — labels dropped")
        return
    for label, title, subtitle in requests:
        kind = CAMPAIGN_CINEMATIC_MAP.get(label)
        if not kind:
            continue
        try:
            request_cinematic(
                kind,
                player_id=player_id,
                title=title,
                subtitle=subtitle,
                source="campaign",
                defer=True,
            )
        except Exception as e:  # pragma: no cover - never break a turn for a card
            logger.warning(f"campaign: cinematic request failed ({label}): {e}")


# ----------------------------------------------------------------------
# Arming
# ----------------------------------------------------------------------

def _arm_beat(beat: StoryBeat, now: tuple[int, int]) -> None:
    """Resolve a beat's absolute fire time and its hard backstop.

    Every armed beat gets a fallback: if the world never satisfies the
    trigger, the beat fires anyway. The story always moves.
    """
    beat.status = "armed"
    beat.armed_day, beat.armed_hour = now
    data = beat.trigger_data if isinstance(beat.trigger_data, dict) else {}

    if beat.beat_type == "deadline_event":
        days = DEADLINE_DAYS.get(str(data.get("deadline")), DEADLINE_DAYS["medium"])
        scheduled = (now[0] + days, now[1])
        beat.scheduled_day, beat.scheduled_hour = scheduled
        beat.fallback_day, beat.fallback_hour = scheduled  # a deadline never slips
        return

    if beat.trigger_type in ("time", "after_beat"):
        delay = DELAY_HOURS.get(str(data.get("delay")), DELAY_HOURS["days"])
        scheduled = _add_hours(now, delay)
        beat.scheduled_day, beat.scheduled_hour = scheduled
        beat.fallback_day, beat.fallback_hour = (scheduled[0] + TIME_FALLBACK_DAYS, scheduled[1])
        return

    beat.fallback_day, beat.fallback_hour = (now[0] + FALLBACK_DAYS, now[1])


def arm_act(session, campaign: Campaign, act: int) -> list[StoryBeat]:
    """Arm every pending beat of `act` and rewrite the DM's arc line.

    `after_beat` beats stay pending — they arm when their prerequisite is
    actually delivered (`run_campaign_tick` step 3).
    """
    now = _now(session)
    armed: list[StoryBeat] = []
    beats = (
        session.query(StoryBeat)
        .filter(
            StoryBeat.campaign_id == campaign.id,
            StoryBeat.act == act,
            StoryBeat.status == "pending",
        )
        .order_by(StoryBeat.sequence)
        .all()
    )
    for beat in beats:
        if beat.trigger_type == "after_beat":
            continue
        _arm_beat(beat, now)
        armed.append(beat)

    _write_arc(session, campaign, act, armed)
    _hold_true_triggers(session, campaign, act)
    return armed


def _hold_true_triggers(session, campaign: Campaign, act: int) -> None:
    """Snapshot the later acts' play triggers that already hold as `act`
    arms (`Campaign.pull_held`): the story pulls ahead only on what play
    makes true from here on (see `_pull_ahead`)."""
    player = _get(session, Player, campaign.player_id)
    if player is None:
        return
    later = (
        session.query(StoryBeat)
        .filter(StoryBeat.campaign_id == campaign.id, StoryBeat.act > act, StoryBeat.status == "pending",
                StoryBeat.trigger_type.in_(_PLAY_TRIGGERS))
        .all()
    )
    campaign.pull_held = [b.id for b in later if _play_trigger_met(session, b, player)]


def _write_arc(session, campaign: Campaign, act: int, armed: list[StoryBeat]) -> None:
    """`DMState.current_arc` is code-owned while a campaign is running; live
    clock labels join `world_pressures` so NPCs can talk about them."""
    dm_state = session.query(DMState).first()
    if not dm_state:
        return
    entry = _act_entry(campaign, act)
    dm_state.current_arc = (
        f"[CAMPAIGN] {campaign.title} — Act {act}: "
        f"{entry.get('title') or ''}. {entry.get('premise') or ''}"
    ).strip()

    labels = [b.clock_label for b in armed if b.beat_type == "deadline_event" and b.clock_label]
    if labels:
        pressures = list(dm_state.world_pressures or [])
        for label in labels:
            if label not in pressures:
                pressures.append(label)
        dm_state.world_pressures = pressures


# ----------------------------------------------------------------------
# Conditions and consequences
# ----------------------------------------------------------------------

def evaluate_condition(session, condition: dict | None, campaign: Campaign, player) -> bool:
    """Did the player make the deadline? Pure code, no judgement calls.

    An unknown check or an unresolvable id reads as NOT met — a deadline you
    cannot verify is a deadline the player did not beat.
    """
    if not isinstance(condition, dict):
        return False
    check = condition.get("check")

    if check == "quest_status":
        quest = _get(session, Quest, condition.get("quest_id"))
        return bool(quest and quest.status == condition.get("status"))

    if check == "truth_revealed":
        wanted = condition.get("truth_id")
        for truth in (campaign.fixed_truths or []):
            if isinstance(truth, dict) and truth.get("id") == wanted:
                return bool(truth.get("revealed"))
        return False

    if check == "npc_status":
        npc = _get(session, NPC, condition.get("npc_id"))
        return bool(npc and npc.status == condition.get("status"))

    if check == "player_visited":
        location = _get(session, Location, condition.get("location_id"))
        return bool(location and location.visited)

    if check == "item_held":
        return _holds(player, condition.get("item_name"))

    return False


def _holds(player, item_name: Any, *, exact: bool = False) -> bool:
    """Does the player carry this item? A substring by default (a met
    condition's "letter" is held as "Sealed Letter"); `exact` when the answer
    retires a beat unfired, where a beat's "Letter" must not be satisfied by
    whatever other letter the player happens to carry."""
    wanted = str(item_name or "").strip().lower()
    if not wanted:
        return False
    for entry in (getattr(player, "inventory", None) or []):
        name = str((entry.get("name") if isinstance(entry, dict) else entry) or "").strip().lower()
        if name == wanted or (not exact and wanted in name):
            return True
    return False


def _held_in_world(session, item_name: Any) -> str | None:
    """What already holds this item in the world — an authored scene object's
    label, or a living person's name — or None. Round 4 (fleet5-bram): a
    beat's give_item put the Cracked Bone Whistle in the pack as the beat
    fired, while the Hollow's whistle object still held it and the directive
    said "let him find or take it": a find the player never made, a second
    whistle on the ground, and the DM telling him he was carrying it."""
    wanted = _slug(str(item_name or ""))
    if not wanted:
        return None
    for location in session.query(Location).all():
        for obj in location.objects or []:
            if not isinstance(obj, dict) or obj.get("consumed"):
                continue
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            held = [payload.get("item"), *(payload.get("items") or [])]
            if any(isinstance(i, dict) and _slug(str(i.get("name") or "")) == wanted for i in held):
                return str(obj.get("label") or "a thing in the world")
    for npc in session.query(NPC).filter(NPC.status == "alive").all():
        for entry in npc.inventory_notable or []:
            name = entry.get("name") if isinstance(entry, dict) else entry
            if _slug(str(name or "")) == wanted:
                return npc.name
    return None


def execute_ops(session, ops: list | None, campaign: Campaign, player,
                *, events: list | None = None,
                deferred: list | None = None) -> tuple[list[str], set[str]]:
    """Apply a beat's consequences. The ONLY code that mutates the world for a beat.

    Runs inside the caller's transaction (the caller commits). Returns
    `(applied, audit_exempt)`: plain-language lines shown to the DM as ALREADY
    APPLIED, and the audit categories whose mutation the engine — not the DM —
    performed this turn.

    An op whose entity id doesn't resolve is dropped with a warning; the beat
    still fires. `tension_floor` is appended to `deferred` instead of applied
    inline (it opens its own session via `apply_tension_floor`, and a nested
    write would contend with our open transaction); when no list is passed it
    is applied immediately, best effort.
    """
    applied: list[str] = []
    exempt: set[str] = set()
    if not ops:
        return applied, exempt
    now = _now(session)

    for op in ops:
        if not isinstance(op, dict):
            continue
        name = op.get("op")
        if name not in OP_NAMES:
            logger.warning(f"campaign op dropped: unknown op {name!r}")
            continue

        if name == "move_npc":
            npc = _get(session, NPC, op.get("npc_id"))
            if not npc:
                logger.warning(f"campaign op dropped: move_npc unknown npc {op.get('npc_id')!r}")
                continue
            target = op.get("to")
            location_id = getattr(player, "current_location_id", None) if target == "player" else target
            location = _get(session, Location, location_id)
            if not location:
                logger.warning(f"campaign op dropped: move_npc unknown location {target!r}")
                continue
            npc.current_location_id = location.id
            applied.append(f"{npc.name} arrived at {location.name}")
            # A companion the story pulls elsewhere leaves the party (the
            # player's own location is where the party IS, so that is fine)
            try:
                from src.services import party as party_mod
                if (npc.id in party_mod.member_ids(player)
                        and location.id != getattr(player, "current_location_id", None)):
                    party_mod.leave(session, player, npc, reason="the story took them elsewhere")
                    applied.append(f"{npc.name} left the player's party")
            except Exception as e:
                logger.warning(f"party update on campaign move skipped: {e}")

        elif name == "npc_status":
            npc = _get(session, NPC, op.get("npc_id"))
            status = op.get("status")
            if not npc or status not in NPC_STATUSES:
                logger.warning(f"campaign op dropped: npc_status {op.get('npc_id')!r}/{status!r}")
                continue
            npc.status = status
            if status == "dead":
                session.add(Event(
                    name=f"The death of {npc.name}",
                    description=f"{npc.name} is dead.",
                    event_type="meso",
                    occurred_day=now[0], occurred_hour=now[1],
                    npcs_involved=[npc.id],
                    player_visible=True, narrated_to_player=True,
                ))
                applied.append(f"{npc.name} is dead")
                # A beat-driven death still ticks a "kill" objective — inside
                # this transaction, published as soon as the caller commits
                try:
                    from src.services import objectives as objectives_mod
                    ticked = objectives_mod.tick_in_session(
                        session, player, objectives_mod.deaths_matcher([npc.id])
                    )
                    if ticked:
                        applied.extend(objectives_mod.done_lines(ticked))
                        objectives_mod.publish(getattr(player, "id", None), ticked)
                except Exception as e:
                    logger.warning(f"objective tick on campaign death skipped: {e}")
            else:
                applied.append(f"{npc.name} is now {status}")
            exempt.add("consequence")

        elif name == "npc_mood":
            npc = _get(session, NPC, op.get("npc_id"))
            mood = str(op.get("mood") or "").strip()[:50]
            if not npc or not mood:
                logger.warning(f"campaign op dropped: npc_mood {op.get('npc_id')!r}")
                continue
            npc.current_mood = mood
            applied.append(f"{npc.name} is now {mood}")
            exempt.add("consequence")

        elif name == "activate_quest":
            quest = _get(session, Quest, op.get("quest_id"))
            if not quest or quest.status != QuestStatus.NOT_STARTED:
                logger.warning(f"campaign op dropped: activate_quest {op.get('quest_id')!r}")
                continue
            quest.status = QuestStatus.ACTIVE
            _quest_sync(player, quest, QuestStatus.ACTIVE)
            applied.append(f'Quest activated: "{quest.title}"')
            exempt.add("quest")
            _push_event(events, {"type": "quest_activated", "quest_id": quest.id,
                                 "title": quest.title, "rewards_applied": []})

        elif name == "complete_quest":
            quest = _get(session, Quest, op.get("quest_id"))
            if not quest:
                logger.warning(f"campaign op dropped: complete_quest {op.get('quest_id')!r}")
                continue
            from src.tools.world_write.quests import _complete_quest

            outcome = _complete_quest(session, quest, player)
            if outcome.get("already_completed"):
                continue
            rewards = outcome.get("rewards_applied") or []
            line = f'Quest completed: "{quest.title}"'
            if rewards:
                line += f" (rewards applied: {', '.join(rewards)})"
            applied.append(line)
            exempt.update({"quest", "item"})
            _push_event(events, {"type": "quest_completed", "quest_id": quest.id,
                                 "title": quest.title, "rewards_applied": rewards})

        elif name == "fail_quest":
            quest = _get(session, Quest, op.get("quest_id"))
            if not quest or quest.status == QuestStatus.FAILED:
                logger.warning(f"campaign op dropped: fail_quest {op.get('quest_id')!r}")
                continue
            quest.status = QuestStatus.FAILED
            _quest_sync(player, quest, QuestStatus.FAILED)
            applied.append(f'Quest failed: "{quest.title}"')
            exempt.add("quest")
            _push_event(events, {"type": "quest_failed", "quest_id": quest.id,
                                 "title": quest.title, "rewards_applied": []})

        elif name == "give_item":
            item_name = str(op.get("item_name") or "").strip()
            if not item_name or player is None:
                logger.warning("campaign op dropped: give_item without a name")
                continue
            holder = _held_in_world(session, item_name)
            if holder is not None:
                # The player finds it (or is handed it) in play, not by fiat
                logger.info(f"campaign op skipped: give_item {item_name!r} — {holder} holds it")
                continue
            inventory = list(player.inventory or [])
            inventory.append({
                "id": _slug(item_name) or str(uuid.uuid4()),
                "name": item_name,
                "type": "misc",
                "description": str(op.get("description") or ""),
                "quantity": 1,
            })
            player.inventory = inventory
            applied.append(f"The player received a {item_name}")
            exempt.add("item")

        elif name == "faction_power":
            faction = _get(session, Faction, op.get("faction_id"))
            shift = op.get("shift")
            if not faction or shift not in FACTION_POWER_SHIFT:
                logger.warning(f"campaign op dropped: faction_power {op.get('faction_id')!r}/{shift!r}")
                continue
            faction.power_level = max(
                FACTION_POWER_MIN,
                min(FACTION_POWER_MAX, (faction.power_level or 50) + FACTION_POWER_SHIFT[shift]),
            )
            applied.append(f"{faction.name} are {shift}")
            exempt.add("consequence")

        elif name == "faction_relationship":
            a = _get(session, Faction, op.get("faction_a_id"))
            b = _get(session, Faction, op.get("faction_b_id"))
            kind = op.get("type")
            if not a or not b or kind not in RELATIONSHIP_TYPES:
                logger.warning(f"campaign op dropped: faction_relationship {kind!r}")
                continue
            link = (
                session.query(FactionRelationship)
                .filter(FactionRelationship.faction_a_id.in_([a.id, b.id]),
                        FactionRelationship.faction_b_id.in_([a.id, b.id]))
                .first()
            )
            if link:
                link.relationship_type = kind
            else:
                session.add(FactionRelationship(
                    faction_a_id=a.id, faction_b_id=b.id, relationship_type=kind,
                ))
            applied.append(f"{a.name} and {b.name} are {kind}")
            exempt.add("consequence")

        elif name == "world_event":
            event_name = str(op.get("name") or "").strip()
            if not event_name:
                logger.warning("campaign op dropped: world_event without a name")
                continue
            scope = op.get("scope") if op.get("scope") in EVENT_SCOPES else "meso"
            consequences = op.get("consequences")
            session.add(Event(
                name=event_name,
                description=str(op.get("description") or event_name),
                event_type=scope,
                occurred_day=now[0], occurred_hour=now[1],
                consequences=consequences if isinstance(consequences, list) else [],
                # The beat directive IS this event's narration
                player_visible=True, narrated_to_player=True,
            ))
            applied.append(f'World event recorded: "{event_name}"')
            exempt.add("consequence")

        elif name == "tension_floor":
            level = op.get("level")
            if level not in TENSION_LEVELS:
                logger.warning(f"campaign op dropped: tension_floor {level!r}")
                continue
            if deferred is None:
                _apply_tension(level)
            else:
                deferred.append(level)
            # no `applied` line — invisible plumbing, not a story fact

    return applied, exempt


def _push_event(events: list | None, payload: dict) -> None:
    if events is not None:
        events.append(payload)


def _quest_sync(player, quest, status: str) -> None:
    """Keep `Player.active_quests` / `completed_quests` honest (same helper the
    quest tools use — never a second implementation)."""
    if player is None:
        return
    from src.tools.world_write.quests import _sync_player_quest_lists

    _sync_player_quest_lists(player, quest.id, status)


def _apply_tension(level: str) -> None:
    from src.services.tension import apply_tension_floor

    apply_tension_floor(level, "campaign beat")


def _op_state(session, op: dict, player) -> str | None:
    """Has the world already made this op's change? "done" / "pending", or
    None for an op that proves nothing either way (a mood, the tension
    floor, a no-op status, an id that would be dropped at fire time)."""
    name = op.get("op")
    if name in ("activate_quest", "complete_quest", "fail_quest"):
        quest = _get(session, Quest, op.get("quest_id"))
        if quest is None:
            return None
        if name == "activate_quest":
            return "done" if quest.status != QuestStatus.NOT_STARTED else "pending"
        wanted = QuestStatus.COMPLETED if name == "complete_quest" else QuestStatus.FAILED
        return "done" if quest.status == wanted else "pending"
    if name == "give_item":
        return "done" if _holds(player, op.get("item_name"), exact=True) else "pending"
    if name == "npc_status":
        npc = _get(session, NPC, op.get("npc_id"))
        if npc is None:
            return None
        if npc.status == op.get("status"):
            return None if npc.status == "alive" else "done"
        return "pending"
    if name == "move_npc":
        npc = _get(session, NPC, op.get("npc_id"))
        target = op.get("to")
        location_id = getattr(player, "current_location_id", None) if target == "player" else target
        if npc is None or not location_id:
            return None
        return None if npc.current_location_id == location_id else "pending"
    if name in ("npc_mood", "tension_floor"):
        return None
    return "pending"    # world_event, faction shifts: news the world has not had


def _ops_already_satisfied(session, beat: StoryBeat, player) -> bool:
    """Would firing this beat change nothing the world has not already shown?

    Playtest 2026-10-04: "A Captain Who Needs Hands" only activated the quest
    the opening had already started, fired anyway, and was nagged as
    undelivered — the DM re-pitched a job the player already held. A beat
    like that was told the moment its quest started. Deadline beats (the
    clock itself is the story) and beats that un-seal a truth (something is
    still to be said) never qualify.
    """
    if beat.beat_type == "deadline_event" or beat.reveals_truth_ids:
        return False
    states = [_op_state(session, op, player) for op in (beat.consequence_met or []) if isinstance(op, dict)]
    # ...nor one whose scene is still to come (fleet8-wenna T3, `_scene_owed`)
    return "done" in states and "pending" not in states and not _scene_owed(session, beat, player)


# ----------------------------------------------------------------------
# The tick
# ----------------------------------------------------------------------

def run_campaign_tick(player_id: str, *, constrained: bool = False) -> dict[str, Any]:
    """Advance the campaign one turn. Pure code — no LLM, no network.

    Called from `DMOrchestrator._build_context` right after `run_world_tick()`
    so CLI and web both get it. At most ONE beat fires per turn; an undelivered
    beat blocks new ones until it lands or the engine gives up on it.

    `constrained`: the engine owns this reply (an ambush's one sentence; the
    orchestrator says so with `hold_beats_this_reply`). The bookkeeping
    runs, but no beat fires or is re-sent: fleet8-bram T4's "Tally Marks at
    the Hollow" fired on the turn the arrival ambush took, and the
    one-sentence reply was nagged for it — it lands next reply.

    Returns the briefing payload (also cached for the web layer via
    `get_last_tick` / `pop_pending_events`), or `{}` when this character has no
    live campaign — a world without one behaves exactly as it does today.
    """
    constrained = constrained or player_id in _HOLD_BEATS
    _HOLD_BEATS.discard(player_id)
    if not player_id or not campaign_enabled():
        return {}

    events: list[dict[str, Any]] = []
    cinematics: list[tuple] = []
    tension: list[str] = []
    # What the player read or found (`_read_tells`), before our own session
    reads = _journal_reads(player_id)
    # ...and everything they heard (the sealed thing-words they have met,
    # `_unmet_sealed_words`): memory is read on its own connection
    heard = _heard_before(player_id)

    with get_session() as session:
        campaign = get_campaign_row(session, player_id)
        if campaign is None:
            if _auto_generate_enabled():
                _spawn_generation(player_id)
            return {}
        if campaign.status != "active":
            # A failed (or died-mid-call) generation is retried off-thread
            # once its backoff runs out — this turn plays without a spine
            if _auto_generate_enabled() and _retry_due(campaign):
                _spawn_generation(player_id)
            return {}
        player = session.get(Player, player_id)
        if player is None:
            return {}

        now = _now(session)
        beats = _campaign_beats(session, campaign)

        # 2. The villain is a load-bearing wall — check it is still standing
        note = _check_villain(session, campaign, beats, now, events)

        # 2b. A beat the player already lived through is never fired — by
        # its ops, or by what play already showed (an act just armed must
        # not fire a reveal the player had on day one)
        _retire_satisfied(session, campaign, beats, player, now, events, cinematics)
        _deliver_by_play(session, campaign, beats, player, now, events, cinematics,
                         reasons=("truths", "met", "quest", "read"), reads=reads)

        # 3. Arm anything waiting on a beat that has now been delivered
        _arm_after_beats(campaign, beats, now)

        # 3b. A beat stalled for too long is skipped, so the act can close —
        # as is one that waited at its place long past its backstop
        _skip_stale_stalled(session, campaign, beats, now, events, cinematics)
        _skip_waiting_past_backstop(session, campaign, beats, player, now, events, cinematics)

        # 3c. Only clocks hold this act and play already met the next act's
        # trigger: the story pulls ahead (and what the new act armed that
        # play already lived through is told, never fired)
        if _pull_ahead(session, campaign, beats, player, events, cinematics):
            _retire_satisfied(session, campaign, beats, player, now, events, cinematics)
            _deliver_by_play(session, campaign, beats, player, now, events, cinematics,
                             reasons=("truths", "met", "quest", "read"), reads=reads)
            _arm_after_beats(campaign, beats, now)

        # 3d. An earlier act's beat a later act already played past is
        # skipped; a clock beat whose scheduled twin already happened is due
        # now, as its aftermath (round 6, fleet7-wenna); one whose quest cue
        # can never come true comes due as what followed (round 7)
        _retire_superseded(session, campaign, beats)
        _bring_due_after_twins(session, beats, now)
        _wake_dead_cues(session, beats, now)

        # 4. A beat the DM never narrated blocks the whole story
        fired = None
        if not constrained:
            fired = _resurface_undelivered(session, campaign, beats, now, events, cinematics, player=player)

        # 4b. A beat that stalled elsewhere fires again where it happens
        if fired is None and not constrained:
            fired = _refire_stalled_at_place(session, campaign, beats, player, now)

        # 5-8. Otherwise: pick at most one eligible beat and fire it
        if fired is None and not constrained:
            fired = _fire_next(session, campaign, beats, player, now, events, cinematics, tension)

        if fired and note:
            fired["directive"] = f"{note}\n{fired['directive']}".strip()
        if fired:
            # Re-computed on every attempt: the player moves between them
            row = next((b for b in beats if b.id == fired["beat_id"]), None)
            if row is not None:
                fired["notes"] = _directive_notes(session, row, player)

        result = _build_briefing(session, campaign, beats, now, fired, events, note, heard=heard)
        session.commit()

    _dispatch_cinematics(player_id, cinematics)
    for level in tension:
        _apply_tension(level)

    _LAST_TICK[player_id] = result
    _queue_events(player_id, events)
    return result


def _check_villain(session, campaign: Campaign, beats: list[StoryBeat],
                   now: tuple[int, int], events: list) -> str | None:
    """A dead villain must not stall the story.

    Successor alive → the mantle passes. No successor → the player decapitated
    the story early; honour it by jumping to act 3 and forcing the finale.
    Returns a one-turn directive prefix, or None.
    """
    villain = _get(session, NPC, campaign.villain_npc_id)
    if not villain or villain.status != "dead":
        return None

    successor = _get(session, NPC, campaign.successor_npc_id)
    old_name = campaign.villain_name or villain.name

    if successor and successor.status == "alive":
        campaign.villain_npc_id = successor.id
        campaign.villain_name = successor.name
        campaign.successor_npc_id = None
        campaign.successor_name = ""
        session.add(Event(
            name="The mantle passes",
            description=f"The mantle passes: {successor.name} takes up {old_name}'s work",
            event_type="macro",
            occurred_day=now[0], occurred_hour=now[1],
            npcs_involved=[successor.id],
            player_visible=True, narrated_to_player=True,
        ))
        logger.info(f"campaign: villain {old_name} is dead — {successor.name} steps in")
        return f"{old_name} is dead. {successor.name} steps into the gap — the story does not stop."

    # No successor: skip everything before act 3 and force the finale
    for beat in beats:
        if beat.act < ACTS_TOTAL and beat.status in ("pending", "armed"):
            beat.status = "skipped"
    campaign.current_act = ACTS_TOTAL
    arm_act(session, campaign, ACTS_TOTAL)

    finale = _finale_beat(beats)
    for beat in beats:
        if beat.act == ACTS_TOTAL and beat.status in ("pending", "armed") and beat is not finale:
            beat.status = "skipped"
    if finale and finale.status in ("pending", "armed"):
        if finale.status == "pending":
            _arm_beat(finale, now)
        finale.fallback_day, finale.fallback_hour = now  # eligible immediately
    logger.info(f"campaign: villain {old_name} is dead with no successor — forcing the finale")
    return f"{old_name} is dead and no one takes their place. The story reaches its end."


def _finale_beat(beats: list[StoryBeat]) -> StoryBeat | None:
    """The last act-3 beat — the one labelled `finale`, else the last sequence."""
    act3 = [b for b in beats if b.act == ACTS_TOTAL]
    if not act3:
        return None
    for beat in act3:
        if beat.cinematic == "finale":
            return beat
    return sorted(act3, key=lambda b: b.sequence)[-1]


def _retire_satisfied(session, campaign: Campaign, beats: list[StoryBeat], player,
                      now: tuple[int, int], events: list, cinematics: list) -> None:
    """Deliver, without firing or re-surfacing, every beat the player already
    lived through (`_ops_already_satisfied`) — so it is never directed, never
    nagged, and whatever waits on it arms as usual.

    Armed beats are checked as they stand. A fired or stalled beat qualifies
    only if firing applied nothing (every op was dropped as already true, as
    in worlds that fired such a beat before this check existed) — a beat
    whose own ops made the world match still has its scene to deliver.
    """
    for beat in beats:
        if beat.status == "armed" or (beat.status in ("fired", "stalled") and not beat.applied_effects):
            if _ops_already_satisfied(session, beat, player):
                logger.info(f"campaign beat already lived through, delivered without firing: {beat.title}")
                beat.variant = beat.variant or "met"
                _deliver_beat(session, campaign, beat, now, events, cinematics)


def _arm_after_beats(campaign: Campaign, beats: list[StoryBeat], now: tuple[int, int]) -> None:
    """Arm `after_beat` beats whose prerequisite has now been delivered.

    An act only ends when all of its beats are delivered or skipped, so a
    prerequisite that can never land (skipped, missing, or authored into a
    LATER act) would stall the campaign permanently. Those arm immediately —
    a beat out of order beats a story that stops.

    A STALLED prerequisite counts as done too. Round 4, fleet5-tamsin: "A
    Name on Minted Silver" stalled in the square, and "The Whistle in Her
    Hand" — the beat that puts the act's proof in her hand — sat pending
    behind it until the stall would be skipped days later. Beats of an act
    the story pulled ahead of (`_pull_ahead`) still arm on their schedule.
    """
    by_id = {b.id: b for b in beats}
    for beat in beats:
        if beat.status != "pending" or beat.trigger_type != "after_beat":
            continue
        if beat.act > (campaign.current_act or 1):
            continue
        data = beat.trigger_data if isinstance(beat.trigger_data, dict) else {}
        prerequisite = by_id.get(data.get("beat_id"))
        if prerequisite is None or prerequisite.act > beat.act \
                or prerequisite.status in ("delivered", "skipped", "stalled"):
            _arm_beat(beat, now)


# --- Act pacing --------------------------------------------------------------
# Round 4, fleet5-bram: Bram finished the act-1 job on day 1, and "A Name on
# the Whistle" — act 2, triggered by that very completion — stayed pending
# because act 1 still held clock beats for day 2 and day 6. Coldiron, who had
# promised "come back with something I can use, I'll tell you what I know",
# stonewalled three turns. A spine paced in days met a player who moves in
# turns. So when everything the current act still holds is a clock (a time
# or after_beat schedule, a deadline — they fire on their own schedule in any
# act) or parked (stalled), and a next-act beat's PLAY trigger already holds
# (a quest status, a place, a milestone), the act advances now. The leftover
# clocks keep their schedules and their consequences. The finale act is never
# pulled ahead: the villain stays out of fights until act 3 earns it.
#
# Round 5, fleet6-wenna: act 2 opened on turn 3. Its "The Wolf's Hollow
# Whistle" triggers on "Teeth on the North Road" being active — and the
# [OPENING] itself had started that job at T0, so the trigger was true
# before act 1 had played anything; Sorrel's ash-gray man and Hesk's fence
# were left as day-2 clocks. Two guards: a trigger counts only once play
# made it true AFTER the act armed (`Campaign.pull_held` snapshots what
# already held then, and the opening's own job; a held trigger that goes
# false — the player walks off — and comes true again counts), and the act
# must have delivered a beat of its own: one scene of its premise, measured
# in story rather than turns or days (fleet5-bram's legitimate pull came on
# day 1, a few hours in, after two act-1 beats; wenna's would still have
# passed a turn count of three).
_CLOCK_TRIGGERS = ("time", "after_beat")
_PLAY_TRIGGERS = ("quest_status", "location", "milestone")


def _is_clock(beat: StoryBeat) -> bool:
    return beat.beat_type == "deadline_event" or beat.trigger_type in _CLOCK_TRIGGERS


def _release_held(session, campaign: Campaign, beats: list[StoryBeat], player) -> set[str]:
    """`Campaign.pull_held` minus every place trigger the player has walked
    away from (coming back is play's doing). A quest status or a milestone
    never comes true twice, so those stay held — including the opening's
    job, noted before the DM's reply has started it. Returns what is still
    held; NULL (a campaign from before the snapshot) holds nothing."""
    if campaign.pull_held is None:
        return set()
    by_id = {b.id: b for b in beats}
    held = [i for i in campaign.pull_held
            if i in by_id and by_id[i].status == "pending"
            and (by_id[i].trigger_type != "location" or _play_trigger_met(session, by_id[i], player))]
    if held != list(campaign.pull_held):
        campaign.pull_held = held
    return set(held)


def _pull_ahead(session, campaign: Campaign, beats: list[StoryBeat], player,
                events: list, cinematics: list) -> bool:
    """Advance to the next act when only clocks hold the current one back,
    the act has delivered a beat, and play met a next-act trigger since the
    act armed (see above) — or the player finished their work and has none
    left (`_starved`). True if it did."""
    act = campaign.current_act or 1
    if player is None:
        return False
    held = _release_held(session, campaign, beats, player)
    if act + 1 >= ACTS_TOTAL:
        return False
    left = [b for b in beats if b.act == act and b.status not in ("delivered", "skipped")]
    now = _now(session)
    if not left or any(b.status == "fired" or (b.status != "stalled" and not _is_clock(b)
                                               and not _waiting_at_place(session, b, player, now))
                       for b in left):
        return False
    if not any(b.act == act and b.status == "delivered" for b in beats):
        return False
    ready = [b for b in beats if b.act == act + 1 and b.status == "pending" and b.id not in held
             and b.beat_type != "deadline_event" and b.trigger_type in _PLAY_TRIGGERS
             and _play_trigger_met(session, b, player)]
    if not ready:
        if not _starved(session, player) or not any(b.act == act + 1 for b in beats):
            return False
        logger.info(f"campaign: act {act} holds only clocks and the player's work is done — pulling ahead")
        _open_next_act(session, campaign, events, cinematics)
        return True
    logger.info(f"campaign: act {act} holds only clocks and '{ready[0].title}' is due — pulling ahead")
    _open_next_act(session, campaign, events, cinematics)
    return True


def _starved(session, player) -> bool:
    """Has the player finished the work they had, with none left? Round 7,
    fleet8-bram: both jobs paid by T16 and act 1 holding only a day-6 clock,
    the tracker was empty from T17 and the DM invented a drover, a quarry
    track and a watcher in the loft to fill it. A player who finished their
    work and has none left is play that ran past the act: the next act
    opens (never the finale's — `_pull_ahead`). Not at the start: no job
    finished yet is the opening, not a lull."""
    from src.services.objectives import active_quests_for

    try:
        if active_quests_for(session, player):
            return False
    except Exception as e:  # unreadable: never pull on a guess
        logger.debug(f"active quests unreadable for the pull: {e}")
        return False
    return session.query(Quest.id).filter(Quest.status.in_((QuestStatus.COMPLETED, QuestStatus.FAILED))).first() \
        is not None


def _skip_stale_stalled(session, campaign: Campaign, beats: list[StoryBeat],
                        now: tuple[int, int], events: list, cinematics: list) -> None:
    finale = _finale_beat(beats)
    player = session.get(Player, campaign.player_id)
    here = getattr(player, "current_location_id", None)
    for beat in beats:
        if beat.status != "stalled" or beat.fired_day is None:
            continue
        places = _place_ids(session, beat) if player is not None else set()
        if places:
            there = _within(session, here, places)
            # Back where it happens with its re-fire left: it fires this very
            # tick (`_refire_stalled_at_place`), never skipped at the door. And
            # the ending waits while the player is away from it (parked when
            # they walked off) — skipped, the last act would close as "won"
            # with nobody ever confronted (the one a dead villain forced ends
            # the story anywhere: `_forced_finale`)
            if (there and (beat.delivery_attempts or 0) <= BEAT_DELIVERY_ATTEMPTS) \
                    or (beat is finale and not there and not _forced_finale(session, beat)):
                continue
        if now[0] - beat.fired_day > STALLED_SKIP_DAYS:
            logger.warning(f"campaign beat skipped after stalling: {beat.title}")
            beat.status = "skipped"
            _check_act_transition(session, campaign, beat, now, events, cinematics)


def _waiting_at_place(session, beat: StoryBeat, player, now: tuple[int, int]) -> bool:
    """An armed beat past its backstop that waits at a place the player is
    not at (`_is_eligible`): parked, like a stalled one."""
    fallback = _stamp(beat, "fallback")
    return (beat.status == "armed" and beat.beat_type != "deadline_event" and player is not None
            and fallback is not None and now >= fallback and not _forced_finale(session, beat)
            and not _at_beat_place(session, beat, player))


def _skip_waiting_past_backstop(session, campaign: Campaign, beats: list[StoryBeat], player,
                                now: tuple[int, int], events: list, cinematics: list) -> None:
    """A beat that waited at its place STALLED_SKIP_DAYS past its backstop
    is skipped (it un-seals nothing), as a stalled one is, so its act can
    close without the place ever being brought to the player. Never the
    finale: skipping the last beat closes act 3 as "won" (`_check_act_transition`)
    with the villain never faced — the ending waits where it happens."""
    finale = _finale_beat(beats)
    for beat in beats:
        if beat is finale or not _waiting_at_place(session, beat, player, now):
            continue
        fallback = _stamp(beat, "fallback")
        if now[0] - fallback[0] > STALLED_SKIP_DAYS:
            logger.warning(f"campaign beat skipped: it waited at its place past its backstop: {beat.title}")
            beat.status = "skipped"
            _check_act_transition(session, campaign, beat, now, events, cinematics)


def _resurface_undelivered(session, campaign: Campaign, beats: list[StoryBeat],
                           now: tuple[int, int], events: list,
                           cinematics: list, player=None) -> dict[str, Any] | None:
    """Re-surface a fired-but-unnarrated beat, or park it after 3 tries.

    Mirrors the Event delivery loop: the world already changed, so the beat
    cannot silently vanish — but it also cannot block the story forever. It
    used to be marked delivered anyway, which un-sealed truths the player
    never heard; now it is `stalled` (see STALLED_SKIP_DAYS).

    A beat re-fired where it happens (`_refire_stalled_at_place`) gets a
    second round of attempts, counted on from the first round's. One that
    stalls with the player already standing at its place has had its
    chance there: both rounds are spent, so it is never re-fired.
    """
    for beat in beats:
        if beat.status != "fired":
            continue
        # A scene at a place the player has left waits there instead of
        # being nagged elsewhere (fleet7-tamsin T8-T9: the Hollow's "SING"
        # re-sent in the yard became a prose trek). Parked now, it fires
        # again when they come back (`_refire_stalled_at_place`)
        if player is not None and beat.beat_type != "deadline_event" and not _forced_finale(session, beat) \
                and not _at_beat_place(session, beat, player):
            logger.info(f"campaign beat waits where it happens (the player left): {beat.title}")
            beat.status = "stalled"
            # Walking off is no failed telling: it re-fires each time they
            # come back (the once-only rule is for a beat failed THERE, whose
            # count is past one round)
            beat.delivery_attempts = min(beat.delivery_attempts or 0, BEAT_DELIVERY_ATTEMPTS)
            continue
        attempts = beat.delivery_attempts or 0
        limit = BEAT_DELIVERY_ATTEMPTS * (2 if attempts > BEAT_DELIVERY_ATTEMPTS else 1)
        if attempts >= limit:
            logger.warning(f"campaign beat stalled (never narrated): {beat.title}")
            beat.status = "stalled"
            places = _place_ids(session, beat)
            if player is not None and places and _within(session, getattr(player, "current_location_id", None),
                                                         places):
                beat.delivery_attempts = 2 * BEAT_DELIVERY_ATTEMPTS
            continue
        beat.delivery_attempts = attempts + 1
        return _fired_payload(beat, attempt=_round_attempt(beat.delivery_attempts))
    return None


def _round_attempt(attempts: int) -> int:
    """Which try this is within the beat's current round of attempts."""
    return attempts - BEAT_DELIVERY_ATTEMPTS if attempts > BEAT_DELIVERY_ATTEMPTS else attempts


def _beat_place_ids(beat: StoryBeat) -> set[str]:
    """Where a beat happens, by its own data: its location lead, its location trigger."""
    ids: set[str] = set()
    target = beat.lead_target if isinstance(beat.lead_target, dict) else {}
    if target.get("type") == "location" and target.get("id"):
        ids.add(str(target["id"]))
    data = beat.trigger_data if isinstance(beat.trigger_data, dict) else {}
    if beat.trigger_type == "location" and data.get("location_id"):
        ids.add(str(data["location_id"]))
    return ids


def _continued_place_ids(session, beat: StoryBeat) -> set[str]:
    """Where an `after_beat` beat with no place of its own happens: the
    places its directive names, when one of them is where the beat it
    follows happens — the same scene, carried on.

    Round 4, fleet5-tamsin: "The Whistle in Her Hand" ("Deliver the find at
    Wolf's Hollow", no lead) follows "A Name on Minted Silver" (lead: the
    Hollow). Armed by its predecessor's stall, it would fire wherever she
    stood — the square again — stall there, and have no place to come back
    at. A directive naming some other place, or none, adds no gate; nor does
    a place named only as where someone heads ("march … against Wolf's
    Hollow" is a scene in town, forge_v2's "Coldiron's Last Roster Call")."""
    if beat.trigger_type != "after_beat":
        return set()
    data = beat.trigger_data if isinstance(beat.trigger_data, dict) else {}
    prerequisite = _get(session, StoryBeat, data.get("beat_id"))
    before = _beat_place_ids(prerequisite) if prerequisite is not None else set()
    if not before:
        return set()
    named = _scene_places(beat.directive or "", session.query(Location).all())
    return named if named & before else set()


# "…at Wolf's Hollow", "near Market Square or the stables", "the heart of
# Wolf's Hollow": where a directive puts its scene, not where someone goes
_AT_PLACE = re.compile(
    r"(?:\b(?:at|in|near|inside|within|around|outside|by|into|reach|reaches|reaching|or)"
    r"|\b(?:heart|edge|mouth)\s+of)\s+(?:the\s+)?$",
    re.IGNORECASE,
)


def _scene_places(directive: str, locations: list[Location], pattern: re.Pattern | None = None) -> set[str]:
    """The ids of the places a directive sets its scene in (see _AT_PLACE;
    `pattern` reads the words before the name another way)."""
    pattern = pattern or _AT_PLACE
    text = directive.replace("’", "'").lower()
    out: set[str] = set()
    for loc in locations:
        name = (loc.name or "").replace("’", "'").lower()
        if len(name) < 4:
            continue
        start = text.find(name)
        while start != -1:
            before = text[max(0, start - 30):start]
            if pattern.search(before) and not _NEWS_REACHES.search(before) \
                    and not _NOT_YET_THERE.search(before):
                out.add(loc.id)
                break
            start = text.find(name, start + 1)
    return out


# Round 6, fleet7-tamsin: "SING" ("When Tamsin reaches Wolf's Hollow per her
# quest, have her find the tally marks…") had a quest_status trigger and no
# location lead, so nothing tied it to the Hollow: it fired in the Stable
# Yard, the beat's nag pushed, and the DM turned the act-1 expedition into a
# two-reply cutscene the database never saw (Wolf's Hollow visited=0). A
# directive that waits on the player ARRIVING somewhere happens there. Over
# every fleet campaign this names eleven beats, each a scene at Wolf's
# Hollow; the other ten carried a location lead too. Word reaching a place is
# news, not an arrival ("have word reach Market Square").
_ARRIVAL_AT = re.compile(
    r"\b(?:reach|reaches|reaching|arriv(?:e|es|ing)\s+(?:at|in)|gets?\s+to|enters?)\s+(?:the\s+)?$",
    re.IGNORECASE,
)
_NEWS_REACHES = re.compile(
    r"\b(?:word|news|rumou?rs?|tidings|it)\s+(?:reach|reaches|reaching)\s+(?:the\s+)?$", re.IGNORECASE,
)
# ...nor is a scene set BEFORE the player gets there ("before Bram reaches
# Wolf's Hollow, have Vex step onto the road"): that one is on the way, and
# held at the Hollow it would wait for an arrival that ends it
_NOT_YET_THERE = re.compile(
    r"\b(?:before|until|till|ere|never)\s+(?:[\w'-]+\s+){0,3}?"
    r"(?:reach|reaches|reaching|arriv(?:e|es|ing)\s+(?:at|in)|gets?\s+to|enters?)\s+(?:the\s+)?$",
    re.IGNORECASE,
)


def _arrival_place_ids(session, beat: StoryBeat) -> set[str]:
    """The places a beat's directive waits for the player to arrive at."""
    directive = beat.directive or ""
    if not re.search(r"\b(?:reach|arriv|gets?\s+to|enters?)", directive, re.IGNORECASE):
        return set()        # the cheap test first: most directives name no arrival
    return _scene_places(directive, session.query(Location).all(), _ARRIVAL_AT)


# Round 9, fleet10-tamsin T2-T6: "SING" came back in new words — "At Wolf's
# Hollow, the player finds the hut with its tally marks…" (a quest_status
# trigger, no lead, no arrival wording) fired in the square, the nag pushed,
# and at T6 the DM told the Hollow as a memory, six replies before she went.
# A directive that OPENS on a place ("At X,", "Inside the hut at X,", "Finale
# at X:") is set there; one whose evidence names the authored Things of one
# place (the tally-marked hut wall and the bone whistle) is about that place.
# Neither binds a beat led to a person: it happens where they are (Coldiron
# "meets Bram at the North Gatehouse" wherever the Captain stands). Replayed
# over every campaign in the fleet databases: see tests/test_spine_round9.py.
_OPENING = re.compile(r"^\s*([^,;:.!?—–(]{1,120})")
# ...but not word sent somewhere ("Have Vane send word through Vex Marrow or
# leave a sign at Wolf's Hollow", pt_ac) nor a person who comes to the player
# ("Have Sorrel Tack corner Rook in Market Square", "Coldiron pulls her aside
# in the Market Square"): that one is wherever the player is. Over the fleet
# campaigns these were six of the nineteen beats the opening would bind
_OPENING_NEWS = re.compile(r"\b(?:send|sends|sent)\s+(?:word|a\s+message|a\s+messenger|news)\b|\bword\s+of\b",
                           re.IGNORECASE)
_COMES_TO_THEM = re.compile(r"\b(?:approach(?:es)?|corners?|pulls?\s+(?:\S+\s+){1,3}?aside|seeks?\s+(?:\S+\s+){1,3}?out"
                            r"|catch(?:es)?\s+up\s+with|comes?\s+to\s+find)\b", re.IGNORECASE)
THING_BIND_MIN = 2          # distinct Things of one place its keywords must name
_THING_KINDS_UNBOUND = ("body", "rest")     # loot and beds are no scene of the story's


def _led_to_person(beat: StoryBeat) -> bool:
    target = beat.lead_target if isinstance(beat.lead_target, dict) else {}
    return target.get("type") == "npc" and bool(target.get("id"))


def _opening_place_ids(session, beat: StoryBeat) -> set[str]:
    """The places a directive's opening clause sets its scene at (see above)."""
    if _led_to_person(beat):
        return set()
    opening = _OPENING.match(beat.directive or "")
    if not opening:
        return set()
    clause = opening.group(1)
    if _OPENING_NEWS.search(clause):
        return set()
    if _COMES_TO_THEM.search(clause) and _named_people(clause, session.query(NPC).all()):
        return set()
    return _scene_places(clause, session.query(Location).all())


def _authored_things(session) -> list[tuple[str, str]]:
    """(label, place id) of every authored Thing in the world — never a body
    or a bed. Read once per session."""
    cache = session.info.setdefault("forge_authored_things", None) if hasattr(session, "info") else None
    if cache is not None:
        return cache
    from src.services.scene_objects import normalize_objects

    found: list[tuple[str, str]] = []
    for location in session.query(Location).all():
        try:
            for obj in normalize_objects(location.objects) if location.objects else []:
                label = str(obj.get("label") or "").strip()
                if label and str(obj.get("kind") or "") not in _THING_KINDS_UNBOUND:
                    found.append((label, location.id))
        except Exception as e:  # one bad row hides its own things only
            logger.debug(f"things at {location.id} unreadable: {e}")
    if hasattr(session, "info"):
        session.info["forge_authored_things"] = found
    return found


def _thing_place_ids(session, beat: StoryBeat) -> set[str]:
    """The one place THING_BIND_MIN or more of whose authored Things the
    beat's keywords name ("bone whistle" → Cracked bone whistle, "hut" →
    Scratches on the hut wall). One Thing alone binds nothing: it can be
    carried off (the whistle goes back to the Captain)."""
    if _led_to_person(beat):
        return set()
    keywords = [str(k) for k in beat.delivery_keywords or [] if str(k).strip()]
    if not keywords:
        return set()
    named: dict[str, set[str]] = {}
    for label, where in _authored_things(session):
        if mentioned_keywords(keywords, label):
            named.setdefault(where, set()).add(label.lower())
    places = {where for where, labels in named.items() if len(labels) >= THING_BIND_MIN}
    return places if len(places) == 1 else set()


def _scene_bound_ids(session, beat: StoryBeat) -> set[str]:
    """Where a beat's own words put it, without a location lead or trigger:
    where it waits for the player to arrive, where its directive opens,
    the scene it carries on, or the place whose Things it is about."""
    return (_arrival_place_ids(session, beat) or _opening_place_ids(session, beat)
            or _continued_place_ids(session, beat) or _thing_place_ids(session, beat))


def _place_ids(session, beat: StoryBeat) -> set[str]:
    """Where a beat happens: its own place (its location lead or trigger),
    else where its words put it (`_scene_bound_ids`)."""
    return _beat_place_ids(beat) or _scene_bound_ids(session, beat)


def _within(session, location_id: str | None, place_ids: set[str]) -> bool:
    """Is `location_id` one of `place_ids`, or somewhere inside one?"""
    seen: set[str] = set()
    while location_id and location_id not in seen:
        if location_id in place_ids:
            return True
        seen.add(location_id)
        row = session.get(Location, location_id)
        location_id = row.parent_id if row is not None else None
    return False


def _refire_stalled_at_place(session, campaign: Campaign, beats: list[StoryBeat], player,
                             now: tuple[int, int]) -> dict[str, Any] | None:
    """A stalled beat fires again — once — when the player reaches its place.

    Round 4, fleet5-tamsin: "A Name on Minted Silver" (Vex Marrow and his
    fresh Greywater silver, on the approach to Wolf's Hollow) fired in the
    square, stalled after three nagged turns, and when she finally walked
    into the Hollow nothing brought it back: the DM invented a coin in the
    gate mud instead, and Vex stood unseen all visit. Now arriving there
    re-fires it as MUST-DELIVER with a fresh round of attempts (counted on
    past BEAT_DELIVERY_ATTEMPTS, which is also the once-only mark)."""
    here = getattr(player, "current_location_id", None)
    if not here:
        return None
    for beat in beats:
        if beat.status != "stalled" or (beat.delivery_attempts or 0) > BEAT_DELIVERY_ATTEMPTS:
            continue
        places = _place_ids(session, beat)
        if not places or not _within(session, here, places):
            continue
        logger.info(f"campaign beat re-fired where it happens: {beat.title}")
        beat.status = "fired"
        beat.delivery_attempts = BEAT_DELIVERY_ATTEMPTS + 1
        beat.fired_day, beat.fired_hour = now
        campaign.last_beat_day, campaign.last_beat_hour = now
        return _fired_payload(beat, attempt=1)
    return None


def _fire_next(session, campaign: Campaign, beats: list[StoryBeat], player,
               now: tuple[int, int], events: list, cinematics: list,
               tension: list) -> dict[str, Any] | None:
    """Eligibility → spacing → priority → fire exactly one beat."""
    candidates = [b for b in beats if b.status == "armed" and _is_eligible(session, b, player, now)]
    if not candidates:
        return None

    last_beat = _stamp(campaign, "last_beat") if campaign.last_beat_day is not None else None
    if last_beat is not None and _hours_between(last_beat, now) < CAMPAIGN_BEAT_SPACING_HOURS:
        # ...but a scene the player is standing in is no crowding (fleet10-
        # tamsin T6-T7: "The Man at the Wall" waited out the spacing a
        # misfired beat had used up while she stood with Sorrel asking about
        # exactly that), nor the scene a find of the player's brought due
        found = _FOUND_DUE.get(campaign.player_id, {})
        candidates = [b for b in candidates if b.beat_type == "deadline_event" or b.id in found
                      or _location_scene_here(session, b, player)]
    if not candidates:
        return None

    beat = sorted(candidates, key=lambda b: (BEAT_PRIORITY.get(b.beat_type, 9), b.act, b.sequence))[0]

    if beat.beat_type == "deadline_event":
        met = evaluate_condition(session, beat.met_condition, campaign, player)
        beat.variant = "met" if met else "missed"
        ops = beat.consequence_met if met else beat.consequence_missed
        if not met and (not beat.cinematic or beat.cinematic == "none"):
            beat.cinematic = "countdown_expired"
    else:
        beat.variant = "met"
        ops = beat.consequence_met

    # A hook is only shown this reply: what sets its stage happens now (a
    # mood, someone in place), what it hands over waits until the player
    # took it up and it was told (`_settle_hook`) — fleet6-bram's "Greywater
    # Silver" put the pouch in Bram's pack, ALREADY APPLIED, before anyone
    # pressed Vex. Its title goes on screen then too, never over the tell:
    # "▲ Coldiron's Lie, Uncovered" above a glance held too long.
    hook = is_hook_beat(beat)
    if hook:
        ops = [op for op in ops or [] if isinstance(op, dict) and op.get("op") in _HOOK_STAGING_OPS]
    applied, exempt = execute_ops(session, ops, campaign, player,
                                  events=events, deferred=tension)

    beat.status = "fired"
    beat.fired_day, beat.fired_hour = now
    beat.delivery_attempts = 1
    beat.applied_effects = applied
    # A beat told away from its place (a deadline's news reaching the player
    # elsewhere) crowds no scene here: it charges no spacing (fleet10-tamsin,
    # where a misfire in the square held Sorrel's sighting in the yard)
    places = _place_ids(session, beat)
    if not places or _within(session, getattr(player, "current_location_id", None), places):
        campaign.last_beat_day, campaign.last_beat_hour = now

    if not hook:
        _announce(beat, campaign, events, cinematics)

    logger.info(f"campaign beat fired: {beat.title} ({beat.beat_type}/{beat.variant})")
    return _fired_payload(beat, attempt=1, applied=applied)


def _location_scene_here(session, beat: StoryBeat, player) -> bool:
    """A place-triggered beat at the place where the player stands, with
    every person it is about (its lead, its ops) there too (`_fire_next`)."""
    here = getattr(player, "current_location_id", None)
    if not here or beat.trigger_type != "location" or beat.beat_type == "deadline_event":
        return False
    places = _place_ids(session, beat)
    if not places or not _within(session, here, places):
        return False
    from src.services.dialogue import is_beast

    people = [npc for npc in (session.get(NPC, i) for i in _beat_people(beat))
              if npc is not None and npc.status == "alive" and not is_beast(npc)]
    return bool(people) and all(npc.current_location_id == here for npc in people)


_HOOK_STAGING_OPS = ("npc_mood", "move_npc", "tension_floor")


def _settle_hook(session, beat: StoryBeat, campaign: Campaign, player, events: list, cinematics: list) -> None:
    """A hook the player took up and the reply told: what it held back at
    fire (`_fire_next`) is applied now — unless play already did it (the
    reply's own @give, the job already taken) — and it is announced."""
    rest = [op for op in (beat.consequence_met or []) if isinstance(op, dict)
            and op.get("op") not in _HOOK_STAGING_OPS and _op_state(session, op, player) != "done"]
    if rest:
        applied, _exempt = execute_ops(session, rest, campaign, player, events=events)
        beat.applied_effects = list(beat.applied_effects or []) + applied
    _announce(beat, campaign, events, cinematics)


def _announce(beat: StoryBeat, campaign: Campaign, events: list, cinematics: list) -> None:
    """The beat's toast (`campaign_beat`) and its cinematic request."""
    events.append({
        "type": "campaign_beat",
        "beat_id": beat.id,
        "act": beat.act,
        "title": beat.title,
        "beat_type": beat.beat_type,
        "variant": beat.variant,
        "cinematic": beat.cinematic or "none",
        "clock_label": beat.clock_label,
        "campaign_title": campaign.title,
    })
    request = _cinematic_request(beat.cinematic, campaign, beat.act, beat.clock_label, beat.variant)
    if request:
        cinematics.append(request)


def _is_eligible(session, beat: StoryBeat, player, now: tuple[int, int]) -> bool:
    """Has this beat's trigger been satisfied — or has its backstop expired?"""
    # A beat the player walks toward happens where its lead points. Round 4:
    # "The Whistle Doesn't Lie" (fires "when Wenna reaches Wolf's Hollow")
    # and "A Name on Minted Silver" (on the approach to the Hollow) had
    # quest_status triggers that came true in the Market Square; a Hollow
    # scene cannot be delivered in the square, and both stalled after three
    # nagged turns. Deadlines are the world's clock and fire wherever she is.
    # Round 6 (fleet7-tamsin "SING"): its backstop does not bring the place
    # to the player either — it waits there, and is skipped once it has
    # waited STALLED_SKIP_DAYS past it (`_skip_waiting_past_backstop`). The
    # finale a dead villain forced (`_check_villain`) is the story ending
    # now, wherever the player is
    if beat.beat_type != "deadline_event" and not _forced_finale(session, beat) \
            and not _at_beat_place(session, beat, player):
        return False
    # A scene built on a scheduled event happens once that event has (round
    # 7, fleet8-wenna: "Build on the scheduled arrival of the half-empty
    # Greywater cart" — due before the cart, it would tell the cart early)
    if beat.beat_type != "deadline_event" and _scheduled_basis(session, beat):
        return False

    fallback = _stamp(beat, "fallback")
    if fallback is not None and now >= fallback:
        return True

    if beat.beat_type == "deadline_event" or beat.trigger_type == "time" or beat.trigger_type == "after_beat":
        scheduled = _stamp(beat, "scheduled")
        return scheduled is not None and now >= scheduled

    return _play_trigger_met(session, beat, player)


def _forced_finale(session, beat: StoryBeat) -> bool:
    """An act-3 beat still standing once the villain died with no successor:
    `_check_villain` skipped every other one, so this is the forced finale."""
    if beat.act != ACTS_TOTAL:
        return False
    campaign = _get(session, Campaign, beat.campaign_id)
    villain = _get(session, NPC, campaign.villain_npc_id) if campaign is not None else None
    return villain is not None and villain.status == "dead"


def _play_trigger_met(session, beat: StoryBeat, player) -> bool:
    """Does play satisfy this beat's own trigger (a place, a quest status, a
    milestone)? Clocks and backstops are `_is_eligible`'s."""
    data = beat.trigger_data if isinstance(beat.trigger_data, dict) else {}

    if beat.trigger_type == "location":
        return bool(data.get("location_id")) and \
            getattr(player, "current_location_id", None) == data.get("location_id")

    if beat.trigger_type == "quest_status":
        quest = _get(session, Quest, data.get("quest_id"))
        return bool(quest and quest.status == data.get("status"))

    if beat.trigger_type == "milestone":
        thresholds = MILESTONE_THRESHOLDS.get(str(data.get("milestone")))
        if not thresholds:
            return False
        level_needed, quests_needed = thresholds
        level = getattr(player, "level", None) or 0
        completed = len(getattr(player, "completed_quests", None) or [])
        return level >= level_needed or completed >= quests_needed

    return False


def _at_beat_place(session, beat: StoryBeat, player) -> bool:
    """Is the player where this beat's location lead points (there, or
    somewhere inside it)? With no location lead, where its words put it
    (`_scene_bound_ids`: an arrival, its opening place, the scene it
    carries on, its Things). True when it points at no place."""
    target = beat.lead_target if isinstance(beat.lead_target, dict) else {}
    if target.get("type") != "location" or not target.get("id"):
        places = _scene_bound_ids(session, beat)
        return not places or _within(session, getattr(player, "current_location_id", None), places)
    if _get(session, Location, target["id"]) is None:
        return True
    loc_id = getattr(player, "current_location_id", None)
    seen: set[str] = set()
    while loc_id and loc_id not in seen:
        if loc_id == target["id"]:
            return True
        seen.add(loc_id)
        row = session.get(Location, loc_id)
        loc_id = row.parent_id if row is not None else None
    return False


def _exempt_for_ops(ops: list | None) -> list[str]:
    """Which audit categories this beat's ops covered.

    Derived from the op list rather than stored, so a beat re-surfaced on a
    later turn still suppresses the same checks — the world changed once, but
    the DM may narrate it on any of the three attempts.
    """
    exempt: set[str] = set()
    for op in (ops or []):
        if not isinstance(op, dict):
            continue
        name = op.get("op")
        if name in ("activate_quest", "fail_quest"):
            exempt.add("quest")
        elif name == "complete_quest":
            exempt.update({"quest", "item"})
        elif name == "give_item":
            exempt.add("item")
        elif name in ("npc_status", "npc_mood", "faction_power",
                      "faction_relationship", "world_event"):
            exempt.add("consequence")
    return sorted(exempt)


def _named_people(directive: str, npcs: list[NPC]) -> list[NPC]:
    """The people a directive names: their full name, or a surname of four
    letters or more as a capitalised word ("Captain Coldiron" is Captain
    Brann Coldiron). Beasts are skipped — "Hollow Wolf" is not named by
    every "Wolf's Hollow"."""
    from src.services.dialogue import is_beast

    found: list[NPC] = []
    lowered = (directive or "").lower()
    for npc in npcs:
        name = " ".join((npc.name or "").split())
        if not name:
            continue
        tokens = name.split()
        named = name.lower() in lowered or (
            len(tokens) >= 2 and len(tokens[-1]) >= 4
            and re.search(rf"(?<![\w'’-]){re.escape(tokens[-1])}(?![\w-])", directive or "") is not None
        )
        if not named:
            continue
        try:
            if is_beast(npc):
                continue
        except Exception:  # pragma: no cover - a malformed row is still a person
            pass
        found.append(npc)
    return found


def _beat_cast(session, beat: StoryBeat, *, alive_only: bool = True,
               npcs: list[NPC] | None = None) -> list[NPC]:
    """Everyone a beat needs as written: its lead's person, whoever its ops
    (met or missed) act on, and everyone its directive names — beasts too,
    by their whole name ("Have Hollow Wolf and Gray-Flank Wolf prowl the
    den's edge"; a beast's last word alone is every "Wolf's Hollow")."""
    from src.services.dialogue import is_beast

    if npcs is None:
        query = session.query(NPC)
        npcs = (query.filter(NPC.status == "alive") if alive_only else query).all()
    elif alive_only:
        npcs = [n for n in npcs if n.status == "alive"]
    ids = set(_beat_people(beat)) | {
        str(op["npc_id"]) for op in (beat.consequence_missed or []) if isinstance(op, dict) and op.get("npc_id")}
    text = " ".join([beat.directive or "", beat.directive_missed or ""])
    found = [n for n in npcs if n.id in ids]
    found += [n for n in _named_people(text, npcs) if n not in found]
    lowered = text.lower().replace("’", "'")
    for npc in npcs:
        name = " ".join((npc.name or "").split()).lower()
        if npc in found or len(name) < 4:
            continue
        if re.search(rf"(?<![\w'-]){re.escape(name)}(?![\w-])", lowered):
            try:
                if is_beast(npc):
                    found.append(npc)
            except Exception:  # pragma: no cover - a malformed row names no beast
                pass
    return found


def story_cast_ids(session, player_id: str | None) -> set[str]:
    """The NPC ids the story still needs as written: the villain, the
    successor, and everyone a beat not yet played (pending, armed, fired,
    stalled) leads to, acts on or names (`_beat_cast`). The encounter
    director never sends them at the player (round 6, fleet7-wenna: its T14
    ambush killed the two wolves act-3 "The Cached Cargo" stages at the den).
    Empty without a live campaign."""
    campaign = get_campaign_row(session, player_id)
    if campaign is None or campaign.status != "active":
        return set()
    ids = {i for i in (campaign.villain_npc_id, campaign.successor_npc_id) if i}
    npcs = session.query(NPC).filter(NPC.status == "alive").all()
    for beat in _campaign_beats(session, campaign):
        if beat.status in ("pending", "armed", "fired", "stalled"):
            ids |= {npc.id for npc in _beat_cast(session, beat, npcs=npcs)}
    return ids


def beasts_kept_for_later(session, player_id: str | None) -> set[str]:
    """The story's beasts a beat still to come needs (pending, armed or
    parked) and no beat firing now plays: a fight the DM starts with them
    before their scene meets a stand-in of their pack (round 9,
    `commands._story_beasts_stood_in`). Empty without a live campaign."""
    campaign = get_campaign_row(session, player_id)
    if campaign is None or campaign.status != "active":
        return set()
    from src.services.dialogue import is_beast

    beasts = [npc for npc in session.query(NPC).filter(NPC.status == "alive").all() if is_beast(npc)]
    if not beasts:
        return set()
    waiting: set[str] = set()
    playing: set[str] = set()
    for beat in _campaign_beats(session, campaign):
        if beat.status in ("pending", "armed", "stalled"):
            waiting |= {npc.id for npc in _beat_cast(session, beat, npcs=beasts)}
        elif beat.status == "fired":
            playing |= {npc.id for npc in _beat_cast(session, beat, npcs=beasts)}
    return waiting - playing


def _directive_notes(session, beat: StoryBeat, player) -> list[str]:
    """What the DM must know to deliver this beat in the world as it is now.

    - Its quest is already the player's: play started (or finished) it, so
      the scene must not pitch the job again — only what is new.
    - Someone it reveals the player already met: no second introduction.
    - Someone it names is not where the player is (fleet4-wenna T20: "What
      Sorrel Saw" put Sorrel "around the stables in Market Square" while
      the player sat in the Drowned Lantern, and the DM walked her into the
      square without @move): they come to the player, or it waits.
    - It stalled elsewhere and is re-fired where it happens: this is the
      real scene, whatever stand-in an earlier reply improvised.
    """
    notes: list[str] = []
    if beat.status == "fired" and (beat.delivery_attempts or 0) > BEAT_DELIVERY_ATTEMPTS:
        notes.append("This beat was due while the player was elsewhere; they are where it happens now. "
                     + ("Show its hook HERE (see HOOK below)" if is_hook_beat(beat) else "Deliver it HERE, as written")
                     + " — if an earlier reply improvised a stand-in for it, this is the real thing.")
    # Someone's entrance (round 9, fleet10-bram T15): the brief was written
    # before this beat fired and may still hold them OFFSTAGE; and when the
    # player's own find brought it due, the find is done (`_due_by_find`)
    try:
        entering = _entrance_people(session, beat, player) if beat.status == "fired" else []
    except Exception as e:  # a note the beat can live without
        logger.debug(f"entrance check for {beat.title!r} skipped: {e}")
        entering = []
    if entering and player is not None:
        shown = [f"{_shown_as(session, player.id, npc)} [real name {npc.name}]"
                 if _shown_as(session, player.id, npc) != npc.name else npc.name for npc in entering]
        found = beat.id in _FOUND_DUE.get(player.id, {})
        notes.append(
            ("THE FIND IS THE CUE: the player already found it themselves, in their own reply — never tell the "
             "finding again; this reply is what it sets off. " if found else "")
            + f"ENTRANCE: {' and '.join(shown)} {'comes' if len(shown) == 1 else 'come'} on in this beat — "
            "on screen this reply, doing what the directive says (any words of theirs as speaker lines). If "
            "their dossier above still says OFFSTAGE, it was written before this beat fired: it no longer holds."
        )
    applied = set(beat.applied_effects or [])
    for quest in _offered_quests(session, beat):
        if f'Quest activated: "{quest.title}"' in applied:
            continue    # this beat's own op started it: offering it IS the scene
        if quest.status == QuestStatus.ACTIVE:
            notes.append(f'The player already took "{quest.title}" — do not offer it again; '
                         "deliver only what is new in this beat.")
        elif quest.status in (QuestStatus.COMPLETED, QuestStatus.FAILED):
            notes.append(f'The player already finished "{quest.title}" — never offer it again; '
                         "deliver only what is new in this beat.")

    # Someone it brings on stage the player already knows (fleet4-tamsin
    # parleyed with Aldric Vane at the Hollow; "Reveal Aldric Vane…" still
    # waited): play spent the introduction, not what this beat un-seals
    if player is not None and _REVEAL_VERB.search(beat.directive or ""):
        alive = session.query(NPC).filter(NPC.status == "alive").all()
        known = [
            npc.name for npc in _revealed_people(beat, alive)
            if session.query(NPCRelationship.id).filter(
                NPCRelationship.npc_id == npc.id, NPCRelationship.player_id == player.id,
                _name_known_clause(),       # knows their NAME: a row alone is a talk (fleet6)
            ).first() is not None
        ]
        if known:
            notes.append(f"The player already knows {' and '.join(known)} — no introduction, no first "
                         "glimpse: they meet again, and only what is new in this beat is news.")

    here_id = getattr(player, "current_location_id", None)
    if here_id:
        npcs = session.query(NPC).filter(NPC.status == "alive").all()
        # The villain, still held back, is only NAMED by a beat that neither
        # leads to him nor reveals him: "A Name on the Whistle" has Coldiron
        # name Aldric Vane, and pulled ahead to the square (fleet5-bram
        # replay) it told the DM "Aldric Vane is at Wolf's Hollow… comes to
        # them HERE" — an invitation to walk the villain into town in act 2
        meets = _beat_people(beat) | {npc.id for npc in _revealed_people(beat, npcs)}
        away = [npc for npc in _named_people(beat.directive or "", npcs)
                if npc.current_location_id != here_id
                and (npc.id in meets or not villain_held_line(session, player.id, npc))]
        if away:
            here = _get(session, Location, here_id)
            where = []
            for npc in away:
                there = _get(session, Location, npc.current_location_id)
                where.append(f"{npc.name} is at {there.name if there else 'somewhere else'}")
            notes.append(
                f"WHERE PEOPLE ARE: the player is at {here.name if here else 'their current place'}; "
                + "; ".join(where) + ". Whoever this beat has meet the player comes to them HERE — "
                "or that part waits until the player goes to them. Never move the player in prose: "
                "only their own @move does."
            )

    # Its truth leaked ahead of it (fleet6-bram T19, `_ahead_of`): the fact
    # is canon, the scene is still owed
    campaign = _get(session, Campaign, beat.campaign_id)
    reveals = {str(i) for i in beat.reveals_truth_ids or []}
    for truth in (campaign.fixed_truths or []) if campaign is not None else []:
        if isinstance(truth, dict) and truth.get("revealed") and str(truth.get("id")) in reveals \
                and beat.id in {str(i) for i in truth.get("scene_owed") or []}:
            heard = " ".join(str(truth.get("revealed_evidence") or truth.get("statement") or "").split())
            notes.append(f"The player already overheard this ({heard}). This beat is the confrontation, not "
                         "the news: the admission still costs a scene — whoever owes it does not get to skip "
                         "it because the player half-knows. Play it as written, building on what they heard.")

    # The last reply's roll failed on it (fleet6-bram T14, mark_beat_delivery)
    if player is not None and beat.id in _ROLL_HELD.get(player.id, set()):
        notes.append("The last reply's roll failed, so this did not land: they held it back. It comes out "
                     "another way — the player tries again, pays, or finds another witness — never by "
                     "replaying the failed exchange as if it had worked.")

    # A hook waits on the player (fleet6-bram T14, `is_hook_beat`)
    if is_hook_beat(beat):
        who = " or ".join(npc.name for npc in _hook_people(session, beat)[:2]) or "the person it is about"
        lead = f' The player\'s lead reads: "{beat.lead.strip()}".' if (beat.lead or "").strip() else ""
        notes.append(f"HOOK — this beat waits on the player. This reply {who} shows a visible tell or comes "
                     "to the player (a glance held too long, a word as they pass) — nobody tells it unasked. "
                     "Never write the player pressing, asking or paying anyone, and roll for nothing the "
                     f"player did not try.{lead} When the player takes it up — asks, presses, pays — deliver "
                     "the rest as written.")
    # ...and what it scripts for the player is theirs (round 7: "Have Bram
    # read the notice board", "before she pockets the whistle")
    cue, passing = player_scripted(beat)
    for phrase in dict.fromkeys(p for p in (cue, passing) if p):
        notes.append(f'PLAYER\'S PART — the directive says "{phrase}": what the player does is their choice, '
                     "never yours to write. Put it within their reach (the thing in plain view, the person "
                     "there) and stop; if they do it, what follows is yours to deliver.")

    # Its scheduled twin was already told (fleet6-wenna T8/T10: the 20:00
    # howl event was narrated, and "The Evening Howl" deadline still due).
    # Only an event told while this beat was already waiting is its twin:
    # fleet-bram's act-3 "The Evening Howl" ("close at the evening howl as
    # always — but this time…") and fleet4-tamsin's finale of that name arm
    # days after Day 1's howl, and are that night's howl, not its aftermath
    try:
        for event in _event_twins(session, beat, told=True):
            notes.append(f'"{event.name}" already happened and was told (Day {event.occurred_day}, '
                         f"{int(event.occurred_hour or 0):02d}:00) — never stage it a second time: deliver "
                         "only what this beat adds, as its aftermath.")
    except Exception as e:  # a note the beat can live without
        logger.debug(f"twin check for {beat.title!r} skipped: {e}")

    # A deadline is the world's clock and fires wherever the player is; one
    # whose scene is somewhere else happens THERE (round 6, fleet7-tamsin:
    # "SING" fired in the Stable Yard and the DM walked the player to Wolf's
    # Hollow in prose). Other beats wait at their place (`_is_eligible`)
    if player is not None and beat.beat_type == "deadline_event":
        places = _place_ids(session, beat)
        here_id = getattr(player, "current_location_id", None)
        if places and not _within(session, here_id, places):
            names = [row.name for row in (_get(session, Location, i) for i in sorted(places)) if row is not None]
            here = _get(session, Location, here_id)
            if names:
                notes.append(
                    f"WHERE IT HAPPENS: {' / '.join(names)} — the player is at "
                    f"{here.name if here else 'another place'}, not there. It happened there without them: "
                    "deliver it as news or its aftermath reaching them HERE (a runner, a sound carried on "
                    "the wind, what it leaves behind). Never take the player there in this reply and never "
                    "stage its scene around them — only their own @move does."
                )

    # Someone it needs is dead (round 6, fleet7-wenna: the T14 ambush killed
    # Hollow Wolf and Gray-Flank Wolf, and act-3 "The Cached Cargo" still has
    # them "prowl the den's edge")
    killed_by_beat = {str(op.get("npc_id")) for op in [*(beat.consequence_met or []), *(beat.consequence_missed or [])]
                      if isinstance(op, dict) and op.get("op") == "npc_status"}
    dead = [npc.name for npc in _beat_cast(session, beat, alive_only=False)
            if npc.status == "dead" and npc.id not in killed_by_beat]
    if dead:
        notes.append(f"{' and '.join(dead)} {'is' if len(dead) == 1 else 'are'} dead — never bring them on "
                     "stage or speak of them as alive. Whatever this beat had them do is done by someone else "
                     "of their side (the rest of the pack, another hand), or told as what their death left behind.")

    # Its cue can never come now (round 7, fleet8-bram: "Teeth on the North
    # Road" paid in full, and act 3 waiting on it being under way)
    done = _dead_cue(session, beat)
    if done is not None:
        how = "finished" if done.status == QuestStatus.COMPLETED else "failed"
        notes.append(f'It was cued by "{done.title}" — and the player already {how} that job. Play this as '
                     "what came after it, never as a step of that job, and never reopen it.")

    # An earlier act's scene, still waiting after the story moved on (round
    # 6, fleet7-wenna: act 2 pulled ahead with act-1 beats still armed)
    if campaign is not None and beat.act < (campaign.current_act or 1):
        notes.append(f"The story is in act {campaign.current_act} now: this act-{beat.act} scene comes late. "
                     "Play it in the world as it stands — never contradict or take back what the player "
                     "already learned, was given or was told; leave out whatever play already made true.")

    # Resistance is the scene, and what it hides has a price (round 6,
    # fleet7-tamsin T16: "If pressed, he deflects, claims he's never seen its
    # like, but Tamsin notices…" — Coldiron deflected and confessed why in
    # the same breath; no roll, no leverage)
    resists = _RESISTS.search(beat.directive or "")
    if resists:
        notes.append(f'COST — someone in this beat resists ("{resists.group(0).strip()}"): play that resistance '
                     "as written. What gets past it is only what the directive lets past, the way it says (what "
                     "the player sees for themselves, or a second press) — never a deflection and a confession "
                     "in one breath. Anything more (the why, the rest of the truth) waits for the player to press "
                     "again and WIN it: a roll_check they make (hard), a real risk, or a trade.")
    return notes


def _fired_payload(beat: StoryBeat, attempt: int, applied: list | None = None) -> dict[str, Any]:
    """What the DM (and the audit) is told about the beat happening NOW."""
    directive = beat.directive or ""
    if beat.variant == "missed" and beat.directive_missed:
        directive = beat.directive_missed
    ops = beat.consequence_missed if beat.variant == "missed" else beat.consequence_met
    return {
        "beat_id": beat.id,
        "title": beat.title,
        "beat_type": beat.beat_type,
        "act": beat.act,
        "directive": directive,
        "fixed_truths": list(beat.fixed_truths or []),
        "variant": beat.variant or "met",
        "applied_effects": list(applied if applied is not None else (beat.applied_effects or [])),
        "cinematic": beat.cinematic or "none",
        "audit_exempt": _exempt_for_ops(ops),
        "delivery_keywords": list(beat.delivery_keywords or []),
        "attempt": attempt,
        "hook": is_hook_beat(beat),
    }


def _build_briefing(session, campaign: Campaign, beats: list[StoryBeat],
                    now: tuple[int, int], fired: dict | None,
                    events: list, note: str | None, *, heard: str = "") -> dict[str, Any]:
    """Assemble the tick payload: summary, truths, clocks, act progress.
    `heard`: everything the player has heard (`_heard_before`)."""
    act = campaign.current_act or 1
    entry = _act_entry(campaign, act)
    act_beats = [b for b in beats if b.act == act]
    delivered = [b for b in beats if b.status == "delivered"]

    # A truth the beat firing now reveals is the story on schedule, not a
    # front-run: two terms in one line is exactly what delivering "Eleven,
    # Not Twenty" says. The DM sees it as REVEAL NOW, not SEALED (a sealed
    # line beside a reveal directive told it both "say it" and "never say it")
    row = next((b for b in beats if fired and b.id == fired.get("beat_id")), None)
    on_schedule = {str(i) for i in (row.reveals_truth_ids or [])} if row is not None else set()
    truths = [
        {
            "id": t.get("id"),
            "kind": t.get("kind"),
            "statement": t.get("statement"),
            "revealed": bool(t.get("revealed")),
            "reveal_now": str(t.get("id")) in on_schedule and not t.get("revealed"),
        }
        for t in (campaign.fixed_truths or []) if isinstance(t, dict)
    ]
    # The audit's copy: the terms that would give each truth away, never
    # the statement (the payload is cached and logged) — `cross_terms` are
    # those the spread-thin and subject rules may count (`_cross_terms`),
    # `subjects` the people a truth is about (the engine's own probes)
    common = campaign_common_words(campaign, beats)
    # ...nor one a stalled beat reveals: its scene was brought on, and play
    # telling it IS its delivery (the seal guard must not cut that)
    open_scenes = on_schedule | _stalled_reveals(beats)
    unrevealed = [t for t in (campaign.fixed_truths or [])
                  if isinstance(t, dict) and not t.get("revealed") and str(t.get("id")) not in open_scenes]
    people = session.query(NPC).all() if unrevealed else []
    sealed = _sealed_probes(unrevealed, people, common)
    player = session.get(Player, campaign.player_id)
    known_words = _known_vocabulary(session, campaign, player) if sealed else []
    # Round 8: the one-word things of each sealed truth the player has not
    # met, and who may say them (`sealed_word_slipped`, fleet9-tamsin T18)
    sealed_words, word_holders = _unmet_sealed_words(session, player, unrevealed, people, common,
                                                     known_words + [heard])
    # The audit nags an undelivered beat by the engine's own evidence rule;
    # a hook is shown as a tell, so its words are not owed this reply
    audit_beat = None
    if fired and not fired.get("hook"):
        audit_beat = {
            "beat_id": fired["beat_id"],
            "title": fired["title"],
            "delivery_keywords": fired["delivery_keywords"],
            # what only a second press says is not owed (`later_keywords`)
            "later_keywords": later_keywords(row.directive if row is not None else "",
                                             fired["delivery_keywords"]),
            "names": _known_vocabulary(session, campaign, player, public=False),
            "common_words": common,
        }
    # The villain before their first scene: any line of theirs is flagged
    # (round 7, `villain_offstage`)
    villain = _get(session, NPC, campaign.villain_npc_id)
    offstage = None
    if villain is not None and player is not None and villain_offstage(session, player.id, villain):
        offstage = {"id": villain.id, "name": villain.name}
    # ...the villain's people held for an entrance of their own (round 9,
    # `cast_offstage`), and the places the player never stood in — told as
    # their memory, `memory_of_unvisited_place` (fleet10-tamsin T6)
    offstage_cast: list[dict[str, str]] = []
    unvisited: list[str] = []
    if player is not None:
        try:
            offstage_cast = [{"id": npc.id, "name": npc.name, "label": _shown_as(session, player.id, npc)}
                             for npc in _cast_offstage_people(session, campaign, player)]
            unvisited = _unvisited_places(session, player)
        except Exception as e:  # the audit lives without them
            logger.debug(f"offstage cast / unvisited places unavailable: {e}")

    return {
        "campaign": {
            "title": campaign.title,
            "act": act,
            "acts_total": ACTS_TOTAL,
            "act_title": entry.get("title") or "",
            "act_premise": entry.get("premise") or "",
            "premise": campaign.premise or "",
            "status": campaign.status,
        },
        "truths": truths,
        "clocks": _clocks(beats, now),
        "delivered_titles": [b.title for b in delivered],
        "beats_delivered": len([b for b in act_beats if b.status in ("delivered", "skipped")]),
        "beats_total": len(act_beats),
        "fired_beat": fired,
        "note": note,
        "events": events,
        # Scheduled events a delivered beat already told (`_retire_twins`)
        "continuity": _CONTINUITY.pop(campaign.player_id, []),
        "audit": {
            "beat": audit_beat,
            "sealed_truths": sealed,
            # What the player already knows (`sealed_truth_hits`' `known`)
            "known_words": known_words,
            "offstage_villain": offstage,
            # Whose reply this is (the guard's cuts, `pop_seal_cuts`), and
            # each sealed truth's unmet thing-words (`sealed_word_slips`)
            "player_id": campaign.player_id,
            "sealed_words": sealed_words,
            "word_holders": word_holders,
            "offstage_cast": offstage_cast,
            "unvisited_places": unvisited,
        },
    }


def _unvisited_places(session, player) -> list[str]:
    """The names of the places the player never stood in: not visited, and
    neither where they are nor anything holding a place they have been (the
    town itself is never "unvisited" to someone in its square)."""
    locations = session.query(Location).all()
    by_id = {loc.id: loc for loc in locations}
    been: set[str] = set()
    for start in [loc.id for loc in locations if loc.visited] + [getattr(player, "current_location_id", None)]:
        seen: set[str] = set()
        while start and start not in seen:
            seen.add(start)
            been.add(start)
            row = by_id.get(start)
            start = row.parent_id if row is not None else None
    return sorted(loc.name for loc in locations if loc.id not in been and loc.name)


def beat_waits_elsewhere(beat_id: str | None) -> bool:
    """Is this beat (not a deadline: the world's clock is owed anywhere)
    bound to a place (`_place_ids`) the player is not at now? Its words are
    owed there, never here — the audit does not nag it (fleet10-tamsin T4-T5:
    "SING", nagged in the square, became a memory of the Hollow at T6).
    False when unreadable."""
    if not beat_id:
        return False
    try:
        with get_session() as session:
            beat = session.get(StoryBeat, beat_id)
            campaign = session.get(Campaign, beat.campaign_id) if beat is not None else None
            player = session.get(Player, campaign.player_id) if campaign is not None else None
            if player is None or beat.beat_type == "deadline_event" or _forced_finale(session, beat):
                return False
            places = _place_ids(session, beat)
            return bool(places) and not _within(session, player.current_location_id, places)
    except Exception as e:
        logger.debug(f"beat place check failed for {beat_id}: {e}")
        return False


def _clocks(beats: list[StoryBeat], now: tuple[int, int]) -> list[dict[str, Any]]:
    """Live countdowns: armed deadline beats with a label, soonest first."""
    clocks = []
    for beat in beats:
        if beat.status != "armed" or beat.beat_type != "deadline_event" or not beat.clock_label:
            continue
        scheduled = _stamp(beat, "scheduled")
        if scheduled is None:
            continue
        remaining = _hours_between(now, scheduled)
        if remaining <= 0:
            urgency, days_left, hours_left = "overdue", 0, 0
        else:
            days_left, hours_left = divmod(remaining, 24)
            if remaining < URGENCY_IMMINENT_HOURS:
                urgency = "imminent"
            elif remaining <= URGENCY_SOON_HOURS:
                urgency = "soon"
            else:
                urgency = "distant"
        clocks.append({
            "beat_id": beat.id,
            "label": beat.clock_label,
            "days_left": days_left,
            "hours_left": hours_left,
            "urgency": urgency,
            "day": scheduled[0],
            "hour": scheduled[1],
        })
    clocks.sort(key=lambda c: (c["day"], c["hour"]))
    return clocks


# ----------------------------------------------------------------------
# Delivery detection + act transitions
# ----------------------------------------------------------------------

# Words that say nothing about WHICH person, place or thing a keyword names:
# function words, honorifics, compass points, times of day, stock adjectives.
# "Captain Coldiron" is matched on "Coldiron", "The Evening Howl" on "howl".
_GENERIC_WORDS = frozenset({
    "the", "and", "for", "with", "from", "into", "onto", "over", "under", "out", "off",
    "upon", "about", "near", "not", "but", "his", "her", "hers", "its", "their", "they",
    "them", "she", "him", "you", "your", "our", "this", "that", "these", "those", "who",
    "what", "was", "were", "are", "has", "had", "have", "all", "any", "one",
    "captain", "lord", "lady", "sir", "dame", "master", "mistress", "mister",
    "north", "south", "east", "west", "northern", "southern", "eastern", "western",
    "morning", "evening", "night", "nightly", "dawn", "dusk", "noon", "midnight",
    "day", "today", "tonight",
    "old", "young", "new", "same", "other", "own", "first", "last", "next", "very",
    "more", "most", "some", "much", "many", "such", "just", "still",
})
_MIN_WORD = 3
_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")


def _tokens(text: str) -> list[str]:
    lowered = (text or "").lower().replace("’", "'").replace("‘", "'")
    return _WORD.findall(lowered)


def _words(text: str) -> list[str]:
    """Lower-cased words; hyphens split, a possessive or contraction drops."""
    return [w.split("'")[0] for w in _tokens(text)]


def _word_forms(word: str) -> set[str]:
    """The word plus the stems a plural or a tense would leave behind, so
    "wolves" meets "wolf", "howling" meets "howl" and "bones" meets "bone".

    A stem that is a function word is never kept, and "-es" comes off whole
    only after a hiss (boxes, witches): otherwise "shed" met every "she",
    "thing" every "the", and "notes" every "not" in the fleet transcripts."""
    forms = {word}

    def add(stem: str) -> None:
        if len(stem) >= _MIN_WORD and stem not in _GENERIC_WORDS:
            forms.add(stem)

    def add_stem(stem: str) -> None:
        add(stem)
        # A doubled final consonant is the tense's, not the word's: "drummed
        # out" meets "drum him out", "stripped" meets "strip" (kill, pass,
        # buzz and stuff keep theirs)
        if len(stem) >= 4 and stem[-1] == stem[-2] and stem[-1] not in "aeiouylsfz":
            add(stem[:-1])

    if word.endswith("ves"):
        add(word[:-3] + "f")
    if word.endswith(("ies", "ied")):
        add(word[:-3] + "y")
    if word.endswith("ing"):
        add_stem(word[:-3])
        add(word[:-3] + "e")
    elif word.endswith("ed"):
        add_stem(word[:-2])
        add(word[:-1])
    elif word.endswith("es"):
        add(word[:-1])
        if word[:-2].endswith(("s", "x", "z", "ch", "sh")):
            add(word[:-2])
    elif word.endswith("s") and not word.endswith("ss"):
        add(word[:-1])
    return forms


def _distinctive(keyword: str) -> list[str]:
    """The words that pick this keyword out. A possessive is a modifier:
    "Wolf's Hollow" is named by "hollow" — in a campaign about wolves,
    every "wolf" would otherwise name the place."""
    kept = [(t.split("'")[0], t.endswith("'s")) for t in _tokens(keyword)]
    kept = [(w, owner) for w, owner in kept if len(w) >= _MIN_WORD and w not in _GENERIC_WORDS]
    heads = [w for w, owner in kept if not owner]
    return heads or [w for w, _ in kept]


def _text_forms(text: str) -> set[str]:
    forms: set[str] = set()
    for word in _words(text):
        forms |= _word_forms(word)
    return forms


def _keyword_hit(keyword: str, forms: set[str], text: str) -> bool:
    """A keyword is mentioned when at least half of its distinctive words
    are — "the hollow" names Wolf's Hollow, "Vex" names Vex Marrow. A
    keyword with no distinctive word ("f0", "the north") must appear whole."""
    words = _distinctive(keyword)
    if not words:
        phrase = " ".join(_words(keyword))
        return bool(phrase) and re.search(rf"\b{re.escape(phrase)}\b", " ".join(_words(text))) is not None
    hits = sum(1 for word in words if _word_forms(word) & forms)
    return hits * 2 >= len(words)


def mentioned_keywords(keywords: Iterable[str] | str, text: str) -> list[str]:
    """The keywords `text` mentions, matched loosely (see `_keyword_hit`).

    Shared by beat delivery here and event delivery in `world_tick`: exact
    matching flagged beats the DM had delivered in other words three times
    over, and the nag pushed it to re-deliver and escalate (playtest
    2026-10-04: "Into the Hollow" said "the hollow", never "Wolf's Hollow").
    """
    if isinstance(keywords, str):
        keywords = [keywords]
    forms = _text_forms(text)
    return [str(k) for k in (keywords or []) if str(k).strip() and _keyword_hit(str(k), forms, text)]


def beat_keywords_delivered(beat: Any, text: str, known: Iterable[str] | None = None,
                            common: Iterable[str] | None = None) -> bool:
    """Did `text` deliver this beat? `beat` is a StoryBeat row or a dict with
    `delivery_keywords` (the audit's copy). At least half the keywords must
    be mentioned, minimum one; a beat with no keywords is delivered by any
    turn. Pass speaker names inside `text` — a speaker line names its NPC.

    Given the world's names (`known`) and the campaign's common words
    (`campaign_common_words`) — or a dict carrying them as `names` /
    `common_words` — only evidence counts (`evidence_told`): keywords that
    say more than a name or the campaign's weather, a named person at most
    seconding one of them — and only the beat's first stage is owed
    (`later_keywords`; the dict carries them as `later_keywords`)."""
    raw = beat.get("delivery_keywords") if isinstance(beat, dict) else getattr(beat, "delivery_keywords", None)
    keywords = [str(k) for k in (raw or []) if str(k).strip()]
    if not keywords:
        return True
    if isinstance(beat, dict):
        known = beat.get("names") if known is None else known
        common = beat.get("common_words") if common is None else common
        later = beat.get("later_keywords") or []
    else:
        later = later_keywords(getattr(beat, "directive", None), keywords)
    if known is None and common is None:
        return len(mentioned_keywords(keywords, text)) * 2 >= len(keywords)
    keywords = _first_stage(keywords, later, known or (), common or ())
    return evidence_told(keywords, text, known or (), common or (), name_support=True)[0]


# --- What counts as evidence a beat was told --------------------------------
# Round 5, fleet6-bram T14: "The Fence's Ledger" (keywords wolf-taken, fence,
# dead carters, Hesk) was delivered — and its truth un-sealed — on "Hesk" (a
# speaker label) and "wolf" (half of "wolf-taken", a word every turn of a
# wolf campaign says), while the Hawker clammed up after a failed roll and
# the player never heard "fence". A name is who a beat is about, not what it
# says; a word in a third or more of the campaign's own beats, truths and
# premises is its weather, not its news; and the everyday words the truth
# check already ignores (`_COMMON_WORDS`: dead, taken, coin…) prove nothing
# either. Only the rest is evidence. Replayed over every fleet transcript
# (fleet-fleet6): see the round-5 notes in tests/test_campaign_evidence.py.
CAMPAIGN_COMMON_SHARE = 1 / 3
COMMON_MIN_DOCS = 12     # fewer texts than this: no word is "campaign-wide"


def campaign_common_words(campaign: Campaign, beats: Iterable[StoryBeat]) -> list[str]:
    """The word forms in CAMPAIGN_COMMON_SHARE or more of this campaign's
    texts (each beat's title, directive and keywords; each truth; the
    premise and every act's), sorted. "wolf" in a story about wolves."""
    docs = [" ".join([str(b.title or ""), str(b.directive or ""), " ".join(str(k) for k in b.delivery_keywords or [])])
            for b in beats]
    docs += [str(t.get("statement") or "") for t in (campaign.fixed_truths or []) if isinstance(t, dict)]
    docs += [str(campaign.premise or "")]
    docs += [str(a.get("premise") or "") for a in (campaign.acts or []) if isinstance(a, dict)]
    docs = [d for d in docs if d.strip()]
    if len(docs) < COMMON_MIN_DOCS:
        return []
    counts: dict[str, int] = {}
    for doc in docs:
        for form in _text_forms(doc):
            if len(form) >= _MIN_WORD and form not in _GENERIC_WORDS:
                counts[form] = counts.get(form, 0) + 1
    return sorted(f for f, n in counts.items() if n >= CAMPAIGN_COMMON_SHARE * len(docs))


def _evidence_words(keyword: str, name_forms: set[str], common_forms: set[str]) -> list[str]:
    """The words of a keyword that say something: distinctive, not part of a
    name, not the campaign's weather, not an everyday word."""
    return [w for w in _distinctive(keyword)
            if w not in _COMMON_WORDS and not (_word_forms(w) & name_forms)
            and not (_word_forms(w) & common_forms)]


def _evidence_phrase(keyword: str, name_forms: set[str], common_forms: set[str],
                     name_runs: Iterable[str] = ()) -> str | None:
    """A keyword of everyday words only ("north wall") is still evidence when
    said whole, as a phrase: Sorrel's "ash-gray cloak … by the north wall"
    told his sighting in five fleet transcripts, and without it his beat
    fired later as news (fleet4-wenna T16 → T20). Never the campaign's
    weather, and never a name: the phrase whole inside one (`name_runs`,
    each name's words — "north gatehouse"), or every word of it that says
    which a name's ("Captain Coldiron"). A word a name merely shares is no
    reason: "north wall" is not North Gatehouse, and dropping it left
    fleet8-bram T18's sighting one keyword short. The phrase, or None."""
    words = [w for w in _words(keyword) if w not in ("the", "a", "an")]
    if len(words) < 2 or any(_word_forms(w) & common_forms for w in words):
        return None
    phrase = " ".join(words)
    if any(re.search(rf"\b{re.escape(phrase)}\b", run) for run in name_runs or ()):
        return None
    which = _distinctive(keyword)
    if which and all(_word_forms(w) & name_forms for w in which):
        return None
    return phrase


def _phrase_said(phrase: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}\b", " ".join(_words(text))) is not None


# fleet8-bram T13: "notice board" was told by "A board creaks somewhere above
# the stalls … like a man noticing a draft" — half its words, a paragraph
# apart — and, Sorrel speaking, a beat stalled after three nags was marked
# delivered on it. A keyword of several words is told WHOLE only with its
# words together (within EVIDENCE_WINDOW words of each other, any order:
# "the board's nailed notice"); half of them anywhere is a partial hit,
# which can second a whole one but never stand first. Replayed over every
# fleet transcript (fleet-fleet8): requiring every keyword whole lost true
# tellings in other words ("the howls keep a schedule" for "charted howls",
# fleet8-bram T9; "the horses loose on the north road" for "lame horse",
# fleet6-wenna T9); this way the board alone tells nothing.
EVIDENCE_WINDOW = 6


def _together(words: list[str], token_forms: list[set[str]]) -> bool:
    """Do all of `words` land within EVIDENCE_WINDOW words of each other in a
    text (`token_forms`: each of its words' forms, in order)? One word:
    anywhere."""
    spots: list[list[int]] = []
    for word in words:
        forms = _word_forms(word)
        at = [i for i, token in enumerate(token_forms) if token & forms]
        if not at:
            return False
        spots.append(at)
    if len(spots) == 1:
        return True
    starts = sorted({i for at in spots for i in at})
    return any(all(any(start <= i <= start + EVIDENCE_WINDOW for i in at) for at in spots) for start in starts)


# --- A beat told in two presses ---------------------------------------------
# fleet8-bram T18: Sorrel told the man in the ash-gray cloak by the north
# wall — "He whistled … and the howling answered him" — and "What Sorrel Saw"
# stayed undelivered for three nags: its "Watchman" is what "Sorrel won't
# give more than that without being pushed twice; on a second press he
# adds…" holds back. fleet8-wenna's Evening Howl ("even pressed he'll only
# admit the man was 'listening'") the same. A keyword only the later press
# says is no evidence the first was told: the beat is delivered on its first
# stage, and the second is the player's to win.
_SECOND_STAGE = re.compile(
    r"\b(?:won['’]?t|will\s+not|doesn['’]?t|does\s+not|refuses\s+to)\s+(?:say|give|tell|admit|offer|share)"
    r"\s+(?:any\s+|much\s+)?more"
    r"|\b(?:on|at|after)\s+a\s+second\s+(?:press|push|ask|asking|try|visit|meeting)"
    r"|\bif\s+(?:\w+\s+){0,3}?(?:press(?:es|ed)?|push(?:es|ed)?|ask(?:s|ed)?)\s+(?:\w+\s+){0,2}?"
    r"(?:again|twice|harder|a\s+second\s+time)\b"
    r"|\beven\s+(?:when\s+|if\s+)?pressed\b",
    re.IGNORECASE,
)


def later_keywords(directive: str | None, keywords: Iterable[str]) -> list[str]:
    """The keywords no evidence of the beat's first telling: those only its
    second stage says (see above) — named after its "won't say more / on a
    second press" and nowhere before it — and those only the tell before an
    "if asked … admits" says (`setup_keywords`, round 9)."""
    keywords = [str(k) for k in keywords or [] if str(k).strip()]
    match = _SECOND_STAGE.search(directive or "")
    later = []
    if match and keywords:
        before, after = _text_forms(directive[:match.start()]), _text_forms(directive[match.start():])
        for keyword in keywords:
            words = _distinctive(keyword) or [w for w in _words(keyword) if len(w) >= _MIN_WORD]
            if not words:
                continue
            in_after = all(_word_forms(w) & after for w in words)
            in_before = any(_word_forms(w) & before for w in words)
            if in_after and not in_before:
                later.append(keyword)
    return later + [k for k in setup_keywords(directive, keywords) if k not in later]


# Round 9, fleet10-wenna T6-T7: "A Tell at the Stable" — "keeps glancing
# toward the north wall. If asked what's on his mind, he admits he saw a man
# in an ash-gray cloak…" — was marked told on "Sorrel" and "north wall", the
# tell alone; her exact question the next reply got "I haven't seen
# anything", and the cloak was never said. In a directive shaped "if asked …
# admits X", what only the tell before the asking says is no evidence: only
# the admission's own words tell it (`later_keywords`), and when the player
# asks, the admission is due (`asked_note`). Not when the asking is a second
# press ("if pressed a second time, harder, he admits": the first stage is
# the beat, `_SECOND_STAGE`), when someone resists first ("deflects if
# pressed with '…' — but his eyes go…"), nor when the beat tells its news
# unasked before ("he tells Bram plainly…; if pressed on why, Vane only
# says…"). Over the fleet campaigns: 19 directives match the loose shape,
# 6 keep it — each a tell, then a confession asked for (see the round-9
# tests).
_ADMITS = re.compile(
    r"(?i:\b(?:if|when|once)\s+(?:asked|pressed|pushed|questioned|prompted|coaxed))\b"
    r"(?P<window>[^.;!?—–]{0,80}?)"
    r"(?:\b(?i:he|she|they)|\b[A-Z][\w'’-]+)\s+(?i:(?:only|finally|quietly|reluctantly)\s+)?"
    r"(?i:(?P<verb>admits|confesses|says|tells|reveals|lets\s+slip|explains|describes|whispers|mentions"
    r"|blurts(?:\s+out)?))\b",
)
_AGAIN = re.compile(r"\b(?:second|again|twice|harder|further|more)\b", re.IGNORECASE)
_TELLS_UNASKED = re.compile(r"\b(?:tells?|says?|admits?|explains?|reveals?|confess(?:es)?|warns?)\b", re.IGNORECASE)
ADMISSION_MIN_WORDS = 3


def admission(directive: str | None) -> tuple[str, str] | None:
    """(the tell before the asking, what is admitted) for a directive shaped
    "if asked … admits X" (see above) — the admission up to a second press
    or its sentence's end — else None."""
    match = _ADMITS.search(directive or "")
    if not match or _AGAIN.search(match.group(0)) or _RESISTS.search(match.group("window") or ""):
        return None
    setup = directive[:match.start()]
    if _TELLS_UNASKED.search(setup) or _RESISTS.search(setup):
        return None
    payload = directive[match.end():]
    later = _SECOND_STAGE.search(payload)
    if later:
        payload = payload[:later.start()]
    end = re.search(r"(?<=[.!?])\s", payload)
    payload = payload[:end.start()] if end else payload
    # "…gone before he could call out — and he [won't say more]"
    payload = re.sub(r"(?:\s*[—–,;]\s*)?\b(?:and|but|though)?\s*\b(?:he|she|they|it)?\s*$", "", payload)
    payload = payload.strip(" —–-,;:").strip()
    return (setup, payload) if len(_words(payload)) >= ADMISSION_MIN_WORDS else None


def setup_keywords(directive: str | None, keywords: Iterable[str]) -> list[str]:
    """The keywords the tell before an "if asked … admits" says, whole there
    (see above) — even when the admission says them again: the glance shows
    them, so saying them is no sign anything was admitted (round-9 review:
    fleet9-wenna's "A Face at the Wall" — "keeps glancing toward the north
    wall … admits … a man … crouched right at the north wall" — was told
    by "Sorrel" and "north wall" exactly as wenna's stable beat was). When
    nothing else is evidence, `_first_stage` keeps them all."""
    shaped = admission(directive)
    if shaped is None:
        return []
    setup, _payload = shaped
    out = []
    for keyword in (str(k).strip() for k in keywords or [] if str(k).strip()):
        # A name is no evidence anyway, and it may still second one
        # (`evidence_told`'s name support): who a beat is about stays
        if keyword[:1].isupper():
            continue
        phrase = " ".join(_words(keyword))
        if phrase and _phrase_said(phrase, setup):
            out.append(keyword)
    return out


def _first_stage(keywords: list[str], later: Iterable[str], known: Iterable[str],
                 common: Iterable[str]) -> list[str]:
    """`keywords` without the second stage's — unless that leaves nothing that
    is evidence (a name only): then the second stage IS the beat's news."""
    later = set(later or ())
    first = [k for k in keywords if k not in later]
    if len(first) == len(keywords):
        return keywords
    name_forms = _text_forms(" ".join(str(k) for k in known if k))
    runs = [" ".join(_words(str(k))) for k in known if k]
    common_forms = set(common or ())
    if any(_evidence_words(k, name_forms, common_forms) or _evidence_phrase(k, name_forms, common_forms, runs)
           for k in first):
        return first
    return keywords


def evidence_told(keywords: Iterable[str], text: str, known: Iterable[str] = (),
                  common: Iterable[str] = (), *, min_hits: int = 2,
                  fallback: bool = True, name_support: bool = False) -> tuple[bool, list[str]]:
    """Did `text` say what these keywords say, names and the campaign's
    common words aside? The evidence keywords (those with a word left) must
    land — at least `min_hits` of them (or all, when fewer exist) and at
    least half, one of them whole — a keyword of several words is whole
    with its words together (`_together`), and half of them is a hit that
    only seconds. With `name_support` (a FIRED beat: the DM was told to say
    it), its person named or speaking stands in for ONE of those — never
    for the first: fleet4-wenna T2 and fleet5-tamsin T3 were Piper showing
    her chart in other words ("notebook", "a pattern"), and two distinctive
    hits would have re-sent both as undelivered; fleet6-bram T14 said none.
    Keywords with no evidence at all fall back to the plain half-of-them
    rule (`fallback`), or tell nothing. Returns (told, the evidence
    keywords hit)."""
    keywords = [str(k) for k in keywords or [] if str(k).strip()]
    known = [str(k) for k in known or () if k]
    name_forms = _text_forms(" ".join(known))
    name_runs = [" ".join(_words(k)) for k in known]
    common_forms = set(common or ())
    evidence: dict[str, list[str] | str] = {}
    for k in keywords:
        words = _evidence_words(k, name_forms, common_forms)
        phrase = None if words else _evidence_phrase(k, name_forms, common_forms, name_runs)
        if words or phrase:
            evidence[k] = words or phrase
    if not evidence:
        if not fallback:
            return False, []
        hits = mentioned_keywords(keywords, text)
        return bool(keywords) and len(hits) * 2 >= len(keywords), hits
    token_forms = [_word_forms(w) for w in _words(text)]
    forms = set().union(*token_forms) if token_forms else set()
    whole = {k for k, words in evidence.items()
             if (_phrase_said(words, text) if isinstance(words, str) else _together(words, token_forms))}
    hits = [k for k, words in evidence.items()
            if k in whole or (not isinstance(words, str) and len(words) > 1
                              and sum(1 for w in words if _word_forms(w) & forms) * 2 >= len(words))]
    said, pool = len(hits), len(evidence)
    # ...unless it is the beat's only evidence: nothing whole could second it
    # (fleet-bram T8's "Into the Hollow": "whistle-note", the notches on the
    # birch shown and the word never said)
    whole = whole or (pool == 1 and bool(hits))
    if whole and said >= min(min_hits, pool) and said * 2 >= pool:
        return True, hits
    if name_support and whole:
        named = [k for k in keywords if k not in evidence
                 and (words := _distinctive(k)) and all(_word_forms(w) & name_forms for w in words)]
        if named and mentioned_keywords(named, text):
            said, pool = said + 1, pool + 1
            return said >= min(min_hits, pool) and said * 2 >= pool, hits
    return False, hits


# --- Sealed truths said aloud ---------------------------------------------
# fleet4-tamsin T19: Maera said "Twenty names on his roster... eleven of his
# men" — the SEALED truth, word for word — and nothing noticed, because the
# old check wanted EVERY spoiler keyword ("coldiron" too; she said "his") and
# read only the prose. Now any TWO distinctive terms do, across prose and
# speech — said together, in one speech line or one paragraph. A term is a
# spoiler keyword or a number the statement states (the number is usually
# the secret: eleven, not twenty).
#
# What is NOT evidence: any name in the world — a person, present or not
# ("Coldiron" names the man, not his lie; "Hesk might know" proves nothing,
# fleet-wenna T18), a place, a faction, the player's own — and what the
# world says openly (its situation, the act's premise, the text of quests
# the player took, truths already out); everyday words the generator still
# writes as "spoilers" ("toll" + "pay" fired on "the wolves pay the toll for
# me", fleet4-tamsin T7); small counts ("two nights back", "three weeks");
# and terms strewn across a reply ("a thin roster" ... "eleven nights" —
# fleet2-wenna T1). Validated on all eleven fleet transcripts: the two true
# reveals caught (fleet3-tamsin T20, Coldiron's own confession; fleet4-tamsin
# T19, Maera), no false ones. A paraphrase still slips by ("a name struck
# off a roster" for "drummed out", fleet4-tamsin T8).

TRUTH_LEAK_TERMS = 2
_NUMBER_WORDS = frozenset({
    "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety", "hundred", "hundreds", "thousand", "thousands", "dozen", "dozens",
})
_MIN_DIGITS = 5
# Words that are everywhere in a story about anything: a term made only of
# these (after the player's known words) is no evidence of a secret
_COMMON_WORDS = frozenset({
    "pay", "paid", "pays", "owe", "owed", "debt", "coin", "silver", "gold", "money",
    "dead", "death", "die", "died", "kill", "killed", "taken", "take", "took",
    "lie", "lies", "lied", "liar", "truth", "true", "real", "secret", "secrets",
    "man", "men", "woman", "name", "names", "town", "road", "gate", "wall",
    "post", "command", "order", "orders", "watch", "guard", "guards", "people", "body",
    "week", "weeks", "month", "months", "year", "years", "time", "way", "place",
})


def truth_terms(truth: dict[str, Any]) -> list[str]:
    """The words that would give a truth away: its spoiler keywords plus every
    number its statement states. Lower-cased, de-duplicated, in order."""
    terms: list[str] = []
    for raw in [*(truth.get("spoiler_keywords") or []),
                *[w for w in _words(str(truth.get("statement") or ""))
                  if w in _NUMBER_WORDS or (w.isdigit() and int(w) >= _MIN_DIGITS)]]:
        term = " ".join(str(raw).split()).lower()
        if term and term not in terms:
            terms.append(term)
    return terms


def _term_words(term: str, known_forms: set[str]) -> list[str]:
    """The words of a term that still say something: distinctive, not a word
    the player already knows, not an everyday word. Empty = no evidence.

    A term that lost half its words or more to what the player KNOWS says
    nothing on what is left: its meaning was the pairing with the known
    thing. fleet7-wenna T15 un-sealed the act-3 cost truth on day 1 from
    "goes down … chokes into silence" — "silence the whistle" (whistle
    known) and "vane taken down" (a name, an everyday word) left "silence"
    and "down". An everyday word never carried a term's meaning, so losing
    one keeps the rest: "roster lie" is still "roster" (fleet3-tamsin T20,
    Coldiron's own confession). See tests/test_campaign_evidence.py."""
    distinctive = _distinctive(term)
    words = [w for w in distinctive if w not in _COMMON_WORDS and not (_word_forms(w) & known_forms)]
    lost_to_known = any(w not in _COMMON_WORDS and _word_forms(w) & known_forms for w in distinctive)
    if lost_to_known and len(words) * 2 <= len(distinctive):
        return []
    return words


def truth_leak_terms(truth: dict[str, Any], text: str,
                     known: Iterable[str] = (), *,
                     known_forms: set[str] | None = None) -> list[str]:
    """The terms of `truth` that `text` says, what the player knows aside.

    Stricter than beat keywords: every remaining word of a term must be
    there ("counting-house silver" is not just "silver"), because one stray
    word must never un-seal a story truth. `truth` may carry precomputed
    `terms` (the audit's copy) instead of keywords + statement; `known_forms`
    is `known` already stemmed (callers looping over lines pass it)."""
    if known_forms is None:
        known_forms = _text_forms(" ".join(str(k) for k in known if k))
    forms = _text_forms(text)
    hits: list[str] = []
    for term in (truth.get("terms") or truth_terms(truth)):
        words = _term_words(term, known_forms)
        if words and all(_word_forms(w) & forms for w in words):
            hits.append(term)
    return hits


def _units(text: str) -> list[str]:
    """One paragraph of prose or one speech line each: terms said together."""
    return [u for u in re.split(r"\n+", text or "") if u.strip()]


def sealed_truth_hits(truths: Iterable[dict[str, Any]], text: str,
                      known: Iterable[str] = (), *,
                      speech: Iterable[tuple[str, str]] | None = None,
                      player_words: str = "") -> dict[str, list[str]]:
    """{truth id: the terms said} for every still-sealed truth that one
    paragraph or speech line of `text` gives away (TRUTH_LEAK_TERMS distinct
    terms or more, together) — or that a unit spread thin across several
    truths names (`_spread_thin`), or whose own subject says its number
    (`speech`: (npc id, line) pairs; see `_subject_counts`). `player_words`
    (the player's input) are no evidence for those two."""
    known_forms = _text_forms(" ".join(str(k) for k in known if k))
    units = _units(text)
    hits: dict[str, list[str]] = {}
    sealed = [t for t in truths or [] if isinstance(t, dict) and not t.get("revealed")]
    for truth in sealed:
        best: list[str] = []
        for unit in units:
            said = truth_leak_terms(truth, unit, known_forms=known_forms)
            if len(said) > len(best):
                best = said
        if len(best) >= TRUTH_LEAK_TERMS:
            hits[str(truth.get("id"))] = best
    banned = _plain_words(player_words)
    # A reply that tells one truth in full is about that one: its stray
    # terms elsewhere spend no second truth (fleet3-tamsin T20)
    for unit in units if not hits else []:
        for truth_id, said in _spread_thin(sealed, unit, known_forms, banned).items():
            hits.setdefault(truth_id, said)
    for truth_id, said in _subject_counts(sealed, speech or (), known_forms, banned).items():
        hits.setdefault(truth_id, said)
    return hits


# --- Round 5: leaks spread thin, and the subject's own count ----------------
# fleet6-wenna T17: a letter the DM wrote gave away three sealed truths at
# once ("…ask who's paying the toll, and ask why the Watch roster never
# changes") — one term each, so the two-terms-of-one-truth rule saw nothing.
# A unit that names two or more sealed truths, each by a word no other of
# them shares, gives each away — unless it tells one of them in full (then
# it is about that one, and a stray "whistle" beside Coldiron's confession
# is not a second truth: fleet3-tamsin T20). fleet-bram T18, fleet3-tamsin
# T14-T15: Coldiron, the subject of the roster truth, said "eleven" in his
# own line ("I've been fighting for it with eleven —"): a count said by the
# person the truth is about is the truth. Only counts: replayed over every
# fleet transcript, single nouns in a subject's mouth were wrong as often as
# right ("hand me the whistle", "I don't patrol fences", "a thin roster",
# fleet2-wenna T1), and "drove that road eleven years" is no headcount.
# Both rules match words exactly (a plural aside: "good word" is not
# "goods", "close enough" is not "closed"), never the player's own words,
# and never a campaign-wide common word (a truth's `cross_terms`, when the
# caller precomputed them).
_NOT_A_COUNT = frozenset({
    "year", "years", "day", "days", "night", "nights", "week", "weeks", "month", "months",
    "hour", "hours", "minute", "minutes", "bell", "bells", "o'clock", "season", "seasons",
    "winter", "winters", "summer", "summers", "mile", "miles", "pace", "paces", "step", "steps",
    "yard", "yards", "feet", "foot", "coin", "coins", "silver", "crown", "crowns", "times",
})


def _plain_words(text: str) -> set[str]:
    """`text`'s words as written, plus each one's singular."""
    words = set(_words(text or ""))
    return words | {w[:-1] for w in words if w.endswith("s") and not w.endswith("ss") and len(w) > 3}


def _is_count(term: str) -> bool:
    return term in _NUMBER_WORDS or (term.isdigit() and int(term) >= _MIN_DIGITS)


def _counted(term: str, text: str) -> bool:
    """Is a number term said as a count of something — not "eleven years",
    "twenty coin", "twenty years of border work"?"""
    lowered = (text or "").lower().replace("’", "'")
    for match in re.finditer(rf"(?<![\w'-]){re.escape(term)}(?![\w'-])", lowered):
        after = re.match(r"[\s-]*([a-z']+)", lowered[match.end():])
        if not after or after.group(1) not in _NOT_A_COUNT:
            return True
    return False


# A statement with two counts holds the secret AND its cover story: "a roster
# of twenty able bodies is a lie; he has eleven" (every fleet campaign's roster
# truth). Coldiron saying "twenty men on that wall" keeps the lie — it tells
# nothing — so the single-word rules never count the cover number. The told
# count is the one a word just before marks ("only eleven", "instead of
# eleven"; "he has eleven" too, unless a claim word is there — "lists",
# "having"); one right after "not" is the claim ("eleven, not twenty").
# When that marks no count, or more than one, none counts alone.
_TOLD_COUNT_CUES = frozenset({
    "only", "just", "but", "instead", "actually", "really", "barely", "mere",
    "remain", "remains", "exist", "exists", "commands",
})
_WEAK_COUNT_CUES = frozenset({"has", "have", "had", "are", "were"})
_CLAIM_COUNT_CUES = frozenset({
    "lists", "list", "listed", "claims", "claimed", "says", "said", "thinks", "believes", "boasts",
    "having", "paper",
})
_TOLD_COUNT_WINDOW = 3      # words before the count


def _cover_counts(statement: str) -> set[str]:
    """The counts a truth's statement gives as its cover story (see above)."""
    words = _words(statement or "")
    counts = [(i, w) for i, w in enumerate(words) if _is_count(w)]
    distinct = {w for _, w in counts}
    if len(distinct) < 2:
        return set()

    def told(i: int) -> bool:
        before = set(words[max(0, i - _TOLD_COUNT_WINDOW):i])
        if i and words[i - 1] == "not":
            return False
        return bool(before & _TOLD_COUNT_CUES) or bool(before & _WEAK_COUNT_CUES and not before & _CLAIM_COUNT_CUES)

    marked = {w for i, w in counts if told(i)}
    return distinct - marked if len(marked) == 1 else distinct


def _cross_terms(truth: dict[str, Any], common: Iterable[str] = ()) -> list[str]:
    """The terms the spread-thin and subject rules may count: never a
    campaign-wide common word (`campaign_common_words`), never a cover
    count (`_cover_counts`)."""
    common_forms = set(common or ())
    cover = _cover_counts(str(truth.get("statement") or ""))
    return [term for term in truth_terms(truth)
            if term not in cover and not any(_word_forms(w) & common_forms for w in _distinctive(term))]


def _exact_terms(truth: dict[str, Any], unit: str, known_forms: set[str], banned: set[str]) -> list[str]:
    """The terms of `truth` that `unit` says word for word (see above)."""
    words = _plain_words(unit)
    if truth.get("cross_terms") is not None:
        terms = truth["cross_terms"]
    else:
        cover = _cover_counts(str(truth.get("statement") or ""))
        terms = [t for t in (truth.get("terms") or truth_terms(truth)) if t not in cover]
    out: list[str] = []
    for term in terms:
        term_words = _term_words(term, known_forms)
        if not term_words or not all(w in words for w in term_words) or any(w in banned for w in term_words):
            continue
        if _is_count(term) and not _counted(term, unit):
            continue
        out.append(term)
    return out


def _spread_thin(sealed: list[dict[str, Any]], unit: str, known_forms: set[str],
                 banned: set[str]) -> dict[str, list[str]]:
    """{truth id: terms} for a unit naming 2+ sealed truths, each by a word
    of its own, none of them in full (see above)."""
    said: dict[str, list[str]] = {}
    for truth in sealed:
        if len(truth_leak_terms(truth, unit, known_forms=known_forms)) >= TRUTH_LEAK_TERMS:
            return {}
        terms = _exact_terms(truth, unit, known_forms, banned)
        if terms:
            said[str(truth.get("id"))] = terms
    if len(said) < 2:
        return {}
    words = {tid: {w for term in terms for w in _term_words(term, known_forms)} for tid, terms in said.items()}
    own = [tid for tid, mine in words.items()
           if mine - set().union(*(theirs for other, theirs in words.items() if other != tid))]
    return {tid: said[tid] for tid in own} if len(own) >= 2 else {}


def _subject_counts(sealed: list[dict[str, Any]], speech: Iterable[tuple[str, str]],
                    known_forms: set[str], banned: set[str]) -> dict[str, list[str]]:
    """{truth id: the counts} its own subject (`subjects`: npc ids the
    statement names) said in their own line (see above)."""
    hits: dict[str, list[str]] = {}
    for npc_id, line in speech:
        if not npc_id or not line:
            continue
        for truth in sealed:
            if str(npc_id) not in {str(s) for s in truth.get("subjects") or ()}:
                continue
            counts = [t for t in _exact_terms(truth, line, known_forms, banned) if _is_count(t)]
            if counts:
                hits.setdefault(str(truth.get("id")), counts)
    return hits


# --- Round 8: one word of a sealed thing ----------------------------------
# fleet9-tamsin T18: the Captain said "that whistle" — a spoiler keyword of
# the villain truth and the proof object of an armed beat — and two terms
# were never said together, so the two-term rule could not see it. One word
# is enough when it names a THING a sealed truth keeps (its statement says
# "a bone whistle", "the Watch roster", "the carved word SING") and the
# player has never met it: not on screen before (their heard words and
# journal, their quest log, the world's open text, an earlier reply — the
# truth's `seen_words`), not in this reply's prose, not in their own words.
# A verb or a quality ("drilled", "admits", "recognizes") is no thing, nor a
# name, a count (the counts rule above has those), a hyphenated or
# two-word keyword (the two-term rule reads those), or a word the whole
# campaign is written in. tamsin's own T18 is NOT one: the main quest's
# fourth step ("Bring the cracked bone whistle back to Captain Coldiron as
# proof") had been on her quest log since T3. Replay over every fleet
# transcript in tests/test_sealed_words_round8.py's docstring.
# A thing is written after a determiner or a possessive, with at most two
# modifiers between — never a verb or an adverb ("the Watch publicly
# admits", "the Fangs are both staged" are no things)
_THING_CUE = re.compile(
    r"(?:\b(?:a|an|the|his|her|their|its|this|that|same|one|own|whose)\s+|[\w-]+['’]s?\s+)"
    r"(?:(?!(?:is|are|was|were|be|been|being|has|have|had|will|would|can|could|both|all|also|not|no|who|which"
    r"|that)\b)(?![\w'’-]*ly\s)[\w'’-]+\s+){0,2}$",
    re.IGNORECASE,
)
# People are told by name or role, never "a thing the truth keeps": a
# truth's "its driver is dead" made every "Drivers live" of the opening a
# leak (fleet5-tamsin T0, fleet5-wenna T1)
_PERSON_KEYWORDS = frozenset({
    "driver", "carter", "rider", "scout", "sergeant", "captain", "predecessor", "successor", "collaborator",
    "informant", "traitor", "deserter", "guard", "watchman", "keeper", "hawker", "herbalist", "apprentice",
    "smith", "man", "woman", "boy", "girl", "child", "stranger", "leader", "master", "partner", "brother",
    "sister", "father", "mother", "son", "daughter", "wife", "husband", "friend", "enemy",
})
# ...nor a word everyone says about anything: fleet8-wenna's cost truth kept
# "terms", fleet9-wenna's "numbers", fleet4-wenna's "pattern" (the act-1
# clue of the howls itself), fleet7-tamsin's "cargo" (every taken cart's) —
# one of those in the first mouth to say it is no slip (review, round 8)
_ABSTRACT_THINGS = frozenset({
    "terms", "numbers", "number", "pattern", "patterns", "names", "name", "words", "word", "price", "cost",
    "costs", "deal", "debt", "favor", "favour", "cargo", "goods", "help", "plan", "plans", "orders", "order",
    "rules", "truth", "lie", "lies", "story", "news", "trouble", "reason", "matter", "count", "counts", "stage",
})


def sealed_object_terms(truths: Iterable[dict[str, Any]], *, names: Iterable[str] = (),
                        common: Iterable[str] = (), open_text: str = "") -> dict[str, list[str]]:
    """{truth id: its one-word keywords that name a thing} for every still-
    sealed truth (see above). `names`: every name in the world (a word of
    one is no secret); `common`: `campaign_common_words`; `open_text`: what
    the world says openly (`open_world_text`) — a word the places, looks
    and lore use is an everyday word there (the stable yard's "paddock
    fence", fleet2-bram T3; the Fangs' "bone whistles")."""
    name_forms = _text_forms(" ".join(str(n) for n in names if n))
    common_forms = set(common or ()) | _text_forms(open_text or "")
    out: dict[str, list[str]] = {}
    for truth in truths or []:
        if not isinstance(truth, dict) or truth.get("revealed"):
            continue
        statement = str(truth.get("statement") or "")
        words: list[str] = []
        for keyword in truth.get("spoiler_keywords") or []:
            distinctive = _distinctive(str(keyword))
            if len(distinctive) != 1 or len(_words(str(keyword))) != 1:
                continue
            word = distinctive[0]
            forms = _word_forms(word)
            if _is_count(word) or word in _COMMON_WORDS or forms & name_forms or forms & common_forms:
                continue
            # a deed or a state ("drilled", "matched", "kenneled") is no thing
            if word in _PERSON_KEYWORDS or word in _ABSTRACT_THINGS \
                    or (len(word) > 5 and word.endswith(("ed", "ing"))):
                continue
            for m in re.finditer(rf"(?<![\w'’-]){re.escape(word)}(?:e?s)?(?![\w'’-])", statement, re.IGNORECASE):
                # "Greywater" is a town, not a thing kept; "SING" is carved
                written = m.group(0)
                proper = written[:1].isupper() and not written.isupper()
                if not proper and _THING_CUE.search(statement[:m.start()]):
                    words.append(word)
                    break
        if words:
            out[str(truth.get("id"))] = list(dict.fromkeys(words))
    return out


_OPEN_NPC_FIELDS = ("profession", "description_physical", "description_personality", "voice_pattern", "goals")
_OPEN_FACTION_FIELDS = ("ideology", "methods", "aesthetic", "goals_short", "goals_long")
_OPEN_BIBLE_FIELDS = ("tagline", "setting_description", "current_situation", "major_conflicts", "faction_overview")


def open_world_text(session) -> str:
    """What the world says openly, as one text: every person's trade, looks,
    manner and wants; every place's description and its Things' labels
    (never what they read or hold); the factions' faces; the bible's lore.
    Never a secret, a truth or a beat. Empty on any trouble."""
    from src.models import WorldBible

    parts: list[str] = []
    try:
        for npc in session.query(NPC).all():
            parts += [str(getattr(npc, f, "") or "") for f in _OPEN_NPC_FIELDS]
        for place in session.query(Location).all():
            parts.append(str(place.description or ""))
            parts += [str(o.get("label") or "") for o in (place.objects or []) if isinstance(o, dict)]
        for faction in session.query(Faction).all():
            parts += [str(getattr(faction, f, "") or "") for f in _OPEN_FACTION_FIELDS]
        bible = session.query(WorldBible).first()
        if bible is not None:
            parts += [str(getattr(bible, f, "") or "") for f in _OPEN_BIBLE_FIELDS]
    except Exception as e:
        logger.debug(f"open world text unreadable: {e}")
        return ""
    return "\n".join(p for p in parts if p and p.strip())


def sealed_word_slips(probes: dict[str, list[str]], speech: Iterable[tuple[str, str, str]],
                      known: Iterable[str] = (), *, exempt_ids: Iterable[str] = ()) -> list[dict[str, str]]:
    """[{speaker, npc_id, word, truth_id, line}] for each thing-word of a
    sealed truth (`sealed_object_terms`) a speaker line says that nothing in
    `known` (what the player has met — see above) holds, once per word.
    `speech`: (speaker, npc id, line); `exempt_ids`: people free to say it
    (a truth's own person whom trust has opened)."""
    known_forms = _text_forms(" ".join(str(k) for k in known if k))
    exempt = {str(i) for i in exempt_ids or () if i}
    out: list[dict[str, str]] = []
    slipped: set[str] = set()
    for speaker, npc_id, line in speech or ():
        if not line or (npc_id and str(npc_id) in exempt):
            continue
        said = _plain_words(line)
        for truth_id, words in (probes or {}).items():
            for word in words:
                if word in slipped or word not in said or _word_forms(word) & known_forms:
                    continue
                slipped.add(word)
                out.append({"speaker": str(speaker or "Someone"), "npc_id": str(npc_id or ""),
                            "word": word, "truth_id": str(truth_id), "line": str(line)})
    return out


def _heard_before(player_id: str) -> str:
    """Everything the player heard or journaled before this turn
    (`dialogue.heard_corpus`). "" on any trouble."""
    try:
        from src.services.dialogue import heard_corpus

        return heard_corpus(player_id)
    except Exception as e:
        logger.debug(f"heard words unread for {player_id}: {e}")
        return ""


def _unmet_sealed_words(session, player, unrevealed: list[dict[str, Any]], people: list[NPC],
                        common: Iterable[str], known_words: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """({truth id: thing-words the player has not met}, ids free to say
    them) for the tick's audit copy (see above). Met: what the player knows
    (`_known_vocabulary`) and heard or journaled (`_heard_before`, in
    `known_words`), what reached their screen before (each truth's
    `seen_words`). Free: a truth's own person whom trust has opened
    (standing.SECRET_TRUST). Never raises."""
    if not unrevealed or player is None:
        return {}, []
    try:
        names = [n.name for n in people if n.name]
        names += [name for (name,) in session.query(Location.name).all() if name]
        names += [name for (name,) in session.query(Faction.name).all() if name]
        probes = sealed_object_terms(unrevealed, names=names, common=common, open_text=open_world_text(session))
        if not probes:
            return {}, []
        met = list(known_words)
        met += [" ".join(str(w) for w in t.get("seen_words") or []) for t in unrevealed]
        met_forms = _text_forms(" ".join(m for m in met if m))
        unmet = {tid: [w for w in words if not (_word_forms(w) & met_forms)] for tid, words in probes.items()}
        unmet = {tid: words for tid, words in unmet.items() if words}
        holders: set[str] = set()
        if unmet:
            from src.services import standing

            for truth in unrevealed:
                if str(truth.get("id")) not in unmet:
                    continue
                for npc in _named_people(str(truth.get("statement") or ""), people):
                    rel = standing.relationship_for(session, player.id, npc.id)
                    if rel is not None and standing.trust_of(rel) >= standing.SECRET_TRUST:
                        holders.add(npc.id)
        return unmet, sorted(holders)
    except Exception as e:
        logger.debug(f"sealed words unread: {e}")
        return {}, []


def _note_seen_words(campaign: Campaign, player_id: str, text: str) -> None:
    """The thing-words of sealed truths this turn put on screen (its prose and
    its speech) are met from now on — said once, they are canon: the next
    reply builds on them, and nobody is flagged for saying them again."""
    audit = (_LAST_TICK.get(player_id) or {}).get("audit") or {}
    by_truth = audit.get("sealed_words") or {}
    if not by_truth or not (text or "").strip():
        return
    said = _plain_words(text)
    truths = [dict(t) if isinstance(t, dict) else t for t in (campaign.fixed_truths or [])]
    changed = False
    for truth in truths:
        if not isinstance(truth, dict):
            continue
        seen = list(truth.get("seen_words") or [])
        for word in by_truth.get(str(truth.get("id")), []):
            if word in said and word not in seen:
                seen.append(word)
                changed = True
        if seen:
            truth["seen_words"] = seen
    if changed:
        campaign.fixed_truths = truths


# --- Round 8: a sealed truth never reaches the screen early ----------------
# fleet9-bram T1: the player asked the Captain two questions, the DM rolled
# persuasion on them, the dice came up critical, and the Captain said "I've
# got eleven able bodies on the whole Watch roster" — the act-2 truth, in
# the first exchange, at trust 50 (the beat firing said "He does not mention
# the roster"); two act-2 beats were spent. Speech is now read line by line
# as it streams (`dialogue.NameGate.player_name_in`) by the rules that mark
# a truth revealed by play (`sealed_truth_hits`: two of its terms in one
# line, its own person saying its count, a line spread over several): the
# sentences that say it are cut before the bubble, the voice, the memory
# file and the journal take the line (all of it gone: SEAL_DEFLECTION), so
# the truth stays sealed, and the DM is told next turn (sealed_truth_cut).
# Never a truth the beat firing now reveals (the tick's `sealed_truths`
# leave it out), nor one the speaker confessed through an earned @reveal
# (a told secret of theirs says it). One-word slips (above) are audited,
# never cut: one noun is too thin to cut a line on.
SEAL_DEFLECTION = "That's all I'll say on it."
SEAL_SEEN_MAX = 400
_SEAL_CUTS: dict[str, tuple[int, list[dict[str, Any]]]] = {}   # player -> (reply, cuts)
_SEAL_SEEN: dict[str, set[str]] = {}                          # lines already cut (a replay re-cuts)
_SEAL_FORMS: dict[str, tuple[dict[str, Any], set[str]]] = {}   # player -> (audit copy, known forms)
_SEAL_COLD: dict[str, dict[str, Any]] = {}                     # player -> audit copy read with no tick
# One sentence with its closing marks and the space after it ("…roster.*")
_SEAL_SENTENCE = re.compile(r"[^.!?…]*(?:[.!?…]+[\"”’)*_]*|$)\s*")


def _stalled_reveals(beats: list[StoryBeat]) -> set[str]:
    """Truth ids a stalled beat reveals. A stalled beat was brought on (never
    told, or parked where it happens) and is delivered when play tells it
    (`mark_beat_delivery`): its truth is no front-run, and no line telling
    it is cut — the cut would leave the scene undeliverable until skipped."""
    return {str(i) for b in beats if b.status == "stalled" for i in (b.reveals_truth_ids or [])}


def _sealed_probes(unrevealed: list[dict[str, Any]], people: list[NPC],
                   common: Iterable[str]) -> list[dict[str, Any]]:
    """The audit's copy of each still-sealed truth (`_build_briefing`)."""
    return [
        {"id": t.get("id"), "spoiler_keywords": list(t.get("spoiler_keywords") or []),
         "terms": truth_terms(t),
         "cross_terms": _cross_terms(t, common),
         "subjects": [n.id for n in _named_people(str(t.get("statement") or ""), people)]}
        for t in unrevealed
    ]


def _seal_audit(player_id: str) -> dict[str, Any]:
    """The audit copy the seal guard reads: this process's last tick, else —
    a history reload after a restart, before any turn ran a tick — the same
    sealed truths and known words read from the campaign row (nothing fired,
    nothing written; kept until a tick replaces it). Without it a reload
    showed every line the stream had cut. Never raises ({})."""
    tick = _LAST_TICK.get(player_id)
    if tick is not None:
        return tick.get("audit") or {}
    cold = _SEAL_COLD.get(player_id)
    if cold is not None:
        return cold
    cold = {}
    try:
        with get_session() as session:
            campaign = get_campaign_row(session, player_id)
            if campaign is not None and campaign.status == "active":
                beats = _campaign_beats(session, campaign)
                open_scenes = {str(i) for b in beats if b.status == "fired"
                               for i in (b.reveals_truth_ids or [])} | _stalled_reveals(beats)
                unrevealed = [t for t in (campaign.fixed_truths or []) if isinstance(t, dict)
                              and not t.get("revealed") and str(t.get("id")) not in open_scenes]
                if unrevealed:
                    sealed = _sealed_probes(unrevealed, session.query(NPC).all(),
                                            campaign_common_words(campaign, beats))
                    cold = {"sealed_truths": sealed,
                            "known_words": _known_vocabulary(session, campaign, session.get(Player, player_id))}
    except Exception as e:
        logger.debug(f"sealed truths unread for the seal guard ({player_id}): {e}")
    _SEAL_COLD[player_id] = cold
    return cold


def _seal_known_forms(player_id: str, audit: dict[str, Any]) -> set[str]:
    cached = _SEAL_FORMS.get(player_id)
    if cached is not None and cached[0] is audit:
        return cached[1]
    forms = _text_forms(" ".join(str(k) for k in audit.get("known_words") or [] if k))
    _SEAL_FORMS[player_id] = (audit, forms)
    return forms


def _confessed(npc_id: str, player_id: str, truth: dict[str, Any], known_forms: set[str]) -> bool:
    """Did this speaker already confess `truth` through an earned @reveal
    (one of their told secrets says it)? Never raises (False)."""
    if not npc_id:
        return False
    try:
        with get_session() as session:
            npc = session.get(NPC, npc_id)
            rel = session.query(NPCRelationship).filter(NPCRelationship.npc_id == npc_id,
                                                        NPCRelationship.player_id == player_id).first()
            secrets = npc.secrets if npc is not None and isinstance(npc.secrets, list) else []
            told = {int(i) for i in (rel.revealed_secrets or []) if str(i).isdigit()} if rel is not None else set()
        for index in told:
            secret = str(secrets[index]) if 0 <= index < len(secrets) else ""
            if secret and (len(truth_leak_terms(truth, secret, known_forms=known_forms)) >= TRUTH_LEAK_TERMS
                           or _subject_counts([truth], [(npc_id, secret)], known_forms, set())):
                return True
    except Exception as e:
        logger.debug(f"confession check skipped for {npc_id}: {e}")
    return False


def seal_line(player_id: str | None, npc_id: str | None, text: str | None, *, speaker: str = "",
              player_input: str = "", whole: str = "") -> str | None:
    """`text`, one speaker line, as the player may hear it: without the
    sentences that say a still-sealed truth (see above). Reads the tick this
    turn ran (`_seal_audit`). The player's own words are theirs to hear
    back. `whole`: the line `text` is a part of (a long line streams as two
    bubbles, `dialogue_markup.split_long_speech`) — judged whole, so two
    terms either side of the split are still one telling; only this part's
    sentences are cut. Never raises: a line it cannot judge goes as written."""
    if not player_id or not text or not campaign_enabled():
        return text
    audit = _seal_audit(player_id)
    sealed = [t for t in audit.get("sealed_truths") or [] if isinstance(t, dict)]
    if not sealed:
        return text
    try:
        known_forms = _seal_known_forms(player_id, audit)
        typed = "" if str(player_input or "").lstrip().startswith("[") else str(player_input or "")
        if typed:
            known_forms = known_forms | _text_forms(typed)
        whole = whole if whole and text in whole else text
        hits = _line_hits(sealed, whole, npc_id, known_forms, typed)
        hits = {tid: terms for tid, terms in hits.items()
                if not _confessed(str(npc_id or ""), player_id,
                                  next(t for t in sealed if str(t.get("id")) == tid), known_forms)}
        if not hits:
            return text
        terms = sorted({term for said in hits.values() for term in said})
        probe = {"terms": terms}
        pieces = [m.group(0) for m in _SEAL_SENTENCE.finditer(text) if m.group(0).strip()]
        kept = [p for p in pieces if not truth_leak_terms(probe, p, known_forms=known_forms)]
        if len(kept) == len(pieces):
            return text             # the other part of a long line held it
        shown = "".join(kept).strip()
        if not re.search(r"\w", shown):
            shown = SEAL_DEFLECTION
        cut = [p.strip() for p in pieces if p not in kept]
        _note_seal_cut(player_id, npc_id, speaker, text, cut, sorted(hits), terms)
        logger.info(f"sealed truth cut from {speaker or npc_id or 'a voice'}'s line: {sorted(hits)} ({terms})")
        return shown
    except Exception as e:
        logger.warning(f"sealed-truth guard skipped a line: {e}")
        return text


def _line_hits(sealed: list[dict[str, Any]], line: str, npc_id: str | None, known_forms: set[str],
               typed: str) -> dict[str, list[str]]:
    """{truth id: terms} one speaker line gives away — `sealed_truth_hits`'
    rules, read with forms already stemmed."""
    hits: dict[str, list[str]] = {}
    for truth in sealed:
        said = truth_leak_terms(truth, line, known_forms=known_forms)
        if len(said) >= TRUTH_LEAK_TERMS:
            hits[str(truth.get("id"))] = said
    banned = _plain_words(typed)
    if not hits:
        hits.update(_spread_thin(sealed, line, known_forms, banned))
    for truth_id, said in _subject_counts(sealed, [(str(npc_id or ""), line)], known_forms, banned).items():
        hits.setdefault(truth_id, said)
    return hits


def _note_seal_cut(player_id: str, npc_id: str | None, speaker: str, line: str, cut: list[str],
                   truth_ids: list[str], terms: list[str]) -> None:
    """Keep a cut for this reply's audit — once: a history replay re-cuts the
    same stored line and must not report it again."""
    from src.services import stakes

    key = f"{npc_id or ''}|{line}"
    seen = _SEAL_SEEN.setdefault(player_id, set())
    if key in seen:
        return
    if len(seen) >= SEAL_SEEN_MAX:
        seen.clear()
    seen.add(key)
    reply = stakes.current_turn(player_id)
    tag, cuts = _SEAL_CUTS.get(player_id, (reply, []))
    if tag != reply:
        cuts = []
    cuts.append({"speaker": speaker or "Someone", "npc_id": npc_id or "", "cut": " ".join(cut),
                 "truth_ids": truth_ids, "terms": terms})
    _SEAL_CUTS[player_id] = (reply, cuts)


def pop_seal_cuts(player_id: str | None) -> list[dict[str, Any]]:
    """The lines this reply's sealed-truth guard cut ({speaker, npc_id, cut,
    truth_ids, terms}), once; [] for another reply's."""
    if not player_id:
        return []
    from src.services import stakes

    tag, cuts = _SEAL_CUTS.pop(player_id, (None, []))
    return cuts if tag == stakes.current_turn(player_id) else []


def _played_keywords(keywords: Iterable[str], text: str,
                     known: Iterable[str] = (), common: Iterable[str] = ()) -> tuple[bool, list[str]]:
    """Did play say what an unfired beat would have said? The fired-beat
    match (`evidence_told`: names and the campaign's common words are
    context — a beat about Sorrel names Sorrel, so "Sorrel" proves nothing),
    with no fallback: at least two evidence keywords, and at least half of
    them, must land. Returns (delivered, the keywords hit)."""
    told, hits = evidence_told(keywords, text, known, common, fallback=False)
    return told and len(hits) >= 2, hits


def mark_beat_delivery(player_id: str, turn_text: str,
                       speakers: list[str] | None = None,
                       spoken: list[dict[str, Any]] | None = None, *,
                       player_input: str | None = None,
                       roll_outcomes: Iterable[str] = (),
                       addressed_npc_id: str | None = None) -> dict[str, Any]:
    """Did the DM actually deliver the fired beat? Loose keyword check, no LLM.

    Called post-turn (before the audit, so a delivered beat is never flagged).
    `speakers` are the names on this turn's speaker lines: a beat that brings
    an NPC on stage is delivered when they speak, named in the prose or not.
    `spoken` (optional) are the speech notifications themselves ({npc_id,
    npc_name, text}): who spoke, by id, and whose words a revealed truth
    was. `player_input` is what the player typed (None: unknown, the CLI),
    `roll_outcomes` this turn's roll_check outcome labels, and
    `addressed_npc_id` the person the player's talk chip chose. Each turn,
    in order:

    1. a SEALED truth this turn said aloud is marked revealed and journaled
       (`_reveal_truths_by_play`);
    2. every fired or stalled beat the turn delivered is marked — by its
       evidence keywords (`evidence_told`) and, for a beat that reveals a
       truth, a term of that truth said; a stalled one by its met_condition;
       either once every truth it reveals is out (unless a leak left its
       scene owed). A turn whose roll failed tells nothing its person was
       in (they held it back: it re-surfaces), and a hook beat
       (`is_hook_beat`) counts only once the player took it up
       (`_player_engaged`) — or by what the player read themselves
       (`_read_tells`, their journal);
    3. the current act's armed and pending beats that play already lived
       through are delivered by play (`_deliver_by_play`).

    Returns `{"delivered", "beat_id", "beat_ids", "act_advanced", "events",
    "truths_revealed", "played"}` (`beat_id` is the first of `beat_ids`;
    `played` are the ids delivered by play) — the events are queued for the
    next SSE flush as well.
    """
    if not player_id or not campaign_enabled():
        return {"delivered": False, "beat_id": None, "beat_ids": [], "act_advanced": None, "events": [],
                "truths_revealed": [], "played": []}

    events: list[dict[str, Any]] = []
    cinematics: list[tuple] = []
    delivered_ids: list[str] = []
    played_ids: list[str] = []
    result = {"delivered": False, "beat_id": None, "beat_ids": delivered_ids,
              "act_advanced": None, "events": events, "truths_revealed": [], "played": played_ids}
    haystack = "\n".join([turn_text or ""] + [str(s) for s in (speakers or []) if s])
    journal: list[str] = []
    told_keywords: list[list[str]] = []      # each beat this turn delivered
    names: list[str] | None = None
    failed_roll = any(str(o) in FAILED_ROLLS for o in roll_outcomes or ())
    player_words = "" if player_input is None or str(player_input).lstrip().startswith("[") else str(player_input)
    held_by_roll: set[str] = set()
    reads = _journal_reads(player_id)       # this reply's @interact reads are in it already

    with get_session() as session:
        campaign = get_campaign_row(session, player_id)
        if campaign is None or campaign.status != "active":
            return result
        now = _now(session)
        act_before = campaign.current_act
        player = session.get(Player, player_id)
        beats = _campaign_beats(session, campaign)
        common = campaign_common_words(campaign, beats)

        # 1. A sealed truth said aloud is out — the spine adapts to it
        for truth, _terms, entry in _reveal_truths_by_play(session, campaign, player, turn_text or "",
                                                           spoken, player_words=player_words,
                                                           common=common, beats=beats):
            result["truths_revealed"].append(str(truth.get("id")))
            if entry:
                journal.append(entry)
        # ...and a sealed thing-word the turn put on screen is canon from now
        _note_seen_words(campaign, player_id, turn_text or "")
        revealed = _revealed_ids(campaign)
        owed = _owed_scenes(campaign)
        names = _known_vocabulary(session, campaign, player, public=False)
        speaker_ids = _speaker_ids(session, speakers, spoken)

        def engaged(beat: StoryBeat) -> bool:
            return _player_engaged(session, beat, player_input, speaker_ids, addressed_npc_id, names, common)

        for beat in beats:
            if beat.status not in ("fired", "stalled"):
                continue
            said = (beat_keywords_delivered(beat, haystack, names, common)
                    and _truths_said(campaign, beat, turn_text or "", names))
            # Someone's entrance is told only with them in it: the whistle in
            # hand and "cracked" in the prose are not Vex stepping out
            # (fleet10-bram T15, `_entrance_people`)
            entering = _entrance_people(session, beat, player) if (said or reads) else []
            if said and entering and not _appeared(session, player_id, entering, speaker_ids, haystack):
                said = False
            # fleet6-bram T14: the roll failed, the Hawker "catches himself
            # before the words get all the way out" — not told, held back
            quiet = failed_roll and _roll_concerns(beat, speaker_ids)
            if said and quiet:
                held_by_roll.add(beat.id)
            told = said and not quiet and (not is_hook_beat(beat) or engaged(beat))
            in_scene = told
            # The player read it with their own eyes (fleet8-bram T9: the
            # notice board, word for word, a turn before it fired) — their
            # read, nobody's scene: what a hook holds back for its people (a
            # job, a hand-over) is not handed over by a read (`_announce`);
            # nor is anyone's entrance
            if not told and not beat.reveals_truth_ids and reads and not entering \
                    and _read_tells(session, beat, reads, names, common):
                told = True
            if not told and beat.status == "stalled":
                # A parked beat also counts as delivered once the world shows
                # its condition met — the player got there without the words
                told = bool(beat.met_condition) and evaluate_condition(
                    session, beat.met_condition, campaign, player
                )
            if not told and beat.reveals_truth_ids and beat.id not in owed:
                # ...or once everything it was there to reveal is out
                told = {str(i) for i in beat.reveals_truth_ids} <= revealed
            if not told:
                continue  # `fired` is re-surfaced next tick; `stalled` waits
            if is_hook_beat(beat):          # held back at fire (`_fire_next`)
                # What it hands over comes with its own scene; told some
                # other way (its truth out, its condition met), play decided
                # what changed hands, as for a beat delivered by play
                if in_scene and player is not None:
                    _settle_hook(session, beat, campaign, player, events, cinematics)
                else:
                    _announce(beat, campaign, events, cinematics)
            _deliver_beat(session, campaign, beat, now, events, cinematics)
            delivered_ids.append(beat.id)
            told_keywords.append(list(beat.delivery_keywords or []))

        # 3. Beats play already lived through are never fired later
        if player is not None:
            for beat, _reason in _deliver_by_play(
                session, campaign, beats, player, now, events, cinematics,
                text=turn_text or "", speaker_ids=speaker_ids, said_by=_said_by(spoken),
                common=common, failed_roll=failed_roll, engaged=engaged, player_words=player_words,
                reads=reads,
            ):
                played_ids.append(beat.id)
                told_keywords.append(list(beat.delivery_keywords or []))
        # The words that told them, for the journal: names are no evidence
        if not (told_keywords and spoken):
            names = None
        _ROLL_HELD[player_id] = held_by_roll

        if delivered_ids:
            result["delivered"] = True
            result["beat_id"] = delivered_ids[0]

        if campaign.current_act != act_before:
            result["act_advanced"] = campaign.current_act
        session.commit()

    for entry in journal:
        try:
            from src.services import memory_files

            memory_files.add_journal_entry(player_id, entry)
        except Exception as e:  # the truth is recorded; the journal line is a nicety
            logger.warning(f"journal entry for a revealed truth failed: {e}")
    if names is not None:
        try:
            from src.services import memory_files

            heard = [line for keywords in told_keywords for line in _beat_journal_lines(keywords, spoken, names)]
            memory_files.add_journal_entries(player_id, heard)
        except Exception as e:  # the beat is delivered; the journal line is a nicety
            logger.warning(f"journal lines for a delivered beat failed: {e}")
    _dispatch_cinematics(player_id, cinematics)
    _queue_events(player_id, events)
    return result


FAILED_ROLLS = ("failure", "critical_failure")


def _truths_said(campaign: Campaign, beat: StoryBeat, text: str, names: Iterable[str]) -> bool:
    """Did `text` say a term of every still-sealed truth this beat reveals,
    names aside (fleet6-bram T14: "Hesk" said, "fence" never)? A truth with
    nothing but names to say needs none. Only names are set aside, not the
    player's public vocabulary: fleet2-bram's Hollow scene told its truth as
    "their drill ground", and "drilled" was in the quest's own text."""
    sealed = {str(t.get("id")): t for t in (campaign.fixed_truths or [])
              if isinstance(t, dict) and not t.get("revealed")}
    wanted = [sealed[str(i)] for i in beat.reveals_truth_ids or [] if str(i) in sealed]
    if not wanted:
        return True
    known_forms = _text_forms(" ".join(str(k) for k in names if k))
    for truth in wanted:
        if any(_term_words(term, known_forms) for term in truth_terms(truth)) \
                and not truth_leak_terms(truth, text, known_forms=known_forms):
            return False
    return True


def _roll_concerns(beat: StoryBeat, speaker_ids: set[str]) -> bool:
    """Could this turn's failed roll have been about this beat? A deadline is
    the world's clock (the howl rises whatever the lock did); a beat that
    reveals something, or whose person spoke this turn, is the exchange."""
    if beat.beat_type == "deadline_event":
        return False
    return bool(beat.reveals_truth_ids) or bool(_beat_people(beat) & set(speaker_ids or ()))


# --- Hooks: beats the player has to take up ---------------------------------
# Round 5, fleet6-bram T14: "The Fence's Ledger" ("Have Hesk Bramble,
# cornered or chatted up at his stall, let slip under pressure or coin…")
# fired as MUST-DELIVER on its place trigger while Bram walked through the
# square to the healer; the DM wrote "You press him" and rolled for an
# interrogation the player never chose. A beat whose directive needs the
# player to act is a hook: it fires as a tell or an approach — the person
# shows it, the lead names it — and it is told only once the player takes it
# up (`_player_engaged`). Deadlines are the world's clock, never hooks.
_HOOK_CUE = re.compile(
    # "pressed" as in leaned on ("pressed into revealing his pay"), not
    # handed over ("a coin … pressed on her by a nervous onlooker",
    # fleet2-tamsin's "A Pocket Full of Trouble"; "pressed into her hand")
    r"\b(?:cornered|pressed(?!\s+(?:(?:on|upon)\s+(?:her|him|them)\b"
    r"|into\s+(?:a|an|the|her|his|their|its|my|your)\b|onto\b|against\b))"
    r"|pressing|press(?:es)?\s+(?:him|her|them)|under\s+pressure"
    r"|if\s+(?:asked|pressed|pushed|bribed|cornered|questioned|paid)"
    r"|when\s+(?:asked|pressed|questioned)|chatted\s+up|talk\s+for\s+coin"
    # ...never "Do not have him confront her directly" (fleet8-tamsin's
    # "A Scout Watching the Scout": Vex is to shadow her, nothing to take up)
    r"|where\s+\w+\s+confronts|(?<!not )(?<!n't )have\s+\w+\s+confront)\b",
    re.IGNORECASE,
)
# ...unless the person comes to the player anyway, the pressing only for
# more ("mention to Wenna … He won't say more unless pressed", fleet4-wenna
# T20; "seek Wenna out … admitting, if pressed", fleet2-wenna). Not a bare
# "approaches": that is the tell the forge is told to write for a hook
# (campaign_forge rule 8, "a tell, an approach") and the HOOK note asks for
_INITIATIVE_CUE = re.compile(
    r"\b(?:unprompted|mentions?\s+to|seeks?\s+\w+\s+out|pulls?\s+\w+\s+aside"
    r"|comes?\s+to\s+(?:find|the\s+player)|calls?\s+out\s+to)\b",
    re.IGNORECASE,
)
# Someone in the beat holds out on the player ("If pressed, he deflects",
# "have Maera deny it first", "refuse to say who pays him"): the COST note
# in `_directive_notes`. Over every fleet campaign: eight beats, each a
# person keeping something back ("refuses to fight" is no secret kept)
_RESISTS = re.compile(
    r"\b(?:deflects?|deflecting|denies|denying|deny|refuses?\s+to\s+(?:say|tell|answer|talk|speak|name|admit)"
    r"|stonewalls?|clams?\s+up|waves?\s+(?:it|her|him|them)\s+off|brush(?:es)?\s+(?:it|her|him|them)\s+off"
    r"|changes?\s+the\s+subject|claims?\s+(?:he|she|they)(?:['’]s|\s+has|\s+have|\s+is|\s+are|\s+was)?\s+never"
    r"|plays?\s+dumb|feigns?\s+ignorance)\b",
    re.IGNORECASE,
)

# Round 7: beats that script the player. fleet8-bram's "Have Bram read the
# notice board…" and "If Bram presses Sorrel Tack at the Stable Yard, have
# the boy admit…" fired as MUST-DELIVER and were nagged three times each —
# the only answer to that nag was to read or press for him. A beat whose
# condition is the player's own act ("If/When <PC> presses, asks, reads…",
# "Have <PC> read…") is a hook: it shows the thing or the person's tell, and
# waits. An act scripted in passing ("…before she pockets the whistle",
# fleet8-tamsin T15, where the @give went round the refused reach) is named
# back to the DM as the player's to choose (`_directive_notes`).
_PLAYER_ACTS = (r"press(?:es)?|push(?:es)?|asks?|questions?|confronts?|corners?|brib(?:e|es)|pays?"
                r"|reads?|examines?|searches|studies|inspects?|talks?\s+(?:to|with)|speaks?\s+(?:to|with)"
                r"|seeks?\s+\w+\s+out|brings?|shows?|accuses?|follows?|threatens?|offers?")
_PLAYER_ACTS_BARE = (r"read|press|push|ask|question|confront|corner|bribe|pay|examine|search|study|inspect"
                     r"|talk\s+(?:to|with)|speak\s+(?:to|with)|bring|show|accuse|follow|threaten|offer"
                     r"|pocket|take|steal")
# By pronoun only a taking: "before she takes it to the Captain" is Piper's
# errand (fleet5-tamsin's "The Pattern in the Dark")
_TAKES_IN_PASSING = r"pockets?|steals?|lifts?|grabs?|picks?\s+(?:\w+\s+)?up"
_ACTS_IN_PASSING = _TAKES_IN_PASSING + r"|takes?|keeps?|opens?|reads?(?!\s+(?:it|them|this|that)\s+as\b)|searches"


def _player_names(beat: Any) -> list[str]:
    """The player character's full and first name, for a beat row still in
    its session (the directive is read for them); [] for anything else."""
    try:
        from sqlalchemy.orm import object_session

        session = object_session(beat) if not isinstance(beat, dict) else None
        if session is None or not getattr(beat, "campaign_id", None):
            return []
        campaign = session.get(Campaign, beat.campaign_id)
        player = session.get(Player, campaign.player_id) if campaign is not None else None
    except Exception as e:  # a detached row reads as before: no player cue
        logger.debug(f"player name for a beat unavailable: {e}")
        return []
    name = " ".join(str(getattr(player, "name", "") or "").split())
    if not name:
        return []
    return [n for n in dict.fromkeys([name, name.split()[0]]) if len(n) >= 3]


def player_scripted(beat: Any) -> tuple[str | None, str | None]:
    """(what the beat waits on the player to do, an act it scripts for them
    in passing), each the directive's own words or None (see above)."""
    names = _player_names(beat)
    directive = str(getattr(beat, "directive", "") or "")
    if not names or not directive:
        return None, None
    who = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    # A condition that opens a sentence governs it; "…vague about Watch
    # numbers if Bram presses" closing a job offer only shades it
    # (fleet-bram's "A Captain Who Needs Hands", which Coldiron brings)
    cue = re.search(
        rf"(?:^|[.!?;:]\s+)(?:(?:if|when|once|should|whenever|after)\s+(?:{who})\s+(?:\w+\s+){{0,2}}?"
        rf"(?:{_PLAYER_ACTS})|have\s+(?:{who})\s+(?:{_PLAYER_ACTS_BARE}))\b",
        directive, re.IGNORECASE,
    )
    passing = re.search(
        rf"\b(?:before|after|until|once|as|while|when)\s+(?:(?:{who})\s+(?:{_ACTS_IN_PASSING})"
        rf"|(?:she|he|they)\s+(?:{_TAKES_IN_PASSING}))\b[^.;,—]{{0,40}}",
        directive, re.IGNORECASE,
    )
    return (cue.group(0).lstrip(".!?;: ").strip() if cue else None), \
        (passing.group(0).strip() if passing else None)


def is_hook_beat(beat: Any) -> bool:
    """Does this beat wait on the player acting (see above)? Never a
    deadline (the world's clock) nor a confrontation: that one stages the
    antagonist in front of the player ("Vane, cornered, makes his last
    offer"), and what the player does there is theirs anyway. Nor a beat
    whose person comes to the player (`_INITIATIVE_CUE`). Over every fleet
    campaign this flags 27 of 172 beats, the confessions and slips that
    wait on someone asking — and, from round 7, the beats whose condition
    is the player's own act (`player_scripted`)."""
    if beat is None or getattr(beat, "beat_type", None) in ("deadline_event", "confrontation"):
        return False
    directive = str(getattr(beat, "directive", "") or "")
    if _INITIATIVE_CUE.search(directive):
        return False
    return bool(_HOOK_CUE.search(directive)) or player_scripted(beat)[0] is not None


def _hook_people(session, beat: StoryBeat) -> list[NPC]:
    """Who a hook is about: its lead and ops' people, and anyone its
    directive names."""
    ids = set(_beat_people(beat))
    alive = session.query(NPC).filter(NPC.status == "alive").all()
    people = [n for n in alive if n.id in ids]
    people += [n for n in _named_people(beat.directive or "", alive) if n not in people]
    return people


def _player_engaged(session, beat: StoryBeat, player_input: str | None, speaker_ids: set[str],
                    addressed_npc_id: str | None, names: Iterable[str], common: Iterable[str]) -> bool:
    """Did the player take this hook up this turn — its matter in their own
    words (an evidence keyword: "the fence", "dead carters"), or its person
    answering them when they spoke to them (`dialogue._talked_to`)? An
    engine turn ([OPENING], [COMBAT OUTCOME]) takes nothing up; no input at
    all (the CLI) gates nothing."""
    if player_input is None:
        return True
    text = str(player_input).strip()
    if not text or text.startswith("["):
        return False
    if evidence_told(beat.delivery_keywords or [], text, names, common, min_hits=1, fallback=False)[1]:
        return True
    try:
        from src.services import dialogue

        speaking = [n for n in (session.get(NPC, i) for i in speaker_ids or ()) if n is not None]
        for npc in _hook_people(session, beat):
            if npc.id not in (speaker_ids or ()):
                continue                    # no reply from them this turn
            others = [o for o in speaking if o.id != npc.id]
            if dialogue._talked_to(npc, text, set(speaker_ids), others, addressed_npc_id):
                return True
    except Exception as e:  # an unreadable input only delays the beat a turn
        logger.debug(f"hook engagement check failed for {beat.title}: {e}")
    return False


# --- A beat and its scheduled twin ------------------------------------------
# Round 5, fleet6-wenna T9: "The Cart Comes Short" was delivered by play in
# Coldiron's words ("cart overturned, no driver") while the world event it
# restates, "The Greywater Cart Arrives Short" (Day 2 09:00, a talking
# driver), stayed scheduled — tomorrow the cart with no driver would roll
# in with its driver. A delivered beat retires the unnarrated scheduled
# event it told (`world_tick.retire_scheduled_twins`), and the next briefing
# says which version is canon: the one the player heard.

def _retire_twins(session, campaign: Campaign, beat: StoryBeat, now: tuple[int, int]) -> None:
    try:
        from src.services import world_tick

        twins = world_tick.retire_scheduled_twins(
            session, beat.title or "", list(beat.delivery_keywords or []), beat.directive or "", now)
    except Exception as e:  # the beat is delivered either way
        logger.warning(f"scheduled twins of {beat.title!r} not checked: {e}")
        return
    for event in twins:
        when = (f" (due Day {event.scheduled_day}, {int(event.scheduled_hour or 0):02d}:00)"
                if event.scheduled_day is not None else "")
        # One already due (the 20:00 howl the deadline beat told at 20:00)
        # did happen: "will not happen" would cancel what the player saw
        due = event.scheduled_day is not None and (event.scheduled_day, event.scheduled_hour or 0) <= tuple(now)
        fate = (f'happened as "{beat.title}" told it' if due
                else f'will not happen as scheduled: "{beat.title}" already told it')
        _CONTINUITY.setdefault(campaign.player_id, []).append(
            f'"{event.name}"{when} {fate}. What the '
            "player heard then is canon — never stage it again, and anyone who speaks of it tells it that way."
        )


# ...and the other way round. Round 6, fleet7-wenna: "The Gate Bars at the
# Howl" ("At the stroke of eight the evening howl rises as already
# scheduled… have Sorrel Tack, shaken, tell anyone nearby…") was a deadline
# armed for Day 6 while the Day-1 20:00 howl it builds on fired and was told
# at T10 — its clock counted down to a howl that had happened, and Sorrel's
# sighting never came. A beat that restates a scheduled event, or is built
# on it ("as already scheduled" with the directive telling it — campaign_forge
# rule 10's way of building on one), comes due once that event has happened,
# as its aftermath (`_directive_notes` says never to stage it twice). Over
# every fleet campaign the "as scheduled" rule adds exactly two pairs to
# `is_scheduled_twin`'s: this one, and fleet7-tamsin's "The Cart Limps In"
# ("arrives half-empty exactly as scheduled") with the Greywater cart.
_BUILT_ON_SCHEDULE = re.compile(r"\b(?:already|exactly|as)\s+scheduled\b", re.IGNORECASE)


def _event_twins(session, beat: StoryBeat, *, told: bool) -> list[Event]:
    """The scheduled events that happened while this beat was already
    waiting and that it restates or builds on (see above). `told`: only
    those narrated to the player. Only an event that happened after the
    beat armed is its twin: a later act's "Evening Howl" is that night's
    howl, not Day 1's aftermath."""
    from src.services.world_tick import event_told, is_scheduled_twin

    armed = _stamp(beat, "armed")
    query = session.query(Event).filter(Event.scheduled_day.isnot(None), Event.occurred_day.isnot(None))
    if told:
        query = query.filter(Event.narrated_to_player == True)  # noqa: E712
    built_on = bool(_BUILT_ON_SCHEDULE.search(beat.directive or ""))
    twins = []
    for event in query.all():
        if armed is not None and (event.occurred_day, event.occurred_hour or 0) < armed:
            continue
        if is_scheduled_twin(event, beat.title or "", list(beat.delivery_keywords or []), beat.directive or "") \
                or (built_on and event_told(event, beat.directive or "")):
            twins.append(event)
    return twins


def _bring_due_after_twins(session, beats: list[StoryBeat], now: tuple[int, int]) -> None:
    """An armed clock beat whose scheduled twin already happened is due now,
    as that event's aftermath — never days later on its own clock. A
    deadline's met/missed is decided then, as at any fire."""
    for beat in beats:
        if beat.status != "armed" or not _is_clock(beat):
            continue
        scheduled = _stamp(beat, "scheduled")
        if scheduled is None or scheduled <= now:
            continue
        try:
            twins = _event_twins(session, beat, told=False)
        except Exception as e:  # the beat keeps its own clock
            logger.debug(f"twin check for {beat.title!r} skipped: {e}")
            continue
        if not twins:
            continue
        logger.info(f"campaign beat due now: its scheduled twin already happened "
                    f"({', '.join(e.name for e in twins)}): {beat.title}")
        beat.scheduled_day, beat.scheduled_hour = now
        if beat.beat_type == "deadline_event":
            beat.fallback_day, beat.fallback_hour = now     # a deadline never slips — nor waits


# --- A cue that can no longer come -------------------------------------------
# Round 7, fleet8-bram: act-3 "Vex Marrow's Ultimatum" waits on "Teeth on the
# North Road" being ACTIVE — and the job was paid in full at T8. A quest that
# is done (or failed) never goes back: that trigger can never come true, and
# the beat would sit out its whole backstop. It comes due soon instead, as
# what followed the job (`_directive_notes` says so).

def _dead_cue(session, beat: StoryBeat) -> Quest | None:
    """The quest of a quest_status trigger that can never come true now —
    it is completed or failed, and the trigger wants another status."""
    if beat.trigger_type != "quest_status":
        return None
    data = beat.trigger_data if isinstance(beat.trigger_data, dict) else {}
    quest = _get(session, Quest, data.get("quest_id"))
    if quest is None or quest.status not in (QuestStatus.COMPLETED, QuestStatus.FAILED):
        return None
    return None if quest.status == data.get("status") else quest


def _wake_dead_cues(session, beats: list[StoryBeat], now: tuple[int, int]) -> None:
    """An armed beat whose quest cue is dead comes due within DELAY_HOURS
    ["soon"] (see above), never later than its own backstop."""
    soon = _add_hours(now, DELAY_HOURS["soon"])
    for beat in beats:
        if beat.status != "armed" or _dead_cue(session, beat) is None:
            continue
        fallback = _stamp(beat, "fallback")
        if fallback is None or fallback > soon:
            logger.info(f"campaign beat's cue can no longer come true — due soon, as what followed: {beat.title}")
            beat.fallback_day, beat.fallback_hour = soon


# --- An earlier act's leftovers ---------------------------------------------
# Round 6, fleet7-wenna: act 2 pulled ahead at T19 ("act 1 holds only clocks")
# with "A Man Who Knows More Than He Says" still armed — Coldiron to "wave it
# off… change the subject to the gate pass", due fifteen minutes after the
# session ended, when he had already handed over the pass, taken the proof
# and named Vane in act 2's "The Cracked Whistle". An earlier act's beat whose
# person a later act's DELIVERED beat already played is superseded: skipped
# (it un-seals nothing). Never a deadline — the world's clock keeps its
# consequences — and never one with a sealed truth still to tell. What is
# left of an earlier act fires with a note that it comes late
# (`_directive_notes`).

def _retire_superseded(session, campaign: Campaign, beats: list[StoryBeat]) -> None:
    act = campaign.current_act or 1
    revealed = _revealed_ids(campaign)
    played_later: dict[int, set[str]] = {}
    for beat in beats:
        if beat.status == "delivered":
            played_later.setdefault(beat.act, set()).update(_beat_people(beat))
    for beat in beats:
        if beat.act >= act or beat.status not in ("pending", "armed", "stalled") \
                or beat.beat_type == "deadline_event":
            continue
        if {str(i) for i in beat.reveals_truth_ids or []} - revealed:
            continue
        people = _beat_people(beat)
        later = set().union(*(ids for a, ids in played_later.items() if a > beat.act)) if played_later else set()
        if people & later:
            logger.info(f"campaign beat skipped: a later act already played its people: {beat.title}")
            beat.status = "skipped"


def _deliver_beat(session, campaign: Campaign, beat: StoryBeat, now: tuple[int, int],
                  events: list, cinematics: list) -> None:
    """Mark a beat delivered, un-seal its truths, retire the scheduled world
    events it already told (`_retire_twins`), then check the act boundary."""
    beat.status = "delivered"
    beat.delivered_day = now[0]
    _FOUND_DUE.get(campaign.player_id, {}).pop(beat.id, None)
    _retire_twins(session, campaign, beat, now)

    revealed_ids = {str(i) for i in (beat.reveals_truth_ids or [])}
    if revealed_ids:
        truths = [dict(t) if isinstance(t, dict) else t for t in (campaign.fixed_truths or [])]
        for truth in truths:
            # A truth play already let out keeps the day it came out
            if isinstance(truth, dict) and str(truth.get("id")) in revealed_ids and not truth.get("revealed"):
                truth["revealed"] = True
                truth["revealed_day"] = now[0]
        campaign.fixed_truths = truths

    dm_state = session.query(DMState).first()
    if dm_state:
        completed = list(dm_state.completed_beats or [])
        if beat.title not in completed:
            completed.append(beat.title)
            dm_state.completed_beats = completed

    _check_act_transition(session, campaign, beat, now, events, cinematics)


def _revealed_ids(campaign: Campaign) -> set[str]:
    return {str(t.get("id")) for t in (campaign.fixed_truths or [])
            if isinstance(t, dict) and t.get("revealed")}


def _speaker_ids(session, speakers: list[str] | None,
                 spoken: list[dict[str, Any]] | None) -> set[str]:
    """Who spoke this turn, by NPC id: the speech notifications' ids, plus
    any speaker label that IS an NPC's name (the CLI passes names only)."""
    ids = {str(s["npc_id"]) for s in (spoken or []) if isinstance(s, dict) and s.get("npc_id")}
    names = [str(n) for n in (speakers or []) if n]
    if names:
        ids |= {row.id for row in session.query(NPC).filter(NPC.name.in_(names)).all()}
    return ids


def _said_by(spoken: list[dict[str, Any]] | None) -> dict[str, str] | None:
    """{npc id: their lines this turn, one per line} from the speech
    notifications, or None when there are none to read (the CLI)."""
    said: dict[str, list[str]] = {}
    for entry in spoken or []:
        if isinstance(entry, dict) and entry.get("npc_id") and entry.get("text"):
            said.setdefault(str(entry["npc_id"]), []).append(str(entry["text"]))
    return {npc_id: "\n".join(lines) for npc_id, lines in said.items()} or None


def _known_vocabulary(session, campaign: Campaign, player, *, public: bool = True) -> list[str]:
    """What the player already knows, as text: every name in the world —
    people, places, factions, their own — and, with `public`, what the
    world says openly (the bible's situation and conflicts, the act's
    premise, the text of every quest they took, the truths already out).
    A word from here is context, never evidence of a sealed truth."""
    from src.models import WorldBible

    vocabulary = [name for (name,) in session.query(NPC.name).all() if name]
    vocabulary += [name for (name,) in session.query(Location.name).all() if name]
    vocabulary += [name for (name,) in session.query(Faction.name).all() if name]
    if player is not None and getattr(player, "name", None):
        vocabulary.append(player.name)
    if not public:
        return vocabulary
    bible = session.query(WorldBible).first()
    if bible is not None:
        vocabulary.append(str(bible.current_situation or ""))
        conflicts = bible.major_conflicts
        vocabulary.extend(str(c) for c in (conflicts if isinstance(conflicts, list) else [conflicts or ""]))
    vocabulary.append(str(_act_entry(campaign, campaign.current_act or 1).get("premise") or ""))
    from src.services.dialogue import shown_step_texts

    for quest in session.query(Quest).filter(Quest.status != QuestStatus.NOT_STARTED).all():
        vocabulary += [quest.title or "", quest.description or ""]
        # ...as their quest log shows the steps: proof never held is "proof"
        # there, so its name is not met (fleet10-bram T1, dialogue.unfound_steps)
        vocabulary += shown_step_texts(session, player, quest)
    vocabulary += [str(t.get("statement") or "") for t in (campaign.fixed_truths or [])
                   if isinstance(t, dict) and t.get("revealed")]
    # The things in their pack are things they know (fleet5-bram T16-T18:
    # "Hand me the whistle" about the whistle the player had just found)
    vocabulary += [str((entry.get("name") if isinstance(entry, dict) else entry) or "")
                   for entry in (getattr(player, "inventory", None) or [])]
    return [v for v in vocabulary if v and v.strip()]


_SENTENCE = re.compile(r"(?<=[.!?…])\s+")
# A speaker line left in the text: Name (tone): "words"
_SPEAKER_LINE = re.compile(
    r"^\s*([A-Z][\w'’.-]*(?:[ -][A-Z][\w'’.-]*){0,4})\s*(?:\([^)]*\))?:\s*[\"“](.*?)[\"”]?\s*$"
)
JOURNAL_QUOTE_CHARS = 300


def _truth_journal_entry(truth: dict[str, Any], text: str, known: list[str],
                         spoken: list[dict[str, Any]] | None) -> str:
    """What the character actually heard, for their journal: the sentences
    of the line that gave the truth away, with who said it — never the
    sealed statement itself (it can hold more than was said)."""
    # (speaker or "", words) — the speech lines first, so a tie is attributed
    lines = [(str(s.get("npc_name") or ""), str(s.get("text") or ""))
             for s in (spoken or []) if isinstance(s, dict) and s.get("text")]
    for unit in _units(text):
        match = _SPEAKER_LINE.match(unit)
        lines.append((match.group(1).strip(), match.group(2)) if match else ("", unit))
    known_forms = _text_forms(" ".join(str(k) for k in known if k))
    speaker, words, best = "", "", []
    for who, said in lines:
        hits = truth_leak_terms(truth, said, known_forms=known_forms)
        if len(hits) > len(best):
            speaker, words, best = who, said.strip(), hits
    if not best:
        return ""
    sentences = [s for s in _SENTENCE.split(words)
                 if truth_leak_terms({"terms": best}, s, known_forms=known_forms)]
    quote = " ".join(sentences) or words
    if len(quote) > JOURNAL_QUOTE_CHARS:
        quote = quote[: JOURNAL_QUOTE_CHARS - 1].rstrip() + "…"
    return f'{speaker} said: "{quote}"' if speaker else quote


BEAT_JOURNAL_LINES = 2


def _beat_journal_lines(keywords: list[str], spoken: list[dict[str, Any]] | None,
                        names: list[str]) -> list[str]:
    """A delivered beat, as the character heard it: at most BEAT_JOURNAL_LINES
    speaker lines from this turn that say its delivery keywords — `Sorrel:
    "…"`, the words on screen under the name on screen, never the directive.
    Round 4 (fleet5-wenna T19-20): the hooded man burying something by the
    north wall was delivered, and never reached her journal. A keyword that
    is only a name proves nothing (a beat about Sorrel names Sorrel)."""
    known_forms = _text_forms(" ".join(str(n) for n in names if n))
    evidence = [str(k) for k in keywords or []
                if any(not (_word_forms(w) & known_forms) for w in _distinctive(str(k)))]
    if not evidence:
        return []
    scored: list[tuple[int, int, str, str]] = []
    for index, line in enumerate(spoken or []):
        if not isinstance(line, dict) or not str(line.get("text") or "").strip():
            continue
        text = str(line["text"]).strip()
        hits = mentioned_keywords(evidence, text)
        if hits:
            scored.append((len(hits), index, str(line.get("npc_name") or "").strip(), text))
    best = sorted(scored, key=lambda s: (-s[0], s[1]))[:BEAT_JOURNAL_LINES]
    out: list[str] = []
    for _, _, speaker, text in sorted(best, key=lambda s: s[1]):
        sentences = [s for s in _SENTENCE.split(text) if mentioned_keywords(evidence, s)]
        quote = " ".join(sentences) or text
        if len(quote) > JOURNAL_QUOTE_CHARS:
            quote = quote[: JOURNAL_QUOTE_CHARS - 1].rstrip() + "…"
        out.append(f'{speaker}: "{quote}"' if speaker else f'"{quote}"')
    return out


def _reveal_truths_by_play(session, campaign: Campaign, player, text: str,
                           spoken: list[dict[str, Any]] | None = None, *,
                           player_words: str = "", common: Iterable[str] = (),
                           beats: list[StoryBeat] | None = None) -> list[tuple[dict, list[str], str]]:
    """Mark every SEALED truth this turn said aloud as revealed — the spine
    adapts instead of nagging. fleet4-tamsin: Maera told Coldiron's "eleven,
    not twenty" at T19 and the truth stayed sealed, so the act-3 deadline
    whose met_condition is that truth would have punished the player for
    learning it. The truth records how it came out (`revealed_by: "play"`
    and the words, the campaign's own ledger); the caller journals the
    words. `player_words` (their input) and the campaign's `common` words
    are no evidence for the spread-thin and subject rules. A truth out
    ahead of the beat built to reveal it (its after_beat predecessor still
    undelivered) records that scene as still owed (`scene_owed`, see
    `_owed_scenes`). Returns [(truth, terms said, journal entry)]."""
    truths = [dict(t) if isinstance(t, dict) else t for t in (campaign.fixed_truths or [])]
    sealed = [t for t in truths if isinstance(t, dict) and not t.get("revealed")]
    if not sealed or not (text or "").strip():
        return []
    known = _known_vocabulary(session, campaign, player)
    npcs = session.query(NPC).all()
    probes = [
        dict(t, subjects=[n.id for n in _named_people(str(t.get("statement") or ""), npcs)],
             cross_terms=_cross_terms(t, common))
        for t in sealed
    ]
    speech = [(str(s.get("npc_id") or ""), str(s.get("text") or "")) for s in spoken or [] if isinstance(s, dict)]
    hits = sealed_truth_hits(probes, text, known, speech=speech, player_words=player_words)
    if not hits:
        return []
    now = _now(session)
    out: list[tuple[dict, list[str], str]] = []
    for truth in truths:
        if not isinstance(truth, dict) or str(truth.get("id")) not in hits:
            continue
        entry = _truth_journal_entry(truth, text, known, spoken)
        truth.update(revealed=True, revealed_day=now[0], revealed_by="play", revealed_evidence=entry)
        owed = _ahead_of(session, beats if beats is not None else _campaign_beats(session, campaign),
                         str(truth.get("id")))
        if owed:
            truth["scene_owed"] = owed
        logger.info(f"campaign truth said aloud, now revealed: {truth.get('id')} ({hits[str(truth.get('id'))]})"
                    + (f" — ahead of its scene, still owed: {owed}" if owed else ""))
        out.append((truth, hits[str(truth.get("id"))], entry))
    campaign.fixed_truths = truths
    return out


# --- A truth out before its scene ------------------------------------------
# Round 5, fleet6-bram T19: an overheard mutter ("eleven able, told the
# council twenty") let the roster truth out, and "Coldiron's Lie, Uncovered"
# — Bram confronting him and Coldiron admitting it, the scene the campaign
# built for a man with no patience for lying officers — was marked delivered
# by play while "The Sergeant's Name", the beat it follows, was still armed.
# The confrontation never happened. A leaked truth stays canon, but a beat
# still waiting on its predecessor keeps its scene: it is never delivered by
# play or by the leak, it arms and fires in order, and its directive says
# the player already overheard the fact (`_directive_notes`).

def _predecessor_pending(session, beat: StoryBeat) -> bool:
    """Does this after_beat beat's prerequisite (same or earlier act) still
    have its own scene to deliver? A STALLED one has had its chances, as in
    `_arm_after_beats`: the story moved on to this beat (a hook the player
    never took up stalls, and its follow-up plays out on its own)."""
    if beat.trigger_type != "after_beat":
        return False
    data = beat.trigger_data if isinstance(beat.trigger_data, dict) else {}
    prerequisite = _get(session, StoryBeat, data.get("beat_id"))
    return (prerequisite is not None and prerequisite.act <= beat.act
            and prerequisite.status not in ("delivered", "skipped", "stalled"))


def _ahead_of(session, beats: list[StoryBeat], truth_id: str) -> list[str]:
    """The ids of the unplayed beats that reveal this truth and still wait
    on their predecessor — their scene is owed whatever leaked."""
    return [b.id for b in beats
            if b.status in ("pending", "armed") and truth_id in {str(i) for i in b.reveals_truth_ids or []}
            and _predecessor_pending(session, b)]


def _owed_scenes(campaign: Campaign) -> set[str]:
    """Every beat id a leaked truth left owed (see above)."""
    return {str(i) for t in (campaign.fixed_truths or []) if isinstance(t, dict)
            for i in t.get("scene_owed") or []}


# --- Play can deliver beats ------------------------------------------------
# fleet4-tamsin: the act-1 quest went to Wolf's Hollow, so play spent the
# spine early — and the spine never heard. "Coldiron Wants Eyes He Can Spare"
# still waited to offer a quest completed at T11; "The Ex-Sergeant's Whistle"
# would "reveal Aldric Vane at Wolf's Hollow" after she parleyed with him
# there; "Piper's Pattern" was played out at T12-15 and still armed.
# fleet4-wenna: "What Sorrel Saw" fired at T20 and Sorrel re-told, as news,
# what he had said at T16. A beat play already delivered is marked delivered
# (by play): its title joins the story so far, whatever waits on it arms as
# usual (the trigger chain stays intact), and it is never fired.
#
# Which beats: the current act's armed and pending ones (and an earlier
# act's, left behind when the story pulled ahead of its clocks) — never a deadline
# (the clock IS the story; met/missed decides it), never the finale, never
# one with a hard consequence still to happen (a death, an arrest, a quest
# completed or failed, a faction shift, a world event): those are changes
# play has to actually reach, never inferred from words.
#
# Why (first that holds):
#   truths    every truth it reveals is already out
#   met       the person it reveals ("Reveal Aldric Vane…", "spot Vex
#             Marrow…") has met the player and stands at its place, which
#             the player has visited — the introduction happened (it
#             reveals no truth: see below)
#   quest     every quest it offers is already taken (it reveals nothing),
#             and nothing else of its scene is still owed (`_scene_owed`)
#   keywords  one of its people spoke this turn and its words landed in
#             their own lines — names aside, at least two keywords and half
#             of them (`_played_keywords`; it reveals nothing)
#   read      the player read (or found) the thing it is about, and the
#             words they read say it — two keywords and half (`_read_tells`;
#             it reveals nothing)
#
# Only `truths` ever delivers a beat that un-seals a truth. Meeting Aldric
# Vane is not hearing that he was drummed out, nor that Vex is paid in
# counting-house silver ("if she gets close, he lets slip…"): a delivered
# beat would print its truths on the player's "What you know" panel and tell
# the DM the player knows them. Such a beat fires as written, with a note
# that the player already knows the person (`_directive_notes`) — no second
# introduction, only what is new.
#
# Which of its ops still apply — only what outlives the scene:
#   npc_mood              applied: the mood the scene left is still true
#   move_npc to a place   applied: later scenes may need them staged there
#   move_npc to "player"  skipped: the scene already happened where they were
#     (or to the player's own place, or of someone in their scene or party)
#   activate_quest        skipped: a quest already taken stays as it is, and
#                         one never taken is not forced on the player — it
#                         is still on offer in the giver's dossier
#   give_item             skipped: play decided what changed hands (the DM's
#                         own @give is the record) — never a second copy
#   tension_floor         skipped: the scene is over, and this turn's tension
#                         was already measured from what happened
# A move never pulls someone into or out of the player's scene or party:
# play left them where they are, and a silent (dis)appearance is no staging.

_SOFT_OPS = ("npc_mood", "move_npc", "give_item", "activate_quest", "tension_floor")
PLAY_REASONS = ("truths", "met", "quest", "keywords", "read")
_REVEAL_VERB = re.compile(
    r"\b(?:reveal|reveals|revealing|introduce|introduces|introducing|unmask|unmasks|"
    r"spot|spots|glimpse|glimpses)\b", re.IGNORECASE,
)
_REVEAL_WINDOW = 60      # characters after the verb in which the person is named


def _play_eligible(session, campaign: Campaign, beat: StoryBeat, finale: StoryBeat | None,
                   player) -> bool:
    # An earlier act's leftovers count too: the story pulled ahead of them
    # (`_pull_ahead`), and play can still tell one before its clock comes
    if beat.status not in ("armed", "pending") or beat.act > (campaign.current_act or 1):
        return False
    if beat.beat_type == "deadline_event" or beat is finale:
        return False
    return not any(
        isinstance(op, dict) and op.get("op") not in _SOFT_OPS
        and _op_state(session, op, player) == "pending"
        for op in (beat.consequence_met or [])
    )


# Round 7, fleet8-wenna T3: "The Cart Comes Back Wrong" — the driver's "eyes
# in the birches" when the scheduled Greywater cart rolls in, and Coldiron
# offering her the gate pass "a little too readily" — was delivered by play
# the moment the campaign was saved, because its one op started the job the
# opening had already started. The player saw "▲ The Cart Comes Back Wrong"
# over Piper's talk of howls, the beat it follows (the Evening Howl) had not
# happened, the cart was still due next morning, and the hand-over of the
# pass that the job's first step needs never came. A job already taken is
# not the scene lived.
_SCHEDULED_WORD = re.compile(r"\bscheduled\b", re.IGNORECASE)


def _scheduled_basis(session, beat: StoryBeat) -> list[Event]:
    """The scheduled events, not happened yet, that this beat's directive
    says it builds on ("Build on the scheduled arrival of the half-empty
    Greywater cart": "scheduled", and two words of the event's name or its
    telling). Never raises."""
    directive = beat.directive or ""
    if not _SCHEDULED_WORD.search(directive):
        return []
    try:
        from src.services.world_tick import event_told

        told = _text_forms(directive)
        return [event for event in session.query(Event).filter(
                    Event.scheduled_day.isnot(None), Event.occurred_day.is_(None)).all()
                if sum(1 for w in _distinctive(event.name or "") if _word_forms(w) & told) >= 2
                or event_told(event, directive)]
    except Exception as e:  # unreadable: as if it built on nothing
        logger.debug(f"scheduled event check for {beat.title!r} skipped: {e}")
        return []


def _scene_owed(session, beat: StoryBeat, player) -> bool:
    """Is something of this beat's scene still to happen, whatever its ops
    say (see above)? A scheduled event it builds on not happened yet, or a
    thing its job needs that the directive hands over and the player does
    not hold. Never once play FINISHED every job it offers: that scene is
    moot (fleet4-tamsin's "Coldiron Wants Eyes", its job done at T11). The
    order alone owes nothing: a job the opening already gave is taken
    whatever beat was meant to come first (round 3's "A Captain Who Needs
    Hands"). Round 9: nor is someone's entrance lived because its job was
    taken or its mood already holds (`_entrance_people`)."""
    offered = _offered_quests(session, beat)
    if offered and all(q.status in (QuestStatus.COMPLETED, QuestStatus.FAILED) for q in offered):
        return False
    directive = beat.directive or ""
    if _scheduled_basis(session, beat):
        return True
    if _entrance_people(session, beat, player):
        return True
    from src.services.objectives import normalize_objectives

    for quest in offered:
        for step in normalize_objectives(quest.objectives):
            item = step.get("item_name")
            if step.get("kind") == "obtain" and item and not step.get("done") \
                    and _phrase_said(" ".join(_words(item)), directive) and not _holds(player, item):
                return True
    return False


# --- Things the player read ---------------------------------------------------
# Round 7, fleet8-bram T9: Bram read the notice board through `@interact`
# word for word ("Sweetroot for nerves. Ask Piper."), and "The Notice Board"
# — fired a turn later, spacing had held it — was nagged three times and
# stalled: play delivered a beat only through its people's own lines. What
# the player read or found is in their journal in the thing's own words
# (`memory_files.interaction_journal_entries`: `Notice board: "…"`, `Found
# X in the crate: …`), canvas [E] and @interact alike.
_READ_ENTRY = re.compile(r'^(?:\([^)]*\)\s*)?(?P<label>[^":\n]{2,80}?)(?:\s+reads)?:\s*"(?P<text>.*)"\s*$', re.S)
_FOUND_ENTRY = re.compile(
    r"^(?:\([^)]*\)\s*)?Found\s+(?P<item>[^:\n]{2,80}?)(?:\s+in\s+the\s+(?P<label>[^:\n]{2,80}?))?"
    r"(?::\s*(?P<text>.*))?$", re.S)


def _journal_reads(player_id: str | None) -> list[str]:
    """The player's journal entries (oldest first), or [] — read before a
    caller opens its own session. Never raises."""
    if not player_id:
        return []
    try:
        from src.services import memory_files

        return memory_files.read_journal(player_id)
    except Exception as e:  # no journal: no read counts, as before
        logger.debug(f"journal for read delivery unavailable: {e}")
        return []


def _thing_places(session) -> dict[str, str]:
    """{an authored Thing's label, lowercased: the place it stands}."""
    from src.services.scene_objects import normalize_objects

    places: dict[str, str] = {}
    for location in session.query(Location).all():
        try:
            for obj in normalize_objects(location.objects) if location.objects else []:
                places.setdefault(str(obj.get("label") or "").strip().lower(), location.id)
        except Exception as e:  # one bad row hides its own things only
            logger.debug(f"things at {location.id} unreadable: {e}")
    places.pop("", None)
    return places


def _read_tells(session, beat: StoryBeat, reads: Iterable[str], names: Iterable[str],
                common: Iterable[str], things: dict[str, str] | None = None) -> bool:
    """Did the player read (or find) this beat's matter themselves? An entry
    for an authored Thing (`things`: label → place) that the beat is about —
    its label names a keyword, or it stands where the beat happens — whose
    words say the beat's first stage: two evidence keywords and half."""
    keywords = [str(k) for k in beat.delivery_keywords or [] if str(k).strip()]
    entries = [str(r) for r in reads or () if r]
    if not keywords or not entries:
        return False
    things = _thing_places(session) if things is None else things
    keywords = _first_stage(keywords, later_keywords(beat.directive, keywords), names, common)
    places: set[str] | None = None
    for entry in entries:
        read, found = _READ_ENTRY.match(entry), _FOUND_ENTRY.match(entry)
        if read:
            label, text = read.group("label").strip(), read.group("text")
        elif found:
            label = (found.group("label") or found.group("item")).strip()
            text = f"{found.group('item')} {found.group('text') or ''}"
        else:
            continue
        where = things.get(label.lower())
        if where is None:
            continue            # a speech line, a note: no Thing of the world
        if not mentioned_keywords(keywords, label):
            if places is None:
                places = _place_ids(session, beat)
            if not places or not _within(session, where, places):
                continue
        told, hits = evidence_told(keywords, f"{label}\n{text}", names, common, fallback=False)
        if told and len(hits) >= 2:
            return True
    return False


def _offered_quests(session, beat: StoryBeat) -> list[Quest]:
    """The quests a beat hands out: its activate_quest ops, plus any quest
    its directive names as an offer ("...offering the quest 'Teeth on the
    North Road'")."""
    quests: list[Quest] = []
    for op in (beat.consequence_met or []):
        if isinstance(op, dict) and op.get("op") == "activate_quest":
            quest = _get(session, Quest, op.get("quest_id"))
            if quest is not None and quest not in quests:
                quests.append(quest)
    directive = (beat.directive or "").lower()
    if "offer" in directive:
        for quest in session.query(Quest).all():
            title = (quest.title or "").strip().lower()
            if len(title) >= 4 and title in directive and quest not in quests:
                quests.append(quest)
    return quests


def later_act_quest_ids(session, player_id: str | None) -> set[str]:
    """Quests the story keeps for a LATER act: handed out by a beat of an
    act still to come (pending or armed), and by no beat of this act or an
    earlier one. Round 5, fleet6-wenna: "A Letter for a Buried Man" — the
    activate_quest of act-3 "What Maera Hid", whose scene has the letter
    "still under her roof, undelivered" — was pitched and finished in act 2,
    and the act-3 confession it was built for lost its premise. Offered once
    that act is current. Empty without a live campaign."""
    campaign = get_campaign_row(session, player_id)
    if campaign is None or campaign.status != "active":
        return set()
    act = campaign.current_act or 1
    later: set[str] = set()
    now: set[str] = set()
    for beat in _campaign_beats(session, campaign):
        if beat.status not in ("pending", "armed", "fired", "stalled"):
            continue
        ids = {q.id for q in _offered_quests(session, beat)}
        if beat.act > act:
            if beat.status in ("pending", "armed"):
                later |= ids
        else:
            now |= ids
    return later - now


def _beat_people(beat: StoryBeat) -> set[str]:
    """The NPC ids a beat is about: its lead target and its ops' people."""
    ids = {str(op["npc_id"]) for op in (beat.consequence_met or [])
           if isinstance(op, dict) and op.get("npc_id")}
    target = beat.lead_target if isinstance(beat.lead_target, dict) else {}
    if target.get("type") == "npc" and target.get("id"):
        ids.add(str(target["id"]))
    return ids


def _revealed_people(beat: StoryBeat, npcs: list[NPC]) -> list[NPC]:
    """The people a directive brings on stage: named right after a reveal
    verb. "Reveal Aldric Vane to Tamsin" — not "catch Maera at the bar"."""
    directive = beat.directive or ""
    found: list[NPC] = []
    for match in _REVEAL_VERB.finditer(directive):
        window = directive[match.end(): match.end() + _REVEAL_WINDOW].lower()
        for npc in npcs:
            if npc.name and npc.name.lower() in window and npc not in found:
                found.append(npc)
    return found


# Round 9, fleet10-bram T15: picking up the whistle delivered "The Whistle in
# the Hut" by play (read) — a beat whose substance is a man's entrance:
# "Taking it rouses Vex Marrow from cover nearby; … warns Bram to leave what
# isn't his … names the counting-house". Vex stood in the Hollow all along
# and never appeared; the act-1 antagonist stayed a silhouette. A
# confrontation, twist or setpiece that brings on someone the player has
# never spoken with is their scene: finding its object is only its cue
# (`_due_by_find`), and no words tell it without them (`mark_beat_delivery`).
_ENTRANCE_BEATS = ("confrontation", "twist", "setpiece")
_ROUSES = r"have|let|bring|stage|rouses?|wakes?|summons?|draws?\s+out|flushes?\s+out|brings?\s+out"


def _staged_people(beat: StoryBeat, npcs: list[NPC]) -> list[NPC]:
    """The people a directive puts on stage: brought on ("rouses Vex
    Marrow", "have Vane step out"), acting there (`_ON_STAGE`: "Vex Marrow
    steps out"), or revealed (`_revealed_people`). Never "do not have…"."""
    directive = beat.directive or ""
    found = list(_revealed_people(beat, npcs))
    for npc in npcs:
        name = " ".join((npc.name or "").split())
        words = [w for w in dict.fromkeys([name, name.split()[-1] if name else ""]) if len(w) >= 4]
        if npc in found or not words:
            continue
        who = "|".join(re.escape(w) for w in words)
        if re.search(rf"(?<!not )(?<!n't )(?<!never )\b(?:{_ROUSES})\s+(?:{who})(?![\w'’-])", directive, re.IGNORECASE) \
                or re.search(rf"(?<![\w'’-])(?:{who})\s+(?:{_ON_STAGE})\b", directive, re.IGNORECASE):
            found.append(npc)
    return found


def _entrance_people(session, beat: StoryBeat, player, npcs: list[NPC] | None = None) -> list[NPC]:
    """Who this beat brings on that the player has never spoken with (see
    above): alive, no beast, its lead's or ops' person or one it stages."""
    if player is None or beat.beat_type not in _ENTRANCE_BEATS:
        return []
    from src.services.dialogue import is_beast

    alive = [n for n in (npcs if npcs is not None else session.query(NPC).filter(NPC.status == "alive").all())
             if n.status == "alive"]
    ids = _beat_people(beat)
    staged = _staged_people(beat, alive)
    people = [n for n in alive if n.id in ids] + [n for n in staged if n.id not in ids]
    # A mood set on someone while another plays the scene is no entrance of
    # theirs (round-9 review: fleet10-tamsin's act-2 "Vex at the Drowned
    # Lantern" — Vex drinks in plain view, Vane's mood op made Vane an
    # entrant too, and the ENTRANCE note would have walked the villain into
    # the tavern; 21 such beats over the fleet campaigns, "on Vane's order")
    ops = [op for op in beat.consequence_met or [] if isinstance(op, dict) and op.get("npc_id")]
    acted = {str(op["npc_id"]) for op in ops if op.get("op") != "npc_mood"} | {n.id for n in staged}
    target = beat.lead_target if isinstance(beat.lead_target, dict) else {}
    if target.get("type") == "npc" and target.get("id"):
        acted.add(str(target["id"]))
    if any(n.id in acted for n in people):
        people = [n for n in people if n.id in acted]
    if not people:
        return []
    spoken = {row[0] for row in session.query(NPCRelationship.npc_id).filter(
        NPCRelationship.player_id == player.id).all()}
    people = [n for n in people if n.id not in spoken and not is_beast(n)]
    # ...nor anyone the story holds offstage for a scene other than this one:
    # their dossier keeps them to signs (`villain_held_line`), and an
    # ENTRANCE note would say the opposite
    return [n for n in people if not _held_for_another_scene(session, beat, player, n)] if people else []


def _held_for_another_scene(session, beat: StoryBeat, player, npc: NPC) -> bool:
    """Is `npc` OFFSTAGE (the villain before their scene, `villain_offstage`;
    one of their people before theirs, `_cast_offstage_people`) for a beat
    other than `beat`? False on any trouble."""
    try:
        campaign = _get(session, Campaign, beat.campaign_id)
        if campaign is None or campaign.status != "active":
            return False
        if npc.id == campaign.villain_npc_id:
            if not villain_offstage(session, player.id, npc):
                return False
            scene = _villain_scene(session, campaign, npc)
            return scene is not None and scene.id != beat.id
        if not any(n.id == npc.id for n in _cast_offstage_people(session, campaign, player)):
            return False
        scene = _first_scene(session, campaign, npc)
        return scene is not None and scene.id != beat.id
    except Exception as e:  # the entrance stands as written
        logger.debug(f"offstage check for {getattr(npc, 'name', '?')} skipped: {e}")
        return False


def _appeared(session, player_id: str, people: list[NPC], speaker_ids: Iterable[str], text: str) -> bool:
    """Did any of `people` come on this reply — a line of theirs, or the
    prose naming them by name or by what the player sees (their label)?"""
    speaking = set(speaker_ids or ())
    for npc in people:
        if npc.id in speaking:
            return True
        if mentioned_keywords([k for k in (npc.name, _shown_as(session, player_id, npc)) if k], text or ""):
            return True
    return False


def _due_by_find(session, campaign: Campaign, beat: StoryBeat, player, entering: list[NPC],
                 now: tuple[int, int]) -> bool:
    """The player found what a person's entrance hangs on, with that person
    here: the find is the cue, and the scene comes due now — armed, its
    backstop reached, the spacing waived (`_fire_next`). Elsewhere it waits
    for its own trigger. True if it came due."""
    here = getattr(player, "current_location_id", None)
    present = [npc for npc in entering if here and npc.current_location_id == here]
    if not present:
        return False
    if beat.status == "pending":
        _arm_beat(beat, now)
    beat.fallback_day, beat.fallback_hour = now
    _FOUND_DUE.setdefault(campaign.player_id, {})[beat.id] = [npc.id for npc in present]
    logger.info(f"campaign beat comes due by a find (never told by it): {beat.title} — "
                f"{', '.join(npc.name for npc in present)} step(s) on")
    return True


def _beat_places(beat: StoryBeat, locations: list[Location]) -> set[str]:
    """Where a beat happens: its location trigger, its lead's place, and
    any place its directive names."""
    places: set[str] = set()
    data = beat.trigger_data if isinstance(beat.trigger_data, dict) else {}
    if beat.trigger_type == "location" and data.get("location_id"):
        places.add(str(data["location_id"]))
    target = beat.lead_target if isinstance(beat.lead_target, dict) else {}
    if target.get("type") == "location" and target.get("id"):
        places.add(str(target["id"]))
    directive = (beat.directive or "").lower()
    places |= {loc.id for loc in locations if loc.name and len(loc.name) >= 4 and loc.name.lower() in directive}
    return places


def _met_in_place(session, beat: StoryBeat, player, npcs: list[NPC], locations: list[Location]) -> bool:
    people = _revealed_people(beat, npcs)
    if not people:
        return False
    places = _beat_places(beat, locations)
    visited = {loc.id for loc in locations if loc.visited}
    for npc in people:
        if npc.current_location_id not in places or npc.current_location_id not in visited:
            continue
        if session.query(NPCRelationship.id).filter(
            NPCRelationship.npc_id == npc.id, NPCRelationship.player_id == player.id,
            _name_known_clause(),           # knows their NAME: a row alone is a talk (fleet6)
        ).first() is not None:
            return True
    return False


def _apply_play_ops(session, beat: StoryBeat, campaign: Campaign, player) -> list[str]:
    """Only what outlives a scene play already delivered (see the table above)."""
    from src.services import party as party_mod

    here = getattr(player, "current_location_id", None)
    with_player = set(party_mod.member_ids(player))

    def staged(op: dict) -> bool:
        if op.get("to") in ("player", here):
            return False
        npc = _get(session, NPC, op.get("npc_id"))
        # Never into or out of the scene the player is in, nor out of their
        # party: nobody narrated them arriving or leaving
        return npc is None or (npc.id not in with_player and npc.current_location_id != here)

    kept = [
        op for op in (beat.consequence_met or [])
        if isinstance(op, dict) and (
            op.get("op") == "npc_mood" or (op.get("op") == "move_npc" and staged(op))
        )
    ]
    applied, _exempt = execute_ops(session, kept, campaign, player)
    return applied


def _deliver_by_play(session, campaign: Campaign, beats: list[StoryBeat], player,
                     now: tuple[int, int], events: list, cinematics: list, *,
                     text: str = "", speaker_ids: Iterable[str] = (),
                     said_by: dict[str, str] | None = None,
                     reasons: Iterable[str] = PLAY_REASONS,
                     offered_now: Iterable[str] = (),
                     common: Iterable[str] | None = None,
                     failed_roll: bool = False,
                     engaged=None,
                     player_words: str = "",
                     reads: Iterable[str] = ()) -> list[tuple[StoryBeat, str]]:
    """Deliver, without firing, every current-act beat play already lived
    through (see the block comment above). `text`/`speaker_ids`/`said_by`
    ({npc id: what they said}) are this turn's (the `keywords` reason needs
    them; without `said_by` — the CLI — the whole text is read); `offered_now`
    are quest ids being handed to the player right now (the opening), which
    count as taken. The `keywords` reason also reads the campaign's
    `common` words (computed when None), never counts on a turn whose roll
    failed (`failed_roll`: the person held it back), tells a hook beat
    only when `engaged(beat)` says the player took it up, and lets the
    player's own question (`player_words`) second a keyword their lines
    already said — never stand alone (fleet4-wenna T16: "What have you been
    seeing at the north wall?" — "a man at the wall… cloak the color of old
    ash"). `reads` are the player's journal entries (the `read` reason,
    `_read_tells`). Runs inside the caller's transaction. Returns
    [(beat, reason)]."""
    if player is None:
        return []
    reasons = set(reasons)
    speaker_ids = set(speaker_ids or ())
    offered_now = {str(q) for q in offered_now or ()}
    finale = _finale_beat(beats)
    owed = _owed_scenes(campaign)
    cache: dict[str, Any] = {}     # read once, and only if a beat needs it
    if common is not None:
        cache["common"] = list(common)

    def common_words() -> list[str]:
        if "common" not in cache:
            cache["common"] = campaign_common_words(campaign, beats)
        return cache["common"]

    def npcs() -> list[NPC]:
        if "npcs" not in cache:
            cache["npcs"] = session.query(NPC).filter(NPC.status == "alive").all()
        return cache["npcs"]

    def locations() -> list[Location]:
        if "locations" not in cache:
            cache["locations"] = session.query(Location).all()
        return cache["locations"]

    def names() -> list[str]:
        if "names" not in cache:
            cache["names"] = _known_vocabulary(session, campaign, player, public=False)
        return cache["names"]

    def things() -> dict[str, str]:
        if "things" not in cache:
            cache["things"] = _thing_places(session)
        return cache["things"]

    reads = [str(r) for r in reads or () if r]
    delivered: list[tuple[StoryBeat, str]] = []
    for beat in beats:
        if not _play_eligible(session, campaign, beat, finale, player):
            continue
        reveals = {str(i) for i in (beat.reveals_truth_ids or [])}
        reason = None
        # What a scene SAYS waits for the scene it follows, and a scene a
        # leak left owed is played, never told by the leak (fleet6-bram T19,
        # `_ahead_of`). A quest taken or a person met is a fact of the world
        # whatever the order (and an arming beat would be retired for it).
        in_order = not _predecessor_pending(session, beat)
        if reveals and "truths" in reasons and reveals <= _revealed_ids(campaign) and beat.id not in owed \
                and in_order:
            reason = "truths"
        elif not reveals and "met" in reasons and _REVEAL_VERB.search(beat.directive or "") \
                and _met_in_place(session, beat, player, npcs(), locations()):
            reason = "met"
        elif not reveals and "quest" in reasons:
            offered = _offered_quests(session, beat)
            # ...its job taken, and nothing else of its scene still to come
            # (fleet8-wenna T3, `_scene_owed`)
            if offered and all(q.status != QuestStatus.NOT_STARTED or q.id in offered_now for q in offered) \
                    and not _scene_owed(session, beat, player):
                reason = "quest"
        people = _beat_people(beat) & speaker_ids
        if reason is None and not reveals and "keywords" in reasons and text and people and not failed_roll \
                and in_order:
            # Their own lines, not the prose around them: the villain
            # snarling while the narration mentions a whistle and a last
            # stand is not his act-3 finale played out (fleet4-wenna T8/T10,
            # fleet2-wenna T7, fleet3-tamsin T9 all matched that way)
            words = "\n".join(said_by.get(p, "") for p in people) if said_by else text
            # What a second press adds is not owed (`later_keywords`)
            keywords = _first_stage(list(beat.delivery_keywords or []),
                                    later_keywords(beat.directive, beat.delivery_keywords or []),
                                    names(), common_words())
            told, hits = _played_keywords(keywords, words, names(), common_words()) \
                if words.strip() else (False, [])
            if not told and hits and player_words.strip():
                told = _played_keywords(keywords, f"{words}\n{player_words}", names(), common_words())[0]
            if told and (engaged is None or not is_hook_beat(beat) or engaged(beat)):
                reason = "keywords"
        # The player read it with their own eyes: their act, no hook to wait on
        # — unless it is someone's entrance: the find is its cue, and the
        # scene comes due with them here (fleet10-bram T15, `_due_by_find`)
        if reason is None and not reveals and "read" in reasons and reads and in_order \
                and _read_tells(session, beat, reads, names(), common_words(), things()):
            entering = _entrance_people(session, beat, player, npcs())
            if not entering:
                reason = "read"
            elif beat.id not in _FOUND_DUE.get(campaign.player_id, {}):
                _due_by_find(session, campaign, beat, player, entering, now)
        if reason is None:
            continue

        applied = _apply_play_ops(session, beat, campaign, player)
        beat.variant = beat.variant or "met"
        beat.applied_effects = applied
        logger.info(f"campaign beat delivered by play ({reason}), never fired: {beat.title}")
        events.append({
            "type": "campaign_beat",
            "beat_id": beat.id,
            "act": beat.act,
            "title": beat.title,
            "beat_type": beat.beat_type,
            "variant": beat.variant,
            "cinematic": "none",          # it already played; no card after the fact
            "clock_label": beat.clock_label,
            "campaign_title": campaign.title,
            "by_play": reason,
        })
        _deliver_beat(session, campaign, beat, now, events, cinematics)
        delivered.append((beat, reason))
    return delivered


def note_opening_offers(player_id: str, quest_ids: Iterable[str]) -> list[str]:
    """The opening hands the player their first job: any beat whose job was
    offering it is told already (fleet4-tamsin: "Coldiron Wants Eyes He Can
    Spare" sat waiting to offer the quest the opening had started). Returns
    the delivered titles. Never raises."""
    quest_ids = [str(q) for q in quest_ids or () if q]
    if not player_id or not quest_ids or not campaign_enabled():
        return []
    events: list[dict[str, Any]] = []
    cinematics: list[tuple] = []
    titles: list[str] = []
    try:
        with get_session() as session:
            campaign = get_campaign_row(session, player_id)
            player = session.get(Player, player_id)
            if campaign is None or campaign.status != "active" or player is None:
                return []
            beats = _campaign_beats(session, campaign)
            delivered = _deliver_by_play(session, campaign, beats, player, _now(session), events,
                                         cinematics, reasons=("quest",), offered_now=quest_ids)
            titles = [beat.title for beat, _reason in delivered]
            # A later act triggered by this job going active is not pulled
            # ahead by it: the opening made it true, not play (fleet6-wenna:
            # act 2 opened on turn 3 off the opening's own quest)
            held = list(campaign.pull_held or [])
            for beat in beats:
                data = beat.trigger_data if isinstance(beat.trigger_data, dict) else {}
                if (beat.act > (campaign.current_act or 1) and beat.status == "pending"
                        and beat.trigger_type == "quest_status" and str(data.get("quest_id")) in quest_ids
                        and data.get("status") == "active" and beat.id not in held):
                    held.append(beat.id)
            if held != list(campaign.pull_held or []):
                campaign.pull_held = held
            session.commit()
    except Exception as e:
        logger.warning(f"opening: beats offering the first quest not marked: {e}")
        return []
    _dispatch_cinematics(player_id, cinematics)
    _queue_events(player_id, events)
    return titles


def _open_next_act(session, campaign: Campaign, events: list, cinematics: list) -> None:
    """current_act + 1: arm it, announce it (event + act card)."""
    campaign.current_act = (campaign.current_act or 1) + 1
    arm_act(session, campaign, campaign.current_act)
    entry = _act_entry(campaign, campaign.current_act)
    events.append({
        "type": "campaign_act",
        "act": campaign.current_act,
        "acts_total": ACTS_TOTAL,
        "act_title": entry.get("title") or "",
        "act_premise": entry.get("premise") or "",
        "cinematic": "act_open",
        "campaign_title": campaign.title,
    })
    request = _cinematic_request("act_open", campaign, campaign.current_act, None, None)
    if request:
        cinematics.append(request)


def _check_act_transition(session, campaign: Campaign, last_beat: StoryBeat,
                          now: tuple[int, int], events: list, cinematics: list) -> None:
    """An act ends when every one of its beats is delivered or skipped."""
    act = campaign.current_act or 1
    act_beats = [b for b in _campaign_beats(session, campaign) if b.act == act]
    if not act_beats or any(b.status not in ("delivered", "skipped") for b in act_beats):
        return

    if act < ACTS_TOTAL:
        _open_next_act(session, campaign, events, cinematics)
        logger.info(f"campaign: act {act} complete — arming act {campaign.current_act}")
        return

    campaign.status = "won" if (last_beat.variant or "met") == "met" else "lost"
    entry = _act_entry(campaign, ACTS_TOTAL)
    events.append({
        "type": "campaign_resolved",
        "status": campaign.status,
        "cinematic": "finale",
        "campaign_title": campaign.title,
        "epitaph": f"Act {ACTS_TOTAL} — {entry.get('title') or ''}".strip(" —"),
    })
    request = _cinematic_request("finale", campaign, ACTS_TOTAL, None, None)
    if request:
        cinematics.append(request)
    logger.info(f"campaign resolved: {campaign.title} — {campaign.status}")


# ----------------------------------------------------------------------
# Read models: HUD state + the spoiler-redacting public view
# ----------------------------------------------------------------------

def campaign_state(player_id: str) -> dict[str, Any] | None:
    """The spoiler-free summary that rides on the `state` SSE event.

    Never emits a sealed truth, an unfired beat title, or a directive — a
    curious player with devtools must not be able to read the ending.
    """
    if not player_id:
        return None
    with get_session() as session:
        campaign = get_campaign_row(session, player_id)
        if campaign is None or campaign.status in ("pending", "failed"):
            return None
        now = _now(session)
        beats = _campaign_beats(session, campaign)
        act = campaign.current_act or 1
        entry = _act_entry(campaign, act)
        act_beats = [b for b in beats if b.act == act]
        resolved = campaign.status != "active"
        return {
            "title": campaign.title,
            "act": act,
            "acts_total": ACTS_TOTAL,
            "act_title": entry.get("title") or "",
            "act_premise": entry.get("premise") or "",
            "beats_delivered": len([b for b in act_beats if b.status in ("delivered", "skipped")]),
            "beats_total": len(act_beats),
            "status": campaign.status,
            "clocks": [] if resolved else _clocks(beats, now),
            "delivered_titles": [b.title for b in beats if b.status == "delivered"],
            "revealed_truths": [
                t.get("statement") for t in (campaign.fixed_truths or [])
                if isinstance(t, dict) and t.get("revealed") and t.get("statement")
            ],
        }


def campaign_dm_view(player_id: str) -> dict[str, Any]:
    """What the `get_campaign` read tool hands the DM.

    Unlike `campaign_public_view` this DOES include sealed truth statements —
    the DM has to know what it is keeping from the player — each flagged
    `revealed: False`. Delivered beats only; never a pending beat.
    """
    state = campaign_state(player_id)
    if state is None:
        return {"campaign": None}

    with get_session() as session:
        campaign = get_campaign_row(session, player_id)
        beats = _campaign_beats(session, campaign)
        return {
            "campaign": {
                "title": campaign.title,
                "premise": campaign.premise or "",
                "stakes_personal": campaign.stakes_personal or "",
                "act_title": state["act_title"],
                "act_premise": state["act_premise"],
                "status": campaign.status,
            },
            "act": state["act"],
            "acts_total": ACTS_TOTAL,
            "villain_name": campaign.villain_name or "",
            "truths": [
                {"kind": t.get("kind"), "statement": t.get("statement"),
                 "revealed": bool(t.get("revealed"))}
                for t in (campaign.fixed_truths or []) if isinstance(t, dict)
            ],
            "clocks": state["clocks"],
            "delivered_beats": [
                {"act": b.act, "title": b.title, "day": b.delivered_day}
                for b in beats if b.status == "delivered"
            ],
        }


def campaign_public_view(player_id: str, spoilers: bool = False) -> dict[str, Any]:
    """`GET /api/campaign/{player_id}`. Sealed truth statements are redacted
    unless `spoilers` is set (a documented author/debug flag)."""
    state = campaign_state(player_id)
    if state is None:
        return {"campaign": None}

    with get_session() as session:
        campaign = get_campaign_row(session, player_id)
        beats = _campaign_beats(session, campaign)
        truths = [t for t in (campaign.fixed_truths or []) if isinstance(t, dict)]
        revealed = [
            {"kind": t.get("kind"), "statement": t.get("statement")}
            for t in truths if t.get("revealed")
        ]
        sealed = [t for t in truths if not t.get("revealed")]
        payload = {
            "campaign": dict(state, premise=campaign.premise or "",
                             villain_name=campaign.villain_name or "",
                             stakes_personal=campaign.stakes_personal or ""),
            "delivered_beats": [
                {"act": b.act, "title": b.title, "day": b.delivered_day}
                for b in beats if b.status == "delivered"
            ],
            "revealed_truths": revealed,
            "clocks": state["clocks"],
            "sealed_truth_count": len(sealed),
        }
        if spoilers:
            payload["sealed_truths"] = [
                {"kind": t.get("kind"), "statement": t.get("statement")} for t in sealed
            ]
        return payload


# ----------------------------------------------------------------------
# The DM context block
# ----------------------------------------------------------------------

SEALED_PROMISE_LINE = (
    "  NO PROMISES OVER SEALED TRUTHS: nobody offers to tell what is SEALED — never \"bring me proof "
    "and I'll tell you what I know\". People bargain with what they can hand over now: coin, a pass, "
    "a favour, a name the player already knows. When the story reveals a truth, its beat says who tells it."
)
# Round 7, fleet8-bram: T16 the herbalist had "seen him… lips moving over the
# roster" — a sealed secret about the Captain, from someone it never names
# (one term said: the leak check needs two); T7 the Captain "carried its twin
# for six years before I lost it to a man I thought the Watch had buried" — a
# past for a story object that the truths say otherwise, journaled as a clue
SEALED_KNOWERS_LINE = (
    "  WHO KNOWS A SEALED TRUTH: only the people it names — anyone else knows nothing of it, not one word "
    "that points at it. Nobody invents a past for a person or thing a truth touches (where it came from, "
    "who held it, what became of them): a guarded person deflects or refuses, and what no truth says stays "
    "unsaid until a beat says it."
)


def format_campaign_context(tick: dict[str, Any] | None) -> str:
    """The campaign block injected into the DM's context.

    Contains: the act, delivered beat titles, EVERY fixed truth (sealed ones
    carry a do-not-say instruction — the DM must know what it is keeping), the
    live clocks, and the one beat firing right now. Contains NOT: any unfired
    beat's title, directive, or consequences.
    """
    if not tick or not tick.get("campaign"):
        return ""

    campaign = tick["campaign"]
    lines = [f"### CAMPAIGN: {campaign.get('title') or 'Untitled'}"]
    act_title = campaign.get("act_title") or ""
    act_premise = campaign.get("act_premise") or ""
    lines.append(
        f"Act {campaign.get('act', 1)} of {campaign.get('acts_total', ACTS_TOTAL)} — "
        f"\"{act_title}\": {act_premise}".rstrip()
    )

    delivered = tick.get("delivered_titles") or []
    if delivered:
        lines.append("Story so far (already delivered): " + " · ".join(delivered))
    lines.append(
        f"This act: {tick.get('beats_delivered', 0)} of {tick.get('beats_total', 0)} beats "
        "delivered. The rest are sealed from you — do NOT foreshadow, hint at, or "
        "gesture toward anything you have not been told."
    )

    # A succession note rides on the fired beat's directive when there is one
    if tick.get("note") and not tick.get("fired_beat"):
        lines.append(f"NOTE: {tick['note']}")

    truths = tick.get("truths") or []
    if truths:
        lines.append("")
        lines.append(
            "FIXED TRUTHS — facts of this world. You may not contradict, soften, or replace them."
        )
        for truth in truths:
            kind = truth.get("kind") or "truth"
            statement = truth.get("statement") or ""
            if truth.get("revealed"):
                lines.append(f"  [KNOWN]  {kind}: {statement}")
            elif truth.get("reveal_now"):
                lines.append(
                    f"  [REVEAL NOW] {kind}: {statement} The beat below brings this out THIS "
                    "reply, exactly as far as its directive says."
                )
            else:
                lines.append(
                    f"  [SEALED] {kind}: {statement} The player does NOT know this. "
                    "Write the scene as if you did not know it either. No knowing "
                    "looks, no NPC almost-saying it."
                )
        # Round 4, fleet5-bram T2: Coldiron promised "come back with something
        # I can use, I'll tell you what I know" about a SEALED truth, then had
        # to stonewall the payoff for three turns (T16-T18)
        if any(not t.get("revealed") and not t.get("reveal_now") for t in truths):
            lines.append(SEALED_PROMISE_LINE)
            lines.append(SEALED_KNOWERS_LINE)

    clocks = tick.get("clocks") or []
    if clocks:
        lines.append("")
        lines.append("CLOCKS — visible countdowns. NPCs know about these and may speak of them.")
        for clock in clocks:
            if clock["urgency"] == "overdue":
                remaining = "overdue"
            else:
                remaining = f"{clock['days_left']} days, {clock['hours_left']} hours"
            lines.append(
                f"  - {clock['label']}: {remaining} (Day {clock['day']}, {clock['hour']:02d}:00)"
            )

    continuity = tick.get("continuity") or []
    if continuity:
        lines.append("")
        for line in continuity:
            lines.append(f"CONTINUITY: {line}")

    fired = tick.get("fired_beat")
    if fired:
        lines.append("")
        header = "STORY HOOK — SHOW IT, THE PLAYER CHOOSES" if fired.get("hook") else "MUST DELIVER"
        lines.append(
            f"THIS TURN — {header}  [{fired.get('beat_type')} · \"{fired.get('title')}\"]"
        )
        lines.append(f"  {fired.get('directive') or ''}".rstrip())
        for note in fired.get("notes") or []:
            lines.append(f"  NOTE: {note}")
        if fired.get("fixed_truths"):
            lines.append("  This beat establishes as fact:")
            for statement in fired["fixed_truths"]:
                lines.append(f"    - {statement}")
        if fired.get("applied_effects"):
            lines.append(
                "  ALREADY APPLIED BY THE ENGINE (narrate as fact; do NOT call tools to redo these):"
            )
            for line in fired["applied_effects"]:
                lines.append(f"    - {line}")
        if fired.get("hook"):
            # fleet6-bram T14: "let it interrupt them" became "You press him"
            lines.append(
                "  Show the hook in this response — a tell, or the person coming to the player. "
                "The rest waits for the player to take it up; never take it up for them."
                if (fired.get("attempt") or 1) == 1 else
                "  The player has not taken this up yet. Keep it in view if the scene allows "
                "(the person lingers, a second tell) — never force it, never act for the player."
            )
        elif (fired.get("attempt") or 1) > 1:
            # No "before anything else": that nag turned a re-sent glimpse
            # of Vex into a full face-to-face reveal (playtest 2026-10-04)
            lines.append(
                "  You were given this beat before and have not delivered it yet. The "
                "world has ALREADY changed. Deliver it in this response — if your last "
                "reply already showed part of it, carry it on from there; never stage "
                "it bigger than the directive says."
            )
        else:
            lines.append(
                "  Deliver this IN THIS RESPONSE. If the player is doing something else, "
                "let it interrupt them — that is what a story is."
            )

    return "\n".join(lines)


# Round 7, fleet8-wenna T3 ("I head toward the North Gatehouse… I'll get that
# pass"): Piper "catches your sleeve before you've gone three steps", and the
# trip she declared never happened in 21 turns. A beat firing as the player
# sets out (the travel line) goes with them, never stops them: its people
# catch up at the destination, the world's clock reaches them on the way, and
# a scene bound to the place they leave waits there.

def departure_note(player_id: str | None, player_input: str | None) -> str:
    """The NOTE line for the beat firing this reply when the player's own
    words set out for another place (`travel_intent`), else "". Reads the
    tick just run (`get_last_tick`). Never raises."""
    tick = _LAST_TICK.get(player_id or "") or {}
    fired = tick.get("fired_beat")
    if not fired or not (player_input or "").strip() or str(player_input).lstrip().startswith("["):
        return ""
    try:
        from src.services import travel_intent
        from src.services.dialogue import is_beast

        # Every phrase, the last that is a place winning — as the travel line
        # itself reads them (`travel_intent._directive`): a later phrase that
        # names no place ("…Then I walk over to Piper") never hides the one
        # that does (fleet8-wenna T3 lost its travel line to such a phrase)
        phrases = list(reversed(travel_intent.destination_phrases(player_input)))
        if not phrases:
            return ""
        with get_session() as session:
            player = session.get(Player, player_id)
            place = None
            for phrase in phrases if player is not None else ():
                found = travel_intent.resolve(session, player, phrase)
                if found is not None:
                    place = found.get("place")
                    break
            beat = _get(session, StoryBeat, fired.get("beat_id"))
            if place is None or beat is None or place.id == player.current_location_id:
                return ""
            here = player.current_location_id
            people = [n.name for n in _beat_cast(session, beat)
                      if n.current_location_id == here and not is_beast(n)]
            deadline = beat.beat_type == "deadline_event"
            bound_here = bool(_place_ids(session, beat)) and _within(session, here, _place_ids(session, beat))
            destination = place.name
    except Exception as e:  # a note the reply can live without
        logger.warning(f"departure note unavailable: {e}")
        return ""
    lead = (f"  NOTE: THE PLAYER IS LEAVING for {destination} this reply, in their own words — this beat never "
            "stops them: the travel line's move comes first.")
    if deadline:
        return lead + (" This is the world's clock: deliver it in this reply — it reaches them on the way or as "
                       "they arrive.")
    # It may wait, so it is not owed this reply: the audit (which reads this
    # tick's copy) does not nag a beat the player walked away from
    if isinstance(tick.get("audit"), dict):
        tick["audit"]["beat"] = None
    if people:
        who = " and ".join(people[:2])
        return (lead + f" {who} {'catch' if len(people[:2]) > 1 else 'catches'} up with them there or on the "
                f"road (write @npc-move {people[0]} | {destination} before their first line), or the beat waits "
                "for a later reply. Never hold the player back to deliver it.")
    if bound_here:
        return lead + " This beat happens where they are leaving: it waits there until they come back."
    return lead + " Deliver it where they arrive, or let it wait."


# The fired beat's closing order (`format_campaign_context`) that a departure
# contradicts: "let it interrupt them" is the line Piper caught Wenna's
# sleeve on (fleet8-wenna T3)
_DELIVER_NOW = re.compile(
    r"\n  (?:Deliver this IN THIS RESPONSE\. If the player is doing something else, let it interrupt "
    r"them — that is what a story is\.|You were given this beat before and have not delivered it yet\. "
    r"[^\n]*)(?=\n|$)"
)


def with_departure(briefing: str, note: str) -> str:
    """The campaign briefing with the departure NOTE (`departure_note`) in
    place of the beat's "deliver it now / let it interrupt them" close."""
    if not note:
        return briefing
    return f"{_DELIVER_NOW.sub('', briefing or '', count=1)}\n{note}"


# Round 9, fleet10-wenna T7: "Sorrel, you looked north when you mentioned the
# howls. Have you seen something up there?" — exactly the question "A Tell at
# the Stable" waits for — got "I haven't seen anything": the trust-gated
# secret in his dossier outweighed a beat the engine had already marked told
# on the glance (`setup_keywords`). When the player asks the person of a live
# "if asked … admits" beat about it (the beat fired or parked, or armed at
# its place with them there), the admission is due THIS reply.
# A sentence that asks, and asks after what someone saw or keeps (or after
# the words of the beat's own tell and asking: "the howls", "what's on his
# mind"), put to them — named in it or the sentence before, unless the chip
# chose them or nobody else is there. Replayed over every fleet transcript
# input in which the beat's person answered while its admission was still
# unsaid: 20 inputs, 6 lines — 5 the player asking them what they saw or
# heard, 1 "did you hear that?" about the howl just past (fleet10-wenna T19);
# one true ask missed ("what exactly are they reacting to?", fleet9-bram T7).
_ASKS = re.compile(
    r"\?|\b(?:tell\s+me|tell\s+us|what\s+(?:did|do|have)\s+you|have\s+you\s+(?:seen|heard|noticed)"
    r"|did\s+you\s+(?:see|hear|notice)|i\s+(?:want|need)\s+to\s+know)\b", re.IGNORECASE)
_ABOUT_IT = re.compile(
    r"\b(?:seen|saw|see|heard|hear|hearing|noticed?|mind|wrong|bother\w*|afraid|scared|worried|hiding"
    r"|look(?:ed|ing)?\s+(?:north|at|toward|towards|out|up)|glanc\w*|star(?:e|ed|es|ing))\b", re.IGNORECASE)
_SENTENCES = re.compile(r"(?<=[.!?…])[\"”’*_)]*\s+|\n+")
# The asking's own verbs and the tell's small words cue nothing
_CUE_STOP = frozenset({"admit", "admits", "aske", "asked", "press", "pressed", "toward", "towards", "down", "keep",
                       "keeps", "still", "while", "there", "here", "about", "into", "onto", "with", "from",
                       "what", "when", "then", "have", "been", "being", "nearby"})


def _asks_about(text: str, cue_forms: set[str], who: str | None = None) -> bool:
    """Does one sentence of `text` ask, and ask after it (see above) — put
    to `who` (an alternation of their names) when given: named in it, or
    called by name in the sentence before ("Sorrel, you looked north… Have
    you seen something up there?")?"""
    sentences = _SENTENCES.split(text or "")
    named = re.compile(rf"(?<![\w'’-])(?:{who})(?![\w-])", re.IGNORECASE) if who else None
    called = re.compile(rf"(?:^|[\"“])\s*(?:{who})\s*[,—–]|,\s*(?:{who})\s*[.!?…]", re.IGNORECASE) if who else None
    for i, sentence in enumerate(sentences):
        if not _ASKS.search(sentence) or not (_ABOUT_IT.search(sentence) or _text_forms(sentence) & cue_forms):
            continue
        if named is None or named.search(sentence) or (i and called.search(sentences[i - 1])):
            return True
    return False


def _asked_whom(session, player_id: str, people: list[NPC], present: list[NPC],
                addressed_npc_id: str | None) -> list[tuple[NPC, str | None]]:
    """Whom of `people` the player's words may be put to, each with the
    names that must call them in the asking (`_asks_about`): none for the
    one their talk chip chose or the only person there; else their name's
    words or the label the player knows them by."""
    if addressed_npc_id and any(n.id == addressed_npc_id for n in present):
        return [(npc, None) for npc in people if npc.id == addressed_npc_id]   # the chip chose
    out: list[tuple[NPC, str | None]] = []
    for npc in people:
        if len(present) == 1:
            out.append((npc, None))
            continue
        words = [w for w in (npc.name or "").split() if len(w) >= 3]
        label = _shown_as(session, player_id, npc)
        if label and label != npc.name:
            words.append(label)
        if words:
            out.append((npc, "|".join(re.escape(w) for w in words)))
    return out


def asked_note(player_id: str | None, player_input: str | None, addressed_npc_id: str | None = None) -> str:
    """The binding line for the reply when the player's words ask the person
    of a live "if asked … admits" beat about it (see above), else "".
    Never raises."""
    text = str(player_input or "").strip()
    if not player_id or not text or text.startswith("[") or not _ASKS.search(text):
        return ""
    try:
        from src.services.dialogue import is_beast

        with get_session() as session:
            campaign = get_campaign_row(session, player_id)
            player = session.get(Player, player_id)
            if campaign is None or campaign.status != "active" or player is None:
                return ""
            present = [n for n in session.query(NPC).filter(
                NPC.current_location_id == player.current_location_id, NPC.status == "alive").all()
                if not is_beast(n)]
            if not present:
                return ""
            beats = _campaign_beats(session, campaign)
            name_forms = _text_forms(" ".join(_known_vocabulary(session, campaign, player, public=False)))
            for beat in beats:
                # (an armed one that un-seals a truth is no line's to open:
                # the seal guard would cut it — it fires first)
                live = beat.status in ("fired", "stalled") or (
                    beat.status == "armed" and beat.act <= (campaign.current_act or 1)
                    and not beat.reveals_truth_ids and _location_scene_here(session, beat, player))
                shaped = admission(beat.directive) if live else None
                if shaped is None:
                    continue
                setup, payload = shaped
                ids = _beat_people(beat)
                people = [n for n in present if n.id in ids] + [
                    n for n in _named_people(setup, present) if n.id not in ids]
                # ...after what the tell shows and the asking names ("the
                # howls", "what's on his mind"), names aside
                asking = _ADMITS.search(beat.directive or "")
                cues = {form for form in _text_forms(f"{setup} {asking.group(0) if asking else ''}")
                        if len(form) >= 4 and form not in _GENERIC_WORDS and form not in _CUE_STOP} - name_forms
                npc = next((npc for npc, who in _asked_whom(session, player_id, people, present, addressed_npc_id)
                            if _asks_about(text, cues, who)), None)
                if npc is None:
                    continue
                label = _shown_as(session, player_id, npc)
                logger.info(f"campaign: the player asked — {beat.title} is admitted this reply")
                return (f"[THE PLAYER ASKED — {label} ADMITS IT NOW: {payload.rstrip('.')}. "
                        f"This reply {label} says it, in their own words as a speaker line — the asking is done: "
                        "no denial, no roll, no trust needed (the story's beat says it now, whatever their "
                        "dossier keeps back). Only this much; whatever the directive holds for a further press "
                        "waits for one.]")
    except Exception as e:  # never cost the turn its context
        logger.warning(f"asked note unavailable: {e}")
    return ""


# Round 3, fleet4-bram (campaign failed): with nothing telling the DM it was
# improvising, it chained the authored boss into the wolves' aftermath — a
# level-1 loss the player never chose — and after quest 1 nothing pushed:
# no clock, no scheduled threat, six turns of haggling at the inn.
NO_SPINE_CONTEXT = (
    "### NO STORY SPINE YET — this character's campaign is not authored (it may arrive later). "
    "Keep authored bosses for a later confrontation: a threat, a test, a glimpse — never a fight "
    "the player did not choose. Every threat you voice gets an @schedule line, so the world "
    "follows through on it."
)


# Round 3: fleet4-wenna T11, the unrevealed villain improvised a grievance
# against Coldiron that contradicts the SEALED truth (it was Coldiron's
# predecessor); fleet4-tamsin T7-T8, he explained his motive on request. Until
# the story reveals them, the villain's dossier says what they may do.
# Round 4, fleet5-tamsin T16: still unrevealed, he named his price ("I want
# his badge off every chest in that town") — the act-2 truth that the toll is
# the Watch disbanded, which no keyword check can catch in other words. So
# no price either: what would make them stop is the story's to reveal too.
VILLAIN_HELD_LINE = (
    "    STORY VILLAIN, NOT YET REVEALED: a stranger with menace — threats, tests and bargains "
    "only. No name, no grievance, no history, no plan, no price or demand: who they are, "
    "why, and what would make them stop are the story's to reveal, later."
)
# Already introduced by name (fleet4-tamsin T8), still nothing of the why
VILLAIN_HELD_LINE_NAMED = (
    "    STORY VILLAIN, NOT YET REVEALED: menace without explanation — threats, tests and "
    "bargains only. No grievance, no history, no plan, no price or demand: why they do "
    "this and what would make them stop are the story's to reveal, later."
)


# Round 7, fleet8-tamsin T17-T19: the villain — seeded at Wolf's Hollow, the
# act-1 job's destination — stepped out of the birches with a wolf at heel
# and demanded the whistle back ("the easy way, or the other one"): act 3's
# "The Hollow Pack Answers the Whistle", played in act 1 off a failed
# stealth roll. Until the first beat that brings them on has fired, the
# villain is OFFSTAGE — signs only — and a line of theirs is flagged by the
# audit (turn_audit, `offstage_villain` in the tick's audit copy).
VILLAIN_OFFSTAGE_LINE = (
    "    STORY VILLAIN — OFFSTAGE until the story brings them on: signs only (fresh tracks, a whistle note in "
    "the dark, a silhouette at a distance that is gone when looked for). No speaker lines, no face to face, "
    "no threat, demand or bargain — if the player goes after them, they find where they were, not them."
)
_STAGING_BEATS = ("confrontation", "setpiece")


_ON_STAGE = (r"steps?|stands?|waits?|appears?|emerges?|confronts?|meets?|produces?|signals?|whistles?"
             r"|demands?|offers?|makes?|arrives?|comes?|walks?|speaks?|tells?|names?|leads?|holds?"
             # ...and in the act: fleet7-tamsin's setpiece "The Counting at
             # the Hollow" has "Aldric Vane overseeing with the whistle at his hip"
             r"|overseeing|watching|waiting|standing|holding|leading|directing|commanding|whistling"
             r"|signall?ing")


def _villain_scene(session, campaign: Campaign, villain: NPC) -> StoryBeat | None:
    """The first beat (by act, then sequence) that brings the villain on: it
    reveals who they are, leads to them or acts on them, or — a
    confrontation or a setpiece — has them act ("have Vane produce his own
    whistle", "where Vane waits with the pack"; "on Vane's order" and "a
    message that Vane can reach her" are someone else's scene, fleet8-tamsin's
    "The Fangs Make an Example"). A beat of any kind that stages them
    ("have Vane step out…") brings them on too — never "do not have Vane…"."""
    identity = {str(t.get("id")) for t in campaign.fixed_truths or []
                if isinstance(t, dict) and t.get("kind") == "villain_identity"}
    name = " ".join((villain.name or "").split())
    words = [w for w in dict.fromkeys([name, name.split()[-1] if name else ""]) if len(w) >= 4]
    who = "|".join(re.escape(w) for w in words)
    staged = re.compile(rf"(?<!not )(?<!n't )(?<!never )\b(?:have|let|bring|stage)\s+(?:{who})(?![\w'’-])",
                        re.IGNORECASE) if words else None
    acts = re.compile(rf"(?<![\w'’-])(?:{who})\s+(?:{_ON_STAGE})\b", re.IGNORECASE) if words else None
    for beat in _campaign_beats(session, campaign):
        if beat.status == "skipped":
            continue
        target = beat.lead_target if isinstance(beat.lead_target, dict) else {}
        # A mood op is no entrance ("menacing", set while Vex does the deed)
        moved = any(isinstance(op, dict) and op.get("op") == "move_npc" and op.get("npc_id") == villain.id
                    for op in beat.consequence_met or [])
        if identity & {str(i) for i in beat.reveals_truth_ids or []} or target.get("id") == villain.id or moved:
            return beat
        directive = beat.directive or ""
        if staged is not None and staged.search(directive):
            return beat
        if beat.beat_type in _STAGING_BEATS and acts is not None and acts.search(directive):
            return beat
    return None


def villain_offstage(session, player_id: str | None, npc) -> bool:
    """Is this the live campaign's villain, still unrevealed, before the
    first beat that brings them on has fired (see above)? False when the
    story authored no such beat."""
    if npc is None or not player_id:
        return False
    campaign = get_campaign_row(session, player_id)
    if campaign is None or campaign.status != "active" or npc.id != campaign.villain_npc_id:
        return False
    if any(isinstance(t, dict) and t.get("kind") == "villain_identity" and t.get("revealed")
           for t in campaign.fixed_truths or []):
        return False
    scene = _villain_scene(session, campaign, npc)
    return scene is not None and scene.status in ("pending", "armed")


def offstage_ids(session, player_id: str | None) -> set[str]:
    """Who the player's screen leaves out of a scene: the story's villain
    before their first scene. Round-7 spine review: the seed parks him at
    the act-1 destination, so his sprite stood there while his dossier said
    OFFSTAGE. He is there, unseen; the DM may show signs of him. Round 9:
    the villain's people held for an entrance of their own too
    (`_cast_offstage_people`)."""
    campaign = get_campaign_row(session, player_id) if player_id else None
    if campaign is None or not campaign.villain_npc_id:
        return set()
    villain = session.get(NPC, campaign.villain_npc_id)
    hidden = {villain.id} if villain_offstage(session, player_id, villain) else set()
    try:
        player = session.get(Player, player_id)
        hidden |= {npc.id for npc in _cast_offstage_people(session, campaign, player)}
    except Exception as e:  # the villain's rule stands on its own
        logger.debug(f"offstage cast unavailable: {e}")
    return hidden


# Round 9 (fleet10-tamsin T12-T20, fleet10-bram T11-T20): Vex Marrow — the
# successor, a dangerous Ashen Fangs scout the seed parks at the act-1
# destination — stood in Wolf's Hollow for nine replies; two fights happened
# around him, the prose said "alone", the canvas drew him, and at bram's T15
# the entrance his own act-1 beat gives him ("Taking it rouses Vex Marrow
# from cover nearby") never came. The villain's people are held like the
# villain: the successor, and anyone of the villain's faction whom a beat
# still to come is the first to bring in, who has never spoken with the
# player, is OFFSTAGE until that beat fires — off the canvas and the state's
# people (`offstage_ids`), a dossier of signs only. Never a beast (the pack
# is the encounter director's), an ally, a quest giver, a trader, the
# target of a step or a lead, or someone in the party. Once their beat fires
# they step on (`_directive_notes`: ENTRANCE).
CAST_OFFSTAGE_LINE = (
    "    OFFSTAGE until the story brings them on — a later beat is their entrance: signs only (a shape at a "
    "distance, fresh tracks, a sound in the dark). No speaker lines, no face to face, no fight: if the player "
    "goes after them, they find where they were, not them."
)
_TRADER = re.compile(r"\b(?:merchant|trader|hawker|peddler|pedlar|vendor|shopkeep\w*|seller|supplier|dealer|fence|smith"
                     r"|innkeep\w*|barkeep\w*|keeper|apothecar\w*|herbalist)\b", re.IGNORECASE)


def _first_scene(session, campaign: Campaign, npc: NPC, beats: list[StoryBeat] | None = None) -> StoryBeat | None:
    """The first beat (by act, then sequence; never a skipped one) whose cast
    takes this person in (`_beat_cast`: lead, ops, named)."""
    for beat in beats if beats is not None else _campaign_beats(session, campaign):
        if beat.status != "skipped" and _beat_cast(session, beat, npcs=[npc]):
            return beat
    return None


def _cast_offstage_people(session, campaign: Campaign | None, player) -> list[NPC]:
    """The villain's people held for a scene of their own (see above)."""
    if campaign is None or campaign.status != "active" or player is None:
        return []
    from src.services import party as party_mod
    from src.services.dialogue import is_beast
    from src.services.objectives import normalize_objectives

    villain = _get(session, NPC, campaign.villain_npc_id)
    faction = getattr(villain, "faction_id", None)
    side = [npc for npc in session.query(NPC).filter(NPC.status == "alive").all()
            if npc.id != campaign.villain_npc_id
            and (npc.id == campaign.successor_npc_id or (faction and npc.faction_id == faction))]
    if not side:
        return []
    keep = {str(i) for i in campaign.ally_npc_ids or [] if i} | set(party_mod.member_ids(player))
    keep |= {q.assigned_by_npc_id for q in session.query(Quest).filter(Quest.assigned_by_npc_id.isnot(None))}
    keep |= {row[0] for row in session.query(NPCRelationship.npc_id).filter(
        NPCRelationship.player_id == player.id).all()}
    beats = _campaign_beats(session, campaign)
    for beat in beats:
        target = beat.lead_target if isinstance(beat.lead_target, dict) else {}
        if beat.status in ("armed", "fired", "stalled") and target.get("type") == "npc":
            keep.add(str(target.get("id")))
    for quest in session.query(Quest).filter(Quest.status == QuestStatus.ACTIVE).all():
        for step in normalize_objectives(quest.objectives):
            if not step.get("done") and step.get("target_id"):
                keep.add(str(step["target_id"]))
    out = []
    for npc in side:
        if npc.id in keep or is_beast(npc) or _TRADER.search(npc.profession or ""):
            continue
        scene = _first_scene(session, campaign, npc, beats)
        if scene is not None and scene.status in ("pending", "armed"):
            out.append(npc)
    return out


def cast_offstage(session, player_id: str | None, npc) -> bool:
    """Is this one of the villain's people held for their entrance (see
    above)? False on any trouble."""
    if npc is None or not player_id:
        return False
    try:
        campaign = get_campaign_row(session, player_id)
        return any(n.id == npc.id for n in _cast_offstage_people(session, campaign, session.get(Player, player_id)))
    except Exception as e:
        logger.debug(f"offstage cast check failed for {getattr(npc, 'id', '?')}: {e}")
        return False


def _shown_as(session, player_id: str, npc: NPC) -> str:
    """What the player knows this person as: their name once heard, else
    their role label."""
    try:
        from src.services.dialogue import player_has_met, stranger_label

        return stranger_label(npc, met=player_has_met(session, player_id, npc.id)) or npc.name
    except Exception:  # pragma: no cover - a label is a nicety
        return npc.name


def _name_known_clause():
    """Relationship rows whose NPC the player knows by NAME — a row is the
    relationship, written on the first exchange (dialogue: who knows whom)."""
    from src.services.dialogue import name_known_clause

    return name_known_clause()


def villain_held_line(session, player_id: str | None, npc) -> str | None:
    """The dossier line for the story's villain while still unrevealed, else None.

    With a live campaign: its villain, until a `villain_identity` truth is
    out (or, if it authored none, until the player has met them). With no
    live spine (none, authoring, failed): every boss-tier NPC the player has
    not met — kept for a confrontation the story has not written yet. A
    resolved campaign holds no one back. Before the beat that first brings
    the villain on has fired, the line is VILLAIN_OFFSTAGE_LINE — and for
    one of the villain's people held for their entrance, CAST_OFFSTAGE_LINE
    (round 9, `cast_offstage`)."""
    if npc is None or not player_id:
        return None
    met = session.query(NPCRelationship.id).filter(
        NPCRelationship.npc_id == npc.id, NPCRelationship.player_id == player_id,
        _name_known_clause(),               # knows their NAME: a row alone is a talk (fleet6)
    ).first() is not None
    campaign = get_campaign_row(session, player_id)
    if campaign is not None and campaign.status == "active":
        if not campaign.villain_npc_id or npc.id != campaign.villain_npc_id:
            return CAST_OFFSTAGE_LINE if campaign.villain_npc_id and cast_offstage(session, player_id, npc) \
                else None
        identity = [t for t in (campaign.fixed_truths or [])
                    if isinstance(t, dict) and t.get("kind") == "villain_identity"]
        held = not any(t.get("revealed") for t in identity) if identity else not met
    elif campaign is not None and campaign.status not in ("pending", "failed"):
        return None
    else:
        held = (getattr(npc, "danger_tier", None) or "").strip().lower() == "boss" and not met
    if not held:
        return None
    # Before their first scene, not even menace in person (fleet8-tamsin T17)
    if villain_offstage(session, player_id, npc):
        return VILLAIN_OFFSTAGE_LINE
    return VILLAIN_HELD_LINE_NAMED if met else VILLAIN_HELD_LINE


# fleet9-bram T1: a critical success on a question was read as licence to
# confess the Captain's act-2 roster figure (dm.md: a confession "costs a won
# check"). The person a sealed truth is about hears it in their dossier: no
# roll buys it before its scene (`seal_line` cuts it if said)
SEALED_HOLDER_LINE = (
    "    Keeps a SEALED story truth (FIXED TRUTHS) — no roll gets it out of them before the story's own scene, "
    "not even a critical success: a won press buys a tell (a pause, a count done twice, a look away) or their "
    "cover story, never the fact itself. If said, the engine cuts it from their line."
)
# ...and when trust has opened a secret of theirs that says it, the dossier's
# "Would now confess" line offers it: the two lines must not say opposite
# things. The guard lets a confession through once its @reveal landed
# (`_confessed`), so that is the one way out before the scene
SEALED_CONFESS_LINE = (
    "    Keeps a SEALED story truth (FIXED TRUTHS) — no roll gets it out of them before the story's own scene, "
    "not even a critical success. Trust has earned only the confession their 'Would now confess' line offers: "
    "write that @reveal FIRST, then they say it. Said without it, the engine cuts it from their line."
)


def sealed_holder_line(session, player_id: str | None, npc) -> str | None:
    """SEALED_HOLDER_LINE for someone a still-sealed truth is about (its
    statement names them), unless the beat firing now — or a stalled one
    play may still deliver — reveals it; SEALED_CONFESS_LINE when trust has
    opened a secret of theirs that says it. None without a live campaign,
    or on any trouble."""
    if npc is None or not player_id:
        return None
    try:
        campaign = get_campaign_row(session, player_id)
        if campaign is None or campaign.status != "active":
            return None
        beats = _campaign_beats(session, campaign)
        firing = {str(i) for beat in beats if beat.status == "fired"
                  for i in (beat.reveals_truth_ids or [])} | _stalled_reveals(beats)
        for truth in campaign.fixed_truths or []:
            if not isinstance(truth, dict) or truth.get("revealed") or str(truth.get("id")) in firing:
                continue
            if _named_people(str(truth.get("statement") or ""), [npc]):
                secret = _holder_secret(session, player_id, npc, truth)
                if secret == "told":
                    continue        # confessed with its @reveal: theirs to say (`_confessed`)
                return SEALED_CONFESS_LINE if secret == "open" else SEALED_HOLDER_LINE
    except Exception as e:
        logger.debug(f"sealed holder line skipped for {getattr(npc, 'id', '?')}: {e}")
    return None


def _holder_secret(session, player_id: str, npc, truth: dict[str, Any]) -> str | None:
    """"told" when a secret of this person's that says `truth` was confessed
    (@reveal), "open" when trust (standing.SECRET_TRUST) opens an untold
    one — what their dossier offers to confess — else None."""
    from src.services import standing

    rel = standing.relationship_for(session, player_id, npc.id)
    if rel is None:
        return None
    told = {int(i) for i in (rel.revealed_secrets or []) if str(i).isdigit()}
    probe = {**truth, "terms": truth_terms(truth), "subjects": [npc.id]}
    # Names are no evidence, as for the guard (`_known_vocabulary`)
    names = _text_forms(" ".join(n for (n,) in session.query(NPC.name).all() if n))
    says = [i for i, secret in enumerate(npc.secrets if isinstance(npc.secrets, list) else [])
            if secret and (len(truth_leak_terms(probe, str(secret), known_forms=names)) >= TRUTH_LEAK_TERMS
                           or _subject_counts([probe], [(npc.id, str(secret))], names, set()))]
    if any(i in told for i in says):
        return "told"
    return "open" if says and standing.secret_refusal(npc, rel) is None else None


# fleet9-wenna T12: "Old Marrow worked nights before me" — the DM gave the
# successor villain's surname to a stable hand it invented. The story's own
# people (the villain, the successor, the allies, everyone a beat still to
# play needs) and the world's dead keep their names to themselves
MAX_NAMES_IN_USE = 16


def names_in_use(session, player_id: str | None) -> list[str]:
    """The names no invented person may borrow, the story's cast first
    (`story_cast_ids`, the allies), then the lost (the lost alone without
    a live campaign). [] on any trouble."""
    if not player_id:
        return []
    try:
        campaign = get_campaign_row(session, player_id)
        ids = list(story_cast_ids(session, player_id))
        if campaign is not None:
            ids += [i for i in (campaign.ally_npc_ids or []) if isinstance(i, str)]
        from src.services.dialogue import is_beast

        out: list[str] = []
        for npc_id in dict.fromkeys(ids):
            npc = session.get(NPC, npc_id)
            if npc is not None and npc.name and not is_beast(npc) and npc.name not in out:
                out.append(npc.name)
        for npc in session.query(NPC).filter(NPC.status.in_(("dead", "missing"))).order_by(NPC.name).all():
            if npc.name and not is_beast(npc) and npc.name not in out:
                out.append(npc.name)
        return out[:MAX_NAMES_IN_USE]
    except Exception as e:
        logger.debug(f"names in use unreadable: {e}")
        return []


def no_spine_context(player_id: str | None) -> str:
    """The one line the DM gets while this character has no live campaign
    (none yet, still being authored, or failed). Empty once a campaign is
    active or resolved, when campaigns are off, or when the DB is unreadable."""
    if not player_id:
        return ""
    try:
        if not campaign_enabled():
            return ""
        with get_session() as session:
            campaign = get_campaign_row(session, player_id)
            if campaign is not None and campaign.status not in ("pending", "failed"):
                return ""
    except Exception as e:  # never cost the turn its context
        logger.debug(f"no-spine check skipped: {e}")
        return ""
    return NO_SPINE_CONTEXT


# ----------------------------------------------------------------------
# Generation: validation + persistence
# ----------------------------------------------------------------------

def persist_campaign(player_id: str, payload: dict[str, Any],
                     campaign_id: str | None = None) -> dict[str, Any]:
    """Validate a generator payload and write it as fact, in one transaction.

    Everything the model got wrong is repaired or dropped, never trusted:
    unknown labels, unresolvable entity ids, deadlines with only one future.
    A campaign that loses too many beats is marked `failed` and the game plays
    exactly as it does today.
    """
    if not isinstance(payload, dict):
        return _fail_campaign(player_id, campaign_id, "generator payload was not an object")

    with get_session() as session:
        campaign = _get(session, Campaign, campaign_id) or get_campaign_row(session, player_id)
        if campaign is None:
            campaign = Campaign(id=str(uuid.uuid4()), player_id=player_id)
            session.add(campaign)
        now = _now(session)

        campaign.title = str(payload.get("title") or "Untitled")[:255]
        campaign.premise = str(payload.get("premise") or "")
        campaign.stakes_personal = str(payload.get("stakes_personal") or "")

        # 1. Exactly three acts, always
        acts = [a for a in (payload.get("acts") or []) if isinstance(a, dict)]
        normalized_acts = []
        for index in range(1, ACTS_TOTAL + 1):
            match = next((a for a in acts if int(a.get("act", 0) or 0) == index), None)
            if match is None and len(acts) >= index:
                match = acts[index - 1]
            normalized_acts.append({
                "act": index,
                "title": str((match or {}).get("title") or f"Act {index}"),
                "premise": str((match or {}).get("premise") or ""),
            })
        campaign.acts = normalized_acts
        campaign.current_act = 1

        # 2. Truths: known kinds only, ids stabilised, keywords lowercased
        truths = []
        for entry in (payload.get("fixed_truths") or []):
            if not isinstance(entry, dict) or entry.get("kind") not in TRUTH_KINDS:
                logger.warning(f"campaign truth dropped: {entry!r}")
                continue
            keywords = [str(k).strip().lower() for k in (entry.get("spoiler_keywords") or []) if str(k).strip()]
            truths.append({
                "id": str(entry.get("key") or entry.get("id") or uuid.uuid4()),
                "kind": entry["kind"],
                "statement": str(entry.get("statement") or ""),
                # <2 keywords: keep the truth, disable the front-run check
                "spoiler_keywords": keywords if len(keywords) >= 2 else [],
                "revealed": False,
                "revealed_day": None,
            })
        campaign.fixed_truths = truths[:6]
        truth_ids = {t["id"] for t in campaign.fixed_truths}

        # 3. Casting: ids must resolve, names always survive
        campaign.villain_name = str(payload.get("villain_name") or "")
        campaign.villain_npc_id = _resolve_npc(session, payload.get("villain_npc_id"), campaign.villain_name)
        campaign.successor_name = str(payload.get("successor_name") or "")
        campaign.successor_npc_id = _resolve_npc(session, payload.get("successor_npc_id"), campaign.successor_name)
        campaign.villain_faction_id = (
            payload.get("villain_faction_id")
            if _get(session, Faction, payload.get("villain_faction_id")) else None
        )
        campaign.ally_npc_ids = [
            npc_id for npc_id in (payload.get("ally_npc_ids") or [])
            if _get(session, NPC, npc_id)
        ]

        # 4-7. Beats
        session.query(StoryBeat).filter(StoryBeat.campaign_id == campaign.id).delete()
        beats, key_map = _build_beats(session, campaign, payload.get("beats") or [], truth_ids)
        if not MIN_BEATS <= len(beats) <= MAX_BEATS:
            # Keep the row and mark it failed: the campaign is inert, and the
            # tick must not keep re-spawning generation for this character.
            error = f"campaign kept {len(beats)} usable beats (need {MIN_BEATS}-{MAX_BEATS})"
            logger.error(f"campaign generation failed for {player_id}: {error}")
            campaign.status = "failed"
            campaign.generation_error = error
            session.commit()
            return {"status": "failed", "campaign_id": campaign.id, "error": error}
        for beat in beats:
            _remap_after_beat(beat, key_map)
            session.add(beat)
        session.flush()

        # 8. Live
        campaign.status = "active"
        campaign.generated_day = now[0]
        campaign.generation_error = None
        _note_villain_threat(session, campaign)
        arm_act(session, campaign, 1)

        # 9. The story arrives in a world that has already moved: authored
        # in the background, it landed during T2 in round 3, after the
        # opening had started the quest its own act-1 beat would "offer".
        # Beats play already covered are delivered on arrival.
        events: list[dict[str, Any]] = []
        cinematics: list[tuple] = []
        player = session.get(Player, player_id)
        early = _deliver_by_play(session, campaign, _campaign_beats(session, campaign), player,
                                 now, events, cinematics, reasons=("quest", "met"))
        session.commit()
        campaign_id = campaign.id
        title = campaign.title

    _dispatch_cinematics(player_id, cinematics)
    _queue_events(player_id, events)
    logger.info(f"campaign persisted: {title!r} — {len(beats)} beats, act 1 armed"
                + (f", {len(early)} already played" if early else ""))
    return {"status": "active", "campaign_id": campaign_id, "beats": len(beats)}


def _clean_lead_target(session, raw: Any) -> dict[str, Any] | None:
    """{"type", "id"} only when the id resolves; a lead with no marker still
    beats no lead, so an unresolvable target drops silently (text survives)."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or "").strip().lower()
    ident = str(raw.get("id") or raw.get("npc_id") or raw.get("location_id") or "").strip()
    if not ident:
        return None
    if kind == "npc" or (not kind and raw.get("npc_id")):
        row = _get(session, NPC, ident)
        if row is None:
            from src.web.tool_rescue import resolve_npc
            row = resolve_npc(session, ident, None)
        return {"type": "npc", "id": row.id} if row is not None else None
    if kind == "location" or (not kind and raw.get("location_id")):
        row = _get(session, Location, ident)
        if row is None:
            wanted = ident.lower()
            rows = [r for r in session.query(Location).all() if (r.name or "").lower() == wanted]
            row = rows[0] if rows else None
        return {"type": "location", "id": row.id} if row is not None else None
    return None


def _build_beats(session, campaign: Campaign, raw_beats: list,
                 truth_ids: set[str]) -> tuple[list[StoryBeat], dict[str, str]]:
    """Validate and construct the beat rows. Returns (beats, key -> beat id)."""
    beats: list[StoryBeat] = []
    key_map: dict[str, str] = {}

    for entry in raw_beats:
        if not isinstance(entry, dict):
            continue
        beat_type = entry.get("beat_type")
        trigger_type = entry.get("trigger_type")
        cinematic = entry.get("cinematic") or "none"
        if beat_type not in BEAT_TYPES:
            logger.warning(f"campaign beat dropped: unknown beat_type {beat_type!r}")
            continue
        if trigger_type not in TRIGGER_TYPES:
            logger.warning(f"campaign beat dropped: unknown trigger_type {trigger_type!r}")
            continue
        if cinematic not in CINEMATIC_LABELS:
            logger.warning(f"campaign beat dropped: unknown cinematic {cinematic!r}")
            continue

        act = max(1, min(ACTS_TOTAL, int(entry.get("act", 1) or 1)))
        beat = StoryBeat(
            id=str(uuid.uuid4()),
            campaign_id=campaign.id,
            act=act,
            sequence=int(entry.get("sequence", 0) or 0),
            beat_type=beat_type,
            title=str(entry.get("title") or "Untitled beat")[:255],
            directive=str(entry.get("directive") or ""),
            directive_missed=(str(entry["directive_missed"]) if entry.get("directive_missed") else None),
            fixed_truths=[str(t) for t in (entry.get("fixed_truths") or [])],
            reveals_truth_ids=[str(t) for t in (entry.get("reveals_truth_ids") or []) if str(t) in truth_ids],
            delivery_keywords=[str(k) for k in (entry.get("delivery_keywords") or []) if str(k).strip()],
            lead=(str(entry["lead"]).strip()[:200] or None) if entry.get("lead") else None,
            lead_target=_clean_lead_target(session, entry.get("lead_target")),
            trigger_type=trigger_type,
            trigger_data=entry.get("trigger_data") if isinstance(entry.get("trigger_data"), dict) else {},
            clock_label=(str(entry["clock_label"])[:120] if entry.get("clock_label") else None),
            cinematic=cinematic,
            status="pending",
        )
        beat.consequence_met = _clean_ops(session, entry.get("consequence_met"))
        beat.consequence_missed = _clean_ops(session, entry.get("consequence_missed"))
        beat.met_condition = _clean_condition(session, entry.get("met_condition"), truth_ids)

        # 7. A deadline you cannot miss is not a deadline
        if beat.beat_type == "deadline_event" and (not beat.met_condition or not beat.consequence_missed):
            logger.warning(f"campaign beat downgraded to setpiece (no missable future): {beat.title}")
            beat.beat_type = "setpiece"
            beat.clock_label = None

        beats.append(beat)
        key = entry.get("key")
        if key:
            key_map[str(key)] = beat.id

    return beats, key_map


def _remap_after_beat(beat: StoryBeat, key_map: dict[str, str]) -> None:
    """`after_beat` references the payload's `key`; store the persisted uuid."""
    if beat.trigger_type != "after_beat":
        return
    data = dict(beat.trigger_data or {})
    target = key_map.get(str(data.get("beat_id")))
    if target:
        data["beat_id"] = target
        beat.trigger_data = data
        return
    logger.warning(f"campaign beat {beat.title!r}: after_beat target unresolved — downgraded to time")
    beat.trigger_type = "time"
    beat.trigger_data = {"delay": "days"}


def _clean_ops(session, ops: Any) -> list[dict]:
    """Drop unknown ops, unknown labels, and unresolvable entity ids."""
    cleaned = []
    for op in (ops or []):
        if not isinstance(op, dict) or op.get("op") not in OP_NAMES:
            logger.warning(f"campaign op dropped at persist: {op!r}")
            continue
        name = op["op"]
        if name in ("move_npc", "npc_status", "npc_mood"):
            if not _get(session, NPC, op.get("npc_id")):
                logger.warning(f"campaign op dropped at persist: unknown npc in {name}")
                continue
            if name == "npc_status" and op.get("status") not in NPC_STATUSES:
                logger.warning(f"campaign op dropped at persist: bad npc status {op.get('status')!r}")
                continue
            if name == "move_npc" and op.get("to") != "player" and not _get(session, Location, op.get("to")):
                logger.warning("campaign op dropped at persist: unknown move_npc target")
                continue
        elif name in ("activate_quest", "fail_quest", "complete_quest"):
            if not _get(session, Quest, op.get("quest_id")):
                logger.warning(f"campaign op dropped at persist: unknown quest in {name}")
                continue
        elif name == "faction_power":
            if not _get(session, Faction, op.get("faction_id")) or op.get("shift") not in FACTION_POWER_SHIFT:
                logger.warning("campaign op dropped at persist: bad faction_power")
                continue
        elif name == "faction_relationship":
            if not _get(session, Faction, op.get("faction_a_id")) \
                    or not _get(session, Faction, op.get("faction_b_id")) \
                    or op.get("type") not in RELATIONSHIP_TYPES:
                logger.warning("campaign op dropped at persist: bad faction_relationship")
                continue
        elif name == "tension_floor":
            if op.get("level") not in TENSION_LEVELS:
                logger.warning(f"campaign op dropped at persist: bad tension level {op.get('level')!r}")
                continue
        elif name == "give_item":
            if not str(op.get("item_name") or "").strip():
                continue
            holder = _held_in_world(session, op.get("item_name"))
            if holder is not None:
                logger.warning(f"campaign op dropped at persist: give_item {op.get('item_name')!r} — "
                               f"{holder} already holds it; the player finds it in play")
                continue
        elif name == "world_event":
            if not str(op.get("name") or "").strip():
                continue
        cleaned.append(op)
    return cleaned


def _clean_condition(session, condition: Any, truth_ids: set[str]) -> dict | None:
    """A met_condition code cannot check is no condition at all."""
    if not isinstance(condition, dict) or condition.get("check") not in MET_CHECKS:
        return None
    check = condition["check"]
    if check == "quest_status":
        if not _get(session, Quest, condition.get("quest_id")) or condition.get("status") not in QUEST_STATUSES:
            return None
    elif check == "truth_revealed":
        if str(condition.get("truth_id")) not in truth_ids:
            return None
    elif check == "npc_status":
        if not _get(session, NPC, condition.get("npc_id")) or condition.get("status") not in NPC_STATUSES:
            return None
    elif check == "player_visited":
        if not _get(session, Location, condition.get("location_id")):
            return None
    elif check == "item_held":
        if not str(condition.get("item_name") or "").strip():
            return None
    return condition


def _resolve_npc(session, npc_id: Any, name: str) -> str | None:
    """Cast by id, then by exact name, then by fuzzy name. Never invents."""
    if _get(session, NPC, npc_id):
        return npc_id
    if name:
        npc = session.query(NPC).filter(NPC.name == name).first()
        if npc:
            return npc.id
        npc = session.query(NPC).filter(NPC.name.ilike(f"%{name}%")).first()
        if npc:
            return npc.id
    if npc_id or name:
        logger.warning(f"campaign casting unresolved: {name!r} ({npc_id!r}) — name kept, id cleared")
    return None


def _note_villain_threat(session, campaign: Campaign) -> None:
    """The villain joins `DMState.active_threats` once, at creation."""
    if not campaign.villain_name:
        return
    dm_state = session.query(DMState).first()
    if not dm_state:
        return
    threats = list(dm_state.active_threats or [])
    if campaign.villain_name not in threats:
        threats.append(campaign.villain_name)
        dm_state.active_threats = threats


def _fail_campaign(player_id: str, campaign_id: str | None, error: str) -> dict[str, Any]:
    """Mark the claimed row failed. The game plays exactly as it does today."""
    logger.error(f"campaign generation failed for {player_id}: {error}")
    with get_session() as session:
        campaign = _get(session, Campaign, campaign_id) or get_campaign_row(session, player_id)
        if campaign is not None:
            campaign.status = "failed"
            campaign.generation_error = error[:2000]
            session.commit()
            return {"status": "failed", "campaign_id": campaign.id, "error": error}
    return {"status": "failed", "campaign_id": None, "error": error}


# ----------------------------------------------------------------------
# Generation: orchestration
# ----------------------------------------------------------------------

# A generation claims its row as "pending" before the LLM call; if the
# process dies mid-call (a restart, a crash) the row stays pending forever
# and every later caller no-ops. Older than this, a pending row is a corpse.
PENDING_STALE_AFTER = timedelta(minutes=5)


def _stale_pending(campaign) -> bool:
    if campaign.status != "pending":
        return False
    created = getattr(campaign, "created_at", None)
    if created is None:
        return True
    return (datetime.utcnow() - created) > PENDING_STALE_AFTER


# A failed generation is retried, not honoured forever: round 3's Bram lost
# his whole story to one transient config-loading race ("Agent
# 'campaign_forge' not found in config") and played 21 turns spineless — the
# authored boss thrown into an unchosen fight, nothing pushing after quest 1.
# At most this many attempts per character (the first included), each retry
# after its backoff, counted from when the failed attempt was claimed.
MAX_GENERATION_ATTEMPTS = 3
RETRY_BACKOFF = (timedelta(minutes=1), timedelta(minutes=5))


def _attempts(campaign) -> int:
    return max(1, int(getattr(campaign, "generation_attempts", None) or 1))


def _retry_due(campaign) -> bool:
    """Should this row be authored again now? A stale pending row (the
    process died mid-call) or a failed one whose backoff ran out — while the
    character has attempts left."""
    if _attempts(campaign) >= MAX_GENERATION_ATTEMPTS:
        return False
    if campaign.status == "pending":
        return _stale_pending(campaign)
    if campaign.status != "failed":
        return False
    created = getattr(campaign, "created_at", None)
    if created is None:
        return True
    wait = RETRY_BACKOFF[min(_attempts(campaign), len(RETRY_BACKOFF)) - 1]
    return datetime.utcnow() - created >= wait


def ensure_campaign(player_id: str, predecessor_id: str | None = None,
                    force: bool = False, generate=None,
                    background: bool = True) -> dict[str, Any]:
    """Claim a campaign row for this character, then author it off-thread.

    The row is written with `status="pending"` FIRST so concurrent callers
    no-op (the same trick `maybe_advance_world` uses to claim a day). The
    player is naming their character while the 10-30s generation runs — a turn
    is never blocked, and a failure degrades to today's game. A failed or
    stale row is claimed again once `_retry_due` says so (the context build
    asks every turn); `force` starts over with a fresh attempt count.
    """
    if not campaign_enabled():
        return {"status": "disabled", "campaign_id": None}
    if not player_id:
        return {"status": "disabled", "campaign_id": None}

    with get_session() as session:
        existing = get_campaign_row(session, player_id)
        if existing is not None and not force and not _retry_due(existing):
            if _stale_pending(existing):
                # Out of attempts and never finished: say so, never "pending"
                existing.status = "failed"
                existing.generation_error = existing.generation_error or "generation never finished"
                session.commit()
            status = "pending" if existing.status == "pending" else (
                "ready" if existing.status == "active" else "exists"
            )
            return {"status": status, "campaign_id": existing.id}
        attempts = 1
        if existing is not None and not force:
            attempts = _attempts(existing) + 1
            if existing.status == "pending":
                logger.warning(
                    f"campaign: pending row for {player_id} never finished "
                    f"(created {existing.created_at}) — regenerating "
                    f"(attempt {attempts} of {MAX_GENERATION_ATTEMPTS})"
                )
            else:
                logger.error(
                    f"campaign: generation for {player_id} failed "
                    f"({existing.generation_error!r}) — retrying "
                    f"(attempt {attempts} of {MAX_GENERATION_ATTEMPTS})"
                )
        if existing is not None:
            session.query(StoryBeat).filter(StoryBeat.campaign_id == existing.id).delete()
            session.delete(existing)
            session.flush()
        campaign = Campaign(id=str(uuid.uuid4()), player_id=player_id, status="pending",
                            generation_attempts=attempts)
        session.add(campaign)
        session.commit()
        campaign_id = campaign.id

    if background:
        threading.Thread(
            target=_run_generation,
            args=(player_id, campaign_id, predecessor_id, generate),
            daemon=True,
        ).start()
        return {"status": "pending", "campaign_id": campaign_id}

    return _run_generation(player_id, campaign_id, predecessor_id, generate)


def _run_generation(player_id: str, campaign_id: str, predecessor_id: str | None,
                    generate=None) -> dict[str, Any]:
    """Brief -> one LLM call -> validated persistence. Never raises."""
    try:
        from src.agents.campaign_forge import build_campaign_brief, generate_campaign_payload

        brief = build_campaign_brief(player_id, predecessor_id=predecessor_id)
        payload = (generate or generate_campaign_payload)(brief)
        return persist_campaign(player_id, payload, campaign_id=campaign_id)
    except Exception as e:
        return _fail_campaign(player_id, campaign_id, str(e))


# Characters with a generation running in this process. The tick asks every
# turn; a retry that is due must start once, not once per turn while the
# 10-30s call runs (two claims would race to delete the same row).
_GENERATING: set[str] = set()
_GENERATING_LOCK = threading.Lock()


def _spawn_generation(player_id: str) -> None:
    """Author (or re-author) this character's campaign in the background —
    a campaign-less first turn, a stale pending row, a failed row whose
    retry is due. Never blocks the caller."""
    with _GENERATING_LOCK:
        if player_id in _GENERATING:
            return
        _GENERATING.add(player_id)
    logger.info(f"campaign: generating for {player_id} in the background")

    def run() -> None:
        try:
            ensure_campaign(player_id, background=False)
        except Exception as e:  # pragma: no cover - ensure_campaign never raises past a claim
            logger.error(f"campaign: background generation crashed for {player_id}: {e}")
        finally:
            with _GENERATING_LOCK:
                _GENERATING.discard(player_id)

    threading.Thread(target=run, daemon=True).start()

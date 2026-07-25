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
import uuid
from typing import Any

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
    return armed


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
        wanted = str(condition.get("item_name") or "").strip().lower()
        if not wanted:
            return False
        for entry in (getattr(player, "inventory", None) or []):
            name = entry.get("name") if isinstance(entry, dict) else entry
            if wanted in str(name or "").lower():
                return True
        return False

    return False


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


# ----------------------------------------------------------------------
# The tick
# ----------------------------------------------------------------------

def run_campaign_tick(player_id: str) -> dict[str, Any]:
    """Advance the campaign one turn. Pure code — no LLM, no network.

    Called from `DMOrchestrator._build_context` right after `run_world_tick()`
    so CLI and web both get it. At most ONE beat fires per turn; an undelivered
    beat blocks new ones until it lands or the engine gives up on it.

    Returns the briefing payload (also cached for the web layer via
    `get_last_tick` / `pop_pending_events`), or `{}` when this character has no
    live campaign — a world without one behaves exactly as it does today.
    """
    if not player_id or not campaign_enabled():
        return {}

    events: list[dict[str, Any]] = []
    cinematics: list[tuple] = []
    tension: list[str] = []

    with get_session() as session:
        campaign = get_campaign_row(session, player_id)
        if campaign is None:
            if _auto_generate_enabled():
                _spawn_generation(player_id)
            return {}
        if campaign.status != "active":
            return {}
        player = session.get(Player, player_id)
        if player is None:
            return {}

        now = _now(session)
        beats = _campaign_beats(session, campaign)

        # 2. The villain is a load-bearing wall — check it is still standing
        note = _check_villain(session, campaign, beats, now, events)

        # 3. Arm anything waiting on a beat that has now been delivered
        _arm_after_beats(campaign, beats, now)

        # 4. A beat the DM never narrated blocks the whole story
        fired = _resurface_undelivered(session, campaign, beats, now, events, cinematics)

        # 5-8. Otherwise: pick at most one eligible beat and fire it
        if fired is None:
            fired = _fire_next(session, campaign, beats, player, now, events, cinematics, tension)

        if fired and note:
            fired["directive"] = f"{note}\n{fired['directive']}".strip()

        result = _build_briefing(session, campaign, beats, now, fired, events, note)
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


def _arm_after_beats(campaign: Campaign, beats: list[StoryBeat], now: tuple[int, int]) -> None:
    """Arm `after_beat` beats whose prerequisite has now been delivered.

    An act only ends when all of its beats are delivered or skipped, so a
    prerequisite that can never land (skipped, missing, or authored into a
    LATER act) would stall the campaign permanently. Those arm immediately —
    a beat out of order beats a story that stops.
    """
    by_id = {b.id: b for b in beats}
    for beat in beats:
        if beat.status != "pending" or beat.trigger_type != "after_beat":
            continue
        if beat.act != campaign.current_act:
            continue
        data = beat.trigger_data if isinstance(beat.trigger_data, dict) else {}
        prerequisite = by_id.get(data.get("beat_id"))
        if prerequisite is None or prerequisite.act > beat.act \
                or prerequisite.status in ("delivered", "skipped"):
            _arm_beat(beat, now)


def _resurface_undelivered(session, campaign: Campaign, beats: list[StoryBeat],
                           now: tuple[int, int], events: list,
                           cinematics: list) -> dict[str, Any] | None:
    """Re-surface a fired-but-unnarrated beat, or give up on it after 3 tries.

    Mirrors the Event delivery loop: the world already changed, so the beat
    cannot silently vanish — but it also cannot block the story forever.
    """
    for beat in beats:
        if beat.status != "fired":
            continue
        attempts = beat.delivery_attempts or 0
        if attempts >= BEAT_DELIVERY_ATTEMPTS:
            logger.warning(f"campaign beat gave up: {beat.title}")
            _deliver_beat(session, campaign, beat, now, events, cinematics)
            continue
        beat.delivery_attempts = attempts + 1
        return _fired_payload(beat, attempt=beat.delivery_attempts)
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
        candidates = [b for b in candidates if b.beat_type == "deadline_event"]
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

    applied, exempt = execute_ops(session, ops, campaign, player,
                                  events=events, deferred=tension)

    beat.status = "fired"
    beat.fired_day, beat.fired_hour = now
    beat.delivery_attempts = 1
    beat.applied_effects = applied
    campaign.last_beat_day, campaign.last_beat_hour = now

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

    logger.info(f"campaign beat fired: {beat.title} ({beat.beat_type}/{beat.variant})")
    return _fired_payload(beat, attempt=1, applied=applied)


def _is_eligible(session, beat: StoryBeat, player, now: tuple[int, int]) -> bool:
    """Has this beat's trigger been satisfied — or has its backstop expired?"""
    fallback = _stamp(beat, "fallback")
    if fallback is not None and now >= fallback:
        return True

    if beat.beat_type == "deadline_event" or beat.trigger_type == "time" or beat.trigger_type == "after_beat":
        scheduled = _stamp(beat, "scheduled")
        return scheduled is not None and now >= scheduled

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
    }


def _build_briefing(session, campaign: Campaign, beats: list[StoryBeat],
                    now: tuple[int, int], fired: dict | None,
                    events: list, note: str | None) -> dict[str, Any]:
    """Assemble the tick payload: summary, truths, clocks, act progress."""
    act = campaign.current_act or 1
    entry = _act_entry(campaign, act)
    act_beats = [b for b in beats if b.act == act]
    delivered = [b for b in beats if b.status == "delivered"]

    truths = [
        {
            "id": t.get("id"),
            "kind": t.get("kind"),
            "statement": t.get("statement"),
            "revealed": bool(t.get("revealed")),
        }
        for t in (campaign.fixed_truths or []) if isinstance(t, dict)
    ]
    sealed = [
        {"id": t.get("id"), "spoiler_keywords": list(t.get("spoiler_keywords") or [])}
        for t in (campaign.fixed_truths or [])
        if isinstance(t, dict) and not t.get("revealed")
    ]

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
        "audit": {
            "beat": (
                {
                    "beat_id": fired["beat_id"],
                    "title": fired["title"],
                    "delivery_keywords": fired["delivery_keywords"],
                }
                if fired else None
            ),
            "sealed_truths": sealed,
        },
    }


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

def mark_beat_delivery(player_id: str, turn_text: str) -> dict[str, Any]:
    """Did the DM actually deliver the fired beat? Keyword check, no LLM.

    Called post-turn (before the audit, so a delivered beat is never flagged).
    Returns `{"delivered", "beat_id", "act_advanced", "events"}` — the events
    are queued for the next SSE flush as well.
    """
    if not player_id or not campaign_enabled():
        return {"delivered": False, "beat_id": None, "act_advanced": None, "events": []}

    events: list[dict[str, Any]] = []
    cinematics: list[tuple] = []
    result = {"delivered": False, "beat_id": None, "act_advanced": None, "events": events}
    lowered = (turn_text or "").lower()

    with get_session() as session:
        campaign = get_campaign_row(session, player_id)
        if campaign is None or campaign.status != "active":
            return result
        now = _now(session)
        act_before = campaign.current_act

        for beat in _campaign_beats(session, campaign):
            if beat.status != "fired":
                continue
            keywords = [str(k).lower() for k in (beat.delivery_keywords or []) if str(k).strip()]
            if keywords and not any(k in lowered for k in keywords):
                continue  # left `fired`; the next tick re-surfaces it
            _deliver_beat(session, campaign, beat, now, events, cinematics)
            result["delivered"] = True
            result["beat_id"] = beat.id
            break

        if campaign.current_act != act_before:
            result["act_advanced"] = campaign.current_act
        session.commit()

    _dispatch_cinematics(player_id, cinematics)
    _queue_events(player_id, events)
    return result


def _deliver_beat(session, campaign: Campaign, beat: StoryBeat, now: tuple[int, int],
                  events: list, cinematics: list) -> None:
    """Mark a beat delivered, un-seal its truths, then check the act boundary."""
    beat.status = "delivered"
    beat.delivered_day = now[0]

    revealed_ids = {str(i) for i in (beat.reveals_truth_ids or [])}
    if revealed_ids:
        truths = [dict(t) if isinstance(t, dict) else t for t in (campaign.fixed_truths or [])]
        for truth in truths:
            if isinstance(truth, dict) and str(truth.get("id")) in revealed_ids:
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


def _check_act_transition(session, campaign: Campaign, last_beat: StoryBeat,
                          now: tuple[int, int], events: list, cinematics: list) -> None:
    """An act ends when every one of its beats is delivered or skipped."""
    act = campaign.current_act or 1
    act_beats = [b for b in _campaign_beats(session, campaign) if b.act == act]
    if not act_beats or any(b.status not in ("delivered", "skipped") for b in act_beats):
        return

    if act < ACTS_TOTAL:
        campaign.current_act = act + 1
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
            else:
                lines.append(
                    f"  [SEALED] {kind}: {statement} The player does NOT know this. "
                    "Write the scene as if you did not know it either. No knowing "
                    "looks, no NPC almost-saying it."
                )

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

    fired = tick.get("fired_beat")
    if fired:
        lines.append("")
        lines.append(
            f"THIS TURN — MUST DELIVER  [{fired.get('beat_type')} · \"{fired.get('title')}\"]"
        )
        lines.append(f"  {fired.get('directive') or ''}".rstrip())
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
        if (fired.get("attempt") or 1) > 1:
            lines.append(
                f"  You were given this beat {fired['attempt']} turns ago and have not "
                "delivered it. The world has ALREADY changed. Deliver it NOW, in this "
                "response, before anything else."
            )
        else:
            lines.append(
                "  Deliver this IN THIS RESPONSE. If the player is doing something else, "
                "let it interrupt them — that is what a story is."
            )

    return "\n".join(lines)


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
        session.commit()

        logger.info(f"campaign persisted: {campaign.title!r} — {len(beats)} beats, act 1 armed")
        return {"status": "active", "campaign_id": campaign.id, "beats": len(beats)}


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

def ensure_campaign(player_id: str, predecessor_id: str | None = None,
                    force: bool = False, generate=None,
                    background: bool = True) -> dict[str, Any]:
    """Claim a campaign row for this character, then author it off-thread.

    The row is written with `status="pending"` FIRST so concurrent callers
    no-op (the same trick `maybe_advance_world` uses to claim a day). The
    player is naming their character while the 10-30s generation runs — a turn
    is never blocked, and a failure degrades to today's game.
    """
    if not campaign_enabled():
        return {"status": "disabled", "campaign_id": None}
    if not player_id:
        return {"status": "disabled", "campaign_id": None}

    with get_session() as session:
        existing = get_campaign_row(session, player_id)
        if existing is not None and not force:
            status = "pending" if existing.status == "pending" else (
                "ready" if existing.status == "active" else "exists"
            )
            return {"status": status, "campaign_id": existing.id}
        if existing is not None:
            session.query(StoryBeat).filter(StoryBeat.campaign_id == existing.id).delete()
            session.delete(existing)
            session.flush()
        campaign = Campaign(id=str(uuid.uuid4()), player_id=player_id, status="pending")
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


def _spawn_generation(player_id: str) -> None:
    """First turn of a campaign-less character: author one in the background."""
    logger.info(f"campaign: no campaign for {player_id} — generating in the background")
    threading.Thread(target=ensure_campaign, args=(player_id,), daemon=True).start()

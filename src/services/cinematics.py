"""Cinematic vocabulary + cooldown enforcement. Code decides; the LLM labels.

A cinematic takes over the screen. Rarity is the whole product: a title card
that fires every third turn is worse than no title cards at all. The limits
below are enforced against a DB ledger (the `cinematics` table) so a page
reload can't reset them and a chatty DM turn can't stack two of them.

Every caller goes through `request_cinematic` — the `play_cinematic` tool,
the automatic boss entrance on `start_combat`, and campaign beats (which
queue theirs with `defer=True` because they have no tool result to ride on).
No LLM, no I/O beyond SQLite.
"""

import re
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from src.models import Cinematic, DMState, WorldClock, get_session


VOCABULARY = ("title_card", "boss_intro", "kill_cam", "vision", "weather")
DM_CALLABLE = ("title_card", "boss_intro", "vision", "weather")   # kill_cam is combat-internal
WEATHER_LABELS = ("rain", "snow", "fog", "embers", "ash", "clear")
SOURCES = ("dm", "auto", "campaign")

# Informational for the client + used for audio hold windows. The JS table in
# game.js is the authority on actual tween timing; test_cinematics keeps the
# vocabulary and this table in agreement.
DURATIONS_MS = {"title_card": 4200, "boss_intro": 3600, "kill_cam": 1500,
                "vision": 0, "weather": 0}   # 0 = open-ended / stateful

# kind -> (min game-MINUTES since the same kind,
#          max plays per game DAY,
#          min REAL seconds since ANY cinematic,
#          max plays per rolling 60 real minutes)
LIMITS = {
    "title_card": (720, 1, 45, 4),
    "boss_intro": (0,   2, 15, 3),
    "vision":     (120, 2, 45, 5),
    "weather":    (45,  4, 10, 12),
}
# kill_cam never reaches this service in normal play (it is client-side and
# can fire at most once per boss per fight) — engine-internal kinds are not
# throttled, they are simply logged.
_UNTHROTTLED = (0, 9999, 0, 9999)

SESSION_WINDOW_MIN = 60      # the rolling real-time window = "a session"

REJECT_REASONS = (
    "unknown_kind", "not_dm_callable", "bad_weather_label", "missing_title",
    "cooldown_game_time", "daily_cap", "too_soon", "session_cap",
    "duplicate", "boss_already_introduced", "world_not_initialized",
)

# Short human phrases, folded into the say-nothing instruction the DM reads.
_REJECT_DETAIL = {
    "unknown_kind": "that is not a cinematic kind",
    "not_dm_callable": "that kind is engine-only",
    "bad_weather_label": "that is not a weather label",
    "missing_title": "a title card needs a title",
    "cooldown_game_time": "too soon in story time since the last one of these",
    "daily_cap": "this kind has already been used today",
    "too_soon": "too soon since the last one",
    "session_cap": "too many of these already this session",
    "duplicate": "that exact moment has already played",
    "boss_already_introduced": "that character has already had their entrance",
    "world_not_initialized": "the world has no clock yet",
}

# A campaign beat carries a LABEL from campaign.CINEMATIC_LABELS; code — not
# the generator — decides which of those labels deserve the screen. Revelation,
# betrayal and arrival beats are carried by the campaign toast and the DM's
# prose in v1: `vision` stays a dream/memory treatment, not a plot-beat skin.
CAMPAIGN_CINEMATIC_MAP = {
    "none": None,
    "act_open": "title_card",
    "revelation": None,
    "betrayal": None,
    "arrival": None,
    "countdown_expired": "title_card",
    "finale": "title_card",
}

# A queued cinematic is a promise to play — it consumes budget exactly like a
# played one, so a campaign tick can't sneak a second card past the caps.
_ACTIVE_STATUSES = ("played", "pending")

# Payload keys, in the order the SSE event carries them
PAYLOAD_FIELDS = ("kind", "title", "subtitle", "npc_id", "mood", "duration_ms")


def _now() -> datetime:
    """Wall clock for the real-time limits (monkeypatched in tests)."""
    return datetime.utcnow()


def _slug(text: str) -> str:
    """Identity fragment for a title card — same words, same card."""
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:120]


def _game_minutes(day: int | None, hour: int | None, minute: int | None) -> int | None:
    """Absolute story minutes, so cooldowns survive a day rollover."""
    if day is None:
        return None
    return day * 24 * 60 + (hour or 0) * 60 + (minute or 0)


def _limits(kind: str) -> tuple[int, int, int, int]:
    return LIMITS.get(kind, _UNTHROTTLED)


def _dedupe_key(kind: str, title: str | None, npc_id: str | None, mood: str | None) -> str | None:
    """The identity of a moment: one entrance per boss, one card per chapter."""
    if kind == "boss_intro":
        return npc_id
    if kind == "title_card":
        return _slug(title) or None
    if kind == "weather":
        return mood
    return None


def rejection_instruction(reason: str) -> str:
    """What the DM is told when a cinematic is refused — say nothing, write prose.

    The failure mode this guards against is the DM *typing the card* after a
    rejection ("a title card reads: THE ROAD NORTH"), which the reality audit
    also catches. Better to pre-empt it here.
    """
    return (
        f"Cinematic not played ({_REJECT_DETAIL.get(reason, reason)}). This is normal "
        "and expected. Do NOT mention it, do NOT describe a title card or its text — "
        "just narrate the moment in prose."
    )


def _validate(kind: str, dm_callable_only: bool, mood: str | None, title: str | None) -> str | None:
    """Menu checks — no DB, no clock. First failure wins."""
    if kind not in VOCABULARY:
        return "unknown_kind"
    if dm_callable_only and kind not in DM_CALLABLE:
        return "not_dm_callable"
    if kind == "weather" and (mood or "") not in WEATHER_LABELS:
        return "bad_weather_label"
    if kind == "title_card" and not (title or "").strip():
        return "missing_title"
    return None


def _identity_reason(session, kind: str, key: str | None, mood: str | None,
                     dm_state: DMState | None) -> str | None:
    """Has this exact moment already happened?"""
    if kind == "weather":
        # Weather is world state: setting the weather that is already there is
        # a no-op, and the DM does it constantly.
        current = (dm_state.weather if dm_state else None) or "clear"
        return "duplicate" if mood == current else None
    if not key:
        return None
    seen = (
        session.query(Cinematic)
        .filter(
            Cinematic.kind == kind,
            Cinematic.dedupe_key == key,
            Cinematic.status.in_(_ACTIVE_STATUSES),
        )
        .first()
    )
    if seen is None:
        return None
    return "boss_already_introduced" if kind == "boss_intro" else "duplicate"


def _limit_reason(session, kind: str, now_dt: datetime,
                  now_minutes: int | None, day: int | None) -> str | None:
    """The four stacked budgets, in the order the spec fixes them."""
    cooldown_min, daily_cap, floor_seconds, session_cap = _limits(kind)

    # Real-time floor against ANY kind — stops two cinematics inside one turn
    if floor_seconds > 0:
        last_any = (
            session.query(Cinematic)
            .filter(Cinematic.status == "played")
            .order_by(Cinematic.created_at.desc())
            .first()
        )
        if last_any is not None and last_any.created_at is not None:
            if (now_dt - last_any.created_at).total_seconds() < floor_seconds:
                return "too_soon"

    # Same-kind cooldown, measured in story time
    if cooldown_min > 0 and now_minutes is not None:
        last_kind = (
            session.query(Cinematic)
            .filter(
                Cinematic.kind == kind,
                Cinematic.status.in_(_ACTIVE_STATUSES),
                Cinematic.game_day.isnot(None),
            )
            .order_by(Cinematic.created_at.desc())
            .first()
        )
        if last_kind is not None:
            then = _game_minutes(last_kind.game_day, last_kind.game_hour, last_kind.game_minute)
            if then is not None and now_minutes - then < cooldown_min:
                return "cooldown_game_time"

    # Per game day
    if day is not None:
        today = (
            session.query(Cinematic)
            .filter(
                Cinematic.kind == kind,
                Cinematic.status.in_(_ACTIVE_STATUSES),
                Cinematic.game_day == day,
            )
            .count()
        )
        if today >= daily_cap:
            return "daily_cap"

    # Rolling real-time window ("a session")
    window_start = now_dt - timedelta(minutes=SESSION_WINDOW_MIN)
    in_window = (
        session.query(Cinematic)
        .filter(
            Cinematic.kind == kind,
            Cinematic.status.in_(_ACTIVE_STATUSES),
            Cinematic.created_at >= window_start,
        )
        .count()
    )
    if in_window >= session_cap:
        return "session_cap"
    return None


def _payload(kind: str, title: str | None, subtitle: str | None,
             npc_id: str | None, mood: str | None) -> dict[str, Any]:
    return {
        "kind": kind,
        "title": title,
        "subtitle": subtitle,
        "npc_id": npc_id,
        "mood": mood,
        "duration_ms": DURATIONS_MS.get(kind, 0),
    }


def request_cinematic(
    kind: str,
    *,
    player_id: str | None = None,
    title: str | None = None,
    subtitle: str | None = None,
    npc_id: str | None = None,
    mood: str | None = None,
    source: str = "dm",          # dm | auto | campaign
    defer: bool = False,         # True -> queue it (status='pending') instead of returning it
    dm_callable_only: bool = True,
) -> dict[str, Any]:
    """Validate + rate-limit a cinematic request.

    Returns EITHER
      {"ok": True, "payload": {kind, title, subtitle, npc_id, mood, duration_ms}}
      (plus "deferred": True when it was queued instead of played now)
    OR
      {"ok": False, "reason": "<one of REJECT_REASONS>", "detail": "...",
       "instruction": "<say nothing, narrate it in prose>"}

    A rejection never carries an "event" key, so nothing can reach the client.

    Every call — accepted or rejected — writes one row to `cinematics`
    (status 'played' | 'pending' | 'rejected'). The ledger IS the cooldown state.
    """
    kind_label = str(kind or "").strip().lower()
    mood_label = str(mood).strip().lower() if mood else None
    title_text = str(title).strip() or None if title else None
    subtitle_text = str(subtitle).strip() or None if subtitle else None
    npc_ident = str(npc_id).strip() or None if npc_id else None
    source_label = source if source in SOURCES else "dm"

    with get_session() as session:
        clock = session.query(WorldClock).first()
        dm_state = session.query(DMState).first()
        now_dt = _now()
        day = clock.day if clock else None
        hour = clock.hour if clock else None
        minute = (clock.minute or 0) if clock else None

        reason = _validate(kind_label, dm_callable_only, mood_label, title_text)
        if reason is None and (clock is None or (kind_label == "weather" and dm_state is None)):
            # Weather is durable world state; without a DMState row there is
            # nowhere to keep it, and without a clock there are no cooldowns.
            reason = "world_not_initialized"

        key = _dedupe_key(kind_label, title_text, npc_ident, mood_label)
        if reason is None:
            reason = _identity_reason(session, kind_label, key, mood_label, dm_state)
        if reason is None:
            reason = _limit_reason(session, kind_label, now_dt,
                                   _game_minutes(day, hour, minute), day)

        status = "rejected" if reason else ("pending" if defer else "played")
        session.add(Cinematic(
            player_id=player_id,
            kind=(kind_label or "unknown")[:30],
            dedupe_key=key,
            title=title_text,
            subtitle=subtitle_text,
            npc_id=npc_ident,
            mood=mood_label,
            source=source_label,
            status=status,
            reject_reason=reason,
            game_day=day,
            game_hour=hour,
            game_minute=minute,
            created_at=now_dt,
        ))

        # Weather is the transition on screen but the durable truth in the DB —
        # build_player_state re-broadcasts it on reload and on scene change.
        if reason is None and kind_label == "weather" and dm_state is not None:
            dm_state.weather = None if mood_label == "clear" else mood_label

        session.commit()

    if reason:
        logger.info(f"Cinematic rejected: {kind_label or kind!r} ({reason})")
        return {
            "ok": False,
            "reason": reason,
            "detail": _REJECT_DETAIL.get(reason, reason),
            "instruction": rejection_instruction(reason),
        }

    logger.info(
        f"Cinematic {'queued' if defer else 'played'}: {kind_label} "
        f"({source_label}{', ' + title_text if title_text else ''})"
    )
    result: dict[str, Any] = {
        "ok": True,
        "payload": _payload(kind_label, title_text, subtitle_text, npc_ident, mood_label),
    }
    if defer:
        result["deferred"] = True
    return result


def drain_pending(player_id: str) -> list[dict[str, Any]]:
    """Pop queued cinematics (status='pending') for this player, oldest first,
    flip them to 'played', and return their payloads. Called once per turn by
    /api/play right after the world tick (which is when campaign beats fire)."""
    if not player_id:
        return []
    with get_session() as session:
        rows = (
            session.query(Cinematic)
            .filter(Cinematic.status == "pending", Cinematic.player_id == player_id)
            .order_by(Cinematic.created_at.asc())
            .all()
        )
        payloads = [_payload(r.kind, r.title, r.subtitle, r.npc_id, r.mood) for r in rows]
        for row in rows:
            row.status = "played"
        if rows:
            session.commit()
    return payloads


def boss_intro_payload(npc, player_id: str | None) -> dict[str, Any] | None:
    """Auto path: build a boss_intro payload for this NPC if the cooldowns
    allow and they have never been introduced. Returns None otherwise.

    `danger_tier == "boss"` is the only boss flag; `boss_title` is the epithet
    printed under their name, falling back to their profession.
    """
    if npc is None:
        return None
    # Imported lazily: combat.py imports THIS module to attach the intro to
    # start_combat, so a module-level import would close the cycle.
    from src.services.combat import is_boss

    try:
        if not is_boss(npc):
            return None
        name = npc.name
        npc_id = npc.id
        subtitle = npc.boss_title or npc.profession or None
    except Exception as e:   # detached/expired row — never break a fight over a flourish
        logger.warning(f"Boss intro skipped (unreadable NPC): {e}")
        return None

    result = request_cinematic(
        "boss_intro",
        player_id=player_id,
        title=name,
        subtitle=subtitle,
        npc_id=npc_id,
        source="auto",
    )
    return result["payload"] if result.get("ok") else None


def recent(limit: int = 20) -> list[dict[str, Any]]:
    """Ledger rows for the debug endpoint / self-playtest scoreboard."""
    with get_session() as session:
        rows = (
            session.query(Cinematic)
            .order_by(Cinematic.created_at.desc())
            .limit(max(1, int(limit or 1)))
            .all()
        )
        return [
            {
                "kind": r.kind,
                "title": r.title,
                "subtitle": r.subtitle,
                "npc_id": r.npc_id,
                "mood": r.mood,
                "source": r.source,
                "status": r.status,
                "reject_reason": r.reject_reason,
                "game_day": r.game_day,
                "game_hour": r.game_hour,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]

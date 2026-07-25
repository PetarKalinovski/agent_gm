"""Ambient scene chatter — the one-liners a place mutters to itself.

One fast-model call per location per refresh window, dispatched in the
background and cached in `Location.ambient_barks`. Nothing here ever sits on
a player-visible fast path: the endpoint serves whatever is already cached (a
stale pool beats silence) and only spawns a regeneration when the pool has
gone stale — the NPC set at the location changed, the place changed state, or
enough in-game hours passed.

Barks are decoration. Every failure path logs and returns an empty list; a
failed generation is then held off for FAILURE_COOLDOWN_SECONDS so a broken
model or a missing API key cannot be re-billed on every scene load.
"""

import asyncio
import hashlib
import json
import time
from typing import Any, Callable

from loguru import logger

from src.config import get_agent_config, load_settings
from src.models import NPC, Location, NPCTier, WorldBible, WorldClock, get_session


BARK_PROMPT = """You write one-line ambient chatter for a living RPG scene.

You are given a place and the people standing in it. Write EXACTLY {n} lines
these people could say out loud, unprompted, to nobody in particular —
overheard fragments, not conversation. Each line is at most 14 words.

Rules:
- Never address the player. No greetings, no questions aimed at them.
- No plot, no quest hooks, no prophecy, no numbers, no stage directions.
- No names of people who are not in the list below.
- Ground every line in THIS place: its trade, its weather, its smells, its
  grievances, its gossip, its complaints about the neighbours.
- Match the world's tone exactly.
- Vary the speakers. If one of the listed NPCs would say it, use their exact
  name. Otherwise use "ambient".

Return ONLY a JSON array. No prose, no code fences, no commentary:
[{"speaker": "<exact NPC name or 'ambient'>", "text": "<the line>"}]
"""

# Defaults used when the game config block hasn't been authored yet
DEFAULT_BARKS_ENABLED = True
DEFAULT_POOL_SIZE = 10
DEFAULT_REFRESH_HOURS = 12.0

TEXT_MAX = 120  # a bark longer than this is a paragraph, not a mutter
POOL_SLACK = 2  # keep a couple of spares past the configured pool size

# Don't re-run a generation that just failed (the AssetManager cooldown only
# engages on an exception, and this module deliberately never raises)
FAILURE_COOLDOWN_SECONDS = 300

# The bark agent proper, then progressively less specific fast-model configs —
# `get_agent_config` raises KeyError on unknown names, and barks must still
# work in a checkout whose agents.yaml predates the `bark` entry.
AGENT_CANDIDATES = ("bark", "recap", "npc_agent", "creator_agent")

# location_id -> monotonic timestamp of the last failed generation
_failures: dict[str, float] = {}


# ----------------------------------------------------------------------
# Config (read defensively — the game block may predate these keys)
# ----------------------------------------------------------------------

def _game_setting(name: str, default: Any) -> Any:
    """Read one `game.*` setting, falling back when it isn't configured."""
    try:
        game = load_settings().game
    except Exception:  # pragma: no cover - settings.yaml unreadable
        return default
    value = getattr(game, name, None)
    return default if value is None else value


def barks_enabled() -> bool:
    return bool(_game_setting("barks_enabled", DEFAULT_BARKS_ENABLED))


def bark_pool_size() -> int:
    try:
        size = int(_game_setting("bark_pool_size", DEFAULT_POOL_SIZE))
    except (TypeError, ValueError):
        return DEFAULT_POOL_SIZE
    return max(1, size)


def bark_refresh_hours() -> float:
    try:
        hours = float(_game_setting("bark_refresh_hours", DEFAULT_REFRESH_HOURS))
    except (TypeError, ValueError):
        return DEFAULT_REFRESH_HOURS
    return max(0.0, hours)


def _agent_name() -> str:
    """The configured agent to run barks with (first candidate that exists)."""
    for name in AGENT_CANDIDATES:
        try:
            get_agent_config(name)
            return name
        except KeyError:
            continue
    return AGENT_CANDIDATES[0]


# ----------------------------------------------------------------------
# Failure cooldown
# ----------------------------------------------------------------------

def _recently_failed(location_id: str) -> bool:
    ts = _failures.get(location_id)
    return ts is not None and (time.monotonic() - ts) < FAILURE_COOLDOWN_SECONDS


def _mark_failure(location_id: str) -> None:
    _failures[location_id] = time.monotonic()


def reset_failure_cooldowns() -> None:
    """Clear the failure registry (tests, and manual retries)."""
    _failures.clear()


# ----------------------------------------------------------------------
# Staleness
# ----------------------------------------------------------------------

def bark_signature(location_id: str) -> str:
    """Fingerprint of what the pool was written for.

    Changes when the place changes state or the cast standing in it changes,
    which is exactly when the old lines stop fitting.
    """
    state = ""
    npc_ids: list[str] = []
    with get_session() as session:
        location = session.get(Location, location_id)
        if location is not None:
            state = location.current_state or ""
        npc_ids = sorted(
            npc.id
            for npc in session.query(NPC).filter(
                NPC.current_location_id == location_id,
                NPC.status == "alive",
            )
        )
    raw = "|".join([location_id, state, *npc_ids])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _hours_since(meta: dict, clock: Any) -> float:
    """In-game hours between the pool's stamp and the clock. Never negative."""
    day = meta.get("day")
    hour = meta.get("hour")
    if day is None or hour is None:
        return 0.0
    try:
        elapsed = (
            (int(getattr(clock, "day", 0)) - int(day)) * 24.0
            + (int(getattr(clock, "hour", 0)) - int(hour))
            + (int(getattr(clock, "minute", 0) or 0) - int(meta.get("minute", 0) or 0)) / 60.0
        )
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, elapsed)


def barks_stale(location: Any, clock: Any = None) -> bool:
    """True when this location's pool needs regenerating."""
    if location is None:
        return False
    if not getattr(location, "ambient_barks", None):
        return True
    meta = getattr(location, "ambient_barks_meta", None) or {}
    if meta.get("signature") != bark_signature(location.id):
        return True
    if clock is None:
        return False
    return _hours_since(meta, clock) > bark_refresh_hours()


def cached_barks(location: Any) -> list[dict]:
    """The cached pool in client shape: [{"npc_id": str|None, "text": str}]."""
    pool = getattr(location, "ambient_barks", None) or []
    out = []
    for entry in pool:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        npc_id = entry.get("npc_id")
        out.append({"npc_id": npc_id if isinstance(npc_id, str) and npc_id else None,
                    "text": text})
    return out


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------

def _strip_fences(raw: str) -> str:
    """Drop ```json fences (and any prose before them) from a model reply."""
    text = (raw or "").strip()
    if "```" in text:
        parts = text.split("```")
        # Prefer the first fenced block; strip a leading language tag
        for part in parts[1:]:
            body = part.strip()
            if body.lower().startswith("json"):
                body = body[4:].strip()
            if body:
                return body
    return text


def parse_barks(raw: str, speakers: dict[str, str], limit: int) -> list[dict]:
    """Coerce a model reply into bark rows. Never raises.

    Args:
        raw: The raw model output.
        speakers: lowercased NPC name -> npc_id, for the NPCs allowed to be
            named as speakers here.
        limit: Maximum number of rows to keep.

    Returns:
        [{"npc_id": str|None, "text": str}] — unknown speakers (including
        "ambient" and anyone not in `speakers`) resolve to None.
    """
    text = _strip_fences(raw)
    data: Any = None
    try:
        data = json.loads(text)
    except Exception:
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start:end + 1])
            except Exception:
                data = None
    if not isinstance(data, list):
        return []

    out: list[dict] = []
    for entry in data:
        if isinstance(entry, str):
            entry = {"speaker": "ambient", "text": entry}
        if not isinstance(entry, dict):
            continue
        line = str(entry.get("text") or "").strip()
        if not line:
            continue
        speaker = str(entry.get("speaker") or "").strip().lower()
        out.append({"npc_id": speakers.get(speaker), "text": line[:TEXT_MAX]})
        if len(out) >= limit:
            break
    return out


# ----------------------------------------------------------------------
# Generation
# ----------------------------------------------------------------------

def _gather_context(location_id: str) -> dict | None:
    """Everything the bark call needs, read in one session."""
    with get_session() as session:
        location = session.get(Location, location_id)
        if location is None:
            return None

        bible = session.query(WorldBible).first()
        tone_parts = []
        if bible:
            if bible.genre:
                tone_parts.append(bible.genre)
            tone_parts.extend(list(bible.sub_genres or [])[:2])
            if bible.tone:
                tone_parts.append(bible.tone)

        npcs = session.query(NPC).filter(
            NPC.current_location_id == location_id,
            NPC.status == "alive",
        ).all()
        # R15: MAJOR NPCs never mutter filler — their lines resolve to ambient
        speakers = [
            {"id": n.id, "name": n.name, "profession": n.profession or "",
             "mood": n.current_mood or ""}
            for n in npcs
            if n.tier != NPCTier.MAJOR and (n.name or "").strip()
        ]

        clock = session.query(WorldClock).first()
        stamp = {"day": clock.day if clock else 1,
                 "hour": clock.hour if clock else 0}

        return {
            "name": location.name,
            "type": location.type.value if location.type else "",
            "description": location.description or "",
            "atmosphere": list(location.atmosphere_tags or []),
            "state": location.current_state or "",
            "tone": ", ".join(p for p in tone_parts if p),
            "speakers": speakers,
            "stamp": stamp,
        }


def _build_request(context: dict) -> str:
    """The user message: the place, then the people standing in it."""
    lines = []
    if context["tone"]:
        lines.append(f"World tone: {context['tone']}")
    header = context["name"]
    if context["type"]:
        header += f" ({context['type']})"
    lines.append(f"Place: {header}")
    if context["description"]:
        lines.append(f"About it: {context['description'][:600]}")
    if context["atmosphere"]:
        lines.append("Atmosphere: " + ", ".join(str(t) for t in context["atmosphere"][:6]))
    if context["state"] and context["state"] != "peaceful":
        lines.append(f"Right now the place is: {context['state']}")

    if context["speakers"]:
        lines.append("People here (use these exact names, or \"ambient\"):")
        for s in context["speakers"][:8]:
            bits = [s["name"]]
            if s["profession"]:
                bits.append(s["profession"])
            if s["mood"] and s["mood"] != "neutral":
                bits.append(f"mood: {s['mood']}")
            lines.append("- " + ", ".join(bits))
    else:
        lines.append('Nobody is named here — every speaker is "ambient".')
    return "\n".join(lines)


def _default_run_agent(prompt: str, n: int) -> str:
    """One fast-model call. Blocking — callers push it off the event loop."""
    from src.agents.base import create_agent

    agent = create_agent(
        agent_name=_agent_name(),
        # The prompt carries a JSON example, so .format() would choke on it
        system_prompt=BARK_PROMPT.replace("{n}", str(n)),
        tools=[],
        callback_handler=None,
    )
    return str(agent(prompt))


async def generate_barks(
    location_id: str,
    run_agent: Callable[[str], str] | None = None,
) -> list[dict]:
    """Generate and cache this location's bark pool. ONE fast-model call.

    Dispatched through the AssetManager's dedupe plumbing:
        spawn_generation(f"barks:{location_id}", lambda: generate_barks(id))

    Args:
        location_id: The location to write barks for.
        run_agent: Override for the model call (tests). Takes the built
            request string, returns the raw reply.

    Returns:
        The rows written, or [] on any failure — barks are decoration, never
        an error. A failure is held off for FAILURE_COOLDOWN_SECONDS.
    """
    if not barks_enabled():
        return []
    if _recently_failed(location_id):
        return []

    try:
        context = _gather_context(location_id)
        if context is None:
            logger.warning(f"Barks: no such location {location_id}")
            return []

        pool_size = bark_pool_size()
        prompt = _build_request(context)
        runner = run_agent or (lambda p: _default_run_agent(p, pool_size))
        raw = await asyncio.to_thread(runner, prompt)

        speakers = {s["name"].strip().lower(): s["id"] for s in context["speakers"]}
        barks = parse_barks(raw, speakers, pool_size + POOL_SLACK)
        if not barks:
            logger.warning(f"Barks [{context['name']}]: nothing usable in the reply")
            _mark_failure(location_id)
            return []

        signature = bark_signature(location_id)
        with get_session() as session:
            location = session.get(Location, location_id)
            if location is None:
                return []
            # JSON columns must be REASSIGNED, not mutated in place
            location.ambient_barks = barks
            location.ambient_barks_meta = {
                "signature": signature,
                "day": context["stamp"]["day"],
                "hour": context["stamp"]["hour"],
            }
            session.commit()

        _failures.pop(location_id, None)
        logger.info(f"Barks [{context['name']}]: cached {len(barks)} lines")
        return barks

    except Exception as e:
        logger.warning(f"Bark generation failed for {location_id}: {e}")
        _mark_failure(location_id)
        return []

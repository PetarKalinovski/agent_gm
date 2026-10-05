"""The mystery figure the story already has — and the one the DM made up.

fleet10-wenna T7-T11: while Sorrel's beat had him glancing at the north
wall (his secret: a man in an ash-gray cloak crouched there two nights
ago), the DM wrote `@new-npc Corran Pike | trade: drover | … | manner:
patient, unreadable` — a one-gloved drover who watched the player from the
far post, kept his name ("Name's mine to keep for now") and left (T17).
Nobody had written him a goal or a secret: four turns went to a thread that
had to dead-end, and the authored one never came up. The same drover, word
for word, was made up in fleet8-bram T13 and fleet9-bram T11.

- `authored_stranger`: the story's own stranger this scene's clues point
  at — an absent member of the cast (villain, successor, a live beat's
  person) whose looks a present person's secret, or the words of a beat
  about someone here, describe ("ash-gray cloak" — his own cloak).
- `brief_line`: the brief offers that stranger whenever the scene has them
  in its clues, before the DM reaches for a new one.
- The audit that names a made-up suspect is
  `turn_audit.invented_suspect_warning`.

Never raises; reads only.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Words a look shares with any clue: never what picks a stranger out
_PLAIN = frozenset({
    "with", "from", "that", "this", "their", "them", "over", "under", "into", "like", "wearing", "worn", "eyes",
    "eyed", "hair", "haired", "face", "faced", "hand", "hands", "skin", "young", "old", "older", "tall", "short",
    "lean", "thin", "broad", "small", "large", "big", "little", "man", "woman", "men", "women", "figure", "person",
    "people", "who", "and", "the", "his", "her", "one", "two", "three", "nights", "night", "ago", "saw", "seen",
    "told", "asks", "ask", "never", "anyone", "someone", "north", "south", "east", "west", "near", "by",
    "a", "an", "of", "on", "in", "at", "to", "for", "he", "she", "it", "no", "not", "was", "is", "are", "be",
})


def _look_words(text: str) -> set[str]:
    """The words of a look or a clue that could pick someone out: hyphens
    split ("ash-gray" -> ash, gray), plain words out."""
    words = re.findall(r"[a-z]+", (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in _PLAIN}


def _telling(shared: set[str]) -> bool:
    """Two shared words, one of them a garment or a colour: "ash-gray cloak"
    picks a man out; "wolf" and "teeth" in a wolf campaign do not."""
    from src.services.dialogue import _COLOURS, _GARMENTS

    return len(shared) >= 2 and bool(shared & (set(_GARMENTS) | set(_COLOURS)))


def _clues(session, player_id: str, present: list[Any]) -> list[tuple[str, str]]:
    """(where it comes from, its words) for what the scene holds about a
    stranger: the secrets of the people here, and the directives of the
    beats about them that are live now (armed, due, stalled) or were told
    today — never one still to come."""
    from src.models import WorldClock
    from src.services.campaign import _beat_cast, _campaign_beats, get_campaign_row

    out: list[tuple[str, str]] = []
    for npc in present:
        for secret in npc.secrets or []:
            if secret:
                out.append((f"{npc.name}'s secret", str(secret)))
    campaign = get_campaign_row(session, player_id)
    if campaign is None or campaign.status != "active":
        return out
    here = {n.id for n in present}
    clock = session.query(WorldClock).first()
    today = clock.day if clock is not None else None
    for beat in _campaign_beats(session, campaign):
        if beat.status not in ("armed", "fired", "stalled") and not (
                beat.status == "delivered" and today is not None and beat.delivered_day == today):
            continue
        try:
            cast = {n.id for n in _beat_cast(session, beat, alive_only=False)}
        except Exception:  # a beat whose cast cannot be read is no clue
            continue
        if cast & here:
            out.append((f"the beat \"{beat.title}\"", str(beat.directive or "")))
    return out


def authored_stranger(session, player_id: str | None, present_ids: Iterable[str]) -> tuple[Any, str] | None:
    """(the NPC row, where the scene speaks of them) for the story's stranger
    this scene's clues describe by their looks, else None."""
    if not player_id:
        return None
    try:
        from src.models import NPC
        from src.services.campaign import get_campaign_row, story_cast_ids
        from src.services.dialogue import is_beast

        campaign = get_campaign_row(session, player_id)
        if campaign is None or campaign.status != "active":
            return None
        ids = set(present_ids or ())
        present = session.query(NPC).filter(NPC.id.in_(list(ids))).all() if ids else []
        clues = _clues(session, player_id, present)
        if not clues:
            return None
        cast = story_cast_ids(session, player_id) | {
            i for i in (campaign.villain_npc_id, campaign.successor_npc_id) if i}
        # A name is no look: "Gray-Flank Wolf" says nothing of a gray cloak
        from src.models import Location

        named: set[str] = set()
        for (name,) in session.query(NPC.name).all() + session.query(Location.name).all():
            named |= _look_words(name or "")
        best: tuple[int, Any, str] | None = None
        for npc in session.query(NPC).filter(NPC.id.in_(list(cast - ids))).all() if cast - ids else []:
            if (npc.status or "alive") != "alive" or is_beast(npc):
                continue
            looks = _look_words(npc.description_physical or "") - named
            for source, words in clues:
                shared = looks & _look_words(words)
                if _telling(shared) and (best is None or len(shared) > best[0]):
                    best = (len(shared), npc, source)
        return (best[1], best[2]) if best else None
    except Exception as e:
        logger.debug(f"authored stranger unavailable: {e}")
        return None


def stranger_words(npc: Any, source: str) -> str:
    """How the DM is told of them: the look the player knows them by, the
    name the DM knows, and where the scene speaks of them."""
    from src.services.dialogue import pronoun, role_label

    who = pronoun(npc) or "they"
    return f"the {role_label(npc)} ({npc.name} — {source} speaks of {'him' if who == 'he' else 'her' if who == 'she' else 'them'})"


def brief_line(session, player: Any, npcs: list[Any]) -> list[str]:
    """One line offering the authored stranger when the scene's clues point
    at them, else none. Never raises."""
    try:
        found = authored_stranger(session, getattr(player, "id", None), [n.id for n in npcs or []])
        if not found:
            return []
        npc, source = found
        return [f"The stranger this scene's clues point at is written already: {stranger_words(npc, source)}. "
                "When the scene wants a watcher, a lurker or a mystery figure, show signs of that one (something "
                "left behind, a figure too far off to know) — never a new made-up suspect; @new-npc is for "
                "ordinary folk with nothing to hide, and what anyone here knows of that stranger still waits "
                "behind its own gate."]
    except Exception as e:
        logger.debug(f"stranger line skipped: {e}")
        return []

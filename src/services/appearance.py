"""Appearance signatures: which sprite set the player's gear entitles them to.

Pure functions over an already-loaded Player row. No queries, no disk, no
network — this runs inside `build_player_state`, which runs after every
world-write tool.

Two axes, deliberately orthogonal (00-integration extension, ruling A1):
  body   <- equipped armor  -> idles, walk cycles, dodge cycle   (58 files)
  weapon <- equipped weapon -> attack cycle only                  (6 files)
The attack cycle is keyed on the PAIR (A2) because it must show both.

`Item.attire` NEVER enters a signature or a prompt (A10) — it is social
metadata owned by the perception system; anything visually load-bearing
about a hooded cloak already lives in its `visual_desc`.
"""

import hashlib
import re
from typing import Any

from src.config import load_settings
from src.services import progression

# Bump to invalidate EVERY generated variant (prompt machinery changed).
# Folded into the hash input, so old directories simply stop being addressed.
APPEARANCE_VERSION = 1

BASE_BODY_SIG = "base"
BASE_WEAPON_SIG = "unarmed"
BASE_ACTION_SIG = f"{BASE_BODY_SIG}-{BASE_WEAPON_SIG}"

# Axis prefixes keep the two hash spaces visibly distinct in a directory
# listing; the body axis is bare hex because it is the common one.
BODY_PREFIX = ""
WEAPON_PREFIX = "w"

# A paragraph in an image prompt buys nothing and destabilises the strip.
VISUAL_PHRASE_MAX = 180

# Fallback nouns when an item carries neither visual_desc nor a usable name
_SLOT_NOUN = {"weapon": "a plain weapon", "armor": "plain travelling clothes"}

# Signatures become directory names and arrive from the client as `?a=` —
# anything outside this shape is refused rather than joined onto a path.
_SIG_RE = re.compile(r"^[a-z0-9]{1,32}(-[a-z0-9]{1,32})?$")


def enabled() -> bool:
    """The `appearance.enabled` kill switch. Missing config reads as on."""
    try:
        return bool(load_settings().appearance.enabled)
    except Exception:  # pragma: no cover - config is always loadable in practice
        return True


def is_valid_signature(sig: Any) -> bool:
    """True for a signature safe to use as a directory name.

    `?a=` is client-supplied, and `sprite_dir` joins it onto a path — this is
    the guard that keeps `?a=../../..` from ever becoming a filesystem walk.
    """
    return isinstance(sig, str) and bool(_SIG_RE.match(sig))


def visual_phrase(item: dict | None, slot: str) -> str | None:
    """The concrete phrase that goes into an image prompt for this item.

    Fallback chain (frozen seam): visual_desc -> name -> slot noun.
    Trimmed, whitespace-collapsed and clamped to VISUAL_PHRASE_MAX.
    """
    if not isinstance(item, dict):
        return None

    phrase = ""
    for candidate in (item.get("visual_desc"), item.get("name"), _SLOT_NOUN.get(slot)):
        if isinstance(candidate, str) and candidate.strip():
            phrase = candidate
            break

    phrase = " ".join(phrase.split())
    if not phrase:
        return None
    return phrase[:VISUAL_PHRASE_MAX]


def _axis_sig(prefix: str, item: dict | None, slot: str, empty: str) -> str:
    """'base'/'unarmed' when the slot is empty, else prefix + 8 hex.

    Hash input is EXACTLY what reaches the prompt plus the item identity:
        f"{APPEARANCE_VERSION}|{item_id}|{visual_phrase.lower()}"
    so re-authoring an item's visual_desc regenerates its art, two different
    items never share a directory, and bumping APPEARANCE_VERSION invalidates
    the world.
    """
    if not item:
        return empty
    raw = "|".join((
        str(APPEARANCE_VERSION),
        str(progression.item_key(item)),
        (visual_phrase(item, slot) or "").strip().lower(),
    ))
    return prefix + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def body_signature(player) -> str:
    """'base' | 'a3f9c1b2' — the armor axis (idles, walks, dodge)."""
    if not enabled():
        return BASE_BODY_SIG
    armor = progression.resolve_loadout(player).get("armor")
    return _axis_sig(BODY_PREFIX, armor, "armor", BASE_BODY_SIG)


def weapon_signature(player) -> str:
    """'unarmed' | 'w9c14ab02' — the weapon axis (attack cycle only)."""
    if not enabled():
        return BASE_WEAPON_SIG
    weapon = progression.resolve_loadout(player).get("weapon")
    return _axis_sig(WEAPON_PREFIX, weapon, "weapon", BASE_WEAPON_SIG)


def action_signature(player) -> str:
    """f"{body}-{weapon}" — the attack strip must show both (A2)."""
    return f"{body_signature(player)}-{weapon_signature(player)}"


def appearance_payload(player) -> dict[str, Any]:
    """What rides on `state.appearance`. WANTED signatures only (A6).

    Readiness is deliberately absent: it costs ~58 `stat()` calls and this
    runs after every world-write tool. The two endpoints that already touch
    disk report readiness (`AssetManager.appearance_status`).
    """
    body = body_signature(player)
    weapon = weapon_signature(player)
    return {
        "body_sig": body,
        "weapon_sig": weapon,
        "action_sig": f"{body}-{weapon}",
        "version": APPEARANCE_VERSION,
    }


def base_payload() -> dict[str, Any]:
    """The payload for a world with no player row (and the disabled case)."""
    return {
        "body_sig": BASE_BODY_SIG,
        "weapon_sig": BASE_WEAPON_SIG,
        "action_sig": BASE_ACTION_SIG,
        "version": APPEARANCE_VERSION,
    }


def split_action_sig(sig: str) -> tuple[str, str]:
    """'a3f9c1b2-w9c14ab02' -> ('a3f9c1b2', 'w9c14ab02').

    A malformed pair degrades to the base pair rather than raising — the only
    caller is a generation task, and a wrong directory is worse than a no-op.
    """
    body, _, weapon = str(sig or "").partition("-")
    return (body or BASE_BODY_SIG), (weapon or BASE_WEAPON_SIG)


def outfit_phrase(player) -> str | None:
    """The armor phrase for the outfit-variant idle prompt, or None if bare."""
    armor = progression.resolve_loadout(player).get("armor")
    return visual_phrase(armor, "armor") if armor else None


def weapon_phrase(player) -> str | None:
    """The weapon phrase for the attack-cycle prompt, or None if unarmed."""
    weapon = progression.resolve_loadout(player).get("weapon")
    return visual_phrase(weapon, "weapon") if weapon else None

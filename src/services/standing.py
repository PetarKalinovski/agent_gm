"""Standing — reputation and disposition with teeth, thresholds in ONE place.

Reputation per faction, an NPC's disposition and trust, a faction's hold on
a place: the engine tracked all of it and handed the numbers to the model,
and the model was the only thing that ever "enforced" them. Now code does:

  * merchants price by what they think of the player (`price_multiplier`);
  * a place held by a faction that hates the player refuses them at the
    door (`travel.check_route`, reason "standing");
  * someone who hates the player and does not trust them will not talk
    (`conversation_refusal`, no model call, voiced through a speech event);
  * a secret needs trust to get out (`reveal_secret`, SECRET_TRUST).

Every number below is the only copy. Prompts say "the engine decides";
they never quote a threshold.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

from src.models import Faction, NPCRelationship
from src.services import progression

REPUTATION_DEFAULT = 50                      # matches update_player_reputation's baseline
STANDING_BANDS = (("honoured", 80), ("liked", 65), ("neutral", 35), ("disliked", 20), ("hostile", 0))

HOSTILE_DISPOSITIONS = frozenset({"hostile", "furious", "murderous", "enemy", "hateful", "vengeful", "contemptuous"})
WARM_DISPOSITIONS = frozenset({"friendly", "warm", "devoted", "loyal", "fond", "grateful", "trusting", "affectionate"})

REFUSAL_TRUST = 30       # hostile AND below this -> no conversation
JOIN_TRUST = 60          # follows the player only from here up (or when warm).
                         # Above the stranger default (50): one conversation must not
                         # recruit anyone — the 2026-10-04 playtest was offered the villain
SECRET_TRUST = 60        # reveal_secret needs at least this much
BUYBACK_FRACTION = 0.40  # what a merchant pays for what the player sells
GIFT_TRUST = 5           # a gift given in the player's own words (commands._gift_trust),
                         # once a game-day per person: two days' kindness opens a secret
GIFT_TRUST_CAP = 70      # ...and gifts alone never buy more trust than this

STANDING_PRICE_MUL = {"honoured": 0.80, "liked": 0.90, "neutral": 1.00, "disliked": 1.15, "hostile": 1.40}
DISPOSITION_PRICE_MUL = {"warm": 0.90, "neutral": 1.00, "hostile": 1.25}
MUL_FLOOR, MUL_CEIL = 0.70, 1.60

_REFUSAL_LINES = (
    "Not to you. Not today, not ever.",
    "You've got a nerve, coming to me. Get out of my sight.",
    "I don't talk to your kind. Move along before I call someone who does worse than talk.",
    "Whatever it is, the answer's no. Go on.",
    "I know what you are. Save your breath.",
)


# ----------------------------------------------------------------------
# Reputation / disposition
# ----------------------------------------------------------------------

def reputation_of(player, faction_id: str | None) -> int:
    """The player's score with a faction; the legacy default when unknown."""
    if not faction_id:
        return REPUTATION_DEFAULT
    rep = getattr(player, "reputation", None) or {}
    try:
        return int(rep.get(faction_id, REPUTATION_DEFAULT))
    except (TypeError, ValueError):
        return REPUTATION_DEFAULT


def standing_label(score: int) -> str:
    for label, floor in STANDING_BANDS:
        if score >= floor:
            return label
    return "hostile"


def standing_for(player, faction_id: str | None) -> str:
    """Pure dict math — safe on every exit of every scene load."""
    return standing_label(reputation_of(player, faction_id))


def disposition_band(relationship: Any) -> str:
    """warm | neutral | hostile from an NPCRelationship row (or dict, or None)."""
    if relationship is None:
        return "neutral"
    raw = relationship.get("current_disposition") if isinstance(relationship, dict) \
        else getattr(relationship, "current_disposition", None)
    word = str(raw or "").strip().lower()
    if word in HOSTILE_DISPOSITIONS:
        return "hostile"
    if word in WARM_DISPOSITIONS:
        return "warm"
    return "neutral"


def trust_of(relationship: Any) -> int:
    if relationship is None:
        return 0
    raw = relationship.get("trust_level") if isinstance(relationship, dict) \
        else getattr(relationship, "trust_level", None)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def relationship_for(session, player_id: str | None, npc_id: str | None):
    if not session or not player_id or not npc_id:
        return None
    return session.query(NPCRelationship).filter(
        NPCRelationship.npc_id == npc_id, NPCRelationship.player_id == player_id
    ).first()


# ----------------------------------------------------------------------
# Prices
# ----------------------------------------------------------------------

def price_multiplier(session, player, npc) -> tuple[float, str]:
    """(multiplier, in-fiction reason) — what this merchant thinks of them.

    Neutral standing with no relationship is exactly 1.0, so every legacy
    price is unchanged until something is actually known.
    """
    if player is None or npc is None:
        return 1.0, ""
    reasons: list[str] = []
    mul = 1.0

    faction_id = getattr(npc, "faction_id", None)
    if faction_id:
        band = standing_for(player, faction_id)
        mul *= STANDING_PRICE_MUL.get(band, 1.0)
        if band != "neutral":
            faction = session.get(Faction, faction_id) if session is not None else None
            name = faction.name if faction is not None else "their faction"
            reasons.append({
                "honoured": f"honoured by {name}", "liked": f"in good standing with {name}",
                "disliked": f"out of favour with {name}", "hostile": f"hated by {name}",
            }[band])

    relationship = relationship_for(session, getattr(player, "id", None), getattr(npc, "id", None))
    band = disposition_band(relationship)
    mul *= DISPOSITION_PRICE_MUL.get(band, 1.0)
    if band == "warm":
        reasons.append(f"{npc.name} likes them")
    elif band == "hostile":
        reasons.append(f"{npc.name} dislikes them")

    mul = max(MUL_FLOOR, min(MUL_CEIL, mul))
    return round(mul, 4), "; ".join(reasons)


def buy_price(base: int, mul: float) -> int:
    """Round half up, never below one coin."""
    try:
        base_value = max(0, int(base or 0))
    except (TypeError, ValueError):
        base_value = 0
    if base_value == 0:
        return 0            # a free thing stays free
    return max(1, int(math.floor(base_value * float(mul) + 0.5)))


def sell_price(base: int) -> int:
    """What the counter pays: the buyback fraction of the base, floor one."""
    try:
        base_value = max(0, int(base or 0))
    except (TypeError, ValueError):
        base_value = 0
    return max(1, int(math.floor(base_value * BUYBACK_FRACTION + 0.5))) if base_value else 0


def base_price(item: dict[str, Any] | None) -> int:
    """The catalog's number for mechanical items; the authored value otherwise."""
    if not isinstance(item, dict):
        return 0
    price = progression.catalog_price(item)
    if price is None:
        try:
            price = int(item.get("value") or 0)
        except (TypeError, ValueError):
            price = 0
    return max(0, int(price))


def price_for(session, player, npc, item: dict[str, Any] | None) -> int:
    """THE one buy price: catalog base x what this merchant thinks of them."""
    mul, _ = price_multiplier(session, player, npc)
    return buy_price(base_price(item), mul)


def npc_purse(npc) -> int | None:
    """None = bottomless (every legacy merchant); a number = a real till."""
    value = getattr(npc, "currency", None)
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# Refusals
# ----------------------------------------------------------------------

def refusal_line(npc_id: str | None) -> str:
    """The same NPC always says the same thing — code picks, not chance."""
    digest = hashlib.sha1((npc_id or "").encode("utf-8")).hexdigest()
    return _REFUSAL_LINES[int(digest[:8], 16) % len(_REFUSAL_LINES)]


def conversation_refusal(session, player, npc, relationship: Any = None) -> dict[str, Any] | None:
    """Why this NPC will not talk to this player, or None when they will."""
    if npc is None or player is None:
        return None
    if relationship is None:
        relationship = relationship_for(session, getattr(player, "id", None), getattr(npc, "id", None))
    if disposition_band(relationship) != "hostile":
        return None
    trust = trust_of(relationship)
    if trust >= REFUSAL_TRUST:
        return None
    line = refusal_line(getattr(npc, "id", None))
    return {
        "line": line,
        "trust": trust,
        "disposition": str(getattr(relationship, "current_disposition", "") or "hostile"),
        "note": (
            f"{npc.name} refused to talk to the player (the engine decided: they are hostile "
            "and trust is too low). Their one line was ALREADY spoken on the player's screen. "
            "Do NOT put words in their mouth — narrate the cold shoulder and what it costs, "
            "and let the player find another way in."
        ),
    }


def join_refusal(npc, relationship: Any) -> dict[str, Any] | None:
    """Why this NPC will not travel with the player, or None when they would.

    Hostile never follows; warm follows regardless of the number; everyone
    else needs JOIN_TRUST. The number lives here and nowhere else.
    """
    band = disposition_band(relationship)
    if band == "hostile":
        return {"error": "hostile", "message": f"{getattr(npc, 'name', 'They')} would sooner see the player dead than walk beside them."}
    if band == "warm":
        return None
    trust = trust_of(relationship)
    if trust >= JOIN_TRUST:
        return None
    return {
        "error": "trust",
        "message": (
            f"{getattr(npc, 'name', 'They')} does not trust the player enough to leave "
            "their life behind and follow a near-stranger. Earn it first."
        ),
    }


def secret_refusal(npc, relationship: Any) -> dict[str, Any] | None:
    """Trust gate for reveal_secret; None when the words can come out."""
    trust = trust_of(relationship)
    if trust >= SECRET_TRUST:
        return None
    return {
        "refused": True,
        "reason": "trust",
        "instruction": (
            f"{getattr(npc, 'name', 'They')} does not trust the player enough to get the words "
            "out. Narrate the half-sentence they swallow, and do NOT say the secret."
        ),
    }

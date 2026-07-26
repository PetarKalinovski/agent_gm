"""How the world READS the player, and who it thinks they are.

Two facts that must never be confused:

  APPEARANCE — what strangers see. Derived entirely from the equipped armor's
  `attire` dict plus a code-owned default. A famous knight in a farmhand's
  coat is a farmhand.

  IDENTITY — who they actually are. Derived from whether an NPC has an
  NPCRelationship row with them. Clothes do not change it; a hood can only
  make it *contested*.

Reputation, faction reactions, and the player's history apply when the player
is RECOGNISED, or when a uniform claims the identity on their behalf — never
merely because the player happens to be who they are.

Numbers live in `progression.py`; this module holds no numbers except the
style rank and the two recognition difficulties, both of which are label
tables. Every function here is either pure (takes an already-loaded row) or a
thin session wrapper named `*_for`.

Import direction is one-way: `perception -> progression`, never back.
"""

import threading
from typing import Any

from loguru import logger

from src.services import progression as prog

# ---------------------------------------------------------------------------
# Vocabulary — fixed menus, nothing may be added at runtime
# ---------------------------------------------------------------------------

STYLES = ("filthy", "rough", "common", "fine", "noble")   # ascending
STYLE_RANK = {s: i for i, s in enumerate(STYLES)}
DEFAULT_STYLE = "common"
CONCEALING_TOKEN = "concealing"
DRESS_TOKENS = STYLES + (CONCEALING_TOKEN,)

# Armor with no authored attire still has to read as SOMETHING.
STYLE_BY_TIER = {"crude": "rough", "common": "common", "fine": "fine",
                 "superior": "fine", "master": "noble", "legendary": "noble"}

# The one-line human string a stranger would use. Never shown to the player
# verbatim — it goes to the DM and to the character panel.
STYLE_READ_AS = {
    "filthy": "someone sleeping rough",
    "rough":  "someone who works with their hands",
    "common": "an ordinary townsperson",
    "fine":   "someone with money",
    "noble":  "someone of rank",
}
CONCEALED_READ_AS = "someone who does not want to be seen"
UNKNOWN_COLOURS_READ_AS = "someone in unfamiliar colours"

FAMILIARITY = ("stranger", "acquainted", "close")
VERDICTS = ("known", "unknown", "contested")

# How hard it is to stay unrecognised by someone who has met you. Words from
# progression.DIFFICULTY_DC — code picks them so the DM cannot sandbag.
RECOGNITION_DIFFICULTY = {"acquainted": "tricky", "close": "hard"}
RECOGNITION_SKILL = "stealth"
# A failed stealth roll means they were MADE: the player rolled to stay hidden.
RECOGNISED_OUTCOMES = ("failure", "critical_failure")

CLOSE_TRUST = 70          # trust_level at or above this reads as "close"
CLOSE_MOMENTS = 3         # ...or this many key_moments

READ_AS_MAX = 90          # chars; truncated on a word boundary
DRESS_REASON_MAX = 200

# Said by the door when an author wrote a gate but forgot to write a reason.
DEFAULT_DRESS_REASON = "This house has standards about how people come dressed."

# ---------------------------------------------------------------------------
# Situational check factors — four rows, first match wins, never stacked
# ---------------------------------------------------------------------------

CHECK_FACTORS = ("dark_or_weather", "filthy_in_fine_company",
                 "hooded_and_watched", "wearing_their_colours")

_FACTOR_ROWS = {
    "dark_or_weather": {
        "effect": "advantage",
        "because": "the dark and the weather are covering them",
    },
    "filthy_in_fine_company": {
        "effect": "disadvantage",
        "because": "they are dressed like something the tide left, in a room that notices",
    },
    "hooded_and_watched": {
        "effect": "disadvantage",
        "because": "nobody trusts a face they cannot see",
    },
    "wearing_their_colours": {
        "effect": "advantage",
        "because": "they are wearing the colours of the people they are dealing with",
    },
}

COVERING_WEATHER = {"fog", "rain", "snow", "ash"}
FINE_ROOM_TAGS = {"wealthy", "opulent", "noble", "refined"}
FINE_ROOM_MIN_STYLE = "fine"
DRESSED_DOWN_STYLES = {"filthy", "rough"}


# ---------------------------------------------------------------------------
# Normalizers — the authoring boundary
# ---------------------------------------------------------------------------


def normalize_attire(raw: Any, *, slot: str | None = None) -> dict | None:
    """Clamp an authored attire dict to the fixed menu, or None.

    Returns None (and logs) when `slot` is supplied and is anything but
    "armor" — the frozen seam says attire exists on armor and nowhere else.
    Unknown style words fall back to DEFAULT_STYLE rather than erroring: a
    garbled label must never cost a turn.
    """
    if not isinstance(raw, dict):
        return None
    if slot is not None and str(slot).strip().lower() != "armor":
        logger.debug(f"attire dropped: only armor carries it (slot={slot!r})")
        return None

    style = raw.get("style")
    style = style.strip().lower() if isinstance(style, str) else ""
    if style not in STYLE_RANK:
        style = DEFAULT_STYLE

    uniform = raw.get("uniform_faction_id")
    uniform = uniform.strip() or None if isinstance(uniform, str) else None

    return {
        "style": style,
        "uniform_faction_id": uniform,
        "concealing": bool(raw.get("concealing")),
    }


def normalize_dress_code(raw: Any) -> dict | None:
    """Clamp an authored dress code, or None when nothing survives.

    - min_style: one of STYLES, else None
    - banned_styles: deduped subset of DRESS_TOKENS, order-stable
    - reason: str, stripped, capped at DRESS_REASON_MAX, digits NOT stripped
      (a house rule may legitimately say "after the ninth bell")

    A dict with neither a min_style nor a banned_styles entry returns None —
    an empty gate is worse than no gate, because the DM would still see it.
    """
    if not isinstance(raw, dict):
        return None

    min_style = raw.get("min_style")
    min_style = min_style.strip().lower() if isinstance(min_style, str) else None
    if min_style not in STYLE_RANK:
        min_style = None

    banned: list[str] = []
    for token in (raw.get("banned_styles") or []):
        if not isinstance(token, str):
            continue
        token = token.strip().lower()
        if token in DRESS_TOKENS and token not in banned:
            banned.append(token)

    if not min_style and not banned:
        return None

    reason = raw.get("reason")
    reason = reason.strip()[:DRESS_REASON_MAX] if isinstance(reason, str) else ""
    return {"min_style": min_style, "banned_styles": banned, "reason": reason}


# ---------------------------------------------------------------------------
# Reading the player
# ---------------------------------------------------------------------------


def attire_of(item: dict | None) -> dict | None:
    """The normalized attire of an inventory entry, armor-slot only.

    Mechanizes through `progression.mechanize_item` first, so a legacy
    `type:"armor"` item is eligible too.
    """
    if not isinstance(item, dict):
        return None
    mech = prog.mechanize_item(item)
    if not mech or mech.get("slot") != "armor":
        return None
    return normalize_attire(item.get("attire"), slot="armor")


def _truncate(text: str, limit: int = READ_AS_MAX) -> str:
    """Cap on a word boundary with an ellipsis."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    if " " in cut:
        cut = cut[:cut.rfind(" ")].rstrip()
    return cut.rstrip(" ,;:-—") + "…"


def _read_as_base(style: str, uniform: str | None, concealing: bool,
                  faction_name: str | None) -> str:
    """The half of `read_as` that survives without a visual description."""
    if concealing:
        return CONCEALED_READ_AS
    if uniform and faction_name:
        return f"a member of the {faction_name}"
    if uniform:
        return UNKNOWN_COLOURS_READ_AS
    return STYLE_READ_AS.get(style, STYLE_READ_AS[DEFAULT_STYLE])


def read_as_short(profile: dict) -> str:
    """The `base` half of `read_as` — what a door's refusal quotes."""
    return str(profile.get("read_as") or "").split(" — ")[0]


def perception_profile(player, *, faction_name: str | None = None) -> dict:
    """How this player READS to a stranger. Pure — no queries.

    Returns {"style", "uniform_faction_id", "uniform_faction_name",
    "concealing", "visual_desc", "read_as", "source"} where `source` is
    "attire" | "tier" | "bare".
    """
    armor = prog.resolve_loadout(player).get("armor") if player is not None else None

    style = DEFAULT_STYLE
    uniform: str | None = None
    concealing = False
    visual_desc: str | None = None
    source = "bare"

    if armor:
        raw_desc = armor.get("visual_desc")
        if isinstance(raw_desc, str) and raw_desc.strip():
            visual_desc = raw_desc.strip()

        attire = attire_of(armor)
        if attire:
            style = attire["style"]
            uniform = attire["uniform_faction_id"]
            concealing = attire["concealing"]
            source = "attire"
        else:
            mech = prog.mechanize_item(armor) or {}
            label = prog.tier_label(mech.get("tier"))
            style = STYLE_BY_TIER.get(label or "", DEFAULT_STYLE)
            source = "tier"

    base = _read_as_base(style, uniform, concealing, faction_name)
    read_as = _truncate(f"{base} — {visual_desc}" if visual_desc else base)

    return {
        "style": style,
        "uniform_faction_id": uniform,
        "uniform_faction_name": faction_name if uniform else None,
        "concealing": concealing,
        "visual_desc": visual_desc,
        "read_as": read_as,
        "source": source,
    }


def profile_for(session, player) -> dict:
    """`perception_profile` with the uniform faction's NAME resolved.

    One extra query, and only when a uniform is actually worn.
    """
    profile = perception_profile(player)
    uniform = profile.get("uniform_faction_id")
    if not uniform:
        return profile

    name = None
    try:
        from src.models import Faction
        faction = session.get(Faction, uniform)
        name = faction.name if faction else None
    except Exception as e:  # a missing faction must never break a turn
        logger.debug(f"Uniform faction {uniform!r} unresolved: {e}")
    if not name:
        return profile
    return perception_profile(player, faction_name=name)


# ---------------------------------------------------------------------------
# Dress verdict — pure
# ---------------------------------------------------------------------------


def dress_verdict(profile: dict, dress_code: dict | None) -> dict:
    """{"allowed", "failed", "token", "reason", "requirement"}.

    `requirement` is the single human string that rides the existing
    `Connection.requirements` channel, so every consumer that joins
    requirements into a message keeps working untouched.
    """
    allowed = {"allowed": True, "failed": "", "token": "", "reason": "", "requirement": ""}
    code = normalize_dress_code(dress_code)
    if not code:
        return allowed

    banned = code["banned_styles"]
    style = profile.get("style") or DEFAULT_STYLE

    if profile.get("concealing") and CONCEALING_TOKEN in banned:
        failed, token = "banned", CONCEALING_TOKEN
    elif style in banned:
        failed, token = "banned", style
    elif code["min_style"] and STYLE_RANK.get(style, 0) < STYLE_RANK[code["min_style"]]:
        failed, token = "min_style", style
    else:
        return allowed

    reason = code["reason"] or DEFAULT_DRESS_REASON
    return {
        "allowed": False,
        "failed": failed,
        "token": token,
        "reason": reason,
        "requirement": f"{reason} (the player currently reads as {read_as_short(profile)})",
    }


# ---------------------------------------------------------------------------
# Recognition
# ---------------------------------------------------------------------------


def familiarity_of(relationship) -> str:
    """'stranger' | 'acquainted' | 'close' from an already-loaded row.

    Pure, so a caller that has already batched the rows never queries twice.
    """
    if relationship is None:
        return "stranger"
    try:
        trust = int(relationship.trust_level or 0)
    except (TypeError, ValueError):
        trust = 0
    moments = len(relationship.key_moments or [])
    if trust >= CLOSE_TRUST or moments >= CLOSE_MOMENTS:
        return "close"
    return "acquainted"


def familiarity(session, player_id: str, npc_id: str) -> str:
    """'stranger' | 'acquainted' | 'close'.

    The NPCRelationship row is the ONLY signal. It is written by
    `_record_npc_exchange` on every real exchange, which is exactly the
    definition we want: they know you because you have actually spoken.
    """
    if not player_id or not npc_id:
        return "stranger"

    from src.models import NPCRelationship

    return familiarity_of(session.query(NPCRelationship).filter(
        NPCRelationship.npc_id == npc_id,
        NPCRelationship.player_id == player_id,
    ).first())


# (player_id, npc_id) -> {"day": int, "recognized": bool}. Process-local,
# exactly like `pending_actions`: a restart mid-scene costs one extra roll.
_recognition_memo: dict[tuple[str, str], dict] = {}
_memo_lock = threading.Lock()


def remember_recognition(player_id: str, npc_id: str, *, recognized: bool, day: int) -> None:
    """Make a contested recognition stick for the rest of the game-day."""
    if not player_id or not npc_id:
        return
    try:
        day = int(day)
    except (TypeError, ValueError):
        day = 0
    with _memo_lock:
        _recognition_memo[(str(player_id), str(npc_id))] = {
            "day": day, "recognized": bool(recognized),
        }


def recalled_recognition(player_id: str, npc_id: str, day: int | None) -> bool | None:
    """The remembered answer, or None when unresolved or stale (a new day
    means a new hood and a fresh question)."""
    if not player_id or not npc_id:
        return None
    with _memo_lock:
        entry = _recognition_memo.get((str(player_id), str(npc_id)))
    if not entry:
        return None
    if day is not None and entry.get("day") != int(day):
        return None
    return bool(entry.get("recognized"))


def clear_recognition(player_id: str | None = None) -> None:
    """Forget resolved recognitions (world switch, player deletion)."""
    with _memo_lock:
        if player_id is None:
            _recognition_memo.clear()
            return
        for key in [k for k in _recognition_memo if k[0] == str(player_id)]:
            _recognition_memo.pop(key, None)


def _current_day(session) -> int:
    from src.models import WorldClock
    clock = session.query(WorldClock).first()
    return clock.day if clock else 1


def recognition(session, player, npc, *, profile: dict | None = None,
                day: int | None = None) -> dict:
    """Does this NPC know who they are looking at?

    Never rolls: a `contested` verdict carries the exact `roll_check` call the
    DM should make when the fiction forces the question.
    """
    profile = profile if profile is not None else perception_profile(player)
    player_id = getattr(player, "id", None)
    npc_id = getattr(npc, "id", None)
    npc_name = getattr(npc, "name", None) or "They"
    player_name = getattr(player, "name", None) or "this person"

    fam = familiarity(session, player_id, npc_id)
    concealing = bool(profile.get("concealing"))
    assumed = profile.get("uniform_faction_id")
    assumed_name = profile.get("uniform_faction_name")

    check: dict | None = None
    if fam == "stranger":
        verdict, identity_known = "unknown", False
        line = f"{npc_name} does not know this person. What they see: {profile['read_as']}."
    elif not concealing:
        verdict, identity_known = "known", True
        line = f"{npc_name} knows this person on sight: {player_name}."
    else:
        if day is None:
            day = _current_day(session)
        recalled = recalled_recognition(player_id, npc_id, day)
        if recalled is True:
            verdict, identity_known = "known", True
            line = f"{npc_name} has already placed them today, hood or no hood."
        elif recalled is False:
            verdict, identity_known = "unknown", False
            line = f"{npc_name} looked and did not place them today; the hood is holding."
        else:
            verdict, identity_known = "contested", False
            difficulty = RECOGNITION_DIFFICULTY[fam]
            check = {"skill": RECOGNITION_SKILL, "difficulty": difficulty,
                     "about_npc_id": npc_id}
            line = (
                f"{npc_name} has met them before but their face is covered — call "
                f'roll_check(player_id, "{RECOGNITION_SKILL}", "{difficulty}", '
                f'about_npc_id="{npc_id}") before deciding whether they are named.'
            )

    return {
        "familiarity": fam,
        "verdict": verdict,
        "identity_known": identity_known,
        "assumed_faction_id": assumed,
        "assumed_faction_name": assumed_name,
        "reputation_visible": identity_known,
        "check": check,
        "line": line,
    }


_BLOCK_HEADER = "**Who you see (the engine decided this — it is not yours to change):**"


def npc_recognition_block(session, player, npc, *, day: int | None = None) -> str:
    """The '**Who you see**' block for the NPC's system prompt.

    Fixed templates only — the engine decided this, and the NPC does not get a
    vote.
    """
    profile = profile_for(session, player)
    verdict = recognition(session, player, npc, profile=profile, day=day)
    read_as = profile["read_as"]
    faction = profile.get("uniform_faction_name")

    if verdict["verdict"] == "contested":
        return "\n".join([
            _BLOCK_HEADER,
            f"This person's face is covered: {read_as}.",
            "You have met them before, but you have NOT placed them. Unless the "
            "narrator tells you the hood came off, do not use their name, do not "
            "refer to anything you have done together, and do not act on your "
            "history with them. You may be uneasy. You may ask them to show their "
            "face.",
        ])

    if verdict["identity_known"]:
        name = getattr(player, "name", None) or "someone you know"
        return "\n".join([
            _BLOCK_HEADER,
            f"You know this person: {name}. You recognise them on sight, whatever "
            "they are wearing. Everything in your memory of them applies.",
            f"What they have on: {read_as}. You may absolutely have an opinion "
            "about that.",
        ])

    if faction:
        return "\n".join([
            _BLOCK_HEADER,
            f"You do not know this person. What you see: {read_as}.",
            f"They are wearing {faction} colours, so treat them as {faction}: give "
            f"them what you would give the {faction}, refuse them what you would "
            f"refuse the {faction}. You do not know their name. If you have reason "
            "to doubt the coat, you may doubt it out loud — but you do not know.",
        ])

    return "\n".join([
        _BLOCK_HEADER,
        f"You do not know this person. What you see: {read_as}.",
        "You do not know their name, their history, or their reputation. Do not "
        "use a name you were never told, and do not react to anything they have "
        "done elsewhere. Judge them on what is in front of you.",
    ])


# ---------------------------------------------------------------------------
# World-derived check modifiers
# ---------------------------------------------------------------------------


def _factor(factor_id: str) -> dict:
    row = _FACTOR_ROWS[factor_id]
    return {"factor": factor_id, "effect": row["effect"], "because": row["because"]}


def _dark_or_weather(session) -> bool:
    from src.models import DMState, WorldClock

    clock = session.query(WorldClock).first()
    if clock is not None and clock.get_time_of_day() == "night":
        return True
    state = session.query(DMState).first()
    weather = (getattr(state, "weather", None) or "").strip().lower() if state else ""
    return weather in COVERING_WEATHER


def _player_location(session, player):
    from src.models import Location

    location_id = getattr(player, "current_location_id", None)
    return session.get(Location, location_id) if location_id else None


def _npcs_at(session, location_id: str | None) -> list:
    from src.models import NPC

    if not location_id:
        return []
    return session.query(NPC).filter(
        NPC.current_location_id == location_id,
        NPC.status == "alive",
    ).all()


def _npcs_at_location(session, location) -> list:
    return _npcs_at(session, getattr(location, "id", None))


def _room_is_fine(location) -> bool:
    """A room that notices what you are wearing."""
    if location is None:
        return False
    code = normalize_dress_code(getattr(location, "dress_code", None))
    if code and code["min_style"] and \
            STYLE_RANK[code["min_style"]] >= STYLE_RANK[FINE_ROOM_MIN_STYLE]:
        return True
    tags = {str(t).strip().lower() for t in (getattr(location, "atmosphere_tags", None) or [])}
    return bool(tags & FINE_ROOM_TAGS)


def _colours_match(location, npcs, uniform: str) -> bool:
    if location is not None and getattr(location, "controlling_faction_id", None) == uniform:
        return True
    return any(getattr(n, "faction_id", None) == uniform for n in npcs)


def situational_factor(session, player, skill: str, *, profile: dict | None = None,
                       location=None, npcs_present=None) -> dict | None:
    """{"factor", "effect", "because"} or None.

    Evaluated in table order; the FIRST match wins and at most one factor ever
    applies. Disadvantages precede the uniform advantage so a filthy or hooded
    impostor gets no free help. Runs only inside `roll_check` — never on a
    per-turn path.
    """
    skill = (skill or "").strip().lower()
    if skill not in prog.SKILLS:
        return None
    profile = profile if profile is not None else perception_profile(player)

    cache: dict[str, Any] = {}

    def _location():
        if location is not None:
            return location
        if "loc" not in cache:
            cache["loc"] = _player_location(session, player)
        return cache["loc"]

    def _npcs():
        if npcs_present is not None:
            return list(npcs_present)
        if "npcs" not in cache:
            cache["npcs"] = _npcs_at_location(session, _location())
        return cache["npcs"]

    # 1. dark_or_weather — stealth only
    if skill == "stealth":
        return _factor("dark_or_weather") if _dark_or_weather(session) else None

    if skill == "persuasion":
        # 2. filthy_in_fine_company
        if profile.get("style") in DRESSED_DOWN_STYLES and _room_is_fine(_location()):
            return _factor("filthy_in_fine_company")
        # 3. hooded_and_watched
        if profile.get("concealing") and _npcs():
            return _factor("hooded_and_watched")

    # 4. wearing_their_colours — persuasion and cunning
    if skill in ("persuasion", "cunning"):
        uniform = profile.get("uniform_faction_id")
        if uniform and _colours_match(_location(), _npcs(), uniform):
            return _factor("wearing_their_colours")

    return None


def combine_modifier(dm_modifier: str, factor_effect: str | None) -> str:
    """The DM's word and the world's word, resolved. Never stacks: two
    advantages are still one advantage, and opposites cancel to none."""
    if not factor_effect:
        return dm_modifier
    if dm_modifier == "none":
        return factor_effect
    if dm_modifier == factor_effect:
        return dm_modifier
    return "none"


# ---------------------------------------------------------------------------
# DM context — the APPEARANCE lines
# ---------------------------------------------------------------------------


def _name_list(names: list[str]) -> str:
    """'A' / 'A and B' / 'A, B and others'."""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]}, {names[1]} and others"


def appearance_lines(player_id: str) -> list[str]:
    """The 1-3 context lines describing how the player reads right now.

    Line 1 is always present. Lines 2-3 fire only when there is a live social
    risk in the room, so a plainly-dressed player in an empty street costs one
    line and nothing else. Never raises: a broken appearance must not cost a
    turn.
    """
    try:
        from src.models import NPCRelationship, Player, get_session

        with get_session() as session:
            player = session.get(Player, player_id)
            if not player:
                return []

            profile = profile_for(session, player)
            lines = [f"- Player reads as: {profile['read_as']}"]

            uniform = profile.get("uniform_faction_id")
            faction_name = profile.get("uniform_faction_name")
            concealing = bool(profile.get("concealing"))
            if not uniform and not concealing:
                return lines

            # The location ROW is never needed here — only its id — so the
            # per-turn budget stays at five reads in the worst case
            npcs = _npcs_at(session, player.current_location_id)

            # Risk line U / U-lite — the uniform makes a claim
            if uniform and faction_name:
                wearing = [n.name for n in npcs if getattr(n, "faction_id", None) == uniform]
                if wearing:
                    lines.append(
                        f"- They are wearing {faction_name} colours and {faction_name} "
                        f"are standing here ({', '.join(wearing[:2])}). Strangers will "
                        f"treat them as {faction_name}. If someone with standing to "
                        "know decides the coat is not theirs, that is a crime, not a "
                        "misunderstanding."
                    )
                else:
                    lines.append(
                        f"- They are wearing {faction_name} colours: strangers here "
                        f"will assume they are {faction_name}, and act accordingly."
                    )

            # Risk line C — a covered face among people who have met them
            if concealing and npcs:
                # One batched read for every relationship in the room, and the
                # familiarity is then computed from the rows we already hold
                rows = {
                    r.npc_id: r for r in session.query(NPCRelationship).filter(
                        NPCRelationship.player_id == player_id,
                        NPCRelationship.npc_id.in_([n.id for n in npcs]),
                    ).all()
                }
                met = [n for n in npcs if n.id in rows]
                if met:
                    day = _current_day(session)
                    unresolved = [n for n in met
                                  if recalled_recognition(player_id, n.id, day) is None]
                    placed = [n for n in met
                              if recalled_recognition(player_id, n.id, day) is True]
                    if unresolved:
                        first = unresolved[0]
                        difficulty = RECOGNITION_DIFFICULTY[
                            familiarity_of(rows[first.id])
                        ]
                        names = _name_list([n.name for n in unresolved])
                        verb = "has" if len(unresolved) == 1 else "have"
                        lines.append(
                            f"- Their face is covered. {names} {verb} met them before "
                            f"and may see past it. If {first.name} looks at them "
                            "closely — if they speak to the player, or the player "
                            "speaks to them — call roll_check(player_id, "
                            f'"{RECOGNITION_SKILL}", "{difficulty}", '
                            f'about_npc_id="{first.id}") BEFORE deciding whether the '
                            "player gets named."
                        )
                    elif placed:
                        names = _name_list([n.name for n in placed])
                        verb = "has" if len(placed) == 1 else "have"
                        lines.append(
                            f"- Their face is covered, and {names} {verb} already "
                            "placed them today. They know exactly who is under the "
                            "hood."
                        )

            return lines
    except Exception as e:
        logger.debug(f"Appearance lines unavailable: {e}")
        return []

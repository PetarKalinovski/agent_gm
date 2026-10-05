"""Command lines: the DM changes the world by writing `@verb …` lines.

The DM used to change the world through ~50 tools, and every tool call cost
a full model round-trip — four to eight of them in sequence on an ordinary
turn (measured 2026-10-04: 38-85 s a turn). Now the DM writes one reply, and
anything mechanical in it is a line of its own:

    @give Gate Pass | kind: key | about: A brass-sealed pass for the north gate.
    @trust Coldiron +5
    @move North Gate | through

`src/web/dialogue_markup.py` hides those lines from the player as they
stream; `CommandRunner.run` parses each one and applies it through the SAME
engine functions the tools were (src/tools/world_write/*, src/services/*),
so every rule the engine enforces — gated routes, trust thresholds, catalog
prices, cinematic rarity, duplicate guards — still holds. The DM never sees
a result mid-reply: what failed (and the few successes worth knowing) comes
back next turn through `ledger()`.

Grammar, one line: `@verb main | key: value | bare extra …`
  * the verb is case-insensitive and "-" == "_" (quest-start == quest_start);
  * segments split on "|"; `key: value` (key = one word) is an option, a key
    may repeat (step:, reward:), and a key the verb does not know is put
    back as a bare extra — so "Fire: the warehouse burned" stays prose;
  * every name may be an id or a name. Resolution is fuzzy but never a
    guess: an ambiguous or missing name FAILS the command with a sentence
    the DM can act on.
"""

from __future__ import annotations

import difflib
import importlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from src.models import (
    NPC,
    Connection,
    Faction,
    Location,
    LocationType,
    Player,
    Quest,
    QuestStatus,
    WorldClock,
    get_session,
)
from src.services import progression as prog
from src.services.cinematics import WEATHER_LABELS
from src.services.combat import ARCHETYPES, DANGER_TIERS
from src.services.perception import STYLES
from src.services.rest import rest_here, rest_now
from src.services.scene_objects import normalize_objects, resolve_interaction
from src.tools.world_write.cinematics import play_cinematic
from src.tools.world_write.combat import start_combat
from src.tools.world_write.creation import _same_name, create_location, create_npc
from src.tools.world_write.dm_state import schedule_world_event, update_dm_state
from src.tools.world_write.events import create_event
from src.tools.world_write.items import _validate_inventory, adjust_currency, transfer_item, use_item
from src.tools.world_write.npcs import (
    kill_npc,
    move_npc,
    reveal_secret,
    update_npc,
    update_npc_mood,
    update_npc_relationship,
)
from src.tools.world_write.party import join_party, leave_party
from src.tools.world_write.player import (
    add_to_inventory,
    move_player,
    regrade_inventory_item,
    remove_from_inventory,
    update_player_health,
    update_player_reputation,
)
from src.tools.world_write.progression import stock_merchant
from src.tools.world_write.quests import activate_quest, create_quest, update_quest_status
from src.tools.world_write.time import advance_time

logger = logging.getLogger(__name__)

# Successful runs of these change what the HUD shows; the server pushes a
# fresh state snapshot after each (it diffs, so a generous set costs nothing)
STATE_TOOLS = frozenset({
    "move_player", "advance_time", "rest", "update_player_health", "adjust_currency",
    "add_to_inventory", "remove_from_inventory", "use_item", "transfer_item",
    "stock_merchant", "update_player_reputation", "update_npc_relationship",
    "join_party", "leave_party", "start_combat", "kill_npc", "move_npc",
    "create_npc", "create_location", "activate_quest", "update_quest_status",
    "create_quest", "update_quest_objectives", "update_dm_state", "play_cinematic", "interact",
})

HEALTH_LABELS = ("healthy", "winded", "hurt", "badly_hurt", "critical", "dead")
TENSION_LABELS = ("low", "rising", "high", "climax", "falling")
GIVE_KINDS = ("weapon", "armor", "trinket", "potion", "key", "clue", "misc")
QUEST_SCALES = ("errand", "job", "mission", "saga")
STEP_KINDS = ("talk_to", "go_to", "obtain", "kill", "deliver", "custom")
MAX_MINUTES = 14 * 24 * 60
MAX_QUANTITY = 99
_REFUSAL_KEYS = ("error", "blocked", "refused", "rejected")


# ----------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------

@dataclass
class CommandResult:
    """One command line, applied (or not).

    ok=False -> `message` says why, in one sentence the DM can act on next
    turn. ok=True -> `message` is non-empty only when the engine's answer is
    worth telling the DM. `result` is the engine function's raw dict, so the
    server can lift SSE events out of it ("event", "cinematic",
    "progression", "objectives_done"…).
    """

    line: str
    verb: str
    ok: bool
    tool: str | None
    result: dict = field(default_factory=dict)
    message: str = ""


class CommandError(Exception):
    """A command the engine never saw: bad arguments or a name that did not resolve."""


@dataclass
class _Outcome:
    result: dict
    message: str = ""
    ok: bool | None = None          # None -> decided from the result's refusal keys


@dataclass
class ParsedCommand:
    verb: str                                   # canonical: lowercase, "_" -> "-"
    main: str                                   # the free text right after the verb
    options: dict[str, list[str]]               # known keys only, repeats kept in order
    extras: list[str]                           # bare segments (and unknown "key: value")
    segments: list[str]                         # every segment after main, raw, in order

    def opt(self, key: str) -> str | None:
        values = self.options.get(key)
        return values[0] if values else None

    def opts(self, key: str) -> list[str]:
        return list(self.options.get(key) or [])

    def flag(self, word: str) -> bool:
        """A bare `| through` (or `| through: yes`)."""
        if any(e.strip().lower() == word for e in self.extras):
            return True
        value = self.opt(word)
        return value is not None and _yes(value, default=True)

    def free_extras(self, *flags: str) -> list[str]:
        return [e for e in self.extras if e.strip().lower() not in flags]

    def rest_text(self) -> str:
        """Everything after the first "|", verbatim — for free-text verbs."""
        return " | ".join(self.segments).strip()


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------

_VERB_RE = re.compile(r"^@\s*([A-Za-z][\w-]*)(.*)$", re.S)
_KEY_RE = re.compile(r"^([A-Za-z][\w-]*)\s*:\s*(.*)$", re.S)
# The model sometimes lists its commands as bullets or code; the mark is not
# part of the command (kept in step with src/web/dialogue_markup.py)
_COMMAND_LEAD_RE = re.compile(r"^\s*(?:[-*•]\s+)?`?\s*(?=@)")


def is_command_line(line: str) -> bool:
    """First non-space character is "@" (a leading code backtick or list
    bullet is tolerated)."""
    return bool(_COMMAND_LEAD_RE.match(line or ""))


def canonical_verb(word: str) -> str:
    return (word or "").strip().lower().replace("_", "-")


def _split_line(line: str) -> tuple[str, str, list[str]] | None:
    text = _COMMAND_LEAD_RE.sub("", line or "", count=1).strip().strip("`").strip()
    m = _VERB_RE.match(text)
    if not m:
        return None
    parts = [p.strip() for p in m.group(2).split("|")]
    main = parts[0].lstrip(":").strip() if parts else ""
    return canonical_verb(m.group(1)), main, [p for p in parts[1:] if p]


def parse_command(line: str, known_keys: frozenset[str] | set[str] | None = None) -> ParsedCommand | None:
    """The line's parts, or None when it is not a command line.

    `known_keys` limits which `key: value` segments count as options (the
    rest become bare extras, verbatim); None accepts every one-word key.
    """
    split = _split_line(line)
    if split is None:
        return None
    verb, main, segments = split
    options: dict[str, list[str]] = {}
    extras: list[str] = []
    for seg in segments:
        m = _KEY_RE.match(seg)
        key = canonical_verb(m.group(1)) if m else None
        if m and (known_keys is None or key in known_keys):
            options.setdefault(key, []).append(m.group(2).strip())
        else:
            extras.append(seg)
    return ParsedCommand(verb=verb, main=main, options=options, extras=extras, segments=segments)


_MINUS = "-−–"
_SIGNED_RE = re.compile(r"(?<![\w.])([+\-−–]?)\s?(\d+)(?![\w.])")
_THOUSANDS_RE = re.compile(r"(?<=\d)[,_](?=\d{3}(?!\d))")
# The number must stand on its own: after a space, a comma, a "#", inside a
# short "[1]", or as an explicit sign right after a letter ("Vell+5"). Never
# the tail of a bracketed id — "[…-0123456789a7]" ends in a digit 62% of the
# time, and reading that 7 as a trust change is a silent wrong write.
_TRAILING_NUMBER_RE = re.compile(
    r"^(.*?)(?:"
    r"(?:(?<=[\s,])|^)\[\s*([+\-−–]?)\s?(\d{1,6})\s*\]"
    r"|(?:(?<=[\s,])|^)#?([+\-−–]?)\s?(\d+)"
    r"|(?<=[^\W\d_])([+\-−–])\s?(\d{1,4})"        # short: a bare id's "d-123456789012" is not a delta
    r")\s*$",
    re.S,
)


def parse_signed(text: str | None) -> int | None:
    """The first signed whole number in the text: "+30", "-12 coin", "30",
    "+1,000"."""
    m = _SIGNED_RE.search(_THOUSANDS_RE.sub("", text or ""))
    if not m:
        return None
    value = int(m.group(2))
    return -value if m.group(1) and m.group(1) in _MINUS else value


def split_trailing_number(text: str | None) -> tuple[str, int | None]:
    """"Harbor Watch -10" -> ("Harbor Watch", -10); "Coldiron [1]" -> ("Coldiron", 1);
    "Coldiron [<id>]" -> ("Coldiron [<id>]", None)."""
    s = (text or "").strip()
    m = _TRAILING_NUMBER_RE.match(s)
    if not m or not m.group(1).strip():
        return s, None
    sign, digits = next((m.group(i), m.group(i + 1)) for i in (2, 4, 6) if m.group(i + 1) is not None)
    value = int(digits)
    return m.group(1).strip().rstrip(",").strip(), (-value if sign and sign in _MINUS else value)


_QTY_TAIL_RE = re.compile(r"^(.*?)(?:\s+|\s*\(\s*)[x×]\s*(\d+)\s*\)?\s*$", re.I | re.S)
_QTY_HEAD_RE = re.compile(r"^\s*(\d+)\s*[x×]\s+(.+)$", re.I | re.S)


def split_quantity(text: str | None) -> tuple[str, int]:
    """"Torch x3" / "Torch (x3)" / "3x Torch" -> ("Torch", 3); no count -> 1.

    A count of nought is nothing, never one: fleet8-tamsin T9's `@give
    Willow-Bark Draught x0` (the DM cancelling a line) put a second draught
    in the pack — raises CommandError."""
    s = (text or "").strip()
    m = _QTY_TAIL_RE.match(s)
    if m and m.group(1).strip():
        return m.group(1).strip(), _count(int(m.group(2)))
    m = _QTY_HEAD_RE.match(s)
    if m:
        return m.group(2).strip(), _count(int(m.group(1)))
    return s, 1


def _count(n: int) -> int:
    if n < 1:
        # Both fleet x0 lines meant "none" (fleet8-tamsin T9 cancelling a
        # line, fleet7-wenna T3 "given away this turn"): never invite a
        # rewrite that would hand one over after all
        raise CommandError("a count of x0 is nothing — the line did nothing and nothing moved; leave it so. "
                           "Only if one truly changed hands write it with no count (given away by the player: "
                           "@hand <item> | to: <person>)")
    return min(MAX_QUANTITY, n)


_UNIT_MINUTES = {
    "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
    "d": 1440, "day": 1440, "days": 1440,
}
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45, "fifty": 50,
    "sixty": 60, "ninety": 90, "few": 3, "couple": 2,
}


def parse_duration(text: str | None) -> int | None:
    """Minutes, or None. "20m", "2h", "1h30m", "90" (bare = minutes),
    "30 minutes", "an hour", "half an hour", "2 hours and 15 minutes"."""
    s = (text or "").strip().lower()
    if not s or s[0] in _MINUS:
        return None                 # time never runs backwards
    s = re.sub(r"\bhalf (?:an |a )?hour\b", "30m", s)
    s = re.sub(r"\b(?:a )?quarter(?: of an)? hour\b", "15m", s)
    s = re.sub(r"\b(?:an?|one)\s+(?=(?:hour|hr|minute|min|day)\b)", "1 ", s)
    s = re.sub(r"\b(" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")\b",
               lambda m: str(_NUMBER_WORDS[m.group(1)]), s)
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return int(round(float(s)))
    total = 0.0
    found = False
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([a-z]*)", s):
        unit = m.group(2)
        if unit and unit not in _UNIT_MINUTES:
            return None
        # "1h30": a trailing bare number after a unit is minutes
        total += float(m.group(1)) * (_UNIT_MINUTES[unit] if unit else 1)
        found = True
    if not found:
        return None
    return int(round(total))


_DAYPARTS = {
    "dawn": 6, "first light": 6, "sunrise": 6, "morning": 8, "noon": 12, "midday": 12,
    "afternoon": 15, "dusk": 18, "sunset": 18, "evening": 19, "night": 21, "midnight": 0,
}
_CLOCK_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?")
# "3h", "2 days", "90m" — a duration with no "in"; "8pm" and "20:00" are clock times
_HAS_DURATION_UNIT = re.compile(r"^\d+(?:\.\d+)?\s*(?:m|mins?|minutes?|h|hrs?|hours?|d|days?)\b")


def _clock_hour(text: str) -> int | None:
    for word, hour in _DAYPARTS.items():
        if re.search(rf"\b{word}\b", text):
            return hour
    m = _CLOCK_RE.search(text)
    if not m:
        return None
    hour = int(m.group(1))
    if m.group(3) == "pm" and hour < 12:
        hour += 12
    elif m.group(3) == "am" and hour == 12:
        hour = 0
    return hour if 0 <= hour <= 23 else None


def parse_when(text: str | None, day: int, hour: int, minute: int = 0) -> tuple[int, int] | None:
    """(day, hour) for "in 3h", "in 2d", "day 3 20:00", "tomorrow 08:00",
    "tonight" (21:00), "dawn" (the next 06:00), "20:00" (the next one)."""
    s = (text or "").strip().lower()
    if not s:
        return None
    now = (day * 24 + hour) * 60 + (minute or 0)

    def at(minutes_total: int) -> tuple[int, int]:
        return minutes_total // (24 * 60), (minutes_total // 60) % 24

    def next_at(target_hour: int) -> tuple[int, int]:
        today = (day * 24 + target_hour) * 60
        return at(today if today > now else today + 24 * 60)

    if s.startswith("in ") or _HAS_DURATION_UNIT.match(s):
        minutes = parse_duration(s[3:] if s.startswith("in ") else s)
        if minutes and 0 < minutes <= MAX_MINUTES:
            # Events fire on the hour: "in 30m" at 8:10 is 9:00, not the 8:00
            # that has already passed (it would fire before the scene moved)
            slot = (now + minutes) - (now + minutes) % 60
            return at(slot if slot > now else slot + 60)
        return None
    m = re.match(r"^day\s*(\d+)(.*)$", s)
    if m:
        target = int(m.group(1))
        hr = _clock_hour(m.group(2)) if m.group(2).strip() else 8
        if hr is None:
            return None
        result = (target, hr)
        return result if (target * 24 + hr) * 60 > now else None
    if s.startswith("tomorrow"):
        hr = _clock_hour(s[len("tomorrow"):]) if s[len("tomorrow"):].strip() else 8
        return (day + 1, hr) if hr is not None else None
    if s.startswith("tonight"):
        hr = _clock_hour(s[len("tonight"):]) if s[len("tonight"):].strip() else 21
        if hr is None:
            return None
        return (day, hr) if (day * 24 + hr) * 60 > now else at(now + 60)
    hr = _clock_hour(s)
    if hr is not None:
        return next_at(hr)
    return None


def _yes(value: str | None, default: bool) -> bool:
    word = (value or "").strip().lower()
    if word in ("yes", "y", "true", "1", "inside", "in"):
        return True
    if word in ("no", "n", "false", "0", "outside", "out"):
        return False
    return default


def _clip(text: str, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


# Engine messages still written for the tool era, said in commands
_TOOL_TO_COMMAND = (
    (re.compile(r"call(?:ing)? move_player again with requirements_met=True"), "write `@move <place> | through`"),
    (re.compile(r"\bmove_player\b"), "@move"),
    (re.compile(r"\bmove_npc\b"), "@npc-move"),
    (re.compile(r"\bleave_party\b"), "@leave"),
    (re.compile(r"\bjoin_party\b"), "@join"),
    (re.compile(r"\bstart_combat\b"), "@fight"),
    (re.compile(r"\bactivate_quest\b"), "@quest-start"),
    (re.compile(r"\bupdate_quest_status\([^)]*\)|\bupdate_quest_status\b"), "@quest-done"),
    (re.compile(r"\breveal_secret\b"), "@reveal"),
    (re.compile(r"\badd_to_inventory\b"), "@give"),
    (re.compile(r"\badvance_time\b"), "@time"),
    (re.compile(r"\bupdate_player_health\b"), "@health"),
    (re.compile(r"\btransfer_item\(is_purchase=True\)|\btransfer_item\b"), "@buy"),
    (re.compile(r"\bget_npcs_at_location\b"), "the scene"),
    (re.compile(r"\bprompt_creator_agent\b"), "@new-npc"),
)


def _commandize(text: str) -> str:
    for pattern, replacement in _TOOL_TO_COMMAND:
        text = pattern.sub(replacement, text)
    return text


def _refused(result: Any) -> bool:
    return isinstance(result, dict) and any(result.get(k) for k in _REFUSAL_KEYS)


# Bare error codes some engine functions return, as sentences
_ERROR_CODES = {
    "player_not_placed": "the player is not anywhere yet — nothing was made",
    "no_player": "there is no such player",
    "no_npc": "there is no such person",
}


def _engine_reason(result: dict) -> str:
    for key in ("message", "error", "instruction", "reason"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            value = _ERROR_CODES.get(value.strip(), value.strip())
            return _clip(_commandize(value), 260)
    return "the engine refused it"


# ----------------------------------------------------------------------
# Name resolution
# ----------------------------------------------------------------------

_NPC_STOP = {
    "captain", "sergeant", "lord", "lady", "sir", "dame", "master", "mistress", "old",
    "young", "mother", "father", "brother", "sister", "doctor", "dr", "the", "madam",
    "of", "and", "a", "an",
}
_PLACE_STOP = {"the", "a", "an", "of", "ye", "old"}
_PLAIN_STOP = {"the", "a", "an", "of", "some"}
_ID_RE = re.compile(r"\[([0-9a-fA-F][0-9a-fA-F-]{7,35})\]")
_BARE_ID_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F-]{0,27})?$")
_FUZZY_WORD = 0.82


def _words(text: str) -> list[str]:
    s = (text or "").lower().replace("'", "").replace("’", "")
    return re.findall(r"[a-z0-9à-ÿ]+", s)


def _key(text: str, stop: set[str]) -> list[str]:
    return [w for w in _words(text) if w not in stop and len(w) >= 2]


def _clean_label(label: str | None) -> str:
    s = (label or "").strip().strip('"“”`*').strip()
    return s.rstrip(".,;!").strip()


def _match_tier(label: str, rows: list, name_of: Callable[[Any], str],
                stop: set[str]) -> tuple[Any, list]:
    """(the one row the label names, []) or (None, the rows it could mean)."""
    full = " ".join(_words(label))
    if not full or not rows:
        return None, []

    def decide(hits: list) -> tuple[Any, list] | None:
        if len(hits) == 1:
            return hits[0], []
        if hits:
            return None, hits
        return None

    steps: list[Callable[[Any], bool]] = [lambda r: " ".join(_words(name_of(r))) == full]
    key = set(_key(label, stop))
    if key:
        keyed = {id(r): set(_key(name_of(r), stop)) for r in rows}
        steps += [
            lambda r: keyed[id(r)] == key,                         # same words, other titles
            lambda r: key <= keyed[id(r)],                         # "Coldiron" -> "Brann Coldiron"
            lambda r: bool(keyed[id(r)]) and keyed[id(r)] <= key,  # a longer label than the name
            lambda r: all(                                         # a near-spelling of every word
                any(len(w) >= 4 and difflib.SequenceMatcher(None, w, n).ratio() >= _FUZZY_WORD
                    for n in keyed[id(r)])
                for w in key
            ),
        ]
    for step in steps:
        decided = decide([r for r in rows if step(r)])
        if decided is not None:
            return decided
    return None, []


def _walk_tiers(label: str, tiers: list[list], name_of: Callable[[Any], str],
                id_of: Callable[[Any], str], stop: set[str], what: str) -> Any:
    for rows in tiers:
        unique: dict[str, Any] = {}
        for row in rows:
            unique.setdefault(id_of(row), row)
        hit, ambiguous = _match_tier(label, list(unique.values()), name_of, stop)
        if hit is not None:
            return hit
        if ambiguous:
            options = ", ".join(f"{name_of(r)} [{str(id_of(r))[:8]}]" for r in ambiguous[:4])
            raise CommandError(f"'{label}' could be {options} — write the full name or the id")
    raise CommandError(f"no {what} called '{label}'")


def _by_id(session, model, label: str):
    """A row for an exact id, a bracketed id, or an id prefix of 8+ characters."""
    candidates = [m.group(1) for m in _ID_RE.finditer(label)]
    if _BARE_ID_RE.match(label.strip()):
        candidates.append(label.strip())
    for ident in candidates:
        row = session.get(model, ident) or session.get(model, ident.lower())
        if row is not None:
            return row
        rows = session.query(model).filter(model.id.like(f"{ident.lower()}%")).limit(2).all()
        if len(rows) == 1:
            return rows[0]
    return None


def _without_ids(label: str) -> str:
    return _ID_RE.sub("", label).strip()


# ----------------------------------------------------------------------
# The runner
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class _Spec:
    handler: Callable[["CommandRunner", ParsedCommand], Any]
    tool: str | None
    usage: str
    keys: frozenset[str] = frozenset()


# A quest's pay handed over in an earlier reply is not handed over again.
# fleet6-wenna T11-T13: @quest-done paid the Yarrow Tincture, the next reply
# showed it (as reward_unnarrated asked), a handover audit then asked for a
# @give, and T13's `@give Yarrow Tincture` put a second one in the pack. Per
# player, in memory: one runner is one reply, so its serial counts replies
# (a restart only forgets the last two replies' payouts)
RECENT_PAYOUT_REPLIES = 2
_REPLY_SERIAL: dict[str, int] = {}
# player id -> [(reply serial, "item" | "coin", slug or amount, name, quest title, giver id)]
_RECENT_PAYOUTS: dict[str, list[tuple[int, str, Any, str, str, str | None]]] = {}
# Each player's runner of the last reply: a road barred for the night opens
# to a roll won or a bribe paid in this reply or that one (`_won_the_night`)
_LAST_RUNNER: dict[str, "CommandRunner"] = {}


class CommandRunner:
    """Applies one reply's command lines, in order, for one player.

    Remembers what the reply itself created, so `@new-place Cellar` then
    `@move Cellar` (or `@new-npc` then `@fight`) resolves in the same reply.
    `present_ids` are the NPCs the scene brief listed; after a successful
    `@move` the people at the new location count as present instead.
    `after_combat` marks a reply that answers a [COMBAT OUTCOME]: it narrates
    an aftermath and may not start another fight (`answers_combat_outcome`).
    """

    def __init__(self, player_id: str, present_ids: list[str] | None = None,
                 after_combat: bool = False) -> None:
        self.player_id = player_id
        self.present_ids: list[str] = list(present_ids or [])
        self.after_combat = after_combat
        self.serial = _REPLY_SERIAL[player_id] = _REPLY_SERIAL.get(player_id, 0) + 1
        self.results: list[CommandResult] = []
        self.created_npc_ids: list[str] = []
        self.created_place_ids: list[str] = []
        # @boss names an entrance; it plays with the @fight that follows
        self.pending_boss: dict[str, str] = {}
        # Coin THIS reply already moved, so a later line restating it is not
        # paid twice. The DM writes its commands blind; on 2026-10-04 it paid
        # a 60-coin reward twice (@quest-done, then @coin +60 "for" the same
        # promise) and charged an 8-coin bed twice (@coin -8, then @rest,
        # which charges the bed itself: 16 of a 25-coin purse for one night)
        self.paid_coin: set[int] = set()     # rewards @quest-done handed over
        # ...and the rest of what it handed over: fleet4-bram T10 and -wenna
        # T14 followed @quest-done with `@give Wolf-Tooth Charm` and `@rep
        # Lantern Watch +10` (two charms, Watch standing 70 instead of 60)
        self.paid_items: dict[str, str] = {}  # item slug -> the reward's name
        self.paid_rep: dict[str, int] = {}    # faction id -> the reward's delta
        # ...and the giver's thanks: @quest-done raises their trust in code,
        # and 4 of the fleet's 13 @quest-done replies added their own
        # `@trust Coldiron +10/+15` for the same job
        self.paid_trust: set[str] = set()     # npc ids @quest-done already thanked
        # ...and a kindness: a gift raises trust in code (`_gift_trust`), and
        # a @trust for the same gift in the same reply is one kindness, not two
        self.trust_raised: dict[str, str] = {}  # npc id -> "gift" | "trust", what raised it this reply
        self.gift_trust: dict[str, int] = {}    # npc id -> what the gift added (a bigger @trust adds the rest)
        # What @interact put in the pack this reply (fleet4-bram T9: the
        # crate's silver piece, then `@give Greywater Silver Piece` again)
        self.found_items: dict[str, str] = {}  # item slug -> the thing it came out of
        # Coin and goods lines this reply already applied, word for word.
        # fleet8-tamsin T9 (three model calls, a roll between them) wrote
        # `@coin -1 | tossed to Sorrel for his watch tonight` twice and both
        # landed: two coins for the one the player tossed
        self.applied_lines: set[str] = set()
        # Pay up front booked this reply ({"title", "giver_id", "what"}): a
        # @coin -N to that giver in the same reply recast it as a purchase
        # (fleet8-wenna T4; turn_audit.advance_bought_warnings)
        self.advances: list[dict[str, Any]] = []
        # @quest-done paid while its giver was not with the player
        # ({"title", "giver_id"}): the next context says how it reaches them
        # (src/services/settle.py)
        self.paid_away: list[dict[str, Any]] = []
        # What the player typed this turn (the web path sets it): an @interact
        # that takes or searches is theirs to ask for (`_reached_for`). None
        # (the CLI, tools) skips that check
        self.player_input: str | None = None
        # What the engine already did for the player since the last reply (the
        # web path sets it: pending_actions.tool_equivalents) — a draught they
        # drank from the quick slot is not @use'd a second time (`_h_use`)
        self.code_actions: set[str] | None = None
        # ...and which things: the canvas's own lines for what it consumed
        # ("They drank a Willow-Bark Draught…"). None: any canvas use counts
        self.canvas_used: list[str] | None = None
        # One night per reply (a paid room runs @rest; a second would sleep a day)
        self.rested = False
        self.coin_debits: list[int] = []     # @coin -N taken, not yet matched to a bed
        self.bed_charge = 0                  # the bed @rest settled, until a @coin restates it
        self.bed_waived = False              # ...settled by a broken night, which is free
        # The road's own minutes: @move spends them unless the reply already
        # wrote @time (fleet5-bram T7: a narrated 45-minute walk cost the
        # 15-minute turn floor, the same road home cost 45, and the 20:00 howl
        # was told early, then again). A later @time restating the trip is
        # reduced by what the road already took, as a restated bed fee is
        self.time_written = False
        self.road_minutes = 0                # spent by @move, until a @time restates it
        # @trust / @reveal on someone the player has not met used to wait for
        # the end of the reply, because the row they write counted as an
        # introduction (fleet-wenna T17). Since the split (dialogue: who
        # knows whom) they land at once on a stranger's row
        # (`_open_relationship`) and no handler holds a line: this stays
        # empty unless one returns `held_for_intro` again
        self.held_for_intro: dict[int, str] = {}  # index in results -> npc id
        self._settling = False
        # The web path sets this to the turn's name gate: a @journal entry is
        # on the player's screen, so a name nobody said is masked there too
        # (fleet4-wenna T17 journaled "Sorrel Tack" before he gave his
        # surname). Entries wait for the reply's end, when the gate has heard
        # every line of it
        self.journal_mask = None
        self.held_journal: list[str] = []
        self.held_journal_at: list[int] = []     # each held entry's index in results
        # ...and what the engine journals itself (an @interact's words and
        # finds, a clue @give'n): the same mask, but skipped when the journal
        # already says it (memory_files.add_journal_entries)
        self.held_auto_journal: list[str] = []
        # What the dice said this reply (the web path shares its own list as
        # rolls land) and the last reply's runner: a road barred for the night
        # opens to a won roll or a bribe, never to `| through` alone
        # (fleet8-tamsin T13). A reply's runner becomes "the last reply" when
        # its ledger is taken (`ledger`) — never an engine runner made mid-turn
        # (stakes' charged price is no bribe)
        self.roll_outcomes: list[str] = []
        # ...and what each roll was for, when the stream knows it (the web
        # path shares its `rolls`: {outcome, reason}; the CLI knows outcomes)
        self.rolls: list[dict] = []
        self.previous: CommandRunner | None = _LAST_RUNNER.get(player_id)

    # -- public ------------------------------------------------------------

    def run(self, line: str) -> CommandResult:
        """Apply one command line. Never raises."""
        try:
            result = self._run(line)
        except Exception as e:  # the contract: a bad line never costs the turn
            logger.exception(f"command crashed: {line!r}")
            result = CommandResult(line=(line or "").strip(), verb="", ok=False, tool=None,
                                   message=f"the engine could not apply it ({e.__class__.__name__})")
        if isinstance(result.result, dict) and result.result.get("held_for_intro"):
            self.held_for_intro[len(self.results)] = result.result["held_for_intro"]
        self.results.append(result)
        logger.debug(f"command {'ok' if result.ok else 'failed'}: {result.line}"
                     + (f" — {result.message}" if result.message else ""))
        return result

    def settle_introductions(self) -> None:
        """Apply the held @trust/@reveal lines whose person was introduced
        this reply (call after the turn's dialogue is recorded); the rest are
        reported as not applied. Idempotent."""
        # The engine's words first, then the DM's: a @journal restating what
        # the engine just wrote is skipped (fleet6-tamsin T7/T19: the notice
        # and the satchel letter went in twice each, the engine's exact words
        # and the DM's paraphrase; fleet6-bram T12 kept only the paraphrase)
        auto, self.held_auto_journal = self.held_auto_journal, []
        masked_auto: list[str] = []
        if auto:
            try:
                masked_auto = [self.journal_mask(t) for t in auto] if self.journal_mask else auto
                _memory().add_journal_entries(self.player_id, masked_auto)
            except Exception as e:      # never journal a name the mask could not hide
                logger.warning(f"engine journal entries not kept: {e}")
                masked_auto = []
        entries, self.held_journal = self.held_journal, []
        at, self.held_journal_at = self.held_journal_at, []
        for i, text in enumerate(entries):
            try:
                masked = self.journal_mask(text) if self.journal_mask else text
            except Exception as e:
                logger.warning(f"journal entry not masked: {e}")
                masked = text
            try:
                _write_journal(self, masked, at[i] if i < len(at) else None, masked_auto)
            except CommandError as e:
                logger.warning(f"journal entry not kept: {e}")
        held, self.held_for_intro = self.held_for_intro, {}
        for index, npc_id in held.items():
            original = self.results[index]
            with get_session() as session:
                from src.services.dialogue import player_has_met
                met = player_has_met(session, self.player_id, npc_id)
            if met:
                self._settling = True
                try:
                    self.results[index] = self._run(original.line)
                except Exception as e:
                    logger.exception(f"held command crashed: {original.line!r}")
                    self.results[index] = CommandResult(
                        line=original.line, verb=original.verb, ok=False, tool=original.tool,
                        message=f"the engine could not apply it ({e.__class__.__name__})")
                finally:
                    self._settling = False
            else:
                self.results[index] = CommandResult(
                    line=original.line, verb=original.verb, ok=False, tool=original.tool,
                    result={"refused": "not_introduced"},
                    message=("not applied — they never gave their name this reply, so they are still a "
                             "stranger. Once a speaker line under their name says it, write it again"))

    def ledger(self) -> list[str]:
        """One line per failed or notable command, for the DM's next turn."""
        self.settle_introductions()
        # The reply is done: the next one reads it, one reply back only
        self.previous = None
        _LAST_RUNNER[self.player_id] = self
        lines = []
        for r in self.results:
            if not r.message:
                continue
            head = _clip(r.line, 90)
            lines.append(f"{head}: {r.message}" if r.ok else f"{head}: FAILED — {r.message}")
        for name in self.pending_boss.values():
            lines.append(f"@boss {name}: saved — the entrance plays when you @fight them, not before")
        return lines

    # -- dispatch ----------------------------------------------------------

    def _run(self, line: str) -> CommandResult:
        text = (line or "").strip()
        split = _split_line(text) if is_command_line(text) else None
        if split is None:
            return CommandResult(line=text, verb="", ok=False, tool=None,
                                 message="not a command line (it must start with @verb)")
        verb = split[0]
        canonical = verb if verb in _REGISTRY else _ALIASES.get(verb)
        if canonical is None:
            known = ", ".join(f"@{v}" for v in _REGISTRY)
            return CommandResult(line=text, verb=verb, ok=False, tool=None,
                                 message=f"unknown command @{verb} (known: {known})")
        spec = _REGISTRY[canonical]
        # The same coin or goods line twice in one reply is one payment
        # (fleet8-tamsin T9: a line written again after a roll_check call)
        repeat = " ".join(text.split()) if canonical in _ONCE_PER_REPLY else None
        if repeat is not None and repeat in self.applied_lines:
            return CommandResult(line=text, verb=canonical, ok=False, tool=spec.tool,
                                 result={"skipped": "already_applied"},
                                 message=("already applied this reply — the same line twice is one change, not "
                                          "two; nothing more was moved"))
        cmd = parse_command(text, spec.keys)
        cmd.verb = canonical
        ignored = _ignored_note(canonical, spec, cmd)
        try:
            outcome = spec.handler(self, cmd)
        except CommandError as e:
            return CommandResult(line=text, verb=canonical, ok=False, tool=spec.tool,
                                 message="; ".join(m for m in (str(e), ignored) if m))
        if not isinstance(outcome, _Outcome):
            outcome = _Outcome(result=outcome if isinstance(outcome, dict) else {"value": outcome})
        ok = outcome.ok if outcome.ok is not None else not _refused(outcome.result)
        message = outcome.message
        if not ok and outcome.ok is None and not message:
            message = _engine_reason(outcome.result)
        if ok and repeat is not None:
            self.applied_lines.add(repeat)
        return CommandResult(line=text, verb=canonical, ok=ok, tool=spec.tool,
                             result=outcome.result, message="; ".join(m for m in (message, ignored) if m))

    # -- world reads (short sessions; primitives out) ----------------------

    def _player_row(self, session) -> Player:
        player = session.get(Player, self.player_id)
        if player is None:
            raise CommandError("no such player")
        return player

    def current_location_id(self) -> str | None:
        with get_session() as session:
            return self._player_row(session).current_location_id

    def here_and_above(self) -> list[str]:
        """The player's location id, then every ancestor's, nearest first."""
        with get_session() as session:
            loc_id = self._player_row(session).current_location_id
            chain: list[str] = []
            while loc_id and loc_id not in chain:
                chain.append(loc_id)
                row = session.get(Location, loc_id)
                loc_id = row.parent_id if row is not None else None
        return chain

    def npc(self, label: str | None) -> tuple[str, str]:
        """(id, name): present, then party, then made this reply, then anyone alive."""
        label = _clean_label(label)
        if not label:
            raise CommandError("name the person")
        with get_session() as session:
            row = _by_id(session, NPC, label)
            if row is not None:
                return row.id, row.name
            player = self._player_row(session)
            alive = NPC.status == "alive"
            present = [n for n in session.query(NPC).filter(NPC.id.in_(self.present_ids)).all()
                       if n.status == "alive"] if self.present_ids else []
            if player.current_location_id:
                present += session.query(NPC).filter(
                    NPC.current_location_id == player.current_location_id, alive).all()
            party_ids = [i for i in (player.party_members or []) if isinstance(i, str)]
            party = session.query(NPC).filter(NPC.id.in_(party_ids), alive).all() if party_ids else []
            made = session.query(NPC).filter(NPC.id.in_(self.created_npc_ids)).all() \
                if self.created_npc_ids else []
            everyone = session.query(NPC).filter(alive).all()
            # The dead and the missing last: @note and @npc may still name them
            gone = session.query(NPC).filter(NPC.status != "alive").all()
            row = _walk_tiers(_without_ids(label), [present, party, made, everyone, gone],
                              lambda n: n.name, lambda n: n.id, _NPC_STOP, "person")
            return row.id, row.name

    def place(self, label: str | None) -> tuple[str, str]:
        """(id, name): the ways out of here first, then made this reply, then anywhere."""
        label = _clean_label(label)
        if not label:
            raise CommandError("name the place")
        with get_session() as session:
            row = _by_id(session, Location, label)
            if row is not None:
                return row.id, row.name
            player = self._player_row(session)
            near: list[Location] = []
            here = session.get(Location, player.current_location_id) if player.current_location_id else None
            if here is not None:
                ids = {here.id}
                for conn in session.query(Connection).filter(
                    (Connection.from_location_id == here.id) | (Connection.to_location_id == here.id)
                ).all():
                    ids.add(conn.to_location_id if conn.from_location_id == here.id else conn.from_location_id)
                ids.update(c.id for c in session.query(Location).filter(Location.parent_id == here.id).all())
                if here.parent_id:
                    ids.add(here.parent_id)
                near = session.query(Location).filter(Location.id.in_(ids)).all()
            made = session.query(Location).filter(Location.id.in_(self.created_place_ids)).all() \
                if self.created_place_ids else []
            everywhere = session.query(Location).all()
            row = _walk_tiers(_without_ids(label), [near, made, everywhere],
                              lambda p: p.name, lambda p: p.id, _PLACE_STOP, "place")
            return row.id, row.name

    def quest(self, label: str | None, prefer: tuple[str, ...] = ()) -> tuple[str, str, str]:
        """(id, title, status); quests in a `prefer`red status win a tie."""
        label = _clean_label(label)
        if not label:
            raise CommandError("name the quest")
        with get_session() as session:
            row = _by_id(session, Quest, label)
            if row is None:
                rows = session.query(Quest).all()
                tiers = ([[q for q in rows if q.status in prefer]] if prefer else []) + [rows]
                row = _walk_tiers(_without_ids(label), tiers, lambda q: q.title, lambda q: q.id,
                                  _PLAIN_STOP, "quest")
            return row.id, row.title, row.status

    def faction(self, label: str | None) -> tuple[str, str]:
        label = _clean_label(label)
        if not label:
            raise CommandError("name the faction")
        with get_session() as session:
            row = _by_id(session, Faction, label)
            if row is None:
                row = _walk_tiers(_without_ids(label), [session.query(Faction).all()],
                                  lambda f: f.name, lambda f: f.id, _PLAIN_STOP, "faction")
            return row.id, row.name

    def inventory_item(self, label: str | None) -> dict[str, Any]:
        """The player's inventory entry (normalized dict) the label names."""
        label = _clean_label(label)
        if not label:
            raise CommandError("name the item")
        with get_session() as session:
            entries = _validate_inventory(self._player_row(session).inventory)
        return self._item_in(label, entries, "item in their pack")

    def npc_item(self, npc_id: str, label: str | None) -> dict[str, Any]:
        label = _clean_label(label)
        if not label:
            raise CommandError("name the item")
        with get_session() as session:
            npc = session.get(NPC, npc_id)
            entries = _validate_inventory(npc.inventory_notable if npc else [])
            owner = npc.name if npc else "they"
        return self._item_in(label, entries, f"item {owner} has")

    @staticmethod
    def _item_in(label: str, entries: list[dict[str, Any]], what: str) -> dict[str, Any]:
        wanted = prog._slug(label)
        for entry in entries:
            if entry.get("id") == label or entry.get("id") == wanted:
                return entry
        return _walk_tiers(label, [entries], lambda e: str(e.get("name") or e.get("id") or ""),
                           lambda e: str(e.get("id") or e.get("name")), _PLAIN_STOP, what)


# ----------------------------------------------------------------------
# Handlers — one per verb, each a thin adapter onto an engine function
# ----------------------------------------------------------------------

def _first(cmd: ParsedCommand, key: str, *flags: str) -> str:
    """An option's value, else the first bare extra (minus flag words)."""
    value = cmd.opt(key)
    if value:
        return value
    extras = cmd.free_extras(*flags)
    return extras[0] if extras else ""


def _split_on(text: str, word: str) -> tuple[str, str] | None:
    """"Gate Pass to Coldiron" -> ("Gate Pass", "Coldiron") on the LAST " to "."""
    m = re.match(rf"^(.+)\s+{word}\s+(.+)$", text or "", re.I)
    return (m.group(1).strip(), m.group(2).strip()) if m else None


# -- moving & time ---------------------------------------------------------

def _relocate(npc_id: str, place_id: str) -> None:
    with get_session() as session:
        npc = session.get(NPC, npc_id)
        if npc is not None:
            npc.current_location_id = place_id
            npc.home_location_id = npc.home_location_id or place_id
            session.commit()


# Location types a person stands in — a street, a landmark, a building, a
# room — even with rooms under them (a square with a cellar off it, a temple
# with its halls). Anything bigger is a map of such places
_SCENE_TYPES = frozenset({LocationType.DISTRICT, LocationType.POI, LocationType.BUILDING,
                          LocationType.INTERIOR, LocationType.ROOM})


def _whole_noun(kind: LocationType) -> str:
    word = kind.value
    if word == "settlement":
        return "town"
    return "region" if word == "root" or word.startswith("region") else word


def whole_town(place_id: str) -> str | None:
    """Why nobody can stand in this place, or None when it is a scene.

    A town, city or region with places under it is a map of places, not one.
    2026-10-04: `@move Emberfall | through` stood the player in the whole
    town while the prose had her at the garrison door; the next @new-npc was
    born there and a quest's "Go to Emberfall" ticked for it.

    Not `is_map_container`: generated worlds set it on temples, castles and
    shops people stand in, and on towns the world itself plays as a scene.
    So a place someone lives in, or the player has already stood in, is a
    scene whatever its type (asoiaf's Ashford Town, where the player starts;
    young_justice's The Cave, home to fifteen).

    Public so the scene brief can keep such a place off "Ways out".
    """
    with get_session() as session:
        place = session.get(Location, place_id)
        if place is None or place.type in _SCENE_TYPES or place.visited:
            return None
        children = session.query(Location).filter(Location.parent_id == place.id).all()
        if not children or session.query(NPC.id).filter(NPC.home_location_id == place.id).first():
            return None
        # The DM's own context, not the player's: an undiscovered room is still a place to name
        inside = [c.name for c in sorted(children, key=lambda c: not c.discovered)][:4]
        name, noun = place.name, _whole_noun(place.type)
    return f"{name} is the whole {noun} — name a place in it ({', '.join(inside)}), or @new-place one"


def _scene(place_id: str) -> None:
    """Fail the command when nobody can stand in this place."""
    refusal = whole_town(place_id)
    if refusal:
        raise CommandError(refusal)


def _move_target(r: CommandRunner, label: str) -> tuple[str, str] | _Outcome:
    """A place, or the place a named person or thing is at.

    The DM writes "@move Captain Brann Coldiron" or "@move Notice board" when
    the prose walks the player over to someone (autoplay 2026-10-04). Someone
    or something HERE needs no move at all; someone elsewhere is a move to
    wherever they are."""
    try:
        return r.place(label)
    except CommandError as place_error:
        here = r.current_location_id()
        try:
            npc_id, npc_name = r.npc(label)
        except CommandError:
            npc_id = None
        if npc_id:
            with get_session() as session:
                npc = session.get(NPC, npc_id)
                where = npc.current_location_id if npc is not None else None
                where_name = npc.current_location.name if npc is not None and npc.current_location else None
            if where == here:
                return _Outcome({"success": True, "already_here": True, "npc": npc_name},
                                message=f"{npc_name} is right here — walking up to someone needs no @move")
            if where and where_name:
                return where, where_name
        if here:
            from src.services.scene_objects import public_objects

            with get_session() as session:
                loc = session.get(Location, here)
                labels = [o.get("label", "") for o in public_objects(loc.objects if loc else [])]
            wanted = _key_words(label)
            if wanted and any(wanted <= _key_words(lbl) or _key_words(lbl) <= wanted for lbl in labels if lbl):
                return _Outcome({"success": True, "already_here": True},
                                message=f"'{label}' is here in the scene — no @move needed (@interact to use it)")
        raise place_error


def _key_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", (text or "").lower()) if w not in {"the", "a", "an", "of"}}


def _road_minutes(origin: str | None, destination: str) -> int:
    """The roads' travel time between two places, in minutes. A direct
    Connection is the one `check_route` walks (either way when it runs both
    ways), what a canvas door charges. Otherwise the quickest way over the
    authored roads, where an end with no road of its own steps through its
    doorway (HIERARCHY_HOP_HOURS) to the place it sits in or a room inside it:
    fleet-wenna T4 went from Cooper's Rooms, a room the DM made off the
    square, to Wolf's Hollow — out the door and up the 45-minute north road,
    a journey the DM is told never to @time. 0 for a hierarchy hop or a route
    nothing models: no road, no road time."""
    if not origin or origin == destination:
        return 0
    import heapq

    from src.services.travel import HIERARCHY_HOP_HOURS

    with get_session() as session:
        roads: dict[str, list[tuple[str, float]]] = {}
        on_a_road: set[str] = set()
        for conn in session.query(Connection).all():
            hours = float(conn.travel_time_hours or 0)
            roads.setdefault(conn.from_location_id, []).append((conn.to_location_id, hours))
            if conn.bidirectional:
                roads.setdefault(conn.to_location_id, []).append((conn.from_location_id, hours))
            on_a_road.update((conn.from_location_id, conn.to_location_id))
        direct = [hours for there, hours in roads.get(origin, []) if there == destination]
        if direct:
            return int(round(min(direct) * 60))

        def doorways(place_id: str) -> list[str]:
            place = session.get(Location, place_id)
            kin = [place.parent_id] if place is not None and place.parent_id else []
            return kin + [row[0] for row in session.query(Location.id).filter(Location.parent_id == place_id).all()]

        # Only a road-less END gets a door: two roaded places never shortcut
        # through the town node they both sit in (the square and the Hollow
        # are both Emberfall's; the road between them is still 45 minutes)
        if origin not in on_a_road:
            roads.setdefault(origin, []).extend((kin, HIERARCHY_HOP_HOURS) for kin in doorways(origin))
        if destination not in on_a_road:
            for kin in doorways(destination):
                roads.setdefault(kin, []).append((destination, HIERARCHY_HOP_HOURS))
    best = {origin: 0.0}
    queue = [(0.0, origin)]
    while queue:
        hours, here = heapq.heappop(queue)
        if here == destination:
            return int(round(hours * 60))
        if hours > best.get(here, float("inf")):
            continue
        for there, cost in roads.get(here, ()):
            if hours + cost < best.get(there, float("inf")):
                best[there] = hours + cost
                heapq.heappush(queue, (hours + cost, there))
    return 0


def _spend_road(r: CommandRunner, origin: str | None, place_id: str, place_name: str) -> int:
    """After a move: the road's time passes, as it does for a canvas door
    (travel.travel_now). Only what the turn floor would not cover anyway —
    the square-to-inn step (6 min) still costs the floor's 15, never less —
    and never when the reply already wrote @time (it told the journey)."""
    from src.services.world_tick import MIN_TURN_HOURS

    minutes = _road_minutes(origin, place_id)
    if r.time_written or minutes <= MIN_TURN_HOURS * 60:
        return 0
    clock = advance_time(hours=minutes / 60.0, reason=f"the road to {place_name}")
    if isinstance(clock, dict) and clock.get("error"):
        return 0
    r.road_minutes += minutes
    return minutes


def _h_move(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    target = _move_target(r, cmd.main)
    if isinstance(target, _Outcome):
        return target
    place_id, place_name = target
    _scene(place_id)
    origin = r.current_location_id()
    if place_id == origin:
        # Not a failure, but the DM's map was wrong: fleet5-tamsin T2/T8
        # wrote `@move Market Square` from the square, heard nothing back,
        # and went on narrating a walk that never happened
        result = {"success": True, "destination": place_name, "already_here": True}
        # ...but standing here still ticks a "go to" aimed here, as the old
        # same-place move_player did (a quest taken up where its place is)
        from src.services import objectives as objectives_mod

        ticked = objectives_mod.note_arrival(r.player_id, place_id)
        if ticked:
            result["objectives_done"] = objectives_mod.done_lines(ticked)
            return _Outcome(result, message=f"already there — they were in {place_name}; nothing moved")
        # A ✓ that moved nothing read as a move: fleet6-tamsin T16 answered
        # speaker_not_here with `@move The Drowned Lantern` (the PEOPLE had
        # moved), fleet6-wenna T6/T9/T11 toured a stable yard and a gatehouse
        # on three no-op moves — so it fails, and says what would have worked
        return _Outcome(result, ok=False, message=(
            f"already here — they are in {place_name}; nothing moved. If someone else moved, "
            f"@npc-move <name> | {place_name}; for a new spot inside, @new-place <spot> then @move <spot>"))
    # A road barred for the night opens to a won roll or a bribe, never to
    # `| through` on the pass alone (fleet8-tamsin T11-T13)
    night = _barred_tonight(r, origin, place_id)
    if night is not None and cmd.flag("through") and not _won_the_night(r, origin, place_id):
        return _Outcome({"blocked": True, "refused": "barred_tonight", "destination": place_name,
                         "requirements": night}, ok=False, message=_night_refusal(place_name))
    result = move_player(player_id=r.player_id, destination_id=place_id,
                         requirements_met=cmd.flag("through"))
    if result.get("success"):
        spent = _spend_road(r, origin, place_id, place_name)
        if spent:
            result["road_minutes"] = spent
        # People this same reply invented belong to the scene it is moving
        # into (2026-10-04: "@new-npc Mirelle Ash" then "@move Cooper's
        # Rooms" left her talking from the square the player had just left)
        for npc_id in r.created_npc_ids:
            with get_session() as session:
                npc = session.get(NPC, npc_id)
                stranded = npc is not None and npc.current_location_id == origin
            if stranded:
                _relocate(npc_id, place_id)
    if result.get("blocked"):
        if night is not None:
            return _Outcome(result, ok=False, message=_night_refusal(place_name))
        needs = ", ".join(str(x) for x in result.get("requirements") or []) or "something stands in the way"
        return _Outcome(result, ok=False, message=(
            f"the way to {place_name} is blocked ({_clip(needs, 160)}) — they did not go; only if the "
            f"fiction truly gets them past it, write @move {place_name} | through"))
    if result.get("success"):
        r.present_ids = []          # the people at the new place are the present ones now
    return _Outcome(result)


def _barred_tonight(r: CommandRunner, origin: str | None, place_id: str) -> list[str] | None:
    """The route's requirements when it is barred for the night
    (travel.barred_tonight), else None."""
    from src.services.travel import check_route

    if not origin:
        return None
    with get_session() as session:
        route = check_route(session, origin, place_id, r._player_row(session))
    return list(route.get("requirements") or []) if route.get("barred_tonight") else None


_WON_ROLL = frozenset({"success", "critical_success", "partial"})
# (the verb as written: `@gold -3` is a @coin line too)
_COIN_PAID_OUT = re.compile(r"^@[\w-]+\s+-\s*\d", re.IGNORECASE)


# What a roll or a bribe that gets someone through a barred way is about
_FOR_THE_WAY = re.compile(
    r"\b(?:gates?|gatehouse|gate-?keeper|guards?|sentr(?:y|ies)|watchm[ae]n|walls?|keeper|barred|bars?"
    r"|doors?|portcullis|wicket|postern|road|through|past|climb\w*|brib\w*"
    r"|let\s+(?:me|us|them|her|him|you)\s+(?:in|out|through|pass|by))\b",
    re.IGNORECASE,
)
_WAY_STOP = frozenset({"north", "south", "east", "west", "the", "old", "house", "inn"})


def _way_words(origin: str | None, place_id: str | None) -> list[str]:
    """Name words of the two ends of the road and of the people at its near
    end (the sergeant a bribe goes to)."""
    names: list[str] = []
    try:
        with get_session() as session:
            for loc_id in (origin, place_id):
                loc = session.get(Location, loc_id) if loc_id else None
                if loc is not None:
                    names.append(loc.name or "")
            if origin:
                names += [n.name or "" for n in session.query(NPC).filter(
                    NPC.current_location_id == origin, NPC.status == "alive").all()]
    except Exception as e:
        logger.debug(f"way words skipped: {e}")
    words = {w.lower() for name in names for w in re.findall(r"[A-Za-z][\w'’-]{3,}", name)}
    return sorted(words - _WAY_STOP)


def _about_the_way(text: str, words: list[str]) -> bool:
    text = text or ""
    return bool(_FOR_THE_WAY.search(text)) or any(
        re.search(rf"(?<![\w'’-]){re.escape(w)}(?![\w'’-])", text, re.IGNORECASE) for w in words)


def _won_the_night(r: CommandRunner, origin: str | None = None, place_id: str | None = None) -> bool:
    """Did this reply or the last win a roll (or half-win it), or pay coin
    out — the bribe — FOR this way? Either is the fiction that opens a
    barred gate; the pass the Captain sealed is not. A roll or a payment
    about something else (a tip in the stable yard, a won word with the
    herbalist) opens nothing (round-7 travel review); a roll whose reason the
    stream never saw (the CLI) and a bare `@coin -N` still count."""
    words = _way_words(origin, place_id)
    for runner in (r, r.previous):
        if runner is None:
            continue
        rolls = [x for x in getattr(runner, "rolls", None) or () if isinstance(x, dict)]
        if rolls:
            if any(str(x.get("outcome")) in _WON_ROLL and _about_the_way(str(x.get("reason") or ""), words)
                   for x in rolls):
                return True
        elif any(str(o) in _WON_ROLL for o in runner.roll_outcomes or ()):
            return True
        for res in runner.results:
            if res.ok and res.verb == "coin" and _COIN_PAID_OUT.match(res.line or ""):
                reason = (res.line or "").partition("|")[2]
                if not reason.strip() or _about_the_way(reason, words):
                    return True
    return False


def _night_refusal(place_name: str) -> str:
    return (f"the way to {place_name} is barred for the night — a pass does not open it; they did not go. "
            "Show what does in the story (a won roll — roll_check first — or a bribe, @coin -N), then write "
            f"@move {place_name} | through; or they wait for first light")


def _h_time(r: CommandRunner, cmd: ParsedCommand) -> dict | _Outcome:
    text = cmd.main or (cmd.extras[0] if cmd.extras else "")
    minutes = parse_duration(text)
    if minutes is None or minutes <= 0:
        raise CommandError(f"could not read '{text}' as a duration — write 20m, 2h or 1h30m")
    if minutes > MAX_MINUTES:
        raise CommandError("that is more than two weeks — skip time in smaller steps")
    reason = cmd.extras[0] if cmd.main and cmd.extras else ""
    r.time_written = True
    road, r.road_minutes = r.road_minutes, 0     # restated once; a second @time is another wait
    if road and minutes <= road:
        return _Outcome({"skipped": "already_charged", "minutes": minutes, "road_minutes": road}, ok=False,
                        message=f"not charged twice — @move already spent the {road}-minute road; "
                                "never @time for a journey @move made")
    if road:
        result = advance_time(hours=(minutes - road) / 60.0, reason=reason)
        return _Outcome(result, message=f"@move already spent the {road}-minute road, so only "
                                        f"{minutes - road} more minutes passed — @time is only for what "
                                        "else takes time, never the journey")
    return advance_time(hours=minutes / 60.0, reason=reason)


def _nearest_bed(r: CommandRunner) -> tuple[str, str] | None:
    """(id, name) of a place one open step away where a night can be spent."""
    from src.services.travel import check_route

    with get_session() as session:
        player = r._player_row(session)
        here = session.get(Location, player.current_location_id) if player.current_location_id else None
        if here is None:
            return None
        candidates = {c.to_location_id if c.from_location_id == here.id else c.from_location_id
                      for c in session.query(Connection).filter(
                          (Connection.from_location_id == here.id)
                          | ((Connection.to_location_id == here.id) & (Connection.bidirectional == True))  # noqa: E712
                      ).all()}
        candidates |= {c.id for c in session.query(Location).filter(Location.parent_id == here.id).all()}
        if here.parent_id:
            candidates.add(here.parent_id)
        for loc_id in candidates:
            loc = session.get(Location, loc_id)
            if loc is None or rest_here(loc) is None:
                continue
            if check_route(session, here.id, loc.id, player).get("blocked"):
                continue
            return loc.id, loc.name
    return None


def _sleep(r: CommandRunner) -> dict:
    """`rest_now` where they stand, the night charged once per reply.

    A @coin earlier in the reply that took exactly this bed's price WAS the
    payment, so the night is not charged again: the refund without a second
    write, and the purse check cannot refuse the bed over coin the DM already
    took for it. The bed this rest settled is remembered, so a later @coin
    restating it is skipped.

    A night a scheduled event breaks is not charged at all (rest.py: the bed
    is paid when they sleep through). So the reply's own bed fee goes back,
    and a later one is skipped — otherwise the seed's 20:00 Evening Howl,
    which wakes a 19:00 sleeper, would leave the fee taken and the next
    turn's @rest charging the bed again."""
    with get_session() as session:
        player = r._player_row(session)
        here = session.get(Location, player.current_location_id) if player.current_location_id else None
        spec = rest_here(here)
    price = int(spec["price"]) if spec else 0
    prepaid = price > 0 and price in r.coin_debits
    result = rest_now(r.player_id, pay=not prepaid)
    if result.get("error"):
        return result
    broken = bool(result.get("interrupted"))
    if prepaid:
        r.coin_debits.remove(price)
        if broken:
            adjust_currency(owner_id=r.player_id, amount=price, owner_type="player")
            result["refunded"] = price
        else:
            result["prepaid"] = price
    elif price:
        r.bed_charge, r.bed_waived = price, broken
    return result


def _slept(result: dict, walked_to: str | None = None) -> _Outcome:
    """A night's result as a command outcome."""
    reason = result.get("error")
    if not reason:
        notes = []
        if walked_to:
            notes.append(f"they walked to {walked_to} to sleep — the night was spent there")
        if result.get("prepaid"):
            notes.append(f"charged once — your @coin -{result['prepaid']} paid for the bed, so the night "
                         "cost nothing more (never @coin for a bed; @rest charges it)")
        if result.get("refunded"):
            notes.append(f"the night was broken, and a broken night is not charged — your @coin "
                         f"-{result['refunded']} for the bed was given back (@rest charges it when they sleep through)")
        if result.get("interrupted"):
            # The prose you wrote assumed a morning that never came
            notes.append(f"{result.get('summary') or 'they were woken in the night'} — it is NOT morning; "
                         "open on what woke them")
        return _Outcome(result, message="; ".join(notes))
    messages = {
        "no_rest_here": "there is nowhere safe to sleep here — they did not rest; an inn or a camp is where a night is spent",
        "cannot_afford": f"the bed costs {result.get('price')} coin and they carry {result.get('currency')} — they did not rest",
        "dead": "the dead do not rest",
    }
    return _Outcome(result, ok=False, message=messages.get(reason, f"they could not rest ({reason})"))


def _h_rest(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    if r.rested:
        # One night per reply: a paid room already ran the rest, and a second
        # rest_now from first light would sleep through a whole day
        return _Outcome({"success": True, "already_rested": True},
                        message="the night was already taken in this reply")
    result = _sleep(r)
    if not result.get("error"):
        r.rested = True
    if result.get("error") == "no_rest_here":
        # "Go to bed" with an inn next door means the inn. Playtest
        # 2026-10-04: @rest in the market failed, the prose slept on a cot
        # anyway, and the next morning was a fiction the world never saw
        bed = _nearest_bed(r)
        if bed is not None:
            moved = move_player(player_id=r.player_id, destination_id=bed[0])
            if moved.get("success"):
                r.present_ids = []      # as after @move: the inn's people are the present ones
                night = _sleep(r)
                r.rested = not night.get("error")
                return _slept(night, walked_to=bed[1])
    return _slept(result)


# -- the player --------------------------------------------------------------

_HEALTH_SYNONYMS = {"ok": "healthy", "fine": "healthy", "unhurt": "healthy", "wounded": "hurt",
                    "injured": "hurt", "badly_wounded": "badly_hurt", "dying": "critical"}


def _h_health(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    label = re.sub(r"[\s-]+", "_", cmd.main.strip().lower())
    label = _HEALTH_SYNONYMS.get(label, label)
    if label not in HEALTH_LABELS:
        raise CommandError(f"health is one of {', '.join(HEALTH_LABELS)} (not '{cmd.main}')")
    cause = _first(cmd, "cause")
    worse = _worse_after_fight(r, label)
    if worse is not None:
        return worse
    result = update_player_health(player_id=r.player_id, new_status=label, cause=cause)
    if label == "dead" and result.get("player_died"):
        return _Outcome(result, message="the character is dead — what follows is their epilogue")
    return _Outcome(result)


def _worse_after_fight(r: CommandRunner, label: str) -> _Outcome | None:
    """The refusal for an aftermath @health that makes the player worse, or
    None. The engine already applied the fight's wounds from how it went
    (combat.apply_combat_result): fleet10-tamsin T14 got "Your arm throbs
    where teeth caught you" and @health winded after a fight won with no hit
    taken, T19 @health badly_hurt after one that added none. A better label
    (a wound dressed) still lands; so does death after a defeat — that call
    is the DM's (a downed player is critical)."""
    if not r.after_combat:
        return None
    with get_session() as session:
        player = session.get(Player, r.player_id)
        now = (player.health_status if player is not None else None) or "healthy"
    if now not in HEALTH_LABELS or HEALTH_LABELS.index(label) <= HEALTH_LABELS.index(now):
        return None
    if label == "dead" and now == "critical":
        return None
    unhurt = " — they came through the fight unhurt: no wound, no throbbing bite" if now == "healthy" else ""
    return _Outcome({"refused": "after_combat_wound", "health": now, "asked": label}, ok=False, message=(
        f"refused — the fight's wounds are already counted: their health stays '{now}'{unhurt}. The engine "
        "applied what landed in the fight; this aftermath narrates it and adds no wound of its own. Never write "
        "a worse @health after a fight (a wound dressed, better, is fine), and never build on a wound the "
        "aftermath's prose gave them"))


# "a night's room", "Maera's bed", "lodging" — but not "a round for the room"
_BED_WORDS = re.compile(
    r"\b(?:a room|their room|her room|his room|my room|room for the night|bed|beds|lodging|a night|"
    r"the night|night's|cot|bunk)\b"
)


def _h_coin(r: CommandRunner, cmd: ParsedCommand) -> dict | _Outcome:
    amount = parse_signed(cmd.main)
    if amount is None and cmd.extras:
        amount = parse_signed(cmd.extras[0])
    if not amount:
        raise CommandError("write the change as +N or -N, e.g. @coin -12")
    if amount > 0 and amount in r.paid_coin:
        return _Outcome({"skipped": "already_paid", "amount": amount}, ok=False,
                        message=f"not paid twice — @quest-done already handed over the {amount} coin reward")
    reason = " ".join([cmd.main, *cmd.extras]).lower()
    recent = _recent_payout(r, "coin", amount, reason=reason) if amount > 0 else None
    if recent:
        return _Outcome({"skipped": "already_paid", "amount": amount}, ok=False, message=(
            f"not paid twice — \"{recent}\" already put the {amount} coin in their purse a reply ago; "
            "show it changing hands in the prose, never @coin it again"))
    if amount < 0 and not r.bed_charge and _BED_WORDS.search(reason):
        # "@coin -8 | a night's room" then dawn in the prose, with the clock
        # still at night (both round-2 fleet sessions). Paying for a bed IS
        # sleeping in it: run the night, at the house's own price
        outcome = _h_rest(r, cmd)
        note = "a paid bed is @rest — the night was taken and charged at the house's price"
        return _Outcome(outcome.result, ok=outcome.ok,
                        message="; ".join(m for m in (note, outcome.message) if m))
    if amount < 0 and -amount == r.bed_charge:
        r.bed_charge = 0            # restated once; a second -N is a different payment
        why = ("the night was broken, and a broken night is not charged — @rest charges the bed when "
               "they sleep through" if r.bed_waived else f"@rest already took the {-amount} coin for the bed")
        return _Outcome({"skipped": "already_charged", "amount": amount}, ok=False,
                        message=f"not charged twice — {why}")
    if amount < 0:
        with get_session() as session:
            purse = int(r._player_row(session).currency or 0)
        if purse < -amount:
            raise CommandError(f"they carry only {purse} coin — nothing was taken")
    result = adjust_currency(owner_id=r.player_id, amount=amount, owner_type="player")
    if amount < 0 and not _refused(result):
        r.coin_debits.append(-amount)
        # ...and its reason says it was a gift, not a price or a loss (`_COIN_NOT_GIFT`)
        given = not (_GIFT_NOT.search(reason) or _COIN_NOT_GIFT.search(reason))
        kindness = _gift_trust(r, _coin_recipient(r, " ".join(cmd.extras)), f"{-amount} coin") if given else None
        if kindness is not None:
            return kindness.with_result(result)
    if amount > 0 and not _refused(result):
        advance = _note_advance(r, reason, coin=amount)
        if advance:
            return _Outcome(result, message=advance)
    return result


# Kindness counts. fleet10-wenna (a persona made of gifts) pressed a stone
# into one hand at T5 and two coin into another at T12, "Thank you for the
# truth" — every trust in the world stayed at 50 of the 60 a secret needs,
# and the stable hand's clue stayed locked though she had asked him exactly
# about it. A gift the player gives in their own words, landed by `@coin -N`
# or `@hand` to a present person, raises that person's trust a little —
# once a game-day each, never past standing.GIFT_TRUST_CAP. A price, a fee
# or a bribe is no gift. Replayed over every fleet transcript's 26 landed
# @coin -N / @hand lines: the 13 coin gifts and tips read as gifts; the 7
# prices (rooms, bread, bandages, a ledger fee, a bar drink) and a pickpocket
# line do not; the 5 @hand lines are the jobs' proof, which `_h_hand` leaves out.
_GIFT_GIVE = re.compile(
    r"\b(?:give|gives|gave|giving|press|presses|pressed|pressing|toss|tosses|tossed|tossing|tip|tips|tipped"
    r"|tipping|offer|offers|offered|offering|hand|hands|handed|handing|slide|slides|slid|sliding"
    r"|hold(?:s|ing)?\s+(?:it|them)\s+out)\b",
    re.IGNORECASE,
)
_GIFT_NOT = re.compile(
    r"\b(?:buy|buys|buying)\s+(?!you\b|him\b|her\b|them\b|yourself\b)|\b(?:purchase\w*|pay(?:s|ing)?\s+for"
    r"|how\s+much\s+(?:for|is|are|does|do|will|would)|price\w*|room|bed|counter|bar|bribe\w*"
    r"|look\s+the\s+other\s+way|let\s+(?:me|us)\s+(?:pass|through|in)|toll|fee|rent|wager\w*|bet|stake"
    r"|in\s+exchange|to\s+look\s+at|sell\w*|trade|bandages?|spent|costs?)\b",
    re.IGNORECASE,
)
# The @coin line's own reason, the DM's account of where the coin went: a
# price (`_GIFT_NOT`), a loss or wages there is no gift whatever the
# player's words (fleet9-bram T11 "a quick hand in the press lifts coin from
# his purse" — a theft — came with no gift words, but a theft beside a
# hand-over would have thanked the thief's neighbour). In the player's own
# words "spent" is a price too (fleet2-tamsin T7: "I hand over the coin…
# Two coin well spent" bought a sweet). Replayed over the 21 landed @coin -N
# lines of every fleet transcript: the 13 gifts and tips still count, the
# room and the theft are caught by their reasons, no gift is lost.
_COIN_NOT_GIFT = re.compile(
    r"\b(?:lost|loses?|stolen|stole|steal\w*|lift(?:s|ed|ing)?|cut|pickpocket\w*|robb\w*"
    r"|fined|charge[sd]?|debt|owed|repa(?:y|id)\w*|wages?|hire[sd]?|hiring)\b",
    re.IGNORECASE,
)


def gift_in_words(text: str | None) -> bool:
    """Did the player give something away in these words (not pay for it)?"""
    text = text or ""
    return bool(_GIFT_GIVE.search(text)) and not _GIFT_NOT.search(text)


def _named_in(npc: NPC, text: str) -> bool:
    """Is this person named in `text` — a word of their name, their whole
    role label or trade ("the stable hand"; never "hand" alone)?"""
    from src.services.dialogue import role_label

    lowered = text.lower()
    words = [w for w in re.findall(r"[a-z]+", (npc.name or "").lower()) if len(w) >= 3 and w not in _NPC_STOP]
    if any(re.search(rf"\b{re.escape(w)}\b", lowered) for w in words):
        return True
    phrases = {str(npc.profession or "").strip().lower()}
    try:
        phrases.add(role_label(npc).lower())
    except Exception:
        pass
    return any(p and len(p) >= 4 and re.search(rf"\b{re.escape(p)}\b", lowered) for p in phrases)


def _coin_recipient(r: CommandRunner, reason: str) -> str | None:
    """Who a @coin -N went to: the present person its reason names, else
    the one the player's words name, else the only person here."""
    from src.services.dialogue import is_beast

    with get_session() as session:
        here = r._player_row(session).current_location_id
        people = [n for n in session.query(NPC).filter(NPC.status == "alive").all()
                  if (n.id in r.present_ids or (here and n.current_location_id == here)) and not is_beast(n)]
        for text in (reason, r.player_input or ""):
            named = [n.id for n in people if text and _named_in(n, text)]
            if len(named) == 1:
                return named[0]
            if named:
                return None
        return people[0].id if len(people) == 1 else None


@dataclass
class _Kindness:
    message: str
    gift: dict[str, Any]

    def with_result(self, result: dict) -> _Outcome:
        return _Outcome({**(result or {}), "gift_trust": self.gift}, message=self.message)


def _gift_trust(r: CommandRunner, npc_id: str | None, what: str) -> _Kindness | None:
    """The trust a gift just earned, applied (see above), or None: no words
    known (the CLI), no gift in them, nobody to receive it, a beast, a
    kindness already counted today or this reply, or trust at the cap."""
    from src.services import standing
    from src.services.dialogue import ensure_relationship, is_beast

    if not npc_id or r.player_input is None or _ENGINE_INPUT.match(r.player_input) \
            or not gift_in_words(r.player_input) or npc_id in r.trust_raised:
        return None
    if any(a.get("giver_id") == npc_id for a in r.advances):
        return None                     # pay booked against their job is no gift
    with get_session() as session:
        npc = session.get(NPC, npc_id)
        if npc is None or npc.status != "alive" or is_beast(npc):
            return None
        clock = session.query(WorldClock).first()
        day = clock.day if clock else 1
        rel = ensure_relationship(session, r.player_id, npc_id)
        before = standing.trust_of(rel)
        if rel.last_gift_day == day or before >= standing.GIFT_TRUST_CAP:
            return None
        rel.trust_level = after = min(standing.GIFT_TRUST_CAP, before + standing.GIFT_TRUST)
        rel.last_gift_day = day
        rel.last_interaction_day = day
        name = npc.name
        session.commit()
    r.trust_raised[npc_id] = "gift"
    r.gift_trust[npc_id] = after - before
    opens = before < standing.SECRET_TRUST <= after
    logger.info(f"Kindness: {what} to {name}, trust {before} -> {after}")
    return _Kindness(
        message=(f"{name} took the {what} as a kindness: their trust rose to {after}/100 (the engine adds it once "
                 "a day — never write @trust for it). Let it show in how they treat the player, never as a number"
                 + ("; it is enough now for what they were holding back — their dossier says what they would "
                    "confess" if opens else "") + "."),
        gift={"npc_id": npc_id, "npc_name": name, "trust": after},
    )


# A clue is the shop's "quest_item": no stats, never worn, never sold
_ITEM_TYPE_BY_KIND = {"weapon": "weapon", "armor": "armor", "trinket": "misc",
                      "potion": "consumable", "key": "quest_item", "clue": "quest_item", "misc": "misc"}
_KIND_SYNONYMS = {"armour": "armor", "clothes": "armor", "clothing": "armor", "consumable": "potion",
                  "draught": "potion", "quest": "key", "quest_item": "key", "quest-item": "key",
                  "item": "misc", "gear": "weapon", "document": "clue", "evidence": "clue",
                  "letter": "clue", "note": "clue", "notes": "clue", "paper": "clue", "papers": "clue"}

# Paper is a clue whatever kind the DM wrote. fleet4-tamsin T15: `@give
# Piper's Howl Notes | kind: trinket | tier: common` went on as her combat
# trinket. Read off the name's head noun — its last word before any "of /
# to / on …" — so "Letter of Marque" is paper and "Page's Dagger" is not
_DOCUMENT_WORDS = frozenset({
    "note", "notes", "letter", "letters", "ledger", "ledgers", "map", "maps", "chart", "charts",
    "page", "pages", "paper", "papers", "deed", "deeds", "list", "lists", "journal", "journals",
    "diary", "diaries", "receipt", "receipts", "missive", "manifest", "roster", "writ",
    # fleet-tamsin T5: "Piper's Howl Tally" (a scrap of paper) went on as a trinket
    "tally", "tallies", "notebook", "logbook", "orders", "dispatch", "warrant", "contract",
    "sketch", "rubbing", "record", "records",
})
_HEAD_NOUN_CUT = re.compile(r"\s+(?:of|on|to|from|for|about|with)\s+", re.I)


def _is_document(name: str) -> bool:
    head = _HEAD_NOUN_CUT.split(name or "", maxsplit=1)[0]
    words = re.findall(r"[a-z]+", head.lower())
    return bool(words) and words[-1] in _DOCUMENT_WORDS


# Money and evidence are never gear either. fleet5-tamsin T12: `@give Silver
# Coin | kind: trinket | tier: common` went on as an equipped combat trinket
# worth 60 — more than her whole purse. A "piece" or "mark" is money only
# with a metal before it ("Shoulder Piece" is armour); never "token" — a
# ferry token is a key, a wolf token a charm
_MONEY_HEADS = frozenset({
    "coin", "coins", "penny", "pennies", "purse", "shilling", "shillings", "groat", "groats",
    "ducat", "ducats", "florin", "florins", "doubloon", "doubloons",
})
_METAL_HEADS = frozenset({"piece", "pieces", "mark", "marks"})
_METALS = frozenset({"silver", "gold", "golden", "copper", "bronze", "brass", "minted"})
_EVIDENCE_HEADS = frozenset({"scrap", "scraps", "fragment", "fragments", "button", "buttons"})


def _is_money_or_evidence(name: str) -> bool:
    head = _HEAD_NOUN_CUT.split(name or "", maxsplit=1)[0]
    words = re.findall(r"[a-z]+", head.lower())
    if not words:
        return False
    last = words[-1]
    return (last in _MONEY_HEADS or last in _EVIDENCE_HEADS
            or (last in _METAL_HEADS and bool(_METALS & set(words[:-1]))))


_GEAR_WORDS = {
    "weapon": {"sword", "blade", "knife", "dagger", "axe", "hatchet", "mace", "club", "spear", "bow",
               "crossbow", "staff", "hammer", "sabre", "saber", "rapier", "cudgel", "flail", "pike"},
    "armor": {"armor", "armour", "mail", "jerkin", "coat", "cloak", "brigandine", "cuirass", "gambeson",
              "vest", "hauberk", "plate", "helm", "helmet", "shield", "robe"},
    "trinket": {"charm", "amulet", "ring", "talisman", "token", "pendant", "brooch", "locket", "flask"},
}


def _gear_slot_from_name(name: str) -> str | None:
    """'Iron Sword' -> weapon: a tier with no kind is gear the name gives away."""
    words = set(re.findall(r"[a-z]+", (name or "").lower()))
    for slot, nouns in _GEAR_WORDS.items():
        if words & nouns:
            return slot
    return None


def _shape(entry: dict[str, Any]) -> tuple:
    """What a pack entry IS: (slot, tier, potion) when it has mechanics, else
    (its type,). Two entries of one name and one shape stack."""
    mech = prog.mechanize_item(entry)
    if mech:
        return mech["slot"], mech["tier"], mech["effect_tag"]
    return (str(entry.get("type") or "misc").strip().lower(),)


def _plain(shape: tuple) -> bool:
    return shape in (("misc",), ("",))


def _shape_words(shape: tuple) -> str:
    if len(shape) == 3:
        slot, tier, tag = shape
        return f"a {prog.tier_label(tier) or 'common'} {slot}" if slot else f"a {tag} potion"
    return {"quest_item": "a clue or key", "consumable": "something to eat or drink",
            "misc": "plain stuff"}.get(shape[0], shape[0])


def _pack_entry(r: CommandRunner, name: str) -> dict[str, Any] | None:
    """The entry `add_to_inventory` would stack a gift of this name onto."""
    item_id = name.lower().replace(" ", "_")
    with get_session() as session:
        entries = _validate_inventory(r._player_row(session).inventory)
    return next((e for e in entries if e.get("id") == item_id
                 or str(e.get("name") or "").lower() == name.lower()), None)


def _h_give(r: CommandRunner, cmd: ParsedCommand) -> dict | _Outcome:
    name, quantity = split_quantity(cmd.main)
    name = _clean_label(name)
    if not name:
        raise CommandError("name the item, e.g. @give Gate Pass | kind: key")
    kind = (cmd.opt("kind") or "").strip().lower()
    kind = _KIND_SYNONYMS.get(kind, kind)
    tier = (cmd.opt("tier") or "").strip().lower() or None
    potion = (cmd.opt("potion") or "").strip().lower() or None
    style = (cmd.opt("style") or "").strip().lower() or None
    uniform = cmd.opt("uniform")
    hood = cmd.flag("hood")
    notes: list[str] = []
    if (not kind or kind in prog.SLOTS) and not potion and _is_document(name):
        if kind:
            notes.append(f"{name} is paper — kept as a clue (no stats, never worn or sold), not a {kind}")
        kind, tier, style, uniform, hood = "clue", None, None, None, False
    elif (kind in prog.SLOTS or (not kind and tier)) and not potion and _is_money_or_evidence(name):
        notes.append(f"{name} is money or evidence — kept as a clue (no stats, never worn or sold), not "
                     f"{('a ' + kind) if kind else 'gear'}; coin they can spend is @coin +N")
        kind, tier, style, uniform, hood = "clue", None, None, None, False
    if not kind:
        # A potion tag means a potion; anything about how it is WORN means clothes
        kind = "potion" if potion else "armor" if (style or uniform or hood) else "misc"
    if kind not in GIVE_KINDS:
        raise CommandError(f"kind is one of {', '.join(GIVE_KINDS)} (not '{kind}')")
    slot = kind if kind in prog.SLOTS else None
    if tier and not slot:
        guessed = None if cmd.opt("kind") else _gear_slot_from_name(name)
        if guessed:
            kind = slot = guessed
        elif cmd.opt("kind"):
            # Bread does not come in tiers; the gift still happens (autoplay
            # 2026-10-04: "@give Traveler's Bread | kind: misc | tier: crude"
            # failed outright and the bread existed only in prose)
            tier = None
        else:
            raise CommandError("a tier needs kind: weapon, armor or trinket")
    if slot:
        tier = tier or "common"
        if tier not in prog.TIER_LABELS:
            raise CommandError(f"tier is one of {', '.join(prog.TIER_LABELS)} (not '{tier}')")
    effect_tag = None
    if kind == "potion":
        effect_tag = potion or "heal_light"
        if effect_tag not in prog.CONSUMABLES:
            raise CommandError(f"potion is one of {', '.join(prog.CONSUMABLES)} (not '{effect_tag}')")
    attire = None
    if kind == "armor" and (style or uniform or hood):
        if style and style not in STYLES:
            raise CommandError(f"style is one of {', '.join(STYLES)} (not '{style}')")
        attire = {"style": style, "uniform_faction_id": r.faction(uniform)[0] if uniform else None,
                  "concealing": hood}
    item_type, tier = _ITEM_TYPE_BY_KIND[kind], (tier if slot else None)
    key = prog._slug(name)
    if key in r.found_items or _found_this_reply(r, name):
        # Dropped quietly: the pickup already happened, and the line only
        # restates it (fleet10-bram T15 and -tamsin T16 booked it as a
        # failure, which read as the find not landing)
        return _Outcome({"skipped": "already_found", "item": name}, ok=True)
    authored = _authored_substitute(r, name)
    if authored is not None:
        return authored
    # From the hand that holds it: what a person here carries leaves them
    # (fleet9-wenna T6: the Captain's Gate Pass, held out of his own coat)
    from_holder = _from_present_holder(r, name, key)
    if from_holder is not None:
        return from_holder

    # One name, one thing. add_to_inventory stacks by name and keeps the
    # first entry's mechanics, so a second @give that says something else
    # about it was silently lost (fleet4-wenna T14: the DM's trinket charm
    # merged into the reward's plain one; its kind, tier and text gone)
    have = _pack_entry(r, name)
    old, new = (_shape(have) if have else None), _shape(
        {"type": item_type, "slot": slot, "tier": tier, "effect_tag": effect_tag})
    if have is not None and _plain(old) and not _plain(new):
        # The fuller word wins over plain stuff, on the SAME entry: the DM
        # is saying what the thing is, not handing over a second one
        result = regrade_inventory_item(
            r.player_id, have.get("name") or name, item_type=item_type, slot=slot, tier=tier,
            effect_tag=effect_tag, description=cmd.opt("about") or "",
            visual_desc=cmd.opt("looks") or None, attire=attire)
        if _refused(result):
            return _Outcome(result)
        what = f"that one is {_shape_words(new)} now"
        if key in r.paid_items:
            return _Outcome(result, message=f"not paid twice — @quest-done already handed over the "
                                            f"{r.paid_items[key]}; {what}, as you wrote it")
        return _Outcome(result, message=(
            f"{have.get('name')} was already in their pack as plain stuff — {what}; no second one was "
            "added (if they truly got another, @give it again)"))
    if key in r.paid_items:
        return _Outcome({"skipped": "already_paid", "item": name}, ok=False,
                        message=f"not paid twice — @quest-done already handed over the {r.paid_items[key]}")
    recent = _recent_payout(r, "item", name) if have is not None else None
    if recent:
        return _Outcome({"skipped": "already_paid", "item": name}, ok=False, message=(
            f"not paid twice — \"{recent}\" already put the {have.get('name') or name} in their pack a reply "
            "ago; show it changing hands in the prose, never @give it again"))
    if have is not None and old != new:
        if not _plain(new) and old == ("quest_item",):
            # Evidence restated as gear (the Hollow's whistle as "kind:
            # trinket"): it stays evidence, and the renamed-copy advice below
            # would mint a second, wearable whistle
            raise CommandError(f"they already carry {have.get('name')} as a clue or key — it stays one "
                               "(carried, never worn); nothing was added")
        if not _plain(new):
            example = f" (e.g. @give {(tier or 'common').capitalize()} {name} | kind: {kind} | tier: {tier})" \
                if slot else ""
            raise CommandError(f"they already carry {_shape_words(old)} called {have.get('name')} — nothing "
                               f"was added; a different one needs its own name{example}")
        notes.append(f"stacked onto the {have.get('name')} they already carry ({_shape_words(old)})")
    result = add_to_inventory(
        player_id=r.player_id, item_name=name, item_type=item_type,
        description=cmd.opt("about") or "", quantity=quantity, slot=slot,
        tier=tier, effect_tag=effect_tag,
        visual_desc=cmd.opt("looks") or None, attire=attire,
    )
    if item_type == "quest_item" and have is None and not _refused(result):
        # A clue or key in hand is a thing the character now knows of
        about = _clip(cmd.opt("about") or "", 240)
        _auto_journal(r, [f"{name} — {about}" if about else f"Came by {name}"])
    if not _refused(result):
        advance = _note_advance(r, cmd.opt("about") or "", item=name)
        if advance:
            notes.append(advance)
    if notes and not _refused(result):
        return _Outcome(result, message="; ".join(notes))
    return result


def _from_present_holder(r: CommandRunner, name: str, key: str) -> _Outcome | None:
    """A @give of a key or clue someone here carries (`inventory_notable`):
    it leaves their keeping for the player's pack, as itself, instead of a
    copy appearing while they still hold the original (the seed's Captain
    carries the Gate Pass, fleet9-wenna T1-T6). Only a quest item: a
    merchant's shelf is @buy's, and a draught one person gives must not
    empty the hawker's stock beside her. None when nobody here holds it, or
    the player already carries one (the stacking rules decide)."""
    if key in r.paid_items or _pack_entry(r, name) is not None:
        return None
    from src.services.lead import same_item

    ids = _present_npc_ids(r)
    holder = None
    with get_session() as session:
        for npc in session.query(NPC).filter(NPC.id.in_(list(ids)), NPC.status == "alive").all():
            for entry in _validate_inventory(npc.inventory_notable or []):
                held = str(entry.get("name") or "")
                if held and entry.get("type") == "quest_item" and (
                        prog._slug(held) == key or same_item(name, held) or same_item(held, name)):
                    holder = (npc.id, npc.name, entry)
                    break
            if holder is not None:
                break
    if holder is None:
        return None
    npc_id, npc_name, entry = holder
    held = str(entry["name"])
    result = transfer_item(npc_id, r.player_id, entry.get("id") or prog._slug(held), 1,
                           from_type="npc", to_type="player")
    if _refused(result):
        return _Outcome(result)
    about = _clip(str(entry.get("description") or ""), 240)
    _auto_journal(r, [f"{held} — {about}" if about else f"Came by {held}"])
    named = "" if prog._slug(held) == key else f" (it is called {held})"
    return _Outcome({**result, "item": held, "from": npc_name},
                    message=f"{npc_name} handed over their {held}{named} — it left their keeping for the "
                            "player's pack")


# Kinds of authored Thing that hold an item, and the held items that are
# clues rather than supplies: a gifted sweetroot bundle or iron poker is
# just a gift, whatever a market crate across town holds
_HOLDING_KINDS = ("take", "open", "search")
_SUPPLY_TYPES = ("consumable", "weapon", "armor")


def _names_held_item(name: str, held: str) -> bool:
    """Does a @give's `name` mean the authored `held` item? The same slug;
    every word of a two-word-or-longer name inside it ("Silver Coin" in
    "Greywater Silver Piece": money nouns are one noun); or one word that IS
    its head noun ("Whistle")."""
    from src.services.lead import item_words

    if prog._slug(name) == prog._slug(held):
        return True
    wanted, words = item_words(name), item_words(held)
    if len(wanted) >= 2:
        return wanted <= words
    head_words = _HEAD_NOUN_CUT.split(held, maxsplit=1)[0].split()
    return len(wanted) == 1 and bool(head_words) and wanted == item_words(head_words[-1])


def _authored_hit(r: CommandRunner, name: str, *, things: bool = False
                  ) -> tuple[tuple[str, str, dict[str, Any], str] | None, str | None]:
    """((location id, place name, the Thing, its item's name) | None, here):
    the authored Thing still holding the item `name` names — here first,
    elsewhere only a clue (`_authored_substitute`). `things`: the Thing's own
    label counts too ("@interact Split supply crate" a world away)."""
    from src.services import objectives as objectives_mod

    here = r.current_location_id()
    with get_session() as session:
        rows = [(loc.id, loc.name, loc.objects) for loc in session.query(Location).all() if loc.objects]
        player = session.get(Player, r.player_id)
        wanted = [str(o.get("item_name")) for q in objectives_mod.active_quests_for(session, player)
                  for o in objectives_mod.normalize_objectives(q.objectives)
                  if o.get("item_name") and not o.get("done")] if player is not None else []

    def clue(item: dict[str, Any]) -> bool:
        # Elsewhere only a clue is never re-made: a key or clue, money or
        # paper, or what a quest step asks for. The market crate's Coil of
        # Hemp Rope must not refuse a coil of rope Sorrel hands over
        held = str(item["name"])
        return (item.get("type") == "quest_item" or _is_money_or_evidence(held) or _is_document(held)
                or any(objectives_mod._item_matches(w, [held]) for w in wanted))

    hit = None
    for loc_id, loc_name, raw in sorted(rows, key=lambda row: row[0] != here):
        for obj in normalize_objects(raw):
            item = obj["payload"].get("item") or {}
            if (obj["kind"] in _HOLDING_KINDS and not obj["consumed"] and item.get("name")
                    and item.get("type") not in _SUPPLY_TYPES
                    and (_names_held_item(name, item["name"])
                         or (things and prog._slug(name) == prog._slug(obj["label"])))
                    and (loc_id == here or clue(item))):
                hit = (loc_id, loc_name, obj, item["name"])
                break
        if hit:
            break
    return hit, here


def _what_it_is(obj: dict[str, Any], held: str) -> str:
    """"the cracked bone whistle", or "the Greywater Silver Piece in the split supply crate"."""
    from src.services.lead import _thing_phrase

    label = obj["label"]
    thing = _thing_phrase(label)
    return thing if _names_held_item(label, held) or _names_held_item(held, label) else f"the {held} in {thing}"


def _plain_head(held: str) -> str:
    """What prose calls the item: its head noun, with the word before a
    generic one ("whistle"; "silver piece")."""
    words = re.findall(r"[a-z]+", _HEAD_NOUN_CUT.split(held or "", maxsplit=1)[0].lower())
    if not words:
        return "one"
    if words[-1] in ("piece", "pieces", "coin", "coins", "bundle", "bit", "scrap", "set") and len(words) >= 2:
        return " ".join(words[-2:])
    return words[-1]


def _authored_elsewhere_refusal(r: CommandRunner, name: str, *, verb: str) -> _Outcome | None:
    """The refusal for an @interact or @hand of an authored item the player
    never took, lying at another place: by name and by its real place, or
    None when `name` is no such item."""
    hit, here = _authored_hit(r, name, things=(verb == "interact"))
    if hit is None or hit[0] == here:
        return None
    loc_id, loc_name, obj, held = hit
    what = _what_it_is(obj, held)
    result = {"refused": "authored_elsewhere", "item": held, "location_id": loc_id, "place": loc_name}
    if verb == "hand":
        message = (f"no item in their pack called '{name}' — {what} still lies at {loc_name}, where nobody has "
                   "taken it; nothing changed hands")
    else:
        message = (f"that is {what} at {loc_name}, not here — nothing was taken and it is not in their pack; "
                   f"the player finds it with @interact there")
    return _Outcome(result, ok=False, message=message)


def _found_this_reply(r: CommandRunner, name: str) -> bool:
    """Is `name` another way of saying what @interact put in the pack this
    reply? Every word it names is the find's own ("the cracked bone whistle",
    "Bone Whistle" after the Cracked Bone Whistle) — the slug alone missed
    them, and each became a second whistle in the pack."""
    words = {w for w in re.findall(r"[a-z]+", (name or "").lower().replace("'", "").replace("’", ""))
             if len(w) >= 3 and w not in _THING_STOP}
    if not words:
        return False
    for slug in r.found_items:
        own = set(re.findall(r"[a-z]+", slug))
        if words <= own | {f"{w}s" for w in own}:
            return True
    return False


def _authored_substitute(r: CommandRunner, name: str) -> _Outcome | None:
    """A @give of an item an authored Thing still holds: taken through the
    Thing when it is here, refused anywhere else. fleet5-tamsin T12: the DM
    handed over a "Silver Coin" from the gate mud while the Hollow's split
    crate still held the Greywater Silver Piece — the authored clue never
    found, a substitute in its place."""
    hit, here = _authored_hit(r, name)
    if hit is None:
        return None
    loc_id, loc_name, obj, held = hit
    label = obj["label"]
    what = _what_it_is(obj, held)
    if loc_id != here:
        # ...and the clean way out of a find the prose already wrote
        # (fleet8-wenna T13-T20: a "cord with something pale" on a dead
        # stand-in wolf became the Hollow's whistle, refused four times)
        return _Outcome({"refused": "authored_elsewhere", "item": held, "location_id": loc_id,
                         "place": loc_name}, ok=False,
                        message=(f"that is {what} at {loc_name} — the player finds it with @interact there; "
                                 "nothing was given (never a substitute for an authored thing). If they found "
                                 "something here, it is a different, lesser thing — @give it under its own name "
                                 f"(kind: clue), never another {_plain_head(held)} — the real one still waits at "
                                 f"{loc_name}"))
    if obj["payload"].get("lock"):
        return _Outcome({"refused": "authored_locked", "item": held}, ok=False, message=(
            f"that is {what} here, locked ({_clip(obj['payload']['lock'], 100)}) — once the fiction "
            f"opens it, write @interact {label} | through; never @give what it holds"))
    # The player's own reach, as @interact's: fleet8-tamsin T15 refused
    # `@interact Cracked bone whistle` ("did not reach for it") and the next
    # line, `@give Cracked Bone Whistle`, took it through the Thing anyway
    unreached = _not_reached(r, obj)
    if unreached is not None:
        return unreached
    # ...and a look is no take (fleet9-bram T14: "see what's inside without
    # disturbing it", and the rope went in his pack)
    if _looks_only(r, obj):
        return _Outcome({"refused": "looked_only", "label": label}, ok=False, message=(
            f"the player only looked — {what} stays where it is; nothing was given. It is theirs only when "
            f"they take it (then @interact {label})"))
    # Here and open: the pickup the prose describes, through the Thing itself
    touched = _touch(r, loc_id, obj, unlocked=False)
    note = f"that is {what} here — taken through it as @interact {label} (write that for Things here)"
    return _Outcome(touched.result, ok=touched.ok,
                    message="; ".join(m for m in (note, touched.message) if m))


def _auto_journal(r: CommandRunner, entries: list[str]) -> None:
    """Engine-written journal lines: held for the reply's name mask on the
    web path, written now (deduped) where there is none."""
    entries = [e for e in entries if e]
    if not entries:
        return
    if r.journal_mask is not None:
        r.held_auto_journal.extend(entries)
        return
    try:
        _memory().add_journal_entries(r.player_id, entries)
    except CommandError as e:
        logger.warning(f"engine journal entries not kept: {e}")


def _h_take_ambiguous(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    raise CommandError(
        "@take is ambiguous and did nothing — if the player picks something UP write @give "
        "(or @interact for a thing in the scene); if they LOSE it write @remove"
    )


def _h_take(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    name, quantity = split_quantity(cmd.main)
    entry = r.inventory_item(name)
    result = remove_from_inventory(player_id=r.player_id, item_name=entry.get("name") or entry.get("id"),
                                   quantity=quantity)
    removed = result.get("quantity_removed")
    if result.get("success") and isinstance(removed, int) and removed < quantity:
        return _Outcome(result, message=f"they only had {removed} {entry.get('name')}")
    return _Outcome(result)


def _h_use(r: CommandRunner, cmd: ParsedCommand) -> dict | _Outcome:
    entry = r.inventory_item(cmd.main)
    unchosen = _use_not_chosen(r, entry)
    if unchosen is not None:
        return unchosen
    return use_item(user_id=r.player_id, item_id=entry.get("id") or prog._slug(entry.get("name", "")),
                    user_type="player")


# Using up the player's things is the player's call. fleet10-tamsin T20 ("I
# need shelter, fire if I can manage it, and time to think") had her only
# healing draught drunk "in two bitter swallows" with @use — badly hurt,
# alone at night, exactly when spending the last potion was hers to decide.
# Their words must drink, use or bandage — or name the thing or its kind
# ("the draught", "my salve"); a wound they set out to treat ("stop the
# bleeding", fleet10-bram T20) reaches for the remedy too. Saving it, a
# maybe, a "not yet" reach for nothing
_USE_REACH = re.compile(
    r"\b(?:drink|drinks|drank|drinking|sip|sips|sipping|swallow\w*|gulp\w*|quaff\w*|uncork\w*"
    r"|knock\w*\s+(?:it|the\s+[\w'’-]+)\s+back|down(?:s|ed)?\s+(?:it|the|my|half)|eat|eats|eating|chew\w*"
    r"|apply|applies|applying|rub\w*\s+(?:it|the|some)|smear\w*|bandag\w*"
    r"|(?:bind|binds|binding|dress|dresses|dressing|tend|tends|tending|clean|cleans|cleaning|wrap|wraps|wrapping"
    r"|treat|treats|treating)\s+(?:to\s+)?(?:my|the|this|that)\s+(?:[\w'’-]+\s+)?(?:wounds?|cuts?|gash(?:es)?"
    r"|bites?|knuckles|arm|leg|shoulder|side|knee|ribs|hand)"
    r"|patch\w*\s+(?:myself|me|it|them)\s+up|tend\w*\s+(?:to\s+)?myself|stop\s+the\s+bleeding|stanch\w*"
    r"|staunch\w*)\b",
    re.IGNORECASE,
)
_USE_HELD_BACK = re.compile(
    r"(?:\b(?:later|not|never|without|instead|rather|unless|if|maybe|perhaps|whether|should|could|might|would)\b"
    r"|n['’]t\b)(?:\s+[\w'’,-]+){0,3}\s*$",
    re.IGNORECASE,
)
# ...a plan for later: "If it gets worse I'll drink the draught" is no drink
# now (the held-back words above see only the few words before the verb)
_USE_IF = re.compile(r"^\s*(?:if|unless|in\s+case|should\s+(?:it|things|I|the))\b", re.IGNORECASE)
# ...and the thing itself kept back: "I keep the draught for later"
_USE_SAVED = re.compile(r"\b(?:save|saves|saving|saved|keep|keeps|keeping|kept|hoard\w*|hold(?:ing)?\s+on\s*to)\s+"
                        r"(?:[\w'’-]+\s+){0,2}$", re.IGNORECASE)
_REMEDY_WORDS = frozenset({"draught", "draughts", "draft", "potion", "potions", "tonic", "remedy", "medicine",
                           "salve", "tincture", "elixir", "vial", "flask", "bandage", "bandages", "poultice",
                           "healing"})


def _use_not_chosen(r: CommandRunner, entry: dict[str, Any]) -> _Outcome | None:
    """The refusal for an @use the player never reached for, or None. No
    words known (the CLI, tools): no check."""
    name = str(entry.get("name") or entry.get("id") or "it")
    # ...that very thing: a draught from the quick slot is no reason to
    # refuse the bread they eat now (`canvas_used`: what the canvas says they
    # drank; unknown, any canvas use counts)
    used = getattr(r, "canvas_used", None)
    already = (any(name.lower() in str(s).lower() for s in used) if used is not None
               else "use_item" in (r.code_actions or ()))
    if already:
        # They drank it on the canvas (the quick slot) before this reply: the
        # engine already applied it and [SINCE YOUR LAST TURN] says so
        return _Outcome({"refused": "already_used", "item": name}, ok=False, message=(
            f"not used twice — the player already used their {name} themselves (it is in [SINCE YOUR LAST "
            "TURN], already applied): narrate it, never @use it again"))
    if r.player_input is None:
        return None
    text = r.player_input
    if not _ENGINE_INPUT.match(text):
        words = {w for w in re.findall(r"[a-z]+", name.lower().replace("-", " ")) if len(w) >= 5
                 and w not in _THING_STOP} | _REMEDY_WORDS
        for sentence in re.split(r"(?<=[.!?…])\s+|\n+", text):
            if _USE_IF.match(sentence) and not re.search(r"\bnow\b", sentence, re.IGNORECASE):
                continue
            if any(not _USE_HELD_BACK.search(sentence[max(0, m.start() - 40):m.start()])
                   for m in _USE_REACH.finditer(sentence)):
                return None
            if any(m.group(0).lower() in words and not _USE_HELD_BACK.search(sentence[max(0, m.start() - 40):m.start()])
                   and not _USE_SAVED.search(sentence[max(0, m.start() - 40):m.start()])
                   for m in re.finditer(r"[A-Za-z]+", sentence)):
                return None
    return _Outcome({"refused": "not_chosen", "item": name}, ok=False, message=(
        f"offer it — the player did not reach for it: nothing was used, their {name} is still in their pack. "
        "Using up their things is their call: show the hurt and let them see it is there to use; it is spent "
        f"in the reply after they say they drink or use it (then @use {name})"))


def _h_hand(r: CommandRunner, cmd: ParsedCommand) -> dict | _Outcome:
    item_text, target = cmd.main, cmd.opt("to")
    if not target:
        split = _split_on(item_text, "to")
        if split:
            item_text, target = split
        elif cmd.extras:
            target = re.sub(r"^to\s+", "", cmd.extras[0], flags=re.I)
    if not target:
        raise CommandError("say who gets it: @hand <item> | to: <name>")
    name, quantity = split_quantity(item_text)
    try:
        entry = r.inventory_item(name)
    except CommandError:
        # Proof never picked up: where it really lies (fleet7-tamsin T14)
        elsewhere = _authored_elsewhere_refusal(r, _clean_label(name), verb="hand")
        if elsewhere is not None:
            return elsewhere
        raise
    npc_id, _ = r.npc(target)
    result = transfer_item(from_id=r.player_id, to_id=npc_id, item_id=entry["id"], quantity=quantity,
                           from_type="player", to_type="npc")
    # A gift, not the job's proof handed over (that ticked a step) nor a
    # clue or key passed along (`_gift_trust`)
    if not _refused(result) and not result.get("objectives_done") \
            and str(entry.get("type") or "") != "quest_item" and npc_id in _present_npc_ids(r):
        kindness = _gift_trust(r, npc_id, str(entry.get("name") or "gift"))
        if kindness is not None:
            return kindness.with_result(result)
    return result


def _h_buy(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    item_text, seller = cmd.main, cmd.opt("from")
    if not seller:
        split = _split_on(item_text, "from")
        if split:
            item_text, seller = split
        elif cmd.extras:
            seller = re.sub(r"^from\s+", "", cmd.extras[0], flags=re.I)
    if not seller:
        raise CommandError("say who sells it: @buy <item> | from: <name>")
    npc_id, npc_name = r.npc(seller)
    name, quantity = split_quantity(item_text)
    entry = r.npc_item(npc_id, name)
    from src.services import standing

    with get_session() as session:
        player = r._player_row(session)
        price = standing.price_for(session, player, session.get(NPC, npc_id), entry) * quantity
        purse = int(player.currency or 0)
    if price > purse:
        raise CommandError(f"{entry.get('name')} costs {price} coin and they carry {purse} — no sale")
    result = transfer_item(from_id=npc_id, to_id=r.player_id, item_id=entry["id"], quantity=quantity,
                           from_type="npc", to_type="player", is_purchase=True)
    if result.get("error") == "Not enough quantity":
        return _Outcome(result, ok=False, message=f"{npc_name} does not have {quantity} of {entry.get('name')}")
    return _Outcome(result)


_STOCK_KIND_WORDS = {"weapon": "weapon", "armor": "armor", "armour": "armor", "trinket": "trinket",
                     "potion": "consumable", "consumable": "consumable", "draught": "consumable"}


def _stock_entry(segment: str) -> dict[str, Any]:
    if ":" not in segment:
        raise CommandError(f"stock line '{segment}' needs the form 'Name: weapon common' or 'Name: potion heal_light'")
    name, spec = (part.strip() for part in segment.rsplit(":", 1))
    if not name:
        raise CommandError(f"stock line '{segment}' has no name")
    kind = tier = tag = None
    count = 1
    for token in spec.lower().replace(",", " ").split():
        qty = re.fullmatch(r"[x×](\d+)|(\d+)[x×]?", token)
        if qty:
            count = int(qty.group(1) or qty.group(2))
        elif token in _STOCK_KIND_WORDS:
            kind = _STOCK_KIND_WORDS[token]
        elif token in prog.TIER_LABELS:
            tier = token
        elif token in prog.CONSUMABLES:
            tag = token
        else:
            raise CommandError(f"stock line '{segment}': '{token}' is not a kind, tier or potion word")
    if kind is None:
        kind = "consumable" if tag else None
    if kind is None:
        raise CommandError(f"stock line '{segment}' needs a kind: weapon, armor, trinket or potion")
    entry: dict[str, Any] = {"kind": kind, "name": name, "count": max(1, count)}
    if kind == "consumable":
        entry["effect_tag"] = tag or "heal_light"
    else:
        entry["tier"] = tier or "common"
    return entry


def _h_stock(r: CommandRunner, cmd: ParsedCommand) -> dict:
    npc_id, _ = r.npc(cmd.main)
    entries = [_stock_entry(seg) for seg in cmd.extras]
    if not entries:
        raise CommandError("list the stock: @stock <name> | Ashwood Blade: weapon common | Tonic: potion heal_light x3")
    return stock_merchant(npc_id=npc_id, entries=entries)


# -- the things in the scene -------------------------------------------------

# Verbs the DM puts in front of the thing ("search the crate") are not its
# name, nor is how the dead lie ("the dead wolf" is "Hollow Wolf's body")
_OBJECT_STOP = _PLAIN_STOP | {"search", "read", "open", "take", "loot", "examine", "check",
                              "dead", "fallen", "slain"}
# A body is labelled "<name>'s body" (src/services/loot.py); prose calls it other things
_BODY_WORDS_RE = re.compile(r"\b(?:corpse|carcass|remains|cadaver)\b", re.I)
_SPENT = {"take": "already taken", "open": "already opened", "search": "already searched",
          "body": "already searched"}


def _object_match(label: str, rows: list[dict[str, Any]]) -> tuple[Any, list]:
    """`_match_tier` over objects; a "corpse" is retried as a "body" only when
    the words as written name nothing (an authored "Ancient remains" stays itself)."""
    hit, ambiguous = _match_tier(label, rows, lambda o: o["label"], _OBJECT_STOP)
    said = _BODY_WORDS_RE.sub("body", label)
    if hit is None and not ambiguous and said != label:
        hit, ambiguous = _match_tier(said, rows, lambda o: o["label"], _OBJECT_STOP)
    return hit, ambiguous


def _scene_object(r: CommandRunner, label: str) -> tuple[str, dict[str, Any]]:
    """(location id, the object) for a label naming one thing where the player stands."""
    with get_session() as session:
        player = r._player_row(session)
        here = session.get(Location, player.current_location_id) if player.current_location_id else None
        if here is None:
            raise CommandError("the player is not anywhere yet")
        location_id, place_name = here.id, here.name
        objects = normalize_objects(here.objects)
    if not objects:
        raise CommandError(f"there is nothing at {place_name} to search, take, open or read")
    wanted = label.strip("[]").strip()
    hit = next((o for o in objects if o["id"] == wanted), None)
    # The untouched first: "the crate" is the one still worth opening
    for rows in ([o for o in objects if not o["consumed"]], objects):
        if hit is not None:
            break
        hit, ambiguous = _object_match(label, rows)
        # Same-named things (two bodies of two "Grey Wolf"s) are the same to
        # whoever wrote the line; any of them is the one they meant
        if ambiguous and len({o["label"].lower() for o in ambiguous}) == 1:
            hit = ambiguous[0]
        elif ambiguous:
            raise CommandError(f"'{label}' could be {', '.join(o['label'] for o in ambiguous)} — name one")
    if hit is None:
        raise CommandError(f"nothing at {place_name} is called '{label}' — the things here: "
                           + ", ".join(o["label"] for o in objects))
    return location_id, hit


def _found(result: dict, payload: dict[str, Any]) -> str:
    """What a touch turned up, for the DM who wrote the line blind.

    With each find's own description: on the seed the item IS the clue
    ("Split down the barrel — silent now. The same make as the one Captain
    Coldiron never mentions."), and the pack shows the DM only its name."""
    inner = result.get("result") or {}
    held = [payload["item"]] if payload.get("item") else list(payload.get("items") or [])
    about = {str(i.get("name")): str(i.get("description") or "").strip() for i in held if isinstance(i, dict)}
    found = []
    for item in inner.get("items_gained") or []:
        name = str(item.get("name") or "")
        if not name:
            continue
        note = name + (f" x{item['quantity']}" if (item.get("quantity") or 1) > 1 else "")
        found.append(note + (f" ({_clip(about[name], 160)})" if about.get(name) else ""))
    if inner.get("coin_gained"):
        found.append(f"{inner['coin_gained']} coin")
    parts = [f"found {', '.join(found)} — in their pack now"] if found else []
    # Written words are the thing's own, whole: fleet6-wenna T17 invented a
    # letter's contents (three sealed truths) where the authored text was
    # short, and a clipped quote invites the same
    document = inner.get("document") if isinstance(inner.get("document"), dict) else {}
    text = payload.get("text") if document else inner.get("panel_text")
    if text and result.get("kind") != "body":       # a body's text only lists the finds
        if result.get("kind") == "read":
            parts.append(f'it reads: "{text}" — quote this; never add to it')
        else:
            parts.append(f'what they saw: "{_clip(text, 220)}"')
    if document.get("text"):
        parts.append(f'the {document.get("item") or "document"} reads: "{document["text"]}" — quote this '
                     "word for word; never add to it or say more than it says")
    return "; ".join(parts) or "nothing worth taking"


# Taking and searching are the player's own acts. fleet7-tamsin T11 walked
# to Piper and asked a question; the reply had her stoop for a loose bundle
# of sweetroot "and pocket it without breaking stride" with `@interact
# Dropped sweetroot bundle` — a pickpocket's theft played for her, off
# screen, no risk. A take/search (or a body) needs the player's words to
# reach for something, or to name the thing; reading and opening stay the
# DM's to resolve, and a locked thing's `| through` is a different matter.
_REACH_KINDS = ("take", "search", "body")
_REACHES = re.compile(
    r"\b(?:take|takes|taking|took|grab\w*|snatch\w*|pocket\w*|palm(?:s|ed|ing)?|pilfer\w*|filch\w*"
    r"|swip(?:e|es|ed|ing)|steal\w*|stole|lift(?:s|ed|ing)?|nick(?:s|ed|ing)?|pinch(?:es|ed|ing)?|scoop\w*"
    r"|collect\w*|gather\w*|retriev\w*|salvag\w*|stash\w*|loot\w*|keep\s+it|help\s+myself"
    r"|pick(?:s|ed|ing)?\s+(?:\w+\s+){0,3}?up|search\w*|rummag\w*|rifl(?:e|es|ed|ing)|frisk\w*|sift\w*"
    r"|dig(?:s|ging)?\s+(?:through|in|into)|go(?:es|ing)?\s+through|went\s+through"
    r"|look(?:s|ed|ing)?\s+(?:in|inside|into|through|under|for|around\s+for)|check(?:s|ed|ing)?"
    r"|inspect\w*|examin\w*|pry|pries|pried|prying|empt(?:y|ies|ied|ying)|turn(?:s|ed|ing)?\s+out"
    r"|feel(?:s|ing)?\s+(?:for|inside|around\s+(?:for|in))|reach(?:es|ed|ing)?\s+(?:for|into|in|down)"
    # ...and getting it open or out (fleet9-bram T14 "fish the sweetroot out
    # of the mud", the satchel's buckle worked loose)
    # (the verb, never "the crate lies open")
    r"|(?<=\bI\s)open\w*|open(?:s|ing)?\s+(?:it|them|the|that|this|its|his|her|their)"
    r"|un(?:buckl\w*|fasten\w*|ti(?:e|es|ed)|tying|lac(?:e|es|ed|ing)|wrap\w*|do(?:es|ne|ing)?|latch\w*|cork\w*"
    r"|knot\w*)"
    r"|(?:fish|draw|pull|work|ease|prise|prize|flip|tug|wrench|wriggl|jimm)\w*\s+(?:[\w'’-]+\s+){0,3}?"
    r"(?:out|loose|open|free|off))\b",
    re.IGNORECASE,
)
_QUOTED_WORDS = re.compile(r"[\"“][^\"”\n]*[\"”]")
_THING_STOP = frozenset({"the", "a", "an", "of", "on", "in", "old", "some", "small", "large", "loose", "near",
                         "by", "with", "and", "its", "his", "her", "their"})
# A reach counts for THIS thing. fleet10-bram T14 "trying to get a better
# angle on that metallic glint in the crate" put the crate's silver in his
# pack (the crate named, nothing reached for), and T15 "I pocket the coin…
# edge my body toward the doorframe" took the whistle on the coin's verb.
# The reach's own sentence names the thing (its lid, buckle or inside, for a
# container), or its "it" points back at a sentence that did, or it searches
# the place at large naming no other thing. Named with no reach, the thing
# is looked at: what it holds is seen and left where it lies
_SEARCH_VERB = re.compile(r"(?:search|rummag|comb|sift|look|go|went|dig|rifl)", re.IGNORECASE)
# ...and not undone ("before I decide if I'm taking it" — fleet6-tamsin T19)
_REACH_NEGATED = re.compile(r"(?:\b(?:not|never|without|nor|if|whether|unless)\b|n['’]t\b)(?:\s+[\w'’-]+){0,3}\s*$",
                            re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")


def _thing_words(obj: dict[str, Any]) -> set[str]:
    """The words that name a Thing: its label's and what it holds (and, for
    a container, its lid, buckle or inside)."""
    payload = obj.get("payload") or {}
    held = (payload.get("item") or {}).get("name") if isinstance(payload.get("item"), dict) else ""
    words = {word for name in (obj.get("label") or "", held or "")
             for word in re.findall(r"[a-z]+", str(name).lower().replace("'", "").replace("’", ""))
             if len(word) >= 4 and word not in _THING_STOP}
    if obj.get("kind") in ("open", "search"):
        words |= {"lid", "inside", "buckle", "flap", "contents"}
    return words


def _names_thing(text: str, words: set[str]) -> bool:
    for word in re.findall(r"[a-z]+", (text or "").lower().replace("'", "").replace("’", "")):
        if word in words or (word.endswith("s") and word[:-1] in words) or (
                word.endswith("ves") and f"{word[:-3]}f" in words) or (
                word.endswith("ies") and f"{word[:-3]}y" in words):
            return True
    return False


def _reach_verdict(player_input: str, obj: dict[str, Any], others: list[dict[str, Any]] = ()) -> str:
    """"reach" (the player's words take or search this thing), "named" (they
    name it, reaching for nothing — a look) or "none". `others`: the other
    Things here, which a search of the place at large must not name."""
    text = str(player_input or "")
    # What they DO is their narration; "…sent me to find proof" is talk
    narration = _QUOTED_WORDS.sub(" ", text)
    words = _thing_words(obj)
    other_words = set().union(*(_thing_words(o) for o in others if o.get("id") != obj.get("id"))) - words
    named_before, before = False, ""
    for sentence in _SENTENCE_SPLIT.split(narration):
        named = _names_thing(sentence, words)
        for m in _REACHES.finditer(sentence):
            if _REACH_NEGATED.search(sentence[max(0, m.start() - 30):m.start()]):
                continue
            if named:
                return "reach"
            # "I see the whistle. I pick it up" — or "I pick it up" about the
            # thing the last reply showed, when nothing else here was named
            pronoun = re.search(r"\b(?:it|them)\b", m.group(0), re.IGNORECASE) or re.match(
                r"\s+(?:\w+ly\s+)?(?:it|them)\b", sentence[m.end():], re.IGNORECASE)
            if pronoun and (named_before or not _names_thing(before + " " + sentence[:m.start()], other_words)):
                return "reach"
            # "I search the square for anything dropped in the rush"
            if _SEARCH_VERB.match(m.group(0)) and not _names_thing(sentence, other_words) \
                    and obj.get("kind") in ("take", "search", "open"):
                return "reach"
        named_before, before = named, sentence
    return "named" if _names_thing(text, words) else "none"


def _reached_for(player_input: str, obj: dict[str, Any]) -> bool:
    """Did the player's words take or search this thing? (fleet7-wenna T11
    "I pick it up carefully… I pocket it" took the whistle; fleet7-tamsin
    T11's walk to Piper took nothing)"""
    return _reach_verdict(player_input, obj) == "reach"


# The engine's own input ("[OPENING] …", "[COMBAT OUTCOME — …") is nobody
# reaching for anything: the aftermath text says "a body … the player can
# search", and fleet8-bram T5 had the whistle taken in an aftermath whose
# player had last said "examine the scrap without touching it yet"
_ENGINE_INPUT = re.compile(r"^\s*\[[A-Z][A-Z_'-]*(?: [A-Z][A-Z_'-]*)*(?:\]|\s*[—–:-])")


def _reach_of(r: CommandRunner, obj: dict[str, Any], others: list[dict[str, Any]] = ()) -> str | None:
    """How the player's words stand to a take or search (an "open" thing that
    holds an item is a take too: the seed's satchel puts its letter in the
    pack): `_reach_verdict`'s word, or None when there is nothing to check —
    not a take, a spent thing, no words known (the CLI, tools). An engine
    input reaches for nothing."""
    takes = obj["kind"] in _REACH_KINDS or (
        obj["kind"] == "open" and isinstance((obj.get("payload") or {}).get("item"), dict))
    if not takes or r.player_input is None or obj["consumed"]:
        return None
    if _ENGINE_INPUT.match(r.player_input):
        return "none"
    return _reach_verdict(r.player_input, obj, others)


def _not_reached(r: CommandRunner, obj: dict[str, Any]) -> _Outcome | None:
    """The not_reached_for refusal for a take or search the player's own
    words never reached for (`_reach_of`), else None."""
    if _reach_of(r, obj) in (None, "reach"):
        return None
    return _unreached(obj)


def _unreached(obj: dict[str, Any]) -> _Outcome:
    return _Outcome({"refused": "not_reached_for", "label": obj["label"]}, ok=False, message=(
        f"the player did not reach for it — put it in view and stop. Nothing was taken: the "
        f"{obj['label']} is still there. Show it where it lies and end on their choice; it is theirs "
        f"only in the reply after they say they take or search it (then @interact {obj['label']})"))


def _h_interact(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    """Touch one of this scene's objects through the canvas's own resolver.

    2026-10-04: the Hollow's authored proof (a cracked bone whistle, the
    split crate's Greywater silver) sat untouched while the DM invented
    substitutes — a typed "I search the crate" had no way to reach the object
    the canvas's [E] would have opened."""
    label = _clean_label(cmd.main)
    if not label:
        raise CommandError("name the thing: @interact <one of the things here>")
    try:
        location_id, obj = _scene_object(r, label)
    except CommandError as e:
        # The proof is not here: say so by name and by its real place, as
        # @give's refusal does. fleet8-wenna T18/T20: "there is nothing at
        # Stable Yard to search" never said the whistle lay at the Hollow,
        # and the prose kept it in the yard's mud
        elsewhere = _authored_elsewhere_refusal(r, label, verb="interact")
        if elsewhere is not None:
            return elsewhere
        raise e
    if obj["kind"] == "rest":
        # A bed is a night's sleep: the same charge-once, once-a-reply path as @rest
        return _h_rest(r, cmd)
    shut = bool(obj["payload"].get("lock")) and not cmd.flag("through")
    with get_session() as session:
        place = session.get(Location, location_id)
        others = normalize_objects(place.objects) if place is not None else []
    reach = _reach_of(r, obj, others)
    if reach == "named" and isinstance(obj["payload"].get("item"), dict) and not shut:
        # Named, reached for nothing (fleet10-bram T14 "a better angle on that
        # metallic glint in the crate"): a look — what it holds is seen
        return _look_without_taking(r, location_id, obj)
    if reach in ("named", "none"):
        return _unreached(obj)
    # Opened only to look: what is inside is seen, journaled, and left where
    # it lies (fleet9-bram T14: "ease the lid open just enough to see what's
    # inside without disturbing it" put the crate's rope in his pack)
    if _looks_only(r, obj) and not shut:
        return _look_without_taking(r, location_id, obj)
    # `| through` is the DM saying the fiction opened a lock (the owner's
    # leave, the key, a good roll) — as @move's is for a blocked road. Without
    # it a locked thing could never give up what it holds: the seed's satchel
    # letter (a quest's first step) was reachable only by an invented @give
    return _touch(r, location_id, obj, unlocked=cmd.flag("through"))


def _looks_only(r: CommandRunner, obj: dict[str, Any]) -> bool:
    """A take, open or search of a Thing holding an item, on a turn whose
    player only meant to look at it (`intents.look_only`). No words known
    (the CLI, tools) or an engine input: no."""
    item = (obj.get("payload") or {}).get("item")
    if obj["kind"] not in ("take", "open", "search") or not isinstance(item, dict) or obj["consumed"]:
        return False
    if r.player_input is None or _ENGINE_INPUT.match(r.player_input):
        return False
    from src.services.intents import look_only

    return look_only(r.player_input, obj["label"], str(item.get("name") or ""),
                     container=obj["kind"] in ("open", "search"))


def _look_without_taking(r: CommandRunner, location_id: str, obj: dict[str, Any]) -> _Outcome:
    """What the Thing shows and holds, seen and journaled, nothing taken:
    the Thing is not spent, so the reply after the player reaches for it
    takes it the ordinary way."""
    payload = obj.get("payload") or {}
    item = payload.get("item") or {}
    name = str(item.get("name") or "")
    about = _clip(str(item.get("description") or "").strip(), 160)
    text = str(payload.get("text") or "").strip()
    # A thing lying loose (the whistle at the threshold) is no container
    inside = obj["kind"] in ("open", "search")
    seen = f"{'Inside' if inside else 'There'}: {name}" + (f" — {about}" if about else "")
    result = {
        "success": True, "object_id": obj["id"], "kind": obj["kind"], "label": obj["label"],
        "consumed": False, "looked": True,
        "result": {"floater": None, "panel_title": obj["label"],
                   "panel_text": f"{text}\n\n{seen}" if text else seen,
                   "items_gained": [], "coin_gained": 0},
    }
    _auto_journal(r, [(f"Saw {name} in the {obj['label'].lower()}" if inside else f"Saw {name} lying there")
                      + " and left it there" + (f" — {about}" if about else "")])
    shown = f'what they saw: "{_clip(text, 220)}"; ' if text else ""
    where = f"the {obj['label']} still holds it" if inside else "it lies where it was"
    return _Outcome(result, message=(
        f"{shown}{'inside' if inside else 'there'}: {name}" + (f" ({about})" if about else "")
        + f" — the player only looked, so it was left where it lies: nothing taken, not in their pack, {where}. "
          f"Show it in place; it is theirs when they take it (then @interact {obj['label']} again)"))


def _touch(r: CommandRunner, location_id: str, obj: dict[str, Any], *, unlocked: bool) -> _Outcome:
    """Resolve one Thing here (`resolve_interaction`, the canvas's own path)
    and journal what it showed and held."""
    result = resolve_interaction(r.player_id, location_id, obj["id"], unlocked=unlocked)
    if result.get("locked"):
        return _Outcome(result, ok=False, message=(
            f"{obj['label']}: locked ({_clip(result.get('lock_hint') or 'it will not give', 120)}) — it did not "
            f"open; once the fiction opens it, write @interact {obj['label']} | through"))
    if result.get("spent"):
        return _Outcome(result, ok=False, message=(
            f"{obj['label']}: {_SPENT.get(obj['kind'], 'already used')} — there is nothing more in it"))
    if not result.get("success"):
        return _Outcome(result, ok=False, message=_engine_reason(result))
    gained = [i for i in (result.get("result") or {}).get("items_gained") or [] if isinstance(i, dict)]
    for item in gained:
        if item.get("name"):
            r.found_items[prog._slug(str(item["name"]))] = obj["label"]
    # ...remembered for one reply: "leave the rope where it is" puts it back
    # (fleet9-bram T15; src/services/intents.py)
    if gained and obj["kind"] != "body":
        from src.services.intents import note_taken

        note_taken(r.player_id, location_id, obj["id"], obj["label"], gained)
    # What it read and what was in it go in their journal — round 4 kept
    # 0-1 entries a session while the DM read notices and opened crates
    try:
        _auto_journal(r, _memory().interaction_journal_entries(result, obj["payload"]))
    except Exception as e:     # the touch happened: a journal hiccup must not report it failed
        logger.warning(f"interaction not journaled: {e}")
    return _Outcome(result, message=_found(result, obj["payload"]))


# -- people ------------------------------------------------------------------

def _number_from(cmd: ParsedCommand) -> tuple[str, int | None]:
    """The main's trailing number (or the first extra's), and the rest."""
    label, number = split_trailing_number(cmd.main)
    if number is None:
        for extra in cmd.extras:
            if re.fullmatch(r"\s*[+\-−–]?\s?\d+\s*", extra):
                return cmd.main, parse_signed(extra)
    return label, number


def _h_rep(r: CommandRunner, cmd: ParsedCommand) -> dict | _Outcome:
    label, delta = _number_from(cmd)
    if not delta:
        raise CommandError("write the change as +N or -N after the faction, e.g. @rep Harbor Watch -10")
    faction_id, faction_name = r.faction(label)
    paid = r.paid_rep.get(faction_id)
    if paid and (paid > 0) == (delta > 0):
        # The same way the reward already moved them; a slight after a
        # payday (the other sign) is a new thing and still lands
        return _Outcome({"skipped": "already_paid", "faction_id": faction_id, "delta": delta}, ok=False,
                        message=f"not paid twice — @quest-done already moved {faction_name} by {paid:+d}")
    return update_player_reputation(player_id=r.player_id, faction_id=faction_id, delta=delta)


def _h_trust(r: CommandRunner, cmd: ParsedCommand) -> dict:
    label, delta = _number_from(cmd)
    words = [e for e in cmd.extras if not re.fullmatch(r"\s*[+\-−–]?\s?\d+\s*", e)]
    disposition = (cmd.opt("disposition") or (words[0] if words else "")).strip().lower()
    if delta is None and not disposition:
        raise CommandError("write the change as +N or -N, e.g. @trust Piper +10 | friendly")
    npc_id, _ = r.npc(label)
    _open_relationship(r, npc_id)
    if delta and delta > 0 and npc_id in r.paid_trust:
        # The job's thanks is already in their trust; a new mood still lands
        from src.tools.world_write.quests import QUEST_GIVER_TRUST

        paid = f"not paid twice — @quest-done already raised their trust by +{QUEST_GIVER_TRUST} for the job"
        if not disposition:
            return _Outcome({"skipped": "already_paid", "npc_id": npc_id, "delta": delta}, ok=False, message=paid)
        return _Outcome(update_npc_relationship(npc_id=npc_id, player_id=r.player_id, trust_delta=0,
                                                new_disposition=_clip(disposition, 30)),
                        message=f"the mood landed; {paid}")
    if delta and delta > 0 and r.trust_raised.get(npc_id) == "gift":
        # The gift's kindness is already in their trust (`_gift_trust`)
        gift = "not counted twice — the engine already raised their trust for the gift this reply"
        counted = r.gift_trust.get(npc_id, 0)
        if delta > counted:
            # ...but a bigger change is more than the gift: a won persuasion
            # roll in the same reply (the dossier's within-reach line names
            # its @trust) still lands, less what the gift already added
            r.gift_trust[npc_id] = delta
            return _Outcome(update_npc_relationship(npc_id=npc_id, player_id=r.player_id,
                                                    trust_delta=delta - counted,
                                                    new_disposition=_clip(disposition, 30) or None),
                            message=f"+{delta - counted} landed — the gift already added +{counted} of it")
        if not disposition:
            return _Outcome({"skipped": "already_raised", "npc_id": npc_id, "delta": delta}, ok=False, message=gift)
        return _Outcome(update_npc_relationship(npc_id=npc_id, player_id=r.player_id, trust_delta=0,
                                                new_disposition=_clip(disposition, 30)),
                        message=f"the mood landed; {gift}")
    if delta and delta > 0:
        r.trust_raised.setdefault(npc_id, "trust")
    return update_npc_relationship(npc_id=npc_id, player_id=r.player_id, trust_delta=delta or 0,
                                   new_disposition=_clip(disposition, 30) or None)


def _open_relationship(r: CommandRunner, npc_id: str) -> None:
    """Trust and secrets live on the relationship row, which is not an
    introduction any more (dialogue: who knows whom). A stranger's row is
    written here, their name still unknown to the player, so the change
    lands — `update_npc_relationship` would write one that knows both names.
    fleet6-bram T14 / fleet6-tamsin T4: `@trust Hesk Bramble -5` and `@trust
    Sorrel Tack -5` were held for an introduction that never came, and the
    cost of a failed roll was lost."""
    from src.services.dialogue import ensure_relationship

    with get_session() as session:
        ensure_relationship(session, r.player_id, npc_id)
        session.commit()


def _h_mood(r: CommandRunner, cmd: ParsedCommand) -> dict:
    if cmd.extras:
        label, mood = cmd.main, cmd.extras[0]
    else:
        label, _, mood = cmd.main.rpartition(" ")
    if not label.strip() or not mood.strip():
        raise CommandError("write @mood <name> | <mood>")
    npc_id, _ = r.npc(label)
    return update_npc_mood(npc_id=npc_id, new_mood=_clip(mood.strip().lower(), 40))


def _h_reveal(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    label, index = _number_from(cmd)
    npc_id, npc_name = r.npc(label)
    from src.models import NPCRelationship

    with get_session() as session:
        npc = session.get(NPC, npc_id)
        secrets = npc.secrets if npc is not None else None
        rel = session.query(NPCRelationship).filter_by(npc_id=npc_id, player_id=r.player_id).first()
        told = set((rel.revealed_secrets or []) if rel else [])
    # reveal_secret indexes the column as a list; anything else is not tellable
    if not isinstance(secrets, list) or not secrets:
        raise CommandError(f"{npc_name} has no secrets to tell")
    _open_relationship(r, npc_id)
    if index is None:
        hidden = [i for i in range(len(secrets)) if i not in told]
        if not hidden:
            raise CommandError(f"{npc_name} has no secret left to tell")
        index = hidden[0]
    result = reveal_secret(npc_id=npc_id, player_id=r.player_id, secret_index=abs(index))
    if result.get("refused"):
        return _Outcome(result, ok=False, message=(
            f"{npc_name} does not trust them enough to say it — nothing was revealed; "
            "play the words they swallow"))
    if result.get("error") == "Secret index out of range":
        return _Outcome(result, ok=False, message=f"{npc_name} has no secret [{index}]")
    return _Outcome(result)


def _h_join(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    npc_id, _ = r.npc(cmd.main)
    result = join_party(player_id=r.player_id, npc_id=npc_id)
    if result.get("error") == "already":
        return _Outcome(result, ok=True)
    if result.get("error"):
        return _Outcome(result, ok=False, message=f"refused — {_clip(result.get('message') or result['error'], 220)}")
    return _Outcome(result)


def _h_leave(r: CommandRunner, cmd: ParsedCommand) -> dict:
    npc_id, _ = r.npc(cmd.main)
    return leave_party(player_id=r.player_id, npc_id=npc_id, reason=_first(cmd, "reason"))


def _h_note(r: CommandRunner, cmd: ParsedCommand) -> dict:
    text = cmd.rest_text()
    if not text:
        raise CommandError("write @note <name> | <what they now know or believe>")
    if _names_the_player(r, cmd.main):
        # A note on the player character is a fact THEY learned: the journal
        _memory().add_journal_entry(r.player_id, text)
        return {"success": True, "journal": text}
    npc_id, npc_name = r.npc(cmd.main)
    _memory().add_npc_note(r.player_id, npc_id, text)
    return {"success": True, "npc_id": npc_id, "npc_name": npc_name, "note": text}


def _names_the_player(r: CommandRunner, label: str | None) -> bool:
    words = set(re.findall(r"[a-z']+", (label or "").lower()))
    if not words or words & {"me", "player", "journal", "myself"}:
        return bool(words)
    with get_session() as session:
        name = (r._player_row(session).name or "").lower()
    return bool(name) and (words <= set(re.findall(r"[a-z']+", name)))


def _h_journal(r: CommandRunner, cmd: ParsedCommand) -> dict | _Outcome:
    text = " | ".join(p for p in [cmd.main, cmd.rest_text()] if p).strip()
    if not text:
        raise CommandError("write @journal <a fact the character has learned>")
    if r.journal_mask is not None:
        r.held_journal.append(text)         # written by settle_introductions
        r.held_journal_at.append(len(r.results))     # the index this line's result will have
        return {"success": True, "journal": text}
    said = _write_journal(r, text, None, [])
    if said:
        return _Outcome({"success": True, "journal": text, "skipped": "already_journaled"},
                        message=_journal_skip_note(said))
    return {"success": True, "journal": text}


# A @journal line is checked against the engine's own entries from this
# reply and the newest few already kept
JOURNAL_LOOKBACK = 4


def _write_journal(r: CommandRunner, text: str, index: int | None, engine_entries: list[str]) -> str | None:
    """Keep one @journal entry unless the journal already says it, and then
    report the skip on that line's result (`index` in `r.results`). Returns
    the entry that already says it."""
    memory = _memory()
    said = memory.journal_repeats(text, list(engine_entries)
                                  + memory.read_journal(r.player_id, limit=JOURNAL_LOOKBACK))
    if not said:
        memory.add_journal_entry(r.player_id, text)
        return None
    if index is not None and 0 <= index < len(r.results):
        old = r.results[index]
        r.results[index] = CommandResult(line=old.line, verb=old.verb, ok=True, tool=old.tool,
                                         result={**(old.result or {}), "skipped": "already_journaled"},
                                         message=_journal_skip_note(said))
    return said


def _journal_skip_note(said: str) -> str:
    entry = re.sub(r"^\s*\([^)]*\)\s*", "", said)
    return f'not written — the journal already says it ("{_clip(entry, 90)}"); never journal one fact twice'


def _h_plan(r: CommandRunner, cmd: ParsedCommand) -> dict:
    text = " | ".join(p for p in [cmd.main, cmd.rest_text()] if p).strip()
    if not text:
        raise CommandError("write @plan <the thread you mean to pay off>")
    _memory().add_dm_note(r.player_id, text)
    return {"success": True, "note": text}


def _memory():
    """Imported per call: the memory layer is optional and may be missing."""
    try:
        return importlib.import_module("src.services.memory_files")
    except Exception as e:
        logger.warning(f"memory_files unavailable: {e}")
        raise CommandError("memory is unavailable right now — the note was not kept")


# -- fights ------------------------------------------------------------------

def answers_combat_outcome(player_input: str | None) -> bool:
    """Is this turn's input the engine's own [COMBAT OUTCOME] report?
    (`combat.combat_outcome_message` writes "[COMBAT OUTCOME — resolved …".)"""
    return (player_input or "").lstrip().upper().startswith("[COMBAT OUTCOME")


# An aftermath is not a new fight. fleet4-bram T7: the reply to the wolves'
# [COMBAT OUTCOME] wrote `@boss Aldric Vane` + `@fight Aldric Vane, Vex
# Marrow` — a third turn with no player decision, a certain loss for a winded
# level-1 character, and the once-ever boss intro spent on it
_AFTERMATH_REFUSAL = (
    "refused — this reply answers a fight that just ended, and no new one starts in it. End on the "
    "threat (who steps forward, what they raise, what they say) and let the player answer; if they "
    "push, @fight next turn")


def _h_fight(r: CommandRunner, cmd: ParsedCommand) -> dict | _Outcome:
    if r.after_combat:
        return _Outcome({"refused": "aftermath"}, ok=False, message=_AFTERMATH_REFUSAL)
    labels = [p.strip() for p in re.split(r",|\s&\s", cmd.main) if p.strip()]
    labels += [e for e in cmd.extras if e.strip()]
    if not labels:
        raise CommandError("name who they fight: @fight <name>[, <name>]")
    ids: list[str] = []
    for label in labels:
        try:
            found = [r.npc(label)[0]]
        except CommandError:
            parts = [p.strip() for p in re.split(r"\s+and\s+", label) if p.strip()]
            if len(parts) < 2:
                raise
            found = [r.npc(p)[0] for p in parts]
        ids += [i for i in found if i not in ids]
    held_back = _story_villains_held_back(r, ids)
    ids = [i for i in ids if i not in held_back]
    # Nobody the story holds offstage is there to fight, and the story's own
    # beasts meet the player through their pack before their scene (round 9)
    offstage = _offstage_named(r, ids)
    ids = [i for i in ids if i not in offstage]
    if offstage and not ids:
        return _Outcome({"refused": "offstage", "held_back": list(offstage.values())}, ok=False, message=(
            f"{', '.join(offstage.values())} is not on stage — a later beat is their entrance; nobody fights "
            "them before it. Write signs of them only (a shape gone when looked for), never a fight"))
    stood_in = _story_beasts_stood_in(r, ids)
    ids = [stood_in[i][0] if i in stood_in else i for i in ids]
    result = start_combat(player_id=r.player_id, npc_ids=ids)
    # A held @boss entrance plays first (the tracker emits a result's
    # "cinematic" ahead of its combat_start), unless the engine already
    # attached one for an authored boss
    entrance = next((i for i in ids if i in r.pending_boss), None)
    if entrance and isinstance(result, dict) and not result.get("error") and not result.get("cinematic"):
        cine = play_cinematic(player_id=r.player_id, kind="boss_intro", npc_id=entrance,
                              title=r.pending_boss[entrance])
        if isinstance(cine, dict) and not cine.get("rejected"):
            result["cinematic"] = {k: cine.get(k) for k in
                                   ("kind", "title", "subtitle", "npc_id", "mood", "duration_ms")}
    for i in ids:
        r.pending_boss.pop(i, None)
    notes: list[str] = []
    if isinstance(result, dict) and not result.get("error"):
        if stood_in:
            result["stood_in"] = {name: stand for _i, name, stand in stood_in.values()}
            notes.append("; ".join(
                f"{name} is the story's own (a later scene needs it): {stand}, one of its pack, came at the player "
                f"in its place — the fight is with {stand}. Write {name} hanging back and slipping off out of "
                "reach, never hurt or dead" for _i, name, stand in stood_in.values()))
        if offstage:
            result["offstage"] = list(offstage.values())
            notes.append(f"{', '.join(offstage.values())} is not on stage (a later beat is their entrance) and "
                         "took no part — signs of them only")
    if held_back and isinstance(result, dict) and not result.get("error"):
        names = ", ".join(held_back.values())
        result["held_back"] = list(held_back.values())
        notes.insert(0, f"{names} did not join this fight — the story's own fight with them comes later; "
                        "they watch, call the pack, or withdraw. Write them doing that")
    if notes:
        return _Outcome(result, message=". ".join(notes))
    return result


# Round 9 (fleet10-bram T16, fleet10-tamsin T13): after a lost stand-off the
# DM wrote `@fight Gray-Flank Wolf, Hollow Wolf` and killed the drilled pack
# that act 3's "The Pack Answers" and the finale stage ("Vane stands among
# the drilled pack — Hollow Wolf and Gray-Flank Wolf"). The encounter
# director already springs a stand-in of the pack for the story's beasts
# (encounters._stand_in); a fight the DM starts does the same: before their
# scene the player fights one of their pack, same look and tier, and the
# named ones slip off. Why not let them die and re-cast the later beats?
# Their directives, keywords and the finale's "drilled pack" would all point
# at the dead, and a re-cast pack would have to appear from nowhere when the
# beat fires; a stand-in keeps every authored scene as written, at the cost
# of one more wolf of the same pack — a cost the director already pays.
def _story_beasts_stood_in(r: CommandRunner, ids: list[str]) -> dict[str, tuple[str, str, str]]:
    """{a story beast's id: (its stand-in's id, its name, the stand-in's
    name)} for each of `ids` a beat still to come needs
    (`campaign.beasts_kept_for_later`). Never raises."""
    if not ids:
        return {}
    try:
        from src.services.campaign import beasts_kept_for_later
        from src.services.encounters import _stand_in

        with get_session() as session:
            kept = beasts_kept_for_later(session, r.player_id) & set(ids)
            player = session.get(Player, r.player_id) if kept else None
            out: dict[str, tuple[str, str, str]] = {}
            taken = set(ids)
            for npc_id in [i for i in ids if i in kept]:
                like = session.get(NPC, npc_id)
                # (one not here is no fight at all: start_combat says so)
                if like is None or player is None or not player.current_location_id \
                        or like.current_location_id != player.current_location_id:
                    continue
                # A packmate already standing here (an earlier stand-in the
                # player fled from) takes the fight before a new one is made:
                # every @fight on the named wolves would otherwise add a wolf
                # to the world, and a sprite to draw (round-9 review)
                stand = _packmate_here(session, r.player_id, player, like, taken)
                if stand is None:
                    stand = _stand_in(session, player, like)
                    stand.danger_tier = like.danger_tier or stand.danger_tier
                    stand.combat_archetype = like.combat_archetype or stand.combat_archetype
                    stand.description_physical = like.description_physical or stand.description_physical
                    session.commit()
                taken.add(stand.id)
                out[npc_id] = (stand.id, like.name, stand.name)
                logger.info(f"@fight: {like.name} is kept for a later beat — {stand.name} fights in its place")
            return out
    except Exception as e:  # the fight goes on as written
        logger.warning(f"story-beast stand-in skipped: {e}")
        return {}


def _packmate_here(session, player_id: str, player, like: NPC, taken: set[str]) -> NPC | None:
    """A living beast here of `like`'s kind and tier that no beat needs
    (`campaign.story_cast_ids`), fit to fight and not already in this fight
    or the party — else None."""
    from src.services import party as party_mod
    from src.services.campaign import story_cast_ids
    from src.services.dialogue import is_beast

    kind = (like.species or "").strip().lower() or (like.name or "").split()[-1].lower()
    tier = (like.danger_tier or "").strip().lower()
    candidates = [
        n for n in session.query(NPC).filter(NPC.current_location_id == player.current_location_id,
                                             NPC.status == "alive").all()
        if n.id not in taken and n.condition != "wounded" and is_beast(n)
        and ((n.species or "").strip().lower() or (n.name or "").split()[-1].lower()) == kind
        and (n.danger_tier or "").strip().lower() == tier
    ]
    if not candidates:
        return None
    unavailable = story_cast_ids(session, player_id) | set(party_mod.member_ids(player))
    return next((n for n in candidates if n.id not in unavailable), None)


def _offstage_named(r: CommandRunner, ids: list[str]) -> dict[str, str]:
    """{id: what the player knows them as} for each of `ids` the story holds
    offstage before their scene (`campaign.offstage_ids`). Never raises."""
    if not ids:
        return {}
    try:
        from src.services.campaign import _shown_as, offstage_ids

        with get_session() as session:
            hidden = offstage_ids(session, r.player_id) & set(ids)
            return {i: _shown_as(session, r.player_id, session.get(NPC, i)) for i in ids if i in hidden}
    except Exception as e:
        logger.debug(f"offstage check for @fight skipped: {e}")
        return {}


def _story_villains_held_back(r: CommandRunner, ids: list[str]) -> dict[str, str]:
    """The campaign's villain and successor stay out of a GROUP fight before
    the last act. Autoplay 2026-10-04: a level-1 folklorist backing away got
    the boss thrown in beside his wolves and scout — a certain loss, and the
    story's climax spent on a scuffle. Attacking them alone is still a fight
    the player chose.

    The authored boss tier counts as a story villain too, as it does for
    ambushes (encounters.enemies_here) — and it is the ONLY rule when there
    is no spine: no campaign, or one still pending or failed. fleet4-bram:
    pt_i's campaign had failed, so this returned nothing and Aldric (boss)
    went in beside Vex against a winded level-1 player. A story that is over
    (won or lost), or in its last act, holds nobody back."""
    if len(ids) < 2:
        return {}
    try:
        from src.services.campaign import get_campaign_row

        with get_session() as session:
            campaign = get_campaign_row(session, r.player_id)
            status = campaign.status if campaign is not None else None
            if status in ("won", "lost"):
                return {}
            villains: set[str] = set()
            if status == "active":
                if int(campaign.current_act or 1) >= 3:
                    return {}
                villains = {i for i in (campaign.villain_npc_id, campaign.successor_npc_id) if i}
            held = {}
            for i in ids:
                npc = session.get(NPC, i)
                if i in villains or (npc is not None and (npc.danger_tier or "").strip().lower() == "boss"):
                    held[i] = npc.name if npc is not None else i
        return {} if len(held) == len(ids) else held
    except Exception as e:
        logger.debug(f"villain hold-back check skipped: {e}")
        return {}


def _h_kill(r: CommandRunner, cmd: ParsedCommand) -> dict:
    npc_id, _ = r.npc(cmd.main)
    return kill_npc(npc_id=npc_id, cause_of_death=_first(cmd, "cause") or "unknown")


def _h_npc_move(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    label, where = cmd.main, _first(cmd, "to")
    if not where:
        split = _split_on(label, "to")
        if split:
            label, where = split
    if not where:
        raise CommandError("say where: @npc-move <name> | <place>")
    npc_id, npc_name = r.npc(label)
    place_id, _ = r.place(re.sub(r"^to\s+", "", where, flags=re.I))
    _scene(place_id)
    result = move_npc(npc_id=npc_id, destination_id=place_id)
    if result.get("party_left"):
        return _Outcome(result, message=f"{npc_name} left the party (sent elsewhere)")
    return _Outcome(result)


def _h_npc(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    npc_id, _ = r.npc(cmd.main)
    status = (cmd.opt("status") or "").strip().lower() or None
    tier = (cmd.opt("tier") or "").strip().lower() or None
    fights = (cmd.opt("fights") or "").strip().lower() or None
    if tier and tier not in DANGER_TIERS:
        raise CommandError(f"tier is one of {', '.join(t for t in DANGER_TIERS if t != 'boss')}")
    if fights and fights not in ARCHETYPES:
        raise CommandError(f"fights is one of {', '.join(ARCHETYPES)}")
    goals, secrets, mood = cmd.opts("goal"), cmd.opts("secret"), cmd.opt("mood")
    fields = dict(
        description_physical=cmd.opt("looks"), description_personality=cmd.opt("manner"),
        voice_pattern=cmd.opt("voice"), profession=cmd.opt("trade"),
        status=None if status == "dead" else status,
        add_goal=goals[0] if goals else None, add_secret=secrets[0] if secrets else None,
        danger_tier=tier, combat_archetype=fights,
    )
    changes = any(v is not None for v in fields.values()) or bool(mood)
    if status != "dead" and not changes:
        raise CommandError("nothing to change — e.g. @npc Piper | status: missing | goal: find her brother")

    result: dict[str, Any] = {}
    if changes:
        result = update_npc(npc_id=npc_id, **fields)
        if _refused(result):
            return _Outcome(result)
        for goal in goals[1:]:
            update_npc(npc_id=npc_id, add_goal=goal)
        for secret in secrets[1:]:
            update_npc(npc_id=npc_id, add_secret=secret)
        if mood:
            update_npc_mood(npc_id=npc_id, new_mood=_clip(mood.lower(), 40))
    if status == "dead":
        # A death written as a status still leaves a body and settles the
        # kill objectives — and its npc_death event must reach the client
        died = kill_npc(npc_id=npc_id, cause_of_death=cmd.opt("cause") or "unknown")
        if result:
            died["updated"] = result
        return _Outcome(died)
    return _Outcome(result)


# fleet7-wenna T1-T2: the Captain named the dead carter "Fenwick" and the DM
# wrote `@new-npc Fenwick | trade: carter | … | status: dead`, a second dead
# carter beside the one the world authored (the satchel letter's "Tobin");
# round 5 did the same with "Hollis". Someone the story already lost in that
# trade is who the DM means.
_LOST = ("dead", "missing")
_TRADE_STOP = frozenset({"former", "retired", "young", "old", "the", "and", "with", "from", "who", "that", "for"})
_LOST_WORDS = re.compile(r"\b(?:dead|died|dies|killed|murdered|slain|missing|vanished|buried|body|corpse)\b", re.I)


def _trade_words(trade: str) -> tuple[list[str], set[str]]:
    """(the head word of each trade it lists, the trades they name): "Carter"
    -> ([carter], {carter}); "Barkeep" -> ([barkeep], {keeper}) —
    `dialogue._TRADE_SYNONYMS`. The head only (`dialogue._role_words`): a dead
    "Captain of the Lantern Watch" is no missing watchman, and a dead "Keeper
    of The Drowned Lantern" shares nothing with a "Lantern Watch sergeant"
    (every word of the trade used to count, "lantern" and "road" included)."""
    from src.services.dialogue import _NO_TRADE, _TRADE_CLASS, _TRADE_PARTS, _role_words, _trade_compounds

    heads: list[str] = []
    text = re.sub(r"\([^)]*\)", " ", trade or "").strip()
    if text.lower().strip(" .") in _NO_TRADE:
        return heads, set()            # "trade: unknown" names no trade to share
    for part in _TRADE_PARTS.split(text):
        words = [w.lower() for w in _role_words(part, "") if w.lower() not in _TRADE_STOP]
        head = re.sub(r"['’]s$", "", _trade_compounds(words)[-1]) if words else ""
        if len(head) >= 4 and head not in heads:
            heads.append(head)
    return heads, {_TRADE_CLASS.get(h, h) for h in heads}


def _lost_in_trade(r: CommandRunner, name: str, trade: str) -> str | None:
    """"Tobin (carter, dead)": someone of this trade the world has already
    lost — an NPC row that is dead or missing, or one a truth of this
    player's story names ("the carter Tobin, found dead…") — who is not the
    person being made. None when there is nobody, or no trade to go by."""
    words, wanted = _trade_words(trade)
    if not wanted:
        return None
    from src.services.dialogue import is_beast

    with get_session() as session:
        for npc in session.query(NPC).filter(NPC.status.in_(_LOST)).all():
            if _same_name(npc.name, name) or is_beast(npc):
                continue
            if _trade_words(npc.profession or "")[1] & wanted:
                return f"{npc.name} ({npc.profession or 'no trade'}, {npc.status})"
        try:
            from src.models.campaign import Campaign

            truths = [t for c in session.query(Campaign).filter(Campaign.player_id == r.player_id).all()
                      for t in (c.fixed_truths or [])]
        except Exception as e:
            logger.debug(f"truths unreadable for the lost-person check: {e}")
            truths = []
    for truth in truths:
        statement = str(truth.get("statement") if isinstance(truth, dict) else truth or "")
        if not _LOST_WORDS.search(statement):
            continue
        for w in words:
            m = (re.search(rf"\b(?:the|a)\s+(?:dead\s+|missing\s+|murdered\s+|late\s+)?{re.escape(w)}\s+"
                           rf"(?:named\s+|called\s+)?([A-Z][a-z]{{2,}})", statement, re.I)
                 or re.search(rf"\b([A-Z][a-z]{{2,}}),?\s+(?:the|a)\s+(?:dead\s+|missing\s+|murdered\s+)?"
                              rf"{re.escape(w)}\b", statement))
            if m and m.group(1)[0].isupper() and not _same_name(m.group(1), name) \
                    and m.group(1).lower() not in _LEAD_WORDS:
                return f"{m.group(1)} (the {w} the story names: \"{_clip(statement, 120)}\")"
    return None


# Capitalised at a sentence's start, and no one's name: "Then the carter…"
_LEAD_WORDS = frozenset({"the", "and", "but", "who", "his", "her", "their", "its", "our", "then", "when", "where",
                         "after", "before", "while", "once", "since", "now", "later", "soon", "even", "only",
                         "until", "with", "from", "into", "that", "this", "there", "here", "every", "each"})


def _h_new_npc(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    name = _clean_label(cmd.main)
    if not name:
        raise CommandError("name them: @new-npc <name> | trade: … | looks: … | manner: … | voice: …")
    tier = (cmd.opt("tier") or "").strip().lower() or None
    fights = (cmd.opt("fights") or "").strip().lower() or None
    if tier == "boss":
        raise CommandError("a boss is authored in the world editor, never made mid-scene — use deadly")
    goals = [g.strip() for w in cmd.opts("wants") for g in w.split(";") if g.strip()]
    # Someone only talked about: the dead, the missing. fleet5-wenna T3's
    # `| status: dead` was dropped and the dead carter stood alive in Market
    # Square for eight turns — in the brief, on the canvas
    status = (cmd.opt("status") or "").strip().lower() or None
    unread = ""
    if status not in (None, "alive", "dead", "missing"):
        unread = f"status '{status}' is not one @new-npc takes (dead or missing) — made alive, here"
        status = None
    absent = status in ("dead", "missing")
    if absent:
        lost = _lost_in_trade(r, name, cmd.opt("trade") or "")
        if lost:
            raise CommandError(
                f"nothing was made — the story already has {lost}. That is who you mean: use that name "
                "from now on, never a second one")
    # Where they stand is settled before they exist: a bad `at:` must not
    # leave a person made and the command reported failed
    place_id = r.place(cmd.opt("at"))[0] if cmd.opt("at") and not absent else None
    if place_id:
        _scene(place_id)
    result = create_npc(
        player_id=r.player_id, name=name, profession=cmd.opt("trade") or "",
        appearance=cmd.opt("looks") or "", personality=cmd.opt("manner") or "",
        voice=cmd.opt("voice") or "", goals=goals or None, secret=cmd.opt("secret"),
        mood=cmd.opt("mood") or "neutral", danger_tier=tier, combat_archetype=fights,
        status=status if absent else None,
    )
    if result.get("id"):
        r.created_npc_ids.append(result["id"])
        if result.get("created") and place_id:
            _relocate(result["id"], place_id)
    if result.get("exists"):
        state = "" if result.get("status") in (None, "alive") else f" ({result['status']})"
        return _Outcome(result, message=f"{result['name']}{state} already existed — used them; nothing new was made")
    if result.get("created") and absent:
        return _Outcome(result, message=f"{name} is {status} — on record, standing nowhere; nobody sees them here")
    if result.get("created") and unread:
        return _Outcome(result, message=unread)
    missing = [k for k in ("trade", "looks", "manner", "voice") if not cmd.opt(k)]
    if result.get("created") and missing:
        return _Outcome(result, message=f"made without {', '.join(missing)} — add it with @npc {name} | …")
    return _Outcome(result)


def _h_new_place(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    name = _clean_label(cmd.main)
    if not name:
        raise CommandError("name it: @new-place <name> | about: …")
    minutes = parse_duration(cmd.opt("minutes")) if cmd.opt("minutes") else None
    result = create_location(
        player_id=r.player_id, name=name, description=_first(cmd, "about"),
        kind=(cmd.opt("kind") or "room").strip().lower(),
        inside_current=_yes(cmd.opt("inside"), default=True),
        travel_minutes=minutes or 5,
    )
    if result.get("id"):
        r.created_place_ids.append(result["id"])
    if result.get("exists"):
        return _Outcome(result, message=f"{result['name']} already existed — used it; nothing new was made")
    return _Outcome(result)


# -- quests ------------------------------------------------------------------
#
# The quest tools find "the" player themselves and only manage it when the
# world holds exactly one (`quests._get_player`). A world with a second
# character would then never list the quest as theirs and — worse — complete
# it with no rewards and no XP. The runner knows its player, so it keeps that
# player's lists right after every change and, in a shared world, completes
# through the same `_complete_quest` the tool uses, with the right player.

def _sole_player(player_id: str) -> bool:
    """Would the quest tools' own player lookup land on this player?"""
    with get_session() as session:
        ids = [row[0] for row in session.query(Player.id).limit(2).all()]
    return ids == [player_id]


def _sync_quest_lists(player_id: str, quest_id: str, status: str) -> None:
    """Idempotent: a no-op when the tool already synced this player."""
    from src.tools.world_write.quests import _sync_player_quest_lists

    try:
        with get_session() as session:
            player = session.get(Player, player_id)
            if player is None:
                return
            before = (list(player.active_quests or []), list(player.completed_quests or []))
            _sync_player_quest_lists(player, quest_id, status)
            if (list(player.active_quests or []), list(player.completed_quests or [])) != before:
                session.commit()
    except Exception as e:  # the quest itself already changed; never undo that
        logger.warning(f"quest list sync failed for {quest_id}: {e}")


def _after_quest_change(r: CommandRunner, result: dict) -> dict:
    if isinstance(result, dict) and result.get("id") and result.get("status") and not _refused(result):
        _sync_quest_lists(r.player_id, result["id"], result["status"])
    return result


def _complete_for_player(player_id: str, quest_id: str) -> dict:
    """`update_quest_status(…, "completed")` for a named player: same
    completion path, same result shape, the rewards land on THIS player."""
    from src.tools.world_write.quests import _complete_quest

    with get_session() as session:
        quest = session.get(Quest, quest_id)
        if quest is None:
            return {"error": f"Quest {quest_id} not found"}
        outcome = _complete_quest(session, quest, session.get(Player, player_id))
        session.commit()
        result: dict[str, Any] = {
            "id": quest.id, "title": quest.title, "status": quest.status,
            "message": f"Quest '{quest.title}' marked as completed", "event": "quest_completed",
        }
        if outcome["rewards_applied"]:
            result["rewards_applied"] = outcome["rewards_applied"]
            result["message"] += f". Rewards applied: {', '.join(outcome['rewards_applied'])}"
        if outcome["progression"]:
            result["progression"] = outcome["progression"]
        if outcome.get("giver_trust"):
            result["giver_trust"] = outcome["giver_trust"]
        return result


def _h_quest_start(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    quest_id, title, status = r.quest(cmd.main, prefer=(QuestStatus.NOT_STARTED,))
    if status == QuestStatus.ACTIVE:
        return _Outcome({"success": True, "id": quest_id, "title": title, "already_active": True})
    if status != QuestStatus.NOT_STARTED:
        raise CommandError(f"'{title}' is already {status} — it cannot start again")
    wait = _not_taken_yet(r, quest_id, title)
    if wait:
        return _Outcome({"refused": "not_accepted", "id": quest_id, "title": title}, ok=False, message=wait)
    result = _after_quest_change(r, activate_quest(quest_id=quest_id))
    if _refused(result):
        return _Outcome(result)
    # What play already did since the giver offered it counts (fleet7-wenna:
    # Sorrel asked at T4-T7 for a job pitched at T2; src/services/job_offers.py)
    from src.services import job_offers

    credited = job_offers.backfill(r.player_id, quest_id)
    if credited:
        steps = "; ".join(f"'{rec['text']}'" for rec in credited)
        return _Outcome(result, message=f"already done in play since it was offered, and ticked: {steps}")
    return _Outcome(result)


def _norm_title(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower().replace("’", "'").replace("'", "")))


def _not_taken_yet(r: CommandRunner, quest_id: str, title: str) -> str | None:
    """Why an authored job its giver has not had a yes for must not start
    yet, or None.

    fleet8-wenna T3: the player was walking off to the gatehouse; Piper
    caught her sleeve, pitched "Counting the Howls" with its pay, ended
    "waiting" — and the same reply wrote @quest-start. A job pitched in an
    earlier reply is the player's to take (job_offers' [START THIS JOB] binds
    that start, and the engine starts one play already worked); one pitched
    first in THIS reply, or never pitched, starts only when the player's own
    words take it (`job_offers.read_answer`) or ask for it by name. The
    [OPENING] hands its first job over as written; the CLI and tools (no
    `player_input`) skip the check."""
    text = r.player_input
    if text is None or text.lstrip().startswith("[OPENING]"):
        return None
    from src.services import job_offers

    with get_session() as session:
        quest = session.get(Quest, quest_id)
        if quest is None or not quest.assigned_by_npc_id:
            return None
        # (whatever the offer book made of their answer: fleet7-bram T2 "I'll
        # take a look … I'm not doing it for the Watch" read as a no)
        offer = job_offers.open_offers(r.player_id).get(quest_id)
        if offer is not None:
            return None
        named = _norm_title(quest.title)
        if named and f" {named} " in f" {_norm_title(text)} ":
            return None
        terms = dict(job_offers.offer_terms(session, quest), offered=set())
    verdict, _ = job_offers.read_answer(text, terms, age=0, fresh=True)
    if verdict == "accept":
        return None
    # A pitch the offer book missed (told in prose, or in a voice it could not
    # tie to the giver) whose start this guard refused in the last reply: the
    # player has had their turn to answer and said no no — never a job stuck
    # behind the guard for good because their yes was in words it misses
    if offer is None and verdict != "decline" and r.previous is not None and any(
            res.verb in ("quest-start", "quest-new") and isinstance(res.result, dict)
            and res.result.get("refused") == "not_accepted" and res.result.get("id") == quest_id
            for res in r.previous.results):
        return None
    return (f"not started — wait for their answer — they have not said yes to \"{title}\". Let the offer "
            "stand as said (no second pitch, nothing paid); the reply where they take it writes @quest-start, "
            "and if they simply go and do its first step the engine starts it")


def _quest_status(status: str) -> Callable[[CommandRunner, ParsedCommand], _Outcome]:
    def handler(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
        quest_id, title, current = r.quest(cmd.main, prefer=(QuestStatus.ACTIVE,))
        if current == status:
            return _Outcome({"success": True, "id": quest_id, "title": title, "already": status})
        if current in (QuestStatus.COMPLETED, QuestStatus.FAILED):
            raise CommandError(f"'{title}' is already {current}")
        if status == QuestStatus.COMPLETED:
            undone = _work_only_told(r, quest_id, title)
            if undone:
                return _Outcome({"refused": "work_not_done", "id": quest_id, "title": title},
                                ok=False, message=undone)
            r.paid_coin |= _reward_coin(quest_id)
            items, rep = _reward_goods(quest_id)
            r.paid_items.update(items)
            r.paid_rep.update(rep)
        if status == QuestStatus.COMPLETED and not _sole_player(r.player_id):
            result = _after_quest_change(r, _complete_for_player(r.player_id, quest_id))
        else:
            result = _after_quest_change(r, update_quest_status(quest_id=quest_id, status=status))
        thanked = result.get("giver_trust") if isinstance(result, dict) else None
        if isinstance(thanked, dict) and thanked.get("npc_id"):
            r.paid_trust.add(thanked["npc_id"])
        if status == QuestStatus.COMPLETED and not _refused(result):
            _note_paid_away(r, quest_id, title)
            said = _remember_quest_payout(r, quest_id)
            if said:
                return _Outcome(result, message=said)
        return _Outcome(result)
    return handler


def _note_paid_away(r: CommandRunner, quest_id: str, title: str) -> None:
    """A payout with its giver nowhere near the player: the next context
    says how it reaches them (fleet8-tamsin T11-T12: the pay "settles into
    your pocket before you reach the gate", Piper long gone)."""
    try:
        with get_session() as session:
            quest = session.get(Quest, quest_id)
            giver = quest.assigned_by_npc_id if quest is not None else None
        if giver and giver not in _present_npc_ids(r):
            r.paid_away.append({"title": title, "giver_id": giver, "quest_id": quest_id})
    except Exception as e:      # the quest already paid; this is only a note
        logger.debug(f"payout presence unreadable for {quest_id}: {e}")


def _work_only_told(r: CommandRunner, quest_id: str, title: str) -> str | None:
    """Why @quest-done must not pay yet, or None: a place step the player
    never stood at, an item step whose thing they never held
    (`objectives.undone_in_play`). fleet7-tamsin T14 paid the mission in
    full with "Reach Wolf's Hollow" and the whistle undone — the trip and
    the find were prose, `@hand Cracked Bone Whistle` had just failed."""
    from src.services import objectives as objectives_mod

    with get_session() as session:
        rows = objectives_mod.undone_in_play(session, session.get(Quest, quest_id), r._player_row(session))
    if not rows:
        return None
    steps = "; ".join(f"'{row['text']}' — {row['why']}" + (f" ({row['where']})" if row["where"] else "")
                      for row in rows)
    # A ruling is for a thing that exists nowhere; a place never stood at or
    # a thing still lying where it was put is refused by @step-done too
    # (`_told_only`) — offering it was the way round this very guard
    ruled = [row for row in rows if not _told_only_row(row)]
    ruling = (f" Only if the story truly settled {'that step' if len(ruled) == 1 else 'such a step'} another way "
              f"that the player saw, rule it with @step-done {title} | <step> first." if ruled else
              " @step-done will not mark a place they never stood at or a thing still lying where it was put.")
    return (f"'{title}' is not done, so nothing was paid — no coin, no reward. Still open: {steps}. "
            "The work happens in play: they go there (@move), take the thing where it lies once they reach for "
            "it (@interact), hand it over (@hand <item> | to: <person>), and then @quest-done pays. Whatever "
            "your story told of it did not happen: open the next reply where they really are, without it."
            + ruling)


def _told_only_row(row: dict[str, Any]) -> bool:
    """An `undone_in_play` row no ruling may tick: a place the player never
    stood at, or an item an authored Thing still holds (`where` names it)."""
    return row["kind"] == "go_to" or bool(row.get("where"))


def _told_only(r: CommandRunner, quest_id: str, obj: dict[str, Any]) -> str | None:
    """Why @step-done must not tick this place or item step, or None.
    fleet7-tamsin T14: the refused @quest-done offered @step-done, and
    "@step-done … | Reach Wolf's Hollow" would have paid the same trip that
    only happened in prose — the whistle still lying at the Hollow, act 2
    resting on it. An item that exists nowhere stays the DM's to rule."""
    if obj.get("kind") not in ("go_to", "obtain", "deliver"):
        return None
    from src.services import objectives as objectives_mod

    with get_session() as session:
        rows = objectives_mod.undone_in_play(session, session.get(Quest, quest_id), r._player_row(session))
    row = next((x for x in rows if x["id"] == obj["id"]), None)
    if row is None or not _told_only_row(row):
        return None
    return (f"'{obj['text']}' was not marked: {row['why']}" + (f" ({row['where']})" if row["where"] else "")
            + ". The engine ticks it when it happens in play — they go there (@move), take the thing once "
            "they reach for it (@interact), hand it over (@hand). Whatever your story told of it did not "
            "happen: open the next reply where they really are.")


# -- advance pay -------------------------------------------------------------
# fleet6-tamsin T6 won a roll to be paid up front (`@coin +10`, `@give Yarrow
# Tincture`), and T11's @quest-done paid the authored reward again: 45 coin,
# two tinctures. Coin or a reward item handed over while that quest's giver
# is here is recorded on the quest (rewards["advanced"]); completion pays the
# balance (quests._apply_rewards) and the brief's reward line says what is
# already paid (quests.reward_words).

# A sale, a bet, a find, a refund or a theft is its own money, never the
# job's pay — the thief persona's `@coin +8 | lifted from a drunk's purse`
# with the Captain in the square came off his 60-coin job (review of round
# 5). Money paid BACK is a refund; "half now, the rest when you're back" is
# the advance itself
_NOT_AN_ADVANCE = re.compile(
    r"\b(?:sells?|sold|sale|buys?|bought|trade[sd]?|trading|bets?|wager|won|wins|winnings|loot(?:ed)?"
    r"|found|finds|refund(?:s|ed)?|change|returns?|returned"
    r"|(?:pays?|paid|paying|gives?|gave|giving|hands?|handed|handing)\b[^|.;,]{0,12}?\bback"
    r"|steal(?:s|ing)?|stole|stolen|lift(?:s|ed|ing)?|pickpocket\w*|pinch(?:es|ed|ing)?|filch\w*"
    r"|swip(?:e|es|ed|ing)|pilfer\w*|nick(?:s|ed|ing)?|cutpurse\w*)\b",
    re.IGNORECASE,
)
# A @give's `about:` is the thing's description, not a reason: only how it
# was come by rules it out — "a pinch of yarrow; it lifts a fever" is the
# tincture Piper paid up front, not a theft
_NOT_AN_ADVANCE_ITEM = re.compile(
    r"\b(?:sells?|sold|sale|buys?|bought|trade[sd]?|trading|bets?|wager|won|winnings|loot(?:ed)?"
    r"|found|stole|stolen|pickpocket\w*|filch\w*|pilfer\w*|swiped)\b",
    re.IGNORECASE,
)


# A trade whose head noun is an everyday word names nobody: the seed's
# "Stable hand" would make "pressed into your hand" name Sorrel
_PLAIN_TRADE_HEADS = frozenset({"hand", "hands", "wolf", "woman", "girl", "help", "folk", "worker", "keep"})


def _called_by(npc: NPC | None) -> set[str]:
    """The words a reason names this person by: their name's words and their
    trade's head noun, four letters or more ("Old" names nobody)."""
    if npc is None:
        return set()
    trade = re.split(r"\s+of\s+|,|\s+and\s+", npc.profession or "", maxsplit=1)[0]
    out = {w for w in re.findall(r"[a-z]+", (npc.name or "").lower()) if len(w) >= 4}
    out |= {w for w in re.findall(r"[a-z]+", trade.lower())[-1:] if len(w) >= 4 and w not in _PLAIN_TRADE_HEADS}
    return out


def _advanced_quest(session, reason: str, hits: list[tuple[Quest, str | None]],
                    present: set[str]) -> tuple[Quest, str | None] | None:
    """Which open quest a payment is the pay of, among those it fits.

    The giver the reason names wins: fleet6-tamsin T6's `@coin +10 | Piper
    pays up front` fitted Piper's job AND the Captain's 60-coin one (he was
    in the square too) — read as neither, T11 paid the 10 coin again. A
    reason naming someone else here and no giver is their money (`@coin +6
    | Hesk pays for the pelt`). Otherwise one fitting job is it, an active
    one before one not yet taken up; two are no telling."""
    said = set(re.findall(r"[a-z]+", (reason or "").lower()))
    if said:
        named = [h for h in hits if said & _called_by(session.get(NPC, h[0].assigned_by_npc_id))]
        if named:
            return named[0] if len({h[0].id for h in named}) == 1 else None
        givers = {h[0].assigned_by_npc_id for h in hits}
        if any(said & _called_by(session.get(NPC, npc_id)) for npc_id in present - givers):
            return None
    for status in (QuestStatus.ACTIVE, QuestStatus.NOT_STARTED):
        level = [h for h in hits if h[0].status == status]
        if level:
            return level[0] if len(level) == 1 else None
    return None


def _present_npc_ids(r: CommandRunner) -> set[str]:
    ids = set(r.present_ids)
    with get_session() as session:
        here = r._player_row(session).current_location_id
        if here:
            ids.update(row[0] for row in session.query(NPC.id).filter(
                NPC.current_location_id == here, NPC.status == "alive").all())
    return ids


def _reward_item_names(rewards: Any) -> list[str]:
    items = rewards.get("items") if isinstance(rewards, dict) else None
    names = [" ".join(str((e.get("name") if isinstance(e, dict) else e) or "").split()) for e in items or []]
    return [n for n in names if n]


def _note_advance(r: CommandRunner, reason: str, *, coin: int = 0, item: str | None = None) -> str | None:
    """Record coin or an item just handed over as part of a quest's reward
    paid up front: the giver of an open quest is here, and the payment fits
    what that quest still owes (`_advanced_quest` picks among several).
    Returns the sentence for the DM, or None when it is no advance."""
    from src.tools.world_write.quests import reward_balance, reward_coin

    if not (coin or item) or (_NOT_AN_ADVANCE if coin else _NOT_AN_ADVANCE_ITEM).search(reason or ""):
        return None
    present = _present_npc_ids(r)
    if not present:
        return None
    with get_session() as session:
        open_quests = session.query(Quest).filter(
            Quest.status.in_((QuestStatus.ACTIVE, QuestStatus.NOT_STARTED)),
            Quest.assigned_by_npc_id.in_(sorted(present)),
        ).all()
        hits: list[tuple[Quest, str | None]] = []
        for quest in open_quests:
            balance = reward_balance(quest.rewards)
            if coin and coin <= reward_coin(balance):
                hits.append((quest, None))
            elif item:
                named = next((n for n in _reward_item_names(balance)
                              if _names_held_item(item, n) or _names_held_item(n, item)), None)
                if named:
                    hits.append((quest, named))
        chosen = _advanced_quest(session, reason, hits, present)
        if chosen is None:
            return None
        quest, named = chosen
        rewards = dict(quest.rewards or {})
        advanced = dict(rewards.get("advanced") or {}) if isinstance(rewards.get("advanced"), dict) else {}
        if coin:
            advanced["currency"] = int(advanced.get("currency") or 0) + coin
        if named:
            advanced["items"] = list(advanced.get("items") or []) + [named]
        rewards["advanced"] = advanced
        quest.rewards = rewards                 # reassign: in-place JSON edits are not persisted
        title, giver = quest.title, quest.assigned_by_npc_id
        session.commit()
    if coin:
        _remember_payout(r, "coin", coin, f"{coin} coin", title, giver)
    else:
        _remember_payout(r, "item", prog._slug(named), named, title, giver)
    what = f"{coin} coin" if coin else f"the {named}"
    r.advances.append({"title": title, "giver_id": giver, "what": what})
    return f"counted as {title}'s reward paid up front ({what}); @quest-done will not pay it again"


def _remember_payout(r: CommandRunner, kind: str, value: Any, name: str, title: str,
                     giver: str | None) -> None:
    rows = [row for row in _RECENT_PAYOUTS.get(r.player_id) or []
            if row[0] >= r.serial - RECENT_PAYOUT_REPLIES]
    rows.append((r.serial, kind, value, name, title, giver))
    _RECENT_PAYOUTS[r.player_id] = rows


def _remember_quest_payout(r: CommandRunner, quest_id: str) -> str:
    """After @quest-done: remember what it paid (the balance) for the next
    replies' guards. Returns what the DM is told about an advance ('' when
    none was paid) — with nothing left to pay, "show the rest changing
    hands" would have asked for pay that does not exist."""
    from src.tools.world_write.quests import reward_advanced, reward_balance, reward_coin, reward_words

    try:
        with get_session() as session:
            quest = session.get(Quest, quest_id)
            if quest is None:
                return ""
            rewards = quest.rewards if isinstance(quest.rewards, dict) else {}
            title, giver = quest.title, quest.assigned_by_npc_id
    except Exception as e:
        logger.debug(f"payout not remembered for {quest_id}: {e}")
        return ""
    balance = reward_balance(rewards)
    coin = reward_coin(balance)
    if coin:
        _remember_payout(r, "coin", coin, f"{coin} coin", title, giver)
    for name in _reward_item_names(balance):
        _remember_payout(r, "item", prog._slug(name), name, title, giver)
    advanced = reward_advanced(rewards)
    if not (advanced["currency"] or advanced["items"]):
        return ""
    paid = reward_words({"currency": advanced["currency"], "items": advanced["items"]})
    if not (coin or _reward_item_names(balance)):
        return (f"paid nothing more — {paid} was paid up front and settled it; show the giver's thanks, "
                "never the advance again and never more pay")
    return f"paid only the balance — {paid} was paid up front; show the rest changing hands, never the advance again"


def _recent_payout(r: CommandRunner, kind: str, value: Any, *, reason: str = "") -> str | None:
    """The quest whose pay put this coin amount or item in the player's hands
    in one of the last RECENT_PAYOUT_REPLIES replies (never this one: the
    reply's own guards cover it), else None. The same coin counts while that
    quest's giver is here, when the reason names the quest or its giver, or
    while its reward_unnarrated is outstanding — a stranger's ten coin is a
    new payment. fleet8-wenna T17/T18: @quest-done paid Piper's balance as
    the player walked off, then `@coin +10 | Piper's remaining payment` from
    the Stable Yard paid it again (Piper was in the square)."""
    rows = [row for row in _RECENT_PAYOUTS.get(r.player_id) or []
            if r.serial - RECENT_PAYOUT_REPLIES <= row[0] < r.serial and row[1] == kind]
    if not rows or (kind == "coin" and _REFUND.search(reason or "")):
        return None
    present: set[str] | None = None
    unshown = _UNNARRATED.get(r.player_id) or {}
    for _serial, _kind, paid, name, title, giver in rows:
        if kind == "item":
            if paid == prog._slug(str(value)) or _names_held_item(str(value), name):
                return title
            continue
        if paid == value:
            if title in unshown and unshown[title] >= r.serial - RECENT_PAYOUT_REPLIES:
                return title
            if _reason_names_job(reason, title, giver):
                return title
            present = _present_npc_ids(r) if present is None else present
            if giver and giver in present:
                return title
    return None


# Coin the player paid coming back is no payout restated (turn_audit's
# advance_bought asks for exactly that: "@coin +10 | Piper returns the coin").
# The COIN returns, never the giver: [PAID WHILE APART] asks for the pay to
# arrive with them, and "@coin +10 | Piper returns with the balance" is that
# payout paid a second time
_REFUND = re.compile(
    r"\b(?:refund\w*"
    r"|(?:returns?|returned|returning)\s+(?:(?:the|your|their|her|his|my)\s+)?(?:\d+\s+)?(?:coins?|money|silver"
    r"|it|them)"
    r"|(?:pushes|pushed|gives|gave|hands|handed|presses|pressed|slides|slid)\s+(?:it|them|the\s+coins?)\s+back"
    r"|(?:pushes|pushed|gives|gave|hands|handed|slides|slid)\s+back\s+(?:the\s+|your\s+)?(?:\d+\s+)?coins?)\b",
    re.IGNORECASE)


# Quest titles whose payout reward_unnarrated flagged (player id -> {title:
# reply serial}); the server notes them after the audit
# (`note_reward_unnarrated`). In memory, like the payouts they qualify
_UNNARRATED: dict[str, dict[str, int]] = {}


def note_reward_unnarrated(player_id: str, titles: list[str]) -> None:
    """The reply that just ran paid these quests off screen: until its
    payouts age out, the same coin again is that pay restated."""
    serial = _REPLY_SERIAL.get(player_id, 0)
    marks = _UNNARRATED.setdefault(player_id, {})
    for title in titles:
        if title:
            marks[title] = serial


def _stem(word: str) -> str:
    """"howls"/"howl", "counting"/"count": enough to hear a title in a reason."""
    for tail in ("ing", "es", "s"):
        if word.endswith(tail) and len(word) - len(tail) >= 4:
            return word[: -len(tail)]
    return word


def _reason_names_job(reason: str, title: str, giver_id: str | None) -> bool:
    """Does a @coin reason name the quest (its title's words, two of them or
    all if fewer) or its giver (`_called_by`)?"""
    said = {_stem(w) for w in re.findall(r"[a-z]+", (reason or "").lower())}
    if not said:
        return False
    own = {_stem(w) for w in re.findall(r"[a-z]+", (title or "").lower()) if len(w) >= 4}
    if own and len(own & said) >= min(2, len(own)):
        return True
    if not giver_id:
        return False
    try:
        with get_session() as session:
            called = _called_by(session.get(NPC, giver_id))
    except Exception:
        return False
    return bool({_stem(w) for w in called} & said)


def _reward_coin(quest_id: str) -> set[int]:
    """The coin amounts a quest's completion pays (what @quest-done hands over)."""
    try:
        with get_session() as session:
            quest = session.get(Quest, quest_id)
            rewards = dict(quest.rewards or {}) if quest is not None else {}
    except Exception:
        return set()
    out = set()
    for key in ("currency", "gold", "coin", "coins"):
        try:
            value = int(rewards.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            out.add(value)
    # ...and the balance, when part of it was paid up front: a 60-coin job
    # with 30 advanced pays 30, and a restated `@coin +30` is that 30 again
    from src.tools.world_write.quests import reward_balance, reward_coin

    balance = reward_coin(reward_balance(rewards))
    if balance > 0:
        out.add(balance)
    return out


def _reward_goods(quest_id: str) -> tuple[dict[str, str], dict[str, int]]:
    """The items ({slug: name}) and standing ({faction id: delta}) a quest's
    completion pays. Reward items may be names or {name, …} dicts; standing
    is keyed by faction id, or by a name the world resolves."""
    try:
        with get_session() as session:
            quest = session.get(Quest, quest_id)
            rewards = dict(quest.rewards or {}) if quest is not None else {}
            items: dict[str, str] = {}
            for entry in rewards.get("items") or []:
                name = str((entry.get("name") if isinstance(entry, dict) else entry) or "").strip()
                if name:
                    items[prog._slug(name)] = name
            rep: dict[str, int] = {}
            raw = rewards.get("reputation")
            factions = session.query(Faction).all() if isinstance(raw, dict) and raw else []
            for key, delta in (raw.items() if factions else ()):
                row = next((f for f in factions if f.id == key), None) \
                    or next((f for f in factions if _same_name(f.name, str(key))), None)
                if row is not None and isinstance(delta, (int, float)) and int(delta):
                    rep[row.id] = int(delta)
    except Exception as e:
        logger.debug(f"reward goods unreadable for {quest_id}: {e}")
        return {}, {}
    return items, rep


_STEP_RE = re.compile(r"^(talk[\s_-]?to|go[\s_-]?to|obtain|kill|deliver|custom)\b[\s:]*(.*)$", re.I | re.S)


def _step(r: CommandRunner, raw: str) -> dict[str, Any]:
    """One `step:` -> a structured objective with a human sentence."""
    m = _STEP_RE.match(raw.strip())
    if not m:
        return {"text": _clip(raw, 200), "kind": "custom"}
    kind = re.sub(r"[\s-]", "_", m.group(1).lower()).replace("talkto", "talk_to").replace("goto", "go_to")
    target, _, note = m.group(2).partition(" — ")
    target = target.strip()
    tail = f" — {note.strip()}" if note.strip() else ""
    if not target:
        raise CommandError(f"step '{raw}' names nothing")
    if kind == "custom":
        return {"text": _clip(m.group(2).strip(), 200), "kind": "custom"}
    if kind == "talk_to":
        npc_id, npc_name = r.npc(target)
        return {"text": f"Talk to {npc_name}{tail}", "kind": kind, "target_id": npc_id}
    if kind == "kill":
        npc_id, npc_name = r.npc(target)
        return {"text": f"Kill {npc_name}{tail}", "kind": kind, "target_id": npc_id}
    if kind == "go_to":
        place_id, place_name = r.place(target)
        # A step that is already true is free XP: 2026-10-04's "Go to
        # Emberfall" ticked by stepping into the town around the player
        if place_id in r.here_and_above():
            raise CommandError(f"step '{raw}': they are already in {place_name} — a go_to step must lead "
                               "somewhere they are not (a report-back is talk_to <person>)")
        refusal = whole_town(place_id)
        if refusal:
            raise CommandError(f"step '{raw}': {refusal}")
        return {"text": f"Go to {place_name}{tail}", "kind": kind, "target_id": place_id}
    if kind == "obtain":
        return {"text": f"Obtain {target}{tail}", "kind": kind, "item_name": target}
    split = _split_on(target, "to")
    if not split:
        raise CommandError(f"step '{raw}' must say who receives it: deliver <item> to <name>")
    item, who = split
    npc_id, npc_name = r.npc(who)
    return {"text": f"Deliver {item} to {npc_name}{tail}", "kind": "deliver",
            "item_name": item, "target_id": npc_id}


_REWARD_COIN_RE = re.compile(
    r"\+?(\d+)\s*(?:(?:gold|silver|copper)\s+)?(?:coins?|gold|silver|copper|crowns?|marks?|pieces?|currency)?",
    re.I,
)
# XP is code's to give (progression.strip_authored_xp); written as a reward
# it would otherwise arrive as an inventory item called "50 xp"
_REWARD_XP_RE = re.compile(r"\+?\d*\s*(?:xp|exp|experience)(?:\s+points?)?", re.I)


def _rewards(values: list[str]) -> tuple[dict[str, Any], bool]:
    """(the rewards dict, whether an XP reward was dropped)."""
    coin, items, dropped_xp = 0, [], False
    for raw in values:
        text = _THOUSANDS_RE.sub("", raw.strip())
        m = _REWARD_COIN_RE.fullmatch(text)
        if m:
            coin += int(m.group(1))
        elif _REWARD_XP_RE.fullmatch(text):
            dropped_xp = True
        elif text:
            items.append(text)
    rewards: dict[str, Any] = {}
    if coin:
        rewards["currency"] = coin
    if items:
        rewards["items"] = items
    return rewards, dropped_xp


def _h_quest_new(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    title = _clean_label(cmd.main)
    if not title:
        raise CommandError("give the quest a title: @quest-new <title> | about: … | step: …")
    with get_session() as session:
        same = [q for q in session.query(Quest).all() if _same_name(q.title, title)]
        existing = [(q.id, q.title, q.status) for q in same]
    if existing:
        quest_id, found_title, status = existing[0]
        if status == QuestStatus.NOT_STARTED:
            # The authored job by another door: the same yes is owed
            wait = _not_taken_yet(r, quest_id, found_title)
            if wait:
                return _Outcome({"refused": "not_accepted", "id": quest_id, "title": found_title}, ok=False,
                                message=wait)
            result = _after_quest_change(r, activate_quest(quest_id=quest_id))
            return _Outcome(result, message=f"'{found_title}' already existed — started it instead of a copy")
        if status == QuestStatus.ACTIVE:
            return _Outcome({"success": True, "id": quest_id, "title": found_title, "exists": True},
                            message=f"'{found_title}' is already active — nothing new was made")
        raise CommandError(f"a quest called '{found_title}' is already {status} — choose another title")
    steps = [_step(r, raw) for raw in cmd.opts("step")]
    if not steps:
        raise CommandError("a quest needs at least one step, e.g. | step: talk_to Coldiron")
    scale = (cmd.opt("scale") or "").strip().lower() or None
    if scale and scale not in QUEST_SCALES:
        raise CommandError(f"scale is one of {', '.join(QUEST_SCALES)}")
    ceiling, why = _scale_ceiling(steps)
    granted = min(scale or prog.DEFAULT_QUEST_SCALE, ceiling, key=QUEST_SCALES.index)
    giver = r.npc(cmd.opt("from"))[0] if cmd.opt("from") else None
    rewards, dropped_xp = _rewards(cmd.opts("reward"))
    result = _after_quest_change(r, create_quest(
        title=title, description=_first(cmd, "about") or title, objectives=steps,
        rewards=rewards or None, assigned_by_npc_id=giver,
        start_active=True, scale=granted,
    ))
    notes = []
    if scale and granted != scale:
        notes.append(f"scale lowered to {granted} — {why}")
    if dropped_xp:
        notes.append("the xp reward was dropped — the engine pays growth from | scale:")
    if _refused(result):
        return _Outcome(result)
    return _Outcome(result, message="; ".join(notes))


def _scale_ceiling(steps: list[dict[str, Any]]) -> tuple[str, str]:
    """The biggest scale a DM-made quest's own steps can carry, and why.

    Quest XP comes from the scale alone, and the DM picked it freely: on
    2026-10-04 three-turn talk-and-walk quests it called "mission" paid 220
    XP each — two levels in twenty turns without a fight."""
    kinds = {s["kind"] for s in steps}
    if len(steps) <= 2 and not kinds & {"kill", "obtain", "deliver"}:
        return "errand", "one or two steps with nothing to kill, find or deliver are an errand"
    if len(steps) >= 3 and kinds & {"kill", "deliver"}:
        return QUEST_SCALES[-1], ""
    return "job", "mission and saga need three or more steps with a kill or a delivery"


def _objective_in(objectives: list[dict[str, Any]], label: str) -> dict[str, Any] | None:
    wanted = label.strip().lower()
    for obj in objectives:
        if obj["id"].lower() == wanted or obj["text"].lower() == wanted:
            return obj
    try:
        return _walk_tiers(label, [objectives], lambda o: o["text"], lambda o: o["id"], _PLAIN_STOP, "step")
    except CommandError:
        return None


def _step_text_elsewhere(quest_id: str, text: str) -> bool:
    """Does another active quest have an undone step with these words?"""
    from src.services import objectives as objectives_mod

    wanted = text.strip().lower()
    with get_session() as session:
        others = session.query(Quest).filter(Quest.status == QuestStatus.ACTIVE, Quest.id != quest_id).all()
        return any(not o["done"] and o["text"].strip().lower() == wanted
                   for q in others for o in objectives_mod.normalize_objectives(q.objectives))


def _tick_one_objective(player_id: str, quest_id: str, objective_id: str) -> list[dict[str, Any]]:
    """Mark one objective on one quest, with the same records, client event
    and ledger line `note_custom_done` would have produced."""
    from src.services import objectives as objectives_mod

    with get_session() as session:
        quest = session.get(Quest, quest_id)
        if quest is None or quest.status != QuestStatus.ACTIVE:
            return []
        objectives = objectives_mod.normalize_objectives(quest.objectives)
        hit = next((o for o in objectives if o["id"] == objective_id and not o["done"]), None)
        if hit is None:
            return []
        clock = session.query(WorldClock).first()
        hit["done"], hit["done_day"] = True, (clock.day if clock else 1)
        quest.objectives = objectives          # reassign: in-place JSON edits are not persisted
        records = [{"quest_id": quest.id, "quest_title": quest.title, "objective_id": hit["id"],
                    "text": hit["text"], "kind": hit["kind"], "all_done": objectives_mod.all_done(objectives)}]
        # ...and the same XP slice (objectives.pay_step_xp)
        objectives_mod.pay_step_xp(session, session.get(Player, player_id), quest, len(objectives), records)
        session.commit()
    objectives_mod.publish(player_id, records)
    return records


def _h_step_done(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    from src.services import objectives as objectives_mod

    if cmd.extras:
        quest_id, title, status = r.quest(cmd.main, prefer=(QuestStatus.ACTIVE,))
        with get_session() as session:
            quest = session.get(Quest, quest_id)
            objectives = objectives_mod.normalize_objectives(quest.objectives)
        obj = _objective_in(objectives, cmd.extras[0])
        if obj is None:
            raise CommandError(f"'{title}' has no step '{cmd.extras[0]}'")
    else:
        # "@step-done Find the courier": the step alone, across active quests
        with get_session() as session:
            candidates = [(q.id, q.title, q.status, o)
                          for q in session.query(Quest).filter(Quest.status == QuestStatus.ACTIVE).all()
                          for o in objectives_mod.normalize_objectives(q.objectives)]
        hits = [c for c in candidates if _objective_in([c[3]], cmd.main) is not None and not c[3]["done"]]
        if len(hits) != 1:
            raise CommandError("write @step-done <quest> | <step>" if not hits
                               else f"'{cmd.main}' matches steps on several quests — name the quest")
        quest_id, title, status, obj = hits[0]
    if status != QuestStatus.ACTIVE:
        raise CommandError(f"'{title}' is not active")
    if obj["done"]:
        return _Outcome({"success": True, "quest_id": quest_id, "objective": obj["text"], "already_done": True})
    # A place never stood at, a thing still lying out: the trip and the find
    # told in prose are no ruling (fleet7-tamsin T14; `_told_only`)
    told = _told_only(r, quest_id, obj)
    if told:
        return _Outcome({"refused": "work_not_done", "quest_id": quest_id, "objective": obj["text"]},
                        ok=False, message=told)
    # A roll on this very step just failed: ticking it is the failure for
    # free (fleet6-wenna T8 failed the howl count, T9 ticked "mark where it
    # rises"; src/services/stakes.py binds a failure to the step it tried)
    from src.services import stakes

    refusal = stakes.step_refusal(r.player_id, quest_id, obj)
    if refusal:
        return _Outcome({"refused": "failed_attempt", "quest_id": quest_id, "objective": obj["text"]},
                        ok=False, message=refusal)
    # `note_custom_done` matches across EVERY active quest (its one-quest
    # scope does not hold): the text, not the id, since "o1" names the first
    # step of each — and when another quest has a step with the very same
    # words, the tick is done here, on this quest alone
    if _step_text_elsewhere(quest_id, obj["text"]):
        records = _tick_one_objective(r.player_id, quest_id, obj["id"])
    else:
        records = objectives_mod.note_custom_done(r.player_id, quest_id, obj["text"])
    if not records:
        raise CommandError(f"could not mark '{obj['text']}' on '{title}'")
    result = {"success": True, "quest_id": quest_id, "records": records,
              "objectives_done": objectives_mod.done_lines(records)}
    if any(rec.get("all_done") and rec.get("quest_id") == quest_id for rec in records):
        return _Outcome(result, message=f"every step of '{title}' is met — write @quest-done {title} when it is truly finished")
    return _Outcome(result)


# -- the story & the screen ----------------------------------------------------

def _h_tension(r: CommandRunner, cmd: ParsedCommand) -> dict:
    label = cmd.main.strip().lower()
    if label not in TENSION_LABELS:
        raise CommandError(f"tension is one of {', '.join(TENSION_LABELS)}")
    return update_dm_state(tension=label)


def _cinematic(result: dict) -> _Outcome:
    # A refusal is normal and must leave no trace: not ok, nothing to say
    if result.get("rejected"):
        return _Outcome(result, ok=False, message="")
    return _Outcome(result)


def _h_weather(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    label = cmd.main.strip().lower()
    if label not in WEATHER_LABELS:
        raise CommandError(f"weather is one of {', '.join(WEATHER_LABELS)}")
    return _cinematic(play_cinematic(player_id=r.player_id, kind="weather", mood=label))


def _h_title(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    title = cmd.main.strip()
    if not title:
        raise CommandError("write @title <TITLE> | <subtitle>")
    return _cinematic(play_cinematic(player_id=r.player_id, kind="title_card", title=title,
                                     subtitle=_first(cmd, "subtitle") or None))


def _h_vision(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    return _cinematic(play_cinematic(player_id=r.player_id, kind="vision", title=cmd.main.strip() or None))


def _h_boss(r: CommandRunner, cmd: ParsedCommand) -> _Outcome:
    """Hold the entrance for the fight. A boss intro is once-ever per
    character; played on a mere arrival (2026-10-04: Aldric's, a scene before
    anyone drew steel) it was spent when the fight it exists for came."""
    if r.after_combat:
        # Nothing is held either: the entrance belongs to the fight the
        # player chooses next turn, not to this aftermath's ledger
        return _Outcome({"refused": "aftermath"}, ok=False, message=_AFTERMATH_REFUSAL)
    npc_id, npc_name = r.npc(cmd.main)
    r.pending_boss[npc_id] = npc_name
    return _Outcome({"held": npc_id})


def _involved(r: CommandRunner, cmd: ParsedCommand) -> tuple[list[str], list[str]]:
    npc_ids = [r.npc(n)[0] for n in re.split(r",", cmd.opt("npcs") or "") if n.strip()]
    place = cmd.opt("place")
    place_ids = [r.place(place)[0]] if place else [i for i in [r.current_location_id()] if i]
    return npc_ids, place_ids


def _h_event(r: CommandRunner, cmd: ParsedCommand) -> dict:
    name = cmd.main.strip()
    if not name:
        raise CommandError("write @event <name> | <what happened>")
    witnessed = cmd.flag("witnessed")
    what = " | ".join(cmd.free_extras("witnessed")) or name
    npc_ids, place_ids = _involved(r, cmd)
    return create_event(name=_clip(name, 120), description=what, event_type="player" if witnessed else "meso",
                        npcs_involved=npc_ids, locations_involved=place_ids,
                        player_visible=True, player_witnessed=witnessed)


def _h_schedule(r: CommandRunner, cmd: ParsedCommand) -> dict:
    parts = cmd.extras
    if not parts:
        raise CommandError("write @schedule <when> | <name> | <what happens>")
    name, what = (parts[0], " | ".join(parts[1:])) if len(parts) > 1 else (_clip(parts[0], 60), parts[0])
    with get_session() as session:
        clock = session.query(WorldClock).first()
        now = (clock.day, clock.hour, clock.minute or 0) if clock else (1, 8, 0)
    when = parse_when(cmd.main, *now)
    if when is None:
        raise CommandError(f"could not read '{cmd.main}' as a future time — write in 3h, in 2d, tomorrow 08:00, tonight or day 3 20:00")
    npc_ids, place_ids = _involved(r, cmd)
    return schedule_world_event(name=_clip(name, 120), description=what, event_type="meso",
                                scheduled_day=when[0], scheduled_hour=when[1],
                                npcs_involved=npc_ids, locations_involved=place_ids)


# ----------------------------------------------------------------------
# The registry: verb -> handler, legacy tool name, usage, option keys
# ----------------------------------------------------------------------

_REGISTRY: dict[str, _Spec] = {
    # moving & time
    "move": _Spec(_h_move, "move_player", "@move <place> [| through]", frozenset({"through"})),
    "time": _Spec(_h_time, "advance_time", "@time <duration: 20m, 2h, 1h30m>"),
    "rest": _Spec(_h_rest, "rest", "@rest"),
    # the player
    "health": _Spec(_h_health, "update_player_health",
                    "@health <healthy|winded|hurt|badly_hurt|critical|dead> [| cause: …]", frozenset({"cause"})),
    "coin": _Spec(_h_coin, "adjust_currency", "@coin <+N|-N> [| reason]"),
    # items
    "give": _Spec(_h_give, "add_to_inventory",
                  "@give <item> [xN] [| kind: …] [| tier: …] [| potion: …] [| about: …] [| looks: …] "
                  "[| style: …] [| uniform: <faction>] [| hood]",
                  frozenset({"kind", "tier", "potion", "about", "looks", "style", "uniform", "hood"})),
    "remove": _Spec(_h_take, "remove_from_inventory", "@remove <item> [xN]"),
    "take": _Spec(_h_take_ambiguous, None, "@take — ambiguous; use @give or @remove"),
    "use": _Spec(_h_use, "use_item", "@use <item>"),
    "hand": _Spec(_h_hand, "transfer_item", "@hand <item> | to: <person>", frozenset({"to"})),
    "buy": _Spec(_h_buy, "transfer_item", "@buy <item> | from: <person>", frozenset({"from"})),
    "stock": _Spec(_h_stock, "stock_merchant",
                   "@stock <person> | <Name>: <weapon|armor|trinket> <tier> [xN] | <Name>: potion <tag> [xN]"),
    "interact": _Spec(_h_interact, "interact", "@interact <thing here> [| through]", frozenset({"through"})),
    # people
    "rep": _Spec(_h_rep, "update_player_reputation", "@rep <faction> <+N|-N>"),
    "trust": _Spec(_h_trust, "update_npc_relationship", "@trust <person> <+N|-N> [| <disposition>]",
                   frozenset({"disposition"})),
    "mood": _Spec(_h_mood, "update_npc_mood", "@mood <person> | <mood>"),
    "reveal": _Spec(_h_reveal, "reveal_secret", "@reveal <person> <index>"),
    "join": _Spec(_h_join, "join_party", "@join <person>"),
    "leave": _Spec(_h_leave, "leave_party", "@leave <person> [| reason]", frozenset({"reason"})),
    "npc-move": _Spec(_h_npc_move, "move_npc", "@npc-move <person> | <place>", frozenset({"to"})),
    "npc": _Spec(_h_npc, "update_npc",
                 "@npc <person> [| tier: …] [| trade: …] [| status: …] [| goal: …] [| secret: …] "
                 "[| fights: …] [| looks: …]",
                 frozenset({"tier", "trade", "status", "goal", "secret", "fights", "looks", "manner",
                            "voice", "mood", "cause"})),
    "new-npc": _Spec(_h_new_npc, "create_npc",
                     "@new-npc <name> | trade: … | looks: … | manner: … | voice: … [| wants: …] [| secret: …] "
                     "[| tier: …] [| fights: …] [| mood: …] [| at: <place>] [| status: dead|missing]",
                     frozenset({"trade", "looks", "manner", "voice", "wants", "secret", "tier", "fights", "mood", "at",
                                "status"})),
    "new-place": _Spec(_h_new_place, "create_location",
                       "@new-place <name> | about: … [| kind: …] [| inside: yes|no] [| minutes: N]",
                       frozenset({"about", "kind", "inside", "minutes"})),
    # fights
    "fight": _Spec(_h_fight, "start_combat", "@fight <person>[, <person>…]"),
    "kill": _Spec(_h_kill, "kill_npc", "@kill <person> [| cause]", frozenset({"cause"})),
    # quests
    "quest-start": _Spec(_h_quest_start, "activate_quest", "@quest-start <quest>"),
    "quest-done": _Spec(_quest_status(QuestStatus.COMPLETED), "update_quest_status", "@quest-done <quest>"),
    "quest-fail": _Spec(_quest_status(QuestStatus.FAILED), "update_quest_status", "@quest-fail <quest>"),
    "quest-new": _Spec(_h_quest_new, "create_quest",
                       "@quest-new <title> | about: … [| from: <person>] [| scale: …] | step: <kind> <target> … "
                       "[| reward: 30 coin] [| reward: <item>]",
                       frozenset({"about", "from", "scale", "step", "reward"})),
    "step-done": _Spec(_h_step_done, "update_quest_objectives", "@step-done <quest> | <step>"),
    # the story & the screen
    "tension": _Spec(_h_tension, "update_dm_state", "@tension <low|rising|high|climax|falling>"),
    "weather": _Spec(_h_weather, "play_cinematic", "@weather <rain|snow|fog|embers|ash|clear>"),
    "title": _Spec(_h_title, "play_cinematic", "@title <TITLE> [| subtitle]", frozenset({"subtitle"})),
    "vision": _Spec(_h_vision, "play_cinematic", "@vision"),
    "boss": _Spec(_h_boss, "play_cinematic", "@boss <person>"),
    "event": _Spec(_h_event, "create_event",
                   "@event <name> | <what happened> [| witnessed] [| npcs: a, b] [| place: X]",
                   frozenset({"npcs", "place", "witnessed"})),
    "schedule": _Spec(_h_schedule, "schedule_world_event", "@schedule <when> | <name> | <what happens>",
                      frozenset({"npcs", "place"})),
    # memory
    "note": _Spec(_h_note, "memory", "@note <person> | <what they now know or believe>"),
    "plan": _Spec(_h_plan, "memory", "@plan <a thread to pay off>"),
    "journal": _Spec(_h_journal, "memory", "@journal <a fact the character has learned>"),
}

# Spellings the model reaches for that mean exactly one documented verb
_ALIASES = {
    "go": "move", "travel": "move", "wait": "time", "sleep": "rest",
    "lose": "remove", "drop": "remove",
    "coins": "coin", "gold": "coin", "money": "coin", "attack": "fight",
    "quest-complete": "quest-done", "quest-activate": "quest-start",
    "move-npc": "npc-move", "create-npc": "new-npc", "create-place": "new-place",
}

# Verbs whose line, written twice word for word in one reply, is applied once
_ONCE_PER_REPLY = frozenset({"coin", "give", "remove"})

# "take" stays registered only to explain why it did nothing; it is not taught
_UNTAUGHT = {"take"}
COMMANDS: dict[str, str] = {verb: spec.usage for verb, spec in _REGISTRY.items() if verb not in _UNTAUGHT}


# A `key: value` segment the verb does not take is dropped — and the DM must
# hear it was. fleet5-wenna T3: `@new-npc Ennis Quill | … | status: dead`
# came back ✓ with no message and the dead man stood alive in the square.
# Not for the verbs whose extras ARE free text ("@event Fire | Smoke: the
# warehouse burned", a step's own words, a coin's reason); and a verb that
# reads its first extra in place of a missing option (`@health hurt | a
# bite` is the cause) keeps that one.
_FREE_TEXT_VERBS = frozenset({
    "time", "coin", "stock", "mood", "fight", "step-done", "note", "plan", "journal", "event",
    "schedule", "title",
})
_FIRST_EXTRA_FOR = {
    "health": "cause", "leave": "reason", "kill": "cause", "npc-move": "to", "hand": "to", "buy": "from",
    "new-place": "about", "quest-new": "about", "trust": "disposition",
}


def _ignored_note(verb: str, spec: _Spec, cmd: ParsedCommand) -> str:
    """"ignored status: dead (@new-npc takes …)" for each unknown key: value
    segment the verb dropped, or ""."""
    if verb in _FREE_TEXT_VERBS:
        return ""
    extras = list(cmd.extras)
    fallback = _FIRST_EXTRA_FOR.get(verb)
    if fallback and not cmd.opt(fallback):
        free = [e for e in extras if e.strip().lower() not in spec.keys
                and not re.fullmatch(r"\s*[+\-−–]?\s?\d+\s*", e)]
        if free:
            extras.remove(free[0])          # read as the missing option
    dropped = [e for e in extras if _KEY_RE.match(e)]
    if not dropped:
        return ""
    takes = f"@{verb} takes {', '.join(sorted(spec.keys))}" if spec.keys else f"@{verb} takes no key: value options"
    return f"ignored {', '.join(_clip(d, 60) for d in dropped)} ({takes})"

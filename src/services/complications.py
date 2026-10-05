"""A world with teeth: after a quiet stretch, one complication.

fleet5-wenna played 21 turns with no fight, no wound and no failed roll; the
eight night turns she spent in town (T11-T18) had no incident at all. The
prompt says nothing is free, and on a quiet stretch the DM forgets it. So the
engine counts. A turn that cost the player nothing — no fight, no health
lost, no failed or partial roll, no refusal, nothing stolen, no coin lost —
is a quiet one. After QUIET_TURNS of them (plus a small deterministic
jitter, so it never lands on a count the player can feel), the next ordinary
reply gets ONE directive line: a complication picked here, in code, from a
menu that fits where the player stands — a watcher at the window or a hand
in the satchel in town, fresh tracks or a hunting call in the wild. The DM
writes it as something that costs or forces a choice. The count starts over
when it fires, so two never come in a row.

Never on an engine turn ([OPENING], [COMBAT OUTCOME], an ambush), never in a
parley, never when a campaign beat or a world event already owns the reply,
never on the morning a rest just opened. In memory, like the encounter
director: a restart only resets the count.

Round 5 (fleet6) found threats that never acted: the tamsin watcher fired at
T17, "hasn't moved" T18-T20, was never made a person, and wore Vex Marrow's
ash cloak; the T17 reply copied the template nearly word for word. So:

  * every complication is tied to a real entity — an ambient, unmet,
    unstaged person already there, or a ready `@new-npc` stub in the line —
    and is never dressed like the story's villain or successor;
  * it stays OPEN with its "ignored" branch. A reply whose commands act on
    it closes it; UNANSWERED_REPLIES replies without one (or the player
    walking off) re-inject it as [COMPLICATION — UNANSWERED], which needs a
    command now. One nudge, then it is let go;
  * the wording rotates, and the line says to put it in the DM's own words;
  * bad news never touches the world's schedule or the story's clock
    (fleet6-wenna T9 told a still-scheduled cart as already lost), and a
    summons cites only what the summoner could know in public (fleet6-wenna
    T18 knew "about the letter" she read alone upstairs).

And at night, at the edge of town, with the danger rising in a beast-haunted
story, the complication is a real one: ONE weak beast scout made in code
(src/services/spawn.py) — fight it, drive it off, or try something stranger
(fleet6-wenna stood at the paddock fence three night turns while the howl
"walked the fence line in", and nothing came).

Round 7 (fleet8): a vigil is the player's own choice, and the world honours
it. A player keeping watch for something (`keeping_vigil`: "I'll stay",
"I'm going to stand out here and mark the next howl", settling into the
loft to listen) is never pulled off it by a town complication — it waits
(fleet8-tamsin T8: a summons dragged her out of the loft twenty minutes
before the howl she had spent three turns setting up), and so does every
complication while a public scheduled event is due within the hour near
them (the event IS what they are waiting for). What the vigil watches for
may still come: a night watch at the town's edge brings the scout whatever
the tension, after one quiet reply (fleet8-bram T14 stood at the paddock
fence at night for the howl and nothing came). Bad news is carried by
nobody new and costs only what follows from the player's own doing
(fleet8-wenna T20: a made-up "Yura" and @trust -5 with the Captain for the
town's mood).

Round 8 (fleet9): danger with choices.

  * A theft is a choice before it is a cost (fleet9-bram T11: thief, an
    unchosen roll and @coin -6 in one reply, every sentence of it dropped,
    "answered"): the reply that brings it shows the hand and the thief in
    sight, nothing charged or rolled; the thief stays quarry (a fight chip)
    for two replies; its cost answers it only once the player SAW it
    (turn_audit.theft_shown), else the next reply shows it, never charging it
    twice.
  * A beast that left — the engine sent a calmed or prose-dismissed wolf home
    (encounters.send_off) — closes its complication; one not with the player
    is never pressed into a fight (fleet9-wenna T14 -> T16).
  * A run of talk ends on a choice with a clock (fleet9-wenna T1-T15): after
    TALK_TURNS talk-only replies, a way about to bar at nightfall ("go now or
    wait for dawn") or someone packing up for the night ("ask now or lose
    them till morning"), never through a vigil or with a danger present.
  * Going over the wall at night draws the night-edge scout, like a watch
    kept at the fence (the barred night's ways: a bribe, the wall, a bed).

Round 9 (fleet10): pushes with something behind them.

  * No quiet- or talk-streak push while the player is with the person of the
    step they work, or of a beat waiting at this place (`_working_with`;
    fleet10-tamsin T7: a runner pulled her off Sorrel mid-conversation).
  * A summons carries news or a clock (`_summons_news`) — or there is none
    (fleet10-tamsin T7-T10: four replies walked back for "a day late" again).
  * A watcher is a lookout of the story's other side, made by the engine with
    a name in the world's own style (`world_name`) and a tell the player can
    learn (`_faction_watcher`; fleet10-bram T7-T10's stock laundress).
"""

from __future__ import annotations

import logging
import re
import zlib
from typing import Any, Iterable

from src.models import NPC, Location, LocationType, Player, get_session

logger = logging.getLogger(__name__)

QUIET_TURNS = 6
JITTER = 3                    # the threshold is QUIET_TURNS .. QUIET_TURNS + JITTER - 1
COSTLY_OUTCOMES = ("partial", "failure", "critical_failure")
_HEALTH_LADDER = ("dead", "critical", "badly_hurt", "hurt", "winded", "healthy")
UNANSWERED_REPLIES = 2        # replies after the one that brought it, before it is pressed
SCOUT_QUIET = 3               # quiet turns before a night-edge scout (once per game day)
VIGIL_SCOUT_QUIET = 1         # ...when the player stands a night watch at the edge for it
VIGIL_MAX_WAITS = 3           # replies in a row a town complication waits out a vigil
EVENT_SOON_MINUTES = 60       # a public event due this soon near the player owns the hour
NIGHT_HOURS = set(range(20, 24)) | set(range(0, 6))
_RISING = ("rising", "high", "climax")
# Talk-only replies in a row before the world forces a choice with a clock
# on it (fleet9-wenna T1-T15: an interview treadmill, the same clue from four
# mouths, and no choice with a cost until a wolf came over the rail)
TALK_TURNS = 5
CLOSING_HOURS = 3             # a way that bars at nightfall is pressed this long before it
EVENING_HOUR = 17             # ...and from this hour people pack up for the night

# Per player: quiet turns in a row, complications fired so far, the last kind
_quiet: dict[str, int] = {}
_fired: dict[str, int] = {}
_last_kind: dict[str, str] = {}
# ...the complication still waiting for an answer, and the day a scout came
_open: dict[str, dict[str, Any]] = {}
_scout_day: dict[str, int] = {}
# ...and how many replies in a row a complication waited out their vigil
_vigil_waits: dict[str, int] = {}
# ...talk-only replies in a row, and audit lines for the server to attach
_talk: dict[str, int] = {}
_warnings: dict[str, list[dict[str, str]]] = {}
# ...and who spoke in their last two replies (`_working_with`)
_spoke: dict[str, list[set[str]]] = {}
# Kinds whose reply brings only the choice — nothing charged, nothing rolled
_CHOICE_FIRST = frozenset({"pickpocket"})

# (kind, what happens, how it bites) — the DM gets one, worded as an order.
# `needs`: "goods" (something to steal), "quest" (a thread someone else wants)
_TOWN = (
    ("watcher", None,
     "someone is watching them — a figure at a window or across the room who looks away a moment too "
     "late, and is still there when they look again",
     "they confront the watcher, slip away from them (it costs time: @time), or let it go and are "
     "followed (@tension rising)"),
    # A choice, not a verdict (fleet9-bram T11: the DM wrote the thief, an
    # unchosen "noticing" roll and @coin -6 in one reply, and the player never
    # saw a thing): this reply shows the hand and the thief in sight, and the
    # cost waits for the player's answer
    ("pickpocket", "goods",
     "a hand at their belt — someone brushes past in the press, they feel it go, and the thief is still in "
     "sight, moving off with something of theirs",
     "this reply shows ONLY the hand and the thief in sight, moving off — the player chooses: grab the wrist, "
     "give chase (roll_check then), call out, or let it go. The cost lands only once they answer or let it go "
     "(@coin -N from their purse, or @remove one small thing — never what they wear, never a clue or a "
     "quest's proof); a thief caught is theirs to deal with"),
    ("rival", "quest",
     "someone else has been asking the same questions they have, a step ahead of them, and people "
     "have started to notice",
     "they find out who, or the trail goes cold where the rival has been (@trust down with whoever "
     "talked to the rival, or @time spent catching up)"),
    ("summons", None,
     "a summons — a runner or a note: someone with standing wants them, now, about what the town SAW "
     "them do, and refusing has a price",
     "going costs them what they were doing (@time); refusing costs standing (@rep or @trust down)"),
    ("bad_news", None,
     "bad news — word that something THEY set in motion here has gone wrong: someone they spoke with "
     "is in trouble for it, a price they rely on just rose, a door they need is closing",
     # fleet8-wenna T20: "@trust Captain Brann Coldiron -5 | the town's fear …
     # curdle into anger at the Watch's silence" — for nothing she did
     "it changes what they can do next: a door closes, a price rises, someone is now afraid "
     "(@tension rising, or @coin for a price that rose; @trust down only with someone over what the "
     "player themselves did with them — never for the town's mood)"),
)
_WILD = (
    ("tracks", None,
     "fresh tracks cross their path — big, recent, and heading the same way they are",
     "they follow them (@time, and what waits at the end), or go around and lose the time (@time)"),
    ("snapped_branch", None,
     "a branch snaps somewhere close; something is keeping pace with them just out of sight",
     "they stand and face it, or press on watched (@tension rising)"),
    ("hunting_call", None,
     "a hunting call rises — and is answered from the other side of them",
     "they are being herded: hold ground, break for it (@time, maybe @health), or hide (roll_check)"),
)

# More ways to say each one: fleet6-tamsin T17 copied the watcher's line
# almost word for word ("half-turned away a beat too late... still there").
# The first wording is the menu's own; the count of complications so far
# picks the next.
_WORDINGS: dict[str, tuple[str, ...]] = {
    "watcher": (
        "a face keeps turning up wherever they stand — by the door, across the room, at the next stall — "
        "and it stops pretending it is chance",
        "someone has matched their pace twice now, and this time does not bother to look busy",
    ),
    "pickpocket": (
        "someone small and quick bumps them hard in passing and is already moving away too fast — their "
        "purse strings hang loose, and the bumper is still in sight",
        "the press of bodies closes round them for a breath, and when it opens their pack is lighter and a "
        "back is slipping away between the stalls",
    ),
    "rival": (
        "someone was here an hour ago asking their exact questions, and paid better for the answers",
        "a name they did not expect keeps coming up — another hand on the same thread, ahead of them",
    ),
    "summons": (
        "word comes by a runner, out of breath: someone with standing wants them now, for what they were "
        "seen doing, and is not used to waiting",
    ),
    "bad_news": (
        "news reaches them that the thing they started here has cost someone — and that someone is "
        "angry, or frightened, or gone",
    ),
    "tracks": (
        "the ground ahead is freshly marked — prints too big and too new, going their way",
    ),
    "snapped_branch": (
        "the brush to one side goes quiet in the wrong way, and stays level with them as they move",
    ),
    "hunting_call": (
        "two calls cross over their head, one ahead and one behind, close enough to answer each other",
    ),
}

# What happens if the player lets it go — the line the engine presses when
# UNANSWERED_REPLIES replies wrote nothing that acted on it
_IGNORED: dict[str, str] = {
    "watcher": "they let it go, so the watcher acts: follows them when they move (@npc-move {who} | <the "
               "player's place>), or comes over now, or something of theirs leaves with the watcher (@remove)",
    "pickpocket": "the thief is gone with it: @remove one small thing (never what they wear, never a clue or "
                  "a quest's proof) or @coin -N from their purse",
    "rival": "the rival got there first: @trust -5 with whoever talked to them, or the trail costs time (@time)",
    "summons": "they did not come, and refusing has its price: @trust {who} -5 (or @rep down with their side)",
    "bad_news": "it lands on them now: @tension rising (a @trust or @coin only for what the player "
                "themselves did)",
    "tracks": "what made them finds the player first: @fight it if it is here, or @tension rising — it is closer",
    "snapped_branch": "it shows itself: @fight {who}, or it breaks off with a sound that says it will be back "
                      "(@tension rising)",
    "hunting_call": "the circle closes: @fight, or they break for it (@time, and maybe @health)",
    "scout": "it comes for them over the rail: @fight {who}",
    "closing": "the window shut while they talked: show it shut (a bar dropping, a door closing, a back "
               "turned) — the way it barred stays barred tonight (a bribe, the wall or a bed till dawn)",
    "leaving": "they are gone for the night: @npc-move {who} | {home}",
}

# Commands that answer each kind (a fight, or the entity speaking, answers
# any of them): "@tension rising" in the reply that BROUGHT a watcher is no
# answer — fleet6-tamsin T17 wrote it before the player had chosen anything
_ACTS: dict[str, frozenset[str]] = {
    "watcher": frozenset({"npc-move", "remove", "coin-", "trust", "time"}),
    "pickpocket": frozenset({"remove", "coin-", "hand", "trust"}),
    "rival": frozenset({"trust-", "rep-", "time", "npc-move"}),
    "summons": frozenset({"move", "time", "trust-", "rep-"}),
    "bad_news": frozenset({"tension", "trust-", "coin-", "rep-", "remove", "time"}),
    "tracks": frozenset({"time", "move", "tension"}),
    "snapped_branch": frozenset({"tension", "npc-move", "time"}),
    "hunting_call": frozenset({"time", "health", "move"}),
    "scout": frozenset({"npc-move", "remove", "npc"}),
    "closing": frozenset({"move", "time", "rest"}),
    "leaving": frozenset({"npc-move", "move"}),
}
_ALWAYS_ACTS = frozenset({"fight", "kill"})

# A complication's stranger, ready to write. Never a name or a look the
# story already gave someone (checked against the world at fire time)
_TOWN_STUBS = (
    ("Corran Pike", "drover", "lean and weathered, a drover's oilskin, one leather glove",
     "patient, unreadable", "low, says little"),
    ("Ilse Marrick", "laundress", "red-knuckled, sleeves rolled, a grey shawl pinned with a bone pin",
     "sharp-eyed, quick", "clipped, answers a question with a question"),
    ("Wat Fenning", "porter", "broad and bald, a porter's strap across his chest, sawdust on his sleeves",
     "slow to anger, slower to forget", "rumbling, few words"),
    ("Nell Brisk", "peddler", "small and wiry, a peddler's tray on a strap, a green felt cap",
     "cheerful, nosy", "fast and sing-song"),
)
_BEAST_STUB_LOOKS = ("Torn-Ear", "Rib-Thin", "Pale-Muzzle", "Burr-Coat", "Lame-Foot")
# Animals that hunt people: a horse in the stable yard is no beast-haunted story
PREDATORS = frozenset({
    "wolf", "wolves", "bear", "boar", "lion", "tiger", "panther", "leopard", "jaguar", "lynx", "hyena",
    "jackal", "coyote", "wolverine", "spider", "serpent", "viper", "snake", "wyvern", "drake", "wyrm",
    "raptor", "crocodile", "basilisk", "manticore", "griffin", "hound", "hounds",
})
_EDGE_WORDS = re.compile(r"\b(?:gate|gates|wall|walls|palisade|fence|paddock|stables?|yard|outskirts|edge|"
                         r"rampart|watchtower)\b", re.IGNORECASE)

# Places that are wild whatever their type (Wolf's Hollow sits under the town
# in the hierarchy, past its gate)
_WILD_WORDS = re.compile(
    r"\b(?:hollow|woods?|forest|wilds?|wilderness|road|trail|path|hills?|moor|marsh|bog|swamp|fen|"
    r"cave|cavern|den|ruins?|camp|clearing|glade|ridge|pass|crag|heath|thicket|riverbank|shore)\b",
    re.IGNORECASE,
)
_TOWN_TYPES = {LocationType.SETTLEMENT, LocationType.CITY, LocationType.TOWN, LocationType.VILLAGE,
               LocationType.DISTRICT, LocationType.BUILDING, LocationType.INTERIOR, LocationType.ROOM}
_INDOORS = {LocationType.BUILDING, LocationType.INTERIOR, LocationType.ROOM}


def reset_state() -> None:
    """Forget every count (tests, world switches)."""
    _quiet.clear()
    _fired.clear()
    _last_kind.clear()
    _open.clear()
    _scout_day.clear()
    _vigil_waits.clear()
    _talk.clear()
    _warnings.clear()
    _spoke.clear()


def open_complication(player_id: str) -> dict[str, Any] | None:
    """The complication still waiting for an answer, if any."""
    return _open.get(player_id)


def talk_streak(player_id: str) -> int:
    """Talk-only replies in a row (`note_turn`)."""
    return _talk.get(player_id, 0)


def pop_warnings(player_id: str) -> list[dict[str, str]]:
    """Audit lines the last reply's complication bookkeeping raised (the
    server attaches them to the turn's [REALITY AUDIT])."""
    return _warnings.pop(player_id, [])


def note_calmed(player_id: str, npc_ids: Iterable[str]) -> bool:
    """A roll the player won faced down these NPCs (encounters.note_check):
    the complication that brought one of them is answered — fleet8-wenna
    T11 calmed the night-edge scout on a real roll, and its "if they ignore
    it, it comes for them" press would have undone that too."""
    op = _open.get(player_id)
    if op is None or not op.get("entity_id") or op["entity_id"] not in set(npc_ids or ()):
        return False
    logger.info(f"Complication {op['kind']} answered by a won roll for {player_id}")
    _open.pop(player_id, None)
    return True


# The player keeping watch for something, by their own choice: their actions
# (outside quotes), or their own "I'll stay" / "I'm going to stand out here"
# said aloud. "Sorrel, stay here tonight. Listen for when that answer-back
# howl comes" (fleet8-tamsin T9, as she left with the runner) is an order to
# someone else, not her vigil
# ...and waiting on an answer, or watching a face for a lie, is the talk
_TALK_OBJECT = (r"(?!\s+(?:(?:any|a|an|the|some)\s+)?(?:(?:his|her|their|your|my)\s+|signs?\s+of\b|answers?\b"
                r"|reply\b|response\b|word\b|decision\b|verdict\b|reaction\b|flinch\b|flicker\b|tell\b|turn\b"
                r"|(?:him|her|them|you)\s+to\s+(?:answer|speak|reply|finish|respond|decide|say|explain|continue"
                r"|go\s+on|talk|think)\b))")
_VIGIL_ACT = re.compile(
    rf"\b(?:wait(?:s|ing)?\s+(?:for|until|till){_TALK_OBJECT}|wait(?:s|ing)?\s+(?:out|here|there|at|by|beside)"
    r"|(?:keep|keeps|keeping|stand|stands|standing|hold|holds|holding)\s+(?:a\s+|my\s+)?(?:watch|vigil|post)"
    r"|settl(?:e|es|ing)\s+(?:in|into|down)\s+(?:position|cover|to\s+"
    r"(?:wait|watch|listen))|stake\s+out|lie\s+in\s+wait|vigil"
    rf"|(?:watch|watches|watching|listen|listens|listening)\s+(?:hard\s+|close(?:ly)?\s+)?for{_TALK_OBJECT}"
    r"|(?:stand|stay|wait)(?:s|ing)?\s+out\s+(?:here|there|tonight|the\s+(?:night|howl|dark))"
    r"|(?:position|positions|positioning|plant|plants|planting)\s+(?:myself|me)\s+(?:where|by|at|near|on|in|beside)"
    r"|until\s+(?:it|the\s+[\w-]+|they|he|she)\s+(?:comes?|starts?|rises?|answers?|shows?|returns?))\b",
    re.IGNORECASE,
)
# A bare "I'll listen" takes a job (fleet8-bram T10, fleet6-tamsin T7), and
# "I'll stay out of it" or "I'll wait for your answer" keeps no watch
_VIGIL_SAID = re.compile(
    r"\b(?:I|we)(?:['’]ll|\s+will|['’]m\s+(?:going\s+to|gonna)|\s+am\s+going\s+to|['’]re\s+going\s+to)\s+"
    r"(?:[\w'’-]+\s+){0,2}?(?:wait(?!\s+(?:for|on)\s+(?:(?:his|her|their|your|my|an?|the)\s+)?"
    r"(?:answers?|reply|response|word|decision|verdict|you|him|her|them)\b)"
    r"|stay(?!\s+(?:out\s+of|away|quiet|calm|close|silent|put|with)\b)|stand\s+(?:out|watch|here|guard)"
    r"|keep\s+watch|watch\s+(?:for|out|from)|listen\s+(?:for|out)|mark\s+(?:it|the|where|when))\b",
    re.IGNORECASE,
)
_QUOTED = re.compile(r"[\"“][^\"”\n]*[\"”]")
# ...and only a watch the player keeps, now: "I don't wait for a full
# answer" (fleet3-bram T10), "without waiting for more" (fleet8-bram T11) and
# "a hot meal … beats standing watch in the cold" (fleet7-bram T20, on his way
# to bed) keep none, and "like he's waiting for permission to exist"
# (fleet-tamsin T7) is someone else's. On the replayed corpus these were 13
# of 32 matches — each one a night-edge scout sent at a player who was not
# watching for anything
_NOT_KEPT = re.compile(r"\b(?:not|never|no|without|don['’]?t|won['’]?t|instead\s+of|than|beats)\s+"
                       r"(?:[\w'’-]+\s+){0,2}$", re.IGNORECASE)
_SUBJECT = re.compile(r"\b(?:(I)|[Hh]e|[Ss]he|[Tt]hey|[Ss]ome(?:one|body)|[Ww]ho)(?:['’](?:s|re|d|m|ll|ve))?\b")
_CLAUSE_END = re.compile(r"[.!?…\n;—–]")
_SENTENCE_END = re.compile(r"[.!?…\n]")


def _kept_by_player(text: str, match: re.Match) -> bool:
    """Is this watch-word the player's own, not negated? The sentence's last
    subject before it is "I" (or there is none), and no "not"/"without"
    stands just before it in its clause."""
    clause = max((m.end() for m in _CLAUSE_END.finditer(text, 0, match.start())), default=0)
    if _NOT_KEPT.search(text[clause:match.start()]):
        return False
    sentence = max((m.end() for m in _SENTENCE_END.finditer(text, 0, match.start())), default=0)
    subjects = _SUBJECT.findall(text[sentence:match.start()])
    return not subjects or subjects[-1] == "I"


def keeping_vigil(player_input: str) -> str | None:
    """The words of a watch the player keeps by choice, or None."""
    text = (player_input or "").strip()
    if not text or text.startswith("["):
        return None
    bare = _QUOTED.sub(" ", text)
    m = next((hit for hit in _VIGIL_ACT.finditer(bare) if _kept_by_player(bare, hit)), None)
    if m is None:
        m = next((hit for quote in _QUOTED.findall(text) for hit in _VIGIL_SAID.finditer(quote)
                  if _kept_by_player(quote, hit)), None)
    return m.group(0).strip() if m else None


# A reply the player spent talking: they said something or asked something.
# What breaks the streak is anything done to the world — a roll, a fight, a
# cost, a thing handled, a bed, a trade; a walk between two interviews is
# neither (fleet9-wenna walked Square -> Gatehouse -> Square between them)
_ASKED = re.compile(r"\?|[\"“][^\"”\n]{3,}[\"”]")
_ACT_VERBS = frozenset({"interact", "fight", "attack", "kill", "rest", "buy", "sell", "use", "equip"})


def _talk_turn(player_input: str, commands: list[Any], roll_outcomes: Iterable[str], bit: bool) -> bool | None:
    """True for a talk-only reply, False for one that did something, None
    for neither (a walk)."""
    from src.services.commands import _ALIASES, parse_command

    if bit or any(True for _ in roll_outcomes or ()):
        return False
    verbs = set()
    for row in commands:
        get = row.get if isinstance(row, dict) else (lambda key, _r=row: getattr(_r, key, None))
        parsed = parse_command(str(get("line") or "")) if get("ok") else None
        if parsed is not None:
            verbs.add(_ALIASES.get(parsed.verb, parsed.verb))
    if verbs & _ACT_VERBS:
        return False
    if "move" in verbs:
        return None
    return True if _ASKED.search(player_input or "") else None


# Going over the wall at night: a roll-first act (roll_intent) that draws the
# night-edge scout like a watch kept at the fence does (round 8: the barred
# gate's ways — a bribe, the wall, a bed till first light)
_WALLS = re.compile(r"\b(?:wall|walls|palisade|rampart|stockade|gate)\b", re.IGNORECASE)
# ...a climb, not a flight (fleet7-wenna T16 "I turn and run for the road")
_CLIMB = re.compile(r"\b(?:climb\w*|scal(?:e|es|ing)|scrambl\w*|shimmy\w*|vault\w*|over)\b", re.IGNORECASE)


def climbing_out(player_input: str) -> bool:
    """Is the player going over a wall (or the gate) — a climb they make now?"""
    from src.services.roll_intent import risky_act

    act = risky_act(player_input)
    return bool(act and act[0] == "athletics" and _CLIMB.search(act[1])
                and _WALLS.search(_QUOTED.sub(" ", player_input or "")))


def _event_soon(session, player: Player) -> str | None:
    """A public scheduled event due within EVENT_SOON_MINUTES at or near
    where the player stands (or everywhere): it owns the coming hour."""
    from src.models import Event, WorldClock

    clock = session.query(WorldClock).first()
    if clock is None:
        return None
    now = ((clock.day or 1) * 24 + (clock.hour or 0)) * 60 + (clock.minute or 0)
    here = session.get(Location, player.current_location_id) if player.current_location_id else None
    near: set[str] = set()
    if here is not None:
        near = {here.id} | ({here.parent_id} if here.parent_id else set())
        near |= {loc.id for loc in session.query(Location).filter(
            Location.parent_id.in_([i for i in near if i])).all()}
    for event in session.query(Event).filter(Event.scheduled_day.isnot(None), Event.occurred_day.is_(None),
                                             Event.player_visible == True).all():  # noqa: E712
        due = ((event.scheduled_day or 1) * 24 + (event.scheduled_hour or 0)) * 60
        places = set(event.locations_involved or [])
        if 0 <= due - now <= EVENT_SOON_MINUTES and (not places or places & near):
            return event.name or "a scheduled event"
    return None


def quiet_turns(player_id: str) -> int:
    return _quiet.get(player_id, 0)


def threshold(player_id: str) -> int:
    """QUIET_TURNS plus a jitter fixed by who they are and how many came
    before: deterministic, but not the same count every time."""
    seed = zlib.crc32(f"{player_id}:{_fired.get(player_id, 0)}".encode())
    return QUIET_TURNS + seed % JITTER


def _worse(after: str | None, before: str | None) -> bool:
    try:
        return _HEALTH_LADDER.index(str(after)) < _HEALTH_LADDER.index(str(before or "healthy"))
    except ValueError:
        return False


def _stealable(player: Player) -> bool:
    """Coin, or something in the pack a thief could lift: not what they wear
    (every character starts with a knife in hand) and not a clue — @remove
    has no guard, and the bone whistle is the quest's proof."""
    if (player.currency or 0) > 0:
        return True
    from src.services import progression

    worn = {i for i in progression.equipment_pointers(player).values() if i}
    return any(
        isinstance(entry, dict) and progression.item_key(entry) not in worn
        and str(entry.get("type") or "").strip().lower() != "quest_item"
        for entry in player.inventory or []
    )


def _command_bit(commands: Iterable[Any], player_input: str = "") -> bool:
    """A command that cost the player or fought for them: @fight, @kill, a
    thing taken (@remove), coin lost (@coin -N), a road the engine barred.
    Never what the player chose to give away (`_given_away`)."""
    from src.services.commands import parse_command, parse_signed

    for row in commands or ():
        get = row.get if isinstance(row, dict) else (lambda key, _r=row: getattr(_r, key, None))
        result = get("result") or {}
        if isinstance(result, dict) and result.get("blocked"):
            return True                 # the engine refused them the way
        if not get("ok"):
            continue
        parsed = parse_command(str(get("line") or ""))
        if parsed is None:
            continue
        if parsed.verb in ("fight", "attack", "kill"):
            return True
        if parsed.verb in ("remove", "lose", "drop") and not _given_away(parsed, player_input):
            return True
        if parsed.verb in ("coin", "coins", "gold", "money"):
            value = parsed.main or (parsed.extras[0] if parsed.extras else "")
            if (parse_signed(value) or 0) < 0 and not _given_away(parsed, player_input):
                return True
    return False


# A gift is no friction. Round 6, fleet7-wenna: T3's `@remove Dried Lavender
# Bundle` ("I pull out a small bundle of dried lavender… and offer it to her.
# 'A gift…'") and T6's `@coin -2 | a few coins pressed into Sorrel's hand for
# his trouble` each started the quiet count over, and nine turns of eager
# witnesses and gift-giving never reached a complication. What the world
# takes (a thief, a fine, a toll the DM charges) still counts: its reason says
# so, or the player never reached for it.
_GIVEN_REASON = re.compile(
    r"\b(?:gift(?:s|ed)?|tip(?:s|ped)?|thanks|thank-you|kindness|for\s+(?:his|her|their|your)\s+(?:trouble|help)"
    r"|pressed\s+(?:in|into|on)|given\s+(?:away|freely)|gives?\s+(?:it\s+)?away|gave|offered|donat\w*|alms"
    r"|pays?\s+for|paid\s+for|bought|buys?)\b",
    re.IGNORECASE,
)
_GIVE_VERB = re.compile(
    r"\b(?:give|gives|giving|gave|offer|offers|offering|offered|hand|hands|handing|handed|press|presses|"
    r"pressing|pressed|slip|slips|slipping|toss|tosses|tossing|tossed|gift|gifts|tip|tips|tipped)\b",
    re.IGNORECASE,
)
_MONEY_WORDS = re.compile(r"\b(?:coin|coins|silver|copper|gold|money|purse|crowns?|pennies)\b", re.IGNORECASE)
# ...but a price the world sets is friction however it is worded: "paid for
# the toll", "the coin the guard demands", a bribe to get past, a theft
_IMPOSED = re.compile(
    r"\b(?:tolls?|fined|fines|fine\s+(?:for|of)|(?:pays?|paid|paying)\s+(?:a|the|his|her|their)\s+fine"
    r"|bribe\w*|tax|taxes|levy|levied|tribute|ransom|extort\w*|demand\w*|stol(?:e|en)|steal\w*|thie(?:f|ves)"
    r"|pickpocket\w*|cutpurse|robb\w*|snatch\w*|confiscat\w*|seiz\w*|forfeit\w*|lost|loses|losing)\b",
    re.IGNORECASE,
)


def _given_away(parsed: Any, player_input: str) -> bool:
    """Did the player give this away by choice? The command's own reason
    says a gift, a tip, a purchase; or, with no reason, what the player did
    hands over that very thing (its name, or coin). Never a price the world
    imposed (`_IMPOSED`)."""
    reason = " ".join(str(e) for e in (parsed.extras or []))
    if reason.strip():
        return bool(_GIVEN_REASON.search(reason)) and not _IMPOSED.search(reason)
    text = player_input or ""
    if _IMPOSED.search(text):
        return False
    if not _GIVE_VERB.search(text):
        return False
    if parsed.verb in ("coin", "coins", "gold", "money"):
        return bool(_MONEY_WORDS.search(text))
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(word)}", lowered)
               for word in re.findall(r"[a-z]{4,}", str(parsed.main or "").lower()))


def note_turn(player_id: str, *, player_input: str = "", commands: Iterable[Any] = (),
              roll_outcomes: Iterable[str] = (), state_before: dict[str, Any] | None = None,
              state_after: dict[str, Any] | None = None, ambushed: bool = False,
              refused: bool = False, speaker_ids: Iterable[str] = (),
              location_id: str | None = None, cost_seen: bool | None = None) -> int:
    """Post-turn: count a quiet turn, or start over after one with teeth.
    Engine turns ([OPENING]…) do not count; a [COMBAT OUTCOME] is a fight.
    `speaker_ids` (who spoke) and `location_id` (where the player ended
    up) tell whether the open complication was answered; `cost_seen`
    (turn_audit.theft_shown over what the player read; None: unknown)
    whether a theft's cost reached the screen. Returns the quiet count."""
    if not player_id:
        return 0
    text = (player_input or "").lstrip()
    if text.startswith("[COMBAT OUTCOME"):
        _quiet[player_id] = 0
        _talk[player_id] = 0
        _open.pop(player_id, None)          # the fight answered whatever was open
        return 0
    if text.startswith("["):
        return _quiet.get(player_id, 0)
    speaker_ids = list(speaker_ids or ())
    _spoke[player_id] = (_spoke.get(player_id, []) + [set(speaker_ids)])[-2:]
    commands = list(commands or ())
    try:
        _track_open(player_id, commands, speaker_ids, location_id, fought=ambushed,
                    cost_seen=cost_seen, player_input=player_input or "")
    except Exception as e:
        logger.warning(f"open complication tracking failed: {e}")
    before, after = state_before or {}, state_after or {}
    roll_outcomes = list(roll_outcomes or ())
    bit = (
        ambushed or refused
        or any(o in COSTLY_OUTCOMES for o in roll_outcomes)
        or _worse(after.get("health"), before.get("health"))
        or _command_bit(commands, player_input or "")
    )
    _quiet[player_id] = 0 if bit else _quiet.get(player_id, 0) + 1
    try:
        talked = _talk_turn(player_input, commands, roll_outcomes, bit)
    except Exception as e:      # the count is a nudge, never a cost to the turn
        logger.debug(f"talk streak not counted: {e}")
        talked = None
    if talked is True:
        _talk[player_id] = _talk.get(player_id, 0) + 1
    elif talked is False:
        _talk[player_id] = 0
    return _quiet[player_id]


_COST_LINE = re.compile(r"\s*@(?:coin|coins|gold|money|remove|lose|drop)\b", re.IGNORECASE)
# What a thief's take reads like when its line gives a reason: "@coin -6 | a
# quick hand in the press lifts coin from his purse" (fleet9-bram T11). A
# bribe at the barred gate, a toll, a bed or a draught drunk while the thief
# is still in sight is not the theft
_THEFT_REASON = re.compile(
    r"\b(?:thie(?:f|ves|ving)|stol(?:e|en)|steal\w*|pickpocket\w*|cutpurse|snatch\w*|lift(?:s|ed|ing)?"
    r"|filch\w*|pinch(?:ed|es)?|nick(?:ed|s)?|swip(?:e|es|ed)|purse|pocket|pouch|belt|hand|press|crowd|brush\w*"
    r"|bump\w*|gone|took|taken|lost|rob\w*)\b",
    re.IGNORECASE,
)


def _theft_cost(line: str, player_input: str, entity_name: str | None) -> bool:
    """Is this landed line the thief's take — coin out (never in) or a thing
    removed, not given away by the player, its reason (if any) about the
    theft or the thief?"""
    from src.services.commands import parse_command, parse_signed

    if not _COST_LINE.match(line or ""):
        return False
    parsed = parse_command(line)
    if parsed is None or _given_away(parsed, player_input):
        return False
    if parsed.verb in ("coin", "coins", "gold", "money"):
        value = parsed.main or (parsed.extras[0] if parsed.extras else "")
        # (a payout, or coin won back from the thief, is no cost)
        if (parse_signed(value) or 0) >= 0:
            return False
    extras = [str(e) for e in parsed.extras or []]
    reason = " ".join(extras if parsed.main else extras[1:])      # "@coin | -6 | why": the value came first
    if not reason.strip():
        return True
    own = {w.lower() for w in re.findall(r"[A-Za-z][\w'’-]{2,}", entity_name or "")}
    return bool(_THEFT_REASON.search(reason)) or bool(own & {w.lower() for w in re.findall(r"[A-Za-z][\w'’-]+",
                                                                                           reason)})


def _row_line(row: Any) -> tuple[str, bool]:
    """(line, ok) of a CommandResult or a transcript row."""
    get = row.get if isinstance(row, dict) else (lambda key, _r=row: getattr(_r, key, None))
    return str(get("line") or ""), bool(get("ok"))


# Verbs that act on a PERSON: they answer the complication only when they
# name its person ("@npc-move Piper | home" is no answer to the watcher).
# A fight with anyone answers anything: the scene is a fight now
_ON_SOMEONE = frozenset({"npc-move", "npc", "trust", "trust-"})


def _applied_tags(commands: Iterable[Any], entity_name: str | None = None) -> set[str]:
    """What the reply's landed commands did, as tags: the verb, plus
    "coin-"/"trust-"/"rep-" for a loss and "time+" for half an hour or more
    (the turn's own @time 15m is no answer to anything). With `entity_name`,
    a verb that acts on a person counts only when it names them."""
    from src.services.commands import _ALIASES, parse_command, parse_duration, parse_signed, split_trailing_number

    own = {w.lower() for w in re.findall(r"[A-Za-z][\w'’-]{2,}", entity_name or "")}
    tags: set[str] = set()
    for row in commands or ():
        get = row.get if isinstance(row, dict) else (lambda key, _r=row: getattr(_r, key, None))
        if not get("ok"):
            continue
        line = str(get("line") or "")
        parsed = parse_command(line)
        if parsed is None:
            continue
        verb = _ALIASES.get(parsed.verb, parsed.verb)
        found = {verb}
        value = parsed.main or (parsed.extras[0] if parsed.extras else "")
        if verb == "coin" and (parse_signed(value) or 0) < 0:
            found.add("coin-")
        if verb in ("trust", "rep"):
            delta = split_trailing_number(parsed.main)[1]
            if delta is None and parsed.extras:
                delta = parse_signed(parsed.extras[0])
            if delta is not None and delta < 0:
                found.add(f"{verb}-")
        if verb == "time" and (parse_duration(value) or 0) >= 30:
            found.add("time+")
        if own and not ({w.lower() for w in re.findall(r"[A-Za-z][\w'’-]{2,}", line)} & own):
            found -= _ON_SOMEONE
        tags |= found
    return tags


def _track_open(player_id: str, commands: list[Any], speaker_ids: list[str], location_id: str | None,
                *, fought: bool = False, cost_seen: bool | None = None, player_input: str = "") -> None:
    """After each reply: did it answer the open complication? Its person
    speaking, a fight, or a command that acts on it closes it; walking off
    lets it follow sooner; after its one nudge it is let go.

    Round 8: a person or beast that left the scene (the engine sent a
    calmed wolf home — encounters.send_off) is no longer pressed; a theft
    answers only once its cost reached the player's screen (fleet9-bram T11:
    "answered" while every sentence of it was dropped), and the reply that
    brings it carries the choice, not the verdict."""
    op = _open.get(player_id)
    if op is None:
        return
    op["replies"] += 1
    gone = False
    if op.get("entity_id") or op.get("entity_name"):
        with get_session() as session:
            row = session.get(NPC, op["entity_id"]) if op.get("entity_id") else next(
                (n for n in session.query(NPC).filter(NPC.status == "alive").all()
                 if (n.name or "").strip().lower() == op["entity_name"].strip().lower()), None)
            if row is not None:
                op["entity_id"] = row.id        # the @new-npc line made them real
                gone = row.status != "alive" or row.current_location_id not in (op.get("where"), location_id)
                if op["kind"] == "leaving" and row.status == "alive" and op.get("where") \
                        and row.current_location_id != op["where"]:
                    _back_by_morning(session, row, op["where"])
            elif op.get("entity_id"):
                gone = True
    choice_first = op["kind"] in _CHOICE_FIRST
    if choice_first and op.get("entity_id") and not op.get("quarry"):
        # The thief stays someone to chase or fight for two replies
        from src.services import encounters

        encounters.mark_quarry(player_id, op["entity_id"])
        op["quarry"] = True
    # (a summoner is elsewhere by design; a thief's leaving is the ignored branch)
    if gone and not choice_first and op["kind"] != "summons" and not fought:
        logger.info(f"Complication {op['kind']} closed for {player_id}: who it was about has left the scene")
        _open.pop(player_id, None)
        return
    tags = _applied_tags(commands, op.get("entity_name"))

    # (a tip the player gave in the meantime, a bribe or a payout is no
    # thief's take)
    cost = [line for line, ok in (_row_line(r) for r in commands)
            if ok and _theft_cost(line, player_input, op.get("entity_name"))] if choice_first else []
    if cost and not fought:
        if op["replies"] == 1:
            _warnings.setdefault(player_id, []).append({
                "check": "complication_choice_skipped",
                "detail": (f"You charged the theft ({'; '.join(c[:60] for c in cost)}) in the reply that brought the "
                           "thief: the player never got to grab the wrist, give chase or let it go. Next time that "
                           "reply shows only the hand and the thief in sight — no roll, no cost — and the cost "
                           "lands a reply later, if they let it go."),
            })
        if cost_seen is False:
            op["unseen"] = cost
            logger.info(f"Complication {op['kind']} cost landed unseen for {player_id}: {cost}")
            return
        logger.info(f"Complication {op['kind']} answered for {player_id} (its cost is on screen)")
        _open.pop(player_id, None)
        return
    if choice_first and op["replies"] == 1 and not fought:
        return                              # the reply that brought it put the choice; the answer is next
    acts = (_ACTS.get(op["kind"], frozenset()) | _ALWAYS_ACTS) - {"time"}
    if any(t == "time" for t in _ACTS.get(op["kind"], ())):
        acts = acts | {"time+"}
    if choice_first:
        acts -= {"coin-", "remove"}         # its cost was read above, a gift is none
    spoke = bool(op.get("entity_id")) and op["entity_id"] in speaker_ids
    if fought or spoke or tags & acts:
        logger.info(f"Complication {op['kind']} answered for {player_id}")
        _open.pop(player_id, None)
        return
    if location_id and op.get("where") and location_id != op["where"]:
        op["walked"] = True
    if op.get("nudged") and op["replies"] > op.get("nudged_at", 0):
        logger.info(f"Complication {op['kind']} let go unanswered for {player_id}")
        _open.pop(player_id, None)


def reply_owned(player_id: str, *, world_tick: dict[str, Any] | None = None, campaign_briefing: str = "",
                canvas_notes: str = "") -> bool:
    """Does something else already own this reply — a world event that must
    land, a campaign beat firing, the morning after a rest? Never raises
    (a check it cannot make owns nothing)."""
    try:
        tick = world_tick or {}
        if any(e.get("must_deliver") for e in (tick.get("fired_events") or []) + (tick.get("undelivered_events") or [])):
            return True
        if campaign_briefing:
            from src.services.campaign import get_last_tick

            # This turn's beat is in this turn's briefing; a resolved campaign's
            # last tick lingers in the cache but not in the text
            beat = get_last_tick(player_id).get("fired_beat") or {}
            if beat.get("title") and str(beat["title"]) in campaign_briefing:
                return True
        # The morning a rest just opened is the morning's (its title card is up)
        return "- Rested:" in (canvas_notes or "")
    except Exception as e:
        logger.warning(f"reply ownership check failed: {e}")
        return False


def unanswered_line(player_id: str, *, engine_turn: bool = False, world_tick: dict[str, Any] | None = None,
                    campaign_briefing: str = "", canvas_notes: str = "") -> str | None:
    """Pre-turn: the open complication nobody answered, pressed once —
    [COMPLICATION — UNANSWERED] with its ignored branch and the command it
    needs (fleet6-tamsin T18-T20: the watcher "hasn't moved"). Never raises."""
    try:
        op = _open.get(player_id)
        if not op or engine_turn or op.get("nudged"):
            return None
        if op.get("unseen"):
            # The cost landed but nothing the player read showed it (fleet9-bram
            # T11: 6 coin gone behind three dropped sentences): shown now, once
            _open.pop(player_id, None)
            logger.info(f"Complication {op['kind']} shown after its unseen cost for {player_id}")
            return (
                f"[COMPLICATION — UNSEEN. Your last reply already charged it ({'; '.join(op['unseen'])}), but "
                f"nothing the player read showed it happen: {op['what']}. Show it THIS reply, in a sentence or "
                "two after answering what the player did — the hand at the belt, the thief in sight or just "
                "gone, the purse lighter — in second person. It is ALREADY charged: never write the command "
                "again, never refund it.]"
            )
        if op.get("entity_id") and op["kind"] == "scout":
            # A beast that is not where the player is cannot come over the
            # rail (fleet9-wenna T16: the wolf her prose had sent off)
            with get_session() as session:
                beast = session.get(NPC, op["entity_id"])
                player = session.get(Player, player_id)
                away = (beast is None or beast.status != "alive" or player is None
                        or beast.current_location_id != player.current_location_id)
            if away:
                logger.info(f"Complication {op['kind']} let go for {player_id}: its beast is not with them")
                _open.pop(player_id, None)
                return None
        due = op["replies"] >= 1 + UNANSWERED_REPLIES or (op.get("walked") and op["replies"] >= 2)
        if not due or reply_owned(player_id, world_tick=world_tick, campaign_briefing=campaign_briefing,
                                  canvas_notes=canvas_notes):
            return None
        op["nudged"], op["nudged_at"] = True, op["replies"]
        make = (f" They are not a person in the world yet: write this first, exactly — {op['stub']}"
                if op.get("stub") and not op.get("entity_id") else "")
        logger.info(f"Complication {op['kind']} pressed for {player_id} after {op['replies']} replies")
        return (
            f"[COMPLICATION — UNANSWERED. {op['replies']} replies ago you brought it in: {op['what']}. Nothing "
            f"came of it, and the world does not wait. Act on it NOW, in this reply, with a command — "
            f"{op['ignored']}.{make} Show it land in the prose, after answering what the player did.]"
        )
    except Exception as e:
        logger.warning(f"unanswered complication check failed: {e}")
        return None


def _place_setting(session, here: Location | None) -> str:
    """"town" or "wild" for a place."""
    if here is None:
        return "town"
    if here.type in _INDOORS:
        return "town"
    rest = here.rest if isinstance(here.rest, dict) else {}
    if rest.get("kind") == "camp":
        return "wild"
    about = f"{here.name} {' '.join(str(here.description or '').split()[:30])}"
    if _WILD_WORDS.search(here.name or "") or (here.type not in _TOWN_TYPES and _WILD_WORDS.search(about)):
        return "wild"
    if here.type in _TOWN_TYPES:
        return "town"
    people = session.query(NPC).filter(NPC.current_location_id == here.id, NPC.status == "alive").all()
    from src.services.dialogue import is_beast

    return "town" if any(not is_beast(n) for n in people) else "wild"


def _setting(session, player: Player) -> str:
    """"town" or "wild" for where the player stands."""
    here = session.get(Location, player.current_location_id) if player.current_location_id else None
    return _place_setting(session, here)


def _night_edge(session, player: Player, *, watching: bool = False, climbing: bool = False) -> Location | None:
    """The wild place a beast scout would come in from, when it is night,
    the danger is rising (or the player is `watching` for it — fleet8-bram
    T14, tension low, a night watch at the paddock fence), the story is
    beast-haunted and the player stands at the edge of town (a town place
    with a road out to the wild, or a gate / wall / fence / paddock of its
    own). None otherwise. `climbing`: they go over the wall — from a
    gatehouse's room too, the wall is outside it."""
    from src.models import Connection, DMState, WorldClock

    clock = session.query(WorldClock).first()
    if clock is None or int(clock.hour or 0) not in NIGHT_HOURS:
        return None
    state = session.query(DMState).first()
    if not watching and (state is None or (state.tension or "low") not in _RISING):
        return None
    here = session.get(Location, player.current_location_id) if player.current_location_id else None
    if here is None or (here.type in _INDOORS and not climbing) or not _beast_species(session)[0]:
        return None

    def edge(place: Location) -> bool:
        return bool(_EDGE_WORDS.search(f"{place.name} {place.description or ''}"))

    def wild_source(place: Location | None) -> bool:
        # Where a beast comes FROM: the wild itself, not a yard on the fence
        return (place is not None and place.type not in _INDOORS and not edge(place)
                and _place_setting(session, place) == "wild")

    neighbours: list[Location] = []
    for conn in session.query(Connection).filter(
        (Connection.from_location_id == here.id) | (Connection.to_location_id == here.id)
    ).all():
        other = session.get(Location, conn.to_location_id if conn.from_location_id == here.id
                            else conn.from_location_id)
        if wild_source(other):
            neighbours.append(other)
    neighbours.sort(key=lambda p: p.name or "")
    if _place_setting(session, here) == "town":
        if neighbours:
            return neighbours[0]
        if not edge(here):
            return None
    else:
        # A yard on the paddock fence reads "wild" by its words; it is the
        # edge of town when it sits inside a town place (the seed's Stable
        # Yard under Market Square — fleet6-wenna's north paddock fence)
        parent = session.get(Location, here.parent_id) if here.parent_id else None
        if not edge(here) or _WILD_WORDS.search(here.name or "") or parent is None \
                or _place_setting(session, parent) != "town":
            return None
    if neighbours:
        return neighbours[0]
    anywhere = [p for p in session.query(Location).filter(Location.discovered == True).all()  # noqa: E712
                if p.id != here.id and wild_source(p)]
    return sorted(anywhere, key=lambda p: p.name or "")[0] if anywhere else None


def _scout_line(session, player: Player, wild: Location, day: int, *, keep_open: bool = False,
                vigil: str | None = None, climbing: bool = False) -> str:
    """Make ONE weak beast scout real at the edge of the scene, hold the
    ambush roll off it for a reply (the choice comes first), and say so.
    `keep_open`: another complication is still open — it stays the one
    pressed; the scout bites through the encounter director if ignored.
    `vigil`: the player is keeping watch, and this is what came;
    `climbing`: they go over the wall in the dark, and this is what heard."""
    from src.services import encounters, spawn

    species, faction_id = _beast_species(session)
    here = session.get(Location, player.current_location_id)
    scout = spawn.spawn_beast_scout(session, here, species=species or "wolf", faction_id=faction_id,
                                    toward=wild)
    session.commit()
    encounters.hold(scout.id)
    _scout_day[player.id] = day
    if not keep_open:
        _open[player.id] = {
            "kind": "scout", "entity_id": scout.id, "entity_name": scout.name, "stub": None,
            "what": f"a lone {species or 'beast'} scout ({scout.name}) came in close at the edge of town",
            "ignored": _IGNORED["scout"].format(who=scout.name),
            "replies": 0, "where": player.current_location_id,
        }
    logger.info(f"Night-edge scout for {player.id}: {scout.name} at {here.name if here else '?'}"
                + (f" (they keep a watch: {vigil!r})" if vigil else " (they go over the wall)" if climbing else ""))
    why = ("the player is going over the wall in the dark, and the noise of it carries" if climbing
           else "the player is keeping watch out here in the dark, and this is what the watch brings"
           if vigil else "the danger is rising")
    return (
        f"[COMPLICATION — night at the edge of town, and {why}: a lone {species or 'beast'} "
        f"has come in close from the {wild.name} side — {scout.name}, thin and hungry, a scout testing the "
        "town. The engine made it REAL: it stands at the edge of this scene now. Write it in two or three "
        "sentences after answering what the player did, in your own words. The choice is the player's, "
        f"never yours: fight it (@fight {scout.name}); drive it off, calm it, feed it or back away from it — "
        f"roll_check first: a win and it slinks off: @npc-move {scout.name} | {wild.name} (gone into the "
        f"dark — the engine moves it if you do not), a loss and it comes for them: @fight {scout.name}; or "
        "something stranger — follow it out, raise the alarm. Do not start the fight for them; if they ignore "
        "it, it comes for them.]"
    )


def _pick(player_id: str, setting: str, *, has_goods: bool, has_quest: bool,
          has_news: bool = True) -> tuple[str, str, str]:
    menu = [m for m in (_WILD if setting == "wild" else _TOWN)
            if (m[1] != "goods" or has_goods) and (m[1] != "quest" or has_quest)
            and (m[0] != "summons" or has_news)]
    menu = [m for m in menu if m[0] != _last_kind.get(player_id)] or menu
    start = zlib.crc32(player_id.encode()) + _fired.get(player_id, 0)
    kind, _, what, bite = menu[start % len(menu)]
    return kind, what, bite


def _wording(player_id: str, kind: str, what: str) -> str:
    """The menu's line or one of its other wordings, by how many came before."""
    wordings = (what,) + tuple(w for w in _WORDINGS.get(kind, ()) if w != what)
    return wordings[_fired.get(player_id, 0) % len(wordings)]


# ---------------------------------------------------------------------------
# Who it is: a real person or beast, never the story's villain in disguise
# ---------------------------------------------------------------------------

def _staged_ids(session) -> set[str]:
    try:
        from src.services.world_simulation import staged_npc_ids

        return staged_npc_ids(session)
    except Exception:
        return set()


def _quest_people(session) -> set[str]:
    """Everyone a quest is about: its giver and its steps' targets."""
    from src.models.quests import Quest
    from src.services import objectives as objectives_mod

    ids: set[str] = set()
    for quest in session.query(Quest).all():
        if quest.assigned_by_npc_id:
            ids.add(quest.assigned_by_npc_id)
        ids |= {o["target_id"] for o in objectives_mod.normalize_objectives(quest.objectives)
                if o.get("target_type") == "npc" and o.get("target_id")}
    return ids


def _story_people(session, player: Player) -> list[NPC]:
    """The campaign's villain and successor: never the face of a watcher."""
    from src.services.encounters import _story_bosses

    return [n for n in (session.get(NPC, i) for i in _story_bosses(session, player)) if n is not None]


def _look_ban(session, player: Player) -> str:
    """'never dressed like the Ash-Cloaked Man or the Wolf-Mantled Man' —
    fleet6-tamsin T18 gave the T17 watcher Vex Marrow's wet-ash cloak while
    Vex stood at Wolf's Hollow in the database."""
    from src.services.dialogue import role_label

    labels = [role_label(n) for n in _story_people(session, player)]
    labels = [lab for lab in dict.fromkeys(labels) if lab and lab.lower() != "stranger"]
    if not labels:
        return ""
    return (" Never dress or describe them like " + " or ".join(f"the {lab}" for lab in labels)
            + " — that is someone else's look, and that person is elsewhere.")


def _known_words(session, player: Player) -> set[str]:
    words = {w.lower() for w in re.findall(r"[A-Za-z]+", player.name or "")}
    for npc in session.query(NPC).all():
        words |= {w.lower() for w in re.findall(r"[A-Za-z]+", npc.name or "")}
    return words


def _person_stub(session, player: Player) -> dict[str, Any]:
    """A stranger the DM can make real with one line. The name is the
    world's own (`world_name`): the stub table's four names made the same
    laundress every run (fleet9-bram T19, fleet10-bram T7)."""
    pick = (zlib.crc32(player.id.encode()) + _fired.get(player.id, 0)) % len(_TOWN_STUBS)
    _, trade, looks, manner, voice = _TOWN_STUBS[pick]
    name = world_name(session, f"{player.id}:stub:{_fired.get(player.id, 0)}")
    here = session.get(Location, player.current_location_id) if player.current_location_id else None
    at = f" | at: {here.name}" if here is not None else ""
    return {"id": None, "name": name,
            "stub": f"@new-npc {name} | trade: {trade} | looks: {looks} | manner: {manner} | voice: {voice}{at}"}


# ---------------------------------------------------------------------------
# A name in the world's own style — never a fixed list, never one in use
# ---------------------------------------------------------------------------

# Everyday words the recombination can spell ("Sorrel" + "Marrow" ->
# "Sorrow"): a person named Pillow is no person
_COMMON_WORDS = frozenset("""
    allow arrow pillow quill quick sorrow tallow mallow wallow taper paper sonnet billow fellow hollow
    yellow mellow follow swallow barrow narrow harrow burrow furrow morrow borrow shallow callow bellow
    pepper copper hopper dapper rapper tipper zipper sipper kipper supper upper proper viper river
    better letter setter bitter litter sitter fitter matter latter hatter batter butter cutter gutter
    banner manner tanner dinner winner sinner inner runner cannon canon common lemon melon salon talon
    robin cabin basin raisin resin satin tannin margin virgin muffin coffin
    darrel barrel morrel sorrel quarrel squirrel apparel laurel mussel tassel vessel cancel
    bramble ramble gamble humble tumble rumble jumble mumble fumble nimble thimble
    belle dello hello cello jello mello tallon pardon garden warden harden
    taller caller holler dollar collar pillar killer miller filler
    master sister mister faster vesper whisper temper hamper pamper camper damper
    raven haven maven seven eleven given riven driven duper toper vaper coper brack
""".split())
_TITLES = frozenset({"captain", "sergeant", "lord", "lady", "sir", "dame", "master", "mistress", "mister", "old",
                     "young", "brother", "sister", "father", "mother", "the", "of", "watchman", "doctor"})
_VOWELS = "aeiouy"


def _style_examples(session) -> list[str]:
    """The given names that set the world's style: the world bible's naming
    conventions for people (their examples in brackets, "Earthy English-
    adjacent (Brann, Maera, Sorrel)"), then the people the world already
    has — never a beast's name."""
    from src.models import WorldBible
    from src.services.dialogue import is_beast

    found: list[str] = []
    try:
        bible = session.query(WorldBible).first()
        conventions = getattr(bible, "naming_conventions", None) if bible is not None else None
        if isinstance(conventions, dict):
            people = [str(v) for k, v in conventions.items()
                      if re.search(r"human|people|person|character|given|first|folk|common|name", str(k), re.I)]
            texts = people or [str(v) for v in conventions.values()]
        else:
            texts = [str(conventions)] if conventions else []
        for text in texts:
            for chunk in re.findall(r"\(([^)]*)\)", text) or [text]:
                found += re.findall(r"\b[A-Z][a-z]{2,}\b", chunk)
    except Exception as e:      # no bible: the people are the style
        logger.debug(f"naming conventions unreadable: {e}")
    for npc in session.query(NPC).all():
        try:
            if is_beast(npc):
                continue
        except Exception:
            continue
        found += [w for w in re.findall(r"\b[A-Z][a-z]{2,}\b", npc.name or "") if w.lower() not in _TITLES]
    return list(dict.fromkeys(found))


def _in_use(session) -> set[str]:
    """Every word of every name the world knows: its people (any status),
    the characters and its places."""
    used: set[str] = set()
    for row in session.query(NPC.name).all():
        used |= {w.lower() for w in re.findall(r"[A-Za-z]+", row[0] or "")}
    for row in session.query(Player.name).all():
        used |= {w.lower() for w in re.findall(r"[A-Za-z]+", row[0] or "")}
    for row in session.query(Location.name).all():
        used |= {w.lower() for w in re.findall(r"[A-Za-z]+", row[0] or "")}
    return used


def world_name(session, seed: str) -> str:
    """A given name in this world's style that nobody in it bears: the head
    of one of its names (up to its first vowels) on the tail of another —
    "Tobin" and "Sorrel" give "Torrel" — five to eight letters, no everyday
    word, no three consonants in a row. Deterministic for `seed`."""
    examples = _style_examples(session)
    used = _in_use(session) | {e.lower() for e in examples}

    def split(name: str) -> tuple[str, str]:
        m = re.match(rf"^([^{_VOWELS}]*[{_VOWELS}]+)(.*)$", name.lower())
        return (m.group(1), m.group(2)) if m else (name.lower(), "")

    parts = [split(e) for e in examples if len(e) >= 3]
    heads = sorted({h for h, _ in parts if h})
    # (a long tail is a surname's: "Coldiron" lends no given name its "ldiron")
    tails = sorted({t for _, t in parts if 2 <= len(t) <= 4})
    pool = sorted({h + t for h in heads for t in tails
                   if 5 <= len(h + t) <= 8 and not re.search(rf"[^{_VOWELS}]{{3,}}", h + t)
                   and not re.search(rf"[{_VOWELS}]{{3,}}", h + t) and not re.search(r"(..)\1", h + t)
                   and (h + t) not in used and (h + t) not in _COMMON_WORDS})
    if not pool:
        # A world with no names yet: plain syllables, still never one in use
        pool = sorted({a + b for a in ("ar", "bel", "cor", "dar", "el", "fen", "hal", "mar", "ren", "tor")
                       for b in ("den", "ett", "ira", "lin", "mot", "rick", "sel", "wen")} - used)
    pick = pool[zlib.crc32(seed.encode()) % len(pool)] if pool else f"stranger{zlib.crc32(seed.encode()) % 97}"
    return pick[:1].upper() + pick[1:]


def _beast_species(session) -> tuple[str, str | None]:
    """(species, faction id) of the predators this world already has —
    the scout belongs to the story's own pack."""
    from src.services.dialogue import is_beast

    counts: dict[tuple[str, str | None], int] = {}
    for npc in session.query(NPC).filter(NPC.status == "alive").all():
        if not is_beast(npc) or (npc.danger_tier or "").lower() == "harmless":
            continue
        words = set(re.findall(r"[a-z]+", f"{npc.species or ''} {npc.name or ''}".lower())) & PREDATORS
        if words:
            species = sorted(words)[0]
            species = "wolf" if species == "wolves" else species.rstrip("s") if species == "hounds" else species
            key = (species, npc.faction_id)
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return "", None
    return max(counts, key=lambda k: (counts[k], k[0]))


def _beast_stub(session, player: Player) -> dict[str, Any]:
    species = _beast_species(session)[0] or "wolf"
    known = _known_words(session, player)
    looks = [lk for lk in _BEAST_STUB_LOOKS if lk.lower() not in known] or list(_BEAST_STUB_LOOKS)
    name = f"{looks[_fired.get(player.id, 0) % len(looks)]} {species.title()}"
    return {"id": None, "name": name,
            "stub": (f"@new-npc {name} | trade: {species} | looks: a lone {species}, thin and wary | "
                     f"manner: hungry, bold at the edges | voice: none | tier: weak | fights: skirmisher")}


# ---------------------------------------------------------------------------
# A watcher belongs to the story's other side (fleet10-bram T7-T10)
# ---------------------------------------------------------------------------

# What the town sees of a lookout (trade, looks, manner, voice); the name is
# the world's own (`world_name`)
_COVERS = (
    ("laundress", "red-knuckled, sleeves rolled, a basket of wet linen on one hip", "sharp-eyed, quick",
     "clipped, answers a question with a question"),
    ("porter", "broad, a porter's strap across the chest, sawdust on the sleeves", "slow, patient, never far off",
     "rumbling, few words"),
    ("peddler", "small and wiry, a peddler's tray of pins and laces on a strap", "cheerful, nosy",
     "fast and sing-song"),
    ("water-carrier", "lean, two buckets on a yoke, wet to the shins", "unhurried, always somewhere near",
     "soft, polite, forgettable"),
    ("tinker", "a tinker's pack of pots and wire, a burn-scarred thumb", "chatty about nothing",
     "quick, full of questions about the road"),
)
# Garments: a lookout never wears the villain's own look (the look ban); a
# small token of the side is a tell, a cloak is a costume
_GARMENTS = re.compile(r"\b(?:cloaks?|mantles?|coats?|robes?|hoods?|capes?|tabards?|uniforms?|armou?r|"
                       r"colou?rs|livery|masks?|pelts?)\b", re.IGNORECASE)


def _antagonist_side(session, player: Player) -> dict[str, Any] | None:
    """The campaign's villain faction, and who one of its lookouts reports
    to: the successor, else another living member (never the villain — the
    story stages them), else nobody named. None without one."""
    from src.models import Faction
    from src.services.campaign import get_campaign_row
    from src.services.dialogue import is_beast, role_label

    campaign = get_campaign_row(session, player.id)
    if campaign is None or campaign.status not in ("pending", "active"):
        return None
    villain = session.get(NPC, campaign.villain_npc_id) if campaign.villain_npc_id else None
    faction_id = campaign.villain_faction_id or (villain.faction_id if villain is not None else None)
    faction = session.get(Faction, faction_id) if faction_id else None
    if faction is None:
        return None
    members = [n for n in session.query(NPC).filter(NPC.faction_id == faction.id, NPC.status == "alive").all()
               if not is_beast(n) and n.id != campaign.villain_npc_id]
    members.sort(key=lambda n: (n.id != campaign.successor_npc_id, n.name or ""))
    boss = members[0] if members else None
    # What a lookout must never carry: a sealed truth's words, or a beat's
    # still to come (the cracked bone whistle is the act's own find)
    from src.services.campaign import _campaign_beats

    unrevealed = [t for t in (campaign.fixed_truths or []) if isinstance(t, dict) and not t.get("revealed")]
    keywords = [str(k) for t in unrevealed for k in (t.get("spoiler_keywords") or [])]
    keywords += [str(k) for b in _campaign_beats(session, campaign) if b.status not in ("delivered", "skipped")
                 for k in (b.delivery_keywords or [])]
    sealed = {w for k in keywords for w in re.findall(r"[a-z]{4,}", k.lower())}
    return {"faction": faction, "boss": boss, "boss_label": role_label(boss) if boss is not None else "",
            "sealed": sealed}


def _tell(side: dict[str, Any]) -> str:
    """One thing about a lookout the player can learn: a small token of the
    side's own look (its aesthetic), never a garment of it and never a word
    of a truth the story keeps sealed; else a habit."""
    faction = side["faction"]
    pieces = [p.strip(" .") for p in re.split(r",|;|\band\b", str(faction.aesthetic or "")) if p.strip(" .")]
    pieces = [p for p in pieces if not _GARMENTS.search(p)
              and not (set(re.findall(r"[a-z]{4,}", p.lower())) & side["sealed"])]
    if pieces:
        piece = pieces[-1].lower()
        piece = re.sub(r"ies\b", "y", piece) if piece.endswith("ies") else re.sub(r"(?<!s)s\b", "", piece)
        return f"a {piece} kept out of sight under the collar — it shows when they turn too fast"
    return f"goes still and looks away whenever anyone speaks of {faction.name}"


def _faction_watcher(session, player: Player) -> dict[str, Any] | None:
    """A lookout of the story's other side, made real here by the engine: a
    cover trade the town sees, the world's own kind of name (never one in
    use), the villain faction's, a dossier line saying who they report to
    and one tell the player can learn. fleet10-bram T7-T10 spent three
    replies on "Ilse Marrick", a stock laundress (fleet9-bram T19 met her
    too) who connected to nothing. None without a villain faction."""
    from src.models import NPCTier

    side = _antagonist_side(session, player)
    if side is None or not player.current_location_id:
        return None
    faction, boss = side["faction"], side["boss"]
    seed = f"{player.id}:watcher:{_fired.get(player.id, 0)}"
    trade, looks, manner, voice = _COVERS[zlib.crc32(seed.encode()) % len(_COVERS)]
    name = world_name(session, seed)
    tell = _tell(side)
    to = (f"{boss.name} (to the player: the {side['boss_label']})" if boss is not None
          else f"{faction.name} — never says to whom")
    watcher = NPC(
        name=name, tier=NPCTier.MINOR, profession=trade, faction_id=faction.id,
        current_location_id=player.current_location_id, home_location_id=player.current_location_id,
        position_x=82.0, position_y=58.0,
        description_physical=looks[:1].upper() + looks[1:] + ".",
        description_personality=(f"{manner[:1].upper() + manner[1:]} — the cover. In truth a lookout for "
                                 f"{faction.name}: watches the newcomer and reports to {to}. A tell the player "
                                 f"can learn: {tell}."),
        voice_pattern=voice,
        goals=[f"Keep eyes on the newcomer and carry what they do to {boss.name if boss else faction.name}"],
        secrets=[f"Watches the newcomer for {faction.name}, and reports to "
                 f"{boss.name if boss is not None else 'them'}"],
        current_mood="watchful",
    )
    session.add(watcher)
    session.commit()
    logger.info(f"Watcher for {player.id}: {name} ({trade}) of {faction.name}, reports to "
                f"{boss.name if boss else 'nobody named'}")
    return {"id": watcher.id, "name": name, "stub": None, "made": True, "trade": trade, "tell": tell,
            "faction": faction.name, "to": to}


def _entity(session, player: Player, kind: str) -> dict[str, Any] | None:
    """Who the complication is: {"id", "name", "stub"} — an existing
    ambient, unmet, unstaged person (or beast) already here, else a ready
    @new-npc line. A summons names a met person elsewhere. None for news.
    A watcher is a lookout of the story's other side when it has one
    (`_faction_watcher`)."""
    from src.models import NPCTier
    from src.services import party as party_mod
    from src.services.dialogue import is_beast, player_has_met, relationship_row

    if kind == "bad_news":
        return None
    if kind == "watcher":
        agent = _faction_watcher(session, player)
        if agent is not None:
            return agent
    villains = {n.id for n in _story_people(session, player)}
    party = set(party_mod.member_ids(player))
    alive = session.query(NPC).filter(NPC.status == "alive").all()
    if kind == "summons":
        from src.models.quests import Quest, QuestStatus

        # Someone they have met, with standing, who stays where they are —
        # a quest's giver first; never the story's villain sending for them.
        # Met is spoken with AND named: a name only heard of sends for no one
        givers = {q.assigned_by_npc_id for q in session.query(Quest).filter(Quest.status == QuestStatus.ACTIVE).all()}
        elsewhere = [n for n in alive if n.current_location_id and n.current_location_id != player.current_location_id
                     and n.id not in villains and n.id not in party and not is_beast(n)
                     and relationship_row(session, player.id, n.id) is not None
                     and player_has_met(session, player.id, n.id)]
        elsewhere.sort(key=lambda n: (n.id not in givers, n.name))
        return {"id": elsewhere[0].id, "name": elsewhere[0].name, "stub": None} if elsewhere else None
    # Nobody the story already uses: the campaign's cast, and the people its
    # quests are about (the seed's Sorrel is ambient, unmet — and a talk_to)
    staged = _staged_ids(session) | villains | _quest_people(session)
    beast = kind in ("tracks", "snapped_branch", "hunting_call")
    here = [n for n in alive if n.current_location_id == player.current_location_id
            and n.id not in staged and n.id not in party and is_beast(n) == beast
            and not player_has_met(session, player.id, n.id)
            and (beast or n.tier == NPCTier.AMBIENT)]
    if here and kind in ("watcher",) + ("tracks", "snapped_branch", "hunting_call"):
        pick = sorted(here, key=lambda n: n.name)[_fired.get(player.id, 0) % len(here)]
        return {"id": pick.id, "name": pick.name, "stub": None}
    return _beast_stub(session, player) if beast else _person_stub(session, player)


def _dealt_with(session, player: Player) -> NPC | None:
    """The person the player last dealt with (met, spoken with, alive, not
    the story's villain or their own companion) — whom bad news of their
    doing is about. A quest's giver first on a tie."""
    from src.models import NPCRelationship
    from src.models.quests import Quest, QuestStatus
    from src.services import party as party_mod
    from src.services.dialogue import is_beast, player_has_met

    villains = {n.id for n in _story_people(session, player)}
    party = set(party_mod.member_ids(player))
    givers = {q.assigned_by_npc_id for q in session.query(Quest).filter(Quest.status == QuestStatus.ACTIVE).all()}
    found = []
    for rel in session.query(NPCRelationship).filter(NPCRelationship.player_id == player.id).all():
        npc = session.get(NPC, rel.npc_id)
        if npc is None or npc.status != "alive" or npc.id in villains or npc.id in party or is_beast(npc) \
                or not player_has_met(session, player.id, npc.id):
            continue
        found.append((-(rel.last_interaction_day or 0), npc.id not in givers, npc.name or "", npc))
    return min(found, key=lambda f: f[:3])[3] if found else None


def _schedule_names(session) -> list[str]:
    """What the world and the story will do on their own clocks: bad news
    never runs ahead of these (fleet6-wenna T9)."""
    from src.models import Event

    names = [e.name for e in session.query(Event).filter(
        Event.scheduled_day.isnot(None), Event.occurred_day.is_(None),
    ).all() if e.name]
    try:
        from src.models.campaign import StoryBeat

        names += [b.clock_label or b.title for b in session.query(StoryBeat).filter(
            StoryBeat.status.in_(("armed", "fired", "stalled")),
            StoryBeat.beat_type == "deadline_event",
        ).all() if (b.clock_label or b.title)]
    except Exception:
        pass
    return list(dict.fromkeys(names))[:6]


def _summons_source(session, player: Player, summoner: dict[str, Any] | None) -> str:
    """What the summoner could know without being told: the job they handed
    over in the open."""
    if not summoner or not summoner.get("id"):
        return "what the town saw them do"
    from src.models.quests import Quest, QuestStatus

    job = session.query(Quest).filter(Quest.status == QuestStatus.ACTIVE,
                                      Quest.assigned_by_npc_id == summoner["id"]).first()
    return f"the job they took from {summoner['name']} in the open ('{job.title}')" if job \
        else f"what {summoner['name']} has seen or heard of them in public"


SUMMONS_SOON_HOURS = 6        # a scheduled hour this close is a clock worth sending for


def _summons_news(session, player: Player) -> str | None:
    """What a summons can carry that is new to the player: a fired public
    event nobody has told them yet, a public event coming up within
    SUMMONS_SOON_HOURS (the brief's "Coming up"), or a story clock with its
    deadline. None: no summons (fleet10-tamsin T7-T10)."""
    from src.models import Event, WorldClock

    clock = session.query(WorldClock).first()
    if clock is None:
        return None
    now = ((clock.day or 1) * 24 + (clock.hour or 0)) * 60 + (clock.minute or 0)
    # (the world's schedule only: a "Death of" row is the player's own doing)
    untold = session.query(Event).filter(Event.occurred_day.isnot(None), Event.scheduled_day.isnot(None),
                                         Event.player_visible == True,  # noqa: E712
                                         Event.narrated_to_player == False).all()  # noqa: E712
    if untold:
        e = max(untold, key=lambda e: ((e.occurred_day or 0), (e.occurred_hour or 0)))
        return f"word of {e.name} — {e.description}".rstrip(" —")
    coming = []
    for e in session.query(Event).filter(Event.scheduled_day.isnot(None), Event.occurred_day.is_(None),
                                         Event.player_visible == True).all():  # noqa: E712
        due = ((e.scheduled_day or 1) * 24 + (e.scheduled_hour or 0)) * 60
        if 0 < due - now <= SUMMONS_SOON_HOURS * 60:
            coming.append((due, e))
    if coming:
        due, e = min(coming, key=lambda c: c[0])
        return (f"{e.name}, due at {int(e.scheduled_hour or 0):02d}:00 — they want the player before it, and the "
                "summons says what it will change for them")
    try:
        from src.models.campaign import StoryBeat
        from src.services.campaign import get_campaign_row

        campaign = get_campaign_row(session, player.id)
        if campaign is not None and campaign.status == "active":
            for beat in session.query(StoryBeat).filter(StoryBeat.campaign_id == campaign.id,
                                                        StoryBeat.status.in_(("armed", "fired", "stalled")),
                                                        StoryBeat.beat_type == "deadline_event").all():
                if beat.clock_label and beat.fallback_day is not None:
                    due = (beat.fallback_day * 24 + (beat.fallback_hour or 0)) * 60
                    if due > now:
                        return (f"a clock running out: {beat.clock_label} (by day {beat.fallback_day}, "
                                f"{int(beat.fallback_hour or 0):02d}:00)")
    except Exception as e:      # no clock read, no summons
        logger.debug(f"story clock for a summons unavailable: {e}")
    return None


def _working_with(session, player: Player) -> str | None:
    """Why no quiet- or talk-streak push may cut in here, or None: the
    player is with the person of the step they are working (the first undone
    step of an active quest names someone present), or with the person a
    beat waiting on this place needs (armed or stalled, not a clock). fleet10-
    tamsin T7: a runner pulled her off Sorrel mid-conversation while "The Man
    at the Wall" waited on Sorrel at the Stable Yard, and never came."""
    from src.services import objectives as objectives_mod

    here = player.current_location_id
    if not here:
        return None
    present = {n.id: n for n in session.query(NPC).filter(NPC.current_location_id == here,
                                                          NPC.status == "alive").all()}
    if not present:
        return None
    # A companion walks everywhere with them: "Bring what you heard back to
    # Piper" with Piper in the party would hold every push for good
    from src.services import party as party_mod

    companions = set(party_mod.member_ids(player))
    # ...and someone who merely stands in a hub all day is no conversation:
    # the step's person holds pushes only while they spoke in the last two
    # replies (round-9 review: the herbalist's girl in the square would have
    # held every push there until her step ticked). No record yet (a fresh
    # process): presence alone, as before.
    spoke = _spoke.get(player.id)
    recent = set().union(*spoke) if spoke else None
    for quest in objectives_mod.active_quests_for(session, player):
        step = objectives_mod.first_undone(objectives_mod.normalize_objectives(quest.objectives))
        if step and step.get("target_type") == "npc" and step.get("target_id") in present \
                and step["target_id"] not in companions \
                and (recent is None or step["target_id"] in recent):
            return f"they work '{step.get('text')}' with {present[step['target_id']].name} here"
    try:
        from src.services.campaign import _beat_cast, _campaign_beats, _place_ids, get_campaign_row

        campaign = get_campaign_row(session, player.id)
        if campaign is None or campaign.status != "active":
            return None
        for beat in _campaign_beats(session, campaign):
            if beat.status not in ("armed", "stalled") or beat.beat_type == "deadline_event" \
                    or beat.trigger_type == "time" or here not in _place_ids(session, beat):
                continue
            cast = [n for n in _beat_cast(session, beat, npcs=list(present.values())) if n.id in present]
            if cast:
                return f"'{beat.title}' waits here on {cast[0].name}"
    except Exception as e:      # a check it cannot make holds nothing
        logger.debug(f"beat cast here unavailable: {e}")
    return None


# ---------------------------------------------------------------------------
# A choice with a clock: what ends a talk-only streak
# ---------------------------------------------------------------------------
NIGHTFALL = 20                # the hour a way that bars after dark bars


def _closing_gate(session, player: Player) -> dict[str, Any] | None:
    """A way the player's quests point down that bars at nightfall, within
    CLOSING_HOURS: "the gate bars in 20 minutes — go now or wait for dawn"."""
    from src.models import Connection, WorldClock
    from src.services import objectives as objectives_mod
    from src.services.stakes import _shut_at_night
    from src.services.travel import check_route

    clock = session.query(WorldClock).first()
    if clock is None:
        return None
    hour, minute = int(clock.hour or 0), int(clock.minute or 0)
    left = (NIGHTFALL - hour) * 60 - minute
    if hour in NIGHT_HOURS or not 0 < left <= CLOSING_HOURS * 60:
        return None
    targets: list[str] = []
    for quest in objectives_mod.active_quests_for(session, player):
        for step in objectives_mod.normalize_objectives(quest.objectives):
            if not step.get("done") and step.get("kind") == "go_to" and step.get("target_type") == "location" \
                    and step.get("target_id") and step["target_id"] not in targets:
                targets.append(step["target_id"])
    for target_id in targets:
        target = session.get(Location, target_id)
        if target is None or target.id == player.current_location_id or not _shut_at_night(session, target):
            continue
        road = next((c for c in session.query(Connection).filter(Connection.discovered == True).all()  # noqa: E712
                     if c.to_location_id == target.id or (c.bidirectional and c.from_location_id == target.id)), None)
        near = session.get(Location, road.from_location_id if road.to_location_id == target.id
                           else road.to_location_id) if road is not None else None
        if near is None:
            continue
        route = check_route(session, near.id, target.id, player)
        needs = "; ".join(str(r) for r in route.get("requirements") or []) if route.get("blocked") else ""
        soon = f"about {left} minutes" if left <= 90 else f"about {round(left / 60)} hours"
        return {
            "kind": "closing",
            "what": (f"the way to {target.name} bars at nightfall ({NIGHTFALL}:00) — {soon} from now — and once "
                     f"it bars at {near.name}, the road waits for first light"),
            "bite": ("go now, before it bars" + (f" (and what the way asks — {needs} — they need before then)"
                                                  if needs else "")
                     + ", or let the night take the road: once it bars only a bribe, a hard climb over the wall "
                       "or a bed till dawn is left"),
            "who": "",
        }
    return None


def _leaving(session, player: Player) -> dict[str, Any] | None:
    """Someone here who packs up for the evening — not the story's cast, not a
    quest's people, not a companion: "the hawker is packing up — ask him now
    or lose him tonight". Where they go: their home, else the nearest place
    with a roof. None in daylight or indoors."""
    from src.models import Connection, WorldClock
    from src.services import party as party_mod
    from src.services.dialogue import is_beast, player_has_met

    clock = session.query(WorldClock).first()
    hour = int(clock.hour or 0) if clock is not None else 12
    here = session.get(Location, player.current_location_id) if player.current_location_id else None
    if here is None or here.type in _INDOORS or not (hour >= EVENING_HOUR or hour in NIGHT_HOURS):
        return None
    skip = (_staged_ids(session) | {n.id for n in _story_people(session, player)} | _quest_people(session)
            | set(party_mod.member_ids(player)))
    people = [n for n in session.query(NPC).filter(NPC.current_location_id == here.id, NPC.status == "alive").all()
              if n.id not in skip and not is_beast(n) and (n.danger_tier or "").lower() != "boss"]
    people.sort(key=lambda n: (not player_has_met(session, player.id, n.id), n.name or ""))
    roofs = []
    for conn in session.query(Connection).filter(
            (Connection.from_location_id == here.id) | (Connection.to_location_id == here.id)).all():
        other = session.get(Location, conn.to_location_id if conn.from_location_id == here.id
                            else conn.from_location_id)
        if other is not None and other.type in _INDOORS and other.discovered:
            roofs.append(other)
    roofs.sort(key=lambda p: (not (isinstance(p.rest, dict) and p.rest), p.name or ""))
    for npc in people:
        home = session.get(Location, npc.home_location_id) if npc.home_location_id else None
        dest = home if home is not None and home.id != here.id else (roofs[0] if roofs else None)
        if dest is None:
            continue
        return {
            "kind": "leaving", "entity_id": npc.id, "entity_name": npc.name,
            "what": (f"{npc.name} is packing up for the night ({npc.profession or 'their trade'}) — heading for "
                     f"{dest.name}, and not to be found here again till morning"),
            "bite": "ask what they came to ask now, walk along with them, or lose them till morning",
            "who": f" Who: {npc.name}, already here — as the player knows them (a stranger stays a role).",
            "ignored": _IGNORED["leaving"].format(who=npc.name, home=dest.name),
        }
    return None


def _back_by_morning(session, npc: NPC, place_id: str) -> None:
    """Someone a "leaving" push sent off for the night is back where they
    stood at first light — the line promised "till morning", and nothing else
    walks them back (the seed's Hesk, gone to the tavern at dusk, would keep
    his stall shut for good). The world tick's own night-clearing ledger
    does the walking (world_tick.back_at_first_light)."""
    try:
        from src.models import WorldClock
        from src.services import world_tick

        clock = session.query(WorldClock).first()
        if clock is None or npc.id in world_tick._gone_in:
            return
        day = (clock.day or 1) + (1 if int(clock.hour or 0) >= world_tick.FIRST_LIGHT_HOUR else 0)
        world_tick._gone_in[npc.id] = (place_id, npc.current_location_id, day)
    except Exception as e:      # a courtesy, never a cost to the turn
        logger.debug(f"morning return not booked: {e}")


def _talk_push(player_id: str, session, player: Player, *, gate_only: bool = False) -> str | None:
    """The line that ends a talk-only streak — a way about to bar, or someone
    about to leave — and its bookkeeping. None when the world has neither."""
    push = _closing_gate(session, player) or (None if gate_only else _leaving(session, player))
    if push is None:
        return None
    if push["kind"] == "leaving":
        _open[player_id] = {
            "kind": "leaving", "entity_id": push["entity_id"], "entity_name": push["entity_name"], "stub": None,
            "what": push["what"], "ignored": push["ignored"], "replies": 0, "where": player.current_location_id,
        }
    streak = _talk.get(player_id, 0)
    _quiet[player_id] = 0
    _talk[player_id] = 0
    _fired[player_id] = _fired.get(player_id, 0) + 1
    _last_kind[player_id] = push["kind"]
    logger.info(f"Complication for {player_id}: {push['kind']} (after {streak} talk-only replies)")
    return (
        f"[COMPLICATION — the talk has run on, and the world will not wait for it: {push['what']}. Put it to the "
        f"player in a sentence or two after answering what they said — not a new scene, in your own words, a "
        f"choice with a cost: {push['bite']}.{push['who']} Name what is closing plainly; if they keep talking, "
        "the chance is lost.]"
    )


def directive_line(player_id: str, *, engine_turn: bool = False, world_tick: dict[str, Any] | None = None,
                   campaign_briefing: str = "", canvas_notes: str = "", player_input: str = "") -> str | None:
    """Pre-turn: the one complication line for this reply, or None. Firing
    starts the quiet count over. `player_input`: what the player just did —
    a vigil they keep (`keeping_vigil`) is served or waited out. Never
    raises."""
    try:
        return _directive_line(player_id, engine_turn=engine_turn, world_tick=world_tick,
                               campaign_briefing=campaign_briefing, canvas_notes=canvas_notes,
                               player_input=player_input)
    except Exception as e:
        logger.warning(f"complication check failed: {e}")
        return None


def _directive_line(player_id: str, *, engine_turn: bool, world_tick: dict[str, Any] | None,
                    campaign_briefing: str, canvas_notes: str, player_input: str = "") -> str | None:
    quiet = _quiet.get(player_id, 0)
    vigil = None if engine_turn else keeping_vigil(player_input)
    if not vigil and player_id:
        _vigil_waits.pop(player_id, None)
    # Over the wall in the dark: the night-edge scout comes to the noise
    # (round 8's barred night), whatever the quiet count
    climbing = not engine_turn and climbing_out(player_input)
    # ...and a long run of talk with nothing done forces a choice
    talky = _talk.get(player_id, 0) >= TALK_TURNS
    floor = 0 if climbing else VIGIL_SCOUT_QUIET if vigil else min(SCOUT_QUIET, threshold(player_id))
    if not player_id or engine_turn or (quiet < floor and not talky):
        return None
    # One at a time: the open one is pressed by unanswered_line (a night
    # watch at the edge may still bring the scout it is watching for)
    opened = player_id in _open
    if opened and not (vigil or climbing):
        return None
    from src.services import encounters

    if encounters.in_parley(player_id):
        return None
    # The reply already belongs to something: a beat that must land, a world
    # event happening around them, the morning after a rest
    if reply_owned(player_id, world_tick=world_tick, campaign_briefing=campaign_briefing,
                   canvas_notes=canvas_notes):
        return None
    with get_session() as session:
        player = session.get(Player, player_id)
        if player is None or (player.health_status or "healthy") in ("critical", "dead"):
            return None
        # ...or the scene is the work: the player is with the person of the
        # step they are on, or of a beat waiting here (fleet10-tamsin T7). A
        # watch they keep or a wall they climb is their own choice: it holds
        busy = None if (vigil or climbing) else _working_with(session, player)
        if busy:
            logger.info(f"Complication waits for {player_id}: {busy}")
            return None
        # ...or the coming hour does: a public event due near them is what
        # they are waiting for (fleet8-tamsin T8, twenty minutes before the
        # howl)
        soon = _event_soon(session, player)
        if soon and not climbing:
            # ...unless talk has run on and the way the event bars is the
            # choice itself ("the gate bars in 20 minutes — go now or wait")
            line = (_talk_push(player_id, session, player, gate_only=True)
                    if talky and not vigil and not encounters.enemies_here(session, player, beasts_only=False)
                    else None)
            if line is None:
                logger.info(f"Complication waits for {player_id}: {soon} is due within the hour")
            return line
        # Night at the edge of a beast-haunted town: the danger is a real
        # beast, once a night, and sooner than the quiet count — whatever
        # the tension when the player stands watch there for it
        from src.models import WorldClock

        clock = session.query(WorldClock).first()
        day = clock.day if clock is not None else 1
        if _scout_day.get(player_id) != day:
            # A watch already facing a danger has its answer: it brings no
            # second beast early (fleet7-tamsin T18 "positioning myself where
            # it can see me", between the Captain and a wolf) — the plain
            # night-edge rule still holds
            watching = (bool(vigil) or climbing) and not encounters.enemies_here(session, player, beasts_only=False)
            wild = None
            if watching or not (opened or quiet < min(SCOUT_QUIET, threshold(player_id))):
                wild = _night_edge(session, player, watching=watching, climbing=climbing)
            if wild is not None:
                line = _scout_line(session, player, wild, day, keep_open=opened, vigil=vigil, climbing=climbing)
                _quiet[player_id] = 0
                _fired[player_id] = _fired.get(player_id, 0) + 1
                _last_kind[player_id] = "scout"
                _vigil_waits.pop(player_id, None)
                return line
        # A climb is its own danger (the roll decides it); nothing more comes
        if opened or climbing:
            return None
        setting = _setting(session, player)
        # A town complication never pulls them off a watch they chose: it
        # waits (a few replies at most). The wild's own menu — tracks, a
        # snapped branch, a call answered — is what a watch out there is for
        if vigil and setting == "town" and _vigil_waits.get(player_id, 0) < VIGIL_MAX_WAITS:
            if quiet >= threshold(player_id):
                _vigil_waits[player_id] = _vigil_waits.get(player_id, 0) + 1
                logger.info(f"Complication waits for {player_id}: they keep a vigil ({vigil!r})")
            return None
        # A run of talk ends on a choice with a clock — a way about to bar,
        # someone about to leave — never while a danger stands here
        pushing = talky and not encounters.enemies_here(session, player, beasts_only=False)
        if pushing:
            line = _talk_push(player_id, session, player)
            if line is not None:
                return line
        # (with neither, the talk streak brings the ordinary menu early)
        if quiet < threshold(player_id) and not pushing:
            return None
        _vigil_waits.pop(player_id, None)
        has_goods = _stealable(player)
        has_quest = bool(player.active_quests)
        # A summons carries news or a clock, or it is no summons (fleet10-
        # tamsin T7-T10: a runner walked her back across town to hear the
        # opening's "a day late" again)
        news = _summons_news(session, player)
        kind, what, bite = _pick(player_id, setting, has_goods=has_goods, has_quest=has_quest,
                                 has_news=news is not None)
        if kind == "summons" and news is None:
            logger.info(f"Complication waits for {player_id}: a summons with no news is no summons")
            return None
        what = _wording(player_id, kind, what)
        entity = _entity(session, player, kind)
        who_name = (entity or {}).get("name") or ("the one who sent for them" if kind == "summons" else "them")
        if kind == "summons":
            who = (f" Who sends: {entity['name']} (not here) — and they know only {_summons_source(session, player, entity)}; "
                   "never anything the player found, read or heard in private." if entity else
                   " The summoner knows only what the town saw the player do — never anything found, read or "
                   "heard in private.")
            who += (f" What they send for them about — the only reason for the summons, and new to the player: "
                    f"{news}. Nothing else in it is news: never retell what the player was already told as if "
                    "it had just happened.")
        elif entity and entity.get("made"):
            # A lookout of the story's other side (fleet10-bram T7-T10)
            who = (f" Who: {entity['name']} — the engine made them REAL: they stand at the edge of this scene now, "
                   f"to look at a {entity['trade']} (a stranger: call them by what they look like until they "
                   f"say a name). For you only, never said unless the player earns it: they watch for "
                   f"{entity['faction']} and report to {entity['to']}. One tell the player can learn by looking "
                   f"close or pressing: {entity['tell']}." + _look_ban(session, player))
        elif kind == "bad_news":
            schedule = _schedule_names(session)
            # Its cost follows from the player's own doing, and nobody new
            # carries it (fleet8-wenna T20: a "Shawled Woman" named herself
            # Yura, and the Captain's trust fell over the town's mood)
            about = _dealt_with(session, player)
            who = ((f" It is about {about.name}, whom they have dealt with, and about what the player did with "
                    f"them — a @trust down is with {about.name}, over that, or with nobody." if about else
                    " It is about what the player themselves did here — no @trust with anyone who had no part "
                    "in it.")
                   + " Whoever brings it is someone already in the scene or nobody named (a runner, a voice "
                     "from the crowd) — never a new name in the prose.")
            who += (" Never anything on the world's own schedule or the story's clock ("
                    + "; ".join(schedule) + ") — those come on their own, exactly as written." if schedule else "")
        elif entity and entity.get("stub"):
            who = (f" Who: a stranger — make them real FIRST with this line, exactly: {entity['stub']}"
                   + _look_ban(session, player))
        elif entity:
            who = f" Who: {entity['name']}, already here." + _look_ban(session, player)
        else:
            who = ""
        ignored = _IGNORED.get(kind, "it lands: write the command for what it costs").format(who=who_name)
        if kind == "summons" and not entity:
            # Nobody by name to lose trust with: "@trust the one who sent for
            # them -5" could only fail
            ignored = "they did not come, and refusing has its price: @rep down with the side that sent for them"
        _open[player_id] = {
            "kind": kind, "entity_id": (entity or {}).get("id"), "entity_name": (entity or {}).get("name"),
            "stub": (entity or {}).get("stub"), "what": what, "ignored": ignored,
            "replies": 0, "where": player.current_location_id,
        }
    _quiet[player_id] = 0
    _talk[player_id] = 0
    _fired[player_id] = _fired.get(player_id, 0) + 1
    _last_kind[player_id] = kind
    logger.info(f"Complication for {player_id}: {kind} ({setting})")
    if kind in _CHOICE_FIRST:
        # The choice first, the cost after (fleet9-bram T11)
        return (
            f"[COMPLICATION — the world pushes back THIS reply. It has been quiet too long: {what}. "
            f"Weave it into the scene in two or three sentences after answering what the player did, in your "
            f"own words and in second person — the player must SEE it: the hand, the thief in sight. It must "
            f"cost them something or force a choice now, and this one forces the choice first: {bite}.{who} "
            "Nothing is charged and nothing is rolled THIS reply — no @coin, no @remove, no roll_check: the "
            "player answers first. If they let it go, it does not go away.]"
        )
    return (
        f"[COMPLICATION — the world pushes back THIS reply. It has been quiet too long: {what}. "
        f"Weave it into the scene in two or three sentences after answering what the player did — not a "
        f"new scene, not a fight unless they start one — and in your own words: this is a note, not a line "
        f"to copy. It must cost them something or force a choice now: {bite}. Write the command for "
        f"whatever it costs.{who} If the player lets it go, it does not go away.]"
    )

"""What the player reaches for: a thing held out to them, a room, a page.

Round 8 lost what the player took in their own words, in all three sessions.
The prompt tells the DM never to take things FOR the player, so it held the
item out ("holds it out — not quite letting go yet", "The pass waits in his
hand", "slides a brass key across the bar", "She holds the chart out… Will
you take it?"); the player typed "I take the pass carefully" / "I pocket the
key" / "I take the chart and tuck it into my satchel", and the reply went on
— "pass given, business done" — with no @give (fleet9-wenna T6-T10,
fleet9-bram T5-T6 and T18-T19). The main quest's first step (obtain the Gate
Pass) never ticked, and the gate would have refused them in the morning.

The sibling of `travel_intent`: a code reader over what the player typed, a
binding [THIS REPLY: …] line in the context, and an engine fallback after the
reply when the line went unanswered.

  * `take_directive` / `taken_fallback` — the player takes what a present
    person held out last reply, or the item an open obtain step names while
    its holder stands here: "@give <it> first". A reply that wrote no
    @give / @buy / @interact for it, and did not have the holder pull it
    back, gets it given by the engine (canonical name for an obtain step).
  * `holder_lines` — who carries what an open obtain step asks for, so the
    DM never invents an errand to fetch it (fleet9-wenna T1-T6: "get my pass
    at the gatehouse", three turns of walking, then the Captain handed it
    over himself).
  * `room_directive` — they ask for a room where Location.rest is: the price,
    and @rest charges it (fleet9-wenna T20: three coin set down, a key on
    the bar, no @rest, no @coin, the brief's price was 8).
  * `read_directive` — they ask to read or see a Thing here by name:
    @interact it (fleet9-wenna T20: "you're welcome to look" and the act-2
    guest ledger stayed unread).
  * `look_only` / `note_taken` / `put_back_line` — a search on "just enough
    to see what's inside without disturbing it" shows the contents without
    taking them, and what the last reply's @interact took goes back when the
    player leaves it (fleet9-bram T14-T15: the rope).
  * `must_deliver_sentences` — a MUST-DELIVER event told in text the
    roll-first hold threw away is kept (fleet9-wenna T10: the Evening Howl).
  * `note_speakers` / `new_npc_directive` — a role-only voice heard in a
    second reply gets a binding @new-npc line (fleet9-wenna T3-T6: the Gate
    Watchman, three turns of clues, no row, no memory).

In memory, per player (a restart forgets an issued line; the next reply
reads the player's words afresh).
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_lock = threading.RLock()

# What the player says out loud: "…" spans. Their narration is what they DO
_QUOTED = re.compile(r"[\"“][^\"”\n]*[\"”]")
_SENTENCE = re.compile(r"(?<=[.!?…])\s+|\n+")


def _player_words(player_input: str | None) -> str:
    """The player's own words, or "" for an engine input ([OPENING], …)."""
    text = str(player_input or "").strip()
    return "" if text.startswith("[") else text


def _narration(text: str) -> str:
    return _QUOTED.sub(" ", text or "")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text or "") if s.strip()]


def _stem(word: str) -> str:
    """"keys" -> "key", "pages" -> "page", "pass" stays "pass"."""
    w = word.lower().replace("’", "'").split("'")[0]
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def reset(player_id: str | None = None) -> None:
    """Forget every issued line and remembered take (tests, world switches)."""
    with _lock:
        for store in (_ISSUED, _TAKEN, _VOICES, _VOICE_TOLD):
            if player_id is None:
                store.clear()
            else:
                store.pop(player_id, None)


# ----------------------------------------------------------------------
# What a phrase names
# ----------------------------------------------------------------------

_DETERMINERS = frozenset({"the", "a", "an", "his", "her", "their", "its", "this", "that", "these", "those",
                          "your", "some", "one", "both", "each", "that", "said", "other"})
# Where an object phrase ends: "the pass carefully, tucking it…", "the chart
# and tuck it into my satchel", "the key and finish my ale"
_PHRASE_STOP = frozenset({
    "into", "in", "inside", "from", "with", "and", "but", "to", "onto", "under", "beneath", "beside", "alongside",
    "next", "as", "while", "before", "after", "then", "without", "off", "out", "away", "back", "up", "down", "for",
    "so", "because", "like", "between", "over", "across", "toward", "towards", "at", "on", "against", "again",
    "too", "now", "here", "there", "gently", "slowly", "quickly", "quietly", "carefully", "deliberately", "firmly",
    "without", "until", "till", "where", "which", "that", "who", "whose", "if", "or", "nor", "yet", "still",
    "aside", "round", "around", "two", "three", "four",
    # a clause of its own: "the note she holds out"
    "he", "she", "they", "i", "you", "we", "him", "them", "me", "us", "is", "was", "are", "were",
})
# Never a thing anyone hands over: a breath, a step, a seat, a drink taken,
# a hand held (fleet8-wenna T4 "I take her hand gently"), a road, the job
_NOT_A_THING = frozenset({
    "moment", "breath", "look", "step", "seat", "while", "second", "minute", "chance", "time", "word",
    "glance", "pause", "turn", "measure", "stock", "care", "heart", "point", "pull", "drink", "sip", "swig",
    "gulp", "mouthful", "bite", "leg", "hand", "hands", "arm", "finger", "fingers", "offer", "job", "work", "deal",
    "road", "path", "stair", "stairs", "way", "risk", "information", "name", "advice", "warning", "lead",
    "stool", "chair", "bench", "place", "position", "spot", "lesson", "hint", "rest", "nap", "bow", "knee",
    "notebook", "notebooks", "pencil", "pen", "journal", "satchel", "pack", "bag", "coat", "cloak", "pocket",
    "knife", "dagger", "weapon", "lantern", "torch", "it", "them", "everything", "nothing", "anything",
    "something", "advantage", "opportunity", "initiative", "cover", "shelter", "aim", "breather", "beat",
    "half-step", "half", "deep", "long", "slow", "small", "quick", "pace", "pulse", "temperature",
    # a person taken along or at their word ("I take him at his word"), and
    # the idioms of the road and the watch ("the first watch", "the left fork")
    "him", "me", "us", "you", "myself", "himself", "herself", "watch", "fork", "bait", "hit", "edge", "booth",
})
# Money is @coin, never a thing in the pack; what is served is finished at
# the table (the money check covers the price)
_MONEY_OR_SERVED = frozenset({
    "coin", "coins", "copper", "coppers", "silver", "gold", "money", "purse", "pay", "payment", "fee", "wage",
    "wages", "change", "reward", "tip", "bribe", "price", "crown", "crowns", "penny", "pennies",
    "mug", "tankard", "cup", "glass", "ale", "beer", "wine", "mead", "cider", "bowl", "plate", "stew", "soup",
    "meal", "bottle", "flagon", "jug", "pint", "dram", "shot", "tea", "broth", "porridge", "supper", "dinner",
})
_PRONOUNS = frozenset({"it", "them", "both", "one"})


def _phrase(raw: str) -> tuple[str, list[str], bool]:
    """(the cleaned phrase, its head nouns, own) for an object phrase.

    "the pass carefully" -> ("pass", ["pass"], False); "a brass key across"
    -> ("brass key", ["key"], False); "a creased square of stamped leather"
    -> ("creased square of stamped leather", ["square", "leather"], False);
    "my notebook" -> (…, …, True) — the player's own thing; "it" -> ("it",
    [], False)."""
    raw = re.split(r"[,.;:!?—–()\"“”\n]", raw or "", maxsplit=1)[0]
    words = re.findall(r"[A-Za-z][\w'’-]*", raw)
    own = False
    kept: list[str] = []
    for i, word in enumerate(words):
        low = word.lower()
        if i == 0 and low in ("my", "our"):
            own = True
            continue
        if not kept and (low in _DETERMINERS or low.endswith(("'s", "’s")) and i + 1 < len(words)):
            continue                # "the", "Piper's" chart
        if kept and (low in _PHRASE_STOP or (low.endswith("ly") and len(low) > 4)):
            break
        if not kept and low in _PRONOUNS:
            return low, [], own
        kept.append(word)
        if len(kept) >= 6:
            break
    if not kept:
        return "", [], own
    low = [w.lower() for w in kept]
    if "of" in low[1:]:
        at = low.index("of", 1)
        nouns = [_stem(low[at - 1])] + ([_stem(low[-1])] if at + 1 < len(low) else [])
    else:
        nouns = [_stem(low[-1])]
    return " ".join(kept), [n for n in nouns if len(n) >= 3], own


def _names_noun(nouns: Iterable[str], text: str) -> bool:
    """Is one of these nouns said in `text` (any number, any case)?"""
    said = {_stem(w) for w in re.findall(r"[A-Za-z][\w'’-]*", text or "")}
    return any(n in said for n in nouns)


def _title(phrase: str) -> str:
    """"brass key" -> "Brass Key"; "creased square of stamped leather" ->
    "Creased Square of Stamped Leather"."""
    small = {"of", "on", "to", "for", "the", "a", "an", "in"}
    words = phrase.split()
    return " ".join(w if (i and w.lower() in small) else (w[:1].upper() + w[1:]) for i, w in enumerate(words))


# The keys and passes of the world, and what is paper
_KEY_NOUNS = frozenset({"key", "pass", "token", "seal", "permit", "badge", "writ", "warrant", "sigil", "signet",
                        "chit", "tally", "ticket"})


def _kind_for(name: str, nouns: Iterable[str]) -> str:
    """The @give kind a fallback writes: key, clue (paper), or misc."""
    from src.services.commands import _is_document

    if set(nouns) & _KEY_NOUNS or _stem(name.split()[-1] if name.split() else "") in _KEY_NOUNS:
        return "key"
    if _is_document(name) or any(_is_document(n) for n in nouns):
        return "clue"
    return "misc"


def _gear(name: str) -> bool:
    """A blade, a coat, a charm: gear needs a tier the engine will not guess."""
    from src.services.commands import _gear_slot_from_name

    return _gear_slot_from_name(name) is not None


# ----------------------------------------------------------------------
# The context's share
# ----------------------------------------------------------------------

def context_lines(player_id: str | None, player_input: str | None, last_reply: str | None,
                  present: dict[str, str] | None = None, strangers: dict[str, str] | None = None, *,
                  engine_turn: bool = False, travelling: bool = False) -> dict[str, list[str]]:
    """What this module adds to a reply's context: {"facts": [blocks for
    beside the brief], "binding": [one-line orders for the end]}. Run before
    the context's player block — a put-back changes the pack. An engine turn
    ([OPENING], [COMBAT OUTCOME], an ambush) gets nothing, and spends the
    remembered takes and the issued take line. `travelling`: the player sets
    out elsewhere this reply — a page here is no longer within reach.
    Never raises."""
    out: dict[str, list[str]] = {"facts": [], "binding": []}
    try:
        back = put_back_line(player_id, None if engine_turn else player_input)
        if engine_turn:
            with _lock:
                _ISSUED.pop(player_id or "", None)
            return out
        holders = holder_lines(player_id, present)
        if holders:
            out["facts"].append(holders)
        lines = [back,
                 take_directive(player_id, player_input, last_reply, present, strangers),
                 room_directive(player_id, player_input),
                 None if travelling else read_directive(player_id, player_input),
                 new_npc_directive(player_id)]
        out["binding"] = [line for line in lines if line]
    except Exception as e:      # the lines are nudges, never a cost to the turn
        logger.warning(f"reach lines unavailable: {e}")
    return out


# ----------------------------------------------------------------------
# 1. The thing held out, taken
# ----------------------------------------------------------------------

# The player's own hand closing on something: "I take the pass carefully",
# "I nod and take the pass, tucking it into my coat", "I pocket the key",
# "I take the chart and tuck it into my satchel", "I close my fingers around
# the vial". Narration only — "I'll take it" said aloud is a yes to a job
_TAKE = re.compile(
    r"(?:^|(?<=[\s,;:—–(]))(?:I|and|then)\s+"
    r"(?:(?:[a-z]+ly|just|simply|finally|reach\s+out\s+and|lean\s+in\s+and|step\s+forward\s+and)\s+){0,2}"
    r"(?P<verb>take|accept|pocket|tuck|stow|pick\s+up|grab|snatch|palm"
    r"|close\s+my\s+(?:hand|fingers|fist)\s+(?:around|over|on))"
    r"\s+(?P<obj>[^.,;:!?—–()\"“”\n]{1,80})",
    re.IGNORECASE,
)
# A wish or a refusal is no taking: "I don't take it", "I won't take your coin"
_NOT_TAKING = re.compile(r"\b(?:not|never|don['’]t|won['’]t|wouldn['’]t|can['’]t|refuse\s+to|instead\s+of)\s*$",
                         re.IGNORECASE)

# Last reply: someone holds a thing out. The object of each pattern is a
# definite or indefinite phrase, or a pronoun whose thing the reply named
_HELD = (r"(?P<obj>it|them|(?:the|a|an|his|her|their|its|this|that|one|some|[A-Z][\w-]*['’]s)\s+"
         r"[\w'’-]+(?:,?\s+[\w'’-]+){0,4}?)")
# (pattern, runs_on): when the object is not closed off by the pattern, the
# phrase runs on to its own end — "holds out a folded paper from inside his
# coat" is the folded paper, "draws out a creased square of stamped leather,
# holding it" the square of leather
_HOLDS_OUT = [(re.compile(p, re.IGNORECASE), runs_on) for p, runs_on in (
    # "holds it out", "holding the chart out a little further"
    (rf"\b(?:holds?|holding|held)\s+{_HELD}\s+out\b", False),
    # "holds out a folded paper"
    (rf"\b(?:holds?|holding|held)\s+out\s+{_HELD}", True),
    # "slides a brass key across the bar", "sets the letter on the table"
    (rf"\b(?:slides?|sliding|slid|push(?:es|ing|ed)|nudg(?:es|ing|ed)|sets?|setting|lays?|laying|laid"
     rf"|plac(?:es|ing|ed)|puts?|putting|toss(?:es|ing|ed)|offer(?:s|ing|ed)?|extend(?:s|ing|ed)?"
     rf"|proffer(?:s|ing|ed)?)\s+{_HELD}\s+(?:across|over\s+to|toward|towards|out\s+to|in\s+front\s+of\s+you"
     rf"|(?:on|onto)\s+the\s+(?:bar|counter|table|board|desk|plank))\b", False),
    # "offers you a vial"
    (rf"\b(?:offer(?:s|ing|ed)?|proffer(?:s|ing|ed)?|extend(?:s|ing|ed)?)\s+you\s+{_HELD}", True),
    # Put in their hands with no @give: "She presses the torn page into your
    # hands" (fleet4-wenna T2), "thrusts a scrap of bark-paper into your
    # hands" (fleet7-tamsin T3) — then "I take the page"
    (rf"\b(?:press(?:es|ed|ing)?|thrust(?:s|ing)?|push(?:es|ed|ing)|shov(?:es|ed|ing)|puts?|putting"
     rf"|plac(?:es|ed|ing)|slip(?:s|ped|ping)?|tuck(?:s|ed|ing)?)\s+{_HELD}\s+(?:into|in)\s+your\s+"
     rf"(?:[\w'’-]+\s+)?(?:hand|hands|palm|fingers|grip)\b", False),
    (rf"\b(?:thrust(?:s|ing)?|shov(?:es|ed|ing)|push(?:es|ed|ing))\s+{_HELD}\s+(?:at|toward|towards)\s+you\b",
     False),
    # "The pass waits in his hand, close enough to take"
    (rf"\b{_HELD}\s+(?:waits|waiting|sits|rests|lies)\s+(?:in|on)\s+(?:his|her|their)\s+"
     rf"(?:open\s+|outstretched\s+|flat\s+)?(?:hand|palm|fingers)\b", False),
    # "pass still in his hand, waiting for you to close your fingers on it"
    (r"\b(?P<obj>(?:(?:the|a|an|his|her|their)\s+)?[\w'’-]+)\s+still\s+in\s+(?:his|her|their)\s+"
     r"(?:hand|palm|fingers|grip)\b", False),
    # "draws out a creased square of stamped leather, holding it a moment"
    (rf"\b(?:draws?|drew|pull(?:s|ed)?|produc(?:es|ed)|fish(?:es|ed))\s+(?:out\s+)?{_HELD}"
     rf"[^.!?\n]{{0,80}}?\b(?:holding|holds|held)\s+(?:it|them)\b", True),
)]
# ...or says so: "Will you take it?", "Here's your pass", "It's yours"
_OFFERED_ALOUD = re.compile(
    # never "take it or leave it" (a price), "take the job", "keep it quiet"
    r"\b(?:take\s+(?:(?:it|this|them)(?!\s+or\s+leave)|the\s+(?P<a>[\w'’-]+)|my\s+(?P<b>[\w'’-]+))"
    r"|here['’]?s\s+(?:your|the|my)\s+(?P<c>[\w'’-]+)|that['’]s\s+your\s+(?P<d>[\w'’-]+)"
    r"|(?:it|this|that)['’]s\s+yours|keep\s+it(?=\s*[.!,;—–]|\s+safe\b|\s*$))",
    re.IGNORECASE,
)
# The holder kept it after all: "pulls it back", "tucks it back into his
# coat", "closes her fingers over it", "won't let go"
# The holder's own hand, never "you tuck it away" (the player, taking it) —
# so the verbs are the third person's. `obj`: what was kept, when named.
# Still held out is NOT kept: the player took what was offered ("pass still
# in his hand, waiting for you to close your fingers on it", fleet9-wenna T7,
# right after "I take the pass carefully") — the reply only failed to say so
_KEPT_OBJ = r"(?P<obj>it|them|the\s+[\w'’-]+(?:\s+[\w'’-]+)?)"
_KEPT_BACK = [re.compile(p, re.IGNORECASE) for p in (
    rf"\b(?:pulls|pulled|pulling|draws|drew|drawing|snatch(?:es|ed|ing)|yanks|yanked|whips|whipped"
    rf"|tugs|tugged|takes|took|taking)\s+{_KEPT_OBJ}\s+back\b",
    r"\b(?:closes|closed|closing|curls|curled|curling|tightens|tightened)\s+(?:his|her|their)\s+"
    r"(?:hand|fingers|fist|grip)\s+(?:over|around|on)\s+(?P<obj>it|them)\b",
    r"\b(?:grip|fingers|hand|fist)\s+(?:tightens|tighten|tightened|closes|close|closed)\s+(?:on|over|around)\s+"
    r"(?P<obj>it|them)\b",
    rf"\b(?:tucks|tucked|slips|slipped|puts|stows|stowed)\s+{_KEPT_OBJ}\s+"
    r"(?:back\s+)?(?:away|into\s+(?:his|her|their)|inside\s+(?:his|her|their))\b",
    rf"\b(?:keeps|kept|keeping)\s+(?:(?:a\s+)?(?:firm\s+)?hold\s+of\s+)?{_KEPT_OBJ}\s+(?:back|close|for\s+now|a\s+moment\s+longer)\b",
    r"\b(?:holds|held|holding)\s+on\s+to\s+(?P<obj>it|them)\b",
    # never "doesn't let go of your gaze / your sleeve" (fleet2-tamsin T7, fleet4-wenna T2)
    r"\b(?:won['’]t|will\s+not|doesn['’]t|does\s+not|refuses?\s+to)\s+(?:let\s+(?:it\s+|them\s+)?go"
    r"(?!\s+of\s+(?:your|my|his|her|their|the)\s+(?:gaze|eyes|look|sleeve|arm|hand|wrist|shoulder|collar))"
    r"|hand\s+(?:it|them)\s+over|give\s+(?:it|them)\s+up|part\s+with)\b",
    r"\bout\s+of\s+(?:your\s+)?reach\b",
    rf"\bholds?\s+{_KEPT_OBJ}\s+back\b",
    r"\bslaps?\s+your\s+hand\s+away\b",
    r"\b(?:catch(?:es)?|caught|grabs?|grabbed|clamps?|clamped)\s+(?:on\s+)?your\s+(?:wrist|hand|arm|fingers)\b",
)]

_KEPT_NOT = re.compile(r"\b(?:not|never|without|instead\s+of)\s+(?:[\w'’-]+\s+)?$", re.IGNORECASE)


def _kept_back(prose: str | None, nouns: Iterable[str] = ()) -> bool:
    """Did the reply's narration have the holder keep it? "not snatching it
    back" (fleet8-tamsin T9) is the opposite. With the taken thing's
    `nouns`, a kept thing named otherwise is another thing: fleet9-wenna T8
    ("pass given, business done … She holds the chart out") kept the chart,
    not the pass — and "it" is the thing its sentence named last, when that
    is another thing ("Piper takes the coin and tucks it away")."""
    text = _narration(prose or "")
    nouns = set(nouns)
    for pattern in _KEPT_BACK:
        for m in pattern.finditer(text):
            if _KEPT_NOT.search(text[max(0, m.start() - 25):m.start()]):
                continue
            obj = (m.groupdict().get("obj") or "").strip()
            if nouns and obj:
                if obj.lower() in ("it", "them"):
                    start = max(text.rfind(c, 0, m.start()) for c in ".!?\n") + 1
                    named = [n for a in _ANTECEDENT.finditer(text[start:m.start()])
                             for n in [_phrase(a.group(0))[1]]
                             if n and not any(w in _NOT_A_THING for w in n)]
                    if named and not set(named[-1]) & nouns:
                        continue
                elif not set(_phrase(obj)[1]) & nouns:
                    continue
            return True
    return False


# player id -> the take line this reply was given (`taken_fallback` reads it)
_ISSUED: dict[str, dict[str, Any]] = {}
TAKE_MARK = "[THIS REPLY: the player takes what was held out to them"


# "take note", "take heart", "take cover": no thing at all ("take the note"
# she held out is one)
_IDIOMS = frozenset({"note", "notice", "stock", "care", "heart", "aim", "cover", "hold", "charge", "part", "place",
                     "advantage", "offense", "offence", "stand", "shelter", "sides", "turns", "pains", "comfort"})
# The thing an "it" stands for, earlier in its own sentence: "I fold the pass
# carefully and tuck it into my satchel" tucks the pass (fleet5-wenna T11)
_ANTECEDENT = re.compile(r"\b(?:the|a|an|his|her|their|this|that|my|your)\s+([\w'’-]+(?:\s+[\w'’-]+)?)",
                         re.IGNORECASE)


# "I take it slow", "I take it you're the Captain", "take it all in": no hand
# closes on anything
_TAKE_IT_IDIOM = re.compile(
    r"(?:it|them)\s+(?:slow|slowly|easy|in\b|all\s+in|seriously|personally|well|badly|hard|to\s+mean|as\b"
    r"|that\b|from\s+here|you\b|he\b|she\b|they\b|we\b|this\b)",
    re.IGNORECASE,
)


def _takes_in(text: str) -> Iterable[re.Match]:
    """Each `_TAKE` in `text`, overlapping: a take's object runs to the
    clause's end, so "I take a deep breath and pocket the note" hid the
    pocketing inside the breath's object — the next search starts after
    each verb, not after its object."""
    pos = 0
    while True:
        m = _TAKE.search(text, pos)
        if m is None:
            return
        yield m
        pos = m.end("verb")


def player_takes(player_input: str | None) -> list[dict[str, Any]]:
    """Each thing the player's narration takes now: {"said", "phrase",
    "nouns", "pronoun"} — the player's own things, a breath, a seat, a drink
    and money left out. An "it" with a thing before it in its sentence is
    that thing; a bare "it" counts only when nothing else is taken (the
    pass taken and "tucking it into my coat" is one take)."""
    text = _narration(_player_words(player_input))
    out: list[dict[str, Any]] = []
    for m in _takes_in(text):
        if _NOT_TAKING.search(text[max(0, m.start() - 20):m.start()]) \
                or (m.group("obj").split() or [""])[0].lower() in _IDIOMS or _TAKE_IT_IDIOM.match(m.group("obj")):
            continue
        phrase, nouns, own = _phrase(m.group("obj"))
        if phrase in _PRONOUNS and not own:
            start = max(text.rfind(c, 0, m.start()) for c in ".!?\n")
            before = [a for a in _ANTECEDENT.finditer(text[start + 1:m.start()])]
            if before:
                phrase, nouns, own = _phrase(before[-1].group(0))
            else:
                out.append({"said": m.group(0).strip(), "phrase": phrase, "nouns": [], "pronoun": True})
                continue
        if own or not phrase:
            continue
        if not nouns or any(n in _NOT_A_THING or n in _MONEY_OR_SERVED for n in nouns):
            continue
        out.append({"said": m.group(0).strip(), "phrase": phrase, "nouns": nouns, "pronoun": False})
    if any(not t["pronoun"] for t in out):
        out = [t for t in out if not t["pronoun"]]
    return out


def _reply_parts(reply: str) -> tuple[str, list[tuple[str, str]]]:
    """(prose, [(speaker label, words)]) of a raw reply; @command lines out."""
    from src.web.dialogue_markup import parse_dialogue_line

    prose: list[str] = []
    speech: list[tuple[str, str]] = []
    for line in (reply or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.lstrip("-*` ").startswith("@"):
            continue
        parsed = parse_dialogue_line(stripped)
        if parsed and parsed.get("quoted"):
            speech.append((str(parsed.get("speaker") or ""), str(parsed.get("text") or "")))
            if parsed.get("remainder"):
                prose.append(str(parsed["remainder"]))
            continue
        prose.append(stripped)
    return "\n".join(prose), speech


def held_out(reply: str | None) -> list[dict[str, Any]]:
    """What the last reply had someone hold out, slide across or offer:
    [{"phrase", "nouns", "speaker"}] in reply order ("it" when the pattern
    named no thing). `speaker`: the last speaker label before it, else ""."""
    from src.web.dialogue_markup import parse_dialogue_line

    out: list[dict[str, Any]] = []
    last_speaker = ""
    for line in (reply or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.lstrip("-*` ").startswith("@"):
            continue
        parsed = parse_dialogue_line(stripped)
        if parsed and parsed.get("quoted"):
            last_speaker = str(parsed.get("speaker") or "")
            words = str(parsed.get("text") or "")
            for m in _OFFERED_ALOUD.finditer(words):
                noun = next((g for g in m.groups() if g), "")
                phrase, nouns, _ = _phrase(noun) if noun else ("it", [], False)
                if noun and (not nouns or any(n in _NOT_A_THING for n in nouns)):
                    continue
                out.append({"phrase": phrase or "it", "nouns": nouns, "speaker": last_speaker, "aloud": True})
            stripped = str(parsed.get("remainder") or "")
            if not stripped:
                continue
        found: list[tuple[int, dict[str, Any]]] = []
        for pattern, runs_on in _HOLDS_OUT:
            for m in pattern.finditer(stripped):
                obj = m.group("obj")
                if runs_on and obj.lower() not in _PRONOUNS:
                    obj = stripped[m.start("obj"):m.start("obj") + 90]
                phrase, nouns, _ = _phrase(obj)
                # "holds her hand out" offers nothing to keep, nor does a drink
                if not phrase or any(n in _MONEY_OR_SERVED or n in _NOT_A_THING for n in nouns):
                    continue
                # Put in their hands, not held out: a @give in that same reply
                # was it, by another name ("presses a small brass token into
                # your palm" + `@give Gate Pass`, fleet6-tamsin T2)
                handed = bool(re.search(r"\b(?:into|in)\s+your\b|\b(?:at|toward|towards)\s+you\b", m.group(0),
                                        re.IGNORECASE))
                found.append((m.start(), {"phrase": phrase, "nouns": nouns, "speaker": last_speaker,
                                          "aloud": False, "handed": handed}))
        out.extend(entry for _, entry in sorted(found, key=lambda pair: pair[0]))   # in the order written
    return out


def _present_id(label: str, present: dict[str, str], strangers: dict[str, str]) -> str | None:
    """The present person a speaker label is, by name or role label."""
    low = (label or "").strip().lower()
    if not low:
        return None
    for npc_id, name in present.items():
        n = str(name or "").lower()
        if low == n or low in n.split() or (len(low) >= 4 and low in n) or (len(n) >= 4 and n in low):
            return npc_id
    for npc_id, role in strangers.items():
        if low == str(role or "").lower():
            return npc_id
    return None


def _open_obtain_steps(session, player) -> list[dict[str, Any]]:
    """[{"item", "quest", "holder"}] for each undone obtain step of an active
    quest; `holder` (npc, entry) the living person carrying it as a key or
    clue (inventory_notable, quest_item — a shelf's ware is @buy's, as
    commands._from_present_holder has it), or None."""
    from src.models import NPC
    from src.services import objectives as objectives_mod
    from src.services.lead import same_item

    rows: list[dict[str, Any]] = []
    people = session.query(NPC).filter(NPC.status == "alive").all()
    for quest in objectives_mod.active_quests_for(session, player):
        for obj in objectives_mod.normalize_objectives(quest.objectives):
            if obj.get("done") or obj.get("kind") != "obtain" or not obj.get("item_name"):
                continue
            item = str(obj["item_name"])
            holder = None
            for npc in people:
                for entry in npc.inventory_notable or []:
                    if not isinstance(entry, dict) or entry.get("type") != "quest_item":
                        continue
                    held = str(entry.get("name") or "")
                    if held and same_item(item, held):
                        holder = (npc, entry)
                        break
                if holder:
                    break
            rows.append({"item": item, "quest": quest.title, "holder": holder})
    return rows


def _pack_has(player, nouns: Iterable[str], name: str = "") -> bool:
    from src.services.lead import same_item

    nouns = set(nouns)
    for entry in getattr(player, "inventory", None) or []:
        held = str((entry.get("name") if isinstance(entry, dict) else entry) or "")
        if not held:
            continue
        if name and (same_item(name, held) or same_item(held, name)):
            return True
        words = [_stem(w) for w in re.findall(r"[A-Za-z][\w'’-]*", held)]
        if words and words[-1] in nouns:
            return True
    return False


def _thing_here(location, nouns: Iterable[str]) -> bool:
    """A Thing lying here that the nouns name (its label or what it holds):
    that is @interact's, not a hand-over."""
    from src.services.scene_objects import normalize_objects

    nouns = set(nouns)
    if location is None or not nouns:
        return False
    for obj in normalize_objects(location.objects):
        if obj["kind"] in ("rest", "body") or obj["consumed"]:
            continue
        item = (obj["payload"].get("item") or {}).get("name") or ""
        for name in (obj["label"], item):
            words = {_stem(w) for w in re.findall(r"[A-Za-z][\w'’-]*", str(name))}
            if words & nouns:
                return True
    return False


def take_directive(player_id: str | None, player_input: str | None, last_reply: str | None,
                   present: dict[str, str] | None = None, strangers: dict[str, str] | None = None) -> str | None:
    """The binding line for a reply whose player takes what was held out to
    them, or None. Never raises."""
    with _lock:
        _ISSUED.pop(player_id or "", None)
    try:
        return _take_directive(player_id, player_input, last_reply or "", present or {}, strangers or {})
    except Exception as e:      # the line is a nudge, never a cost to the turn
        logger.warning(f"take intent unavailable: {e}")
        return None


def _take_directive(player_id: str | None, player_input: str | None, last_reply: str,
                    present: dict[str, str], strangers: dict[str, str]) -> str | None:
    from src.models import Location, Player, get_session

    takes = player_takes(player_input)
    if not player_id or not takes:
        return None
    offered = held_out(last_reply)
    prose, speech = _reply_parts(last_reply)
    said_last = prose + "\n" + "\n".join(w for _, w in speech)
    # The last reply already handed something over (a @give, a payout): its
    # "That's yours" was answered, and "I close my fingers around the fang"
    # is the reward's charm in other words (fleet7-wenna T18-T19)
    gave_last = bool(_GAVE.search(last_reply))
    with get_session() as session:
        player = session.get(Player, player_id)
        if player is None:
            return None
        here = session.get(Location, player.current_location_id) if player.current_location_id else None
        steps = _open_obtain_steps(session, player)
        found = None
        for take in takes:
            found = _resolve_take(take, offered, said_last, steps, player, here, present, strangers,
                                  gave_last=gave_last)
            if found:
                break
    if not found:
        return None
    name, holder = found["name"], found["holder"]
    kind = _kind_for(name or found["noun"], found["nouns"])
    said = " ".join(str(found["said"]).split())[:80]
    what = name if name else f"the {found['noun']}"
    from_whom = f", from {holder}" if holder else ""
    give = (f"@give {name} | kind: {kind}" if name
            else f"@give <a short name for the {found['noun']}, as the story showed it> | kind: {kind}")
    who = holder or "whoever held it"
    line = (
        f"{TAKE_MARK} — {what}{from_whom} (they said \"{said}\"). Write {give} first (after any roll this "
        f"reply needs) — or, if {who} pulls it back, show that in the story. Never narrate it changing hands "
        "without the @give.]"
    )
    with _lock:
        _ISSUED[player_id] = {"line": line, "name": name, "kind": kind, "nouns": list(found["nouns"]),
                              "noun": found["noun"], "holder": holder, "said": said,
                              "gear": bool(name) and _gear(name)}
    logger.info(f"Take intent: {what}{from_whom} ({said!r})")
    return line


_GAVE = re.compile(r"^[\s>*`-]*@\s*(?:give|buy|quest-done|interact)\b", re.IGNORECASE | re.MULTILINE)


def _resolve_take(take: dict[str, Any], offered: list[dict[str, Any]], said_last: str, steps: list[dict[str, Any]],
                  player, here, present: dict[str, str], strangers: dict[str, str], *,
                  gave_last: bool = False) -> dict[str, Any] | None:
    """{"name" ("" = the DM names it), "noun", "nouns", "holder", "said"} for
    one take the world can answer, else None. `gave_last`: the last reply
    wrote a hand-over command — only a thing it held out BY NAME, or a quest
    step's item, is still owed."""
    def head(item: str) -> list[str]:
        """An item's head noun, in a list: "Gate Pass" -> ["pass"]."""
        _, nouns, _ = _phrase(item)
        return nouns[:1]

    def step_for(nouns: Iterable[str]) -> dict[str, Any] | None:
        nouns = set(nouns)
        for step in steps:
            if set(head(step["item"])) & nouns:
                return step
        return None

    def holder_of(step: dict[str, Any] | None) -> tuple[str | None, bool]:
        if step is None or step["holder"] is None:
            return None, False
        npc = step["holder"][0]
        return npc.name, npc.id in present

    named = [o for o in offered if o["nouns"]]
    if take["pronoun"]:
        # "I take it": the thing held out last; an obtain item the reply named
        # whose holder is here; else the line asks the DM to name it
        if not offered:
            return None
        for o in reversed(named):
            step = step_for(o["nouns"])
            if step and not _pack_has(player, o["nouns"], step["item"]):
                holder, here_now = holder_of(step)
                return {"name": step["item"], "noun": o["nouns"][-1], "nouns": o["nouns"],
                        "holder": holder or o["speaker"] or None, "said": take["said"]}
        for step in steps:
            holder, here_now = holder_of(step)
            nouns = head(step["item"])
            if nouns and here_now and _names_noun(nouns, said_last) and not _pack_has(player, nouns, step["item"]):
                return {"name": step["item"], "noun": nouns[0], "nouns": nouns, "holder": holder, "said": take["said"]}
        if named and not gave_last:
            o = named[-1]
            if _pack_has(player, o["nouns"]) or _thing_here(here, o["nouns"]):
                return None
            return {"name": _title(o["phrase"]), "noun": o["nouns"][-1], "nouns": o["nouns"],
                    "holder": o["speaker"] or None, "said": take["said"]}
        return None
    nouns = take["nouns"]
    step = step_for(nouns)
    # A person, never a thing: "I take Sorrel with me to the fence" named
    # someone the last reply also named, and the engine would have put a
    # "Sorrel" in the pack. Their name's words, or their role label's
    people = {_stem(w) for label in [*present.values(), *strangers.values()]
              for w in re.findall(r"[A-Za-z][\w'’-]*", str(label or ""))}
    if step is None and set(nouns) & people:
        return None
    if _pack_has(player, nouns, step["item"] if step else ""):
        return None
    hit = next((o for o in reversed(named) if set(o["nouns"]) & set(nouns)
                and not (gave_last and o.get("handed"))), None)
    if step is not None:
        holder, here_now = holder_of(step)
        # Its holder stands here and the last reply held something out or
        # spoke of it (fleet9-bram T5: "a creased square of stamped leather,
        # holding it… Pass'll be here when you're back"); or nobody carries
        # it and the reply held it out: the step's own item, by its own name
        mentioned = bool(offered) or _names_noun(nouns, said_last)
        nobody_has_it = step["holder"] is None
        if (here_now and mentioned) or (nobody_has_it and (hit or (offered and _names_noun(nouns, said_last)))):
            return {"name": step["item"], "noun": nouns[-1], "nouns": nouns,
                    "holder": holder or (hit or {}).get("speaker") or None, "said": take["said"]}
        return None
    if _thing_here(here, nouns) and hit is None:
        return None                     # lying in the scene: @interact's
    if hit is not None:
        # The thing as the reply held it out: "a brass key" -> Brass Key
        return {"name": _title(hit["phrase"]), "noun": nouns[-1], "nouns": nouns,
                "holder": hit["speaker"] or None, "said": take["said"]}
    if offered and not gave_last and _names_noun(nouns, said_last):
        speaker = offered[-1]["speaker"] or None
        return {"name": _title(take["phrase"]), "noun": nouns[-1], "nouns": nouns, "holder": speaker,
                "said": take["said"]}
    return None


def issued_take(player_id: str | None) -> dict[str, Any] | None:
    """The take line this player's current reply was given (tests, replay)."""
    with _lock:
        return dict(_ISSUED[player_id]) if player_id in _ISSUED else None


def taken_fallback(player_id: str | None, context: str | None, results: Iterable[Any],
                   prose: str, roll_outcomes: Iterable[str] = ()) -> dict[str, Any] | None:
    """After the reply: the player took what was held out, the reply's
    binding line said @give, and nothing gave it. As with [START THIS JOB]
    and the travel line, the engine does it.

    Returns {"line": "@give …", "ledger": …} for the server to apply through
    the reply's own runner, {"audit": warning} when the engine cannot name or
    grade the thing (gear needs a tier), or None: the line was answered (any
    @give / @buy / @interact — landed or refused: fleet6-wenna T4 answered
    "the scrap" with `@give Piper's Howl Chart`, and a second, engine-made
    "Scrap" would be the same paper twice), the pack holds it, the story had
    its holder pull it back, or a roll this reply failed (a snatch the dice
    refused is no hand-over)."""
    if not player_id:
        return None
    with _lock:
        issued = _ISSUED.pop(player_id, None)
    if not issued or issued["line"] not in (context or ""):
        return None
    nouns = set(issued["nouns"])
    name = issued["name"]
    if any(getattr(r, "verb", "") in ("give", "buy", "interact") for r in results or []) \
            or any(o in ("failure", "critical_failure") for o in roll_outcomes or ()):
        return None
    try:
        from src.models import Player, get_session

        with get_session() as session:
            player = session.get(Player, player_id)
            if player is None or _pack_has(player, nouns, name):
                return None
    except Exception as e:
        logger.debug(f"take fallback pack unreadable: {e}")
        return None
    if _kept_back(prose, nouns | set(_phrase(name)[1] if name else ())):
        return None
    if not name or issued.get("gear"):
        what = name or f"the {issued['noun']}"
        return {"audit": {"check": "taken_not_given", "detail": (
            f"The player took {what} (\"{issued['said']}\") and your reply wrote no @give, so it is not in "
            f"their pack. If it changed hands, write @give <its name> | kind: … first in your next reply; if "
            "the holder kept it, show that.")}}
    holder = issued.get("holder")
    line = f"@give {name} | kind: {issued['kind']}"
    return {"line": line, "ledger": (
        f"(engine) {line}: the player took it{' from ' + holder if holder else ''} (\"{issued['said']}\") "
        "and your reply wrote no @give, so the engine put it in their pack. It is theirs now — never hand "
        "it over again, and never have anyone still holding it.")}


# A job taken in the very reply that puts its first thing in the player's
# hands. fleet10-tamsin T2: "In," — and the Captain "presses a folded square
# of stamped leather into your palm — the gate pass" in the reply that wrote
# `@quest-start Teeth on the North Road`, and no @give. The take line is
# built before the reply, when that obtain step did not exist, so nothing
# answered it and the pass reached her pack a reply late (T3).
_INTO_YOUR_HANDS = re.compile(
    r"\b(?:press(?:es|ed)?|hand(?:s|ed)?|give(?:s|n)?|gave|pass(?:es|ed)?|slip(?:s|ped)?|tuck(?:s|ed)?|puts?"
    r"|drop(?:s|ped)?|lays?|laid|push(?:es|ed)?|slides?|slid|folds?|folded|closes?|closed)\b[^.!?\n]{0,80}?"
    r"\b(?:into|in|onto)\s+your\s+(?:open\s+)?(?:hand|hands|palm|palms|fingers|grip|fist)\b"
    r"|\b(?:hands?|handed|gives?|gave|passes|passed|tosses|tossed)\s+you\b",
    re.IGNORECASE)
# ...never a hand-over promised, refused or put off: "He'll hand you the pass
# once you've proven yourself", "If you bring the whistle back, he hands you
# the pass", "He doesn't hand you the pass. Not yet." (review probes: each
# gave the pass with the job)
_HANDED_NOT = re.compile(
    r"(?:\b(?:not|never|will|would|could|should|might|may|shall|must|if|unless|whether|until|promis\w*"
    r"|offers?\s+to|means?\s+to|wants?\s+to|about\s+to|going\s+to|ready\s+to|tomorrow)\b"
    r"|n['’]t\b|['’](?:ll|d)\b)", re.IGNORECASE)


def _handed_now(sentence: str) -> bool:
    """Does this sentence put a thing in the player's hands, told as done?"""
    return any(not _HANDED_NOT.search(sentence[:m.start()]) for m in _INTO_YOUR_HANDS.finditer(sentence))


def started_take(player_id: str | None, results: Iterable[Any], prose: str | None) -> dict[str, Any] | None:
    """After the reply's commands: a quest this reply started has an obtain
    step whose item a living holder standing with the player carries, and
    the prose puts it in the player's hands by its noun ("into your palm —
    the gate pass"); no @give, @buy or @interact answered it. Returns
    {"line": "@give …", "ledger": …} for the server to apply through the
    reply's runner, as `taken_fallback` does; else None. Never raises."""
    try:
        return _started_take(player_id, list(results or ()), prose or "")
    except Exception as e:      # a fallback, never a cost to the turn
        logger.warning(f"started-job take unavailable: {e}")
        return None


def _started_take(player_id: str | None, results: list[Any], prose: str) -> dict[str, Any] | None:
    from src.models import NPC, Player, Quest, get_session
    from src.services import objectives as objectives_mod
    from src.services.lead import same_item

    if not player_id or not prose.strip():
        return None
    if any(getattr(r, "verb", "") in ("give", "buy", "interact") for r in results):
        return None
    started = [r.result["id"] for r in results
               if getattr(r, "ok", False) and getattr(r, "tool", "") in ("activate_quest", "create_quest")
               and isinstance(r.result, dict) and r.result.get("id")
               and not r.result.get("already_active") and not r.result.get("exists")]
    if not started:
        return None
    told = [s for s in _sentences(_narration(prose)) if _handed_now(s)]
    if not told:
        return None
    with get_session() as session:
        player = session.get(Player, player_id)
        if player is None or not player.current_location_id:
            return None
        here = [n for n in session.query(NPC).filter(NPC.current_location_id == player.current_location_id,
                                                      NPC.status == "alive").all()]
        for quest_id in started:
            quest = session.get(Quest, quest_id)
            if quest is None:
                continue
            for step in objectives_mod.normalize_objectives(quest.objectives):
                if step.get("done") or step.get("kind") != "obtain" or not step.get("item_name"):
                    continue
                item = str(step["item_name"])
                holder = next((npc for npc in here for entry in npc.inventory_notable or []
                               if isinstance(entry, dict) and entry.get("type") == "quest_item"
                               and same_item(item, str(entry.get("name") or ""))), None)
                nouns = _phrase(item)[1][-1:]
                if holder is None or not nouns or _pack_has(player, nouns, item):
                    continue
                said = next((s for s in told if _names_noun(nouns, s)), None)
                if said is None or _kept_back(prose, set(nouns)):
                    continue
                line = f"@give {item} | kind: {_kind_for(item, nouns)}"
                logger.info(f"Started-job take: {item} from {holder.name} ({said[:80]!r})")
                return {"line": line, "ledger": (
                    f"(engine) {line}: your reply started \"{quest.title}\" and had {holder.name} put the "
                    f"{nouns[0]} in the player's hands, with no @give, so the engine gave it. It is theirs "
                    "now — never hand it over again, and never have anyone still holding it.")}
    return None


# ----------------------------------------------------------------------
# 1b. Who carries what a quest step asks for
# ----------------------------------------------------------------------

def holder_lines(player_id: str | None, present: dict[str, str] | None = None) -> str:
    """One line per open obtain step whose item a living person carries, or
    "". Never raises."""
    if not player_id:
        return ""
    try:
        from src.models import Location, Player, get_session

        out: list[str] = []
        with get_session() as session:
            player = session.get(Player, player_id)
            if player is None:
                return ""
            for step in _open_obtain_steps(session, player):
                if step["holder"] is None or _pack_has(player, [], step["item"]):
                    continue
                npc = step["holder"][0]
                if npc.id in (present or {}):
                    out.append(f"- The {step['item']} (\"{step['quest']}\") is in {npc.name}'s keeping, here: "
                               f"they hand it over in person, in the reply the player asks for it or takes it — "
                               f"@give {step['item']} (it leaves their hands). Never send the player elsewhere "
                               "to fetch it.")
                else:
                    where = session.get(Location, npc.current_location_id) if npc.current_location_id else None
                    out.append(f"- The {step['item']} (\"{step['quest']}\") is in {npc.name}'s keeping"
                               + (f", at {where.name}" if where is not None else "")
                               + " — only they hand it over; nobody else has one.")
        return ("### WHO CARRIES WHAT THE QUESTS ASK FOR\n" + "\n".join(out)) if out else ""
    except Exception as e:
        logger.warning(f"holder lines unavailable: {e}")
        return ""


# Its holder hands it over in words, by name: "That's your pass. North
# Gatehouse, show it" (fleet9-tamsin T3, the reply that started the job — no
# @give, and the step sat open to T18). Never "Say the word and it's yours"
# (fleet2-bram T0, fleet5-bram T0: an offer, conditional)
_YOURS_ALOUD = re.compile(r"\b(?:here['’]?s|that['’]s|this\s+is|there['’]s)\s+your\s+(?P<n>[\w'’-]+(?:\s+[\w'’-]+)?)",
                          re.IGNORECASE)


def handed_aloud_warning(player_id: str | None, spoken: Iterable[dict[str, Any]],
                         results: Iterable[Any]) -> dict[str, str] | None:
    """After the reply: the person carrying an open obtain step's item told
    the player it is theirs, by its name, and the reply wrote no @give (nor
    @buy / @interact / @quest-done): the audit warning, else None. Replayed
    over every transcript: one hit, fleet9-tamsin T3 (fleet5-tamsin T10 and
    fleet7-wenna T8 said it with the @give). Never raises."""
    if not player_id:
        return None
    try:
        if any(getattr(r, "ok", False) and getattr(r, "verb", "") in ("give", "buy", "interact", "quest-done")
               for r in results or []):
            return None
        lines = [(str(s.get("npc_id") or ""), str(s.get("text") or "")) for s in spoken or []
                 if isinstance(s, dict) and s.get("npc_id")]
        if not lines:
            return None
        from src.models import Player, get_session

        with get_session() as session:
            player = session.get(Player, player_id)
            if player is None:
                return None
            for step in _open_obtain_steps(session, player):
                if step["holder"] is None or _pack_has(player, [], step["item"]):
                    continue
                npc = step["holder"][0]
                head = set(_phrase(step["item"])[1][:1])
                for npc_id, text in lines:
                    if npc_id != npc.id:
                        continue
                    for m in _YOURS_ALOUD.finditer(text):
                        if head & set(_phrase(m.group("n"))[1]):
                            return {"check": "item_handover_without_command", "detail": (
                                f"{npc.name} told the player the {step['item']} is theirs (\"{m.group(0)}\") but "
                                f"your reply wrote no @give, so it is still in {npc.name}'s keeping and the "
                                f"quest step is open. If it changed hands, write @give {step['item']} first in "
                                "your next reply (it leaves their keeping); if not, have them still hold it.")}
    except Exception as e:
        logger.debug(f"handed-aloud check unavailable: {e}")
    return None


# ----------------------------------------------------------------------
# 2. A room for the night
# ----------------------------------------------------------------------

# Asking for a bed, or paying for one: "a room for the night", "I'd like a
# room", "three coin for the room" (fleet9-wenna T20), "stay the night",
# "put me up". "The room goes quiet" and "scan the room" ask for nothing
_ROOM = re.compile(
    r"\b(?:a|one|your\s+(?:cheapest|best|spare)|any|the\s+cheapest|a\s+spare|a\s+private|a\s+cheap|a\s+small)\s+"
    r"(?:room|bed|cot|bunk)s?\s+(?:for\s+(?:the|a|one|tonight|two|three)|tonight|here|upstairs|to\s+sleep)\b"
    r"|\b(?:rent|hire|take|book|get|need|want|have|like|buy|pay\s+for)\s+(?:a|one|your|the\s+cheapest)\s+"
    r"(?:spare\s+|private\s+|cheap\s+|small\s+)?(?:room|bed|cot|bunk)\b"
    r"|\b(?:coins?|silver|copper|that)\s+for\s+(?:the|a)\s+(?:room|bed)\b"
    r"|\b(?:any|got\s+(?:a|any))\s+(?:rooms?|beds?)\s+(?:free|left|open|spare|going|to\s+let)\b"
    # the player's own night: "Can I stay the night?" — never "Sorrel, stay
    # here tonight" (fleet8-tamsin T9, an order to the stable hand)
    r"|\b(?:I|we)(?:['’](?:ll|d))?\b(?:\s+[\w'’]+){0,3}?\s+(?:stay|sleep|bed\s+down|lodge)\s+(?:here\s+)?"
    r"(?:the\s+night|tonight|overnight)\b"
    r"|\b(?:place|somewhere|where)\s+to\s+(?:stay|sleep|bed\s+down)\b|\bput\s+(?:me|us)\s+up\b"
    r"|\b(?:a\s+)?(?:room|bed)\s+for\s+the\s+night\b|\bwhere\s+(?:can|could|might)\s+I\s+(?:sleep|stay|bed\s+down)\b",
    re.IGNORECASE,
)
_ROOM_NOT = re.compile(r"\b(?:not|never|don['’]t|won['’]t|can['’]t|cannot|no\s+need|rather\s+than|instead\s+of)\b"
                       r"[^.!?]{0,25}$", re.IGNORECASE)
# A night already slept is owed, not asked for: "I owe you 8 coin for the room
# and the meal" the morning after (fleet5-wenna T19) is no second night
_ROOM_PAST = re.compile(r"\b(?:owe|owed|owing|last\s+night|this\s+morning|for\s+the\s+night\s+I)\b", re.IGNORECASE)
_ROOM_NOT_WITHIN = re.compile(r"\b(?:not|never|cannot|doubt|hardly)\b|n['’]t\b", re.IGNORECASE)


def asks_for_room(player_input: str | None) -> str:
    """The words that ask for (or pay for) a night's bed, or ""."""
    text = _player_words(player_input)
    for sentence in _sentences(text):
        if _ROOM_PAST.search(sentence):
            continue
        for m in _ROOM.finditer(sentence):
            # ...the negation can sit inside the words too: "I can't sleep
            # tonight", "I doubt I'll sleep tonight", "we won't stay the night"
            if not _ROOM_NOT.search(sentence[max(0, m.start() - 30):m.start()]) \
                    and not _ROOM_NOT_WITHIN.search(m.group(0)):
                return m.group(0).strip()
    return ""


def room_directive(player_id: str | None, player_input: str | None) -> str | None:
    """The binding line for a reply whose player asks for a room where one
    can be had, or None. Never raises."""
    asked = asks_for_room(player_input)
    if not asked or not player_id:
        return None
    try:
        from src.models import Location, Player, get_session
        from src.services.rest import rest_here

        with get_session() as session:
            player = session.get(Player, player_id)
            here = session.get(Location, player.current_location_id) if player and player.current_location_id else None
            spec = rest_here(here)
        if not spec:
            return None
    except Exception as e:
        logger.warning(f"room intent unavailable: {e}")
        return None
    price = int(spec.get("price") or 0)
    cost = f"{price} coin" if price else "free"
    charges = f"it charges the {price} coin itself" if price else "it costs nothing"
    return (
        f"[THIS REPLY: the player asks for a room (\"{asked[:60]}\") — a night here is {cost} "
        f"({spec.get('kind')}, {spec.get('quality')} bed). If they take it, write @rest: {charges} and sleeps "
        "them to morning. Never a @coin for the room, and never a room or a key handed over without @rest. If "
        f"they only ask, the price is {cost}, no other number; if the keeper turns them away, say why in the "
        "story.]"
    )


# ----------------------------------------------------------------------
# 3. A page asked for, and a lid opened only to look
# ----------------------------------------------------------------------

_READ_VERB = re.compile(
    # "see" only asked for: "I'd like to see your guest ledger" (fleet9-wenna
    # T20) — "I see a ledger on the bar" is noticing it
    r"\b(?:read|reads|reading|(?:let\s+me|can\s+I|could\s+I|may\s+I|like\s+to|want\s+to|need\s+to|to)\s+see"
    r"|look\s+(?:at|over|through)|looking\s+(?:at|over|through)|examine\w*"
    r"|study|studying|inspect\w*|peruse\w*|check\w*|glance\s+over|scan\w*|skim\w*|consult\w*"
    r"|leaf\w*\s+through|flip\w*\s+through|go\w*\s+through|have\s+a\s+look\s+at|take\s+a\s+look\s+at"
    r"|show\s+me|let\s+me\s+see|browse\w*)\b",
    re.IGNORECASE,
)
_READ_NOT = re.compile(r"\b(?:not|never|don['’]t|won['’]t|without|no\s+need\s+to|rather\s+than|instead\s+of)\s+"
                       r"(?:\w+\s+){0,2}$", re.IGNORECASE)
# The reader is the player: "he reads people like a ledger" (fleet6-wenna T1)
# is the Captain, not a request
_READER = frozenset({"i", "i'd", "i'll", "i'm", "me", "we", "let", "to", "can", "could", "may", "please", "just"})
_LABEL_STOP = frozenset({"the", "a", "an", "of", "on", "in", "old", "some", "small", "large", "loose", "near",
                         "by", "with", "and", "its", "his", "her", "their"})
_LABEL_CUT = re.compile(r"\s+(?:on|of|in|at|by|near|from|with|under|beside)\s+", re.IGNORECASE)


def _label_words(label: str) -> tuple[set[str], str]:
    """(its words, its head noun): "Tally marks on the hut wall" is marks —
    "that north wall" is no request to read them (fleet8-bram T20)."""
    words = [_stem(w) for w in re.findall(r"[A-Za-z][\w'’-]*", label or "") if w.lower() not in _LABEL_STOP]
    words = [w for w in words if len(w) >= 3]
    first = [_stem(w) for w in re.findall(r"[A-Za-z][\w'’-]*", _LABEL_CUT.split(label or "", maxsplit=1)[0])
             if w.lower() not in _LABEL_STOP and len(w) >= 3]
    return set(words), (first[-1] if first else (words[-1] if words else ""))


_OTHER_READER = frozenset({"he", "she", "they", "it", "you", "his", "her", "their", "someone", "everyone"})


def _reads_as_player(sentence: str, start: int, verb: str) -> bool:
    """Is the player the one reading? The nearest subject before the verb
    decides: "I", "let me", "to see" yes; "he reads", "Coldiron looks at"
    no; a sentence that opens on the verb asks it."""
    before = sentence[:start]
    raw = re.findall(r"[A-Za-z][\w'’]*", before)
    for i in range(len(raw) - 1, max(-1, len(raw) - 6), -1):
        word = raw[i].lower().replace("’", "'")
        if word in _READER:
            return True
        # "he reads", "he quickly reads", "Coldiron looks at" — never the
        # Notice board's own capital ("…the Notice board, scanning it")
        if (i >= len(raw) - 2 and word in _OTHER_READER) or (
                i == len(raw) - 1 and i and raw[i][:1].isupper() and word not in ("i", "i'd", "i'll", "i'm")):
            return False
    if not raw:
        return True
    # "…the notice board, scanning it": the player's own running action
    return verb.lower().endswith("ing") and bool(re.search(r",\s*$", before))


def read_target(player_input: str | None, things: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """The read-kind Thing the player asks to read or see by name ("I'd like
    to see your guest ledger"), or None. Their quoted words count: asking
    the keeper IS the request."""
    text = _player_words(player_input)
    if not text:
        return None
    for sentence in _sentences(text.replace("“", "\"").replace("”", "\"")):
        for m in _READ_VERB.finditer(sentence):
            if _READ_NOT.search(sentence[max(0, m.start() - 30):m.start()]) \
                    or not _reads_as_player(sentence, m.start(), m.group(0).split()[0]):
                continue
            # its object, up to the clause's end: "someone to read those notes,
            # or just hoping the board grows eyes" reads no board (fleet-bram T14)
            object_part = re.split(r"[,;:—–]|\b(?:or|and|but|then|while)\b", sentence[m.end():], maxsplit=1)[0]
            after = [_stem(w) for w in re.findall(r"[A-Za-z][\w'’-]*", object_part)][:7]
            before = [_stem(w) for w in re.findall(r"[A-Za-z][\w'’-]*", sentence[:m.start()])]
            for thing in things:
                words, head = _label_words(thing["label"])
                if head and head in after:
                    return thing
                # "I head for the notice board, scanning it"
                if head and head in before and after[:1] == ["it"]:
                    return thing
    return None


def read_directive(player_id: str | None, player_input: str | None) -> str | None:
    """The binding line for a reply whose player asks to read a Thing here,
    or None. Never raises."""
    if not player_id or not _player_words(player_input):
        return None
    try:
        from src.models import Location, Player, get_session
        from src.services.scene_objects import normalize_objects

        with get_session() as session:
            player = session.get(Player, player_id)
            here = session.get(Location, player.current_location_id) if player and player.current_location_id else None
            things = [o for o in normalize_objects(here.objects) if o["kind"] == "read"] if here is not None else []
        thing = read_target(player_input, things)
    except Exception as e:
        logger.warning(f"read intent unavailable: {e}")
        return None
    if thing is None:
        return None
    label = thing["label"]
    return (
        f"[THIS REPLY: the player reads the {label} — write @interact {label} first (it gives you the words: "
        "quote them, never add to them), or have its owner refuse, in the story. Never grant a look and leave "
        "it unread.]"
    )


# A lid lifted only to look: "ease the lid open just enough to see what's
# inside without disturbing it" (fleet9-bram T14), "peek inside", "don't
# touch anything", "leave it where it is"
# The verb's object is the thing, or nothing at all: "careful not to move too
# fast", "I tell Sorrel not to move while I search", "I don't take my eyes
# off him" are no look-only
_LOOK_OBJ = r"(?=\s+(?:it|them|anything|a\s+thing|nothing|what|its|the\s+[\w'’-]+)\b|\s*[,.;:!?—–]|\s*$)"
_LOOK_ONLY = re.compile(
    rf"\bwithout\s+(?:\w+\s+){{0,2}}?(?:disturbing|touching|taking|moving|removing|lifting){_LOOK_OBJ}"
    r"|\b(?:just|only|merely|simply)\s+(?:enough\s+)?(?:to\s+)?(?:look|see|peek|peer|glance|check|have\s+a\s+look)\b"
    r"|\b(?:peek|peeks|peeking|peer|peers|peering)\s+(?:in|into|inside|under)\b"
    rf"|\b(?:don['’]t|do\s+not|careful\s+not\s+to|won['’]t|not\s+to)\s+(?:touch|take|disturb|move|remove){_LOOK_OBJ}"
    r"|\b(?:leave|leaving)\s+(?:it|them|everything|the\s+[\w'’-]+(?:\s+[\w'’-]+)?)\s+(?:where\s+(?:it|they)\s+"
    r"(?:is|are|lies?|lay)|be\b|as\s+(?:it|they)\s+(?:is|are|was|were)|in\s+place|undisturbed|untouched)"
    r"|\b(?:touching|taking)\s+nothing\b",
    re.IGNORECASE,
)
_TAKES_TOO = re.compile(r"\b(?:take|takes|taking|grab\w*|pocket\w*|snatch\w*|pick\s+(?:it|them)\s+up|help\s+myself"
                        r"|keep\s+(?:it|them)|stuff\w*|stash\w*)\b", re.IGNORECASE)


def look_only(player_input: str | None, label: str, held: str = "", *, container: bool = False) -> bool:
    """Did the player open or search this Thing only to look? A sentence of
    their narration with a look-only marker that names it — a container
    (`container`: an open or search Thing) also by its lid or its inside —
    and no word in it that takes it. "I fish the sweetroot out of the mud.
    Then I … ease the lid open just enough to see what's inside without
    disturbing it" looks into the crate and takes the sweetroot."""
    text = _narration(_player_words(player_input))
    if not text:
        return False
    words, head = _label_words(label)
    item_words, item_head = _label_words(held)
    names = {w for w in (head, item_head) if w} | ((words | item_words) - {"dropped", "abandoned", "loose", "old"})
    if container:
        names |= {"lid", "inside", "content"}

    def names_it(sentence: str) -> bool:
        said = {_stem(w) for w in re.findall(r"[A-Za-z][\w'’-]*", sentence)}
        return bool(said & names)

    marked = False
    for sentence in _sentences(text):
        if not _LOOK_ONLY.search(sentence):
            continue
        if names_it(sentence):
            marked = True
        # "I pocket the sweetroot and leave the rope where it is": the take
        # names something else; a take of THIS thing undoes the look-only
        for m in _TAKES_TOO.finditer(sentence):
            taken = re.split(r"[,;:—–]|\band\b|\bthen\b|\bbut\b", sentence[m.end():m.end() + 60], maxsplit=1)[0]
            if names_it(taken):
                return False
    return marked


# ----------------------------------------------------------------------
# 3b. Put back what the last reply took
# ----------------------------------------------------------------------

# player id -> [{"location_id", "object_id", "label", "items": [{"name", "quantity"}]}]
_TAKEN: dict[str, list[dict[str, Any]]] = {}


def note_taken(player_id: str | None, location_id: str, object_id: str, label: str,
               items: Iterable[dict[str, Any]]) -> None:
    """An @interact this reply put a Thing's contents in the pack (commands)."""
    gained = [{"name": str(i.get("name")), "quantity": int(i.get("quantity") or 1)}
              for i in items or [] if isinstance(i, dict) and i.get("name")]
    if not player_id or not gained:
        return
    with _lock:
        _TAKEN.setdefault(player_id, []).append({"location_id": location_id, "object_id": object_id,
                                                 "label": label, "items": gained})


_LEAVE = re.compile(
    r"\b(?:leave|leaves|leaving|left)\s+(?P<a>it|them|the\s+[\w'’-]+(?:\s+[\w'’-]+)?)\s+(?:where\s+(?:it|they)"
    r"|be\b|behind|alone|there|in\s+(?:the|its)|as\s+(?:it|they)|for\s+(?:someone|whoever|its))"
    r"|\b(?:put|puts|putting|set|sets|setting|place|places|placing|drop|drops|dropping|return|returns|returning"
    r"|toss|tosses|tossing|slip|slips|slipping|lay|lays|laying|coil|coils|coiling)\s+"
    r"(?P<b>it|them|the\s+[\w'’-]+(?:\s+[\w'’-]+)?)\s+back\b"
    r"|\b(?:return|returns|returning)\s+(?P<c>it|them|the\s+[\w'’-]+(?:\s+[\w'’-]+)?)\s+to\s+(?:the|its)\b",
    re.IGNORECASE,
)


def put_back_line(player_id: str | None, player_input: str | None) -> str | None:
    """The player leaves (or puts back) what the last reply's @interact took:
    the engine returns it to its Thing and says so. Spends the remembered
    takes either way — only the reply right after counts. Never raises."""
    if not player_id:
        return None
    with _lock:
        rows = _TAKEN.pop(player_id, [])
    text = _narration(_player_words(player_input))
    if not rows or not text:
        return None
    try:
        return _put_back(player_id, rows, text)
    except Exception as e:
        logger.warning(f"put-back unavailable: {e}")
        return None


def _put_back(player_id: str, rows: list[dict[str, Any]], text: str) -> str | None:
    from src.models import Location, Player, get_session
    from src.services.scene_objects import normalize_objects
    from src.tools.world_write.player import remove_from_inventory

    returned: list[str] = []
    every = [(row, item) for row in rows for item in row["items"]]
    for m in _LEAVE.finditer(text):
        obj = m.group("a") or m.group("b") or m.group("c") or ""
        if _NOT_TAKING.search(text[max(0, m.start() - 20):m.start()]):
            continue
        phrase, nouns, _ = _phrase(obj)
        if phrase in _PRONOUNS:
            # "I leave them be / alone" is people, never the rope in the pack
            if phrase == "them" and m.group("a") and re.match(r"\s*(?:be|alone)\b", text[m.end("a"):]):
                continue
            picks = every if len(every) == 1 else []
        else:
            picks = [(row, item) for row, item in every
                     if {_stem(w) for w in re.findall(r"[A-Za-z][\w'’-]*", item["name"])} & set(nouns)]
        for row, item in picks:
            if item["name"] in returned:
                continue
            with get_session() as session:
                player = session.get(Player, player_id)
                if player is None or player.current_location_id != row["location_id"] \
                        or not _pack_has(player, [], item["name"]):
                    continue
            removed = remove_from_inventory(player_id, item["name"], item["quantity"])
            if not removed.get("success"):
                continue
            with get_session() as session:
                location = session.get(Location, row["location_id"])
                if location is not None:
                    objects = normalize_objects(location.objects)
                    for o in objects:
                        if o["id"] == row["object_id"]:
                            o["consumed"] = False
                    location.objects = objects      # reassigned: the JSON column must see a new list
                    session.commit()
            returned.append(item["name"])
            logger.info(f"Put back as the player said: {item['name']} -> {row['label']}")
    if not returned:
        return None
    where = rows[0]["label"]
    things = ", ".join(returned)
    return (f"[ENGINE — already done: the player left the {things} in the {where}, as they said — it is out "
            "of their pack and back where it was. Show them leaving it; never @give or @remove it.]")


# ----------------------------------------------------------------------
# 4. A MUST-DELIVER event the roll-first hold would have thrown away
# ----------------------------------------------------------------------

def must_deliver_sentences(text: str | None) -> str:
    """The sentences of `text` that tell an event happening where the player
    stands (the world tick's MUST-DELIVER this reply), joined, or "".

    fleet9-wenna T10: a false haggling roll held the reply; its first prose
    ("…the howl breaks over the square — three long notes, then silence")
    was dropped unread at the roll, the event was re-sent twice, then filed
    as unseen though the player wrote "The howl echoes in my ears". An
    event's own sentence assumes no dice."""
    if not (text or "").strip():
        return ""
    try:
        from src.models import Event, get_session
        from src.services import world_tick

        ids = set(world_tick._must_deliver)
        if not ids:
            return ""
        with get_session() as session:
            events = session.query(Event).filter(Event.id.in_(list(ids))).all()
            kept = [s for s in _sentences(text) if any(world_tick.event_told(e, s) for e in events)]
        return " ".join(kept)
    except Exception as e:
        logger.debug(f"must-deliver sentences unavailable: {e}")
        return ""


# ----------------------------------------------------------------------
# 5. A voice with no row
# ----------------------------------------------------------------------

# player id -> {label key: {"label", "place_id", "replies": [reply numbers]}}
_VOICES: dict[str, dict[str, dict[str, Any]]] = {}
# player id -> {label key: lines given}
_VOICE_TOLD: dict[str, dict[str, int]] = {}
_REPLY_NO: dict[str, int] = {}
VOICE_LINES = 2      # [THIS REPLY: @new-npc …] lines per voice; then the audit's job
# A crowd, a sound or anyone at all is nobody to register
_NOBODY = re.compile(
    r"\b(?:voice|voices|someone|somebody|crowd|townsfolk|people|everyone|several|many|others|all|chorus|man\s+in"
    r"|woman\s+in|stranger|passer|passers|bystander|bystanders|onlooker|onlookers|unknown|narrator|player)\b"
    r"|s$",
    re.IGNORECASE,
)


def _voice_key(label: str) -> str:
    return " ".join(re.findall(r"[a-z]+", (label or "").lower()))


def note_speakers(player_id: str | None, spoken: Iterable[dict[str, Any]], place_id: str | None) -> None:
    """After a reply: count the replies each role-only voice (a speaker line
    that resolved to nobody) spoke in. Never raises."""
    if not player_id:
        return
    try:
        with _lock:
            reply = _REPLY_NO[player_id] = _REPLY_NO.get(player_id, 0) + 1
            voices = _VOICES.setdefault(player_id, {})
            for line in spoken or []:
                if not isinstance(line, dict) or line.get("npc_id"):
                    continue
                label = str(line.get("label") or line.get("npc_name") or line.get("npc") or "").strip()
                key = _voice_key(label)
                if not key or len(key) > 40 or _NOBODY.search(key):
                    continue
                row = voices.setdefault(key, {"label": label, "place_id": line.get("_where") or place_id,
                                              "replies": []})
                if reply not in row["replies"]:
                    row["replies"].append(reply)
    except Exception as e:
        logger.debug(f"speaker bookkeeping failed: {e}")


def new_npc_directive(player_id: str | None) -> str | None:
    """The binding line that registers a role-only voice heard in a second
    reply, or None. At most VOICE_LINES times per voice. Never raises.

    Why a line and not an engine-made row: the engine cannot tell which of
    the unnamed voices a later name belongs to ("Derrin" was said by the
    Captain, in the square, of a watchman at the gatehouse), nor give them
    the look and voice the prose already gave — a wrong row is a stranger on
    the canvas under a made-up portrait. @new-npc carries the DM's own
    words, and refuses a duplicate or a lost person (commands._h_new_npc)."""
    if not player_id:
        return None
    try:
        return _new_npc_directive(player_id)
    except Exception as e:
        logger.warning(f"new-npc line unavailable: {e}")
        return None


def _new_npc_directive(player_id: str) -> str | None:
    from src.models import NPC, Location, get_session
    from src.services.dialogue import _by_trade, resolve_speaker

    with _lock:
        voices = {k: dict(v) for k, v in (_VOICES.get(player_id) or {}).items() if len(v["replies"]) >= 2}
        told = _VOICE_TOLD.setdefault(player_id, {})
    if not voices:
        return None
    lines: list[str] = []
    with get_session() as session:
        people = session.query(NPC).filter(NPC.status == "alive").all()
        for key, row in voices.items():
            if told.get(key, 0) >= VOICE_LINES:
                continue
            label = row["label"]
            # Somebody already is this voice now (a @new-npc since, a name)
            if resolve_speaker(session, label, None) is not None:
                with _lock:
                    (_VOICES.get(player_id) or {}).pop(key, None)
                continue
            place = session.get(Location, row["place_id"]) if row.get("place_id") else None
            at = f" | at: {place.name}" if place is not None else ""
            same_trade = _by_trade(label, people)
            if same_trade is not None:
                where = session.get(Location, same_trade.current_location_id) \
                    if same_trade.current_location_id else None
                lines.append(
                    f"[THIS REPLY: the {label} you have voiced in two replies is nobody in the world — but "
                    f"{same_trade.name} ({same_trade.profession}) is{', at ' + where.name if where else ''}. If it "
                    f"was them, write @npc-move {same_trade.name}{' | ' + place.name if place else ''} and voice "
                    f"them as themself from now on; if not, @new-npc <their name> | trade: {label.lower()} | "
                    f"looks: … | manner: … | voice: …{at}.]")
            else:
                lines.append(
                    f"[THIS REPLY: the {label} has spoken in two replies and is nobody in the world yet — write "
                    f"@new-npc <their name> | trade: {label.lower()} | looks: … | manner: … | voice: …{at} first, "
                    "keeping the look and voice you gave them (a name someone said for them is theirs), so they "
                    "and what they told the player are remembered. If they were a passer-by now gone, write "
                    "nothing.]")
            with _lock:
                told[key] = told.get(key, 0) + 1
    return "\n".join(lines) or None

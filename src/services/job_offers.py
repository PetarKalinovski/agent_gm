"""Authored work: offered, taken, started — even when the DM forgets to.

fleet7 (all three sessions): Piper pitched "Counting the Howls" with its pay
(-bram T3: "Ten coin and a tincture if you ask the stable boy what he hears
after dark"), the player said yes (-bram T5: "Alright. I'll talk to him") or
simply went and did its first step (-wenna T3-T7: four turns with Sorrel),
and no @quest-start was ever written: the witness work earned nothing, no
lead sent them back to her. In round 5 the same job started and paid.

Three engine paths, per player, in memory (a restart forgets an open offer;
the dossier still lists the job):

1. An offer is remembered (`note_reply`, after a reply's dialogue is
   filed): a speaker line from the giver of a not-started authored quest
   that names its pay, or names its first step's person or place as a job,
   or asks the player for its work in its own words (`pitched`). The giver
   merely speaking after an [OFFER THIS REPLY] line no longer counts.
2. The player's next words take it (`start_directive`, in the context
   build): a yes ("Alright", "Deal", "I'll do it", "I'll talk to him"),
   taking the thing the giver held out ("I pocket the vial"), or setting
   out to do its first step ("I'll find Sorrel at the stable yard", "I head
   toward the stables") puts a binding [START THIS JOB] line before
   their words. A no, a "not now", an "after" or a haggle never does — a
   no closes the offer until the giver pitches it again, a haggle holds it
   (play starts nothing) until the giver answers the price.
3. Play does a step before anyone starts it (`note_play`, from the
   objective hooks: talking to a talk_to target, standing at a go_to place,
   holding an obtain item). Started later — by @quest-start, or by the
   engine itself at the end of the reply that did the step (`settle`) —
   the job is credited with what play already did (`backfill`). Done
   before the offer, a step counts only as path 4's credit — a talk only
   when it was about the job (fleet6-tamsin T4 asked Sorrel about the
   ash-gray man's face a reply before Piper offered it) — and a step that
   closes the loop (the report back to the giver) waits for the steps
   before it, as always.
4. Play does a step of a job nobody ever offered (fleet9-bram: Piper only
   pointed at Sorrel at T3, Bram asked him at T7 and stood out the 20:00
   howl at T9, and "Counting the Howls" stayed not_started — 0 XP). That
   work is remembered as credit (`note_play`, a talk only about the job,
   `_talked_about`; a lived-through event for a "stand out the next howl"
   step, `note_witnessed`): the giver's next
   appearance gets an [OFFER THIS REPLY] that names it (`credited`), and
   the start ticks it (`backfill`). Credit never starts a job by itself —
   the player still says yes (`settle` waits on play since a pitch).

A pitch is the giver naming the pay or asking for the work (`pitched`):
fleet9-bram T2-T3, "I've been counting the howls for a week" and "he'd tell
you more … if you asked him proper" were Piper talking, not offering.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable, Iterable

from src.models import NPC, Location, Player, Quest, QuestStatus, get_session

logger = logging.getLogger(__name__)

OFFER_WINDOW = 4       # replies after a pitch in which the player's words can still take it
OFFER_KEEP = 12        # replies an untaken offer is remembered at all
START_LINES = 2        # [START THIS JOB] lines per acceptance; after that the engine's start waits on play

# player id -> {"reply": replies seen, "offers": {quest id: offer},
#               "worked": {quest id: [objective ids play did before any pitch]},
#               "events": {event ids already read for a vigil}}
# offer: {"at": reply it was (last) pitched in, "heard": reply its giver last spoke in,
#         "status": open|accepted|haggling|declined|started,
#         "met": [objective ids play did since], "credit": [ids done before the pitch],
#         "told": START lines given, "said": the taking words}
_STATE: dict[str, dict[str, Any]] = {}
_lock = threading.RLock()


def reset(player_id: str | None = None) -> None:
    """Forget the offers (one player's, or everyone's: tests, world switches)."""
    with _lock:
        if player_id is None:
            _STATE.clear()
        else:
            _STATE.pop(player_id, None)


def _state(player_id: str) -> dict[str, Any]:
    return _STATE.setdefault(player_id, {"reply": 0, "offers": {}})


def offered_before(player_id: str | None, quest_id: str) -> bool:
    """Was this job pitched to this player in an earlier reply? (This
    reply's own offer is noted only after its dialogue is filed.)"""
    with _lock:
        return bool(player_id) and quest_id in _state(player_id)["offers"]


def open_offers(player_id: str | None) -> dict[str, dict[str, Any]]:
    """A copy of the remembered offers, by quest id (tests, the replay)."""
    if not player_id:
        return {}
    with _lock:
        return {q: dict(o, met=list(o["met"]), credit=list(o.get("credit") or []))
                for q, o in _state(player_id)["offers"].items()}


# ----------------------------------------------------------------------
# What a pitch names
# ----------------------------------------------------------------------

_UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_SMALL = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "hundred": 100}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
         "ninety": 90}
_AMOUNT = (r"(\d+|(?:" + "|".join(_TENS) + r")(?:[-\s](?:" + "|".join(_UNITS) + r"))?|"
           + "|".join(list(_SMALL) + list(_UNITS)) + r")")
_MONEY = r"(?:coins?|silver|gold|copper|crowns?|marks?|pieces?|pennies)"
# "ten coin", "twenty silver pieces", "pays ten", "I'll give you twenty"
_PRICE = re.compile(
    rf"\b{_AMOUNT}\s+(?:[a-z]+\s+)?{_MONEY}\b"
    rf"|\b(?:pays?|paying|paid|give\s+you|offer(?:ing)?\s+you|worth)\s+(?:you\s+)?{_AMOUNT}\b",
    re.IGNORECASE,
)
# The giver framing it as work for the player: a price, a job, a favour
_JOB_FRAME = re.compile(
    r"\b(?:job|work|errand|task|pay|pays|paid|coin|reward|favou?r|for\s+me|for\s+your\s+trouble"
    r"|in\s+it\s+for\s+you|I'?ll\s+give\s+you|throw\s+in|if\s+you\s+(?:ask|find|go|bring|fetch|carry|take|stand))\b",
    re.IGNORECASE,
)
# Person and place words too common to name anybody's step
_PLAIN = frozenset({
    "captain", "keeper", "master", "mistress", "lady", "lord", "sergeant", "hand", "boy", "girl", "man",
    "woman", "folk", "help", "worker", "apprentice", "square", "road", "house", "hall", "street",
    "market", "yard", "room", "gate", "town", "city", "village", "district", "quarter", "lane", "bridge",
    "north", "south", "east", "west", "upper", "lower", "inner", "outer", "watch", "with", "from",
    "into", "that", "this", "their", "what", "after", "dark", "night", "back", "bring", "find",
    "piece", "pieces", "coin", "coins", "item", "thing", "some",
})


def _amount(text: str) -> int | None:
    word = text.strip().lower().replace("-", " ")
    if word.isdigit():
        return int(word)
    parts = word.split()
    if parts and parts[0] in _TENS:
        return _TENS[parts[0]] + (_UNITS.get(parts[1], 0) if len(parts) > 1 else 0)
    return _SMALL.get(word) or _UNITS.get(word)


def amounts(text: str) -> set[int]:
    """Every price said in `text` ("ten coin" -> 10, "pays twenty" -> 20)."""
    out: set[int] = set()
    for m in _PRICE.finditer(text or ""):
        value = _amount(m.group(1) or m.group(2) or "")
        if value:
            out.add(value)
    return out


def _words(text: str) -> list[str]:
    return [w.replace("'", "").replace("’", "") for w in re.findall(r"[a-z][a-z'’]*", (text or "").lower())]


def _distinct(text: str) -> set[str]:
    """A name's words that could name it in talk: four letters or more, not
    a title or an everyday place word ("Stable Yard" -> stable)."""
    out = set()
    for w in _words(text):
        w = w[:-1] if w.endswith("s") and len(w) > 4 and not w.endswith("ss") else w
        if len(w) >= 4 and w not in _PLAIN:
            out.add(w)
    return out


def _says(word: str, words: Iterable[str]) -> bool:
    """`word` said, or a near form of it ("stables" for "stable")."""
    return any(w == word or (w.startswith(word) and len(w) - len(word) <= 2) for w in words)


def _first_step(quest) -> dict[str, Any] | None:
    from src.services.objectives import _is_return_step, normalize_objectives

    objectives = normalize_objectives(quest.objectives)
    giver = getattr(quest, "assigned_by_npc_id", None)
    for i, obj in enumerate(objectives):
        if obj["done"] or obj["kind"] == "custom":
            continue
        if _is_return_step(obj, objectives[:i], giver):
            continue
        return obj
    return None


def offer_terms(session, quest) -> dict[str, Any]:
    """What naming this job sounds like: {"coin": the price (balance and
    whole), "items": reward item words, "step": its first engine step,
    "step_words": the words that name that step's person, place or thing}."""
    from src.tools.world_write.quests import reward_balance, reward_coin

    rewards = quest.rewards if isinstance(quest.rewards, dict) else {}
    coin = {c for c in (reward_coin(rewards), reward_coin(reward_balance(rewards))) if c}
    items: set[str] = set()
    for entry in rewards.get("items") or []:
        name = entry.get("name") if isinstance(entry, dict) else entry
        items |= _distinct(str(name or ""))
    step = _first_step(quest)
    step_words: set[str] = set()
    if step is not None:
        target = step.get("target_id")
        if step["kind"] in ("talk_to", "deliver", "kill") and target:
            npc = session.get(NPC, target)
            if npc is not None:
                step_words |= _distinct(npc.name) | _distinct(npc.profession or "")
                place = session.get(Location, npc.current_location_id) if npc.current_location_id else None
                if place is not None:
                    step_words |= _distinct(place.name)
        if step["kind"] == "go_to" and target:
            place = session.get(Location, target)
            if place is not None:
                step_words |= _distinct(place.name)
        if step.get("item_name"):
            step_words |= _distinct(step["item_name"])
    return {"coin": coin, "items": items, "step": step, "step_words": step_words}


def names_offer(text: str, terms: dict[str, Any]) -> bool:
    """Does this line from the giver pitch the job? Its price ("ten coin"),
    a reward item said as pay ("a tincture if you…"), or its first step's
    person or place said as work ("Ask Sorrel at the stables… I'll pay")."""
    if not (text or "").strip():
        return False
    if terms["coin"] & amounts(text):
        return True
    words = _words(text)
    framed = bool(_JOB_FRAME.search(text))
    if framed and any(_says(w, words) for w in terms["items"]):
        return True
    return framed and any(_says(w, words) for w in terms["step_words"])


# The giver asking the player for it: "will you…", "could use someone",
# "need someone to…" — with the job's own words in the same line, that is
# the offer even unpriced (fleet5-bram T0: "I need someone who isn't mine to
# lose — eyes on Wolf's Hollow, find out why the wolves…"). Replayed over
# fleet-fleet9: 5 lines more than the price-or-step rule; the 3 in one line
# were the main job's pitches, the 2 split ones Piper asking the player to
# take her chart (fleet5-wenna T4, fleet9-wenna T8) — no job, left out
_ASKS = re.compile(
    r"\b(?:will|would|could|can)\s+you\b|\bcould\s+use\s+(?:someone|somebody|you|a\s+hand|help)\b"
    r"|\bneed\s+(?:someone|somebody|a\s+pair\s+of)\b|\btake\s+(?:it|this|the\s+job|the\s+work)\s+on\b"
    r"|\bif\s+you(?:'re|\s+are)\s+(?:willing|game|up\s+for)\b",
    re.IGNORECASE,
)


def pitched(session, quest, text: str) -> bool:
    """Do these lines (the giver's own) offer this job — its pay named
    (`names_offer`), or its work asked of the player in the job's words (its
    title, its first step, its reward)? fleet9-bram T2-T3: Piper's "I've
    been counting the howls for a week" and "He'd tell you more … if you
    asked him proper" were taken as the pitch of "Counting the Howls": the
    push stopped, nobody ever offered it, and the work Bram did paid nothing."""
    if not (text or "").strip():
        return False
    terms = offer_terms(session, quest)
    if names_offer(text, terms):
        return True
    work = _distinct(quest.title or "") | terms["step_words"] | terms["items"]
    for line in text.splitlines():
        if not (_JOB_FRAME.search(line) or _ASKS.search(line)):
            continue
        words = _words(line)
        if any(_says(w, words) for w in work):
            return True
    return False


def _clip(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _where(session, obj: dict[str, Any], player) -> str:
    """Where a step's work is, from the player's feet: the Thing holding its
    item ("the Lost traveler's satchel, here where the player stands (locked:
    ask her first)"), whoever carries it, or the person or place it names.
    fleet9-bram T17-T18: the Keeper put the satchel "upstairs, under the bed
    in the end room" and slid a key across the bar, while the satchel lay on
    her own floor and the step said take the letter from it. "" when unknown."""
    from src.services.lead import _pack_names, _thing_phrase, holding_things, same_item

    here = getattr(player, "current_location_id", None)
    item = obj.get("item_name")
    if item and obj.get("kind") in ("obtain", "deliver"):
        if player is not None and any(same_item(item, name) for name in _pack_names(player)):
            return "they already carry it"
        places = session.query(Location).all()
        places.sort(key=lambda p: p.id != here)              # here first
        for place in places:
            for thing in holding_things(place):
                if same_item(item, thing["payload"]["item"]["name"]):
                    lock = thing["payload"].get("lock")
                    at = "here, where the player stands" if place.id == here else f"at {place.name}"
                    # The whistle IS the proof; the letter is IN the satchel
                    what = (f"{_thing_phrase(thing['label'])} lies {at}" if same_item(thing["label"], item)
                            else f"it is in {_thing_phrase(thing['label'])}, {at}")
                    return what + (f" (locked: {_clip(lock, 100)})" if lock else "")
        for npc in session.query(NPC).filter(NPC.status == "alive").all():
            stock = npc.inventory_notable if isinstance(npc.inventory_notable, list) else []
            if any(isinstance(e, dict) and same_item(item, e.get("name")) for e in stock):
                return f"{npc.name} carries it"
    target = obj.get("target_id")
    if target and obj.get("kind") in ("talk_to", "deliver", "kill"):
        npc = session.get(NPC, target)
        if npc is not None:
            if npc.current_location_id and npc.current_location_id == here:
                return f"{npc.name}, here"
            place = session.get(Location, npc.current_location_id) if npc.current_location_id else None
            return f"{npc.name}, at {place.name}" if place is not None else ""
    if target and obj.get("kind") == "go_to":
        place = session.get(Location, target)
        if place is not None:
            return "here" if place.id == here else f"at {place.name}"
    return ""


def job_steps(session, quest, player=None, *, skip: Iterable[str] = ()) -> str:
    """The job as written, for an offer line or a dossier: "1) Take the
    letter from the lost traveler's satchel — the Lost traveler's satchel
    here, where the player stands, holds it (locked: …); 2) …; 3) …". Done
    steps (and `skip`, the ones play already did) are left out; the first
    left says where its work is (`_where`)."""
    from src.services.objectives import normalize_objectives

    skipped = set(skip or ())
    parts: list[str] = []
    for i, obj in enumerate(normalize_objectives(quest.objectives), 1):
        if obj["done"] or obj["id"] in skipped:
            continue
        text = _clip(obj.get("text"), 120)
        if not parts:
            try:
                where = _where(session, obj, player)
            except Exception as e:      # the step's words alone still say it
                logger.debug(f"where of a step unreadable: {e}")
                where = ""
            if where:
                text += f" — {where}"
        parts.append(f"{i}) {text}")
    return "; ".join(parts)


def _proof_here(session, quest, player, *, skip: Iterable[str] = ()) -> str:
    """" PROOF HERE: …" when the job's next step takes or hands over an item
    a Thing in this very room holds — the brief's own PROOF HERE line only
    reaches a started job, one reply too late (fleet9-bram T18: the reply
    that wrote @quest-start had the Keeper send the player "upstairs, under
    the bed in the end room" for the satchel on her floor). "" otherwise."""
    from src.services.lead import _pack_names, _thing_phrase, holding_things, same_item
    from src.services.objectives import normalize_objectives

    if player is None or not player.current_location_id:
        return ""
    try:
        skipped = set(skip or ())
        step = next((o for o in normalize_objectives(quest.objectives)
                     if not o["done"] and o["id"] not in skipped), None)
        item = (step or {}).get("item_name")
        if not item or step["kind"] not in ("obtain", "deliver"):
            return ""
        if any(same_item(item, name) for name in _pack_names(player)):
            return ""
        place = session.get(Location, player.current_location_id)
        for thing in holding_things(place):
            if same_item(item, thing["payload"]["item"]["name"]):
                lock = thing["payload"].get("lock")
                what = (f"{thing['label']} is the {item} this job is about" if same_item(thing["label"], item)
                        else f"{thing['label']} holds the {item} this job is about")
                return (f" PROOF HERE: {what} — {_thing_phrase(thing['label'])} is in this very place; show it "
                        f"where it lies, and it becomes theirs through @interact {thing['label']}"
                        + (f" once the fiction opens it (locked: {_clip(lock, 100)})" if lock else "")
                        + ". Nobody sends them elsewhere for it or hands them a key to it.")
    except Exception as e:      # the proof line is never worth the turn
        logger.debug(f"proof-here clause skipped: {e}")
    return ""


# ----------------------------------------------------------------------
# What the player answers
# ----------------------------------------------------------------------

_QUOTED = re.compile(r"[\"“]([^\"”\n]*)[\"”]")
# A yes said to open a line or a sentence: "Alright. I'll talk to him."
_YES_START = re.compile(
    r"^\W*(?:alright|all\s+right|fine|deal|done|yes|yeah|yep|aye|sure|okay|ok|agreed|very\s+well"
    r"|you'?re\s+on|count\s+me\s+in|I'?m\s+in|consider\s+it\s+done|gladly|happy\s+to)\b"
    r"(?![-'’])(?!\s+(?:if|but\s+not|enough|thing\s+is|weather|work)\b)"
    # "Deal with it yourself", "Done with you", "Yeah, no" are no yes
    r"(?!\s+(?:with\s+(?:it|you|this|that|them|him|her)|for|here|talking)\b)(?!\s*,?\s*no\s*(?:[,.!;—–]|$))",
    re.IGNORECASE,
)
# A yes that names the job, good for a few replies after the pitch
_YES_JOB = re.compile(
    r"\bI'?ll\s+(?:do\s+it|do\s+(?:the|your|that)\s+(?:job|work|errand)|take\s+(?:it|the\s+job|the\s+work"
    r"|that\s+job|your\s+job|the\s+errand|you\s+up\s+on\s+(?:it|that))|handle\s+it|see\s+it\s+done"
    r"|get\s+it\s+done|look\s+into\s+it|take\s+a\s+look(?!\s+(?:around|at|about|inside|outside|over|through)))\b"
    r"|\bI\s+accept\b|\byou'?ve\s+got\s+(?:a\s+deal|yourself\s+a\s+deal|my\s+word)\b|\byou\s+have\s+my\s+word\b"
    r"|\b(?:it'?s|that'?s)\s+a\s+deal\b|\bwe\s+have\s+a\s+deal\b|\bcount\s+me\s+in\b|\bconsider\s+it\s+done\b"
    r"|\bI'?ll\s+(?:come|be)\s+back\s+(?:with|and\s+tell)\b|\bI'?ll\s+bring\s+you\s+(?:what|word|it|back)\b",
    re.IGNORECASE,
)
# "I'll talk to him" right after the pitch: the step's person by pronoun
_YES_PRONOUN = re.compile(
    r"\bI'?ll\s+(?:go\s+)?(?:ask|talk\s+to|speak\s+(?:to|with)|find|see|question|look\s+for|track\s+down"
    r"|check\s+(?:on|with)|go\s+to)\s+(?:him|her|them"
    r"|the\s+(?:[\w'’-]+\s+)?(?:boy|lad|girl|man|woman|keeper|hand|guard|captain))\b",
    re.IGNORECASE,
)
# No, not now, later, someone else — the offer is closed
_NO_START = re.compile(
    r"^\W*(?:(?:no|nope|nah|not\s+(?:now|tonight|yet|today|interested|me))\b(?![-'’])"
    r"(?!\s+(?:point|one|need|matter|doubt|wonder|time|sense|more|longer|reason|idea|offen[cs]e|way|sign"
    r"|use|harm|less|fear|telling|trouble|rush|hurry|chance|question|thanks\s+needed|sooner)\b)"
    # "After," / "Later." alone — never "After the gate closes, I'll find him"
    r"|(?:later|after|maybe\s+later|another\s+time|pass)\s*(?:[,.!;—–]|$)"
    # The plain refusals said alone ("No way." — never "No way through"):
    # missed, they left the offer open for play to start the job anyway
    r"|(?:no\s+way|never|absolutely\s+not|hell\s+no|not\s+a\s+chance|no\s+chance|no\s+deal|forget\s+it"
    r"|not\s+happening|nothing\s+doing|(?:yeah|yes|sure|okay|ok)\s*,?\s+no)\s*(?:[,.!;—–]|$))",
    re.IGNORECASE,
)
_NO_ANY = re.compile(
    r"\bnot\s+interested\b|\bI'?ll\s+pass\b|\bmaybe\s+(?:later|another\s+time)\b|\blater,?\s+maybe\b"
    r"|\bsome\s+other\s+time\b|\bI'?d\s+(?:rather|sooner)\s+not\b|\bnot\s+a\s+chance\b"
    r"|\bI'?ll\s+think\s+(?:about|on)\s+it\b|\bthink\s+it\s+over\b|\b(?:find|ask)\s+someone\s+else\b"
    r"|\bnot\s+my\s+(?:problem|business|concern|job|fight)\b|\bnot\s+for\s+me\b|\bno,?\s+thanks?\b"
    r"|\bno,?\s+thank\s+you\b|\bcount\s+me\s+out\b|\bI'?m\s+not\s+interested\b"
    r"|\bI'?m\s+not\s+(?:taking|doing)\b(?!\s+(?:it|this|that)\s+for\s+(?!you\b))"
    r"|\bI\s+(?:won'?t|can'?t|cannot)\s+(?:do\s+it|do\s+that|take\s+(?:it|that|the\s+job|your\s+job)|help\s+you)\b"
    r"|\b(?:not|never)\s+(?:taking|doing)\s+(?:it|that|your\s+job|the\s+job)\b(?!\s+for\s+(?!you\b))",
    re.IGNORECASE,
)
# Bargaining is not a yes: the giver answers it first
_HAGGLE = re.compile(
    r"\bmake\s+it\b|\bdouble\s+(?:it|that)\b|\bhalf\s+(?:now|up\s+front|in\s+advance|first)\b|\bup\s+front\b"
    r"|\bin\s+advance\b|\bmore\s+than\s+that\b|\bnot\s+(?:enough|for\s+(?:that|so\s+little))\b"
    r"|\bworth\s+more\b|\bsweeten\b|\braise\s+(?:it|that|the\s+price)\b",
    re.IGNORECASE,
)
# Setting out to do the step: a going/asking verb, then (past a few small
# words) the step's person or place — "find Sorrel", "head toward the
# stables", "make my way to the stable yard", never "ask Piper about Sorrel"
_GO_VERB = re.compile(
    r"(?:head\w*|go|goes|going|went|walk\w*|make|makes|made|making|find|finding|seek\w*|ask|asks|asking"
    r"|talk\w*|speak\w*|question\w*|visit\w*|track\w*|hunt\w*|look\w*|set|sets|setting|see|cross\w*"
    r"|return\w*)",
    re.IGNORECASE,
)
_GO_GAP = frozenset({"to", "toward", "towards", "for", "over", "on", "down", "up", "out", "the", "a", "at",
                     "with", "into", "across", "back", "off", "my", "way", "go", "see", "find", "ask",
                     "talk", "speak", "straight", "right", "now", "tonight", "first"})


def _player_words(player_input: str) -> str:
    text = str(player_input or "").strip()
    return "" if text.startswith("[") else text


def _segments(text: str) -> list[str]:
    """Each quoted line and each sentence of narration, trimmed."""
    quotes = [q.strip() for q in _QUOTED.findall(text)]
    rest = _QUOTED.sub(" ", text)
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", rest) if s.strip()]
    inner = [s.strip() for q in quotes for s in re.split(r"(?<=[.!?])\s+", q) if s.strip()]
    return inner + sentences


def _goes_to_step(text: str, step_words: set[str]) -> str:
    """The words that set out on the step ("find Sorrel"), or ""."""
    if not step_words:
        return ""
    raw = re.findall(r"[A-Za-z][A-Za-z'’]*", text or "")
    low = [w.lower().replace("'", "").replace("’", "") for w in raw]
    for i, word in enumerate(low):
        if not _GO_VERB.fullmatch(word):
            continue
        j = i + 1
        while j < len(low) and j - i <= 5 and low[j] in _GO_GAP:
            j += 1
        if j < len(low) and _says_any(low[j], step_words):
            return " ".join(raw[i:j + 1])
    return ""


def _says_any(word: str, names: Iterable[str]) -> bool:
    return any(word == n or (word.startswith(n) and len(word) - len(n) <= 2) for n in names)


# The player's own hand closing on what the giver held out: "I take the
# vial", "I pocket the slip" — the thing named in the giver's pitch
_TAKES = re.compile(
    r"\bI\s+(?:\w+ly\s+)?(?:take|accept|pocket|tuck|pick\s+up|grab|close\s+my\s+(?:hand|fingers)\s+(?:around|over))"
    r"\s+((?:[\w'’-]+[ \t]*){1,4})",
    re.IGNORECASE,
)


_NOT_TAKEN = frozenset({"moment", "breath", "look", "step", "seat", "while", "second", "minute", "chance", "time",
                        "note", "word", "glance", "pause", "turn", "measure", "stock", "care", "heart", "point"})


def _takes_the_offer(text: str, offered: set[str]) -> str:
    """The words that take a thing the giver's pitch named, or "" ("I take
    a long breath" takes nothing)."""
    if not offered:
        return ""
    narration = _QUOTED.sub(" ", text or "")
    for m in _TAKES.finditer(narration):
        words = [w.lower().replace("'", "").replace("’", "") for w in m.group(1).split()]
        if any(w in _NOT_TAKEN for w in words):
            continue
        if any(_says_any(w, offered) for w in words if len(w) >= 4):
            return m.group(0)
    return ""


def read_answer(player_input: str, terms: dict[str, Any], *, age: int = 0,
                fresh: bool | None = None) -> tuple[str | None, str]:
    """("accept" | "decline" | "haggle" | None, the words that decided it).

    `age`: replies since the pitch; `fresh`: the giver spoke in the last
    reply (default: the pitch was the last reply). A bare yes ("Alright",
    "I'll talk to him") answers only a giver still talking — fleet7-bram
    asked Piper a question at T4 and said "Alright. I'll talk to him" at
    T5; a yes that names the job, or setting out to do its first step,
    counts for OFFER_WINDOW replies. A no outranks nothing it shares a line
    with, and "No point asking if I can't get through the gate" is no no."""
    text = _player_words(player_input)
    if fresh is None:
        fresh = age == 0
    if not text or age > OFFER_WINDOW:
        return None, ""
    segments = _segments(text)
    no = next((s for s in segments if _NO_START.search(s)), "") or \
        (lambda m: m.group(0) if m else "")(_NO_ANY.search(text))
    haggle = (lambda m: m.group(0) if m else "")(_HAGGLE.search(text))
    # Naming a bigger price is asking for more; the offered price said back
    # is a repeat ("Ten coin and a tincture," I repeat — fleet7-tamsin T4),
    # and two coins pressed on someone is a tip
    said = amounts(text)
    if terms.get("coin") and any(a > max(terms["coin"]) for a in said):
        haggle = haggle or f"{max(said)} coin"
    yes = ""
    if fresh:
        yes = next((s for s in segments if _YES_START.search(s)), "") or \
            (lambda m: m.group(0) if m else "")(_YES_PRONOUN.search(text))
    yes = yes or (lambda m: m.group(0) if m else "")(_YES_JOB.search(text))
    if fresh and not yes:
        yes = _takes_the_offer(text, terms.get("offered") or set())
    # A question sets out on nothing: "Find Sorrel? Why would I?", "Where
    # do I find Sorrel?" — the giver answers it first
    step = next((w for s in segments if not s.rstrip().endswith("?")
                 for w in [_goes_to_step(s, terms.get("step_words") or set())] if w), "")
    if no and not (yes or step):
        return "decline", no
    if haggle:
        return "haggle", haggle
    if no:
        return None, ""                        # a yes and a no in one breath: the DM reads it
    if yes or step:
        return "accept", (yes or step)
    return None, ""


# ----------------------------------------------------------------------
# The engine paths
# ----------------------------------------------------------------------

def _open_quests(session, player_id: str, giver_ids: set[str], started: Iterable[str]) -> list:
    if not giver_ids:
        return []
    try:
        from src.services.campaign import later_act_quest_ids

        kept = later_act_quest_ids(session, player_id)
    except Exception:        # no spine, or an odd one: every job counts
        kept = set()
    skip = set(started or ()) | set(kept)
    return [q for q in session.query(Quest).filter(
        Quest.status == QuestStatus.NOT_STARTED, Quest.assigned_by_npc_id.in_(list(giver_ids))).all()
        if (q.title or "").strip() and q.id not in skip]


def note_reply(player_id: str | None, spoken: list[dict[str, Any]] | None, *, context: str = "",
               started: Iterable[str] = ()) -> list[str]:
    """After a reply's dialogue is filed: count the reply, and remember each
    job its giver's speaker lines pitched (`pitched`: its pay, or its work
    asked for). An [OFFER THIS REPLY] line (`context`, the reply's context)
    no longer makes whatever the giver said the offer: fleet9-bram T3,
    Piper pointed at Sorrel. A job started this reply (`started`) was not
    merely offered. What play did before the pitch rides along as its credit.
    Returns the titles offered."""
    if not player_id:
        return []
    with _lock:
        state = _state(player_id)
        state["reply"] += 1
        reply = state["reply"]
    lines: dict[str, list[str]] = {}
    for s in spoken or []:
        if isinstance(s, dict) and s.get("npc_id") and str(s.get("text") or "").strip():
            lines.setdefault(s["npc_id"], []).append(str(s["text"]))
    if not lines:
        return []
    offered: list[str] = []
    try:
        with get_session() as session:
            # A giver still talking keeps a bare yes in reach, and has
            # answered a haggle: the offer is open again, at their word
            for quest_id, offer in _live(state):
                quest = session.get(Quest, quest_id)
                if quest is not None and quest.assigned_by_npc_id in lines:
                    with _lock:
                        offer["heard"] = reply
                        offer["lines"] = "\n".join(lines[quest.assigned_by_npc_id])
                        if offer["status"] == "haggling":
                            offer["status"] = "open"
            for quest in _open_quests(session, player_id, set(lines), started):
                said = "\n".join(lines.get(quest.assigned_by_npc_id) or [])
                if not pitched(session, quest, said):
                    continue
                with _lock:
                    offers = state["offers"]
                    old = offers.get(quest.id) or {}
                    # Work play did before anyone offered it (`note_play`,
                    # `note_witnessed`) is credited when it starts
                    credit = list(dict.fromkeys(list(old.get("credit") or [])
                                                + state.setdefault("worked", {}).pop(quest.id, [])))
                    offers[quest.id] = {"at": reply, "heard": reply, "lines": said,
                                        "status": "open" if old.get("status") != "accepted" else "accepted",
                                        "met": list(old.get("met") or []), "credit": credit, "told": 0,
                                        "said": old.get("said", "")}
                offered.append(quest.title)
    except Exception as e:      # an offer missed only costs the old behaviour
        logger.warning(f"job offers not noted: {e}")
    if offered:
        logger.info(f"Job offered: {', '.join(offered)}")
    return offered


def _live(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(q, o) for q, o in state["offers"].items()
            if o["status"] in ("open", "accepted", "haggling") and state["reply"] - o["at"] <= OFFER_KEEP]


def start_directive(player_id: str | None, player_input: str, *, addressed_npc_id: str | None = None) -> str:
    """The [START THIS JOB] line(s) for this reply, or "". Reads the
    player's words against each live offer (`read_answer`): a no closes it,
    a yes (or setting out on its first step) binds the start — said at most
    START_LINES times, then the engine starts it once play does a step.

    `addressed_npc_id`: the person the player is talking to (their talk
    chip). A bare yes or no said to someone else answers THEM — "Yes, sir"
    to the Captain is not a yes to the herbalist's job beside him."""
    if not player_id or not _player_words(player_input):
        return ""
    # A howl lived through last reply is play on a pitched job too (a turn
    # that walks off never asks `credited`)
    note_witnessed(player_id)
    with _lock:
        state = _state(player_id)
        live = _live(state)
    if not live:
        return ""
    out: list[str] = []
    try:
        with get_session() as session:
            player = session.get(Player, player_id)
            for quest_id, offer in live:
                quest = session.get(Quest, quest_id)
                if quest is None or quest.status != QuestStatus.NOT_STARTED:
                    continue
                terms = dict(offer_terms(session, quest), offered=_distinct(offer.get("lines") or ""))
                elsewhere = bool(addressed_npc_id) and addressed_npc_id != quest.assigned_by_npc_id
                verdict, words = read_answer(player_input, terms, age=state["reply"] - offer["at"],
                                             fresh=state["reply"] == offer.get("heard", offer["at"])
                                             and not elsewhere)
                if verdict == "decline" and elsewhere:
                    verdict = None
                with _lock:
                    if verdict == "decline":
                        offer["status"] = "declined"
                        logger.info(f"Job declined: {quest.title} ({words!r})")
                        continue
                    if verdict == "haggle":
                        # Held until the giver answers the price (`note_reply`)
                        offer["status"] = "haggling"
                        continue
                    if verdict == "accept" and offer["status"] != "accepted":
                        offer["status"], offer["said"] = "accepted", words
                        logger.info(f"Job taken: {quest.title} ({words!r})")
                    if offer["status"] != "accepted" or offer["told"] >= START_LINES:
                        continue
                    offer["told"] += 1
                    again = offer["told"] > 1
                said = " ".join(str(offer.get("said") or "").split())[:90]
                how = f"they said \"{said}\"" if said else "they took it"
                out.append(
                    f"[START THIS JOB: @quest-start {quest.title} — the player took it "
                    f"{'and it is still not on their quest log' if again else 'last turn'} ({how}). "
                    "Write that line first in this reply; the pay stands as offered. Never a second "
                    "pitch of it." + _proof_here(session, quest, player,
                                                 skip=list(offer.get("credit") or []) + list(offer["met"]))
                    + "]")
    except Exception as e:      # never let the bookkeeping cost the turn
        logger.warning(f"start-this-job line unavailable: {e}")
    return "\n".join(out)


def note_play(player_id: str | None, matcher: Callable[[dict[str, Any]], bool]) -> list[str]:
    """Play did something (talked to someone, arrived, picked a thing up):
    remember which steps of each live, not-started offer it did. A step
    that closes a loop (back to the giver) waits for the ones before it.
    A job nobody has offered yet keeps what play did as credit (`_note_unoffered`).
    Returns the step ids newly met."""
    if not player_id:
        return []
    from src.services.objectives import _is_return_step, normalize_objectives

    with _lock:
        state = _state(player_id)
        live = [(q, o) for q, o in _live(state) if o["status"] != "haggling"]
        # Pitched and not over, or turned down: not "nobody offered it"
        spoken_for = {q for q, _ in _live(state)} | {q for q, o in state["offers"].items()
                                                     if o["status"] == "declined"}
    met_now: list[str] = []
    with get_session() as session:
        met_now += _note_unoffered(session, player_id, matcher, spoken_for)
        for quest_id, offer in live:
            quest = session.get(Quest, quest_id)
            if quest is None or quest.status != QuestStatus.NOT_STARTED:
                continue
            objectives = normalize_objectives(quest.objectives)
            before = set(offer["met"]) | set(offer.get("credit") or [])
            for i, obj in enumerate(objectives):
                if obj["done"] or obj["kind"] == "custom" or obj["id"] in before:
                    continue
                # A first step aimed at the giver ("Talk to Captain Coldiron at
                # the Watch House", given by Coldiron: forge_fresh) is never
                # play taking the job — talking to the giver IS the offer, and
                # a "what's the pay again?" would start it
                if _is_return_step(obj, objectives[:i], quest.assigned_by_npc_id) and not (
                        i and all(p["done"] or p["id"] in before for p in objectives[:i])):
                    continue
                try:
                    hit = matcher(obj)
                except Exception:
                    hit = False
                if hit:
                    with _lock:
                        offer["met"].append(obj["id"])
                    met_now.append(obj["id"])
                    logger.info(f"Offered job worked before it started: {obj['text']!r} ({quest.title})")
    return met_now


def _unoffered(session, player_id: str, spoken_for: Iterable[str]) -> list:
    """Authored jobs not started, whose giver lives, that nobody has pitched
    (or whose pitch is long forgotten) — never one a later act keeps."""
    try:
        from src.services.campaign import later_act_quest_ids

        kept = later_act_quest_ids(session, player_id)
    except Exception:        # no spine, or an odd one: every job counts
        kept = set()
    skip = set(spoken_for or ()) | set(kept)
    out = []
    for quest in session.query(Quest).filter(Quest.status == QuestStatus.NOT_STARTED,
                                             Quest.assigned_by_npc_id.isnot(None)).all():
        if quest.id in skip or not (quest.title or "").strip():
            continue
        giver = session.get(NPC, quest.assigned_by_npc_id)
        if giver is not None and giver.status == "alive":
            out.append(quest)
    return out


# A talk_to step of a job nobody offered is credit only when the talk was
# about the job: any word with its target is no "Ask Sorrel what the yard
# hears after dark". Replayed over the 109 replies of every fleet run where
# the player spoke with the stable hand: any talk was credit, so
# fleet-tamsin T2, fleet2-wenna T13, fleet4-wenna T15 and fleet6-tamsin T4
# (wolves, tracks, the ash-cloaked man's face) would have had the giver say
# "you already asked him". A word of the job's title said by either of
# them, or two of the step's own words in the player's question, keeps
# those out and takes fleet9-bram T6-T7 ("is it the howling itself") and
# fleet9-tamsin T8 ("you hear things out here after dark")
_TALK_PLAIN = frozenset({
    "about", "after", "again", "know", "knows", "tell", "tells", "said", "says", "talk", "speak", "learn", "what",
    "when", "where", "which", "whom", "whose", "with", "from", "them", "they", "their", "this", "that", "have",
    "find", "your", "been", "were", "will", "would", "could", "should", "there", "here", "some", "anything",
    "something", "everything", "nothing", "everyone", "anyone", "someone", "back", "into", "over",
})
_LAST_EXCHANGE = re.compile(r"^-?\s*(Day\s+\d+\s+\d{1,2}:\d{2})\s*·\s*([^:]{1,60}):\s*\"?(.*?)\"?\s*$")


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _talked_about(session, player_id: str, quest, obj: dict[str, Any]) -> bool:
    """Was the player's last exchange with a talk_to step's target about the
    job — a word of its title said by either, or two of the step's own words
    in the player's question (never the target's or giver's names, trades
    or places)? The exchange is the one just filed (dialogue files the talk,
    then ticks); narration around the player's quotes is not what they asked
    ("low enough that only he hears it"). True when nothing can say."""
    from src.services import memory_files

    target = session.get(NPC, obj.get("target_id")) if obj.get("target_id") else None
    giver = session.get(NPC, quest.assigned_by_npc_id) if quest.assigned_by_npc_id else None
    if target is None:
        return True
    ident: set[str] = set()
    for npc in (target, giver):
        if npc is not None:
            ident |= set(_words(npc.name)) | set(_words(npc.profession or ""))
            place = session.get(Location, npc.current_location_id) if npc.current_location_id else None
            ident |= set(_words(place.name)) if place is not None else set()
    title = {w for w in _distinct(quest.title or "") if w not in ident}
    step = {_stem(w) for w in _words(obj.get("text") or "") if len(w) >= 4 and w not in _TALK_PLAIN
            and w not in ident}
    if not (title or len(step) >= 2):
        return True
    lines = memory_files.memory_parts(player_id, target.id)["recent"]
    parsed = [m for m in (_LAST_EXCHANGE.match(str(line).strip()) for line in lines) if m]
    if not parsed:
        return True
    them = memory_files.speaker_label(target.name)
    everyone: list[str] = []
    asked: list[str] = []
    for m in parsed:
        if m.group(1) != parsed[-1].group(1):
            continue
        quotes = _QUOTED.findall(m.group(3))
        words = _words(" ".join(quotes) if quotes else m.group(3))
        everyone += words
        if m.group(2).strip() != them:
            asked += words

    def said(word: str, words: Iterable[str]) -> bool:      # "howl" in "howling", "hear" in "hears"
        return any(w.startswith(word) and len(w) - len(word) <= 3 for w in words)

    return (any(said(w, everyone) for w in title)
            or sum(1 for w in step if said(w, map(_stem, asked))) >= 2)


def _note_unoffered(session, player_id: str, matcher: Callable[[dict[str, Any]], bool],
                    spoken_for: Iterable[str]) -> list[str]:
    """`note_play` for jobs nobody offered: the steps play did become credit.
    Never a step back to the giver (the report IS the offer conversation,
    and a job that arrived finished would pay for one chat), never a custom
    one (`note_witnessed` reads those), never a talk about something else
    (`_talked_about`)."""
    from src.services.objectives import _is_return_step, normalize_objectives

    met_now: list[str] = []
    for quest in _unoffered(session, player_id, spoken_for):
        objectives = normalize_objectives(quest.objectives)
        with _lock:
            done = set(_state(player_id).setdefault("worked", {}).get(quest.id) or [])
        for i, obj in enumerate(objectives):
            if obj["done"] or obj["kind"] == "custom" or obj["id"] in done:
                continue
            if _is_return_step(obj, objectives[:i], quest.assigned_by_npc_id):
                continue
            try:
                hit = matcher(obj)
            except Exception:
                hit = False
            if hit and obj["kind"] == "talk_to":
                try:
                    hit = _talked_about(session, player_id, quest, obj)
                except Exception as e:      # unreadable: credited, as before
                    logger.debug(f"talk subject unreadable for {quest.title}: {e}")
            if hit:
                with _lock:
                    _state(player_id)["worked"].setdefault(quest.id, []).append(obj["id"])
                met_now.append(obj["id"])
                logger.info(f"Job nobody offered worked anyway: {obj['text']!r} ({quest.title})")
    return met_now


# A "stand out the next howl" step: the player lived through the thing. A
# custom step only the DM ticks; for a job not started yet it was never
# ticked (fleet9-bram T9 stood the 20:00 howl at the fence for Piper's job
# nobody offered). Read from the events the replies told, where the player was
VIGIL_HOURS = 6
_WITNESS = re.compile(
    r"\b(?:stand(?:s|ing)?\s+(?:out|watch|through)|keep(?:s|ing)?\s+(?:watch|vigil)|watch(?:es|ing)?\s+for"
    r"|wait(?:s|ing)?\s+(?:for|out|through)|witness\w*|listen(?:s|ing)?\s+(?:for|to)|hear|time\s+(?:it|them|the))\b",
    re.IGNORECASE,
)
# An event's name words that name no particular thing ("The Evening Howl" is
# the howl). Replayed over the 732 custom steps x 58 events on disk: the
# engine's "Death of <name>" met "Listen to … his son's death", and "Second
# Warning at …" a "private warning" — so those words name nothing either
_TIMELY = frozenset({"evening", "morning", "night", "nightly", "dawn", "dusk", "noon", "midnight", "today",
                     "tonight", "daily", "first", "last", "next", "arrives", "arrive", "comes", "short", "late",
                     "early", "great", "little", "old", "new", "death", "dies", "slain", "killed", "warning",
                     "second", "third", "fourth", "another", "investigation", "news", "report"})


def _event_words(event) -> set[str]:
    return {w for w in _distinct(getattr(event, "name", "") or "") if w not in _TIMELY}


def vigil_matches(step_text: str, event) -> bool:
    """Is this custom step ("Stand out the next howl and mark where it
    rises") the living-through of this event ("The Evening Howl")? A
    witnessing verb in the step, and a word of the event's name in it."""
    if not _WITNESS.search(step_text or ""):
        return False
    words = _words(step_text)
    return any(_says(w, words) for w in _event_words(event))


def _places_of(session, player_id: str) -> set[str]:
    """The player's place and every place it sits inside (the howl over the
    square is heard in the yard off it) — world_tick's own reading."""
    player = session.get(Player, player_id)
    places: set[str] = set()
    location_id = player.current_location_id if player is not None else None
    while location_id and location_id not in places:
        places.add(location_id)
        location = session.get(Location, location_id)
        location_id = location.parent_id if location is not None else None
    return places


def note_witnessed(player_id: str | None) -> list[str]:
    """Events told to the player since this was last asked, that happened
    where they are, within VIGIL_HOURS: each meets the matching custom step
    (`vigil_matches`) of a job not started — `met` on a live offer (play
    after the pitch: the engine starts it), credit on one nobody offered.
    Returns the step ids newly met."""
    if not player_id:
        return []
    from src.models import Event, WorldClock
    from src.services.objectives import normalize_objectives

    met_now: list[str] = []
    try:
        with get_session() as session:
            # An event the player never hears of is marked "narrated" the
            # moment it fires (world_tick): nobody stood out what was hidden
            told = session.query(Event).filter(Event.occurred_day.isnot(None),
                                               Event.player_visible == True,  # noqa: E712
                                               Event.narrated_to_player == True).all()  # noqa: E712
            with _lock:
                state = _state(player_id)
                seen = state.setdefault("events", set())
                fresh = [e for e in told if e.id not in seen]
                seen.update(e.id for e in fresh)
            clock = session.query(WorldClock).first()
            if clock is not None:
                now = (clock.day * 24 + (clock.hour or 0)) * 60 + (clock.minute or 0)
                fresh = [e for e in fresh
                         if now - (e.occurred_day * 24 + (e.occurred_hour or 0)) * 60 <= VIGIL_HOURS * 60]
            places = _places_of(session, player_id) if fresh else set()
            fresh = [e for e in fresh if places & set(e.locations_involved or [])]
            if not fresh:
                return []
            with _lock:
                live = {q: o for q, o in _live(state) if o["status"] != "haggling"}
                spoken_for = {q for q, _ in _live(state)} | {q for q, o in state["offers"].items()
                                                             if o["status"] == "declined"}
            quests = [session.get(Quest, q) for q in live] + _unoffered(session, player_id, spoken_for)
            for quest in quests:
                if quest is None or quest.status != QuestStatus.NOT_STARTED:
                    continue
                for obj in normalize_objectives(quest.objectives):
                    if obj["done"] or obj["kind"] != "custom":
                        continue
                    if not any(vigil_matches(obj.get("text") or "", e) for e in fresh):
                        continue
                    with _lock:
                        if quest.id in live:
                            bucket = live[quest.id]["met"]
                            if obj["id"] in bucket or obj["id"] in (live[quest.id].get("credit") or []):
                                continue
                        else:
                            bucket = state.setdefault("worked", {}).setdefault(quest.id, [])
                            if obj["id"] in bucket:
                                continue
                        bucket.append(obj["id"])
                    met_now.append(obj["id"])
                    logger.info(f"Lived through for a job not started: {obj['text']!r} ({quest.title})")
    except Exception as e:      # a vigil missed only costs the old behaviour
        logger.warning(f"witnessed events not read: {e}")
    return met_now


def credited(player_id: str | None) -> dict[str, list[dict[str, str]]]:
    """{quest id: [{"id", "text"} of each step play did]} for each job nobody
    offered that play already worked — the giver's next appearance offers
    it, crediting them (dm_orchestrator.offer_directive). Reads new vigils
    first."""
    if not player_id:
        return {}
    from src.services.objectives import normalize_objectives

    note_witnessed(player_id)
    with _lock:
        state = _state(player_id)
        worked = {q: list(ids) for q, ids in (state.get("worked") or {}).items() if ids}
        spoken_for = {q for q, _ in _live(state)}
    out: dict[str, list[dict[str, str]]] = {}
    if not worked:
        return out
    with get_session() as session:
        for quest_id, ids in worked.items():
            quest = session.get(Quest, quest_id)
            if quest is None or quest.status != QuestStatus.NOT_STARTED or quest_id in spoken_for:
                continue
            out[quest_id] = [{"id": o["id"], "text": o["text"]}
                             for o in normalize_objectives(quest.objectives) if o["id"] in ids]
    return out


def backfill(player_id: str | None, quest_id: str) -> list[dict[str, Any]]:
    """The job just started: tick the steps play did since it was offered
    (`note_play`) and the ones it did before anyone offered it (its credit),
    with the same toasts and ledger lines as a live tick.
    Returns the objective records ([] when nothing was waiting)."""
    if not player_id:
        return []
    from src.services import objectives as objectives_mod

    with _lock:
        state = _state(player_id)
        offer = state["offers"].get(quest_id)
        worked = (state.get("worked") or {}).pop(quest_id, [])
        met = list(dict.fromkeys((list(offer.get("credit") or []) + list(offer["met"]) if offer else [])
                                 + list(worked)))
    if not met:
        if offer:
            offer["status"] = "started"
        return []
    records: list[dict[str, Any]] = []
    try:
        with get_session() as session:
            player = session.get(Player, player_id)
            quest = session.get(Quest, quest_id)
            if player is None or quest is None or quest.status != QuestStatus.ACTIVE:
                return []
            # One at a time, in the job's own order: a step that waits on an
            # earlier one sees it done
            order = [o["id"] for o in objectives_mod.normalize_objectives(quest.objectives)]
            met.sort(key=lambda oid: order.index(oid) if oid in order else len(order))
            for objective_id in met:
                records += objectives_mod.tick_in_session(
                    session, player, lambda o, oid=objective_id: o["id"] == oid, quest_id=quest_id)
            if records:
                session.commit()
    except Exception as e:
        logger.warning(f"offered job's earlier steps not credited for {quest_id}: {e}")
        return []
    with _lock:
        if offer:
            offer["met"], offer["credit"], offer["status"] = [], [], "started"
    objectives_mod.publish(player_id, records)
    return records


def settle(player_id: str | None) -> list[dict[str, Any]]:
    """End of a reply: a live offer play already worked (a step met since
    the pitch) and still nobody started is started by the engine, through
    the path @quest-start takes, and credited (`backfill`). Returns one row
    per job: {"quest_id", "title", "giver", "steps", "note"} — `note` is
    the line for the DM's next context."""
    if not player_id:
        return []
    from src.services import objectives as objectives_mod
    from src.services.commands import _sync_quest_lists
    from src.tools.world_write.quests import activate_quest

    with _lock:
        ready = [(q, o) for q, o in _live(_state(player_id)) if o["met"] and o["status"] != "haggling"]
    out: list[dict[str, Any]] = []
    for quest_id, offer in ready:
        with get_session() as session:
            quest = session.get(Quest, quest_id)
            if quest is None or quest.status != QuestStatus.NOT_STARTED:
                continue
            giver = session.get(NPC, quest.assigned_by_npc_id) if quest.assigned_by_npc_id else None
            giver_name = giver.name if giver is not None else "Its giver"
        result = activate_quest(quest_id=quest_id)
        if not isinstance(result, dict) or result.get("error"):
            logger.warning(f"offered job not started by the engine: {result}")
            continue
        _sync_quest_lists(player_id, quest_id, QuestStatus.ACTIVE)
        objectives_mod.queue_event(player_id, {"type": "quest_activated", "quest_id": quest_id,
                                               "title": result.get("title"), "rewards_applied": []})
        records = backfill(player_id, quest_id)
        steps = [rec["text"] for rec in records]
        did = "; ".join(f"'{s}'" for s in steps) or "its first step"
        out.append({
            "quest_id": quest_id, "title": result.get("title"), "giver": giver_name, "steps": steps,
            "note": (f"{giver_name} offered \"{result.get('title')}\" and the player went and did it before "
                     f"anyone wrote @quest-start ({did}), so the engine started the job and credited "
                     f"{'that step' if len(steps) <= 1 else 'those steps'}. It is on their quest log now: "
                     "never write @quest-start for it, never pitch it again — carry on from where they "
                     "are; the pay comes with @quest-done when the rest is done."),
        })
        logger.info(f"Job started by the engine: {result.get('title')} ({did})")
    return out

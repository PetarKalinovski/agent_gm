"""Recurring tics: the same prop or gesture in reply after reply.

fleet9-tamsin: Piper's basket in 16 of 21 replies ("basket forgotten on her
hip" T11, "basket forgotten against her hip" T14, "sweetroot forgotten
between her teeth" T15, "sweetroot forgotten" T16). The dossier hands the
DM each person's looks every turn, and nothing said which of them it had
just used, so the supporting cast read as one voice with one prop each.

After every reply (`note_reply`) each person gets what the prose said of
them — never their own lines: a pay or a clue said twice is the "already
told" line's business (scene_brief), and the words must stay right. A
person's tic is

- a detail of their own looks (a word of `description_physical` that is no
  everyday body word: "basket", "halter", "scar") in a sentence naming
  them, in SIGNATURE_REPLIES of the last WINDOW replies, or
- a run of 2-3 content words in a sentence about them (stopwords skipped:
  "basket forgotten … hip") in MIN_REPLIES of them — a run that shows them:
  one of their looks words, a body word of theirs ("her hip", "his
  mouth"), or a gesture ("forgotten", "clutched"). Scenery ("barn door")
  and errands ("gate road") are no tic of anyone's.

The brief (`brief_lines`) names them for the people present: "Already
used — vary". A looks word counts by its compound's parts ("straw" of
"straw-colored"), and back to back too (in SCENE_REPEATS of the last
SCENE_WINDOW replies). The narration's own habits join the line
(`scene_phrases`): a simile, a look toward the same thing, the same four
words — never the plot's own things (quest items, the Things here and what
they read, the pack). Replayed over every transcript: about one reply in
eight gets a narration habit ("he shifts his weight", "warm against your
ribs", "glances toward the north wall"). In memory, per player (like
job_offers): a restart costs a few replies of memory, nothing else. Never
raises.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

WINDOW = 6               # replies remembered per player
MIN_REPLIES = 2          # a gesture back in this many of them is a tic
SIGNATURE_REPLIES = 3    # ...a single detail of their looks, in this many
MAX_PHRASES = 3          # per person in the brief
MAX_PEOPLE = 4

_WORD = re.compile(r"[a-z][a-z'-]*")
_SENTENCE = re.compile(r"(?<=[.!?…])\s+|\n+")
_GAP = 2                 # stopwords a run may skip between two of its words

# Words that carry no detail: grammar, pronouns, and the everyday verbs of
# looking and talking every reply has
_STOP = frozenset("""
a an the and or but nor so yet if then than that this these those there here where when while as at by for from in
into onto on off of to up down out over under with without within about above below across after again against
along among around before behind beneath beside besides between beyond during except inside like near past since
through throughout toward towards upon via is are was were be been being am do does did doing done have has had
having will would shall should can could may might must not no n't don't doesn't didn't won't can't isn't
aren't wasn't weren't he him his she her hers they them their theirs it its itself himself herself themselves you
your yours yourself we us our i me my mine who whom whose which what why how all any both each every few more most
other some such only own same too very just still already even also now once ever never always back away long one
two three first last next another something someone anything anyone nothing nobody everything everyone else
enough much many little less least well quite rather almost nearly really maybe perhaps says said say saying asks
asked ask tells told tell looks look looked looking glances glanced glance turns turned turn nods nodded nod gives
gave give takes took take makes made make gets got get goes went go going comes came come keeps kept keep lets let
seems seemed seem holds held hold sets set puts put stands stood stand sits sat sit moment way thing things bit
lot kind sort side you'll you're you've you'd he's she's it's they're they've they'll they'd he'll she'll he'd
she'd i'm i'll i've i'd we're we'll that's there's what's who's let's
""".split())
# Body and look words everyone has: never a signature of one person
_EVERYDAY = frozenset("""
eyes eye hands hand face faces hair voice head shoulder shoulders fingers finger arms arm mouth lips lip brow
chin jaw gaze skin body feet foot legs leg knees knee back chest neck nose teeth tooth cheek cheeks expression
young old middle-aged man woman boy girl figure person people stranger bright dark small large tall short broad
thin lean wiry heavy weathered worn plain simple quick slow warm cold soft hard rough smooth friendly gray grey
black white red green brown blue
""".split())
# ...which, possessed ("her hip", "his mouth"), show the person
_BODY = frozenset("""
eye eyes hand hands hip hips face hair voice head shoulder shoulders finger fingers fingertip fingertips arm arms
mouth lip lips brow brows eyebrow chin jaw gaze skin feet foot leg legs knee knees chest neck nose teeth tooth
cheek cheeks fist fists thumb thumbs wrist wrists elbow elbows ear ears side spine
""".split())
_POSSESSIVE = frozenset({"his", "her", "their"})
# -ing/-en/-ed words that are no gesture
_NOT_GESTURE = frozenset({"evening", "morning", "ceiling", "building", "clothing", "wedding", "kitchen", "garden",
                          "golden", "wooden", "sudden", "children", "often", "linen", "dozen", "hundred",
                          "nothing", "something", "everything", "anything", "during", "king", "thing", "string",
                          "ring", "spring", "bring", "sing", "wing", "seed", "need", "speed", "bed", "red",
                          "shed", "led", "fed"})

_lock = threading.RLock()
# player id -> deque of replies; a reply is {npc id: {"grams": {key: surface}, "looks": {word}}}
_STATE: dict[str, deque] = {}
# The narration's own habits, nobody's looks: fleet10-wenna's eyes drifting
# north to the hills in 9 replies, fleet10-bram's "watches you the way a
# soldier watches an officer" at T14 and T15 — a simile, a look toward the
# same thing, or the same four words, back within SCENE_WINDOW replies
SCENE_WINDOW = 3
SCENE_REPEATS = 2
MAX_SCENE = 3
# player id -> deque of replies; a reply is {key: surface}
_SCENE: dict[str, deque] = {}


def reset(player_id: str | None = None) -> None:
    """Forget what was said (one player's, or everyone's: tests, world switches)."""
    with _lock:
        if player_id is None:
            _STATE.clear()
            _SCENE.clear()
        else:
            _STATE.pop(player_id, None)
            _SCENE.pop(player_id, None)


def _words(text: str) -> list[tuple[str, int, int]]:
    """(word, start, end) for each word of `text`, lower case."""
    flat = (text or "").replace("’", "'").lower()
    return [(m.group(0).strip("'-"), m.start(), m.end()) for m in _WORD.finditer(flat)]


def _key(word: str) -> str:
    """One form per word: "baskets" and "basket's" are "basket"."""
    w = word[:-2] if word.endswith("'s") else word
    return w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w


# A compound's parts are details of their own: "straw-colored hair" is
# "straw" (fleet10-wenna: Sorrel's straw hair in T5, T11, T12, T19 and T20
# never matched — `_WORD` kept "straw-colored" as one word). Parts that say
# nothing alone are dropped ("colored", "leather", "haired")
_GENERIC_PARTS = frozenset({
    "colored", "coloured", "haired", "eyed", "faced", "skinned", "shouldered", "built", "leather", "like",
    "looking", "worn", "made", "toned", "tinged", "streaked", "stained", "knuckled", "fingered", "handed",
    "legged", "armed", "booted", "brimmed", "rimmed", "edged", "trimmed", "lined", "sleeved", "necked",
    "backed", "length", "sized", "shaped", "colour", "color",
})


def _forms(word: str) -> list[str]:
    """A word and, for a compound, its telling parts: "straw-colored" ->
    ["straw-colored", "straw"]."""
    if "-" not in word:
        return [word]
    parts = [p for p in word.split("-") if len(p) >= 3 and p not in _STOP and p not in _EVERYDAY
             and p not in _GENERIC_PARTS]
    return [word] + parts


def _gesture(word: str) -> bool:
    """A word that is something done: "forgotten", "clutched", "thumping"."""
    return len(word) >= 6 and word.endswith(("ed", "en", "ing")) and word not in _NOT_GESTURE


def phrases(text: str, skip: frozenset[str] | set[str] = frozenset(),
            looks: frozenset[str] | set[str] = frozenset()) -> dict[tuple[str, ...], tuple[str, bool]]:
    """{key: (surface, strong)} for each run of 2-3 content words in `text`
    that shows someone: "basket forgotten against her hip" -> ("basket",
    "forgotten", "hip"), its words no more than _GAP stopwords apart, one of
    them a looks word of theirs (`looks`) or a possessed body word (strong),
    or a gesture alone ("lathered horse" is scenery as often as not: weak).
    `skip` are words that never count (names, places)."""
    out: dict[tuple[str, ...], tuple[str, bool]] = {}
    for sentence in _SENTENCE.split(text or ""):
        tokens = _words(sentence)
        content = []
        for i, (w, s, e) in enumerate(tokens):
            if len(w) < 3 or w in _STOP or w in skip or _key(w) in skip:
                continue
            strong = any(_key(f) in looks for f in _forms(w)) or (
                w in _BODY and i > 0 and tokens[i - 1][0] in _POSSESSIVE)
            content.append((i, w, s, e, strong, strong or _gesture(w)))
        for n in (2, 3):
            for j in range(len(content) - n + 1):
                run = content[j:j + n]
                if any(b[0] - a[0] - 1 > _GAP for a, b in zip(run, run[1:])) or not any(r[5] for r in run):
                    continue
                key = tuple(_key(r[1]) for r in run)
                if len(set(key)) < n:
                    continue
                out[key] = (sentence[run[0][2]:run[-1][3]], any(r[4] for r in run))
    return out


def looks_words(npc: Any) -> set[str]:
    """The words of someone's looks that are theirs alone to wear: "basket",
    "sweetroot", "halter", "straw" of "straw-colored" — never "eyes" or
    "young". A @new-npc's `looks:` is their description, so the people the
    DM made up are read the same (fleet10-wenna: the drover's glove)."""
    out: set[str] = set()
    for w, _, _ in _words(str(getattr(npc, "description_physical", "") or "")):
        for f in _forms(w):
            if len(f) >= (4 if f == w else 3) and f not in _STOP and f not in _EVERYDAY:
                out.add(_key(f))
    return out


class Person:
    """What the prose calls someone (and the words of it, which are never a
    detail), the pronoun that stands for them, and their looks' own words."""

    def __init__(self, npc_id: str, name: str, pattern: re.Pattern | None, pronoun: str, looks: set[str],
                 label: str = ""):
        self.id, self.name, self.pattern, self.pronoun = npc_id, name, pattern, pronoun
        self.words = {_key(w) for w, _, _ in _words(f"{name} {label}")}
        self.looks = looks - self.words


def person_of(npc: Any) -> Person:
    """A Person for an NPC row (inside its session)."""
    from src.services import dialogue

    label = ""
    try:
        label = dialogue.role_label(npc)
    except Exception:
        pass
    terms = [re.escape(w) for w in re.findall(r"[A-Za-z][A-Za-z'’-]+", npc.name or "") if len(w) >= 4]
    label_words = re.findall(r"[A-Za-z][A-Za-z'’-]+", label or "")
    if label_words:
        terms.append(r"\s+".join(re.escape(w) for w in label_words))
        if len(label_words[-1]) >= 5:
            terms.append(re.escape(label_words[-1]))
    pattern = re.compile(r"\b(?:" + "|".join(terms) + r")\b", re.IGNORECASE) if terms else None
    try:
        pronoun = dialogue.pronoun(npc) or ""
    except Exception:
        pronoun = ""
    return Person(npc.id, npc.name or "", pattern, pronoun, looks_words(npc), label)


_STARTS_WITH = re.compile(r"^\W*(he|his|him|she|her|hers)\b", re.IGNORECASE)
_PRONOUN_OF = {"he": "he", "his": "he", "him": "he", "she": "she", "her": "she", "hers": "she"}


def attribute(prose: str, people: list[Person], spoken: list[dict[str, Any]] | None = None
              ) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """({npc id: sentences about them}, {npc id: sentences naming them}). A
    sentence is about the first person it names; one opening on "he"/"her"
    is about the last such person — or the one who spoke just before it
    (`_at`, how much prose preceded each line)."""
    about: dict[str, list[str]] = {}
    naming: dict[str, list[str]] = {}
    lines = sorted((n["_at"], n.get("npc_id")) for n in spoken or []
                   if n.get("npc_id") and isinstance(n.get("_at"), int))
    by_id = {p.id: p for p in people}
    subject: str | None = None
    pos = 0
    text = prose or ""
    for sentence in _SENTENCE.split(text):
        start = text.find(sentence, pos) if sentence else -1
        if start >= 0:
            pos = start + len(sentence)
        for at, npc_id in lines:
            if at <= max(start, 0) and npc_id in by_id:
                subject = npc_id
        if not sentence.strip():
            continue
        named = sorted((m.start(), p.id) for p in people if p.pattern is not None
                       for m in p.pattern.finditer(sentence))
        for _, npc_id in named:
            if sentence not in naming.setdefault(npc_id, []):
                naming[npc_id].append(sentence)
        opener = _STARTS_WITH.match(sentence)
        if opener and subject in by_id and by_id[subject].pronoun in ("", _PRONOUN_OF[opener.group(1).lower()]):
            who = subject
        elif named:
            who = named[0][1]
        else:
            continue
        subject = who
        about.setdefault(who, []).append(sentence)
    return about, naming


def note(player_id: str, prose: str, spoken: list[dict[str, Any]] | None, people: list[Person],
         skip: set[str] | None = None) -> None:
    """Remember what this reply's prose said of each person (module doc)."""
    skip = set(skip or ())
    for p in people:
        skip |= p.words
    reply: dict[str, dict[str, Any]] = {}
    about, naming = attribute(prose, people, spoken)
    for p in people:
        mine, named = about.get(p.id, []), naming.get(p.id, [])
        if not mine and not named:
            continue
        looks = frozenset(p.looks - skip)       # "lantern" is the town's, not the Captain's badge
        grams = phrases("\n".join(mine), frozenset(skip), looks)
        used = {_key(f) for w, _, _ in _words("\n".join(mine + named)) for f in _forms(w)} & looks
        # "straw" and "straw-colored" are one detail, said once
        used -= {u for u in used if "-" in u and any(_key(f) in used for f in _forms(u)[1:])}
        reply[p.id] = {"grams": grams, "looks": used}
    with _lock:
        _STATE.setdefault(player_id, deque(maxlen=WINDOW)).append(reply)
        _SCENE.setdefault(player_id, deque(maxlen=SCENE_WINDOW)).append(scene_phrases(prose, skip))


def tics(player_id: str, npc_id: str) -> list[str]:
    """Someone's recurring details, most-repeated first: quoted runs from
    their latest use, and bare looks words ("basket (4 of the last 6
    replies)")."""
    with _lock:
        replies = list(_STATE.get(player_id) or ())
    mine = [r[npc_id] for r in replies if npc_id in r]
    if not mine:
        return []
    counts: dict[tuple[str, ...], int] = {}
    surface: dict[tuple[str, ...], str] = {}
    strong: dict[tuple[str, ...], bool] = {}
    for r in mine:
        for key, (said, firm) in r["grams"].items():
            counts[key] = counts.get(key, 0) + 1
            surface[key] = said
            strong[key] = strong.get(key, False) or firm
    # A gesture alone must come back once more than a detail of theirs
    repeated = {k: c for k, c in counts.items() if c >= (MIN_REPLIES if strong[k] else MIN_REPLIES + 1)}
    looks: dict[str, int] = {}
    for r in mine:
        for w in r["looks"]:
            looks[w] = looks.get(w, 0) + 1
    # ...or back to back: a detail in SCENE_REPEATS of the last SCENE_WINDOW
    # replies (fleet10-wenna's "straw hair" at T11 and T12, again T19 and T20)
    recent: dict[str, int] = {}
    for r in replies[-SCENE_WINDOW:]:
        for w in (r.get(npc_id) or {}).get("looks") or ():
            recent[w] = recent.get(w, 0) + 1
    out: list[tuple[int, int, str]] = []
    covered: set[str] = set()
    for key, c in sorted(repeated.items(), key=lambda kv: (-kv[1], -len(kv[0]))):
        # one run per detail: "copper clasp catching" says "clasp catching firelight"
        if len(set(key) & covered) >= min(2, len(key)):
            continue
        out.append((c, len(key), f'"{surface[key].strip()}"'))
        covered |= set(key)
    for w, c in sorted(looks.items(), key=lambda kv: -kv[1]):
        if (c >= SIGNATURE_REPLIES or recent.get(w, 0) >= SCENE_REPEATS) and w not in covered:
            out.append((c, 1, f"{w} ({c} of the last {len(replies)} replies)"))
    out.sort(key=lambda t: (-t[0], -t[1]))
    return [s for _, _, s in out[:MAX_PHRASES]]


_SIMILE = re.compile(r"\b(?:like|as\s+if|as\s+though|the\s+way)\s+(?P<body>[^,.;:!?—–\n\"“”]{4,70})",
                     re.IGNORECASE)
# A look toward the same thing: "eyes drift north, toward the hills",
# "glancing once toward the barred gate", "his eyes keep drifting to the hills"
_GAZE = re.compile(
    r"\b(?:eyes?|gaze|glances?|glanced|glancing|looks?|looked|looking|stares?|stared|staring)\b[^.!?;\n]{0,40}?"
    r"\b(?:(?P<dir>north|south|east|west|northward|southward)\b|(?:to|toward|towards|at|on|over|past)\s+the\s+"
    r"(?:[a-z]+\s+)?(?P<thing>[a-z]+)\b)", re.IGNORECASE)
_COMPASS = re.compile(r"\b(?:north|south|east|west)(?=\b|ward)", re.IGNORECASE)
_RUN = 4                 # words in a repeated run of the narration


def scene_phrases(text: str, skip: set[str] | frozenset[str] = frozenset()) -> dict[tuple[str, ...], str]:
    """{key: surface} of what the narration leans on: a simile ("like a
    door pulled to"), a look toward a thing ("eyes … toward the hills"), and
    each run of _RUN words with two content words. Never a word of `skip`
    (people, places, the plot's own things)."""
    out: dict[tuple[str, ...], str] = {}

    def fine(w: str) -> bool:
        return w not in skip and _key(w) not in skip

    for sentence in _SENTENCE.split(text or ""):
        for m in _SIMILE.finditer(sentence):
            words = [_key(w) for w, _, _ in _words(m.group("body")) if len(w) >= 3 and w not in _STOP]
            if len(words) >= 2 and all(fine(w) for w in words):
                out[("like",) + tuple(words[:4])] = m.group(0).strip()
        for m in _GAZE.finditer(sentence):
            # a look north is one habit, whatever it lands on ("toward the
            # north road", "the north wall", "eyes still fixed north")
            compass = _COMPASS.search(m.group(0))
            target = (compass.group(0) if compass else m.group("dir") or m.group("thing") or "").lower()
            if len(target) >= 4 and target not in _STOP and target not in _EVERYDAY and (compass or fine(target)):
                out[("gaze", _key(target))] = m.group(0).strip()
        tokens = [w for w, _, _ in _words(sentence)]
        for j in range(len(tokens) - _RUN + 1):
            gram = tokens[j:j + _RUN]
            if sum(1 for w in gram if len(w) >= 3 and w not in _STOP) >= 2 and all(fine(w) for w in gram):
                out[("run",) + tuple(_key(w) for w in gram)] = " ".join(gram)
    return out


def scene_tics(player_id: str) -> list[str]:
    """The narration's habits back in SCENE_REPEATS of the last SCENE_WINDOW
    replies, most repeated first: quoted as last written."""
    with _lock:
        replies = list(_SCENE.get(player_id) or ())
    counts: dict[tuple[str, ...], int] = {}
    surface: dict[tuple[str, ...], str] = {}
    for reply in replies:
        for key, said in reply.items():
            counts[key] = counts.get(key, 0) + 1
            surface[key] = said
    out: list[str] = []
    covered: list[set[str]] = []
    for key, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][0] != "like", -len(kv[0]))):
        if c < SCENE_REPEATS:
            continue
        words = set(key[1:])
        # one habit, one line: a run inside a simile already said is it
        if any(len(words & seen) >= min(2, len(words)) for seen in covered):
            continue
        covered.append(words)
        out.append(f'"{surface[key]}" ({c} of the last {len(replies)} replies)')
        if len(out) >= MAX_SCENE:
            break
    return out


def note_reply(player_id: str, prose: str, spoken: list[dict[str, Any]] | None = None) -> None:
    """After a reply: the people where the player stands and everyone who
    spoke, what the reply said of each (`note`). Never raises."""
    if not player_id or not (prose or "").strip():
        return
    try:
        from src.models import NPC, Location, Player, get_session

        with get_session() as db:
            player = db.get(Player, player_id)
            if player is None:
                return
            ids = {n.get("npc_id") for n in spoken or [] if n.get("npc_id")}
            rows = db.query(NPC).filter(NPC.current_location_id == player.current_location_id).all() \
                if player.current_location_id else []
            if ids:
                rows += [n for n in db.query(NPC).filter(NPC.id.in_(ids)).all() if n not in rows]
            from src.services.dialogue import is_beast

            people = [person_of(n) for n in rows if (n.status or "alive") == "alive" and not is_beast(n)]
            skip = {_key(w) for (name,) in db.query(Location.name).all() for w, _, _ in _words(name or "")}
            skip |= {_key(w) for w, _, _ in _words(player.name or "")}
            # The story's own things are never a tic to rest: a quest's proof
            # and the Things here come back because the plot needs them (the
            # round-8 replay told the DM to rest the villain's bone whistle)
            skip |= {_key(w) for text in _story_things(db, player) for w, _, _ in _words(text)}
        note(player_id, prose, spoken, people, skip=skip)
    except Exception as e:
        logger.debug(f"tic record skipped: {e}")


def _story_things(db: Any, player: Any) -> list[str]:
    """Names of what the plot needs: every quest step's item and the labels
    of the Things where the player stands."""
    from src.models import Location, Quest

    out: list[str] = []
    try:
        for (objectives,) in db.query(Quest.objectives).all():
            for o in objectives or []:
                if isinstance(o, dict) and o.get("item_name"):
                    out.append(str(o["item_name"]))
        here = db.get(Location, player.current_location_id) if player.current_location_id else None
        for obj in (getattr(here, "objects", None) or []):
            if isinstance(obj, dict):
                out.append(str(obj.get("label") or obj.get("name") or ""))
                # ...and what it reads or holds: "tally marks gouged in
                # threes" is the hut's clue, no habit of the narration's
                # (fleet10-bram T13-T15)
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
                item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
                for text in (payload.get("text"), item.get("name"), item.get("reads"), item.get("description")):
                    out.extend(w for w in re.findall(r"[A-Za-z]{5,}", str(text or "")))
        # ...and what the player carries ("split clean down one side" is the
        # whistle in their pack, fleet5-bram T11-T12)
        for entry in getattr(player, "inventory", None) or []:
            if isinstance(entry, dict):
                for text in (entry.get("name"), entry.get("description")):
                    out.extend(w for w in re.findall(r"[A-Za-z]{5,}", str(text or "")))
    except Exception as e:
        logger.debug(f"story things unavailable: {e}")
    return out


def brief_lines(player_id: str, npcs: list[Any]) -> list[str]:
    """One line for the people present who keep coming back to the same
    details, else none. Never raises."""
    parts = []
    try:
        for npc in npcs:
            found = tics(player_id, npc.id)
            if found:
                parts.append(f"{npc.name}: " + ", ".join(found))
            if len(parts) >= MAX_PEOPLE:
                break
        scene = scene_tics(player_id)
    except Exception as e:
        logger.debug(f"tic line skipped: {e}")
        return []
    if scene:
        parts.append("the narration itself: " + ", ".join(scene))
    if not parts:
        return []
    return ["Already used — vary: these details came back reply after reply — rest each for a few replies and "
            "show these people through something else (a new gesture, another worry, a thing they do): "
            + "; ".join(parts)]

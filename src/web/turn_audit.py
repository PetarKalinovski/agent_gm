"""Post-turn fiction/database consistency audit.

The #1 DM failure class found in self-playtests: narration claiming
mechanics that never happened (quest banners with no quest tool, damage
with no health change, items that never entered the inventory). The
REALITY CONTRACT prompt section fights it at the source; this auditor
catches what slips through — warnings are logged and attached to the
`complete` SSE event so self-playtests get a scoreboard.

High-precision checks only: a false alarm every turn would train everyone
to ignore the audit. Mutations the ENGINE performed between turns (canvas
travel, pickups, potions, combat results, campaign beat ops) arrive as
`code_actions` and are unioned into the succeeded tool set — one mechanism,
every check corrected at once.

Since the fleet playtest it also reads the turn's speaker lines: too many or
too long (too_talky), and strangers using a name nobody gave them
(name_before_introduction). Every detail says what to write next time.
"""

import re
from typing import Any, Callable, Iterable

from src.services.campaign import beat_keywords_delivered, sealed_truth_hits
# The campaign's word model (stems, generic words), for object contents
from src.services.campaign import _GENERIC_WORDS, _text_forms, _word_forms, _words


_QUEST_CLAIM = re.compile(
    r"\b(?:quest\s+(?:accepted|activated|updated|complete[d]?|failed)"
    r"|new\s+quest|QUEST\s+(?:ACCEPTED|ACTIVATED|COMPLETED?|FAILED))\b",
    re.IGNORECASE,
)
_QUEST_TOOLS = {"activate_quest", "update_quest_status", "create_quest", "update_quest_objectives"}

_DAMAGE_CLAIM = re.compile(
    # Second OR first person — playtest caught the DM narrating injuries as
    # "I've pulled something in my shoulder" with zero tool calls
    r"\b(?:you|i)(?:'re|'ve| are| am| have| take| took| suffer(?:ed)?)\b[^.!?\n]{0,50}"
    r"\b(?:damage|wounded?|bleeding|injur(?:ed|y)|hurt(?:\s+now)?"
    r"|pulled something|sprained|twisted my|twisted your|broken (?:rib|arm|leg|ankle|wrist))\b",
    re.IGNORECASE,
)

_ITEM_CLAIM = re.compile(
    r"\b(?:added to your (?:pack|inventory|satchel)"
    r"|you (?:pocket|stow|tuck away|now carry)\b"
    # "You take the brass token from Coldiron" / "I take the pass from him"
    # — but not "you take a long look at the square from the well"
    r"|(?:you|i) (?:take|accept|receive) (?:the|a|an|his|her|their) "
    r"(?!(?:long |quick |deep |last |good |hard |slow |short |careful )?"
    r"(?:look|breath|moment|step|seat|sip|drink|swig|pause|glance|peek|sniff|"
    r"turn|chance|hint|beat|second|minute|while)\b)[^.!?\n]{0,40}\bfrom\b"
    # "She presses it into your palm" — "he hands you the pass" and "presses a
    # key into your palm" name their object, which _ITEM_GIVEN_TO_YOU checks
    r"|presses (?:it|something) [^.!?\n]{0,30}\binto your (?:hand|palm)"
    r")",
    re.IGNORECASE,
)
_ITEM_TOOLS = {"add_to_inventory", "remove_from_inventory", "transfer_item", "interact"}

_ARRIVAL_CLAIM = re.compile(
    r"\byou (?:arrive (?:at|in)|reach the|step into|pass through the gates? of"
    r"|are waved (?:through|in|inside)|are let (?:in|inside|through))\b"
    # fleet4-tamsin T9 narrated the whole walk home — "He just waves you
    # through" at the gate — with no @move: the database kept her at the
    # Hollow. "Waves you off" is a dismissal, not a gate.
    r"|\bwav(?:es?|ed|ing) you (?:through|in|inside|past)\b"
    # "You're back in Market Square" — a named place, not "back in the fight",
    # nor a person's thing ("back at Coldiron's board", "in Coldiron's good
    # graces") — a place's own name goes on capitalised ("Wolf's Hollow")
    r"|\byou(?:'re|’re| are) back (?:in|at|inside) (?:the )?(?-i:[A-Z])(?![\w-]*['’]s\s+(?-i:[a-z]))",
    re.IGNORECASE,
)
# Crossing the room is not arriving anywhere: fleet6-tamsin T15 "as you reach
# the table", T18 "By the time you reach the bar" — both inside the Drowned
# Lantern, where the player was. The warning sent the DM to @move them where
# they stood, and held back the engine's own fix for the people the story had
# walked in (server._bring_speakers_here). Where a claim names its
# destination, that must look like a place: a capitalised name or a place
# noun, never furniture, a corner of the room, or the place they are in.
_ARRIVAL_TARGET = re.compile(
    r"(?:arrive\s+(?:at|in)|reach|step\s+into|back\s+(?:in|at|inside))\s+(?P<target>[^.!?;:,\n—–]{1,60})",
    re.IGNORECASE,
)
_IN_ROOM = frozenset({
    "table", "tables", "bar", "counter", "hearth", "fire", "fireplace", "rail", "railing", "window",
    "stool", "bench", "booth", "corner", "door", "doorway", "threshold", "stairs", "steps", "staircase",
    "fence", "well", "stall", "stalls", "cart", "wagon", "board", "post", "bed", "chair", "desk", "shelf",
    "barrel", "barrels", "crate", "crates", "light", "glow", "warmth", "shadow", "shadows", "dark",
    "darkness", "cold", "edge", "middle", "centre", "center", "end", "side", "paddock", "trough", "pump",
    "lantern", "lamp", "awning", "crowd", "circle", "spot", "rope", "line", "top", "bottom", "foot",
})
_PLACE_WORDS = frozenset({
    "square", "tavern", "inn", "gate", "gates", "gatehouse", "hollow", "road", "village", "town", "city",
    "yard", "stable", "stables", "market", "street", "lane", "alley", "house", "hall", "camp", "bridge",
    "docks", "dock", "harbor", "harbour", "temple", "chapel", "shrine", "shop", "smithy", "forge", "mill",
    "farm", "woods", "forest", "clearing", "treeline", "ridge", "hills", "river", "ford", "crossroads",
    "outskirts", "walls", "tower", "keep", "castle", "manor", "barracks", "garrison", "hut", "cabin",
    "cave", "ruins", "shore", "quay", "warehouse", "granary", "office", "guardhouse", "infirmary",
    "cellar", "attic", "loft", "birches", "palisade", "wharf", "pier",
})
_TARGET_STOP = frozenset({
    "and", "as", "where", "while", "when", "to", "with", "before", "after", "just", "then", "but", "so",
    "in", "at", "on", "by", "for", "from", "beside", "behind", "near", "into", "toward", "towards", "under",
})
_ARTICLES = frozenset({"the", "a", "an", "his", "her", "their", "its", "your", "that", "this"})
# ...nor is reaching a PERSON: "you reach the Captain", "the Keeper", "the
# Ash-Cloaked Man" is crossing to someone, capitalised or not
_PERSON_NOUNS = frozenset({
    "captain", "sergeant", "commander", "lieutenant", "officer", "guard", "guards", "hawker",
    "merchant", "trader", "herbalist", "apprentice", "healer", "priest", "smith", "barkeep", "bartender",
    "innkeeper", "stranger", "boy", "girl", "lad", "lass", "child", "elder", "mayor", "lord", "lady",
    "sir", "master", "mistress", "widow", "drunk", "beggar", "runner", "courier", "carter", "driver",
})
_PERSON_SUFFIXES = ("man", "men", "woman", "women", "keeper", "keepers")
# ...nor is a room of the place they stand in ("the side room", "the back
# room", "the passage behind the bar"): a part of here, no @move
_INNER_ROOMS = frozenset({"room", "rooms", "chamber", "corridor", "hallway", "passage", "alcove", "nook",
                          "stairwell", "landing", "interior", "inside"})


# "The North Gatehouse rises ahead" and someone of that place talking: the
# reply walked the player there with no @move (fleet8-tamsin T11-T12: a gate
# staged and a guard speaking at it, the player still in the square) — and
# none of the arrival words above was said
_AHEAD = re.compile(
    r"(?P<place>\b[A-Z][\w'’-]*(?:\s+(?:of\s+)?[A-Z][\w'’-]*){0,3})(?:['’]s\s+[a-z]+)?\s+"
    r"(?:rises?|rising|looms?|looming|swims?|swimming|heaves?|comes?|coming|appears?|opens?|waits?)\s+"
    r"(?:up\s+)?(?:ahead|into\s+view|before\s+you|in\s+front\s+of\s+you)\b"
)


def _begins(word: str, longer: str) -> bool:
    """"gate" begins "gatehouse" (a word of four letters or more, three short)."""
    return len(word) >= 4 and len(longer) >= len(word) + 3 and longer.startswith(word)


def _place_said(phrase: str, name: str) -> bool:
    """'The North Gatehouse' / 'Gatehouse' / 'The North Gate' name North
    Gatehouse; 'The Hollow' Wolf's Hollow."""
    said = [w for w in re.findall(r"[a-z']+", (phrase or "").lower().replace("’", "'")) if w not in _ARTICLES]
    place = [w for w in re.findall(r"[a-z']+", (name or "").lower().replace("’", "'")) if w not in _ARTICLES]
    return bool(said and place) and (said == place or said[-1] == place[-1] or _begins(said[-1], place[-1]))


def _voice_of_place(label: str, name: str) -> bool:
    """A voice with no row whose label is the place's own: "Gate Guard" at
    North Gatehouse, "Lantern Keeper" at The Drowned Lantern."""
    words = [w for w in re.findall(r"[a-z]+", (label or "").lower()) if len(w) >= 4]
    place = [w for w in re.findall(r"[a-z]+", (name or "").lower()) if len(w) >= 4]
    return any(w == p or _begins(w, p) for w in words for p in place)


def _staged_ahead(turn_text: str, spoken: list[dict[str, Any]] | None,
                  here_id: str | None) -> tuple[str, str, str] | None:
    """(place, the words, the speaker) when the prose brings a named place up
    ahead of the player and someone who stands there speaks in the same
    reply, while the player is somewhere else; else None. The real
    fleet8-tamsin T11 guard had no row at all ("Gate Guard", npc_id null):
    a voice of no row whose label is the staged place's own counts too."""
    hits = [(m.group("place"), m.group(0)) for m in _AHEAD.finditer(turn_text or "")]
    notes = [n for n in spoken or [] if isinstance(n, dict)]
    ids = list(dict.fromkeys(n.get("npc_id") for n in notes if n.get("npc_id")))
    unbacked = list(dict.fromkeys(str(n.get("npc_name") or n.get("label") or "") for n in notes
                                  if not n.get("npc_id") and (n.get("npc_name") or n.get("label"))))
    if not hits or not (ids or unbacked):
        return None
    try:
        from src.models import NPC, Location, get_session

        with get_session() as session:
            for npc_id in ids:
                npc = session.get(NPC, npc_id)
                if npc is None or not npc.current_location_id or npc.current_location_id == here_id:
                    continue
                where = session.get(Location, npc.current_location_id)
                for place, words in hits:
                    if where is not None and _place_said(place, where.name):
                        return where.name, words, npc.name
            if unbacked:
                for where in session.query(Location).all():
                    if where.id == here_id or str(getattr(where.type, "value", where.type)).lower() == "town":
                        continue                # a whole town is no place one stands in
                    for place, words in hits:
                        if not _place_said(place, where.name):
                            continue
                        voice = next((v for v in unbacked if _voice_of_place(v, where.name)), None)
                        if voice:
                            return where.name, words, voice
    except Exception:           # the audit never costs the turn
        import logging
        logging.getLogger(__name__).debug("staged-ahead check failed", exc_info=True)
    return None


def _arrival_claim(text: str, here: str | None = None, *, arrived_here: bool = False) -> str | None:
    """The words of the first claim that the player arrived somewhere they
    are not (`here`: the name of where they are), else None.

    `arrived_here`: the last reply's @move landed them where they stand —
    "waves you past" with no place after it is its door, not a way
    somewhere else (fleet10-wenna T13: the Watchman waved her into the
    gatehouse's side room the reply after `@move North Gatehouse`, and the
    warning had the DM write a refused @move at T14)."""
    here_words = {w for w in re.findall(r"[a-z']+", (here or "").lower()) if w not in _ARTICLES and len(w) > 2}
    for m in _ARRIVAL_CLAIM.finditer(text or ""):
        target = _ARRIVAL_TARGET.search(text[m.start():m.start() + 140])
        if target is None or target.start() > len(m.group(0)):
            if arrived_here:
                continue
            return m.group(0)          # waved through, let in: a gate is a way somewhere
        kept: list[str] = []
        for word in re.findall(r"[\w'’-]+", target.group("target")):
            if word.lower() in _TARGET_STOP:
                break
            kept.append(word)
        lowered = [w.lower() for w in kept]
        referent = kept[lowered.index("of") + 1:] if "of" in lowered else kept
        words = [w for w in referent if w.lower() not in _ARTICLES]
        if not words:
            continue
        # "Coldiron's table" is a table, "the far end of the bar" the bar —
        # but "the Drowned Lantern" is a name, never a lamp
        plain = [re.sub(r"['’]s$", "", w.lower()) for w in words]
        if words[-1][:1].islower() and (plain[-1] in _IN_ROOM or plain[-1] in _INNER_ROOMS):
            continue                   # a room of the place they are in: "step into the side room"
        if set(plain) <= here_words:
            continue                   # "back in Market Square", standing in it
        if "reach" in m.group(0).lower() and not set(plain) & _PLACE_WORDS and (
                plain[-1] in _PERSON_NOUNS or plain[-1].split("-")[-1].endswith(_PERSON_SUFFIXES)):
            continue                   # "you reach the Captain" (but "step into the Drowned Man" is an inn)
        if set(plain) & _PLACE_WORDS or any(w[:1].isupper() for w in words):
            return m.group(0)
    return None


# What the last replies did, per player, for the two checks that read past
# this reply (in memory, like `mark_object_seen`: a restart forgets a reply
# or two, nothing else): where the last reply's @move landed the player, and
# whether a fight or a roll in the last replies could have hurt them
_ARRIVED_LAST: dict[str, str | None] = {}
_HURT_REPLIES: dict[str, list[bool]] = {}
HURT_WINDOW = 2


def note_reply_moves(player_id: str | None, moved_to: str | None, hurt: bool) -> None:
    """After a reply: where its @move left the player (None: it moved
    nobody), and whether it had a fight, a roll that went against them, or
    a wound."""
    if not player_id:
        return
    _ARRIVED_LAST[player_id] = moved_to
    _HURT_REPLIES[player_id] = (_HURT_REPLIES.get(player_id, []) + [bool(hurt)])[-HURT_WINDOW:]


def drop_inner_arrival(warnings: list[dict[str, str]], player_id: str | None, turn_text: str,
                       state_before: dict[str, Any] | None) -> list[dict[str, str]]:
    """`arrival_without_move` minus the arrival into where the last reply's
    @move just took the player (`_arrival_claim(arrived_here=True)`)."""
    before = state_before or {}
    here_id = before.get("location_id")
    if not player_id or not here_id or _ARRIVED_LAST.get(player_id) != here_id:
        return warnings
    if _arrival_claim(turn_text, before.get("location"), arrived_here=True):
        return warnings
    return [w for w in warnings if not (w.get("check") == "arrival_without_move"
                                        and str(w.get("detail", "")).startswith("Narration describes arriving"))]


# How bad the prose says the hurt is, by the words of `_DAMAGE_CLAIM`
_BADLY = re.compile(r"\b(?:badly|gravely|grievously|seriously|critically|broken)\b", re.IGNORECASE)
_LIGHTLY = re.compile(r"\b(?:pulled something|sprained|twisted)\b", re.IGNORECASE)


def drop_told_wound(warnings: list[dict[str, str]], player_id: str | None, turn_text: str,
                    health_before: str | None, *, hurt_now: bool = False) -> list[dict[str, str]]:
    """`damage_without_tool` minus a wound the player already carries.

    fleet10-bram T18: "You're bleeding, you're spent" with health already
    `hurt` from the T16 fight, and [COMBAT OUTCOME] had said never write
    @health — the warning asked for one. When a fight or a roll in this
    reply or the last HURT_WINDOW hurt them (`note_reply_moves`) and their
    health already sits at or below what the prose describes, the prose
    tells that wound; a worse one ("badly hurt" while only `hurt`) still
    needs @health."""
    if not any(w.get("check") == "damage_without_tool" for w in warnings):
        return warnings
    if not (hurt_now or any(_HURT_REPLIES.get(player_id or "", []))):
        return warnings
    claim = _DAMAGE_CLAIM.search(turn_text or "")
    if claim is None or health_before not in _HEALTH_LADDER:
        return warnings
    said = claim.group(0)
    level = "badly_hurt" if _BADLY.search(said) else "winded" if _LIGHTLY.search(said) else "hurt"
    if _HEALTH_LADDER.index(health_before) > _HEALTH_LADDER.index(level):
        return warnings                # better off than the prose says: a new wound
    return [w for w in warnings if w.get("check") != "damage_without_tool"]


# Meta-mechanics claims ("Consequences applied: ... disposition is now
# hostile ... an event is created") — live playtest caught the DM printing
# a fabricated consequence ledger with zero tool calls
_CONSEQUENCE_CLAIM = re.compile(
    r"(?:consequences applied|disposition is now|relationship is (?:now )?(?:severely )?damaged"
    r"|reputation (?:with [^.!?\n]{0,30})?(?:drops|falls|is damaged|takes a hit)"
    r"|an event [^.!?\n]{0,50}is (?:created|recorded))",
    re.IGNORECASE,
)
_CONSEQUENCE_TOOLS = {
    "update_npc_relationship", "update_npc_mood", "update_player_reputation",
    "create_event", "update_faction",
}

# Fabricated inline dialogue — the DM writing quoted NPC lines in narration
# instead of as speaker lines (`Name (tone): "words"`). Those lines
# are silent for the player: no voice, no portrait, no speech bubble. The
# deduper strips re-quotes of REAL speech before the audit runs, so any
# attributed quote still present in the turn text never went through a
# speech tool.
_SPEECH_VERBS = (
    "says|said|replies|replied|asks|asked|answers|answered|adds|added"
    "|calls out|calls|called|growls|growled|mutters|muttered|murmurs|murmured"
    "|whispers|whispered|shouts|shouted|yells|yelled|snaps|snapped|cries|cried"
    "|hisses|hissed|barks|barked|rasps|rasped|sneers|sneered|grunts|grunted"
    "|scoffs|scoffed|chuckles|chuckled|laughs|laughed|warns|warned"
    "|insists|insisted|demands|demanded|offers|offered|drawls|drawled"
    "|purrs|purred|croaks|croaked|wheezes|wheezed|spits|spat|snarls|snarled"
    "|bellows|bellowed|roars|roared|declares|declared|announces|announced"
    "|admits|admitted|promises|promised|urges|urged|pleads|pleaded|begs|begged"
    "|continues|continued|interrupts|interrupted"
)

# Speakers that must never trigger the check: the player ("you"), inanimate
# text-bearers, and generic sentence-starters that can precede a speech verb.
_SPEAKER_STOPWORDS = {
    "you", "i", "it", "we", "that", "this", "what", "who", "one", "some",
    "someone", "somebody", "nobody", "everyone", "people", "others",
    "word", "rumor", "rumors", "legend", "legends", "lore", "history",
    "tradition", "locals", "then", "but", "and", "now", "even", "still",
    "suddenly", "no", "yes", "as", "if", "when", "while", "so",
    "my", "his", "her", "their", "your",
}
_INANIMATE_SPEAKERS = {
    "sign", "note", "letter", "inscription", "plaque", "notice", "poster",
    "label", "page", "book", "scroll", "carving", "graffiti", "banner",
    "placard", "ledger", "wind", "echo", "stone", "walls", "air",
}

_SPEAKER_PATTERN = r"[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*)?|[Tt]he\s+\w+|[Aa]n?\s+\w+|he|she|they"

# "Coldiron growls, \"...\"" / "A guard calls out: '...'"
# A single quote opens a quote only where a word cannot go on (after a space,
# a bracket or a dash), and closes one only where no word goes on after it:
# fleet4-bram T14 read "Piper promised, and Wolf's Hollow beyond it hasn't"
# as Piper saying "s Hollow beyond it hasn" — every one of the five
# dialogue_without_speak flags in the first four fleets was an apostrophe.
_PRE_ATTRIBUTED_QUOTE = re.compile(
    rf"\b(?P<speaker>{_SPEAKER_PATTERN})"
    rf"(?:,[^,\n]{{0,30}},)?\s+(?:{_SPEECH_VERBS})\b[^\"“'‘.!?\n]{{0,25}}"
    rf"(?:[\"“](?P<dq>[^\"”“\n]{{8,400}})[\"”]|(?<![\w'’])['‘](?P<sq>[^'’\n]{{8,400}})['’](?![a-zA-Z]))"
)

# "\"...,\" Piper says" / "\"...\" says Piper"
_POST_ATTRIBUTED_QUOTE = re.compile(
    rf"[\"“](?P<quote>[^\"”“\n]{{8,400}})[\"”],?\s+"
    rf"(?:(?:{_SPEECH_VERBS})\s+(?P<speaker_a>[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*)?)"
    rf"|(?P<speaker_b>[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*)?|he|she|they)\s+(?:{_SPEECH_VERBS}))\b"
)

# The speak()-output format mimicked in plain text: `Coldiron: "..."`
_LABEL_QUOTE = re.compile(
    r"(?m)^(?P<speaker>[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*){0,2}):\s*"
    r"[\"“](?P<quote>[^\"”“\n]{8,400})[\"”]"
)

# Quotes with no said-tag at all — the action-beat style LLMs default to
# ("She wipes her hands on her apron. \"I run this place...\""). Playtesting
# found a whole conversation delivered this way: not one line was voiced,
# and the attribution-based checks above saw nothing. Any quote of four or
# more words left in the narration after the deduper is a line the player
# never heard — unless it is the player's own reported words, or writing.
_ANY_QUOTE = re.compile(r"[\"“](?P<quote>[^\"”“\n]{8,600})[\"”]")
_PLAYER_VERBS = (
    r"(?:say|ask|tell|reply|answer|call|offer|add|manage|murmur|mutter|whisper"
    r"|shout|lie|insist|admit|promise|counter|snap|growl|breathe|repeat"
    r"|announce|declare|try|venture|press|blurt|hiss)"
)
_PLAYER_WORDS_BEFORE = re.compile(
    rf"\byou\b[^\"“\n]{{0,25}}\b{_PLAYER_VERBS}\w*\s*[,:]?\s*$", re.IGNORECASE
)
_PLAYER_WORDS_AFTER = re.compile(
    rf"^[,.]?\s*you\s+{_PLAYER_VERBS}\b", re.IGNORECASE
)
_WRITTEN_TEXT_BEFORE = re.compile(
    r"(?:\b(?:reads?|inscri(?:bed|ption)|carved|written|scrawled|painted|etched"
    r"|titled|labell?ed|stamped|printed|lettered|engraved|spells? out|words?"
    r"|hand)\s*[,:]?\s*$"
    # "The sign says ..." — an inanimate text-bearer with a speech verb
    r"|\b(?:" + "|".join(sorted(_INANIMATE_SPEAKERS)) + r")\b[^\"“\n]{0,20}"
    r"\b(?:says|said|reads?|proclaims?|declares?|warns?|announces?|states?)\s*[,:]?\s*$)",
    re.IGNORECASE,
)


def unspoken_quotes(text: str) -> list[str]:
    """Quoted lines in DM narration that never went through a speech tool.

    Runs on the post-deduper text, so re-quotes of real speech are already
    gone. The player's own reported words and written text are excluded;
    everything else four words or longer is a line somebody said out loud
    and the player never heard.
    """
    found: list[str] = []
    for m in _ANY_QUOTE.finditer(text or ""):
        quote = m.group("quote").strip()
        if len(quote.split()) < 4:
            continue
        before = text[max(0, m.start() - 60):m.start()]
        after = text[m.end():m.end() + 30]
        if _PLAYER_WORDS_BEFORE.search(before) or _PLAYER_WORDS_AFTER.search(after):
            continue
        if _WRITTEN_TEXT_BEFORE.search(before):
            continue
        found.append(quote)
    return found

# Risky actions the DM resolved in the player's favour without asking the
# engine. Fires only on RESOLUTION phrases (the attempt succeeded), never on
# attempts — the whole point of roll_check is that the dice decide.
_RISK_RESOLUTION = re.compile(
    r"\byou\b[^.!?\n]{0,40}\b(?:"
    r"slip past|sneak past|slip by|slip inside|go unnoticed|remain unseen|"
    r"are not seen|pass unnoticed|"
    r"manage to (?:convince|persuade|talk|climb|pry|force|pick|slip|reach)|"
    r"pick the lock|force the lock|jimmy the|"
    r"scale the|haul yourself (?:up|over)|clear the (?:wall|gap|fence)|"
    r"convince(?!\s+yourself)|persuade(?!\s+yourself)|"
    r"talk (?:him|her|them|it) (?:into|down|around|out of)|"
    r"pickpocket|lift the (?:purse|key|coin|pouch)|"
    r"catch the (?:glint|movement|flicker)|spot the (?:tripwire|ambush|trap)"
    r")\b",
    re.IGNORECASE,
)
# A live fight exempts the turn: combat resolves on the canvas and its prose
# legitimately describes closing distance, dodging, and landing blows.
_RISK_EXEMPT_TOOLS = {"roll_check", "start_combat"}

# Engine mechanics printed as prose — numbers next to xp/level/HP/damage, and
# the bare vocabulary itself ("level up", "XP", "you feel stronger"). Growth is
# invisible: even a REAL level-up may not be narrated in mechanical language,
# so this one fires regardless of code_actions.
_MECHANICS_IN_NARRATION = re.compile(
    r"(?:"
    r"\b\+?\d+\s*(?:xp|exp|experience|hit\s*points?|damage|hp)\b"
    r"|\b(?:hp|health)\s*[:\-]?\s*\d+"
    r"|\blevel(?:led|ed)?\s*(?:up)?\s*\d+"
    r"|\breach(?:ed|es)\s+level\s+\d+"
    r"|\byou\s+are\s+now\s+level\b"
    r"|\blevel(?:ed)?\s*up\b"
    r"|\bgain(?:ed)?\s+(?:a\s+)?level\b"
    r"|\bexperience\s+points?\b"
    r"|\bxp\b"
    r"|\byou\s+feel\s+(?:stronger|more\s+experienced)\b"
    r")",
    re.IGNORECASE,
)

# The DM typing the card instead of playing it (especially after a rejected
# play_cinematic). Literal-string precise: ordinary fantasy prose cannot trip it.
_CINEMATIC_TEXT_CLAIM = re.compile(
    r"(?m)(?:^\s*(?:ACT|CHAPTER|PART|EPISODE)\s+(?:[IVXL]+|\d+|ONE|TWO|THREE|FOUR|FIVE)\b"
    r"|\btitle card\b|\bcut to black\b|\bfade to black\b|\bletterbox\b"
    r"|\bthe screen (?:goes|fades|shakes)\b)",
    re.IGNORECASE,
)

# Category labels some callers use instead of tool names (campaign beat ops,
# combat results). Translated to the tools the engine actually ran so the one
# suppression mechanism stays "union into succeeded".
CODE_ACTION_TOOLS = {
    "quest": {"activate_quest", "update_quest_status"},
    "item": {"add_to_inventory"},
    "consequence": {"create_event"},
    "health": {"update_player_health"},
    "arrival": {"move_player"},
}


def _expand_code_actions(code_actions: set[str] | None) -> set[str]:
    """Tool names the engine ran, with category labels translated."""
    names: set[str] = set()
    for action in code_actions or ():
        if not action:
            continue
        names.add(action)
        names |= CODE_ACTION_TOOLS.get(action, set())
    return names


def _speaker_is_person(speaker: str) -> bool:
    tokens = speaker.strip().split()
    if not tokens:
        return False
    first = tokens[0].lower()
    if first in ("the", "a", "an"):
        return tokens[-1].lower() not in _INANIMATE_SPEAKERS
    if first in ("he", "she", "they"):
        return True
    if first in _SPEAKER_STOPWORDS:
        return False
    if tokens[-1].lower() in _INANIMATE_SPEAKERS:
        return False
    return speaker[0].isupper()


def _find_fabricated_dialogue(text: str) -> list[str]:
    """Speakers of attributed quotes that never went through a speech tool."""
    speakers: list[str] = []

    def consider(speaker: str | None, quote: str | None) -> None:
        if not speaker or not quote or len(quote.split()) < 3:
            return
        if _speaker_is_person(speaker):
            speakers.append(speaker.strip())

    for m in _PRE_ATTRIBUTED_QUOTE.finditer(text):
        consider(m.group("speaker"), m.group("dq") or m.group("sq"))
    for m in _POST_ATTRIBUTED_QUOTE.finditer(text):
        consider(m.group("speaker_a") or m.group("speaker_b"), m.group("quote"))
    for m in _LABEL_QUOTE.finditer(text):
        consider(m.group("speaker"), m.group("quote"))

    if not speakers and unspoken_quotes(text):
        speakers.append("someone present (an unattributed quote)")

    seen: set[str] = set()
    unique = []
    for s in speakers:
        if s.lower() not in seen:
            seen.add(s.lower())
            unique.append(s)
    return unique


_CONSUME_VERB = re.compile(
    r"\b(?:drink|drinks|drank|drunk|drinking|swallow\w*|gulp\w*|quaff\w*|uncork\w*|"
    r"sip\w*|knock\w* back|down(?:s|ed)? (?:the|it|half)|eat|eats|ate|chew\w*|apply|applies|applied)\b",
    re.IGNORECASE,
)


_SENTENCES = re.compile(r"(?<=[.!?…])\s+|\n+")
# The player doing the consuming: "you uncork the draught and drink", "you
# knock it back" — "you" BEFORE the verb, so "the trees have swallowed you
# whole" (fleet4-tamsin T9) is nobody drinking anything
_PLAYER_CONSUMES = re.compile(
    r"\byou(?:\s+[\w'’-]+){0,4}?\s+(?:drink|drank|drinking|swallow\w*|gulp\w*|quaff\w*|uncork\w*"
    r"|sip\w*|knock\w*\s+(?:it\s+|the\s+[\w'’-]+\s+)?back|down(?:s|ed)?\s+(?:it|the|your|half)"
    r"|eat|ate|chew\w*|apply|applied|tip\w*\s+(?:it\s+)?back)\b",
    re.IGNORECASE,
)
# "The draught goes down bitter" — the drinking told through the item
_GOES_DOWN = re.compile(r"\b(?:goes|going|went|gone)\s+down\b", re.IGNORECASE)
_TAKEN_UP = re.compile(r"\s+(?:it|them|the\s+rest|half|every\s+drop)\b", re.IGNORECASE)
# The verb's object ends with its clause: "you eat the stew, and Piper sets a
# willow-bark draught by your bowl", "I use my knife to cut a strip of
# willow-bark" and "…as the sun goes down" consume nothing from the pack
_CLAUSE_BREAK = re.compile(
    r"[,;:—–()]|\b(?:and|but|while|as|then|with|to|until|before|after|so|when)\b", re.IGNORECASE,
)


def _clause_after(text: str, at: int, span: int = 40) -> str:
    """The words right after position `at`, up to the end of their clause."""
    return _CLAUSE_BREAK.split(text[at:at + span], 1)[0]
# Frames in which nothing was drunk: "need more than a willow-bark draught
# to close right" (fleet4-tamsin T9 — the audit's nag then had the DM drink
# her only potion unasked), "without the tonic", "if you drank it"
_NOT_CONSUMED = re.compile(
    r"\b(?:not|never|no|without|if|would|could|should|might|need|needs|needed|more\s+than"
    r"|less\s+than|rather\s+than|instead\s+of|unless|until|save|saving|later|whether)\b|n['’]t\b",
    re.IGNORECASE,
)
# What the player typed: they drink it, now, for real — never a question
_INPUT_CONSUMES = re.compile(
    r"\b(?:drink|drinks|drank|drinking|swallow\w*|gulp\w*|quaff\w*|uncork\w*|sip|sips|sipping"
    r"|knock\w*\s+(?:it\s+)?back|downs?\s+(?:it|the|my|half)|eat|eats|eating|chew\w*|apply|applies|applying"
    r"|use|uses|using|take\s+a\s+(?:swig|sip|pull|gulp|mouthful|dose))\b",
    re.IGNORECASE,
)
_INPUT_NOT_NOW = re.compile(
    r"\b(?:not|never|no|don['’]t|won['’]t|if|should|would|could|might|maybe|perhaps|later|save"
    r"|saving|keep|instead|rather|whether|until|before)\b|n['’]t\b",
    re.IGNORECASE,
)
# Every potion is a draught or a tonic: the words that tell THIS one apart
_VESSEL_NOUNS = {
    "draught", "draughts", "draft", "potion", "potions", "tonic", "tonics", "elixir", "brew", "vial",
    "flask", "salve", "tincture", "remedy", "philter", "philtre", "bottle", "dose", "drink", "ration",
    "rations", "bundle", "sprig", "pouch", "packet", "cake", "loaf", "jar", "of", "the", "a", "an",
}


def _consumables(inventory: list[Any]) -> list[dict[str, Any]]:
    return [i for i in inventory or [] if isinstance(i, dict)
            and (i.get("effect_tag") or i.get("type") == "consumable") and len(str(i.get("name") or "")) >= 4]


def _item_mention(name: str, others: list[str]) -> re.Pattern:
    """How text names this consumable: the whole name, its distinctive words
    ("willow-bark" / "willow bark" for "Willow-Bark Draught"), or its vessel
    noun after an article ("the draught") when no other consumable in the
    pack is one."""
    words = re.findall(r"[\w'’]+(?:-[\w'’]+)*", name.lower())
    distinctive = [w for w in words if w not in _VESSEL_NOUNS and len(w) >= 4]
    forms = [re.escape(name.lower())]
    if distinctive:
        forms.append(r"[-\s]+".join(re.escape(part) for w in distinctive for part in w.split("-")))
        if len(distinctive) > 1:        # "the sweetroot" for "Sweetroot Leaf Bundle"
            forms.append(r"[-\s]+".join(re.escape(part) for part in distinctive[0].split("-")))
    shared = {w for other in others for w in re.findall(r"[\w'’]+", other.lower())}
    vessels = [w for w in words if w in _VESSEL_NOUNS and len(w) >= 4 and w not in shared]
    if vessels:
        forms.append(r"(?:the|your|that|this|its)\s+(?:[\w'’-]+\s+)?(?:" + "|".join(map(re.escape, vessels)) + r")")
    return re.compile(r"\b(?:" + "|".join(forms) + r")\b", re.IGNORECASE)


def _unchanged(item: dict[str, Any], after: dict[str, Any]) -> bool:
    counts_after = {str(i.get("name", "")).lower(): i.get("quantity", 1)
                    for i in (after.get("inventory") or []) if isinstance(i, dict)}
    return counts_after.get(str(item.get("name") or "").lower()) == item.get("quantity", 1)


def _consumed_without_use(turn_text: str, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Consumables the prose says the PLAYER drank or ate whose count never moved.

    Playtest 2026-10-04: "the willow-bark draught is bitter going down" — no
    @use, so the character stayed critical with the draught still in the pack.
    The item must be named in the same sentence as the player consuming it,
    and never in a negated, comparative or hypothetical frame.
    """
    if not _CONSUME_VERB.search(turn_text or "") and not _GOES_DOWN.search(turn_text or ""):
        return []
    items = _consumables(before.get("inventory") or [])
    names = [str(i.get("name")) for i in items]
    hits = []
    for item in items:
        name = str(item.get("name"))
        mention = _item_mention(name, [n for n in names if n != name])
        for sentence in _SENTENCES.split(turn_text or ""):
            named = mention.search(sentence)
            if not named:
                continue
            # The item is what they drink: named after the verb ("you uncork
            # the draught"), or before it and taken up again ("…and drink it")
            # — "the stall smells of willow-bark and you swallow hard" is not
            span = None
            for drank in _PLAYER_CONSUMES.finditer(sentence):
                object_named = mention.search(_clause_after(sentence, drank.end()))
                if object_named:
                    span = (drank.start(), drank.end() + object_named.end())
                elif named.start() < drank.start() and _TAKEN_UP.match(sentence[drank.end():]):
                    span = (named.start(), drank.end())
                if span:
                    break
            if span is None and _GOES_DOWN.search(_clause_after(sentence, named.end())):
                span = (named.start(), named.end() + 40)
            if span is None:
                continue
            start, end = span
            # The clause it sits in counts too: "If you drank the draught now…"
            lead = re.split(r"[,;:—–]", sentence[:start])[-1]
            if _NOT_CONSUMED.search(lead + sentence[start:end]):
                continue
            if _unchanged(item, after):
                hits.append(name)
            break
    return hits


def _consumed_in_input(player_input: str, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Pack consumables the PLAYER typed that they drink, eat or use, whose
    count never moved. fleet4-bram T13: "I drink the willow-bark slow" — the
    reply said it took the edge off, wrote no @use, and he stayed critical
    with two draughts in the pack."""
    if not player_input or _ENGINE_INPUT.match(player_input) or not _INPUT_CONSUMES.search(player_input):
        return []
    items = _consumables(before.get("inventory") or [])
    names = [str(i.get("name")) for i in items]
    hits = []
    for item in items:
        name = str(item.get("name"))
        mention = _item_mention(name, [n for n in names if n != name])
        for sentence in _SENTENCES.split(player_input):
            named = mention.search(sentence)
            if sentence.rstrip().endswith("?") or not named or _INPUT_NOT_NOW.search(sentence):
                continue
            # The item is the verb's own object ("I drink the willow-bark
            # slow", "I uncork the willow-bark and drink it") — not a word in
            # the same breath ("I eat with Brann and tell him about my draught")
            if not any(mention.search(_clause_after(sentence, verb.end()))
                       or (named.start() < verb.start() and _TAKEN_UP.match(sentence[verb.end():]))
                       for verb in _INPUT_CONSUMES.finditer(sentence)):
                continue
            if _unchanged(item, after):
                hits.append(name)
            break
    return hits


# ---------------------------------------------------------------------------
# Gear the prose lost
# ---------------------------------------------------------------------------
# fleet10-bram T19: "You go down hard, shoulder first, the knife skittering
# off somewhere in the dark" — no @remove, never picked up again, and the Worn
# Knife stayed equipped: the next fight swings a blade the story lost. A thing
# in the pack, by its name or its kind (a weapon is also "the blade"), with a
# loss in its own clause — after it ("skitters off", "torn from your grip",
# "snaps in two") or before it ("you drop your knife", "knocks the blade from
# your hand") — while the pack still holds it and the reply never has them
# get it back. Someone else's ("his knife", "the wolf's fang") is not theirs
_LOST_AFTER = re.compile(
    # (its own clause: "your blade flashes, and the wolf's fang snaps" loses no blade)
    r"^(?:(?!\b(?:and|but|while|as)\b)[^.!?;:\n,—–]){0,40}?\b(?:skitter\w*"
    r"|clatter\w*\s+(?:away|off|into|across|down|out)|spin(?:s|ning)?\s+(?:away|off)"
    r"|spun\s+(?:away|off)|fl(?:y|ies|ew|ying)\s+(?:from|out\s+of|away|off)"
    r"|(?:torn|wrenched|knocked|ripped|jarred|struck|batted|kicked|flung|sent\s+flying)\s+"
    r"(?:from|out\s+of|away|loose|free)"
    r"|(?:slip(?:s|ped|ping)?|fall(?:s|ing)?|fell|drop(?:s|ped|ping)?|tumbl\w*)\s+(?:from|out\s+of)\s+your"
    r"|(?:is|are|was|were)\s+(?:gone|lost)\b(?!\s+on\b)|lost\s+(?:somewhere|in\s+the|to\s+the|for\s+good)"
    r"|gone\s+(?:somewhere|into\s+the)"
    # (the thing breaking, never the blade doing it: "the knife snaps clean
    # through the rope", "your blade shatters the wolf's skull", "your knife
    # sinks into its flank" lose nothing)
    r"|snap(?:s|ped|ping)?\s+(?:in\s+(?:two|half)|clean\s+(?:in\s+(?:two|half)|off))"
    r"|(?:break|breaks|broke|broken)\s+in\s+(?:two|half)"
    r"|shatter(?:s|ed)?(?=\s*(?:[.,;!?—–]|$)|\s+(?:in|into|against|on|to|like|apart)\b)"
    r"|(?:sink(?:s|ing)?|sank|sunk)\s+(?:into|beneath|under)\s+(?:the\s+)?(?:[\w'’-]+\s+)?(?:mud|water|river"
    r"|stream|pool|bog|mire|muck|snow|silt|murk|current|depths|deep|surface|waves?|dark|black)\b)",
    re.IGNORECASE,
)
# ...put away is not lost: "you drop the whistle into your satchel"
_PUT_AWAY = re.compile(r"^\s*(?:back\s+)?(?:into|in|inside)\s+(?:your|the|my)\s+(?:[\w'’-]+\s+)?(?:satchel|pack"
                       r"|pocket|bag|coat|belt|pouch|sheath|scabbard|boot|purse|doublet|haversack|knapsack)",
                       re.IGNORECASE)
_LOST_BEFORE = re.compile(
    r"(?:\byou\s+(?:\w+ly\s+)?(?:lose|lost|drop|dropped)|\b(?:tears?|tore|wrench(?:es|ed)?|knocks?|knocked|rips?"
    r"|ripped|bats?|batted|kicks?|kicked|snatch(?:es|ed)?|sends?|sent))\s+(?:your|the)\s+(?:[\w'’-]+\s+)?$",
    re.IGNORECASE,
)
# A near miss or a maybe ("nearly skitters away", "before it can slip from
# your grip"), and getting it back in the same reply
_NOT_LOST = re.compile(
    r"\b(?:nearly|almost|never|not|before|if|would|could|might|without|until)\b|n['’]t\b", re.IGNORECASE)
_GOT_BACK = (r"\b(?:you\s+(?:\w+ly\s+)?(?:find|found|recover\w*|retriev\w*|snatch\w*\s+(?:up|back)|scoop\w*\s+up"
             r"|pick\w*\s+(?:it|the\s+[\w'’-]+)\s+(?:back\s+)?up|grab\w*|catch|caught|fish\w*|get|got)"
             r"|your\s+(?:hand|hands|fingers|palm)\s+(?:find|finds|found|close\w*\s+(?:on|around|over)|clamp\w*))\b"
             r"[^.!?\n]{{0,30}}?\b(?:it|them|{nouns})\b")
_SOMEONE_ELSES = re.compile(r"\b(?:his|her|their|its|[\w-]+['’]s)\s+(?:[\w'’-]+\s+)?$", re.IGNORECASE)
_WEAPON_KIND = ("blade", "weapon")


def lost_gear(turn_text: str, before: dict[str, Any], after: dict[str, Any], succeeded: set[str]) -> list[str]:
    """Pack items the prose loses (dropped, knocked away, broken) that the
    pack still holds, with no @remove (see above)."""
    if not turn_text or succeeded & {"remove_from_inventory", "transfer_item"}:
        return []
    hits: list[str] = []
    for item in before.get("inventory") or []:
        if not isinstance(item, dict) or not item.get("name") or not _unchanged(item, after):
            continue
        name = str(item["name"])
        nouns = set(item_nouns(name))
        if item.get("slot") == "weapon" or item.get("type") == "weapon":
            nouns |= set(_WEAPON_KIND)
        if not nouns:
            continue
        alternatives = "|".join(map(re.escape, sorted(nouns)))
        noun_re = re.compile(rf"\b(?:{alternatives})s?\b", re.IGNORECASE)
        got_back = re.compile(_GOT_BACK.format(nouns=alternatives), re.IGNORECASE)
        for sentence in _SENTENCES.split(turn_text):
            lost = None
            for m in noun_re.finditer(sentence):
                lead = sentence[max(0, m.start() - 40):m.start()]
                # (the thing's own warmth or note — "the draught's warmth is
                # gone" — is not the thing)
                if _SOMEONE_ELSES.search(lead) or re.match(r"['’]s\b", sentence[m.end():]):
                    continue
                after_it = _LOST_AFTER.search(sentence[m.end():])
                before_it = _LOST_BEFORE.search(lead)
                if before_it and not after_it and _PUT_AWAY.match(sentence[m.end():]):
                    continue
                if not (after_it or before_it):
                    continue
                span = (sentence[m.end():m.end() + after_it.end()] if after_it else "") + " " + (
                    lead[before_it.start():] if before_it else "")
                clause = re.split(r"[,;:—–]", lead)[-1]
                if _NOT_LOST.search(clause + " " + span):
                    continue
                lost = m
                break
            if lost is None:
                continue
            # ...got back after the loss, in its own sentence too ("…slips
            # from your fingers and you catch it before it hits the ground")
            if not got_back.search(turn_text[turn_text.find(sentence) + lost.end():]):
                hits.append(name)
            break
    return hits


def _equipped(name: str, before: dict[str, Any]) -> bool:
    return any(isinstance(e, dict) and str(e.get("name") or "").lower() == name.lower()
               for e in (before.get("equipment") or {}).values())


# ---------------------------------------------------------------------------
# A wound tended in prose only
# ---------------------------------------------------------------------------
# fleet4-tamsin T18: Coldiron sent her to Maera's healer's kit; Maera "works
# the linen bandage" and "ties off the bandage with a neat, practiced knot" —
# no @health, and she stayed hurt to the end of the session. Someone ELSE
# doing the tending, a tending verb, and the dressing or the wound as its
# object: "your bandaged arm" and "the bandage on your arm" are description.
_DRESSING = r"(?:bandages?|dressings?|wounds?|cuts?|gash(?:es)?|bites?|poultices?|salve|stitches)"
# A body part only as YOUR arm, and only as the verb's direct object: "works
# her way to your side", "cleans his blade on the dead wolf's side" and "the
# wind tugs at your arm" are no dressing. Singular hand: "they bind your
# hands" is a prisoner, not a patient.
_BODY = r"(?:arm|leg|shoulder|side|ribs?|hand|forearm|thigh)"
_WOUND = rf"(?:{_DRESSING}|{_BODY})"
# Never an article or a sentence's opening word read as a name ("The wind…")
_TENDER = (r"(?!(?:the|a|an|it|its|this|that|then|there|when|as|but|and)\b)"
           r"(?:he|she|they|his\s+hands|her\s+hands|their\s+hands|(?-i:[A-Z][a-z]+)(?:['’]s\s+hands)?)")
# The wound is the verb's own object, a few plain words on at most ("working
# quick and rough at the bandage"): "works the room, eyeing your bandaged
# arm" is not a dressing. Never the player tending themselves.
_TENDS = re.compile(
    rf"\b(?!(?:you|your|i)\b){_TENDER}\s+(?:[\w'’,-]+\s+){{0,6}}?"
    r"(?:ti(?:es|ed|ing)\s+off|tying\s+off|finish\w*\s+tying|wrap\w*|wind(?:s|ing)?|bind(?:s|ing)?|"
    r"bound|clean(?:s|ed|ing)?|stitch\w*|sew(?:s|ed|ing)?|dress(?:es|ed|ing)?|tend(?:s|ed|ing)?|"
    r"pack(?:s|ed|ing)?|work(?:s|ed|ing)?|daub\w*|smear\w*|splint\w*|salv(?:es|ed|ing))"
    rf"(?:(?:\s+[\w'’-]+){{0,3}}?\s+(?:(?:at|on|to)\s+)?(?:the|your|a|that)\s+"
    rf"(?:[\w'’-]+\s+){{0,2}}?{_DRESSING}"
    rf"|(?:\s+(?:up|off))?\s+your\s+(?:[\w'’-]+\s+){{0,2}}?{_BODY})\b",
    re.IGNORECASE,
)
_TENDED = re.compile(
    rf"\byour\s+(?:[\w'’-]+\s+){{0,2}}?{_WOUND}\s+(?:is|are|has\s+been|have\s+been|gets?)\s+"
    r"(?:cleaned|dressed|bandaged|stitched|bound|wrapped|splinted|salved|sewn)\b",
    re.IGNORECASE,
)
_HEALTH_LADDER = ("dead", "critical", "badly_hurt", "hurt", "winded", "healthy")
_HEAL_TOOLS = {"update_player_health", "use_item", "rest"}


def _tended_without_health(prose: str, before: dict[str, Any], after: dict[str, Any],
                           succeeded: set[str]) -> str | None:
    """The tending sentence, when someone dressed the player's wound and
    nothing mechanical followed."""
    health = before.get("health")
    if health in (None, "healthy", "dead") or health != after.get("health") or succeeded & _HEAL_TOOLS:
        return None
    m = _TENDS.search(prose or "") or _TENDED.search(prose or "")
    return m.group(0) if m else None


def _one_step_better(health: str | None) -> str:
    try:
        return _HEALTH_LADDER[min(_HEALTH_LADDER.index(str(health)) + 1, len(_HEALTH_LADDER) - 1)]
    except ValueError:
        return "winded"


# ---------------------------------------------------------------------------
# What a scene object holds, told before anyone used it
# ---------------------------------------------------------------------------
# The brief shows the DM what each of "Things here" reads or holds, so the
# reveal can be exact when the player uses it. fleet4-tamsin T9: the unread
# hut carving's "SING" turned up in prose as a memory; fleet4-wenna T12: the
# untouched whistle was "split, silent, the make too familiar" — its DM-only
# description. Three tells: a payload's ALL-CAPS word, its quoted text, or
# LEAK_OVERLAP of its content words packed within LEAK_WINDOW words of the
# prose — a paraphrase clusters, a scene that merely shares the world's
# vocabulary ("Coldiron watches you go… a debt he can't pay", against the
# notice's "The Watch pays… — Capt. B. Coldiron") does not. Its own label's
# words and the names in it (people, places, factions) are public.
LEAK_OVERLAP = 3
LEAK_WINDOW = 8
_CAPS_WORD = re.compile(r"(?<![\w'’-])[A-Z][A-Z'’-]{2,}(?![\w'’-])")
_CAPS_COMMON = {"THE", "AND", "FOR", "BUT", "NOT", "YOU", "HIS", "HER", "ITS", "ARE", "WAS", "WITH",
                "FROM", "THIS", "THAT", "NOW", "ALL", "ONE", "TWO", "OLD", "NEW"}
_PAYLOAD_QUOTE = re.compile(r"(?:^|(?<=[\s(—–-]))[\"“'‘]([^\"”“\n]{6,200}?)[\"”'’](?![a-z])")
_PAYLOAD_FILLER = {
    "down", "under", "same", "never", "ever", "once", "only", "still", "like", "into", "onto", "over",
    "than", "then", "there", "here", "where", "when", "with", "without", "each", "every", "been", "being",
    "made", "would", "could", "should", "might", "must", "will", "shall",
}
# Objects whose contents are out — used, or already leaked and warned about
# once — keyed by player: "location_id/object_id". A read object stays
# unconsumed after reading, so `consumed` alone cannot say it was seen. In
# memory: after a restart a re-quoted notice can be flagged one more time.
_OBJECTS_OUT: dict[str, set[str]] = {}


def mark_object_seen(player_id: str | None, location_id: str | None, object_id: str | None) -> None:
    """A canvas [E] on a thing: what it reads or holds is out. Keyed by
    where it stands — the next typed turn may start in another room, where
    `unseen_scene_objects` never looks at this one."""
    if player_id and location_id and object_id:
        _OBJECTS_OUT.setdefault(player_id, set()).add(f"{location_id}/{object_id}")


def unseen_scene_objects(player_id: str, location_id: str | None,
                         touched: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The authored objects at `location_id` whose contents the player has
    not seen: unconsumed, never used. `touched` is free text naming what was
    used this turn (the reply's successful @interact lines, the canvas
    ledger's summaries) — an object whose label or id appears in it is seen.
    The rows carry `_out_key` so a flagged leak is not flagged twice."""
    if not player_id or not location_id:
        return []
    from src.models import Location, get_session
    from src.services.scene_objects import normalize_objects

    with get_session() as session:
        location = session.get(Location, location_id)
        objects = normalize_objects(location.objects) if location is not None else []
    out = _OBJECTS_OUT.setdefault(player_id, set())
    used = [str(t).lower() for t in touched if t]
    unseen = []
    for obj in objects:
        key = f"{location_id}/{obj['id']}"
        if any(obj["label"].lower() in t or obj["id"].lower() in t for t in used):
            out.add(key)
        if obj["kind"] in ("body", "rest") or obj["consumed"] or key in out:
            continue
        unseen.append({**obj, "_out_key": (player_id, key)})
    return unseen


_INITIAL = re.compile(r"(?:[A-Z]|Capt|Cpt|Sgt|Mr|Mrs|Ms|Dr|St)\.")


def _names_in(text: str) -> set[str]:
    """Lower-cased capitalised words that are not opening a sentence: names
    ("the Watch", "Capt. B. Coldiron", "Ask Piper")."""
    names: set[str] = set()
    for m in re.finditer(r"[A-Z][a-z'’-]+", text or ""):
        before = text[:m.start()]
        if not before.strip() or re.search(r"[\"“'‘(\n]\s*$", before):
            continue
        prev = re.search(r"(\S+)\s+$", before)
        if prev and re.search(r"[.!?:;—–]$", prev.group(1)) and not _INITIAL.fullmatch(prev.group(1)):
            continue
        names.add(re.split(r"['’]", m.group(0).lower())[0])
    return names


def _document_phrases(reads: str) -> list[str]:
    """A document's tells: its runs of four words, three of them its own
    content words ("road before the frost"). Its words loose are no tell — the
    seed letter shares "road", "cart", "three" and "market" with every reply
    of a wolf story, and "The north road has eaten three carts this month;
    the market will be thin come frost" read as the unopened satchel's
    letter (and marked it seen for good)."""
    words = _words(reads or "")
    out: list[str] = []
    for i in range(len(words) - 3):
        run = words[i:i + 4]
        own = sum(1 for w in run if len(w) >= 4 and w not in _GENERIC_WORDS and w not in _PAYLOAD_FILLER)
        phrase = " ".join(run)
        if own >= 3 and phrase not in out:
            out.append(phrase)
    return out


def _payload_tells(obj: dict[str, Any]) -> tuple[set[str], list[str], list[str]]:
    payload = obj.get("payload") or {}
    item = payload.get("item") or {}
    prose_parts = " ".join(str(x) for x in (payload.get("text"), item.get("description")) if x)
    text = " ".join(x for x in (prose_parts, str(item.get("name") or "")) if x)
    public = _text_forms(str(obj.get("label") or ""))
    # A capitalised word mid-sentence is a name — "the Watch", "Capt. B. Coldiron"
    public |= _names_in(prose_parts)
    caps = {w for w in _CAPS_WORD.findall(f"{text} {item.get('reads') or ''}") if w not in _CAPS_COMMON}
    quotes = [" ".join(_words(q)) for q in _PAYLOAD_QUOTE.findall(str(payload.get("text") or ""))
              if len(q.split()) >= 3]
    # A document's own words too: the brief shows the DM the satchel letter's
    # text so @interact can quote it — not so the prose can tell it unopened
    quotes += _document_phrases(str(item.get("reads") or ""))
    words: list[str] = []
    for w in _words(text):
        if (len(w) >= 4 and w not in _GENERIC_WORDS and w not in _PAYLOAD_FILLER and w not in words
                and w not in public and not (_word_forms(w) & public)):
            words.append(w)
    return caps, quotes, words


def _clustered(sentence: str, words: list[str]) -> list[str]:
    """The payload words packed inside one LEAK_WINDOW of `sentence`, if
    LEAK_OVERLAP of them are."""
    tokens = [_word_forms(t) for t in _words(sentence)]
    for start in range(max(1, len(tokens) - LEAK_WINDOW + 1)):
        forms = set().union(*tokens[start:start + LEAK_WINDOW]) if tokens else set()
        shared = [w for w in words if _word_forms(w) & forms]
        if len(shared) >= LEAK_OVERLAP:
            return shared
    return []


def _object_contents_leaked(prose: str, spoken_text: str,
                            objects: list[dict[str, Any]],
                            player_input: str = "") -> list[tuple[dict[str, Any], str]]:
    """(object, tell) for each unseen object whose contents reached the
    prose or someone's mouth. The word overlap reads the prose only: a
    captain talking about stolen carts knows it without the notice board —
    and prose echoing what was said aloud or typed this turn ("Third cart
    this month") is the talk, not the board."""
    said = f"{prose}\n{spoken_text}"
    said_words = " ".join(_words(said))
    sentences = [s for s in _SENTENCES.split(prose or "") if s.strip()]
    heard: set[str] = set()
    for w in _words(f"{spoken_text}\n{player_input}"):
        heard |= _word_forms(w)
    leaks = []
    for obj in objects or []:
        if not isinstance(obj, dict) or obj.get("kind") in ("body", "rest"):
            continue
        caps, quotes, words = _payload_tells(obj)
        words = [w for w in words if not (_word_forms(w) & heard)]
        tell = next((w for w in sorted(caps) if re.search(rf"(?<![\w'’-]){re.escape(w)}(?![\w'’-])", said)), None)
        if tell is None:
            tell = next((f'"{q}"' for q in quotes if q and re.search(rf"\b{re.escape(q)}\b", said_words)), None)
        if tell is None and len(words) >= LEAK_OVERLAP:
            for sentence in sentences:
                shared = _clustered(sentence, words)
                if shared:
                    tell = ", ".join(shared[:4])
                    break
        if tell is not None:
            leaks.append((obj, tell))
    return leaks


# A MUST-DELIVER beat that orders a Thing's contents told has ordered the
# reveal: fleet8-bram T5 and fleet8-tamsin T15 delivered 'the word SING
# carved beneath them exactly as written' as the beat said, and
# object_contents_leak told the DM never to mention SING again.
_LABEL_HEAD_CUT = re.compile(r"\s+(?:on|of|in|at|by|under|near|beside|from)\s+", re.IGNORECASE)


def beat_named_objects(objects: Iterable[dict[str, Any]], directive: str | None) -> list[dict[str, Any]]:
    """The objects a beat directive names: what it reads or holds (an
    all-caps word, a quoted line), its label's head noun ("the scratches"),
    or its item's ("the whistle"). The server leaves these out of this
    turn's object_contents_leak and counts them as seen."""
    text = str(directive or "")
    if not text.strip():
        return []
    forms = _text_forms(text)
    joined = " ".join(_words(text))
    named = []
    for obj in objects or []:
        if not isinstance(obj, dict) or obj.get("kind") in ("body", "rest"):
            continue
        caps, quotes, _ = _payload_tells(obj)
        if any(re.search(rf"(?<![\w'’-]){re.escape(w)}(?![\w'’-])", text) for w in caps) \
                or any(q and re.search(rf"\b{re.escape(q)}\b", joined) for q in quotes):
            named.append(obj)
            continue
        item = (obj.get("payload") or {}).get("item") or {}
        heads = [_words(_LABEL_HEAD_CUT.split(str(obj.get("label") or ""), maxsplit=1)[0])[-1:],
                 _words(re.split(r"\s+of\s+", str(item.get("name") or ""), maxsplit=1)[0])[-1:]]
        if any(h and len(h[0]) >= 4 and h[0] not in _GENERIC_WORDS and _word_forms(h[0]) & forms for h in heads):
            named.append(obj)
    return named


# ---------------------------------------------------------------------------
# Money and objects changing hands in prose only
# ---------------------------------------------------------------------------
# Playtest fleet-bram T20: "Hesk's hand closes around the ten coin" — no @buy,
# the purse stayed at 85 and the knife never arrived. fleet-bram T13/T16 and
# fleet-tamsin T17: the proof branch and the horn were handed to Coldiron in
# prose while the player kept carrying (and wielding) them. Both checks read
# the prose with quoted speech removed — "Ten coin and it's yours" is an
# offer, not a sale. The coin must be the OBJECT of the verb, never just a
# word somewhere after it: "you set the mug down beside the coins" and "you
# pay him no mind, watching the coins" are not payments.

_COUNT = (
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen"
    r"|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|few|several|couple\s+of)"
)
# A currency word as a NOUN: "coin-bright eyes", "coin purse", "the crowns of
# the trees" and "the silver locket" are not money; "gold pieces" and a bare
# "the silver." are. Never the first of two objects either — in a noir world
# "you hand the coppers your papers" is the police.
_COIN_NOUN = (
    r"(?:(?:coins?|coppers|crowns|pennies|pence|shillings?|florins?|ducats?|sovereigns?|groats?)"
    r"(?![\w'’-])(?!\s+(?:purse|pouch|bag|box|jar|belt|of)\b)"
    r"|(?:gold|silver|copper)\s+(?:pieces?|coins?)(?![\w'’-])"
    r"|(?:gold|silver|copper)(?=\s*(?:[.,;:!?—–)]|$)|\s+(?:into|onto|in|on|to|across|over|from"
    r"|and|for|with|without|back)\b))"
    r"(?!\s+(?:the|a|an|your|his|her|their|my|its)\b)"
)
_COIN_PHRASE = (
    rf"(?:\b(?:{_COUNT}|a|an|some|the|your|his|her|their|my|that|those|last)\s+"
    rf"(?:(?!of\b)[\w'’-]+\s+){{0,2}}{_COIN_NOUN}"
    rf"|\b(?:a|the|your|his|her|their)\s+(?:[\w'’-]+\s+)?"
    rf"(?:pouch|purse|handful|fistful|stack|pile|bag|scatter)\s+of\s+(?:[\w'’-]+\s+)?{_COIN_NOUN})"
)
# Who the player pays: "you pay the innkeeper eight coins", "you slip Piper a coin"
_PAYEE = r"(?:him|her|them|the\s+[\w'’-]+|(?-i:[A-Z][\w-]+(?:\s+[A-Z][\w-]+)?)(?![\w'’-]))"
_MONEY_CLAIMS = [
    # The player pays: "you hand over ten coin", "you slide eight coins across".
    # Not "flip" — a coin walked across the knuckles is a habit, not a sale.
    (re.compile(
        r"\byou\s+(?:\w+ly\s+)?(?:pay|hand\s+over|hand|count\s+out(?!\s+your\b)|slide|slip|push|press"
        r"|toss|drop|lay|put|place|set|dig\s+out|fish\s+out|part\s+with|give)"
        rf"(?:\s+(?:over|out|down|across))?(?:\s+{_PAYEE})?\s+{_COIN_PHRASE}",
        re.IGNORECASE,
    ), "pays"),
    # The player is paid: "he pays you ten coin", "counts eight coin into your palm".
    # Not "hands you back your coins" — a refused payment leaves the purse alone.
    (re.compile(
        rf"\b(?:pays|hands|gives|passes|tosses|slides|flips|pushes)\s+you\s+{_COIN_PHRASE}"
        rf"|\bcounts\s+out\s+{_COIN_PHRASE}[^.!?;\n]{{0,20}}?\b(?:for|to)\s+you\b"
        # ...and the gerunds, bare coin too: "already pressing coin and a
        # second vial into your hands" (fleet6-tamsin T11)
        rf"|\b(?:counts|presses|drops|puts|places|tips|pours|slips|tucks|folds|pushes|sets|lays"
        r"|counting|pressing|dropping|putting|placing|tipping|pouring|slipping|tucking|folding|pushing"
        r"|setting|laying|sliding)\s+"
        # never "your coins" — "tucking your coins into your purse" is their
        # own money put away (a gerund's subject is often the player)
        rf"(?!your\b)(?:{_COIN_PHRASE}|{_COIN_NOUN})(?:(?!\bback\b)[^.!?;\n]){{0,30}}?\b(?:into|in|onto)\s+your\s+"
        r"(?:[\w'’-]+\s+)?(?:hand|hands|palm|palms|purse|pocket|fist|fingers|grip)\b"
        # "The coin lands light in your pocket" — but "your coins clink in your
        # purse" is the player's own money jingling, never a payment
        rf"|(?<!\byour\s)\b{_COIN_NOUN}\s+(?:lands?|drops?|falls?|settles?|spills?|goes|slides|slips)\s+"
        r"(?:[\w'’-]+\s+){0,2}?(?:in|into)\s+your\b",
        re.IGNORECASE,
    ), "paid"),
    # Someone takes the player's money: "closes around the ten coin", "takes
    # the coins" — any tense of close, and how: fleet6-tamsin T19 "The
    # Keeper's fingers close over the coin" (20 coin paid, no @coin -20), T12
    # "Piper's hand closes tight around the coin" (her split, no @coin)
    (re.compile(
        r"\b(?:takes|pockets|accepts|sweeps\s+up|scoops\s+up|palms|snatches|collects"
        r"|(?:close|closes|closed|closing)\s+(?:(?:\w+ly|tight|tighter|hard|firm|fast|quick)\s+){0,2}"
        r"(?:around|over|on))\s+"
        rf"(?:your|the)\s+(?:{_COUNT}\s+)?(?:(?!of\b)[\w'’-]+\s+)?{_COIN_NOUN}",
        re.IGNORECASE,
    ), "taken"),
    # Either direction over a counter: "slides eight coin across"
    (re.compile(
        r"\b(?:slides?|sliding|pushes|pushing|counts?\s+out|counting\s+out|stacks?|stacking"
        r"|sets?|setting|lays?|laying)\s+"
        rf"{_COIN_PHRASE}[^.!?;\n]{{0,15}}?\bacross\b",
        re.IGNORECASE,
    ), "pays"),
]
# "If you pay him ten coin, he talks" / "unless he pays you" — not yet
_HYPOTHETICAL_BEFORE = re.compile(
    r"\b(?:if|unless|could|would|should|might|must|can|cannot|to|whether|until|before)\s+"
    r"(?:[\w'’-]+\s+)?$",
    re.IGNORECASE,
)
# "you put the coins back in your purse", "flip a coin across your knuckles",
# "toss a coin into the air" — the player's money going nowhere
_OWN_MONEY_AFTER = re.compile(
    r"^[^.!?;\n]{0,25}?\b(?:back|away|(?:into|in|inside|across|over|along|between)\s+your"
    r"|(?:in|into|up)\s+(?:the\s+)?air)\b",
    re.IGNORECASE,
)
# "Your hand closes around the coins in your pocket"; "Hesk takes the coins from
# the dead man's purse" — the player's own grip, or somebody else's money. A
# counter is where the player's coin waits, so taking it from there still counts.
# Nor coin lying on someone's own cart or in their till: "Hesk's fingers close
# over the coins on his cart" is his takings (the round-5 "close over" widening)
_OWN_GRIP_BEFORE = re.compile(r"\byour\s+(?:[\w'’-]+\s+)?(?:hand|hands|fingers|fist|palm)\s+$", re.IGNORECASE)
_NOT_PLAYERS_COIN_AFTER = re.compile(
    r"^[^.!?;\n]{0,25}?\b(?:(?:in|inside)\s+your"
    r"|(?:from|off)\s+(?:the|his|her|their|its|a|an)\b(?!\s+(?:counter|table|bar|board|desk|plank|tray)\b)"
    r"|(?:on|under|beneath)\s+(?:his|her|their|its)\b"
    r"|in\s+(?:his|her|their|its)\s+(?:own\s+)?(?:till|cashbox|cart|stall|lap)\b)",
    re.IGNORECASE,
)
_MONEY_TOOLS = {"adjust_currency", "transfer_item"}


# A coin the player CARRIES as a thing — fleet5-bram's Greywater Silver Piece,
# the clue from the Hollow's crate. "Coldiron takes the coin from your shaking
# hand... pockets the coin himself" (T16) was that item changing hands, and
# money_without_command told the DM to write @coin -N for it. "The coin",
# "your silver piece" is the carried one; "ten coin" and "a coin" are money.
_PACK_COIN = re.compile(
    r"\b(?:coins?|penny|pennies)\b|\b(?:silver|gold|copper|bronze|brass|minted)\s+(?:pieces?|marks?)\b",
    re.IGNORECASE,
)
_COUNTED = re.compile(rf"\b{_COUNT}\b", re.IGNORECASE)
_THE_ONE_COIN = re.compile(
    r"\b(?:the|your|that)\s+(?:[\w'’-]+\s+)?(?:coin|piece|penny|silver|gold|copper)(?![\w'’-])", re.IGNORECASE
)


def _already_carried(phrase: str, before: dict[str, Any]) -> bool:
    """True when `phrase` names an item the player already carried as the
    turn began ("the Yarrow Tincture" with a Yarrow Tincture in the pack)."""
    text = phrase.lower()
    for item in before.get("inventory") or []:
        name = str(item.get("name") if isinstance(item, dict) else item or "").strip().lower()
        if len(name) >= 4 and name in text:
            return True
    return False


def _carried_coins(before: dict[str, Any]) -> list[str]:
    """Pack items that are a coin (a clue, a keepsake), by name."""
    return [str(i.get("name")) for i in before.get("inventory") or []
            if isinstance(i, dict) and _PACK_COIN.search(str(i.get("name") or ""))]


def _money_claim(prose: str, carried_coin: bool = False) -> str | None:
    """The first sentence fragment that moves coin to or from the player.
    `carried_coin`: the pack holds a coin as an item, so the player handing
    over (or someone taking) THE coin is that item, not money."""
    for pattern, kind in _MONEY_CLAIMS:
        for m in pattern.finditer(prose):
            before = prose[max(0, m.start() - 30):m.start()]
            after = prose[m.end():m.end() + 40]
            if _HYPOTHETICAL_BEFORE.search(before):
                continue
            if kind == "pays" and _OWN_MONEY_AFTER.search(after):
                continue
            if kind == "taken" and (_OWN_GRIP_BEFORE.search(before) or _NOT_PLAYERS_COIN_AFTER.search(after)):
                continue
            if (carried_coin and kind in ("taken", "pays") and _THE_ONE_COIN.search(m.group(0))
                    and not _COUNTED.search(m.group(0))):
                continue
            return m.group(0).strip()
    return None


# Words that follow "you give him ..." or "takes ... from your hand" without
# being an object: gestures, speech, favours, sensations, debris, body parts,
# figurative stakes, and money (the money check owns coin). A blacklist — an
# item whitelist would never end — grown from probing ordinary DM prose.
_NOT_AN_ITEM = {
    # gestures and looks
    "look", "glance", "nod", "shrug", "smile", "grin", "wink", "wave", "stare", "frown",
    "scowl", "salute", "bow", "once-over", "laugh", "sigh", "smirk", "glare", "glower", "gaze",
    "side-eye", "thumbs-up", "half-smile", "handshake", "clap", "embrace", "push", "shove",
    "nudge", "pat", "slap", "squeeze", "kiss", "hug", "tug", "yank", "kick", "tap", "knock",
    "shake", "wipe", "stir", "rap", "jiggle", "study", "pull", "pause", "gesture", "signal",
    "sign",
    # time and room
    "moment", "second", "minute", "while", "hour", "day", "night", "time", "chance", "break",
    "room", "space", "way", "berth", "edge", "floor", "slack", "lift", "ride", "rest",
    # words, information, verdicts
    "word", "warning", "answer", "reply", "name", "message", "news", "password", "story",
    "truth", "lie", "report", "account", "details", "directions", "thought", "idea", "promise",
    "reason", "hint", "try", "go", "go-ahead", "version", "rundown", "earful", "update",
    "all-clear", "gist", "lowdown", "fact", "facts", "address", "location", "description",
    "line", "point", "question", "answer", "advice", "opinion", "verdict", "order",
    "ultimatum", "threat", "speech", "demand", "offer", "terms", "apology", "toast",
    "conversation", "greeting", "compliment", "thanks", "respects", "regards",
    # favours, feelings, stakes
    "benefit", "credit", "attention", "trust", "blessing", "slip", "reins", "problem",
    "choice", "job", "task", "burden", "weight", "lead", "courtesy", "honour", "honor",
    "privilege", "satisfaction", "pleasure", "impression", "sense", "feeling", "assurance",
    "favour", "favor", "kindness", "run", "matter", "courage", "hope", "comfort",
    "confidence", "strength", "peace", "heart", "permission", "support", "encouragement",
    "reassurance", "life", "fate", "future", "decision", "mess", "investigation", "case",
    "keeping", "care", "safety", "blow", "punch", "strike",
    # sensations and debris ("the fire takes the chill from your fingers")
    "chill", "cold", "warmth", "heat", "ache", "sting", "pain", "tremor", "numbness",
    "stiffness", "tension", "splinter", "thorn", "burr", "mud", "dirt", "dust", "blood",
    "ash", "grime", "sweat", "water", "rain", "snow",
    # body parts
    "hand", "back", "shoulder", "face", "cheek", "head", "eye", "ear", "palm", "fist",
    "finger", "fingertip", "thumb", "knuckle", "elbow", "arm", "forearm", "wrist", "knee",
    "lip", "lips", "mouth", "nose", "chin", "brow", "forehead", "hair", "piece", "bit",
    # money
    "coin", "copper", "crown", "purse", "money", "bribe", "tip", "payment", "fee", "toll",
    "price", "cut", "share", "wage", "wages", "pay", "debt",
}
# Served and finished in the same scene, never carried — the money check covers the price
_SERVED = {
    "mug", "tankard", "cup", "glass", "ale", "beer", "wine", "mead", "cider", "drink",
    "bowl", "plate", "stew", "soup", "meal", "bottle", "flagon", "jug", "pint", "dram",
    "shot", "tea", "broth", "porridge",
}
# Shown and kept: a pass is held up at a gate, then waved through — a player
# "handing over" one is not giving it away
_SHOWN = {
    "pass", "papers", "writ", "warrant", "badge", "token", "seal", "permit", "license",
    "licence", "credentials", "sigil", "signet",
}
# Props every NPC owns: "Sorrel takes the lantern" is usually her own lantern
_COMMON_PROPS = {
    "lantern", "torch", "lamp", "candle", "rope", "cloak", "coat", "hat", "blanket", "bedroll",
    "flask", "waterskin", "bread", "loaf", "knife", "bag", "pack", "satchel", "map", "book", "pipe",
}
# Where an object phrase stops: "the branch again and he takes it" -> "the branch"
_OBJECT_STOP = {
    "again", "and", "back", "to", "from", "with", "into", "onto", "over", "across",
    "without", "as", "like", "before", "after", "then", "but", "in", "on", "for", "while", "so",
}


def _in_vocab(word: str, *vocabs: set[str]) -> bool:
    w = word.lower().strip("'’")
    return any(w in v or (w.endswith("s") and w[:-1] in v) for v in vocabs)


# Up to five words, commas allowed: "a long, hard look" must reach its noun.
# What the player hands OVER is definite — "the branch", "your pass" — which
# keeps "you give him a dry laugh" out by construction.
_OBJ = r"(?P<obj>(?:the|your|a|an|his|her|their|its)\s+[\w'’-]+(?:,?\s+[\w'’-]+){0,4})"
_OBJ_DEF = r"(?P<obj>(?:the|your)\s+[\w'’-]+(?:,?\s+[\w'’-]+){0,4})"
# A person, never a place: "to Coldiron" yes, "to Coldiron's office" no
_PERSON = r"(?:him|her|them|[A-Z][\w-]+(?:\s+[A-Z][\w-]+)?(?![\w'’-]))"
_HOLDING = r"(?:hand|hands|palm|palms|fingers|grip|grasp|fist)"

# Case-sensitive on purpose: a capitalised recipient is a person's name
_ITEM_GIVEN = [
    # "you hand him the branch" / "you give Coldiron the horn" / "you hand the
    # guard the letter" — "pass" and "throw" only to a person: "you pass the
    # smithy the long way round" is a walk
    re.compile(
        rf"\b[Yy]ou\s+(?:\w+ly\s+)?(?:(?:hand|give|slip|toss)\s+(?:{_PERSON}|the\s+[a-z][\w'’-]+)"
        rf"|(?:pass|throw)\s+{_PERSON})\s+{_OBJ_DEF}\b"
    ),
    # "you hand the pages over to Piper" / "you slide the pages across to Piper"
    # — but "you slide the chair over to Piper" and "you push the door open to
    # Coldiron" are furniture, so slide/push need "across"
    re.compile(
        rf"\b[Yy]ou\s+(?:\w+ly\s+)?(?:hand|give|slip|toss|return|deliver)\s+{_OBJ_DEF}\s+"
        rf"(?:over\s+|across\s+)?to\s+{_PERSON}"
    ),
    re.compile(rf"\b[Yy]ou\s+(?:\w+ly\s+)?(?:slide|push|press)\s+{_OBJ_DEF}\s+across\s+to\s+{_PERSON}"),
    # "you press the letter into her hand"
    re.compile(
        rf"\b[Yy]ou\s+(?:\w+ly\s+)?(?:hand|give|slip|press|slide|toss|push)\s+{_OBJ_DEF}\s+"
        rf"into\s+(?:his|her|their)\s+(?:[\w'’-]+\s+)?{_HOLDING}\b"
    ),
    # "you hand over the horn"
    re.compile(rf"\b[Yy]ou\s+(?:\w+ly\s+)?hand\s+over\s+{_OBJ_DEF}\b"),
    # "Coldiron takes the branch from your hand" / "takes the pages from you" —
    # not "pulls"/"lifts": those remove splinters and burrs, not belongings;
    # and "takes the stool across from you" is a seat, not a handover
    re.compile(
        rf"\b(?:takes|accepts|snatches|plucks|grabs|wrests|eases|prises|pries|receives)\s+{_OBJ_DEF}"
        rf"\s+(?:[^.!?;\n]{{0,20}}?\s)?(?<!across\s)(?<!away\s)from\s+(?:you\b|your\s+(?:[\w'’-]+\s+)?"
        rf"(?:{_HOLDING}|belt|pack|satchel|coat)\b)"
    ),
]
# "presses the pass into your hand" is literal; "puts his life in your hands"
# (plural, no "into") is the figurative idiom and never matches
_ITEM_RECEIVED = [
    # "He presses the pass into your ink-stained hand"
    re.compile(
        rf"\b(?:presses|puts|places|drops|slips|tucks|folds|closes|sets|pushes|lays)\s+{_OBJ}\s+"
        rf"(?:[^.!?;\n]{{0,20}}?\s)?(?:(?:into|onto)\s+your\s+(?:[\w'’-]+\s+)?{_HOLDING}"
        rf"|in\s+your\s+(?:[\w'’-]+\s+)?(?:hand|palm|fingers|fist|grip|grasp))\b"
    ),
    # "She hands the pass over to you"
    re.compile(rf"\b(?:hands|gives|passes|tosses)\s+{_OBJ}\s+(?:over\s+)?to\s+you\b"),
    # "The scrap goes into your coat beside the gate pass" (fleet6-bram T5:
    # Piper's charcoal parchment pocketed with no @give, marked on at T7,
    # never in the pack) — "back into your coat" is their own thing put away
    re.compile(
        rf"\b{_OBJ}\s+(?:goes|go|went|slides|slips|disappears|vanishes)\s+(?:(?:straight|safely|neatly)\s+)?"
        r"into\s+your\s+(?:[\w'’-]+\s+)?(?:coat|pocket|pack|satchel|bag|belt|pouch|jerkin|doublet|cloak"
        r"|shirt|sleeve|purse)\b",
        re.IGNORECASE,
    ),
]
# The receiving forms item_without_tool always knew, now with the object
# checked: "he gives you a nod" and "presses a kiss into your palm" are not items
_ITEM_GIVEN_TO_YOU = [
    re.compile(rf"\b(?:hands|gives|passes|tosses|slides)\s+you\s+{_OBJ}", re.IGNORECASE),
    re.compile(rf"\bpresses\s+{_OBJ}[^.!?\n]{{0,30}}?\binto\s+your\s+(?:hand|palm)\b", re.IGNORECASE),
]
# Goods across a counter are only a handover when a payment happened too
# (fleet-wenna T20: @coin -3, "sliding a wrapped loaf across the counter", no bread)
_ITEM_ACROSS_COUNTER = re.compile(
    rf"\b(?:slides|sliding|pushes|pushing|sets|setting)\s+{_OBJ}\s+across\s+(?:the\s+)?"
    r"(?:counter|table|bar|board|desk|plank)\b"
)
# ...unless it is a thing one keeps: a key, a pass, a letter slid across the
# bar is a hand-over whether or not coin moved (fleet9-bram T18)
_KEEPSAKES = frozenset({
    "key", "pass", "token", "letter", "note", "map", "chart", "paper", "page", "writ", "seal", "badge", "ring",
    "locket", "pendant", "charm", "scroll", "envelope", "packet", "parcel", "permit", "ticket",
})
# ...and slid across to be read or signed, it is shown, not given ("The clerk
# slides a ledger across the desk so you can read it")
_SHOWN_ACROSS = re.compile(
    r"^[^.!?\n]{0,40}?\b(?:so\s+(?:that\s+)?you\s+can|for\s+you\s+to|to\s+let\s+you|for\s+your)\s+"
    r"(?:read|see|look|check|inspect|study|sign|examine|eyes|signature|mark)\b",
    re.IGNORECASE,
)


def _keepsake(phrase: str) -> bool:
    """Is the head noun of an object phrase a thing one keeps ("a small brass
    key", "the folded letter"), not a thing served or shown?"""
    words = [w.lower().strip("'’") for w in re.findall(r"[A-Za-z][\w'’-]*", phrase or "")]
    if not words:
        return False
    heads = {words[-1]} | ({words[words.index("of") - 1]} if "of" in words[1:] else set())
    return any(h in _KEEPSAKES or (h.endswith("s") and h[:-1] in _KEEPSAKES) for h in heads)


def _slid_keepsake(prose: str) -> str | None:
    """The thing one keeps a reply slid across a counter to the player, not
    shown to be read and not handed back (see `_KEEPSAKES`), or None."""
    for m in _ITEM_ACROSS_COUNTER.finditer(prose or ""):
        if _HYPOTHETICAL_BEFORE.search(prose[max(0, m.start() - 30):m.start()]) \
                or _SHOWN_ACROSS.search(prose[m.end():m.end() + 80]):
            continue
        phrase = _object_phrase(m.group("obj"))
        if phrase and _keepsake(phrase) and not _given_back(prose[m.end():], phrase):
            return phrase
    return None


# A room the player asked for, handed over in the prose, is a night's sleep —
# and that is @rest. fleet9-wenna T20: "I set down three coin for the room",
# the Keeper "takes the coin… sliding it into an apron pocket" and sets "a key
# on the bar": no @rest, no @coin, and the inn's price was 8, not 3
_ROOM_GIVEN = re.compile(
    r"\byour\s+room\b|\broom['’]s\s+yours\b|\broom\s+is\s+yours\b"
    r"|\bshows?\s+you\s+(?:up\s+)?(?:to|toward|towards)\s+(?:a|your|the)\s+(?:room|bed)\b"
    r"|\bkeys?\b[^.!?\n]{0,50}\b(?:on|across|onto)\s+the\s+(?:bar|counter|table)\b"
    r"|\b(?:on|across|onto)\s+the\s+(?:bar|counter|table)\b[^.!?\n]{0,30}\bkeys?\b"
    r"|\bkeys?\b[^.!?\n]{0,40}\b(?:room|upstairs|stairs)\b"
    r"|\b(?:takes|taking|pockets|pocketing|sweeps|sweeping|scoops|scooping|accepts|accepting|collects"
    r"|collecting)\s+(?:the|your)\s+(?:[\w'’-]+\s+)?coins?\b",
    re.IGNORECASE,
)


def room_without_rest(prose: str, commands: Iterable[Any], player_input: str,
                      location_id: str | None) -> dict[str, str] | None:
    """The player asked for a room where one can be had, the reply handed it
    over (a key on the bar, "your room", their coin taken) and wrote no
    @rest, @coin or @buy: the warning, else None. Never raises."""
    try:
        from src.services.intents import asks_for_room

        asked = asks_for_room(player_input)
        if not asked or not location_id:
            return None
        rows = list(commands or ())
        if any(getattr(r, "ok", False) and getattr(r, "verb", "") in ("rest", "coin", "buy") for r in rows):
            return None
        m = _ROOM_GIVEN.search(_strip_quotes(prose or ""))
        if not m:
            return None
        from src.models import Location, get_session
        from src.services.rest import rest_here

        with get_session() as session:
            spec = rest_here(session.get(Location, location_id))
        if not spec:
            return None
    except Exception:
        return None
    price = int(spec.get("price") or 0)
    cost = f"{price} coin" if price else "free"
    return {
        "check": "room_without_rest",
        "detail": (
            f"The player asked for a room (\"{asked[:60]}\") and your reply handed one over (\"{m.group(0)[:60]}\") "
            f"with no @rest — a night here is {cost}, and @rest IS the room: it charges the fee and sleeps them "
            "to morning. If they took it, write @rest first in your next reply (never a @coin for the room); "
            "if they did not, nobody took their coin and no key changed hands."
        ),
    }
# Handed straight back: "...reads it, and gives it back"
_RETURNED = re.compile(
    r"\b(?:hands?|gives?|passes?|slides?|pushes?|tosses?|returns?|presses?)\b[^.!?;\n]{0,30}\bback\b"
    r"|\breturns?\s+(?:it|them)\b",
    re.IGNORECASE,
)
_GIVE_BACK_VERB = r"(?:hands?|gives?|passes?|slides?|pushes?|tosses?|presses?)"
# An NPC's own things: "takes the knife from the rack", "off the cloth"
_OTHER_SOURCE = re.compile(
    r"^[^.!?;\n]{0,25}?\b(?:from|off)\s+(?:the|his|her|their|its|a)\b", re.IGNORECASE
)
_KEEP_VERBS = r"(?:takes|accepts|pockets|confiscates)"


# After a comma the phrase goes on only through more adjectives: "a long, hard
# look" continues, "the branch, watching his face" stops at the branch
_CLAUSE_START = {"the", "a", "an", "his", "her", "their", "its", "your", "my", "he", "she", "they",
                 "it", "you", "i", "we", "who", "which", "that"}
_COIN_WORDS = {"coin", "coins", "coppers", "crowns", "money"}


def _object_phrase(obj: str, *, handed_over: bool = False) -> str | None:
    """The object a handover names, cut at the first function word, or None
    when it is a gesture, a favour, money, a served drink, or a body part
    ("a long, hard look", "a piece of your mind", "a small pouch of coin").

    Only the NOUNS are judged — the word before each comma and the last word
    (or the word before "of") — so "the cold iron key" and "a scrap of
    bread" are objects while "the whole story, start to finish" is not.
    `handed_over`: the player is the giver, so a pass or papers held up to be
    read are not given away either.
    """
    raw = obj.split()
    kept, heads = [raw[0]], []
    for i, w in enumerate(raw[1:], 1):
        word = w.rstrip(",")
        if word.lower() in _OBJECT_STOP:
            break
        kept.append(word)
        if w.endswith(","):
            heads.append(word)
            following = raw[i + 1].lower() if i + 1 < len(raw) else ""
            if (not following or following in _OBJECT_STOP or following in _CLAUSE_START
                    or following.endswith(("ing", "ed"))):
                break
    if len(kept) < 2:
        return None
    lowered = [w.lower() for w in kept]
    of = lowered.index("of", 1) if "of" in lowered[1:] else 0
    heads.append(kept[of - 1] if of else kept[-1])
    vocabs = (_NOT_AN_ITEM, _SERVED, _SHOWN) if handed_over else (_NOT_AN_ITEM, _SERVED)
    if of == 1 or any(_in_vocab(h, *vocabs) for h in heads):
        return None
    # "the silver key" is an object; "the gold" and "a pouch of coin" are money
    if heads[-1].lower() in ("gold", "silver", "copper") or _COIN_WORDS & set(lowered):
        return None
    return " ".join(kept)


def _given_back(after: str, thing: str) -> bool:
    """Shown, then returned — right away, or later in the reply by name.

    fleet-wenna T15: Coldiron "takes the notebook from your hands", a speech
    later "hands the notebook back".
    """
    if _RETURNED.search(after[:160]):
        return True
    head = re.escape(thing.split()[-1])
    return bool(re.search(
        rf"\b{_GIVE_BACK_VERB}\s+(?:the|your)\s+(?:[\w'’-]+\s+)?{head}\s+back\b"
        rf"|\breturns?\s+(?:the|your)\s+(?:[\w'’-]+\s+)?{head}\b",
        after, re.IGNORECASE,
    ))


def _first_handover(prose: str, patterns: list[re.Pattern], *, handed_over: bool = False) -> str | None:
    """The first object a pattern moves that is really an object, really
    happened ("if you hand him the horn, he'll talk" has not), and is not
    handed straight back."""
    for pattern in patterns:
        for m in pattern.finditer(prose):
            if _HYPOTHETICAL_BEFORE.search(prose[max(0, m.start() - 30):m.start()]):
                continue
            phrase = _object_phrase(m.group("obj"), handed_over=handed_over)
            if phrase and not _given_back(prose[m.end():], phrase):
                return phrase
    return None


def _item_head(name: str) -> str:
    """The noun an item is called by: "Wolf-Caller's Horn" -> "horn", but
    "Map of the North Road" -> "map" (never "road": the patrol "takes the
    north road" every morning)."""
    head = re.split(r"\s+of\s+", name, maxsplit=1, flags=re.IGNORECASE)[0].split()
    return re.sub(r"[^\w'’-]", "", head[-1]).lower() if head else ""


def _held_item_taken(prose: str, before: dict[str, Any]) -> str | None:
    """A carried item an NPC takes or pockets by its own name.

    fleet-tamsin T17: "Coldiron takes the horn like it might bite him" — the
    Wolf-Caller's Horn stayed in the pack. Weapons and armour are skipped
    (every NPC owns a knife), so are passes and papers (shown at a gate, then
    kept — @give files them as quest items, so the item type cannot tell), and
    "takes THE lantern" is likely the NPC's own: common props need "your".
    """
    for item in before.get("inventory") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("weapon", "armor", "key") or item.get("slot") in ("weapon", "armor"):
            continue
        head = _item_head(str(item.get("name") or "").strip())
        if len(head) < 4 or _in_vocab(head, _NOT_AN_ITEM, _SHOWN):
            continue
        owner = "your" if head in _COMMON_PROPS else "(?:the|your)"
        pattern = re.compile(
            rf"\b{_KEEP_VERBS}\s+{owner}\s+(?:[\w'’-]+\s+){{0,2}}?{re.escape(head)}\b",
            re.IGNORECASE,
        )
        name = str(item.get("name"))
        for m in pattern.finditer(prose):
            after = prose[m.end():]
            if not _OTHER_SOURCE.search(after) and not _given_back(after, name):
                return name
    return None


# Someone in the scene — a name, a pronoun, "the captain" — keeping what the
# player carried. Not "lifts"/"turns": a coin held to the light is looked at
_KEEPER = r"(?:\b(?:he|she|they)|(?<![\w'’-])(?-i:[A-Z][\w'’-]+)|\bthe\s+[a-z][\w'’-]+)"
_KEEPING_VERBS = (r"(?:takes|pockets|palms|accepts|snatches|confiscates|keeps|plucks|sweeps\s+up"
                  r"|tucks\s+away|closes\s+(?:his|her|their)\s+(?:hand|fist|fingers)\s+(?:around|over|on))")
_NOT_A_KEEPER = {"you", "your", "i", "it", "this", "that", "then", "now", "and", "but"}


def _carried_nouns(item: dict[str, Any], money_moved: bool = False) -> list[str]:
    """What prose calls a carried item: its head noun ("whistle"), and "coin"
    (with its metal) for a coin the pack holds as a thing — the head of
    "Greywater Silver Piece" is a "piece", which says nothing alone. Not
    when the purse moved this reply: then "the barman pockets the coin" was
    the @coin payment, and the carried silver piece never left the pack."""
    name = str(item.get("name") or "")
    nouns: list[str] = []
    head = _item_head(name.strip())
    if len(head) >= 4 and not _in_vocab(head, _NOT_AN_ITEM, _SHOWN):
        nouns.append(head)
    if _PACK_COIN.search(name) and not money_moved:
        nouns += ["coin"] + [m for m in ("silver", "gold", "copper") if m in name.lower()]
    return nouns


def _item_kept_by_someone(prose: str, before: dict[str, Any],
                          money_moved: bool = False) -> tuple[str, str] | None:
    """(item name, the words) for a carried item an NPC takes, pockets or
    keeps in the prose — by its head noun or, for a carried coin, "the coin".

    fleet5-bram T16: "Coldiron takes the coin from your shaking hand... he
    pockets the coin himself" — no @hand, the Greywater Silver Piece stayed
    in the pack (sellable at Hesk's counter) and the money check misread it
    as a payment. Weapons and armour are skipped (every NPC owns a knife),
    so are passes and papers shown at a gate; "from the table" is someone's
    own; a thing handed straight back was only looked at."""
    for item in before.get("inventory") or []:
        if not isinstance(item, dict) or item.get("type") in ("weapon", "armor") \
                or item.get("slot") in ("weapon", "armor"):
            continue
        name = str(item.get("name") or "")
        for noun in _carried_nouns(item, money_moved):
            owner = "your" if noun in _COMMON_PROPS else "(?:the|your)"
            pattern = re.compile(
                rf"(?P<who>{_KEEPER})\s+(?:\w+ly\s+)?{_KEEPING_VERBS}\s+{owner}\s+"
                rf"(?:[\w'’-]+\s+){{0,2}}?{re.escape(noun)}(?![\w'’-])"
                r"(?!\s+(?:purse|pouch|bag|box|jar|belt)\b)",        # "keeps the coin purse": hers
                re.IGNORECASE,
            )
            for m in pattern.finditer(prose):
                who = m.group("who").split()[-1].lower()
                if who in _NOT_A_KEEPER or _HYPOTHETICAL_BEFORE.search(prose[max(0, m.start() - 30):m.start()]):
                    continue
                after = prose[m.end():]
                if _OTHER_SOURCE.search(after) or _given_back(after, noun):
                    continue
                return name, m.group(0).strip()
    return None


# The player leaves a carried thing with someone: fleet6-bram T15 "You set the
# fresh-minted coin on the cart ... he doesn't push the coin away either" (and
# the player's own "Keep that.") — no @hand, so the Greywater Silver Piece,
# the campaign's clue, stayed in the pack and came out again at T18 and T20.
# Set down is only shown until someone accepts it: the prose keeps it, or the
# player's words give it away. A whistle laid on the table between two people
# is evidence on display.
_LEFT_ON = re.compile(
    r"\b[Yy]ou\s+(?:\w+ly\s+)?(?:set|lay|leave|slide|place|put|push|drop|press)\s+"
    r"(?P<obj>(?:the|your)\s+[\w'’-]+(?:\s+[\w'’-]+){0,3}?)\s+(?:down\s+)?(?:on|onto|upon|across|atop)\s+"
    r"(?:the|his|her|their)\s+(?:[\w'’-]+\s+)?"
    r"(?:cart|counter|table|bar|board|desk|plank|stall|tray|bench|barrel|crate)\b"
)
# What is accepted must be the thing itself — "it", or "the coin" by one of
# its nouns: "Coldiron takes the measure of it", "she takes the stool across
# from you", "he keeps the silence" accept nothing (`_accepts`)
_ACCEPTED = re.compile(
    r"\b(?:doesn['’]t|does\s+not|didn['’]t|did\s+not|never|won['’]t)\s+(?:\w+\s+)?"
    r"(?:(?:push|hand|give|slide|pass|toss)\s+(?P<back>it|them|the\s+[\w'’-]+(?:\s+[\w'’-]+)?)\s+(?:back|away)"
    r"|return\s+(?P<ret>it|them|the\s+[\w'’-]+(?:\s+[\w'’-]+)?))\b"
    r"|\b(?:takes|pockets|palms|accepts|keeps|tucks|sweeps|scoops)\s+(?:\w+ly\s+)?"
    r"(?P<kept>it|them|the\s+[\w'’-]+(?:\s+[\w'’-]+)?)\b",
    re.IGNORECASE,
)
_KEEP_IT_SAID = re.compile(
    r"\bkeep\s+(?P<kept>it|that|this|them|the\s+[\w'’-]+)\b|\bit['’]s\s+yours\b|\bfor\s+your\s+trouble\b",
    re.IGNORECASE,
)


def _accepts(pattern: re.Pattern, text: str, nouns: list[str]) -> bool:
    """Does `text` accept the thing: a match whose object is a pronoun or
    names one of `nouns` (a match with no object group, "it's yours", is
    acceptance as it stands)."""
    for m in pattern.finditer(text):
        said = next((g for g in m.groupdict().values() if g), None)
        if said is None:
            return True
        words = re.findall(r"[\w'’-]+", said.lower())
        if words and (words[0] in ("it", "them", "that", "this") or set(words[1:]) & set(nouns)):
            return True
    return False


def _left_with_someone(prose: str, before: dict[str, Any], player_input: str = "",
                       money_moved: bool = False) -> tuple[str, str] | None:
    """(item name, the words) for a carried thing the player sets down for
    someone who keeps it (`_LEFT_ON`), by its head noun or, for a carried
    coin, "coin". Weapons and armour are skipped, as everywhere."""
    typed = "" if _ENGINE_INPUT.match(player_input or "") else (player_input or "")
    for m in _LEFT_ON.finditer(prose):
        if _HYPOTHETICAL_BEFORE.search(prose[max(0, m.start() - 30):m.start()]):
            continue
        obj, after = m.group("obj").lower(), prose[m.end():]
        for item in before.get("inventory") or []:
            if not isinstance(item, dict) or item.get("type") in ("weapon", "armor") \
                    or item.get("slot") in ("weapon", "armor"):
                continue
            called = _carried_nouns(item, money_moved)
            nouns = [n for n in called if re.search(rf"\b{re.escape(n)}\b", obj)]
            if not nouns or _given_back(after, nouns[0]):
                continue
            if _accepts(_ACCEPTED, after[:240], called) or _accepts(_KEEP_IT_SAID, typed, called):
                return str(item.get("name")), m.group(0).strip()
    return None


def _carried_head(phrase: str, before: dict[str, Any]) -> bool:
    """Does `phrase` ("the pass", "the tincture") end on the head noun of
    something the player already carries? Their own thing, put away or
    shown — "the scrap goes into your coat beside the gate pass" is new."""
    words = re.findall(r"[\w'’-]+", phrase.lower())
    if not words:
        return False
    last = words[-1]
    for item in before.get("inventory") or []:
        name = str(item.get("name") if isinstance(item, dict) else item or "").strip()
        if name and (last == _item_head(name) or (last in ("coin", "coins") and _PACK_COIN.search(name))):
            return True
    return False


def _strip_quotes(text: str) -> str:
    return re.sub(r'["“][^"“”]*["”]', " ", text)


# ---------------------------------------------------------------------------
# Rewards on screen (round 4)
# ---------------------------------------------------------------------------
# fleet5-tamsin T9 and fleet5-wenna T12 closed "Counting the Howls" on Piper's
# thanks alone: 10 coin and a Yarrow Tincture landed in a pack the story never
# opened (Tamsin never learned she carried a healing tincture). fleet5-bram
# T18 paid 60 coin and the prose named only the charm. A reply whose
# @quest-done paid out must show the pay — the coin by any money word or its
# amount, each item by its head noun ("tincture", "charm") — or the next turn
# is told exactly what changed hands off screen. Lenient on purpose: any pay
# word counts, so only a payout the reply never touched is flagged. A bare
# coin word is not pay, though: fleet6-wenna T20 paid 20 coin nobody handed
# over, and the check stayed quiet on "Greywater mint marks on coin" in the
# letter talk — coin shows only in a sentence where something changes hands.
_PAY_WORDS = re.compile(
    r"\b(?:purse|pouch|pay|pays|paid|payment|wages?|fee|reward|counts?\s+out)\b",
    re.IGNORECASE,
)
_COIN_WORD = re.compile(r"\b(?:coins?|coppers?|silver|gold|crowns?|money)\b", re.IGNORECASE)
_TRANSFER = re.compile(
    r"\b(?:press(?:es|ed|ing)?|counts?|counted|counting|hands?|handed|handing|gives?|gave|giving"
    r"|pass(?:es|ed|ing)?|slides?|slid|sliding|push(?:es|ed|ing)?|drops?|dropped|dropping|tips?|tipped"
    r"|toss(?:es|ed|ing)?|tucks?|tucked|tucking|puts?|putting|places?|placed|placing|sets?|setting"
    r"|lays?|laid|laying|offers?|offered|holds?\s+out|held\s+out|as\s+promised|into\s+your"
    r"|in\s+your\s+(?:hand|hands|palm|purse)"
    # ...and the receiving end: "You pocket the coins with a nod" was pay shown
    r"|pockets?|pocketed|pocketing|takes?|took|taking|accepts?|accepted|receives?|received"
    r"|catch(?:es)?|caught|flips?|flipped)\b",
    re.IGNORECASE,
)
_AMOUNT_WORDS = {5: "five", 10: "ten", 12: "twelve", 15: "fifteen", 20: "twenty", 25: "twenty-five",
                 30: "thirty", 40: "forty", 50: "fifty", 60: "sixty", 70: "seventy", 80: "eighty",
                 90: "ninety", 100: "hundred"}
# A potion is shown by its vessel: "coin and a second vial" (fleet6-tamsin
# T11) and "a coin into your palm after the vial" (fleet6-wenna T11) were the
# Yarrow Tincture
_POTION_NOUNS = ("vial", "flask", "phial", "bottle", "draught", "tincture", "potion", "elixir", "tonic", "salve")


def _coin_shown(text: str, coin: int) -> bool:
    """Is a coin payout on screen: its amount ("60", "sixty coin"), a pay
    word, or a coin word in a sentence where something changes hands."""
    if re.search(rf"(?<![\d.]){coin}(?!\d)", text) or _PAY_WORDS.search(text):
        return True
    word = _AMOUNT_WORDS.get(coin)
    if word and re.search(rf"\b{word}\s+(?:[\w'’-]+\s+)?(?:coins?|silver|gold|crowns?|coppers?|marks?|pieces?)\b",
                          text, re.IGNORECASE):
        return True
    return any(_COIN_WORD.search(s) and _TRANSFER.search(s) for s in _SENTENCES.split(text))


def _paid_quests(tools: list[dict[str, Any]] | None, before: dict[str, Any],
                 after: dict[str, Any]) -> list[dict[str, Any]]:
    """The quests this reply's @quest-done completed, with what they paid:
    [{title, coin, items, item_info}] (`item_info`: {name: {about, potion}}).
    Only the balance — what was paid up front was shown when it was paid. A
    quest counts when it left the active list this turn and is completed in
    the database. [] when the world cannot be read."""
    if not any(t.get("name") == "update_quest_status" and t.get("status") == "success" for t in tools or []):
        return []
    still = {q.get("id") for q in after.get("quests") or [] if isinstance(q, dict)}
    left = [q.get("id") for q in before.get("quests") or []
            if isinstance(q, dict) and q.get("id") and q.get("id") not in still]
    if not left:
        return []
    try:
        from src.models import Quest, QuestStatus, get_session
        from src.tools.world_write.quests import reward_balance, reward_coin

        paid = []
        with get_session() as session:
            for quest_id in left:
                quest = session.get(Quest, quest_id)
                if quest is None or quest.status != QuestStatus.COMPLETED:
                    continue
                rewards = reward_balance(quest.rewards if isinstance(quest.rewards, dict) else {})
                items, info = [], {}
                for entry in rewards.get("items") or []:
                    name = " ".join(str((entry.get("name") if isinstance(entry, dict) else entry) or "").split())
                    if not name:
                        continue
                    items.append(name)
                    data = entry if isinstance(entry, dict) else {}
                    kind = str(data.get("kind") or data.get("type") or "").lower()
                    info[name] = {"about": str(data.get("about") or data.get("description") or ""),
                                  "potion": kind in ("potion", "consumable") or bool(data.get("effect_tag"))}
                paid.append({"title": quest.title, "coin": reward_coin(rewards), "items": items, "item_info": info})
        return paid
    except Exception:
        return []


def _item_shown(name: str, forms: set[str], *, about: str = "", potion: bool = False, text: str = "") -> bool:
    """Is a reward item in the text: its head noun ("charm"), or every other
    word of its name ("a wolf's tooth on a cord" is the Wolf-Tooth Charm,
    fleet4-wenna T14 — but "wolves" alone is every reply in a wolf story).

    Or a potion's vessel ("a second vial"), or two words of the item's own
    description in a sentence where something changes hands: fleet6-bram T20
    "presses a brass-wired fang on a Watch cord into your other palm" was the
    charm ("A wolf's fang bound in brass wire on a Watch cord"), and the
    audit asked for it to be handed over again."""
    head = _item_head(name)
    if not head or _word_forms(head) & forms:
        return True
    if potion and any(_word_forms(n) & forms for n in _POTION_NOUNS):
        return True
    described = {w for w in _words(about) if len(w) >= 4 and w not in _GENERIC_WORDS}
    if len(described) >= 2 and text:
        for sentence in _SENTENCES.split(text):
            if _TRANSFER.search(sentence):
                said = _text_forms(sentence)
                if sum(1 for w in described if _word_forms(w) & said) >= 2:
                    return True
    words = [w for w in _words(re.split(r"\s+of\s+", name, maxsplit=1, flags=re.IGNORECASE)[0])
             if len(w) >= 3 and w not in _GENERIC_WORDS and w != head]
    return bool(words) and all(_word_forms(w) & forms for w in words)


def _rewards_unshown(text: str, paid: list[dict[str, Any]]) -> list[str]:
    """What of `paid` the reply never showed: "60 coin", "Yarrow Tincture"."""
    forms = _text_forms(text)
    missing: list[str] = []
    for quest in paid:
        coin = int(quest.get("coin") or 0)
        if coin and not _coin_shown(text, coin):
            missing.append(f"{coin} coin")
        info = quest.get("item_info") or {}
        missing += [name for name in quest.get("items") or []
                    if not _item_shown(name, forms, about=str((info.get(name) or {}).get("about") or ""),
                                       potion=bool((info.get(name) or {}).get("potion")), text=text)]
    return missing


def reward_unnarrated_warning(text: str, paid: list[dict[str, Any]]) -> dict[str, str] | None:
    """The reward_unnarrated warning for a reply that paid `paid` and showed
    only part of it (or none), else None."""
    missing = _rewards_unshown(text, paid)
    if not missing:
        return None
    def listed(parts: list[str], last: str) -> str:
        return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f" {last} " + parts[-1]

    titles = " and ".join(f"\"{q['title']}\"" for q in paid)
    everything = [f"{q['coin']} coin" for q in paid if q.get("coin")] + [
        f"{'an' if i[:1].lower() in 'aeiou' else 'a'} {i}" for q in paid for i in q.get("items") or []]
    return {
        "check": "reward_unnarrated",
        "detail": (
            f"Your @quest-done for {titles} paid {listed(everything, 'and')} into the player's pack, but the "
            f"reply never showed the {listed(missing, 'or the')} changing hands — on screen they have not received it. "
            "Show it at the top of your next reply, in one sentence: the giver counts the coin into their hand "
            "and presses each item on them, named (if the giver is no longer with them, it reaches them as a "
            "message or the giver comes to find them — never 'already among your things'). "
            "Never pay it again — no @coin or @give for it."
        ),
    }


# Pay up front recast as a purchase. fleet8-wenna T4: Piper's job paid the
# Yarrow Tincture up front (`@give` booked it as the advance) and the same
# reply wrote `@coin -10 | paid Piper for the tincture and her tally` — the
# player had misread "ten coin, and my tincture besides" as a price. The job
# pays the player; a reply that charges them its giver in the same breath
# has the advance read as a sale.

def advance_bought_warnings(results: Iterable[Any], advances: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """advance_bought: a @coin -N whose reason names the giver of a job
    whose pay up front this same reply booked (`CommandRunner.advances`).
    Takes CommandRunner results or transcript rows. Never raises."""
    warnings: list[dict[str, str]] = []
    try:
        advances = [a for a in advances or [] if a.get("giver_id")]
        if not advances:
            return warnings
        from src.models import NPC, get_session
        from src.services import commands as cmds

        with get_session() as session:
            called = {a["giver_id"]: (cmds._called_by(session.get(NPC, a["giver_id"])),
                                      getattr(session.get(NPC, a["giver_id"]), "name", "the giver"))
                      for a in advances}
        for row in results or []:
            get = row.get if isinstance(row, dict) else (lambda key, _r=row: getattr(_r, key, None))
            if not get("ok"):
                continue
            line = str(get("line") or "")
            parsed = cmds.parse_command(line)
            if parsed is None or cmds._ALIASES.get(parsed.verb, parsed.verb) != "coin":
                continue
            amount = cmds.parse_signed(parsed.main) or (cmds.parse_signed(parsed.extras[0]) if parsed.extras else None)
            if not amount or amount > 0:
                continue
            said = set(re.findall(r"[a-z]+", " ".join([parsed.main, *parsed.extras]).lower()))
            for advance in advances:
                words, name = called.get(advance["giver_id"], (set(), "the giver"))
                if not (words & said):
                    continue
                warnings.append({
                    "check": "advance_bought",
                    "detail": (
                        f"This reply booked {advance['what']} as \"{advance['title']}\"'s pay up front — {name} pays "
                        f"the player — and `{_clip_line(line)}` took {-amount} coin from the player to {name}. "
                        "Pay up front is never bought: next reply have them push the coin back (@coin "
                        f"+{-amount} | {name.split()[0]} returns the coin) or, if the player insists, make it a "
                        "plain gift — the job's own pay is still owed in full."
                    ),
                })
                break
    except Exception:  # an audit never costs the turn
        import logging
        logging.getLogger(__name__).warning("advance audit failed", exc_info=True)
    return warnings


# ---------------------------------------------------------------------------
# Dialogue shape
# ---------------------------------------------------------------------------
# fleet critics: 82 spoken lines in 21 turns, 6-7 speeches a turn, one
# captain giving six speeches in a row — the player never got a word in.
MAX_SPOKEN_LINES = 4
MAX_LINE_WORDS = 60


def _too_talky(spoken: list[dict[str, Any]]) -> str | None:
    # A long line the stream cut in two for the bubble is still one line
    # the DM wrote (`continues`, dialogue_markup.split_long_speech)
    lines: list[str] = []
    for n in spoken:
        text = str(n.get("text") or "")
        if n.get("continues") and lines:
            lines[-1] = f"{lines[-1]} {text}"
        else:
            lines.append(text)
    longest = max((len(t.split()) for t in lines), default=0)
    if len(lines) <= MAX_SPOKEN_LINES and longest <= MAX_LINE_WORDS:
        return None
    return (
        f"Your reply had {len(lines)} spoken line(s), the longest {longest} words — keep replies "
        "to at most 3 short lines of dialogue (well under 60 words each) and let the player answer."
    )


# The player introducing themselves in prose — but not the DM's cover story
# for a leak ("He says your own name back to you", fleet-wenna T5)
_NAME_GIVEN = re.compile(
    r"\byou\s+(?:\w+\s+){0,2}?(?:give|tell|offer|say|trade|share)\b[^.!?\n]{0,30}\byour\s+name\b"
    r"|\byou\s+(?:\w+\s+)?introduce\s+yourself\b",
    re.IGNORECASE,
)
# Engine-authored inputs ("[OPENING] This is Bram Holloway's first moment...",
# "[COMBAT OUTCOME] ...") carry the name for the DM, not for anyone in the scene.
# The real aftermath is "[COMBAT OUTCOME — resolved …": the tag may end at a dash
_ENGINE_INPUT = re.compile(r"^\s*\[[A-Z][A-Z_'-]*(?: [A-Z][A-Z_'-]*)*(?:\]|\s*[—–:-])")
# Titles are not names: "Sir Aldous Kent" must not flag every "Sir" a guard says
_HONORIFICS = {
    "sir", "ser", "lady", "lord", "dame", "master", "mistress", "mister", "miss", "mr", "mrs",
    "ms", "captain", "sergeant", "commander", "lieutenant", "brother", "sister", "father",
    "mother", "doctor", "dr", "professor", "agent", "detective", "inspector", "officer",
    "the", "of", "von", "van", "de", "del", "da", "le", "la",
}
_SENTENCE_START = re.compile(r"(?:^|[.!?…:;—–]\s*|\s-\s*)$")
# Capitalised words that open a sentence before a vocative without being
# another name: "Thanks Rook", "You're Kent, aren't you"
_NOT_A_NAME = {
    "i", "you", "he", "she", "they", "we", "it", "that", "this", "thanks", "thank", "hello",
    "hey", "listen", "look", "well", "yes", "no", "oh", "ah", "good", "morning", "evening",
    "night", "welcome", "easy", "quiet", "go", "come", "sit", "stand", "run", "walk", "so",
    "and", "but", "now", "then", "right", "sorry", "please", "farewell", "goodbye", "see",
}


def _name_tokens(player_name: str) -> list[str]:
    """First and last name, capitalised, titles and particles dropped."""
    parts = [p.strip(".,") for p in player_name.split()]
    parts = [p for p in parts if p and p.lower() not in _HONORIFICS]
    if not parts:
        return []
    tokens = dict.fromkeys(p[0].upper() + p[1:] for p in (parts[0], parts[-1]))
    return [t for t in tokens if len(t) >= 3]


def _says_name(text: str, token: str, tokens: list[str]) -> bool:
    """`token` used as the player's name, not as part of something else.

    A surname like Wolf, Grey or Stone is also a word: "Wolf's Hollow", "the
    Grey Company", "Osric Grey" and a sentence-initial "Will you…" are not
    the player being named.
    """
    return bool(_name_uses(text, token, tokens))


def _name_uses(text: str, token: str, tokens: list[str], *, possessive: bool = False) -> list[re.Match]:
    """Each place `_says_name` reads `token` as the player's name. With
    `possessive`, "Bram's" counts too — unless a name follows it ("Cole's
    Ferry"); the in-stream cut (`dialogue.cut_player_name`) rewrites those."""
    out: list[re.Match] = []
    for m in re.finditer(rf"\b{re.escape(token)}\b", text):
        before, after = text[:m.start()], text[m.end():]
        owned = re.match(r"['’]s\b", after)
        if owned and not possessive:
            continue
        if owned:
            after = after[owned.end():]
        following = re.match(r"\s+([A-Z][\w'’-]*)", after)
        # "Wenna Cole's satchel": the surname's own "'s" is no other name —
        # the cut read it as one and left "Wenna your satchel" on screen
        if following and re.sub(r"['’]s$", "", following.group(1)) not in tokens:
            continue
        preceding = re.search(r"([A-Z][\w'’-]*)\s+$", before)
        if preceding:
            word = preceding.group(1)
            other_name = not (word in tokens or word.lower() in _HONORIFICS | _NOT_A_NAME
                              or re.search(r"['’][a-z]+$", word))
            # "Find Wenna Cole, then." / "Tell Wenna to wait": a verb is
            # capitalised only because it opens the sentence. Before the
            # FIRST name (no word of its own), that is the player named — the
            # cut had left "Find Wenna you, then." (a surname keeps its kin:
            # "Ned Holloway is my cousin")
            if (other_name and token == tokens[0] and token.lower() not in _ordinary_word_names()
                    and _SENTENCE_START.search(before[:preceding.start(1)])):
                other_name = False
            if other_name:
                continue
        if (_SENTENCE_START.search(before) and re.match(r"\s+[a-z]", after)
                and token.lower() in _ordinary_word_names()):
            # "Will you…", "Rook takes the long way": the word, not the name.
            # A name that is no word opens a sentence as the name: fleet6-
            # tamsin T15 Piper, "Tamsin heard it too" — never told it
            continue
        out.append(m)
    return out


def _ordinary_word_names() -> frozenset[str]:
    """Names that are also everyday words ("Will", "Rook", "Cole", "Grey")."""
    from src.services.dialogue import _BARE_WORDS, _WORD_NAMES

    return _WORD_NAMES | _BARE_WORDS


def _names_before_introduction(
    spoken: list[dict[str, Any]], player_name: str, met_before: set[str],
    prose: str, player_input: str,
) -> list[tuple[str, str]]:
    """(speaker, name) for NPCs who call the player by a name nobody told them.

    fleet-tamsin: Coldiron called her "Rook" from turn 16 though nobody in
    the fiction ever said it — the DM read it off the character sheet. A name
    the player typed, the prose has them give ("you tell her your name"), or
    someone said aloud earlier this turn is fair game. `met_before` is who
    knew the player's NAME before this turn — not who had met them: fleet6-
    bram T19 Piper, an old acquaintance, said "Bram", which he never gave.
    """
    tokens = _name_tokens(player_name)
    if not tokens or _gives_name(_NAME_GIVEN, prose):
        return []
    typed = "" if _ENGINE_INPUT.match(player_input or "") else (player_input or "")
    heard = {t for t in tokens if re.search(rf"\b{re.escape(t)}\b", typed + "\n" + prose)}
    leaks: list[tuple[str, str]] = []
    flagged: set[str] = set()
    for line in spoken:
        text = str(line.get("text") or "")
        said = {t for t in tokens if _says_name(text, t, tokens)}
        npc_id = line.get("npc_id")
        if npc_id and npc_id not in met_before and npc_id not in flagged:
            unheard = sorted(said - heard)
            if unheard:
                flagged.add(npc_id)
                leaks.append((str(line.get("npc_name") or "Someone"), unheard[0]))
        heard |= said
    return leaks


# The player giving their own name outside a quote: "I tell her my name",
# "I introduce myself"
_INPUT_NAME_GIVEN = re.compile(
    r"\bI\s+(?:\w+\s+){0,2}?(?:give|tell|offer|say|trade|share)\b[^.!?\n]{0,30}\bmy\s+name\b"
    r"|\bI\s+(?:\w+\s+)?introduce\s+myself\b",
    re.IGNORECASE,
)
_QUOTE_WORDS = re.compile(r'"([^"]*)"|“([^”]*)”')
# The giving words, negated, give nothing: "you never give your name", "I do
# not tell him my name", "I refuse to share my name"
_NAME_WITHHELD = re.compile(r"\b(?:not|never|refuses?|refused|refusing|without)\b", re.IGNORECASE)


def _gives_name(pattern: re.Pattern, text: str) -> bool:
    """Does `text` (narration, quotes already out) have the name given?"""
    return any(not _NAME_WITHHELD.search(m.group(0)) for m in pattern.finditer(text or ""))


def player_name_said(spoken: list[dict[str, Any]], player_name: str, knew: set[str],
                     prose: str, player_input: str) -> bool:
    """Was the player's name said aloud in the scene this turn, so whoever
    stood there knows it now (`dialogue.note_player_name_heard`)?

    The player saying it — in their quoted words ("I'm Wenna Cole," fleet6-
    wenna T14) or "I tell her my name" — the prose having them give it ("you
    tell her your name"), or a speaker line by someone who already knew it
    (`knew`). A stranger's guess teaches nobody: that is the leak
    `_names_before_introduction` flags. The player's name in their own
    narration ("Bram leans in") is not said to anyone. The giving phrases
    are read outside quotes and never negated: '"Will you tell me your
    name?" she asks' and "I do not give my name" give nobody anything.
    """
    tokens = _name_tokens(player_name or "")
    if not tokens:
        return False
    if _gives_name(_NAME_GIVEN, _strip_quotes(prose or "")):
        return True
    typed = "" if _ENGINE_INPUT.match(player_input or "") else (player_input or "")
    if typed and _gives_name(_INPUT_NAME_GIVEN, _strip_quotes(typed)):
        return True
    for m in _QUOTE_WORDS.finditer(typed):
        words = m.group(1) if m.group(1) is not None else m.group(2)
        if any(_says_name(words, t, tokens) for t in tokens):
            return True
    return any(line.get("npc_id") in knew and any(_says_name(str(line.get("text") or ""), t, tokens) for t in tokens)
               for line in spoken or [] if isinstance(line, dict))


def audit_turn(
    turn_text: str,
    tools: list[dict[str, Any]],
    state_before: dict[str, Any] | None,
    state_after: dict[str, Any] | None,
    *,
    code_actions: set[str] | None = None,
    campaign: dict[str, Any] | None = None,
    spoken_text: str = "",
    spoken: list[dict[str, Any]] | None = None,
    player_name: str | None = None,
    met_before: set[str] | None = None,
    player_input: str = "",
    scene_objects: list[dict[str, Any]] | None = None,
    unshown: str = "",
) -> list[dict[str, str]]:
    """Compare what the narration claims against what actually happened.

    code_actions: tool names (or the category labels in CODE_ACTION_TOOLS)
        the ENGINE executed on the player's behalf between turns — canvas
        travel and pickups, potions, equips, combat results, campaign beat
        ops. They are unioned into the succeeded set: the DM may legitimately
        narrate their consequences without having called them.
    campaign: {"beat": {...} | None, "sealed_truths": [...], "known_words":
        [...]} — when present, the two campaign checks run (`known_words`:
        what the player already knows, never evidence of a truth).
    spoken_text: every line NPCs spoke this turn (speaker lines). A beat can
        be delivered — or a sealed truth leaked — through someone's mouth, so
        the campaign checks read narration and speech together.
    spoken: the speaker lines themselves, in order ({npc_name, text, npc_id};
        npc_id only for real NPCs). Drives too_talky and, with the two
        below, name_before_introduction; speaker names join the campaign
        haystack (a "Vex Marrow:" label puts the name on screen).
    player_name: the character's name. met_before: ids of the NPCs who knew
        the player's NAME before this turn (`knows_player_name` on their
        row; met is not enough) — None skips the name check (unknown is
        not "nobody").
    player_input: what the player typed; a name they gave is fair game, and
        a pack consumable they say they drink must meet an @use.
    scene_objects: the authored objects where the turn started whose
        contents the player has NOT seen (`unseen_scene_objects`) — telling
        what they read or hold is object_contents_leak.
    unshown: sentences the reply wrote that the filters kept off the screen,
        the player's name made "you" — only the hand-over checks read them:
        a hand-over the DM wrote still happened in its story (fleet9-bram
        T6: "The Captain presses the pass into Bram's hand" was dropped, and
        no audit fired while the pack never held the pass).

    Returns a list of {check, detail} warnings (empty = clean turn).
    """
    warnings: list[dict[str, str]] = []
    turn_text = turn_text or ""
    # A reply can be all speaker lines and no prose — still too talky, still a beat
    lines = [n for n in spoken or [] if isinstance(n, dict)]
    if not turn_text and not lines:
        return warnings

    succeeded = {
        t.get("name")
        for t in (tools or [])
        if t.get("status") == "success" and t.get("name")
    } | _expand_code_actions(code_actions)
    before = state_before or {}
    after = state_after or {}

    if _QUEST_CLAIM.search(turn_text) and not (succeeded & _QUEST_TOOLS):
        warnings.append({
            "check": "quest_banner_without_tool",
            "detail": "Narration announces a quest change but no quest tool succeeded this turn.",
        })

    health_changed = before.get("health") != after.get("health")
    if (
        _DAMAGE_CLAIM.search(turn_text)
        and "update_player_health" not in succeeded
        and not health_changed
    ):
        warnings.append({
            "check": "damage_without_tool",
            "detail": "Narration says the player was hurt but health never changed in the database.",
        })

    inventory_changed = before.get("inventory") != after.get("inventory")
    items_moved = inventory_changed or bool(succeeded & _ITEM_TOOLS)
    handover_text = f"{turn_text}\n{unshown}" if unshown else turn_text
    item_claimed = not items_moved and bool(
        _ITEM_CLAIM.search(handover_text) or _first_handover(handover_text, _ITEM_GIVEN_TO_YOU)
    )
    if item_claimed:
        warnings.append({
            "check": "item_without_tool",
            "detail": (
                "Narration says an item entered the inventory but the inventory never changed — "
                "write @give <item> | kind: … (or @buy <item> | from: <person> when they paid) "
                "whenever the player receives something."
            ),
        })

    prose = _strip_quotes(turn_text)
    money_moved = before.get("currency") != after.get("currency") or bool(succeeded & _MONEY_TOOLS)
    paid = None if money_moved else _money_claim(prose, carried_coin=bool(_carried_coins(before)))
    if paid:
        warnings.append({
            "check": "money_without_command",
            "detail": (
                f"Narration has coin changing hands (\"{paid[:80]}\") but the purse never moved — "
                "write @coin -N | reason when the player pays (or @buy <item> | from: <person> for "
                "goods, which moves coin and item together) and @coin +N when they are paid."
            ),
        })

    if not items_moved:
        given = _first_handover(prose, _ITEM_GIVEN, handed_over=True) or _held_item_taken(prose, before)
        if given:
            warnings.append({
                "check": "item_handover_without_command",
                "detail": (
                    f"Narration has the player hand over {given} but it never left their pack — "
                    "write @hand <item> | to: <person> whenever they give something away."
                ),
            })
        kept = None if given or "transfer_item" in succeeded else _item_kept_by_someone(
            prose, before, money_moved=money_moved)
        if kept:
            # "The ferryman takes the coin" may still have been a fare: say both
            fare = (" If it was money they paid, it was @coin -N instead."
                    if re.search(r"(?:coin|silver|gold|copper)$", kept[1], re.IGNORECASE) else "")
            warnings.append({
                "check": "item_handed_without_command",
                "detail": (
                    f"Narration has someone keep the player's {kept[0]} (\"{kept[1][:80]}\") but it is "
                    f"still in their pack — if it changed hands, write @hand {kept[0]} | to: <person> "
                    "first thing next reply; if it was only looked at, have it handed back in the prose."
                    + fare
                ),
            })
        left = None if given or kept or "transfer_item" in succeeded else _left_with_someone(
            prose, before, player_input, money_moved=money_moved)
        if left:
            warnings.append({
                "check": "item_handover_without_command",
                "detail": (
                    f"Narration has the player leave their {left[0]} with someone who keeps it "
                    f"(\"{left[1][:80]}\") but it is still in their pack — write @hand {left[0]} | to: "
                    "<person> first thing next reply (a gift that buys goodwill is also @trust <person> "
                    "+N); if it was only shown, have it picked back up in the prose."
                ),
            })
        # item_without_tool already covers the common receiving phrases
        handover_prose = _strip_quotes(handover_text) if unshown else prose
        received = None if item_claimed else _first_handover(handover_prose, _ITEM_RECEIVED)
        if not received and not item_claimed and money_moved:
            received = _first_handover(prose, [_ITEM_ACROSS_COUNTER])
        # ...and with no payment, a thing one keeps slid across to them: "She
        # slides a brass key across the bar" (fleet9-bram T18), then "I pocket
        # the key" and no @give (T19) — never a bowl of stew or a mug
        slid = None
        if not received and not item_claimed and not money_moved:
            slid = _slid_keepsake(handover_prose)
            if slid and (_already_carried(slid, before) or _carried_head(slid, before)):
                slid = None
        if slid:
            slid = slid[0].lower() + slid[1:] if slid.split()[0] in ("The", "A", "An") else slid
            warnings.append({
                "check": "item_handover_without_command",
                "detail": (
                    f"Narration slides {slid} across to the player but it never reached their pack — "
                    "if they take it, write @give <item> | kind: … in the reply they do (first thing next "
                    "reply if they already have); until then it lies where it was put and is not theirs."
                ),
            })
        # Already in the pack: a reward @quest-done paid last reply, shown now
        # as reward_unnarrated asked (fleet6-wenna T12, the Yarrow Tincture),
        # or their own thing by its head noun ("the pass goes into your coat")
        if received and (_already_carried(received, before) or _carried_head(received, before)):
            received = None
        if received and received.split()[0] in ("The", "A", "An", "His", "Her", "Their", "Its"):
            received = received[0].lower() + received[1:]       # "The scrap goes…" opened its sentence
        if received:
            warnings.append({
                "check": "item_handover_without_command",
                "detail": (
                    f"Narration puts {received} into the player's hands but it never reached their "
                    "pack — write @give <item> | kind: … (or @buy <item> | from: <person> when "
                    "they paid) whenever they receive something."
                ),
            })

    # What the player typed comes first: they said they drank it, so the
    # prose's version of the same draught needs no second warning
    drunk = [] if "use_item" in succeeded else _consumed_in_input(player_input, before, after)
    for name in drunk:
        warnings.append({
            "check": "consumable_used_without_command",
            "detail": (
                f"The player said they drink or use their {name}, but your reply wrote no @use and "
                f"it is still in their pack — it did nothing. If they really did, write @use {name} "
                "first in your next reply and let it work; never narrate its effect without it."
            ),
        })
    consumed = [n for n in _consumed_without_use(turn_text, before, after) if n not in drunk]
    if consumed and "use_item" not in succeeded:
        warnings.append({
            "check": "item_used_without_command",
            "detail": (
                f"Narration may have the player drink or use {', '.join(consumed)}, but it is still "
                "in their pack and did nothing. ONLY if the player actually drank or used it, write "
                "@use <item> in your next reply; if they did not, ignore this — never use their "
                "things for them."
            ),
        })
    # Gear the story lost, still in the pack (fleet10-bram T19)
    for name in lost_gear(turn_text, before, after, succeeded):
        warnings.append({
            "check": "lost_gear",
            "detail": (
                f"Your reply lost the player's {name} (dropped, knocked away or broken), but it is still in "
                f"their pack{' and in their hand for the next fight' if _equipped(name, before) else ''}. In your "
                f"next reply either write @remove {name} — it is gone — or have them find it and pick it up, "
                "in the prose."
            ),
        })

    location_changed = before.get("location_id") != after.get("location_id")
    if (not location_changed and "move_player" not in succeeded
            and _arrival_claim(turn_text, before.get("location"))):
        warnings.append({
            "check": "arrival_without_move",
            "detail": (
                "Narration describes arriving somewhere but the player's location never changed. "
                "A walk somewhere needs @move <place> (and @time) in the same reply — if your story "
                "moved them, write it first next reply; otherwise they are still where they were."
            ),
        })
    elif not location_changed and "move_player" not in succeeded:
        staged = _staged_ahead(turn_text, lines, after.get("location_id") or before.get("location_id"))
        if staged:
            place, words, speaker = staged
            warnings.append({
                "check": "arrival_without_move",
                "detail": (
                    f"Narration walked the player up to {place} (\"{words[:70]}\") and voiced {speaker}, who "
                    f"is at {place}, but the player's location never changed — they are still where they "
                    f"were. If your story took them there, write @move {place} first next reply; otherwise "
                    "open it where they really are."
                ),
            })

    # A dressing is a heal: @health one step better
    tended = _tended_without_health(prose, before, after, succeeded)
    if tended:
        better = _one_step_better(before.get("health"))
        warnings.append({
            "check": "healing_without_command",
            "detail": (
                f"Narration has someone tend the player's wound (\"{tended[:80]}\") but their health "
                f"never changed — they are still {before.get('health')}. A healer's dressing is "
                f"@health one step better: write @health {better} | cause: <who>'s dressing in the "
                "reply that does it (and @coin if it cost them)."
            ),
        })

    # Contents of "Things here" the player never used, told anyway
    typed = "" if _ENGINE_INPUT.match(player_input or "") else (player_input or "")
    for obj, tell in _object_contents_leaked(turn_text, spoken_text, scene_objects or [], typed):
        key = obj.get("_out_key")
        if isinstance(key, tuple) and len(key) == 2:
            _OBJECTS_OUT.setdefault(key[0], set()).add(key[1])   # warned once is enough
        label = obj.get("label") or "a thing here"
        warnings.append({
            "check": "object_contents_leak",
            "detail": (
                f"Your reply told what the {label} reads or holds ({tell}) though the player never "
                f"used it — they have not seen it. If they really examined it, the reply needed "
                f"@interact {label}; otherwise never mention its contents until they do (they can "
                "see the thing itself, not what it says or holds)."
            ),
        })

    if _CONSEQUENCE_CLAIM.search(turn_text) and not (succeeded & _CONSEQUENCE_TOOLS):
        warnings.append({
            "check": "consequences_without_tools",
            "detail": (
                "Narration claims relationship/reputation/event consequences but no "
                "relationship, reputation, or event tool succeeded this turn."
            ),
        })

    # Fabricated dialogue: the deduper already removed re-quotes of real
    # speech, so attributed quotes remaining here were written inline — the
    # player never heard them (no voice, no portrait, no bubble)
    fabricated_speakers = _find_fabricated_dialogue(turn_text)
    if fabricated_speakers:
        names = ", ".join(fabricated_speakers)
        warnings.append({
            "check": "dialogue_without_speak",
            "detail": (
                f"Narration contains quoted dialogue attributed to {names} written "
                "inside a prose paragraph — the player gets NO voice, portrait, or "
                "speech bubble for it. Every spoken line goes on its OWN line as a "
                'speaker line: Name (tone): "words".'
            ),
        })

    talky = _too_talky(lines)
    if talky:
        warnings.append({"check": "too_talky", "detail": talky})

    # A payout the story never showed (see reward_unnarrated_warning)
    paid = _paid_quests(tools, before, after)
    if paid:
        said_aloud = spoken_text or "\n".join(str(n.get("text") or "") for n in lines)
        unshown = reward_unnarrated_warning("\n".join((turn_text, said_aloud)), paid)
        if unshown:
            warnings.append(unshown)

    if player_name and met_before is not None:
        for speaker, name in _names_before_introduction(
            lines, player_name, met_before, prose, player_input,
        ):
            warnings.append({
                "check": "name_before_introduction",
                "detail": (
                    f"{speaker} called the player \"{name}\", a name nobody ever told them "
                    "(their dossier says \"Knows the player's name: no\") — until the player "
                    "gives it, they use what they can see (\"soldier\", \"stranger\", \"you there\")."
                ),
            })

    # First-person narration ("I lunge...") — the DM is the world, not the
    # character. Quoted spans are removed first: NPC dialogue legitimately
    # says "I". History momentum makes this sticky; the feedback loop nudges.
    if len(re.findall(r"\b(?:I|I'm|I've|my)\b", prose)) >= 3:
        warnings.append({
            "check": "first_person_narration",
            "detail": (
                "Narration is written in FIRST person ('I lunge...'). Narrate the "
                "player in SECOND person ('you lunge...') — you are the world, "
                "not the character."
            ),
        })

    # The DM decided an uncertain outcome itself instead of asking the engine.
    # This is the check that kills "everything is negotiable".
    if _RISK_RESOLUTION.search(turn_text) and not (succeeded & _RISK_EXEMPT_TOOLS):
        warnings.append({
            "check": "risky_action_without_check",
            "detail": (
                "Narration resolves a risky action (sneaking, persuading, climbing, "
                "lock-picking, spotting) in the player's favour without calling "
                "roll_check. You decided an uncertain outcome yourself — the engine "
                "decides those. Call roll_check BEFORE narrating whether it worked."
            ),
        })

    # Fires even on a legitimate level-up: the numbers are never the player's
    if _MECHANICS_IN_NARRATION.search(turn_text):
        warnings.append({
            "check": "mechanics_in_narration",
            "detail": (
                "Narration printed engine mechanics (experience, levels, hit points, "
                "damage numbers). Progression is engine-owned and invisible: never "
                "write 'level', 'XP', 'experience points', 'you feel stronger', or a "
                "number of damage. If the engine told you the character grew, show it "
                "in the fiction — how they carry themselves, what they no longer "
                "flinch at."
            ),
        })

    # Printing screen directions is always wrong, played cinematic or not
    if _CINEMATIC_TEXT_CLAIM.search(turn_text):
        warnings.append({
            "check": "cinematic_text_in_narration",
            "detail": (
                "Narration prints a cinematic instead of playing one (act/chapter "
                "heading, 'title card', 'cut to black', or screen directions). The "
                "screen is the engine's job: call play_cinematic for the effect and "
                "write only the fiction. If the tool was rejected, narrate the "
                "moment in prose without describing the screen."
            ),
        })

    # Campaign checks — they only run when a campaign exists; the truth check
    # needs two distinctive terms said together (campaign.sealed_truth_hits)
    if campaign:
        # Speaker labels count: in fleet-bram T9-T10 Vex Marrow spoke on screen
        # and "Into the Hollow" was still nagged for never naming him
        said = spoken_text or "\n".join(str(n.get("text") or "") for n in lines)
        speakers = "\n".join(str(n.get("npc_name") or "") for n in lines)
        haystack = "\n".join((turn_text, said, speakers))
        # The same loose match mark_beat_delivery uses, so the audit nags
        # exactly the beats the campaign still holds undelivered — and never
        # one it already marked told another way this turn (fleet8-bram
        # T10-T12: the notice board the player had read for himself)
        beat = campaign.get("beat")
        # ...but never one bound to a place the player is not at: its words
        # are owed there (fleet10-tamsin T4-T5: "SING" nagged in the square
        # was told at T6 as a memory of the Hollow she had never seen)
        if beat and not beat_keywords_delivered(beat, haystack) and _beat_still_owed(beat) \
                and not _beat_waits_elsewhere(beat):
            title = beat.get("title") or "untitled beat"
            warnings.append({
                "check": "campaign_beat_not_delivered",
                "detail": (
                    f"The engine gave you a MUST-DELIVER story beat this turn ('{title}') "
                    "and the world has already changed for it, but your narration never "
                    "mentioned it. Weave it into your next reply at the scale it was written — "
                    "a glimpse stays a glimpse; do not escalate it to make up for the delay."
                ),
            })
        # A sealed truth said aloud: any TWO of its distinctive terms in one
        # paragraph or speech line, names the player knows aside — the test
        # campaign.mark_beat_delivery uses to record it revealed (fleet4-tamsin
        # T19: Maera's "twenty names on his roster... eleven of his men" was
        # missed because Coldiron's name was a required keyword)
        known = list(campaign.get("known_words") or []) + [str(n.get("npc_name") or "") for n in lines]
        # ...with the engine's round-5 rules read the same way: the player's
        # own words are no evidence, and a truth's subject saying its count
        # is (the audit copy carries `cross_terms` and `subjects`)
        leaked = sealed_truth_hits(
            campaign.get("sealed_truths") or [], "\n".join((turn_text, said)), known, player_words=typed,
            speech=[(str(n.get("npc_id") or ""), str(n.get("text") or "")) for n in lines])
        if leaked:
            terms = sorted({term for hits in leaked.values() for term in hits})
            warnings.append({
                "check": "campaign_truth_frontrun",
                "detail": (
                    f"Your reply let a SEALED story truth out ({', '.join(terms)}) before the story "
                    "revealed it. It is out now — the engine records it as known, so build on it and "
                    "never take it back. Keep every other sealed truth out of the characters' mouths: "
                    "no hints, no knowing looks, no one almost saying it."
                ),
            })
        # The villain on stage before the story brings them on (fleet8-tamsin
        # T17-T19: the Wolf-Mantled Man stepped out at the Hollow in act 1 and
        # demanded the whistle back — act 3's setpiece; his dossier says
        # OFFSTAGE, `campaign.villain_offstage`)
        offstage = campaign.get("offstage_villain") or {}
        if offstage.get("id") and any(str(n.get("npc_id") or "") == offstage["id"] for n in lines):
            warnings.append({
                "check": "villain_before_their_scene",
                "detail": (
                    "The story's villain spoke on stage before the story brings them on (their dossier "
                    "says OFFSTAGE). The player saw it, so it happened — but from your next reply they are "
                    "gone: withdrawn into the dark, no more lines, nothing they demanded followed up. Until "
                    "their scene comes, only signs: tracks, a sound in the night, a shape at a distance."
                ),
            })
        # ...and the villain's people held for a scene of their own (round 9,
        # `campaign.cast_offstage`: fleet10-bram's Vex, whose entrance is the
        # whistle's, stood unvoiced in the Hollow while the prose said "alone")
        cast_offstage = {str(c.get("id")): str(c.get("label") or c.get("name") or "")
                         for c in campaign.get("offstage_cast") or [] if isinstance(c, dict) and c.get("id")}
        early = next((cast_offstage[str(n.get("npc_id"))] for n in lines
                      if str(n.get("npc_id") or "") in cast_offstage), None)
        if early:
            warnings.append({
                "check": "cast_before_their_scene",
                "detail": (
                    f"{early} spoke on stage before the story brings them on (their dossier says OFFSTAGE — a "
                    "later beat is their entrance). The player saw it, so it happened, but from your next reply "
                    "they are out of sight again: no more lines, nothing they started followed up. Until their "
                    "beat comes, only signs of them."
                ),
            })
        # A place the player never stood in, told as their memory (fleet10-
        # tamsin T6: "your mind is still back at the Hollow — the hut with its
        # tally marks… the whistle itself lying cracked in the dirt", six
        # replies before she went: the act-1 discovery spent as a false memory)
        recalled = recalled_unvisited_place(turn_text, campaign.get("unvisited_places") or [])
        if recalled:
            place, quote = recalled
            warnings.append({
                "check": "memory_of_unvisited_place",
                "detail": (
                    f"Your reply told the player remembering {place} (\"{_clip_line(quote, 90)}\") — they have "
                    f"never been there. Nothing of {place} is theirs to remember: what is there they have not "
                    "seen, and nobody builds on that line. It waits there until they go with their own @move."
                ),
            })
        warnings.extend(sealed_speech_warnings(campaign, lines, turn_text, typed, told=set(leaked)))

    # Round 8: the dead told as survivors, borrowed names, a reward misnamed
    # (each reads the world only when a line could need it)
    warnings.extend(round8_speech_warnings(lines))
    # A road's length said wrong, in prose or a mouth (round 9: the 45-minute
    # road to the Hollow was "half a day's walk", and the player put it off)
    misstated = distance_warning([turn_text] + [str(n.get("text") or "") for n in lines],
                                 [before.get("location_id"), after.get("location_id")])
    if misstated:
        warnings.append(misstated)
    return warnings


# ---------------------------------------------------------------------------
# distance_misstated (round 9)
# ---------------------------------------------------------------------------
# fleet10-wenna T1 the Captain: "Wolf's Hollow is north past the gate, half a
# day's walk"; T11 Sorrel: "maybe two hours' walk"; fleet10-tamsin T2 "a
# half-day up the north road" — a 45-minute road, and wenna put the main
# quest off for "tomorrow night" (T12). A duration told as a distance to a
# place the world knows, off from the road's own hours by more than
# DISTANCE_SLACK times (and by a quarter hour or more), is flagged.
# Replayed over every transcript on disk (prose and speech): 37 claims, 4
# flagged, all four real misstatements (the three above and fleet2-tamsin T7
# "a day's bones past it"); every right one ("three-quarters of an hour",
# "forty-five minutes", "three minutes", "six minutes off") passes, and the
# times ("the howl still an hour off", "three days gone") are no claims.
DISTANCE_SLACK = 2.0
_SPELLED = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
            "thirty": 30, "forty": 40, "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90, "several": 4,
            "a few": 3, "a couple": 2, "a couple of": 2}
_DAY_HOURS = 10          # a day's walk is a day of walking, not twenty-four hours of it
_DURATION = re.compile(
    r"\b(?P<half>half\s+a\s+day|half-day|half\s+day)"
    r"|\b(?P<dayof>a|one|two|three)\s+day['’]s?(?=\s)"
    r"|\b(?P<qh>three-quarters|three\s+quarters)\s+of\s+an\s+hour"
    r"|\b(?P<hh>half\s+an?\s+hour|half-hour)"
    r"|\b(?P<n>\d+|a\s+few|a\s+couple(?:\s+of)?|several|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven"
    r"|twelve|fifteen|twenty|thirty|forty(?:-five)?|fifty|sixty|ninety)\s+(?P<unit>hours?|hrs?|days?|minutes?|mins?)"
    r"\b['’]?",
    re.IGNORECASE,
)
# Said as a way to go: a travel word just after it ("a half-day up the north
# road", "two hours' walk", "three minutes that way") or just before it
# ("just past the well — three minutes")
_DISTANCE_AFTER = re.compile(
    r"^\W*(?:s\s+)?(?:[\w'’-]+\s+){0,2}?(?:walk\w*|ride|riding|march|journey|trek|hike|off|away|up|down|north\w*"
    r"|south\w*|east\w*|west\w*|out|back|past|along|ahead|on\s+foot|of\s+(?:road|walking|dark|footsteps|track|trail)"
    r"|from\s+here|that\s+way|this\s+way|by\s+road|bones)\b",
    re.IGNORECASE,
)
_DISTANCE_BEFORE = re.compile(
    r"\b(?:walk\w*|ride|march|journey|trek|road|path|distance|past|north|south|east|west|off|away|ahead)\b"
    r"[^.!?]{0,40}$",
    re.IGNORECASE,
)
# ...never a time: "in two hours", "less than an hour", "for a day", "a day
# late", "an hour ago", "six hours before dawn", "a day's walk less if…"
# (and "the howl still an hour off" is a time too)
_TIME_BEFORE = re.compile(r"\b(?:in|for|within|than|after|every|each|last|over|still|yet)\s*$", re.IGNORECASE)
_TIME_AFTER = re.compile(
    r"^\W*(?:s\s+)?(?:[\w'’-]+\s+)?(?:ago|late|overdue|before|till|until|since|left|dead|gone|more|longer|less"
    r"|shorter|to\s+go|from\s+now|behind|old|later|earlier|early|of\s+(?:sleep|rest|daylight|light)"
    r"|past(?=\s*(?:[,.;:!?—–]|$)))\b",
    re.IGNORECASE,
)
# Map containers name no road's end ("the road back to Emberfall")
_CONTAINER_TYPES = {"root", "world", "region_1", "region_2", "region_3", "continent", "kingdom", "province",
                    "galaxy", "sector", "system", "planet", "settlement", "city", "town", "village"}
_PLACE_HEAD_SKIP = {"the", "old", "north", "south", "east", "west", "great", "little", "inn", "house", "hall"}


def distance_claims(text: str) -> list[tuple[str, str, float]]:
    """(sentence, phrase, hours) for each duration `text` tells as a way to
    go (`_DURATION` beside a travel word, never a time — `_TIME_BEFORE`,
    `_TIME_AFTER`)."""
    out: list[tuple[str, str, float]] = []
    for sentence in re.split(r"(?<=[.!?…])\s+|\n+", text or ""):
        for m in _DURATION.finditer(sentence):
            before, after = sentence[:m.start()], sentence[m.end():]
            if _TIME_BEFORE.search(before) or _TIME_AFTER.search(after):
                continue
            if not (_DISTANCE_AFTER.search(after) or _DISTANCE_BEFORE.search(before)):
                continue
            if m.group("half"):
                hours = _DAY_HOURS / 2
            elif m.group("dayof"):
                hours = _SPELLED.get(m.group("dayof").lower(), 1) * _DAY_HOURS
            elif m.group("qh"):
                hours = 0.75
            elif m.group("hh"):
                hours = 0.5
            else:
                raw = re.sub(r"\s+", " ", m.group("n").lower())
                count = float(raw) if raw.isdigit() else float(_SPELLED.get(raw, 1))
                unit = m.group("unit").lower()
                hours = count / 60 if unit.startswith("m") else count * _DAY_HOURS if unit.startswith("d") else count
            out.append((sentence.strip(), re.sub(r"['’]s?$", "", m.group(0).strip()), hours))
    return out


def _road_hours(session, start_id: str) -> dict[str, float]:
    """Hours by road from `start_id` to every place (every authored
    connection, and the doorway between a place and a room inside it —
    never through a map container)."""
    import heapq

    from src.models import Connection, Location
    from src.services.travel import HIERARCHY_HOP_HOURS

    places = {loc.id: loc for loc in session.query(Location).all()}

    def container(loc) -> bool:
        return str(getattr(loc.type, "value", loc.type) or "").lower() in _CONTAINER_TYPES

    edges: dict[str, list[tuple[str, float]]] = {}
    for conn in session.query(Connection).all():
        hours = float(conn.travel_time_hours or 0.5)
        edges.setdefault(conn.from_location_id, []).append((conn.to_location_id, hours))
        if conn.bidirectional:
            edges.setdefault(conn.to_location_id, []).append((conn.from_location_id, hours))
    for loc in places.values():
        parent = places.get(loc.parent_id) if loc.parent_id else None
        if parent is not None and not container(parent) and not container(loc):
            edges.setdefault(parent.id, []).append((loc.id, HIERARCHY_HOP_HOURS))
            edges.setdefault(loc.id, []).append((parent.id, HIERARCHY_HOP_HOURS))
    best = {start_id: 0.0}
    queue = [(0.0, start_id)]
    while queue:
        hours, here = heapq.heappop(queue)
        if hours > best.get(here, float("inf")):
            continue
        for nxt, cost in edges.get(here, ()):
            if hours + cost < best.get(nxt, float("inf")):
                best[nxt] = hours + cost
                heapq.heappush(queue, (hours + cost, nxt))
    return best


def _places_named(session, sentence: str) -> list[tuple[int, Any]]:
    """(position, Location) of each place this sentence names — its whole
    name, or its last word ("the Hollow", "the gatehouse") — first-named
    first; never a map container."""
    from src.models import Location

    found: list[tuple[int, Any]] = []
    lowered = sentence.lower().replace("’", "'")
    for loc in session.query(Location).all():
        if str(getattr(loc.type, "value", loc.type) or "").lower() in _CONTAINER_TYPES:
            continue
        name = re.sub(r"^the\s+", "", (loc.name or "").lower().replace("’", "'")).strip()
        if not name:
            continue
        m = re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", lowered)
        words = re.findall(r"[a-z]+", name)
        head = words[-1] if words else ""
        if m is None and len(head) >= 4 and head not in _PLACE_HEAD_SKIP:
            m = re.search(rf"(?:\bthe\s+|(?<![\w'-])){re.escape(head)}(?![\w-])", lowered)
        if m is not None:
            found.append((m.start(), loc))
    found.sort(key=lambda f: f[0])
    return found


def _fits(claimed: float, actual: float) -> bool:
    return abs(claimed - actual) < 0.25 or actual / DISTANCE_SLACK <= claimed <= actual * DISTANCE_SLACK


def distance_warning(texts: Iterable[str], here_ids: Iterable[str | None]) -> dict[str, str] | None:
    """distance_misstated for this reply's prose and speech, or None: a
    duration told as the way to a place the world knows (`distance_claims`)
    that fits no road to it — not from where the player stands, nor from
    another place the same sentence names ("past the gate, half a day's
    walk") — within DISTANCE_SLACK. Reads the world only when a claim is
    made. Never raises."""
    try:
        claims = [c for t in texts or () for c in distance_claims(t)]
        if not claims:
            return None
        from src.models import get_session
        from src.services.scene_brief import travel_words

        origins = [i for i in dict.fromkeys(here_ids or ()) if i]
        with get_session() as session:
            roads: dict[str, dict[str, float]] = {}

            def hours(start: str, end: str) -> float | None:
                if start not in roads:
                    roads[start] = _road_hours(session, start)
                return roads[start].get(end)

            for sentence, phrase, claimed in claims:
                named = _places_named(session, sentence)
                if not named:
                    continue
                fit, worst = False, None
                for _, place in named:
                    starts = origins + [p.id for _, p in named if p.id != place.id]
                    for start in starts:
                        actual = hours(start, place.id)
                        if actual is None or actual <= 0:
                            continue
                        if _fits(claimed, actual):
                            fit = True
                        elif worst is None and start in origins:
                            worst = (place, actual)
                if fit or worst is None:
                    continue
                place, actual = worst
                words = travel_words(actual)
                return {
                    "check": "distance_misstated",
                    "detail": (
                        f"Your reply put {place.name} {phrase} away (\"{_clip_line(sentence, 110)}\"), but the road "
                        f"there from where the player stands is {words}. The map holds: from your next reply "
                        f"anyone who speaks of it says {words} (the brief's exits give each road in words), and "
                        "nobody plans around a longer or shorter trip than that."
                    ),
                }
    except Exception:
        import logging
        logging.getLogger(__name__).warning("distance audit failed", exc_info=True)
    return None


def sealed_speech_warnings(campaign: dict[str, Any], lines: list[dict[str, Any]], prose: str,
                           typed: str, *, told: set[str] | None = None) -> list[dict[str, str]]:
    """sealed_truth_cut and sealed_word_slipped for one reply (round 8).

    `campaign` is the tick's audit copy: its `player_id` keys the lines the
    stream's guard cut (`campaign.seal_line`, fleet9-bram T1), and its
    `sealed_words` are each sealed truth's thing-words the player had not
    met when the turn began (fleet9-tamsin T18's "whistle" was met: the main
    quest's fourth step named it on her quest log since T3). This reply's
    prose and the player's own words are met too. `told`: truths the prose
    let out anyway (campaign_truth_frontrun) — a cut voiced copy of a quote
    the prose already showed is no "never heard". Never raises."""
    warnings: list[dict[str, str]] = []
    try:
        from src.services.campaign import pop_seal_cuts, sealed_word_slips

        cuts = [c for c in pop_seal_cuts(campaign.get("player_id"))
                if not set(c.get("truth_ids") or ()) <= set(told or ())]
        if cuts:
            first = cuts[0]
            warnings.append({
                "check": "sealed_truth_cut",
                "detail": (
                    f"{first['speaker']}'s line told a SEALED story truth ({', '.join(first['terms'])}) before "
                    "the story reveals it — the engine cut it from the line, so the player never heard it and it "
                    "stays sealed (your history still shows the words: they were NOT said). No roll, not even "
                    "a critical success, buys a sealed truth early: a won press gets a tell (a pause, a count "
                    "done twice, a look away) or their cover story. It comes out when a beat says so."
                ),
            })
        slips = sealed_word_slips(
            campaign.get("sealed_words") or {},
            [(str(n.get("npc_name") or ""), str(n.get("npc_id") or ""), str(n.get("text") or "")) for n in lines],
            known=[prose or "", typed or ""], exempt_ids=campaign.get("word_holders") or ())
        if slips:
            slip = slips[0]
            warnings.append({
                "check": "sealed_word_slipped",
                "detail": (
                    f"{slip['speaker']} said \"{slip['word']}\" (\"{_clip_line(slip['line'], 90)}\") — a word of a "
                    "SEALED story truth the player had never met. It is said now, so nobody takes it back or "
                    "denies it; but it opens nothing: no one explains it, adds to it or builds on it until the "
                    "story's beat does. Keep every sealed truth's words out of the characters' mouths."
                ),
            })
    except Exception:
        import logging
        logging.getLogger(__name__).warning("sealed speech audit failed", exc_info=True)
    return warnings


_ROUND8_CUES = re.compile(
    r"\b(?:surviv|alive|came\s+back|come\s+back|walked\s+(?:away|out|back|off)|recover|since"
    r"|old|young|little|before\s+(?:me|my\s+time|I\b)"
    r"|coin|silver|pay|pays|reward|off\s+one\s+of|cut|bring)",
    re.IGNORECASE,
)


def round8_speech_warnings(lines: list[dict[str, Any]]) -> list[dict[str, str]]:
    """lost_person_alive, name_collision and reward_misnamed over this
    reply's speaker lines (`lines`: {npc_name, npc_id, text}). The world is
    read only when a line carries one of their cues. Never raises."""
    warnings: list[dict[str, str]] = []
    try:
        lines = [n for n in lines or [] if isinstance(n, dict) and str(n.get("text") or "").strip()]
        if not lines or not any(_ROUND8_CUES.search(str(n.get("text") or "")) for n in lines):
            return warnings
        alive = lost_person_alive(lines, lost_people_for_audit())
        if alive:
            warnings.append(alive)
        here = {str(n.get("npc_id")) for n in lines if n.get("npc_id")}
        borrowed = name_collision(lines, people_for_collision(), present_ids=here | _present_with(here))
        if borrowed:
            warnings.append(borrowed)
        misnamed = reward_misnamed(lines, offers_for_audit(here))
        if misnamed:
            warnings.append(misnamed)
    except Exception:
        import logging
        logging.getLogger(__name__).warning("round-8 speech audit failed", exc_info=True)
    return warnings


def _present_with(speaker_ids: set[str]) -> set[str]:
    """Everyone standing where this reply's speakers stand. Never raises."""
    if not speaker_ids:
        return set()
    try:
        from src.models import NPC, get_session

        with get_session() as session:
            places = {n.current_location_id for n in (session.get(NPC, i) for i in speaker_ids)
                      if n is not None and n.current_location_id}
            if not places:
                return set()
            return {row[0] for row in session.query(NPC.id).filter(NPC.current_location_id.in_(list(places))).all()}
    except Exception:
        return set()


def _beat_still_owed(beat: dict[str, Any]) -> bool:
    """Is the audit copy's beat still `fired` in the campaign? The engine
    marks delivery (`campaign.mark_beat_delivery`) before the audit runs; a
    beat it no longer holds as fired was told some other way. No id or no
    readable row: owed, as before."""
    beat_id = beat.get("beat_id")
    if not beat_id:
        return True
    try:
        from src.models import StoryBeat, get_session

        with get_session() as session:
            row = session.get(StoryBeat, beat_id)
            return row is None or row.status == "fired"
    except Exception:
        return True


def _beat_waits_elsewhere(beat: dict[str, Any]) -> bool:
    """Is the audit copy's beat bound to a place the player is not at now
    (`campaign.beat_waits_elsewhere`)? False when unreadable."""
    try:
        from src.services.campaign import beat_waits_elsewhere

        return beat_waits_elsewhere(beat.get("beat_id"))
    except Exception:
        return False


# A place told as the player's own memory: their mind "still back at" it,
# "you remember … at", "since you left", "you can still see … in", "the
# memory of" — then the place by its name or its last word ("the Hollow").
# Narration only: a speaker's own past is theirs. Replayed over every fleet
# transcript (fleet-fleet10, 29 runs) with each run's visits rebuilt from its
# turns: one hit, fleet10-tamsin T6 — the round's false memory.
_MEMORY_OF = (
    r"\b(?:mind|minds|thoughts?|memory|memories|heart)\b(?:\s+[\w'’]+){0,4}?\s+(?:back|still)\s+"
    r"(?:at|in|on|inside)\s+"
    r"|\byou\s+(?:remember|recall)\s+(?:[\w'’,]+\s+){0,6}?(?:at|in|inside|from)\s+"
    r"|\b(?:when|since|after)\s+you\s+(?:were|stood|left|first\s+saw|last\s+saw|came\s+(?:back\s+)?from"
    # (never across a word that sends or tells: "since you were sent to the
    # Hollow", "when you left for the Hollow", "after you were told about it")
    r"|walked\s+out\s+of)\s+(?:(?!(?:to|for|toward|towards|about|of|into|sent|told|asked|bound|headed|heading"
    r"|going|set)\b)[\w'’]+\s+){0,3}?(?:at\s+|in\s+|from\s+)?"
    r"|\b(?:can\s+)?still\s+(?:see|picture|smell|hear)\b[^.!?\n]{0,60}?\b(?:at|in|inside)\s+"
    r"|\bthe\s+(?:memory|memories)\s+of\s+"
)
_SPEAKER_ROW = re.compile(r"^\s*[^:\n\"“]{1,60}:\s*[\"“].*$", re.MULTILINE)
_QUOTED_SPAN = re.compile(r"\"[^\"\n]*\"|“[^”\n]*”")


def recalled_unvisited_place(text: str, places: list[str]) -> tuple[str, str] | None:
    """(place, the sentence) when the narration tells a place in `places`
    (never visited) as the player's memory (see above), else None."""
    if not text or not places:
        return None
    narration = _QUOTED_SPAN.sub(" ", _SPEAKER_ROW.sub(" ", text))
    for place in places:
        name = " ".join(str(place or "").split())
        if len(name) < 4:
            continue
        head = re.sub(r"^the\s+", "", name, flags=re.IGNORECASE).split()[-1]
        alts = [re.escape(re.sub(r"^the\s+", "", name, flags=re.IGNORECASE)).replace("'", "['’]")]
        if len(head) >= 4 and head.lower() != alts[0].lower():
            alts.append(re.escape(head))
        found = re.search(rf"(?:{_MEMORY_OF})(?:the\s+)?(?:{'|'.join(alts)})\b", narration, re.IGNORECASE)
        if found:
            start = max(narration.rfind(".", 0, found.start()), narration.rfind("\n", 0, found.start())) + 1
            end = narration.find(".", found.end())
            return name, narration[start:end if end != -1 else len(narration)].strip()
    return None


# ---------------------------------------------------------------------------
# Names and voices (fleet4 round 3)
# ---------------------------------------------------------------------------
# These read what the stream already decided (src/web/dialogue_markup.py,
# src/services/dialogue.NameGate) and tell the DM what to write next time.

def unintroduced_name_warning(labels: list[tuple[str, str]], prose: list[tuple[str, str]]) -> dict[str, str] | None:
    """A real name the player never heard, on a speaker label or in the prose.

    fleet4: "Wolf-Mantled Man" became "Aldric Vane" with nobody saying it
    (bram T4->T5; Piper, Maera, Sorrel and the Captain the same way), and the
    prose named "Vex Marrow" while his label stayed "Ash-Cloaked Man"
    (wenna T11). The stream showed the player what they know instead; this
    tells the DM how a name is learned. `labels`/`prose`: (what the DM
    wrote, what the player saw).
    """
    seen: list[tuple[str, str]] = []
    for written, shown in list(labels or []) + list(prose or []):
        pair = (str(written).strip(), re.sub(r"^[Tt]he\s+", "", str(shown).strip()))
        if pair[0] and pair not in seen:
            seen.append(pair)
    if not seen:
        return None
    named = "; ".join(f"'{w}' (the player knows them as '{s}')" for w, s in seen[:4])
    # The player saw nothing wrong, so nothing in the story wants mending
    # (fleet7-wenna: audit lines sent replies fixing what nobody saw)
    return {
        "check": "unintroduced_name",
        "detail": (
            f"You named {named}, but nobody has said that name where the player could hear it — "
            "the engine showed what the player knows instead, so the story needs no repair. A name "
            "reaches the player only when someone SAYS it aloud in a speaker line (their own, or someone "
            "present); until then use the name the player knows, as the speaker label and in prose."
        ),
    }


def player_name_cut_warning(cuts: list[tuple[str, str]]) -> dict[str, str] | None:
    """The player's name in the mouth of someone who never heard it, cut from
    the line before anyone saw it (`dialogue.NameGate.player_name_in`).

    fleet7 (all three runs): Piper said "Straight lines, Bram" (bram T16,
    T18) and "Someone trained them, Tamsin" (tamsin T11) — her dossier said
    "Knows the player's name: no", and the audit only caught it once the
    player had read it. Same check as `_names_before_introduction`: the
    scoreboard counts one habit. `cuts`: (speaker as shown, the name)."""
    seen: list[tuple[str, str]] = []
    for speaker, name in cuts or []:
        if (speaker, name) not in seen:
            seen.append((speaker, name))
    if not seen:
        return None
    named = "; ".join(f"{s or 'Someone'} called the player \"{n}\"" for s, n in seen[:3])
    return {
        "check": "name_before_introduction",
        "detail": (
            f"{named} — a name nobody ever told them (their dossier says \"Knows the player's name: no\"); "
            "the engine cut it from the line before the player saw it. Until the player gives their name, "
            "they call them what they see (\"soldier\", \"stranger\") or nothing at all, and no one remarks "
            "on the name."
        ),
    }


def voiced_player_warning(lines: list[dict[str, Any]]) -> dict[str, str] | None:
    """A speaker line in the player character's own voice (dropped by the
    markup filter). fleet4-bram T15: `Bram Holloway: "Anyone been through
    here lately…"` — the player had to ask again in their own words."""
    lines = [line for line in lines or [] if isinstance(line, dict)]
    if not lines:
        return None
    first = lines[0]
    sample = f"{first.get('speaker') or 'You'}: \"{str(first.get('text') or '')[:70]}\""
    return {
        "check": "voiced_the_player",
        "detail": (
            f"You wrote the player character's own words as a speaker line ({sample}) — the engine "
            "dropped it. Never speak for the player: end on what the world says or does, and let "
            "them say their own words."
        ),
    }


# Capitalised words that are never somebody's name
_NOT_PEOPLE = frozenset({
    "i", "i'm", "i'll", "i've", "i'd", "god", "gods", "goddess", "hell", "hells", "heaven", "heavens",
    "saints", "saint", "christ", "ma'am", "mum", "ma", "pa", "da", "aye", "nay", "ok", "okay", "oi",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february",
    "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
    "north", "south", "east", "west", "northern", "southern", "eastern", "western", "mister", "missus",
    # fleet7-wenna T7: Sorrel's "Thursdays are always the loudest" was a person
    "mondays", "tuesdays", "wednesdays", "thursdays", "fridays", "saturdays", "sundays",
    # fleet10-bram T4: the Watchman's "'Scuse me, Sergeant—" named a 'Scuse
    "scuse", "tis", "twas", "twere", "ere", "cept", "nuff", "fraid",
})
# A capital right after an opening apostrophe opens a single-quoted line or
# is a clipped word ('Scuse, 'Tis): a sentence start either way
_OPENING_APOSTROPHE = re.compile(r"(?:^|[\s\"“(\[—–-])['’‘]$")
# A capitalised run ending in one of these is a place, not a person
_PLACE_NOUNS = frozenset({
    "road", "street", "gate", "gates", "bridge", "ford", "hill", "hills", "mill", "inn", "tavern",
    "hall", "keep", "tower", "wall", "walls", "square", "market", "hollow", "wood", "woods", "forest",
    "river", "lake", "pass", "rest", "crossing", "well", "cross", "lane", "way", "row", "house",
    "manor", "abbey", "temple", "church", "chapel", "docks", "harbor", "harbour", "port", "bay",
    "point", "field", "fields", "farm", "moor", "fen", "marsh", "vale", "valley", "peak", "mountain",
    "mountains", "cave", "caves", "mine", "mines", "camp", "fort", "castle", "village", "town", "city",
})
_PERSON_TITLE_WORDS = frozenset((_HONORIFICS - {"the", "of", "von", "van", "de", "del", "da", "le", "la"}) | {
    "old", "young", "lord", "lady", "king", "queen", "prince", "princess", "duke", "baron", "count",
    "general", "colonel", "corporal", "private", "admiral", "constable", "sheriff", "magistrate",
    "judge", "granny", "uncle", "aunt", "goodwife", "maester", "septon", "septa", "abbot", "abbess",
    "bishop", "sentry", "guard", "watchman", "carter", "sergeant",
})
_CAP_WORD = re.compile(r"[A-ZÀ-Þ][\w'’-]*")
# A sentence also opens after one that ended inside a quote: fleet-bram T10
# "'What are we hunting.' Careful with that word" read "Careful" as a name
_OPENS_SENTENCE = re.compile(r"(?:^|[.!?…:;\"“‘(\[*_—–]\s*|[.!?…][\"”'’)\]*_]+\s+|\s-\s*|\n\s*)$")
_ARTICLE_BEFORE = re.compile(r"\b(?:the|a|an)\s+$", re.IGNORECASE)


def _bare_word(word: str) -> str:
    return re.sub(r"['’]s?$", "", word).lower()


def unknown_named_people(texts: list[str], known: set[str], common: set[str]) -> list[str]:
    """Names of people in `texts` (speech lines, @journal entries) who exist
    nowhere in the world.

    fleet4-wenna T16: Sorrel named "Garrick", a wall sentry with no NPC row,
    and the player built Day 2 around finding him; fleet4-bram T18: Maera
    named "Corvin Ashe" for an authored ledger that is deliberately
    nameless. Neither could be tracked, marked or met.

    `known`: lowercased words of every name and term the world holds (NPCs,
    places, factions, items, quests, the world bible, the player — see
    `dialogue.world_words`). `common`: words seen in lowercase anywhere
    (authored text, this reply, the player's input) — a word that is ever
    written small is a word, not a name. Conservative on purpose: a single
    capitalised word that opens a sentence is never taken for a name (only a
    title, a second name word or a mid-sentence capital says "person"), a
    run after "the" is an institution or a place, and a run that ends in a
    place noun ("Garrick's Ford") is a place.
    """
    found: list[str] = []
    for text in texts or []:
        text = str(text or "")
        words = list(_CAP_WORD.finditer(text))
        i = 0
        while i < len(words):
            run = [words[i]]
            while (i + len(run) < len(words)
                   and text[run[-1].end():words[i + len(run)].start()] == " "):
                run.append(words[i + len(run)])
            i += len(run)
            before = text[:run[0].start()]
            if _ARTICLE_BEFORE.search(before) or run[0].group(0) in ("The", "A", "An"):
                continue
            opens = bool(_OPENS_SENTENCE.search(before) or _OPENING_APOSTROPHE.search(before))
            bare = [_bare_word(m.group(0)) for m in run]
            # "Wolf's Hollow", "Harrow farm": a place
            if (bare[-1] in _PLACE_NOUNS and len(bare) > 1) or re.match(
                    rf"\s+(?:{'|'.join(_PLACE_NOUNS)})\b", text[run[-1].end():]):
                continue
            titles: list[str] = []
            names: list[tuple[str, str]] = []        # (as written, bare)
            for m, low in zip(run, bare):
                if not names and low in _PERSON_TITLE_WORDS:
                    titles.append(m.group(0))
                    continue
                if low in _NOT_PEOPLE or low in _PERSON_TITLE_WORDS:
                    continue
                names.append((re.sub(r"['’]s$", "", m.group(0)), low))
            # Ordinary words capitalised only because the sentence starts there
            while opens and not titles and names and names[0][1] in common:
                names.pop(0)
                opens = False
            # A name is a word of three letters or more ("M on the handle" is
            # a mark); a compound is known when any part is
            # ("Holloway-who-didn't-give-his-name")
            names = [(w, low) for w, low in names if len(low) >= 3]
            if not names or any(set(low.split("-")) & known or low in known for _, low in names):
                continue
            if all(low in common for _, low in names):
                continue
            if opens and not titles and len(names) == 1:
                continue        # "Garrick's not on tonight" — could be a word; the mid-sentence use flags
            shown = " ".join(titles + [w for w, _ in names])
            # One person named twice ("Tomas Reed", then "Tomas") is one name
            said = {low for _, low in names}
            if any(said <= {_bare_word(w) for w in f.split()} for f in found):
                continue
            found = [f for f in found if not {_bare_word(w) for w in f.split()} <= said] + [shown]
    return found


def unknown_person_warning(names: list[str]) -> dict[str, str] | None:
    if not names:
        return None
    # fleet7-wenna T1-T2: told to register "Fenwick", the DM made a second dead
    # carter — the world's own (a letter to "Tobin") was the one it meant
    return {
        "check": "unknown_named_person",
        "detail": (
            f"Your reply named {', '.join(repr(n) for n in names[:4])}, but nobody by that name exists "
            "in the world, so the player cannot find, follow or meet them. If you meant someone the world "
            "already has (people in your brief, 'People the story has lost'), use their name from now on; "
            "otherwise @new-npc <name> (and @new-place if they are somewhere new) or drop the name."
        ),
    }


def format_audit_feedback(warnings: list[dict[str, str]]) -> str:
    """Context block injected into the DM's NEXT turn so it self-corrects."""
    if not warnings:
        return ""
    lines = "\n".join(f"- {w['detail']}" for w in warnings)
    return (
        "\n\n[REALITY AUDIT of your previous turn — the engine checked your reply "
        "against the world:\n"
        f"{lines}\n"
        "Where your prose claimed a change, the database did NOT change. THIS turn, "
        "make it real with the right @command or stop asserting it. Never print "
        "mechanics or consequence lists in narration.]"
    )


# ---------------------------------------------------------------------------
# Wounds and dice that cost nothing (fleet5, round 4)
# ---------------------------------------------------------------------------
# These read what the reply's @commands applied and what the dice said, not
# the prose's claims: the question is what the reply should have WRITTEN.

_COSTLY_OUTCOMES = ("partial", "failure", "critical_failure")
_OUTCOME_WORDS = {
    "partial": "PARTIAL — they get what they wanted, at a price",
    "failure": "FAILURE — it went wrong, and wrong costs something",
    "critical_failure": "CRITICAL FAILURE — it went badly wrong",
}
_RISING_TENSION = ("rising", "high", "climax")
# A blow landing on the player, in the prose: "<them> rakes your shoulder" —
# the attacker before the verb and the player after it, in one clause
_ATTACK_VERB = (
    r"\b(?:rakes?|raked|bites?|bit|bitten|claws?|clawed|slash(?:es|ed)?|stabs?|stabbed|strikes?|struck"
    r"|hits?|lunges?|lunged|slams?|slammed|punch(?:es|ed)?|cuts?|maul(?:s|ed)?|gores?|gored"
    r"|tears?|tore|knocks?|knocked|kicks?|kicked|batters?|battered|smash(?:es|ed)?|charges?|charged"
    r"|leaps?|leapt|springs?|sprang|snaps?|snapped|clips?|clipped|swipes?|swiped)\b"
)   # no "catches": in a fall or a trap it is the helper ("Sorrel catches your elbow")
_BLOW = r"[^.!?\n]{0,60}?" + _ATTACK_VERB + r"[^.!?\n]{0,40}?\byou(?:r|rself)?\b"
# A @health cause that names a PERSON is their doing only when it says how:
# "hauling Sorrel out of the ditch" names him as the rescued, not the blow.
# A beast named in a cause is the blow ("a wolf's jaws closing on her
# forearm", fleet-wenna T6)
_VIOLENCE = re.compile(
    _ATTACK_VERB + r"|\b(?:fists?|knuckles|backhand|blows?|elbow|boots?|blades?|knife|knives|daggers?|"
    r"swords?|clubs?|cudgels?|axes?|spears?|arrows?|bolts?|crossbows?|jaws?|teeth|fangs?|talons?)\b",
    re.IGNORECASE,
)
_NAME_STOP = {"the", "a", "an", "of", "captain", "sergeant", "lord", "lady", "sir", "dame", "master",
              "mistress", "old", "young", "mother", "father", "brother", "sister", "doctor", "watch",
              "gate", "guard", "man", "woman"}


def _health_worse(label: str | None, than: str | None) -> bool:
    try:
        return _HEALTH_LADDER.index(str(label)) < _HEALTH_LADDER.index(str(than or "healthy"))
    except ValueError:
        return False


def _health_label(text: str) -> str:
    """A @health line's label as the engine reads it ("badly hurt", "wounded")."""
    from src.services import commands as cmds

    label = re.sub(r"[\s-]+", "_", (text or "").strip().lower())
    return getattr(cmds, "_HEALTH_SYNONYMS", {}).get(label, label)


def _applied(commands: Iterable[Any]) -> list[tuple[str, Any, dict]]:
    """(verb, parsed line, engine result) of each command that landed. Takes
    CommandRunner results or the {"line", "ok"} rows a playtest transcript keeps."""
    from src.services import commands as cmds

    aliases = getattr(cmds, "_ALIASES", {})
    out = []
    for row in commands or []:
        get = row.get if isinstance(row, dict) else (lambda key, _r=row: getattr(_r, key, None))
        if not get("ok"):
            continue
        parsed = cmds.parse_command(str(get("line") or ""))
        if parsed is None:
            continue
        out.append((aliases.get(parsed.verb, parsed.verb), parsed, get("result") or {}))
    return out


def _first_value(parsed: Any) -> str:
    return parsed.main or (parsed.extras[0] if parsed.extras else "")


def people_for_audit(player_id: str | None, npc_ids: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The people the wound check matches against: the brief's present ids
    plus whoever stands where the player is now (a reply can @move into the
    wolves). {id, name, label, beast, species} — species words of a beast.
    Never a companion: they do not strike the player, and @fight refuses
    a party member as an enemy."""
    from src.models import NPC, Player, get_session
    from src.services import party as party_mod
    from src.services.dialogue import is_beast, role_label

    try:
        from src.services.image_generator import ImageGenerator
        animal_words = set(ImageGenerator._BEAST_WORDS)
    except Exception:
        animal_words = set()
    people: list[dict[str, Any]] = []
    with get_session() as session:
        player = session.get(Player, player_id) if player_id else None
        ids = list(dict.fromkeys(i for i in npc_ids or () if i))
        if player is not None and player.current_location_id:
            ids += [n.id for n in session.query(NPC).filter(
                NPC.current_location_id == player.current_location_id, NPC.status == "alive"
            ).all() if n.id not in ids]
        party = set(party_mod.member_ids(player)) if player is not None else set()
        for npc_id in ids:
            npc = session.get(NPC, npc_id)
            if npc is None or npc.id in party:
                continue
            beast = bool(is_beast(npc))
            words = set(re.findall(r"[a-z]+", f"{npc.name} {npc.species or ''}".lower()))
            species = sorted(words & animal_words) if beast else []
            people.append({"id": npc.id, "name": npc.name, "label": role_label(npc),
                           "beast": beast, "species": species})
    return people


def _mention(person: dict[str, Any]) -> re.Pattern | None:
    """Words that name this person in prose: their name's own words, their
    role label whole, a beast's species ("wolf", "wolves")."""
    alts: list[str] = []
    for word in re.findall(r"[A-Za-z][\w'’-]*", str(person.get("name") or "")):
        if len(word) >= 3 and word.lower() not in _NAME_STOP:
            alts.append(re.escape(word))
    label = str(person.get("label") or "").strip()
    if label and label.lower() != str(person.get("name") or "").lower() and label.lower() != "stranger":
        alts.append(re.escape(label).replace(r"\ ", " ").replace(" ", r"[\s-]+"))
    for species in person.get("species") or []:
        stem = species[:-1] if species.endswith("s") and not species.endswith("ss") else species
        alts.append(rf"{re.escape(stem)}(?:s|es)?" if not stem.endswith("f") else
                    rf"(?:{re.escape(stem)}|{re.escape(stem[:-1])}ve)s?")
    return re.compile(r"\b(?:" + "|".join(alts) + r")\b", re.IGNORECASE) if alts else None


def wound_without_fight(commands: Iterable[Any], people: list[dict[str, Any]], prose: str,
                        health_before: str | None) -> dict[str, str] | None:
    """A @health loss dealt by someone who is HERE, with no @fight.

    fleet5-bram T8: Bram called into the dark, hand on his knife; the DM
    rolled, failed him, and wrote `@health winded | cause: a wolf breaks
    cover and rakes a shoulder` — Gray-Flank Wolf stood in the brief. dm.md:
    violence by named people (and beasts) who are here is a fight. The cause
    names them, or a prose sentence has them strike at the player."""
    applied = _applied(commands)
    if any(verb == "fight" for verb, _, _ in applied):
        return None
    current = health_before or "healthy"
    for verb, parsed, _ in applied:
        if verb != "health":
            continue
        label = _health_label(parsed.main)
        if not _health_worse(label, current):
            current = label if label in _HEALTH_LADDER else current
            continue
        current = label
        cause = " ".join([parsed.opt("cause") or "", *parsed.extras])
        for person in people or []:
            pattern = _mention(person)
            if pattern is None:
                continue
            in_cause = bool(pattern.search(cause)) and (person.get("beast") or bool(_VIOLENCE.search(cause)))
            if not in_cause and not re.search(pattern.pattern + _BLOW, prose or "", re.IGNORECASE):
                continue
            name = person["name"]
            return {
                "check": "wound_without_fight",
                "detail": (
                    f"Your reply hurt the player (@health {label}) and the blow came from {name}, who is "
                    f"here — that is a fight, not a wound by fiat. They attacked: answer with @fight "
                    f"{name} this reply, or have them withdraw (@npc-move {name} | <place>) and say "
                    "where they went. Never settle violence from someone in the scene with @health alone."
                ),
            }
    return None


def partial_without_cost(commands: Iterable[Any], outcomes: Iterable[str],
                         health_before: str | None, player_input: str = "") -> dict[str, str] | None:
    """A roll that came back partial or failed, and a reply that charged
    nothing for it.

    fleet5-tamsin T10: a partial on asking Coldiron for his pass handed over
    the whole pass, free. A partial is success AT A PRICE and a failure costs
    something real; the price is a command: time beyond the turn's minimum,
    a wound, coin, someone's trust or standing, a thing lost, the danger
    rising — or the fight the failure starts. What `player_input` gave away
    by choice is no price (fleet8-tamsin T9: a tossed coin stood as the cost
    of a critical failure; stakes.chosen_payment)."""
    from src.services.commands import parse_duration, parse_signed, split_trailing_number
    from src.services.stakes import chosen_payment
    from src.services.world_tick import MIN_TURN_HOURS

    costly = [o for o in outcomes or () if o in _COSTLY_OUTCOMES]
    if not costly:
        return None
    current = health_before or "healthy"
    for verb, parsed, _ in _applied(commands):
        value = _first_value(parsed)
        if verb in ("coin", "remove", "hand") and player_input and chosen_payment(parsed, player_input):
            continue
        if verb in ("fight", "remove", "hand"):
            return None
        if verb == "time" and (parse_duration(value) or 0) > MIN_TURN_HOURS * 60:
            return None
        if verb == "health":
            label = _health_label(parsed.main)
            if _health_worse(label, current):
                return None
            current = label if label in _HEALTH_LADDER else current
        if verb == "coin" and (parse_signed(value) or 0) < 0:
            return None
        if verb in ("trust", "rep"):
            delta = split_trailing_number(parsed.main)[1]
            if delta is None:
                delta = parse_signed(parsed.extras[0]) if parsed.extras else None
            if delta is not None and delta < 0:
                return None
        if verb == "tension" and parsed.main.strip().lower() in _RISING_TENSION:
            return None
    worst = max(costly, key=_COSTLY_OUTCOMES.index)
    return {
        "check": "partial_without_cost",
        "detail": (
            f"Your roll_check came back {_OUTCOME_WORDS[worst]} — and your reply charged nothing for it: "
            "no @time beyond the turn's minimum, no @health, no @coin -N, no @trust or @rep down, no "
            "@remove, no @tension. Make the price concrete in your next reply, in one of those commands "
            "(the hour it ate, the cut it left, the coin it took, the trust it spent, the thing they "
            "dropped, the danger now awake) and show it in the prose. Never a cost in words alone. "
            # fleet6-wenna T10: a partial on reading a notice board cost
            # `@health winded` from its nail, for ten turns
            "Fit the price to the risk: a failed ask costs trust or time, a failed sneak wakes the danger; "
            "a wound only when something could really wound them. If your next reply writes no price "
            "either, the engine charges one itself."
        ),
    }


# ---------------------------------------------------------------------------
# Healing told and never applied (fleet6-bram T16)
# ---------------------------------------------------------------------------
# The Keeper "works the makeshift binding loose with practiced hands... and
# presses the jar's contents into it", then says "That'll hold till
# morning" — no @health, and Bram stayed 'hurt' for the rest of the session
# after three turns of looking for a healer. healing_without_command (in
# audit_turn) reads only "<someone> dresses your <wound>"; this one reads
# the rest the way wound_without_fight reads a blow: someone tending in a
# sentence about a wound, a healer's verdict said aloud, or relief felt.

_WOUND_WORDS = re.compile(
    r"\b(?:wounds?|cuts?|gash(?:es)?|bites?|bleed\w*|blood\w*|bandag\w*|bindings?|dressings?|stitch\w*|"
    r"salves?|poultices?|splints?|sling|ointment|tinctures?|torn\s+flesh)\b",
    re.IGNORECASE,
)
# Someone else, a tending verb, and the dressing or the wound as its own
# object ("works the makeshift binding", "presses the jar's contents into
# it") — "works the room, eyeing your bandaged arm" is no dressing
_DRESSING_PLUS = (r"(?:bindings?|bandages?|dressings?|wounds?|cuts?|gash(?:es)?|bites?|poultices?|salves?|"
                  r"stitches|splints?|ointment|compress(?:es)?)")
_TENDING = re.compile(
    rf"\b(?!(?:you|your|i)\b){_TENDER}\s+(?:[\w'’,-]+\s+){{0,6}}?"
    r"(?:press(?:es|ed)|pack(?:s|ed)|smear(?:s|ed)|daub(?:s|ed)|rub(?:s|bed)|work(?:s|ed)|ti(?:es|ed)|bind(?:s)?|"
    r"bound|wrap(?:s|ped)|stitch(?:es|ed)|clean(?:s|ed)|dress(?:es|ed)|bath(?:es|ed)|swab(?:s|bed)|salv(?:es|ed)|"
    r"sew(?:s|ed)|splint(?:s|ed))"
    rf"(?:(?:\s+[\w'’-]+){{0,3}}?\s+(?:(?:at|on|to|into|over)\s+)?(?:the|your|a|that)\s+(?:[\w'’-]+\s+){{0,2}}?"
    rf"{_DRESSING_PLUS}\b"
    r"|(?:\s+[\w'’-]+){0,4}?\s+into\s+(?:it|the\s+(?:wound|cut|gash|bite))\b)",
    re.IGNORECASE,
)
_HEALER_VERDICT = re.compile(
    r"\b(?:that['’]ll|that\s+will|it['’]ll|this['’]ll|that\s+should|it\s+should)\s+hold\b|\byou['’]ll\s+live\b"
    r"|\bkeep\s+it\s+(?:clean|dry|bound|wrapped)\b|\bgood\s+as\s+new\b|\b(?:stitched|patched|sewn)\s+(?:you\s+)?up\b",
    re.IGNORECASE,
)
_RELIEF = re.compile(
    r"\b(?:the\s+)?(?:pain|ache|throb\w*|sting\w*|bleeding|burn\w*)\s+(?:in\s+your\s+[\w-]+\s+)?(?:[\w'’-]+\s+){0,2}?"
    r"(?:eases?|eased|easing|dulls?|dulled|fades?|faded|ebbs?|ebbed|recedes?|receded|stops?|stopped|slows?|slowed|"
    r"subsides?|subsided)\b"
    r"|\byou\s+feel\s+(?:\w+\s+)?(?:better|steadier|stronger|whole|almost\s+(?:yourself|whole))\b",
    re.IGNORECASE,
)
_RELIEF_DENIED = re.compile(r"\b(?:no|not|never|won['’]t|will|would|could|should|until|unless|if)\b|n['’]t\b",
                            re.IGNORECASE)


def healed_without_command(commands: Iterable[Any], prose: str, spoken_text: str,
                           health_before: str | None, health_after: str | None = None,
                           code_actions: Iterable[str] = ()) -> dict[str, str] | None:
    """The story healed the player and nothing mechanical followed: no
    @health, @use or @rest this reply, no potion on the canvas, health
    where it was. Skips what healing_without_command already reads."""
    if health_before in (None, "healthy", "dead") or (health_after and health_after != health_before):
        return None
    if {verb for verb, _, _ in _applied(commands)} & {"health", "use", "rest"}:
        return None
    if set(code_actions or ()) & _HEAL_TOOLS:
        return None
    narration = _strip_quotes(prose or "")
    if _TENDS.search(narration) or _TENDED.search(narration):
        return None                     # healing_without_command says it already
    evidence = None
    for sentence in _SENTENCES.split(narration):
        # Their wound, not the boy's cut across the room
        if _TENDING.search(sentence) and re.search(r"\b(?:you|your|it)\b", sentence, re.IGNORECASE):
            evidence = sentence
            break
        relief = _RELIEF.search(sentence)
        if relief and not _RELIEF_DENIED.search(sentence[:relief.start()][-40:] + relief.group(0)):
            evidence = sentence
            break
    if evidence is None:
        verdict = _HEALER_VERDICT.search(spoken_text or "")
        if verdict and _WOUND_WORDS.search("\n".join((narration, spoken_text or ""))):
            evidence = verdict.group(0)
    if evidence is None:
        return None
    better = _one_step_better(health_before)
    return {
        "check": "healed_without_command",
        "detail": (
            f"Your reply healed the player in the story (\"{evidence.strip()[:90]}\") but nothing mechanical "
            f"followed — they are still {str(health_before).replace('_', ' ')}. Healing is a command: a "
            f"healer's dressing is @health {better} | cause: <who>'s dressing (and @coin -N if it cost them), "
            "a draught from their pack is @use <item>, a night's sleep is @rest. If it really happened, write "
            "it first thing next reply; if it did not, stop narrating relief."
        ),
    }


# ---------------------------------------------------------------------------
# A roll nobody asked for (fleet6-bram T19, fleet6-wenna T10)
# ---------------------------------------------------------------------------

def roll_without_attempt(player_input: str, rolls: Iterable[dict[str, Any]], *,
                         danger_present: bool = False) -> dict[str, str] | None:
    """roll_check was called, and what the player typed names no act that
    could fail (src/services/roll_intent.py): a plain question, reading
    what is pinned up in public, asking someone willing. fleet6-bram T19
    asked "Where do I find the Captain this hour?" and got a critical-success
    eavesdrop that spent the campaign's confrontation; fleet6-wenna T10
    rolled to read the public notice board and was cut by its nail.

    Not when someone of the danger's side is present (that talk is a
    confrontation), and not for a recognition roll the context ordered
    (it carries about_npc_id)."""
    from src.services import roll_intent

    rolls = [r for r in rolls or () if isinstance(r, dict) and r.get("outcome")]
    if not rolls or danger_present or not (player_input or "").strip() or _ENGINE_INPUT.match(player_input):
        return None
    if any(r.get("about_npc_id") for r in rolls) or roll_intent.attempted(player_input):
        return None
    reasons = "; ".join(dict.fromkeys(str(r.get("reason") or "").strip() for r in rolls if r.get("reason")))
    return {
        "check": "roll_without_attempt",
        "detail": (
            f"You called roll_check ({reasons[:120] or 'a check'}) but the player attempted nothing that could "
            "fail — they asked, read, looked or walked. A roll answers a risky act the PLAYER chose: "
            "sneaking, stealing, a lie, pressing someone against their will, forcing, climbing. Someone willing "
            "simply answers; what is pinned up in public is simply read; someone who will not tell refuses "
            "in their own voice. Never roll for an act they did not choose, and never invent one to roll on "
            "(\"you press him\", an overheard mutter)."
        ),
    }


def recognition_roll(roll: dict[str, Any]) -> bool:
    """A roll the context ordered to decide whether someone places the
    player (roll_check with about_npc_id on the recognition skill)."""
    from src.services.perception import RECOGNITION_SKILL

    return bool(roll.get("about_npc_id")) and str(roll.get("skill") or "") == RECOGNITION_SKILL


def consequence_warnings(player_id: str | None, commands: Iterable[Any], roll_outcomes: Iterable[str],
                         prose: str, *, present_ids: Iterable[str] = (),
                         state_before: dict[str, Any] | None = None,
                         fought: bool = False, state_after: dict[str, Any] | None = None,
                         spoken_text: str = "", code_actions: Iterable[str] = (),
                         player_input: str = "", rolls: Iterable[dict[str, Any]] | None = None,
                         ) -> list[dict[str, str]]:
    """wound_without_fight, partial_without_cost, healed_without_command and
    roll_without_attempt for one reply. `fought`: the engine ran a fight
    this turn (an ambush, a [COMBAT OUTCOME]) — its wounds are the fight's
    and the fight is the cost. `rolls`: {outcome, skill, reason,
    about_npc_id} per roll_check (outcomes alone when absent). Also
    read_text_altered (round 8), which reads the commands too. Never raises."""
    warnings: list[dict[str, str]] = []
    commands = list(commands or [])
    try:
        altered = read_text_altered(commands, "\n".join((prose or "", spoken_text or "")))
        if altered:
            warnings.append(altered)
    except Exception:
        import logging
        logging.getLogger(__name__).warning("read text audit failed", exc_info=True)
    if fought:
        return warnings
    health_before = (state_before or {}).get("health")
    try:
        if any(verb == "health" for verb, _, _ in _applied(commands)):
            people = people_for_audit(player_id, present_ids)
            hit = wound_without_fight(commands, people, prose, health_before)
            if hit:
                warnings.append(hit)
        # A recognition roll's failure IS its price — the person placed the
        # player (perception.remember_recognition) — so it owes nothing more,
        # and the engine must not charge one on top (src/services/stakes.py)
        if rolls is not None:
            roll_outcomes = [r.get("outcome") for r in rolls
                             if isinstance(r, dict) and not recognition_roll(r)]
        cost = partial_without_cost(commands, roll_outcomes, health_before, player_input=player_input)
        if cost:
            warnings.append(cost)
        healed = healed_without_command(commands, prose, spoken_text, health_before,
                                        (state_after or {}).get("health"), code_actions)
        if healed:
            warnings.append(healed)
        rolled = list(rolls) if rolls is not None else [{"outcome": o} for o in roll_outcomes or ()]
        if rolled and player_input:
            from src.services import encounters, roll_intent

            # Only asked when the input names no attempt: the check reads the room
            if not roll_intent.attempted(player_input):
                unasked = roll_without_attempt(
                    player_input, rolled,
                    danger_present=bool(player_id) and encounters.danger_among(player_id, list(present_ids or ())),
                )
                if unasked:
                    warnings.append(unasked)
    except Exception:  # an audit never costs the turn
        import logging
        logging.getLogger(__name__).warning("consequence audit failed", exc_info=True)
    return warnings


# ---------------------------------------------------------------------------
# A refused command, narrated anyway (round 6)
# ---------------------------------------------------------------------------
# fleet7-tamsin T10: `@give Cracked Bone Whistle` was refused ("the player
# finds it with @interact there; nothing was given") and the prose said "You
# pocket the whistle, cord and all"; T14 `@hand Cracked Bone Whistle` failed
# ("no item in their pack") while Coldiron "tucks the whistle away inside his
# coat". Four turns of story rested on a whistle she never held. The generic
# checks fired (item_without_tool, item_handover_without_command) and told
# the DM to "write @give" / "@hand" — the very lines the engine had refused.
# This one names the refused line, what did NOT happen, and how to open the
# next reply; the generic warnings it covers are dropped (`merge_refused`).

# Each frame is tied to the thing itself — a verb anywhere in the sentence
# is no claim: "You keep your eyes on the sweetroot bundle", "You hold your
# breath, watching the whistle", "You stand at the gate, looking north toward
# Wolf's Hollow. The bar does not lift." are the honest replies a refusal
# asks for, and were flagged as the opposite (review of round 6).
# The words between a verb and its thing ("you pocket the cracked whistle")
# never run through one of these: the verb's reach ends there
_GAP_BREAK = (r"(?:your|you|eyes?|breath|gaze|distance|out|back|off|away|on|onto|upon|at|to|toward|towards|for"
              r"|from|over|near|beside|by|past|beyond|behind|watching|while|as|but|without|around|in|into)")
_GAP = rf"(?:(?!{_GAP_BREAK}\b)[\w'’-]+[\s,]+){{0,4}}?"


def _received(sentence: str, noun: str) -> bool:
    """Does the sentence put `noun` in the player's keeping — carried
    (`_possessed`), taken by them, or handed to them?"""
    n = rf"{re.escape(noun)}s?"
    frames = [
        # "you wrap the whistle twice in cloth" (fleet8-wenna T18/T20)
        rf"\byou\s+(?:\w+ly\s+)?(?:pocket\w*|take|took|tuck\w*|slip\w*|stow\w*|grab\w*|pick\w*\s+up|lift\w*"
        rf"|scoop\w*|palm\w*|snatch\w*|wrap\w*)\s+(?:up\s+)?{_GAP}{n}\b",
        rf"\b(?:hands?|handed|gives?|gave|passes|passed|presses|pressed|pushes|pushed|tosses|tossed|slides|slid)"
        rf"\s+you\s+{_GAP}{n}\b",
        rf"\b(?:hands?|handed|gives?|gave|passes|passed|presses|pressed|pushes|pushed|tosses|tossed|slides|slid)"
        rf"\s+{_GAP}{n}\b[^.!?;\n]{{0,30}}?\b(?:to\s+you|into\s+your|in\s+your|across\s+to\s+you)\b",
        rf"\b{n}\b[^.!?;\n]{{0,30}}?\b(?:is|are)\s+(?:now\s+)?yours\b",
    ]
    return _possessed(sentence, noun) or any(re.search(f, sentence, re.IGNORECASE) for f in frames)


def _handed(sentence: str, noun: str) -> bool:
    """Does the sentence have `noun` change hands from the player — given
    over, or taken from them ("He takes the whistle from your palm")?"""
    n = rf"{re.escape(noun)}s?"
    frames = [
        rf"\b(?:takes?|took|accepts?|accepted|receives?|received|pockets?|pocketed|tucks?|tucked|slips?|slipped"
        rf"|weighs?|weighed|turns?|turned|closes?\s+(?:his|her|their)\s+(?:hand|fingers|fist)\s+(?:over|around))"
        rf"\s+{_GAP}{n}\b",
        rf"\byou\s+(?:\w+ly\s+)?(?:hand|give|pass|press|offer|slide|hold\s+out|set|lay|put)\s+(?:over\s+)?{_GAP}{n}\b",
        rf"\b{n}\b[^.!?;\n]{{0,40}}?\bfrom\s+your\s+(?:hand|palm|fingers|grip)\b",
        rf"\b{n}\b[^.!?;\n]{{0,40}}?\binto\s+(?:his|her|their)\s+(?:own\s+)?(?:hand|palm|coat|pocket|keeping)\b",
    ]
    return any(re.search(f, sentence, re.IGNORECASE) for f in frames)
_PAYOUT_FRAME = re.compile(
    r"\b(?:your|the|his|her)\s+(?:promised\s+)?(?:pay|payment|reward|fee|wages|due)\b|\bpaid\s+(?:you|in\s+full)\b"
    r"|\bearned\s+(?:your|the|it)\b",
    re.IGNORECASE,
)
_PLACE_GAP = (r"(?:(?!(?:you|from|toward|towards|for|to|past|beyond|behind|away|back)\b)[\w'’-]+\s+){0,4}?")


def _arrived_at(sentence: str, noun: str) -> bool:
    """Does the sentence put the player AT the place `noun` names — reached,
    entered, standing in it, the place opening below them? (fleet7-bram T11:
    "the ground opens into a hollow")"""
    n = rf"{re.escape(noun)}s?"
    frames = [
        rf"\b(?:arriv\w*|reach\w*|enter\w*|step\w*\s+(?:in|into|onto|out\s+into)|walk\w*\s+into|(?:come|came)"
        rf"\s+(?:in)?to|crest\w*\s+(?:into|above|over))\s+(?:at\s+|in\s+|into\s+)?{_PLACE_GAP}{n}\b",
        rf"\b{n}\b[^.!?;\n]{{0,40}}?\b(?:opens?|spreads?|stretch\w*|lies|yawns?|falls?\s+away)\s+(?:out\s+)?"
        rf"(?:below|before|beneath|around|ahead\s+of)\s+you\b",
        rf"\b(?:ground|road|path|trail|track|trees?|woods?|land|slope)\s+(?:\w+\s+)?(?:opens?|gives?\s+way|drops?"
        rf"|falls?\s+away|dips?)\s+(?:out\s+)?(?:in)?to\s+(?:a|an|the)\s+(?:[\w'’-]+\s+){{0,2}}?{n}\b",
        rf"\byou(?:'re|’re|\s+are|\s+stand\w*|\s+wait\w*|\s+crouch\w*|\s+kneel\w*)\s+(?:now\s+)?(?:in|at|inside"
        rf"|within|on)\s+(?:the\s+)?(?:(?:edge|rim|lip|mouth|heart|floor|bottom|middle|centre|center)\s+of\s+"
        rf"(?:the\s+)?)?(?:[\w'’-]+\s+)?{n}\b",
    ]
    return any(re.search(f, sentence, re.IGNORECASE) for f in frames)
# Said of the thing itself, near it: "without the whistle", "no whistle",
# "the whistle you left behind" — never "takes the whistle from your palm
# without quite touching your fingers" (fleet7-tamsin T13)
_NEGATED_BEFORE = frozenset({"no", "not", "never", "without", "nothing", "dont", "didnt", "doesnt", "havent",
                             "hasnt", "isnt", "wasnt", "cant", "couldnt", "wont", "neither", "nor"})
_NEGATED_AFTER = frozenset({"left", "lost", "missing", "gone", "behind", "forgotten", "not", "never", "isnt",
                            "wasnt", "aint"})


def _negated(words: list[str], at: int) -> bool:
    return bool(_NEGATED_BEFORE & set(words[max(0, at - 4):at])) or \
        bool(_NEGATED_AFTER & set(words[at + 1:at + 4]))


def _heads_only(text: str) -> str:
    """A hyphenated compound read by its head: "crate-silver" is silver,
    "sweetroot-stained" is stained (fleet7-wenna T3 was no sweetroot)."""
    return re.sub(r"\b[\w’']+-(?=[\w’'])", "", text or "")
_ITEM_GENERIC = frozenset({"piece", "pieces", "bundle", "bit", "scrap", "pouch", "bag", "lump", "handful", "set",
                           "pair", "coin", "coins", "thing", "item", "some"})
_SUPERSEDED = {
    "give": ("item_without_tool", "item_handover_without_command"),
    "interact": ("item_without_tool", "item_handover_without_command"),
    "hand": ("item_handover_without_command", "item_handed_without_command"),
    "quest-done": ("money_without_command", "item_without_tool", "item_handover_without_command"),
    "move": ("arrival_without_move",),
}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def item_nouns(name: str) -> set[str]:
    """The words prose calls an item by: its head noun, and the word before a
    generic head ("Greywater Silver Piece" -> silver; "Sweetroot Bundle" ->
    sweetroot, bundle). Hyphens split: "crate-silver" is crate and silver."""
    words = [w for w in re.findall(r"[a-z]+", re.split(r"\s+of\s+", str(name or "").lower())[0]) if len(w) >= 3]
    if not words:
        return set()
    out = {words[-1]} if words[-1] not in _ITEM_GENERIC else set()
    if words[-1] in _ITEM_GENERIC and len(words) >= 2:
        out.add(words[-2])
    return {w for w in out if len(w) >= 4}


def _noun_hits(words: list[str], nouns: set[str]) -> list[int]:
    return [i for i, w in enumerate(words) if w in nouns or (w.endswith("s") and w[:-1] in nouns)]


# A sentence that says the thing is NOT had is the honest answer a refusal
# asks for, whatever words it shares with a claim: fleet8-wenna T16 "You
# reach into your satchel for the whistle — and come up with nothing but a
# fold of empty cloth" was flagged carried_not_held, and the DM told "never
# treat it as theirs" for the one sentence that didn't
_DENIAL = re.compile(
    r"\b(?:come|comes|came|coming)\s+(?:up|away|back)\s+(?:with\s+)?(?:nothing|empty)\b"
    r"|\bnothing\s+but\b|\b(?:find|finds|finding|found|feel|feels|felt|close\s+on|closes\s+on|closed\s+on)"
    r"\s+(?:only|nothing)\b|\bclos(?:e|es|ed|ing)\s+(?:around|on|over)\s+nothing\b"
    r"|\b(?:isn['’]t|wasn['’]t|aren['’]t|is\s+not|was\s+not)\s+(?:there|here|in\s+your|on\s+you|among)\b"
    r"|\bnot\s+there\b|\bempty-handed\b|\bempty\s+(?:cloth|pocket|pockets|satchel|pack|hand|hands|palm|fingers"
    r"|cord|fold)\b|\bonly\s+meant\s+to\b"
    r"|\bnever\s+(?:actually\s+|really\s+|once\s+)?(?:lifted|picked|took|taken|had|held|pocketed|touched)\b"
    r"|\bno\s+longer\s+(?:there|in\s+your|have|has|hold|holds|carry|carries)\b",
    re.IGNORECASE,
)


def _claim(prose: str, nouns: set[str], tied: Callable[[str, str], bool]) -> str:
    """The first sentence whose frame (`tied(sentence, noun)`: `_received`,
    `_handed`, `_arrived_at`) holds one of `nouns`, the thing itself never
    negated ("no whistle", "the whistle you left"), the sentence no denial."""
    if not nouns:
        return ""
    for shown, sentence in zip(_sentences(prose), _sentences(_heads_only(prose))):
        words = [w.replace("'", "").replace("’", "") for w in re.findall(r"[a-z'’]+", sentence.lower())]
        hits = _noun_hits(words, nouns)
        if hits and not all(_negated(words, i) for i in hits) and not _DENIAL.search(sentence) \
                and any(tied(sentence, n) for n in nouns):
            return shown
    return ""


# A claim made by pronoun once the thing is the subject: fleet8-wenna T14
# "the thing in your palm is lighter than you expect", T15 "Sorrel watches
# you tuck it away", T20 "you wrap it twice in cloth" — the refused whistle
# every time, never named in the sentence. After a refusal the reply's first
# PRONOUN_REACH sentences, and as many after any sentence naming the thing,
# read "it" and "the (pale, small…) thing" as the thing. A denial closes the
# window: what follows "you come up with nothing" is about its absence.
PRONOUN_REACH = 2
# "it" after a preposition is rarely the thing held ("the fang into your
# other hand after it", fleet7-tamsin T14: the coin), nor a look ("take it in"),
# nor the empty "it" of the hour and the moment ("It is late, and the cold has
# got into your hands", "It takes a moment for the warmth to come back into
# your fingers" read as the refused thing in the palm)
_EMPTY_IT = (r"(?!\s+(?:takes?|took)\s+(?:a|an|some|long|time|you|them)\b)"
             r"(?!\s+(?:hits|hit|strikes|struck|occurs|occurred|dawns|dawned)\s+(?:you|on|to)\b)"
             r"(?!(?:['’]s|\s+is|\s+was)\s+(?:\w+\s+)?(?:late|early|nearly|almost|past|after|night|morning|evening"
             r"|midnight|noon|dawn|dusk|dark|raining|snowing|time|no\s+use|hard\s+to|easy\s+to)\b)")
_PRONOUN = re.compile(r"(?<!\bafter\s)(?<!\bbefore\s)(?<!\bof\s)(?<!\bwith\s)(?<!\babout\s)(?<!\bfor\s)(?<!\bat\s)"
                      r"(?<!\bto\s)(?<!\bon\s)(?<!\bthan\s)(?<!\blike\s)(?<!\bover\s)(?<!\bunder\s)"
                      r"\bit\b(?!\s+(?:all|easy|slow)\b)(?!\s+in\b(?!\s+(?:your|his|her|their|both|one)\b))"
                      + _EMPTY_IT + r"|\bthe\s+(?:[a-z'’-]+\s+){0,2}?thing\b", re.IGNORECASE)
# ...and the player's own pickup of "it" ("and pocket it", never Piper's
# "and pockets it"; "you take it in" is a look)
_TAKES_IT = re.compile(
    r"(?:(?<!\bif\s)(?<!\bonce\s)(?<!\bwhen\s)\byou\s+(?:\w+ly\s+)?(?:pocket\w*|tuck\w*|stow\w*|slip\w*|wrap\w*"
    r"|take|took|grab\w*|pick\w*|scoop\w*|palm\w*|snatch\w*|clutch\w*|carry|carried)"
    r"|(?:\band|,)\s+(?:then\s+)?(?:pocket|tuck|stow|slip|wrap|take|grab|pick|scoop|palm|snatch|clutch))"
    r"\s+(?:up\s+)?it\b(?!\s+(?:all|in)\b)",
    re.IGNORECASE,
)


def _pronoun_claim(prose: str, nouns: set[str], tied: Callable[[str, str], bool], *,
                   pickup: bool = False) -> str:
    """The first sentence in the window whose "it" / "the thing", read as
    the thing, makes `tied` hold (with `pickup`, or the player picks "it"
    up); "" when none."""
    if not nouns:
        return ""
    noun = sorted(nouns)[0]
    window = PRONOUN_REACH
    for shown, sentence in zip(_sentences(prose), _sentences(_heads_only(prose))):
        if _DENIAL.search(sentence):
            window = 0
            continue
        if re.fullmatch(r"[^.!?:]{1,60}:", sentence):
            continue                # a speaker's label, its words quoted away: no sentence of prose
        words = [w.replace("'", "").replace("’", "") for w in re.findall(r"[a-z'’]+", sentence.lower())]
        if _moves_on(sentence, words, nouns):
            window = 0              # "it" is the lesser thing from here on
            continue
        named = bool(_noun_hits(words, nouns))
        if window > 0 or named:
            # "it" after "a loose bundle of sweetroot…" is the bundle (fleet7-
            # tamsin T11), after "the coin" the coin — unless the sentence
            # named the thing itself
            def ours(at: int, it: bool = True) -> bool:
                if _IF_YOU.search(sentence[:at]):
                    return False    # "there for the taking, if you take it" offers it
                return not it or named or not _ANTECEDENT.search(sentence[:at])

            swapped = _PRONOUN.sub(lambda m: f"the {noun}" if ours(m.start(), m.group(0).lower() == "it")
                                   else m.group(0), sentence)
            took = _TAKES_IT.search(sentence) if pickup else None
            if (swapped != sentence and tied(swapped, noun)) or (took and ours(took.start())):
                return shown
        window = PRONOUN_REACH if named else window - 1
    return ""


_IF_YOU = re.compile(r"\b(?:if|unless|once|when|whenever|until|should)\s+you\b", re.IGNORECASE)
_ANTECEDENT = re.compile(
    r"\b(?:a|an|some|the|your|his|her|their|its|this|that|these|those)\s+"
    r"(?!(?:few|little|moment|breath|beat|while|step|heartbeat|second|way|look|time)\b)[a-z]"
    r"|\b[A-Z][a-z]+['’]s\s+[a-z]",
    re.IGNORECASE,
)
# The repair [SETTLE THIS FIRST] asks for, said: what the thing really was
# ("is only a plain bone toggle", "no whistle at all, just a bone toggle"), or
# the player taking some other named thing ("You pocket the toggle"). After
# it "it" is that lesser thing: "…only a plain bone toggle. You turn it over in
# your palm" read as the whistle in the palm, the line re-armed, and the DM was
# scolded for the very sentence it was told to write
_IS_REALLY = re.compile(
    r"(?:\b(?:is|was|are|were)|['’]s)\s+(?:(?:only|just|merely|really|actually|nothing\s+more\s+than"
    r"|no\s+more\s+than)\s+)?(?:a|an)\s+((?:[\w'’-]+[\s,]+){0,4}[\w'’-]+)", re.IGNORECASE)
_YOU_TAKE_OTHER = re.compile(
    r"\byou\s+(?:\w+ly\s+)?(?:pocket\w*|tuck\w*|stow\w*|slip\w*|take|took|grab\w*|pick\w*\s+up|scoop\w*|palm\w*"
    r"|snatch\w*|wrap\w*)\s+(?:up\s+)?(?:the|a|an|your|that|this)\s+((?:[\w'’-]+\s+){0,2}[\w'’-]+)",
    re.IGNORECASE)
_NO_THING = frozenset({"few", "little", "moment", "breath", "beat", "while", "step", "heartbeat", "second", "way",
                       "look", "time", "deep", "long", "slow", "steadying"})
# ...said of the thing ("it's", "what you pulled free is", "the pale thing on
# the cord is"), never of a person ("Sorrel is a statue at the rail")
_THING_SUBJECT = re.compile(r"\b(?:it|that|this|what|thing|one)\b[^,;:—–]{0,60}$", re.IGNORECASE)


def _moves_on(sentence: str, words: list[str], nouns: set[str]) -> bool:
    """Does this sentence say what the thing really was, or have the player
    take another named thing, so that "it" after it is no longer the thing?
    A sentence naming the thing only to deny it ("no whistle at all") too."""
    hits = _noun_hits(words, nouns)
    if hits and all(_negated(words, i) for i in hits):
        return True
    for rx in (_IS_REALLY, _YOU_TAKE_OTHER):
        for m in rx.finditer(sentence):
            if rx is _IS_REALLY and not _THING_SUBJECT.search(sentence[:m.start()]):
                continue
            said = [w.replace("'", "").replace("’", "") for w in re.findall(r"[a-z'’]+", m.group(1).lower())]
            if said and said[0] not in _NO_THING and not _noun_hits(said, set(nouns) | {"thing"}):
                return True
    return False


# Where a refused authored thing really lies, read off the refusal
# (commands._authored_substitute / _authored_elsewhere_refusal)
_LIES_AT = re.compile(r"(?:^that is .+? at |\bstill lies at |\blies at )(?P<place>[^—;,]+?)(?:,| —|;|$)")


def _refused_rows(commands: Iterable[Any]) -> list[tuple[str, Any, str, str]]:
    """(verb, parsed line, the line, the engine's reason) of each command
    that did NOT land. Takes CommandRunner results or transcript rows."""
    from src.services import commands as cmds

    aliases = getattr(cmds, "_ALIASES", {})
    out = []
    for row in commands or []:
        get = row.get if isinstance(row, dict) else (lambda key, _r=row: getattr(_r, key, None))
        if get("ok"):
            continue
        line = str(get("line") or "")
        parsed = cmds.parse_command(line)
        if parsed is None:
            continue
        out.append((aliases.get(parsed.verb, parsed.verb), parsed, line, str(get("message") or "")))
    return out


def refused_but_narrated(commands: Iterable[Any], prose: str, *, here: str | None = None,
                         pack: Iterable[str] = ()) -> list[dict[str, str]]:
    """refused_but_narrated: a @give, @interact, @hand, @quest-done or @move
    the engine refused this reply, whose effect the prose told anyway — the
    thing pocketed, handed over, the pay counted out, the place reached.
    `here`: where the player really is; `pack`: item names they carry after
    the reply (a @give refused because @quest-done already paid that very
    thing is no contradiction: fleet6-bram T19). Never raises."""
    warnings: list[dict[str, str]] = []
    try:
        text = _strip_quotes(prose or "")
        carried: set[str] = set()
        for name in pack or ():
            carried |= item_nouns(str(name or ""))
        for verb, parsed, line, reason in _refused_rows(commands):
            item = re.sub(r"\s+x\d+$", "", (parsed.main or "").strip(), flags=re.IGNORECASE)
            lies = _LIES_AT.search(reason)
            if verb in ("give", "interact"):
                if item_nouns(item) & carried or re.search(r"\balready\b", reason, re.IGNORECASE):
                    continue
                said = _claim(text, item_nouns(item), _received) \
                    or _pronoun_claim(text, item_nouns(item), _received, pickup=True)
                lost = (f"Nothing was given: no {item} is in their pack" if verb == "give" else
                        f"Nothing was taken: the {item} is still where it was, not in their pack")
                fix = ("open your next reply with them empty-handed of it — it is theirs only when the engine "
                       "gives it (taken in play where it lies, @interact)")
                if lies:
                    # Never "it must still be lying in the mud" here: each
                    # such retcon planted it somewhere new (fleet8-wenna T16-T19)
                    fix = (f"it lies at {lies.group('place').strip()} and nowhere else — never move it into this "
                           "scene. Open your next reply with ONE sentence of what they really hold instead (an "
                           "empty cord, a plain trinket, nothing), then play on; never retell the find")
            elif verb == "hand":
                said = _claim(text, item_nouns(item), _handed) or _pronoun_claim(text, item_nouns(item), _handed)
                never = "no item" in reason.lower()
                lost = (f"Nothing changed hands: the player never had the {item}, so nobody took it from them"
                        if never else f"Nothing changed hands: the {item} is still in the player's pack")
                fix = ("open your next reply where the story really stands — whoever took it has nothing"
                       + ("; if they never found it, the story notices that" if never else
                          "; if it really changes hands, write @hand first"))
            elif verb == "quest-done":
                said = _money_claim(text) or _first_handover(text, _ITEM_GIVEN_TO_YOU) \
                    or next((s for s in _sentences(text) if _PAYOUT_FRAME.search(s)
                             and not re.search(r"\b(?:no|not|never|n['’]t)\b", s, re.IGNORECASE)), "")
                lost = "Nothing was paid — no coin, no reward, no thanks — and the job is still open"
                fix = ("the payout you wrote did not happen: in your next reply it is withheld or put off in the "
                       "fiction until the work is really done (the refusal says what is missing)")
            elif verb == "move":
                place = re.sub(r"\s*\|.*$", "", item)
                nouns = {w for w in re.findall(r"[a-z]+", place.lower()) if len(w) >= 4} - {"north", "south", "east",
                                                                                            "west", "old"}
                said = _claim(text, nouns, _arrived_at) or (_arrival_claim(text, here) or "")
                lost = f"They did not go: they are still at {here or 'where they were'}"
                fix = ("whatever they saw or did there did not happen — open where they really are; only if the "
                       f"fiction truly gets them past what stopped them, write @move {place} | through first")
            else:
                continue
            if not said:
                continue
            warnings.append({
                "check": "refused_but_narrated",
                "verb": verb,
                "detail": (
                    f"Your line `{_clip_line(line)}` was REFUSED ({_clip_line(reason, 140)}), but your prose went on "
                    f"as if it worked (\"{_around(said, item_nouns(item) or [said[:1]], 100)}\"). {lost}. The database is reality: {fix}."
                ),
                "_nouns": " ".join(sorted(item_nouns(item))),
            })
    except Exception:  # an audit never costs the turn
        import logging
        logging.getLogger(__name__).warning("refused-command audit failed", exc_info=True)
    return warnings


def _clip_line(text: str, limit: int = 90) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _around(sentence: str, nouns: Iterable[str], limit: int = 100) -> str:
    """The part of a long sentence around the first of `nouns` it names."""
    text = " ".join(str(sentence or "").split())
    if len(text) <= limit:
        return text
    at = min((m.start() for n in nouns for m in [re.search(rf"\b{re.escape(n)}", text, re.IGNORECASE)] if m),
             default=0)
    start = max(0, min(at - limit // 2, len(text) - limit))
    start = text.find(" ", start) + 1 if start else 0
    return ("…" if start else "") + _clip_line(text[start:], limit)


def merge_refused(warnings: list[dict[str, str]], refused: list[dict[str, str]]) -> list[dict[str, str]]:
    """`warnings` plus the refused_but_narrated ones, minus the generic
    warnings they cover: the DM is told once, and never told to write the
    line the engine just refused. A generic handover warning about some
    other thing stays."""
    if not refused:
        return warnings
    covered: set[str] = set()
    nouns: set[str] = set()
    for w in refused:
        covered |= set(_SUPERSEDED.get(w.get("verb", ""), ()))
        nouns |= set((w.get("_nouns") or "").split())
    if any(w.get("verb") == "move" for w in refused) and any(w.get("check") == "arrived_past_the_way"
                                                             for w in warnings):
        refused = [w for w in refused if w.get("verb") != "move"]    # that one already says it, with the way
    kept = []
    for w in warnings:
        check = w.get("check")
        if check == "carried_not_held" and nouns and any(n in (w.get("detail") or "").lower() for n in nouns):
            continue                     # the refused line already says they do not have it
        if check in covered:
            detail = (w.get("detail") or "").lower()
            if check in ("item_handover_without_command", "item_handed_without_command") and nouns \
                    and not any(n in detail for n in nouns):
                kept.append(w)
            continue
        kept.append(w)
    return kept + [{k: v for k, v in w.items() if not k.startswith("_") and k != "verb"} for w in refused]


# ---------------------------------------------------------------------------
# Carried in the prose, not in the pack (round 6)
# ---------------------------------------------------------------------------
# fleet7-tamsin T13: "He takes the whistle from your palm" — the whistle still
# lay at Wolf's Hollow. fleet7-wenna T16: "the whistle's broken weight knocking
# against notebooks and crate-silver" — the Split supply crate (the act-2
# clue's Greywater Silver Piece) was never searched. Only an AUTHORED thing
# still lying in its place is read: the prose may give the player whatever
# trinkets it likes, but the world's clues are found in play.

_CARRIED_IN = (r"(?:pocket|pockets|pack|satchel|coat|bag|purse|pouch|hand|hands|palm|fist|fingers|belt|sleeve"
               r"|grip)")
_MONEY_NOUNS = frozenset({"silver", "gold", "copper", "coin", "coins", "crown", "crowns"})


def _possessed(sentence: str, noun: str) -> bool:
    """Does the sentence have the player carrying `noun`? Each frame is tied
    to the noun itself: "the whistle … in your palm", "takes the whistle from
    your palm", "you pocket the whistle", "your whistle", "knocking against
    notebooks and crate-silver" — never "the clink of a poker in the hearth"
    (fleet2-wenna T18) or "you tuck that detail away … Piper's sweetroot"."""
    n = rf"{re.escape(noun)}s?"
    # The verb's reach ends at a break word (`_GAP`): "You keep your eyes on
    # the sweetroot bundle", "You hold your breath, watching the whistle"
    # carry nothing
    frames = [
        rf"\b{n}\b[^.!?;\n]{{0,50}}?\b(?:in|into|inside|within)\s+your\s+(?:[\w'’-]+\s+)?{_CARRIED_IN}\b",
        rf"\b(?:in|into|inside)\s+your\s+(?:[\w'’-]+\s+)?{_CARRIED_IN}\b[^.!?;\n]{{0,25}}?\b{n}\b",
        rf"\b{n}\b[^.!?;\n]{{0,40}}?\bfrom\s+your\s+(?:hand|palm|fingers|grip|pocket|satchel|coat|pack|bag)\b",
        rf"\byou\s+(?:\w+ly\s+)?(?:pocket|pocketed|carry|carried|clutch|clutched|hold|held|tuck|tucked|stow"
        rf"|stowed|keep|kept|still\s+have|grip|gripped|cradle|cradled)\s+{_GAP}{n}\b",
        rf"\b(?:knock\w*|clink\w*|rattl\w*|bump\w*|jostl\w*)\s+(?:against|among|beside)\s+{_GAP}{n}\b",
        # The thing named first, then taken by pronoun: fleet7-tamsin T11 "a
        # loose bundle of sweetroot… and you pocket it". After "and" or a
        # comma only the player's own verb form ("and pocket it", never
        # Piper's "and pockets it")
        # (fleet8-wenna T20 "the whistle comes free… and you wrap it twice in cloth")
        rf"\b{n}\b[^.!?;\n]{{0,80}}?(?:(?<!\bif\s)(?<!\bonce\s)(?<!\bwhen\s)\byou\s+(?:\w+ly\s+)?(?:pocket\w*"
        rf"|tuck\w*|stow\w*|slip\w*|take|took|grab\w*|wrap\w*"
        rf"|pick\w*|scoop\w*|palm\w*|snatch\w*|clutch\w*|carry|carried)|(?:\band|,)\s+(?:then\s+)?(?:pocket|tuck"
        rf"|stow|slip|take|grab|pick|scoop|palm|snatch|clutch|wrap))\s+(?:up\s+)?(?:it|them)\b",
    ]
    if noun not in _MONEY_NOUNS:       # "your silver" is their money
        # ...and "your whistle question", "your whistle story" are talk
        frames.append(rf"\byour\s+(?:own\s+)?(?:[\w'’-]+\s+)?{n}\b(?=\s*(?:[,.;:!?—–)\"”’']|$)|\s+(?:is|was|are"
                      rf"|were|in|on|at|from|into|inside|against|and|still|now|safe|tucked|wrapped|hangs?|hung"
                      rf"|knock\w*|clink\w*|rattl\w*|sits?|sat|rests?|lies|lay|weighs?|press\w*|digs?|bump\w*"
                      rf"|swing\w*|dangl\w*|feels?|felt|warm\w*|cold|heavy|has|had|with|under|beneath)\b)")
    return any(re.search(f, sentence, re.IGNORECASE) for f in frames)


def carried_not_held(prose: str, pack: Iterable[str], unheld: Iterable[dict[str, Any]],
                     refused: Iterable[str] = ()) -> dict[str, str] | None:
    """carried_not_held: the prose has the player carrying an authored item
    that still lies where it was put. `pack`: item names in their pack after
    the reply; `unheld`: {"item", "thing", "place"} for each authored Thing
    still holding an item (`unheld_authored_items`); `refused`: item names a
    @give/@interact/@hand refused this reply or the last — for those, "it"
    and "the thing" are read as the item (`_pronoun_claim`)."""
    text = _strip_quotes(prose or "")
    pack_words: set[str] = set()
    for name in pack or []:
        pack_words |= set(re.findall(r"[a-z]+", str(name or "").lower()))
    refused_nouns: set[str] = set()
    for name in refused or ():
        refused_nouns |= item_nouns(str(name or ""))
    for row in unheld or []:
        nouns = {n for n in item_nouns(row.get("item") or "") if n not in pack_words}
        if not nouns:
            continue
        # Money words name a thing only beside another of its words: "crate-
        # silver" (fleet7-wenna T16) is the crate's piece, "his silver chain"
        # (fleet3-tamsin T3) and "a small weight of silver" (a payout) are not
        also = {w for w in re.findall(r"[a-z]+", f"{row.get('item')} {row.get('thing')}".lower())
                if len(w) >= 4 and w not in _ITEM_GENERIC and w not in nouns}
        claimed = ""
        for shown, sentence in zip(_sentences(text), _sentences(_heads_only(text))):
            words = [w.replace("'", "").replace("’", "") for w in re.findall(r"[a-z'’]+", sentence.lower())]
            hits = _noun_hits(words, nouns)
            if not hits or not any(_possessed(sentence, n) for n in nouns) or all(_negated(words, i) for i in hits):
                continue
            if _DENIAL.search(sentence):
                continue            # "…for the whistle — and come up with nothing" (fleet8-wenna T16)
            if nouns <= _MONEY_NOUNS and not also & set(re.findall(r"[a-z]+", shown.lower())):
                continue
            # "ten silver into your palm" is money, not the crate's piece
            if any(i > 0 and (words[i - 1].isdigit() or words[i - 1] in _AMOUNT_WORDS.values()) for i in hits) \
                    or _money_claim(sentence):
                continue
            claimed = shown
            break
        if not claimed and nouns & refused_nouns and not nouns <= _MONEY_NOUNS:
            claimed = _pronoun_claim(text, nouns, _possessed, pickup=True)
        if claimed:
            return {
                "check": "carried_not_held",
                "detail": (
                    f"Your prose has the player carrying the {row['item']} (\"{_around(claimed, nouns, 100)}\"), "
                    f"but they do not have it: it still lies at {row['place']} ({row['thing']}). Nothing of it is "
                    "in their pack. Never treat it as theirs; it becomes theirs only when they take it there in play "
                    f"(@interact {row['thing']})."
                ),
            }
    return None


def unheld_authored_items(player_id: str | None) -> list[dict[str, Any]]:
    """{"item", "thing", "place"} for every authored Thing in the world still
    holding its item (read for carried_not_held). [] when unreadable."""
    try:
        from src.models import Location, get_session
        from src.services.lead import holding_things

        with get_session() as session:
            return [{"item": thing["payload"]["item"]["name"], "thing": thing["label"], "place": location.name}
                    for location in session.query(Location).all() for thing in holding_things(location)]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Round 8: secrets, the dead, borrowed names, authored words
# ---------------------------------------------------------------------------
# Each check was replayed over every data/playtests/*/transcript.jsonl (the
# counts are in tests/test_secrets_round8.py's docstring).

def _flat_words(text: str) -> list[str]:
    """Lower-cased words and numbers, every mark and quote gone: "— Capt. B.
    Coldiron." and "*capt b coldiron*" are the same words."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower().replace("’", "'").replace("'", "")).split()


READ_QUOTE_MIN = 6        # words of the read the reply must carry to be quoting it
READ_RUN_MIN = 3          # a run shorter than this is a coincidence, not the quote
READ_GAP_MIN = 2          # words of the read left out or changed that count
# The read's own stage directions are the reply's to word ("Beneath it,
# smaller: 'Sweetroot for nerves.'" told "in smaller, hastier script",
# fleet7-bram T18): only the words written on the thing are quoted
_READ_FRAMING = re.compile(r"(?:^|\n|(?<=[.!?…])\s)\s*[A-Z][^\n\"“'‘:.!?]{0,40}:\s*(?=[\"“'‘])")


def read_text_altered(commands: Iterable[Any], shown: str) -> dict[str, str] | None:
    """fleet9-bram T2: `@interact Notice board` answered "… — Capt. B.
    Coldiron." with "quote this; never add to it"; the reply quoted the
    notice up to its signature and wrote "Signed with a captain's initial"
    — at T13 the same notice read in full, so the board said two things. A
    reply that quotes a read (READ_QUOTE_MIN of its words, in runs) and
    leaves out or changes a run of it (READ_GAP_MIN words) altered it.
    `shown`: the reply's prose and speech. A reply that tells what a read
    says without quoting it is not judged here."""
    import difflib

    from src.services.dialogue import read_texts

    said = _flat_words(shown)
    if not said:
        return None
    for row in commands or ():
        get = row.get if isinstance(row, dict) else (lambda key, _r=row: getattr(_r, key, None))
        if not get("ok") or get("verb") != "interact":
            continue
        for text in read_texts(get("result")):
            words = _flat_words(_READ_FRAMING.sub("\n", text))
            matcher = difflib.SequenceMatcher(None, words, said, autojunk=False)
            covered = [False] * len(words)
            for a, _b, size in matcher.get_matching_blocks():
                if size >= READ_RUN_MIN:
                    covered[a:a + size] = [True] * size
            if sum(covered) < READ_QUOTE_MIN or all(covered):
                continue
            gaps, run = [], []
            for word, seen in zip(words + [""], covered + [True]):
                if not seen:
                    run.append(word)
                    continue
                if len(run) >= READ_GAP_MIN:
                    gaps.append(" ".join(run))
                run = []
            if gaps:
                return {
                    "check": "read_text_altered",
                    "detail": (
                        f"Your reply quoted what the player read ({_clip_line(text, 80)!r}) but changed or left out "
                        f"part of it (\"{gaps[0]}\"). Authored text is quoted whole, word for word, every time it "
                        "is read — the same thing never says two different things. If it comes up again, quote it "
                        "as written; if you only mention it, say what it is about without quoting."
                    ),
                }
    return None


# fleet9-tamsin T2 (a repeat of fleet8-tamsin T0-T2): the Captain told the
# arc's dead carter as a survivor — "A carter. Three weeks back. Driver
# survived — cart didn't… he hasn't said three words straight since." A
# speaker line about a lost person — their name, or their trade with their
# own time ("three weeks") — that has them living, surviving, coming back
# or unable to speak since. Never a negated one ("never came back").
_SURVIVED = re.compile(
    r"\b(?:surviv(?:ed|es|or|ors|ing)|(?:still\s+|was\s+found\s+|found\s+)?alive|made\s+it\s+(?:back|out|home)"
    # "came back dead in his own cart" (fleet6-tamsin T1) is no survivor
    r"|c[ao]me\s+back(?!\s+(?:dead|cold|wrapped|in\s+(?:a|his|her|the)\s+(?:box|cart|coffin|shroud|sack)))"
    r"|walked\s+(?:away|out|back|off)|walks\s+(?:away|off)|still\s+breathing|on\s+the\s+mend"
    r"|recover(?:ed|ing|s)"
    r"|(?:hasn['’]t|has\s+not|hadn['’]t|won['’]t|can['’]t|couldn['’]t|doesn['’]t)\s+(?:said|say|spoken|speak"
    r"|talk(?:ed)?)\b[^.!?]{0,30}?\bsince"
    r"|not\s+(?:been\s+)?(?:right|the\s+same)\s+since)\b",
    re.IGNORECASE,
)
# A negation, or the player: "you're still breathing to ask me" (fleet-tamsin
# T14), "come back breathing to say it" said TO them (fleet2-tamsin T0)
_NOT_SURVIVED = re.compile(r"\b(?:never|not|no|didn['’]t|did\s+not|nobody|none|wasn['’]t|isn['’]t"
                           r"|you|you['’]re|you['’]ve|you['’]ll)\b[\w\s'’-]{0,14}$",
                           re.IGNORECASE)
_SPAN_OF_TIME = re.compile(r"\b(?:a|one|two|three|four|five|six|seven|eight|nine|ten|\d+|a\s+few|several)\s+"
                           r"(?:days?|nights?|weeks?|months?|years?)\b", re.IGNORECASE)
_TRADE_STOP = frozenset({"with", "from", "that", "this", "their", "former", "retired", "local", "town", "part"})
# The words a trade is told by in a sentence about the person ("A carter.
# Three weeks back. Driver survived", fleet9-tamsin T2)
_TRADE_ALIASES = {"carter": ("driver", "wagoner", "teamster", "drover"),
                  "driver": ("carter", "wagoner", "teamster", "drover")}
_SAID_SENTENCE = re.compile(r"(?<=[.!?…])[\"”’)*_]*\s+")


def lost_people_for_audit() -> list[dict[str, str]]:
    """{"name", "profession", "status", "about"} for the world's dead and
    missing people (beasts are no one's loss). [] when unreadable."""
    try:
        from src.models import NPC, get_session
        from src.services.dialogue import is_beast

        with get_session() as session:
            return [{"name": n.name, "profession": str(n.profession or ""), "status": n.status,
                     "about": str(n.description_physical or "")}
                    for n in session.query(NPC).filter(NPC.status.in_(("dead", "missing"))).all()
                    if n.name and not is_beast(n)]
    except Exception:
        return []


# How the dead are spoken of: their life before ("back when Tobin was still
# alive", "last time I saw him alive", "Tobin would come back through here
# with salt"), a habit ("…came back every tenday") or what might have been
# ("would have survived") — never a survivor. The "Knows:" dossier line
# puts the dead in every mouth, so these are the common case
_LIFE_BEFORE = re.compile(r"\b(?:when|while|whilst|before|used\s+to|would|last\s+(?:time\s+)?(?:[\w'’]+\s+){0,2}"
                          r"(?:saw|seen|spoke)|in\s+(?:his|her|their)\s+day)\b", re.IGNORECASE)
_HABIT_AFTER = re.compile(r"^[^.!?]{0,30}?\b(?:every|each|regular(?:ly)?|twice\s+a|once\s+a)\b", re.IGNORECASE)
# "the other drivers came back" is about someone else
_SOMEONE_ELSE = re.compile(r"\b(?:other|another|second|those|these|rest\s+of\s+the|some|most|all|both)\s+$",
                           re.IGNORECASE)


def _alive_in(sentence: str) -> bool:
    return any(not _NOT_SURVIVED.search(sentence[max(0, m.start() - 30):m.start()])
               and not _LIFE_BEFORE.search(sentence[max(0, m.start() - 45):m.start()])
               and not _HABIT_AFTER.search(sentence[m.end():])
               for m in _SURVIVED.finditer(sentence))


def lost_person_alive(spoken: Iterable[dict[str, Any]], lost: Iterable[dict[str, str]]) -> dict[str, str] | None:
    """The first speaker line that has a lost person alive (see above): a
    sentence that says them (their name, or their trade's word) alive, in
    a line that also says their name or their own time ("three weeks")."""
    from src.services.dialogue import name_words

    lost = [p for p in lost or () if isinstance(p, dict) and p.get("name")]
    if not lost:
        return None
    for line in spoken or ():
        text = str((line or {}).get("text") or "")
        sentences = _SAID_SENTENCE.split(text)
        alive = [i for i, s in enumerate(sentences) if _alive_in(s)]
        if not alive:
            continue
        for person in lost:
            names = [rf"(?<![\w'’-]){re.escape(w)}(?![\w-])" for w in name_words(person["name"])]
            trades = [w for w in re.findall(r"[a-z]{4,}", person.get("profession", "").lower()) if w not in _TRADE_STOP]
            words = trades + [a for t in trades for a in _TRADE_ALIASES.get(t, ())]
            when = {" ".join(m.group(0).lower().split()) for m in _SPAN_OF_TIME.finditer(person.get("about", ""))}

            def told(i: int) -> bool:
                # Them by name, or their trade with their name or their own
                # time beside it — "A carter. Three weeks back. Driver
                # survived" (fleet9-tamsin T2); never a time said a breath
                # apart: "…in three weeks of guessing. … because one carter
                # walked back out alive" was the player (fleet-bram T13)
                s = sentences[i]
                if any(re.search(n, s) for n in names):
                    return True
                near = " ".join(" ".join(sentences[max(0, i - 1):i + 1]).split())
                if not (any(re.search(n, near) for n in names) or any(w in near.lower() for w in when)):
                    return False
                return any(not _SOMEONE_ELSE.search(s[max(0, m.start() - 16):m.start()])
                           for w in words for m in re.finditer(rf"\b{re.escape(w)}s?\b", s, re.IGNORECASE))

            if not any(told(i) for i in alive):
                continue
            who = str((line or {}).get("npc_name") or "Someone")
            return {
                "check": "lost_person_alive",
                "detail": (
                    f"{who}'s line has {person['name']} ({person.get('profession') or 'no trade given'}) alive or "
                    f"come back (\"{_clip_line(text, 110)}\"), but {person['name']} is {person['status']} — "
                    f"{_clip_line(person.get('about') or '', 90)}. In your next reply someone sets it straight in "
                    f"their own voice: {person['name']} is {person['status']}, and whoever survived something "
                    "was someone else. A lost person is never told as a survivor."
                ),
            }
    return None


def people_for_collision() -> list[dict[str, str]]:
    """{"id", "name", "profession", "looks", "status"} for every named
    person (beasts carry no borrowable name). [] when unreadable."""
    try:
        from src.models import NPC, get_session
        from src.services.dialogue import is_beast

        with get_session() as session:
            return [{"id": n.id, "name": n.name, "profession": str(n.profession or ""),
                     "looks": str(n.description_physical or ""), "status": n.status}
                    for n in session.query(NPC).all() if n.name and not is_beast(n)]
    except Exception:
        return []


def name_collision(spoken: Iterable[dict[str, Any]], people: Iterable[dict[str, str]], *,
                   present_ids: Iterable[str] = ()) -> dict[str, str] | None:
    """fleet9-wenna T12: "Old Marrow worked nights before me" — a word of a
    real person's name (Vex Marrow, the successor villain, at the Hollow)
    given to someone the DM invented (`dialogue.borrowed_name`: "Old X",
    "X worked … before me"). Not when they are here or the speaker, nor
    when the line says their whole name (then it is them)."""
    from src.services.dialogue import _word_spans, borrowed_name, name_words

    people = [p for p in people or () if isinstance(p, dict) and p.get("name")]
    here = {str(i) for i in present_ids or () if i}
    trades = {p["id"]: p.get("profession", "") for p in people}
    for line in spoken or ():
        text = str((line or {}).get("text") or "")
        speaker_id = str((line or {}).get("npc_id") or "")
        if not text:
            continue
        for person in people:
            if person["id"] in here or person["id"] == speaker_id:
                continue
            words = name_words(person["name"])
            said = [w for w in words if _word_spans(w, text)]
            if not said or len(said) == len(words) > 1:
                continue
            for word in said:
                frame = next((f for s, e in _word_spans(word, text)
                              if (f := borrowed_name(text, s, e, looks=person.get("looks", ""),
                                                     trade=person.get("profession", ""),
                                                     speaker_trade=trades.get(speaker_id, "")))), None)
                if frame:
                    who = str((line or {}).get("npc_name") or "Someone")
                    return {
                        "check": "name_collision",
                        "detail": (
                            f"{who} gave \"{word}\" to someone you invented (\"{_clip_line(frame, 70)}\") — but "
                            f"{word} is {person['name']}'s name in this world. The engine kept it from the player's "
                            "known names. A name is one person: give an invented person a name of their own, never "
                            "a word of anyone in 'Names in use'. If that person comes up again, call them by "
                            "something else (\"the old night hand\")."
                        ),
                    }
    return None


# fleet9-bram T0 (and fleet7/8-bram T0, fleet8-wenna T0): the main job's
# reward, a Wolf-Tooth Charm, was pitched as "a tooth off one of the brutes
# strung on a Watch cord" — a trophy to cut off a wolf, not the charm the
# Captain hands over. A giver's line that prices their own job (its coin)
# or names a reward item by a word of it ("tooth") without the item's own
# name, or with a trophy's words beside it.
_TROPHY = re.compile(r"\b(?:off\s+(?:one\s+of\s+)?(?:the|them|those|a|one|its|their)\b|cut\s+(?:off|from|out)?|"
                     r"bring(?:s|ing)?\s+(?:me\s+)?back|pull(?:ed)?\s+from|pried|pry|carve[ds]?\s+(?:off|from)"
                     r"|taken?\s+off)\b", re.IGNORECASE)
_PRICE_WORDS = {10: "ten", 15: "fifteen", 20: "twenty", 25: "twenty-five", 30: "thirty", 40: "forty",
               50: "fifty", 60: "sixty", 70: "seventy", 80: "eighty", 90: "ninety", 100: "hundred"}
_REWARD_GENERIC = frozenset({"charm", "tincture", "draught", "potion", "token", "ring", "amulet", "cord", "pass"})
REWARD_PRICE_REACH = 80   # letters between the price and the item it names


def offers_for_audit(giver_ids: Iterable[str]) -> list[dict[str, Any]]:
    """{"giver_id", "title", "coin", "items": [names]} for the authored jobs
    these people hold that pay an item (not started, or under way — a job
    is still pitched after it starts). [] when unreadable."""
    ids = [i for i in dict.fromkeys(giver_ids or ()) if i]
    if not ids:
        return []
    try:
        from src.models import Quest, QuestStatus, get_session
        from src.tools.world_write.quests import reward_coin

        out = []
        with get_session() as session:
            for quest in session.query(Quest).filter(Quest.assigned_by_npc_id.in_(ids),
                                                     Quest.status.in_((QuestStatus.NOT_STARTED,
                                                                       QuestStatus.ACTIVE))).all():
                rewards = quest.rewards if isinstance(quest.rewards, dict) else {}
                items = [" ".join(str(e.get("name") if isinstance(e, dict) else e).split())
                         for e in rewards.get("items") or [] if e]
                items = [i for i in items if i]
                if items:
                    out.append({"giver_id": quest.assigned_by_npc_id, "title": quest.title,
                                "coin": reward_coin(rewards), "items": items})
        return out
    except Exception:
        return []


def reward_misnamed(spoken: Iterable[dict[str, Any]], offers: Iterable[dict[str, Any]]) -> dict[str, str] | None:
    """The first giver line that pitches their job's reward item as
    something else (see above)."""
    offers = [o for o in offers or () if isinstance(o, dict)]
    for line in spoken or ():
        text = str((line or {}).get("text") or "")
        lowered = " ".join(text.lower().replace("-", " ").split())
        for offer in offers:
            if str((line or {}).get("npc_id") or "") != offer.get("giver_id"):
                continue
            coin = int(offer.get("coin") or 0)
            said = [rf"\b{coin}\b"] + ([re.escape(_PRICE_WORDS[coin].replace("-", " "))] if coin in _PRICE_WORDS else [])
            # Where the line names the job's price: the item is priced beside
            # it ("Sixty coin, a tooth off…"), not "sells tea and yarrow" a
            # sentence before "ten coin and a tincture" (fleet6-bram T3)
            prices = [m.start() for p in (said if coin else []) for m in re.finditer(p, lowered)]
            for item in offer.get("items") or []:
                name = " ".join(item.lower().replace("-", " ").split())
                if name in lowered:
                    continue
                # The word that says what it is: "tooth" of a Wolf-Tooth Charm
                # ("wolf" is the whole story's), "yarrow" of a Yarrow Tincture
                heads = [w for w in name.split() if len(w) >= 4 and w not in _REWARD_GENERIC][-1:]
                spans = [m for h in heads for m in re.finditer(rf"\b{re.escape(h)}(?:e?s)?\b", lowered)]
                if not spans:
                    continue
                trophy = next((t for m in spans
                               for t in [_TROPHY.search(lowered[max(0, m.start() - 40):m.end() + 40])] if t), None)
                priced = any(abs(m.start() - p) <= REWARD_PRICE_REACH for m in spans for p in prices)
                if not (trophy or priced):
                    continue
                who = str((line or {}).get("npc_name") or "Someone")
                return {
                    "check": "reward_misnamed",
                    "detail": (
                        f"{who} priced \"{offer.get('title')}\" without its reward item's own name "
                        f"(\"{_clip_line(text, 100)}\"). The reward is \"{item}\" — a finished thing they hand "
                        f"over, the player's to keep. Name it as written: \"{item}\"; never a trophy the player "
                        "must cut off, take from a body or bring back."
                    ),
                }
    return None


# A theft the player SAW. A pickpocket complication counts as answered by its
# cost only once that cost reached the screen (src/services/complications.py):
# fleet9-bram T11 wrote "@coin -6 | a quick hand in the press lifts coin from
# his purse", every sentence of the theft was dropped, and the reply the
# player read opened "Sorrel doesn't notice any of it" — 6 coin gone unseen
_THEFT_SHOWN = re.compile(
    r"\b(?:thief|thieves|cutpurse|pickpocket\w*|lift(?:s|ed|ing)?|filch\w*|snatch\w*|stole|stolen|steal\w*"
    r"|swip(?:e|es|ed|ing)|pinch(?:ed|es)|nick(?:ed|s)|lighter|missing|gone|empty|cut\s+(?:loose|free|away|clean)"
    r"|mak(?:e|es|ing)\s+off|made\s+off|slip(?:s|ped|ping)?\s+(?:off|away)|(?:off|away)\s+with\s+your"
    r"|feel\s+(?:it|them)\s+go)\b",
    re.IGNORECASE,
)
_THEFT_OBJECT = re.compile(r"\b(?:your|my)\s+(?:[\w'’-]+\s+)?(?:purse|pouch|pocket|satchel|pack|belt|money|wallet"
                           r"|coin|coins)\b|\b(?:purse|coins?)\b", re.IGNORECASE)
_COST_LINE = re.compile(r"\s*@(?:coin|coins|gold|money|remove|lose|drop)\b(?P<rest>[^|\n]*)", re.IGNORECASE)


def theft_shown(shown_text: str, commands: Iterable[Any]) -> bool:
    """Did what the player read (the reply's prose after every filter) show
    something of theirs taken — a theft word beside their purse, their pack,
    or the very thing a @remove took?"""
    nouns: set[str] = set()
    for row in commands or ():
        get = row.get if isinstance(row, dict) else (lambda key, _r=row: getattr(_r, key, None))
        m = _COST_LINE.match(str(get("line") or "")) if get("ok") else None
        if m:
            nouns |= {w for w in re.findall(r"[a-z]{4,}", m.group("rest").lower())}
    for sentence in _sentences(shown_text or ""):
        if not _THEFT_SHOWN.search(sentence):
            continue
        lowered = sentence.lower()
        if _THEFT_OBJECT.search(sentence) or any(re.search(rf"\b{re.escape(n)}", lowered) for n in nouns):
            return True
    return False


# A speaker label that borrowed the name of someone the engine keeps where
# they are (server._speech_from_markup, dialogue.borrowed_speaker):
# fleet10-wenna T13-T14, the Captain's "Say it plain, Hesk" to a made-up
# cart driver, and "Hesk:" lines filed in the market hawker's memory
def name_in_use_borrowed_warning(borrowed: Iterable[dict[str, Any]]) -> dict[str, str] | None:
    """One warning for this reply's borrowed labels, else None."""
    rows = [b for b in borrowed or () if isinstance(b, dict) and b.get("name")]
    if not rows:
        return None
    parts = []
    for b in list({b["name"]: b for b in rows}.values())[:3]:
        parts.append(f"\"{b.get('label')}:\" is not {b['name']}, who is at {b.get('there')} ({b.get('why')}) — the "
                     f"line went out under \"{b.get('role')}\", in nobody's memory, and nobody moved")
    first = rows[0]
    return {"check": "name_in_use_borrowed", "detail": (
        "; ".join(parts) + f". Whoever spoke at {first.get('here')} is someone new: first thing in your next reply, "
        f"@new-npc <a name of their own, one nobody in the world has> | trade: … | looks: … | at: {first.get('here')}, "
        "and call them by it from then on (what they said stands). If it truly was "
        f"{first['name']}, write @npc-move {first['name']} | {first.get('here')} before their next line instead.")}


# A made-up suspect in a scene the story already has a mystery for
# (src/services/suspects.py): fleet10-wenna T7 `@new-npc Corran Pike | trade:
# drover | … | manner: patient, unreadable`, watching the player from the
# far post while Sorrel's secret was a man in an ash-gray cloak. Replayed
# over every transcript's 11 @new-npc lines: the 3 framed this way were all
# that same invented drover (fleet8-bram T13, fleet9-bram T11, fleet10-wenna
# T7); the laundress, the runner, the clerk and the dead carters were not
_SUSPICIOUS = re.compile(
    r"\bwatch(?:es|ing|ed)?\s+(?:you|the\s+two\s+of\s+you|your)\b|\b(?:does\s*n['’]?t|did\s*n['’]?t|does\s+not"
    r"|never)\s+look\s+away\b|\bunreadable\b|\bstar(?:es|ed|ing)\s+(?:at\s+)?you\b|\blurk\w*|\bhooded\b|\bcowled\b"
    r"|\brefus\w*\s+(?:to\s+give\s+)?(?:his|her|their|a)\s+name\b|\bname['’]s\s+mine\b"
    r"|\bwo\s*n['’]t\s+(?:give|say|tell)\s+(?:you\s+)?(?:his|her|their|a|my)\s+name\b"
    r"|\b(?:eyes|gaze)\s+(?:follow|follows|track|tracks|followed|tracked)\s+you\b|\btoo\s+still\b"
    r"|\bkeeps?\s+to\s+the\s+shadows\b|\bfrom\s+the\s+shadows\b|\bsecretive\b|\bfurtive\w*",
    re.IGNORECASE,
)


def invented_suspect_warning(player_id: str | None, commands: Iterable[Any], prose: str,
                             present_ids: Iterable[str] = ()) -> dict[str, str] | None:
    """A @new-npc this reply made, framed as a watcher or a mystery (its own
    fields, or the prose sentences about them), in a scene whose clues
    already point at the story's own stranger (`suspects.authored_stranger`)
    — else None. Never raises."""
    made = []
    for row in commands or ():
        get = row.get if isinstance(row, dict) else (lambda key, _r=row: getattr(_r, key, None))
        line = str(get("line") or "")
        if get("ok") and get("verb") == "new-npc" or (get("ok") and line.lstrip().lower().startswith("@new-npc")):
            result = get("result") if isinstance(get("result"), dict) else {}
            if result.get("exists"):
                continue
            made.append((str(result.get("name") or line.split(" ", 1)[-1].split("|")[0]).strip(), line,
                         result.get("id")))
    if not made:
        return None
    try:
        from src.models import get_session
        from src.services import suspects

        for name, line, npc_id in made:
            fields = " ".join(line.split("|")[1:])
            words = [w for w in re.findall(r"[A-Za-z][\w'’-]+", name) if len(w) >= 4]
            trade = re.search(r"\btrade:\s*([^|]+)", line, re.IGNORECASE)
            if trade:
                words += [w for w in re.findall(r"[A-Za-z][\w'’-]+", trade.group(1)) if len(w) >= 4][-1:]
            about = [s for s in _sentences(prose or "")
                     if any(re.search(rf"(?<![\w'’-]){re.escape(w)}", s, re.IGNORECASE) for w in words)]
            framed = _SUSPICIOUS.findall(fields + "\n" + "\n".join(about))
            if not framed:
                continue
            with get_session() as session:
                found = suspects.authored_stranger(session, player_id,
                                                   [i for i in present_ids or () if i and i != npc_id])
                if not found:
                    continue
                stranger = suspects.stranger_words(*found)
            return {"check": "invented_suspect", "detail": (
                f"You made up {name} and framed them as a mystery (\"{framed[0]}\") — but nobody wrote them a "
                "secret or a goal, so every turn the player spends on them leads nowhere. The stranger this "
                f"scene's clues already point at is {stranger}. Next reply, let {name} be an ordinary "
                f"{trade.group(1).strip() if trade else 'local'} with nothing to hide (or send them on with "
                "@npc-move), and let the mystery be that one — show signs of them, never a new suspect.")}
    except Exception as e:  # the audit never costs the turn
        import logging
        logging.getLogger(__name__).debug(f"invented-suspect check failed: {e}")
    return None

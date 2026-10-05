"""Did the player set out for a place? Then this reply moves them there.

Round 6 lost the player's own destination twice on a final turn:

  * fleet7-tamsin T20 "I'm already moving toward the Stable Yard": the reply
    wrote no @move, ended "The Stable Yard sits a few minutes off", and left
    her in the square.
  * fleet7-wenna T20 "I head east toward the healer without another word":
    no healer exists anywhere, so the DM wrote `@move The Drowned Lantern`
    and walked her into the next quest-giver's tavern instead.

A small code reader over what the player typed, the sibling of
`roll_intent`: a sentence of their narration (their quoted words are talk —
"I'll go to the Hollow" said aloud is a plan) with the player as subject, a
verb of going and a destination ("I head toward the Drowned Lantern", "we
make for the Hollow", "let's go to the stable yard"). Facing is not going
("I turn toward the gatehouse", "I glance toward the gate"), nor is a plan
("at first light", "before I head for an inn"), a question, a refusal, or
someone else's walk ("I watch Coldiron walk back to his board").

The destination is matched to a real place by its name ("the square",
"the Hollow", "the gate" for North Gatehouse) or by what the place is ("the
tavern", "the Watch post" — its own description says so). A place that does
not exist but is one ("the healer", "the smithy") gets the same binding line
with the way to make it; a person or a thing in the scene gets none. A road
is where it goes ("the north road", "the road home", "back to town"), and
"going to remember every detail" goes nowhere: the last thing they name that
IS a place is where they end up (fleet8-wenna T3, fleet8-tamsin T17/T20).

The line is read back after the reply (`issued`): a reply that wrote no
@move has the engine walk them there (server._walk_as_said).
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# What the player says out loud: "…" or "..." spans
_QUOTED = re.compile(r"[\"“][^\"”\n]*[\"”]")
_SENTENCE = re.compile(r"(?<=[.!?…])\s+|\n+")

# A plan, a condition or a hypothetical is not setting out (fleet6-wenna
# "I'm going up to Wolf's Hollow at first light" five times in one evening;
# fleet-tamsin T2 "Before I head for an inn to sleep, I want to see...")
_LATER = re.compile(
    r"\b(?:first\s+light|dawn|daybreak|sunrise|sunup|tomorrow|later|someday|tonight|morning"
    r"|(?:before|if|unless)\s+(?:I|we)\b|whether|in\s+case)\b",
    re.IGNORECASE,
)
# A time clause holds back only the going inside it: "When I'm done here I
# head for the gate" waits, but "Once I'm into the darker trees, I pick up
# speed toward the north road" goes now (fleet8-tamsin T17/T20: no line, and
# the reply walked her home while the database kept her at the Hollow)
_WHEN_I = re.compile(r"\b(?:after|once|when|until|till)\s+(?:I|we)\b", re.IGNORECASE)
_CLAUSE_BREAK = re.compile(r"[,;—–]|\bthen\b", re.IGNORECASE)
_GO = (r"(?:head|heads|heading|go|goes|going|walk|walks|walking|mak(?:e|es|ing)|set(?:s|ting)?\s+(?:off|out)"
       r"|move|moves|moving|return|returns|returning|hurr(?:y|ies|ying)|run|runs|running|jog|jogs|jogging"
       r"|ride|rides|riding|march|marches|marching|strid(?:e|es|ing)|stroll|strolls|strolling"
       r"|wander|wanders|wandering|drift|drifts|drifting|cross|crosses|crossing|limp|limps|limping"
       r"|sneak|sneaks|sneaking|creep|creeps|creeping|slink|slinks|slinking|tiptoe|tiptoes|tiptoeing"
       r"|dash|dashes|dashing|race|races|racing|rush|rushes|rushing|trudge|trudges|trudging"
       r"|press(?:es|ing)?\s+on|push(?:es|ing)?\s+on|start(?:s|ing)?\s+(?:walking|moving|back|off|out)"
       r"|pick(?:s|ing)?\s+up\s+(?:the\s+|my\s+|our\s+)?(?:pace|speed))")
# Words between the verb and where to: a way of going, never a new clause
# ("I walk over and speak to the keeper of the Lantern" is a person)
_MANNER = (r"(?:back|over|across|down|up|out|off|on|along|straight|right|directly|quick|quickly|slowly|quietly"
           r"|carefully|briskly|fast|now|then|again|east|west|north|south|northward|southward|eastward|westward"
           r"|downstairs|upstairs|outside|home|the|square|street|lane|road|yard|my|our|way|whole|through)")
_PREP = r"(?:to|toward|towards|for|into|inside|in\s+to)"
# The player, then a few words before the verb ("I'm already moving"), or a
# first act and the going ("I tear my eyes away and move quick toward…")
_SETTING_OUT = re.compile(
    r"(?:^|[\s,;—–(])(?:I|I['’]m|I\s+am|we|we['’]re|we\s+are|let['’]s)\s+"
    r"(?P<pre>(?:[\w'’-]+\s+){0,3}?|(?:[\w'’-]+\s+){1,8}?(?:and|then)\s+(?:[\w'’-]+\s+){0,2}?)"
    + _GO + r"\b(?P<mid>(?:\s+" + _MANNER + r"){0,4}?)\s+"
    + r"(?P<prep>" + _PREP + r")\s+(?P<dest>[^.,;:!?—–()\"“”]+)",
    re.IGNORECASE,
)
# The same going inside a destination, its subject already said: "going to
# head for the gate" is the gate
_GOING_ON = re.compile(
    _GO + r"\b(?:\s+" + _MANNER + r"){0,4}?\s+" + r"(?P<prep>" + _PREP + r")\s+(?P<dest>.+)$",
    re.IGNORECASE,
)
# Before the verb: a wish, a need or a modal is not the act ("I need to get
# to the yard", "I'd go to the Lantern"); a negation is no act at all
_NOT_NOW = re.compile(
    r"\b(?:will|would|should|could|might|must|want|wants|need|needs|plan|planning|mean|intend|think|wonder"
    r"|consider|try|trying|not|never|don['’]t|won['’]t|can['’]t|isn['’]t|ll|d"
    # "as I prepare to walk into Wolf's Hollow" is not the walk yet
    r"|prepare|prepares|preparing|ready|about)\b|['’](?:ll|d)\b",
    re.IGNORECASE,
)
# ...and right before it, someone else's going: "I watch Coldiron walk back
# to his notice board" (fleet-bram T1), "I tell Sorrel to go to the yard"
_SOMEONE_ELSE = re.compile(
    r"\b(?:watch|watches|watching|see|sees|saw|let|lets|help|helps|tell|tells|told|ask|asks|asked|send|sends"
    r"|sent|order|orders|ordered|wave|waves|waved|make|makes|have|has|get|gets)\s+(?:[\w'’-]+\s+){1,3}?(?:to\s+)?$",
    re.IGNORECASE,
)
# Where the destination ends: a new clause, a purpose or a manner
_DEST_END = re.compile(
    r"\s+(?:and|but|or|where|while|with|without|before|after|as|so|because|then|until|till|since|keeping"
    r"|letting|already|now|tonight|at|to\s+(?!the\b|a\b|an\b)\w+)\b.*$",
    re.IGNORECASE,
)
# ...and what is no destination at all: "walking into before the second
# bell sounds" (fleet2-bram T7), "drift toward whoever looks interesting"
_NOT_A_PLACE = re.compile(
    r"^(?:before|after|what|whatever|where|wherever|who|whoever|whom|it|him|her|them|me|us|you|if|when"
    r"|while|whichever|something|anything|nothing)\b",
    re.IGNORECASE,
)
_STOP = {"the", "a", "an", "of", "my", "our", "his", "her", "their", "that", "this", "nearest", "closest",
         "old", "little", "big", "main", "local", "town", "towns"}
# "going to remember every detail of this" is a plan, not a place
# (fleet8-wenna T3: read as the destination, it overwrote "the North
# Gatehouse" and no line formed, so a job pitch stopped her instead). After
# "to", a bare lowercase word that is no article, owner, size, direction or
# place noun is a verb
_DETERMINERS = frozenset({"the", "a", "an", "my", "our", "his", "her", "their", "its", "your", "that", "this",
                          "these", "those", "some", "one", "two", "three", "each", "every", "another", "any"})
_BARE_PLACES = frozenset({"town", "home", "bed", "market", "church", "chapel", "camp", "shore", "sea", "port",
                          "harbor", "harbour", "court", "shelter", "cover", "safety", "ground", "land"})
# What a place is, for a destination that names no place by name: the head
# noun's family, looked for in a place's name and the first sentence of its
# description ("A low-beamed tavern interior…"), and in people's trades
# Services a world can grow on demand when the player sets out for one
# ("the healer", "the smithy"); the features below are things in some scene
# ("the paddock gate", "that hut") and are never made from a sentence
_SERVICE_KINDS = (
    "healer healers physician surgeon doctor apothecary herbalist infirmary hospice chirurgeon",
    "inn tavern alehouse taproom pub lodging lodgings hostel boardinghouse boarding",
    "smith smithy blacksmith forge armourer armorer",
    "temple shrine chapel church sanctuary",
    "post watchhouse guardhouse barracks garrison",
    "dock docks harbor harbour pier wharf quay",
    "market marketplace", "shop store emporium", "mill", "jail gaol prison cells", "library archive",
    "graveyard cemetery", "bathhouse baths",
)
_FEATURE_KINDS = ("stable stables stableyard", "gate gatehouse", "square plaza", "hall", "tower", "cottage",
                  "house", "hut", "camp", "farm", "bridge", "cellar")
_PLACE_KINDS: tuple[frozenset[str], ...] = tuple(frozenset(g.split()) for g in _SERVICE_KINDS + _FEATURE_KINDS)
_KIND_OF: dict[str, frozenset[str]] = {word: group for group in _PLACE_KINDS for word in group}
_SERVICES = frozenset(" ".join(_SERVICE_KINDS).split())


def _narration(text: str) -> str:
    """What the player DOES: their input with what they SAY taken out."""
    return _QUOTED.sub(" ", text or "")


def _words(text: str) -> list[str]:
    raw = (text or "").lower().replace("’", "'")
    return [w.split("'")[0] for w in re.findall(r"[a-z0-9]+(?:'[a-z]+)?", raw)]


def _verb_first(prep: str, dest: str) -> bool:
    """Is this "to <verb>…", not "to <somewhere>"? ("to remember every
    detail", "to see Piper" — never "to the gate", "to town", "to Wolf's
    Hollow", "to the stables")."""
    words = dest.split()
    if (prep or "").lower() != "to" or not words or not words[0][:1].islower():
        return False
    first = words[0].lower().replace("’", "'")
    base = first.split("'")[0]
    if first.endswith("'s") or base in _DETERMINERS or base in _BARE_PLACES or base in _STOP \
            or base in _DIRECTIONS or base in _ROADS:
        return False
    return not any(f in _KIND_OF for f in _forms(base))


def destination_phrases(player_input: str) -> list[str]:
    """Every place the player sets out for NOW, as they named them, in the
    order they said them ("the North Gatehouse"; "the square", "the north
    road"). The caller keeps the last one that is a place."""
    if not player_input or player_input.lstrip().startswith("["):
        return []
    found: list[str] = []
    for sentence in _SENTENCE.split(_narration(player_input)):
        sentence = sentence.strip()
        if not sentence or sentence.endswith("?") or _LATER.search(sentence):
            continue
        for m in _SETTING_OUT.finditer(sentence):
            pre = m.group("pre") or ""
            last = re.split(r"\b(?:and|then)\s+", pre, flags=re.IGNORECASE)[-1]
            if _NOT_NOW.search(pre) or _SOMEONE_ELSE.search(last):
                continue
            # Inside a time clause the going waits ("when I'm done I head…")
            verb_at = m.start("pre")
            held = [w for w in _WHEN_I.finditer(sentence) if w.start() < verb_at]
            if held and not _CLAUSE_BREAK.search(sentence[held[-1].end():verb_at]):
                continue
            prep, raw = m.group("prep"), m.group("dest").strip()
            if _verb_first(prep, raw):
                # "going to head for the gate" goes to the gate; "going to
                # remember…" goes nowhere
                inner = _GOING_ON.match(raw)
                if inner is None or _verb_first(inner.group("prep"), inner.group("dest").strip()):
                    continue
                raw = inner.group("dest").strip()
            dest = _DEST_END.sub("", raw).strip()
            words = dest.split()
            if not words or len(words) > 6 or _NOT_A_PLACE.match(dest):
                continue
            found.append(dest)
    return found


def destination_phrase(player_input: str) -> str | None:
    """Where the player sets out for NOW, as they named it ("the Stable
    Yard", "the healer"), or None. The last one they name is where they end
    up ("I head back toward the square, then make for the north road")."""
    found = destination_phrases(player_input)
    return found[-1] if found else None


def _forms(word: str) -> set[str]:
    forms = {word}
    if len(word) > 4 and word.endswith("es") and word[-3] in "sxz":
        forms.add(word[:-2])
    elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        forms.add(word[:-1])
    return forms


_DIRECTIONS = {"north", "south", "east", "west", "northern", "southern", "eastern", "western", "upper", "lower"}
# A road is where it goes (fleet8-tamsin T17/T20 "toward the north road" home
# from the Hollow: no line, and the reply walked her home in prose while the
# database kept her there). "The north road" is the road a way out's own
# description calls that — from one of its ends, the other end; from
# elsewhere, its nearer end. "The road home", "back to town" or the town's
# own name, from a place with one way out, is that way. A bare "the road" is
# the road they stand on (fleet8-bram T6 "head for the road" at the Hollow)
_ROADS = frozenset({"road", "roads", "path", "track", "trail", "lane", "highway"})
_HOMEWARD = frozenset({"home", "homeward", "back", "town", "village", "city"})
# A family's plain nouns: "the boarding house" is an inn, "the healer's
# cottage" a healer's
_GENERIC_KINDS = {"house", "hall", "hut", "cottage", "tower", "bridge", "cellar", "camp", "farm"}


def _names(phrase: str, name: str) -> bool:
    """Does the phrase name this place? Half of its distinctive words ("the
    square", "the Hollow", "the yard"), a plural ("the stables"), or alone a
    word that begins one ("the north gate" -> North Gatehouse) — and the
    phrase is ABOUT the place: its last word is the place's, or the place
    owns it ("the Drowned Lantern's glow"). "The stable boy" is a person and
    "the paddock gate" a gate in a yard."""
    from src.services.campaign import _distinctive

    words = _distinctive(name)
    raw = re.findall(r"[a-z0-9]+(?:'s)?", (phrase or "").lower().replace("’", "'"))
    owners = {w[:-2] for w in raw if w.endswith("'s")}
    content = [w[:-2] if w.endswith("'s") else w for w in raw]
    content = [w for w in content if w not in _STOP and w not in _DIRECTIONS]
    if not words or not content:
        return False

    def same(word: str, w: str) -> bool:
        return bool(_forms(word) & _forms(w))

    def begins(word: str, w: str) -> bool:
        return len(word) >= 4 and len(w) >= len(word) + 3 and w.startswith(word)

    named = [w for w in words if any(same(p, w) for p in content)]
    begun = [w for w in words if w not in named and any(begins(p, w) for p in content)]
    if (len(named) + len(begun)) * 2 < len(words):
        return False
    head = content[-1]
    if any(same(head, w) for w in words) or any(same(o, w) for o in owners for w in words):
        return True
    return len(content) == 1 and any(begins(head, w) for w in words)


def _road_end(session, player, phrase: str, towns: list) -> Any:
    """The place a road phrase takes the player ("the north road", "the road
    home", "back to town", "toward Emberfall"), or None when it names no
    road, or the road could go two ways (see _ROADS)."""
    from src.models import Connection, Location

    here_id = getattr(player, "current_location_id", None)
    said = [w for w in _words(phrase) if w not in _DETERMINERS and w not in ("of", "to")]
    if not here_id or not said:
        return None
    town_ids = {t.id for t in towns}

    def road(word: str) -> bool:
        return any(f in _ROADS for f in _forms(word))

    named_town = any(_names(phrase, t.name or "") for t in towns)
    rest = [w for w in said if not road(w) and w not in _HOMEWARD and w not in _STOP and w != "way"]
    conns = session.query(Connection).filter(Connection.discovered == True).all()  # noqa: E712
    if named_town or (any(w in _HOMEWARD for w in said) and not rest):
        # Home is the one way out of here — from the Hollow, the gatehouse
        ways = {c.to_location_id if c.from_location_id == here_id else c.from_location_id for c in conns
                if c.from_location_id == here_id or (c.to_location_id == here_id and c.bidirectional)}
        ways -= town_ids
        if not ways:
            here = session.get(Location, here_id)
            if here is not None and here.parent_id and here.parent_id not in town_ids:
                ways = {here.parent_id}
        return session.get(Location, next(iter(ways))) if len(ways) == 1 else None
    if not rest or not any(road(w) for w in said):
        return None
    # A road its description names: every word of the phrase is there
    want = [_forms(w) for w in said if w not in _HOMEWARD]
    ends: set[str | None] = set()
    for conn in conns:
        told = {f for w in _words(f"{conn.description or ''} {conn.travel_type or ''}") for f in _forms(w)}
        if not all(forms & told for forms in want):
            continue
        a, b = conn.from_location_id, conn.to_location_id
        ends.add(b if here_id == a else a if here_id == b else _nearer(session, here_id, a, b, town_ids))
    ends -= town_ids | {None}
    return session.get(Location, next(iter(ends))) if len(ends) == 1 else None


def _nearer(session, here_id: str, a: str, b: str, town_ids: set[str]) -> str | None:
    """The end of a road fewer steps from here (never through a whole
    town), or None when neither is nearer."""
    from collections import deque

    from src.services.lead import _adjacency

    adjacency = _adjacency(session)
    dist = {here_id: 0}
    queue: deque[str] = deque([here_id])
    while queue:
        u = queue.popleft()
        for v in adjacency.get(u, ()):
            if v not in dist and v not in town_ids:
                dist[v] = dist[u] + 1
                queue.append(v)
    da, db = dist.get(a), dist.get(b)
    if da is not None and (db is None or da < db):
        return a
    if db is not None and (da is None or db < da):
        return b
    return None


def _what_it_is(location) -> set[str]:
    first = re.split(r"(?<=[.!?])\s", str(location.description or ""), maxsplit=1)[0]
    return {f for w in _words(f"{location.name} {first}") for f in _forms(w)}


def resolve(session, player, phrase: str) -> dict[str, Any] | None:
    """{"place": Location | None, "kind": head noun | None, "closest": text | None}
    for a destination phrase, or None when it is no place (a person, a
    thing in the scene, "the noise")."""
    from src.models import NPC, Connection, Location
    from src.services.commands import whole_town

    words = [w for w in _words(phrase) if w not in _STOP and w not in _DIRECTIONS]
    here_id = getattr(player, "current_location_id", None)
    # The ways out of here first, then everywhere the player could know
    near: set[str] = set()
    if here_id:
        for conn in session.query(Connection).filter(
            (Connection.from_location_id == here_id) | (Connection.to_location_id == here_id)
        ).all():
            near.add(conn.to_location_id if conn.from_location_id == here_id else conn.from_location_id)
    everywhere = session.query(Location).all()
    town_ids = {p.id for p in everywhere if whole_town(p.id)}
    places = [p for p in everywhere if p.id not in town_ids]
    places.sort(key=lambda p: (p.id not in near, not p.discovered, p.name or ""))
    if words:
        for place in places:
            if _names(phrase, place.name or ""):
                return {"place": place, "kind": None, "closest": None}
    # A road, or the way home (see _ROADS)
    end = _road_end(session, player, phrase, [p for p in everywhere if p.id in town_ids])
    if end is not None:
        return {"place": end, "kind": None, "closest": None, "road": phrase}
    if not words:
        return None
    # The head noun's family; a plain one defers to the word before it
    # ("the boarding house" is an inn). Only the head: "the stable boy" is
    # a person, not a stable
    def kind(word: str) -> frozenset[str] | None:
        return next((_KIND_OF[f] for f in _forms(word) if f in _KIND_OF), None)

    head = words[-1]
    group = kind(head)
    if head in _GENERIC_KINDS and len(words) >= 2 and kind(words[-2]) is not None:
        head, group = words[-2], kind(words[-2])
    if group is None:
        return None
    # What the place is, said by its own name or description: "the tavern"
    # is The Drowned Lantern, "the Watch post" the North Gatehouse — and the
    # rest of the phrase fits it too ("the paddock gate" is no gatehouse)
    others = [w for w in words if w != head and kind(w) is None]
    for place in places:
        said = _what_it_is(place)
        if said & group and all(_forms(w) & said for w in others):
            return {"place": place, "kind": head, "closest": None}
    # No such place. A service the world can grow ("the healer", "the
    # smithy") is made; a gate, a yard or a hut is a thing in some scene
    if head not in _SERVICES:
        return None
    # ...and the nearest real thing to it is someone of that trade — one
    # standing right here is no place to @move to
    closest, closest_here = None, None
    for npc in sorted(session.query(NPC).filter(NPC.status == "alive").all(),
                      key=lambda n: (n.current_location_id != here_id, n.name or "")):
        trade = {f for w in _words(npc.profession or "") for f in _forms(w)}
        where = session.get(Location, npc.current_location_id) if npc.current_location_id else None
        if not (trade & group) or where is None or whole_town(where.id):
            continue
        if where.id == here_id:
            closest_here = f"{npc.name} ({npc.profession})"
        else:
            closest = f"{where.name}, where {npc.name} ({npc.profession}) is"
        break
    return {"place": None, "kind": head, "closest": closest, "closest_here": closest_here}


def directive(player_id: str, player_input: str) -> str | None:
    """The binding line for a reply whose player set out for a place, or
    None. Never raises."""
    try:
        return _directive(player_id, player_input)
    except Exception as e:  # the line is a nudge, never a cost to the turn
        logger.warning(f"travel intent unavailable: {e}")
        return None


def _directive(player_id: str, player_input: str) -> str | None:
    from src.models import Player, get_session
    from src.services.commands import whole_town
    from src.services.lead import next_hop
    from src.services.travel import check_route

    phrases = destination_phrases(player_input)
    if not phrases or not player_id:
        return None
    with get_session() as session:
        player = session.get(Player, player_id)
        if player is None:
            return None
        # The last place they name is where they end up — a later phrase
        # that names no place never hides it (fleet8-wenna T3)
        found, phrase = None, None
        for phrase in reversed(phrases):
            found = resolve(session, player, phrase)
            if found is not None:
                break
        if found is None:
            return None
        place = found["place"]
        if place is None:
            other = f" (or use {found['closest']})" if found["closest"] else ""
            # fleet7-wenna T20 stood in the square with the herbalist: "use
            # Market Square … then @move there" is a move to where she is
            here = (f" Unless they turn instead to {found['closest_here']}, who is right here — then no @move."
                    if found.get("closest_here") else "")
            return (
                f"[THIS REPLY: the player sets out for \"{phrase}\" — no such place exists yet. "
                f"@new-place it{other}, then @move there first (after any roll this reply needs) and open "
                f"there.{here} Never walk them somewhere they did not choose.]"
            )
        if place.id == player.current_location_id:
            return None
        route = check_route(session, player.current_location_id, place.id, player)
        if route.get("blocked"):
            hop_id = next_hop(session, player, place.id)
            # A hop through a whole town is no @move (the engine refuses it)
            hop = (session.get(type(place), hop_id)
                   if hop_id and hop_id != place.id and not whole_town(hop_id) else None)
            from src.services.travel import BARRED_TONIGHT, night_why

            why = night_why(route, player) or "; ".join(
                str(r) for r in route.get("requirements") or []
                if not (route.get("barred_tonight") and r == BARRED_TONIGHT)) or "the way is shut"
            short = f"@move {hop.name}, " if hop is not None else ""
            # fleet8-tamsin T11-T13: after the howl the guard read the seal and
            # "the gate swings wide" — at night the pass is not what opens it.
            # Round 8: the bar was a dead end in all three sessions ("The north
            # road isn't going anywhere before morning") — so the line names
            # the ways still open, for the DM to put in front of the player
            past = (f"the way is barred for the night and a pass does not open it. Say what is still open — a "
                    "price (a bribe to whoever keeps it: @coin -N, the reason naming the gate), a hard climb "
                    "over the wall in the dark (roll_check FIRST; the dark past it has teeth), or a bed till "
                    "first light (@rest at an inn) — the player chooses: never roll or charge for a way they did "
                    f"not take; only a won roll or a paid bribe in this reply gets "
                    f"them through, then @move {place.name} | through"
                    if route.get("barred_tonight") else
                    f"only if the fiction truly gets them past it, write @move {place.name} | through")
            return (
                f"[THIS REPLY: the player sets out for {place.name}, but the way is shut to them ({why}). "
                f"Take them as far as it lets them ({short}show what stops them) — {past}. Never narrate "
                "them arriving otherwise.]"
            )
        # fleet7-wenna T10 "I head straight for the north gate" with the road
        # to the Hollow ahead: the gate is the first step, not the last
        said = f" (they said \"{found['road']}\")" if found.get("road") else ""
        gate = gate_note(gated_hops(session, player, player.current_location_id, place.id))
        return (
            f"[THIS REPLY: the player goes to {place.name}{said} — write @move {place.name} first (after any "
            "roll this reply needs) and open there; if their words carry them on past it, a second @move "
            "takes them further. Never leave them where they stand, and never take them somewhere they "
            f"did not say.{' ' + gate if gate else ''}]"
        )


# A gate on the way, passed on what the player carries: fleet10-bram T11
# `@move Wolf's Hollow` from the Stable Yard crossed the pass-held North
# Gatehouse road, and the prose went "past the paddock fence and into the
# lane… The quarry road climbs ahead" — the pass a quest step had cost, set
# up from T0 ("Gatehouse won't argue with it"), was never shown checked
def gated_hops(session, player, origin_id: str | None, dest_id: str | None) -> list[dict[str, str]]:
    """[{"from", "to", "item"}] for each requirement-held road on the way
    from `origin_id` to `dest_id` that the player passes only on what they
    carry (`travel.held_pass`) — never one they were waved through before
    (`cleared_for`). The way is the roads' fewest steps, as the engine walks
    it (`commands._road_minutes`): a road-less end steps through its
    doorway; two roaded places never cut through the town they sit in.
    Never raises."""
    from collections import deque

    from src.models import Connection, Location
    from src.services.travel import barred_tonight, held_pass

    try:
        if not origin_id or not dest_id or origin_id == dest_id or player is None:
            return []
        roads: dict[str, list[tuple[str, Any]]] = {}
        on_a_road: set[str] = set()
        for conn in session.query(Connection).all():
            roads.setdefault(conn.from_location_id, []).append((conn.to_location_id, conn))
            if conn.bidirectional:
                roads.setdefault(conn.to_location_id, []).append((conn.from_location_id, conn))
            on_a_road.update((conn.from_location_id, conn.to_location_id))

        def doorways(place_id: str) -> list[str]:
            place = session.get(Location, place_id)
            kin = [place.parent_id] if place is not None and place.parent_id else []
            return kin + [row[0] for row in session.query(Location.id).filter(Location.parent_id == place_id).all()]

        if origin_id not in on_a_road:
            roads.setdefault(origin_id, []).extend((kin, None) for kin in doorways(origin_id))
        if dest_id not in on_a_road:
            for kin in doorways(dest_id):
                roads.setdefault(kin, []).append((dest_id, None))
        previous: dict[str, tuple[str, Any] | None] = {origin_id: None}
        queue: deque[str] = deque([origin_id])
        while queue and dest_id not in previous:
            here = queue.popleft()
            for there, conn in roads.get(here, ()):
                if there not in previous:
                    previous[there] = (here, conn)
                    queue.append(there)
        if dest_id not in previous:
            return []
        hops: list[dict[str, str]] = []
        step = dest_id
        while previous.get(step) is not None:
            here, conn = previous[step]
            if conn is not None and conn.requirements and player.id not in (conn.cleared_for or []) \
                    and not barred_tonight(conn):
                item = held_pass(conn.requirements, player)
                a, b = session.get(Location, here), session.get(Location, step)
                if item and a is not None and b is not None:
                    hops.append({"from": a.name, "to": b.name, "item": item})
            step = here
        return list(reversed(hops))
    except Exception as e:  # a note, never a cost to the turn
        logger.debug(f"gated hops unavailable: {e}")
        return []


def gate_note(hops: list[dict[str, str]]) -> str:
    """One sentence for the reply about the gate on the way, or ""."""
    if not hops:
        return ""
    hop = hops[0]
    return (f"On the way the road from {hop['from']} to {hop['to']} is held: the {hop['item']} they carry is what "
            "gets them through — show it checked there, in a sentence.")


# The binding line, read back off the reply's context after the reply
# (server: the engine walks them there when the reply wrote no @move)
_ISSUED_GO = re.compile(r"\[THIS REPLY: the player goes to (?P<place>[^\n\[\]]+?)(?: \(they said \"[^\"\n]*\"\))? "
                        r"— write @move (?P=place) first")
_ISSUED_SHUT = re.compile(r"\[THIS REPLY: the player sets out for (?P<place>[^\n\[\]\"]+?), but the way is shut "
                          r"to them \((?P<why>[^\n]*?)\)\. Take them as far")


def issued(context: str | None) -> dict[str, Any] | None:
    """The travel line this reply was given: {"place": name, "shut": bool,
    "why": what stops them ("" when open)}, or None."""
    text = context or ""
    m = _ISSUED_GO.search(text)
    if m:
        return {"place": m.group("place"), "shut": False, "why": ""}
    m = _ISSUED_SHUT.search(text)
    if m:
        return {"place": m.group("place"), "shut": True, "why": m.group("why")}
    return None

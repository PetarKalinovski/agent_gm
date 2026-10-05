"""Who spoke, and what they will remember of it.

The DM voices every NPC in screenplay lines (`src/web/dialogue_markup.py`).
This module is the code side of that contract:

  * `resolve_speaker` turns the label the DM wrote ("Coldiron", "Brann",
    "Captain Coldiron", "Gate Guard") into a real NPC — people in the room
    first, then the party, then anyone in the world with an unambiguous
    name — or leaves it ambient.
  * `record_turn_dialogue` files each speaker's lines, with the player's
    words, in that NPC's markdown memory (`memory_files`): the durable
    memory the scene brief reads back next time (their dossier). On the
    first real exchange it writes the NPCRelationship row — trust and
    disposition are mechanics, and the join/secret/refusal gates read them
    from the row — and it ticks `talk_to` objectives for someone the player
    talked TO (`_talked_to`), since talking to someone is the thing those
    objectives are about.
  * Who knows whom (fleet6): the row is the relationship; `name_known` and
    `knows_player_name` on it say whose names are known. `player_has_met`
    asks the first ("does the player know their name?").
  * `role_label`: what a stranger is called. Playtest 2026-10-04: chips and
    speaker labels handed the player "Vex" and "Aldric" (the boss) before
    either was introduced, and NPCs called the player by a name nobody had
    said. A person whose name the player has not learned is shown, voiced
    and chipped under a spoiler-free role ("Ash-Cloaked Man", "Watch
    Captain") until their name is said where the player can hear it.
  * `NameGate` (fleet4, round 3): a label is not an introduction unless the
    name was SAID where the player could hear it, and the player sees only
    the part of a name they heard — on speaker lines, and in the prose,
    where an unmet person's unheard name becomes "the Ash-Cloaked Man".
    `player_facing*` (fleet5, round 4) puts the HUD's text — the lead,
    quest steps, chips, toasts, the story panel — through the same rule.

There is no NPC sub-agent any more; this file, the memory files and the
dossiers in `scene_brief.py` are its memory, done in code.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from src.models import NPC, NPCRelationship, Player, WorldClock, get_session

logger = logging.getLogger(__name__)

_TITLE_WORDS = {
    "captain", "sergeant", "lord", "lady", "sir", "dame", "master", "mistress", "old",
    "young", "mother", "father", "brother", "sister", "doctor", "dr", "the", "madam",
}


def _tokens(name: str) -> list[str]:
    # One apostrophe: a model that types "Herbalist’s Apprentice" means the
    # stranger whose label is "Herbalist's Apprentice"
    return [t for t in re.findall(r"[a-zà-ÿ']+", (name or "").lower().replace("’", "'")) if t]


def _distinctive(name: str, *, epithets: bool = True) -> set[str]:
    tokens = _tokens(name)
    if not epithets and "the" in tokens[1:]:
        # "Hilda the Guard": what follows "the" is a role, not her name
        tokens = tokens[:tokens.index("the", 1)]
    return {t for t in tokens if t not in _TITLE_WORDS and len(t) > 2}


def _match(label: str, candidates: list[NPC], *, epithets: bool = True, overlap: bool = True) -> NPC | None:
    """Exact (case-insensitive) name, else a unique distinctive-word match.

    `epithets=False` keeps a bare role ("Guard") from matching someone whose
    name merely carries it as an epithet ("Hilda the Guard"). `overlap=False`
    wants every distinctive word of the label in the name ("Coldiron"), not
    one shared word: across the world, "Ash-Cloaked Man" is not Mirelle Ash.
    """
    low = " ".join(_tokens(label))
    if not low:
        return None
    for npc in candidates:
        if " ".join(_tokens(npc.name)) == low:
            return npc
    words = _distinctive(label)
    if not words:
        return None
    hits = [npc for npc in candidates if words <= _distinctive(npc.name, epithets=epithets)]
    if len(hits) == 1 or not overlap:
        return hits[0] if len(hits) == 1 else None
    hits = [npc for npc in candidates if words & _distinctive(npc.name, epithets=epithets)]
    return hits[0] if len(hits) == 1 else None


# ----------------------------------------------------------------------
# What a stranger is called
# ----------------------------------------------------------------------

STRANGER = "Stranger"
MAX_ROLE_WORDS = 3

# A profession column holding a placeholder is no trade: "None" became the
# label "None" for an infant in The Abyssal Bloom
_NO_TRADE = frozenset({"none", "n/a", "na", "unknown", "nothing", "no", "-", "?", "tbd", "null"})
# Where one profession lists several ("Knight, Blackfyre Agent")
_TRADE_PARTS = re.compile(r"\s*(?:/|&|;|,|—|–|\s-\s|\band\b|\bor\b)\s*", re.I)
# "Maester serving House Ashford" is a maester, not a "Serving House Ashford"
_TRADE_CLAUSE = re.compile(
    r"(?:^|\s+)(?:serving|working|employed|sworn|loyal|assigned|attached|posing|masquerading|disguised|"
    r"currently|formerly)\b.*$", re.I,
)
# A trade that IS the secret: naming it to a stranger's face spoils who they
# are ("Ashen Fangs scout" lurking at the Hollow). Those are read by looks.
_COVERT_ROLES = frozenset({
    "spy", "spymaster", "assassin", "cultist", "smuggler", "fence", "thief", "cutpurse",
    "pickpocket", "informant", "infiltrator", "saboteur", "agent", "operative", "scout",
    "lookout", "poisoner", "killer", "murderer", "pretender", "conspirator", "traitor",
    "mole", "shadow",
})
# "Captain of the Lantern Watch" -> "Watch Captain": the institution is
# public; anything else after "of" (a proper name, a list of wares) is dropped
_INSTITUTIONS = frozenset({
    "watch", "guard", "garrison", "militia", "guild", "temple", "church", "cathedral", "chapel",
    "shrine", "library", "archive", "archives", "bank", "harbor", "harbour", "docks", "market",
    "stables", "mill", "forge", "smithy", "mine", "mines", "gate", "gates", "keep", "castle",
    "palace", "court", "council", "academy", "college", "school", "infirmary", "hospital",
    "granary", "treasury", "mint", "lighthouse", "ferry", "fleet", "navy", "army", "constabulary",
})
_GARMENTS = {
    "cloak": "Cloaked", "hood": "Hooded", "mantle": "Mantled", "coat": "Coated", "greatcoat": "Coated",
    "robe": "Robed", "robes": "Robed", "apron": "Aproned", "veil": "Veiled", "mask": "Masked",
    "gown": "Gowned", "uniform": "Uniformed", "cape": "Caped", "shawl": "Shawled", "hat": "Hatted",
    "furs": "Furred", "armor": "Armored", "armour": "Armoured",
}
_COLOURS = frozenset({
    "ash", "gray", "grey", "black", "white", "red", "crimson", "scarlet", "blue", "green", "brown",
    "gold", "golden", "silver", "yellow", "purple", "violet", "copper", "bronze", "russet", "ochre",
    "rust", "tawny", "pale", "dark", "sable", "ivory", "indigo", "emerald", "azure", "umber",
})
_AGE_WORDS = frozenset({"old", "young", "elderly", "aged", "ancient", "middle-aged", "teenage", "youthful"})
# Adjectives that end in -ly. Any other -ly word bends the word after it
# ("solidly built", "hauntingly beautiful" — "Solidly Man" was a label)
_LY_ADJECTIVES = frozenset({
    "elderly", "burly", "portly", "surly", "kindly", "friendly", "lovely", "ugly", "sickly", "stately",
    "homely", "comely", "gangly", "wily", "curly", "jolly", "lonely", "lowly", "holy", "oily", "lively",
    "motherly", "fatherly", "scholarly", "saintly", "courtly", "lordly", "ungainly", "unruly", "sprightly",
})
# How a garment is made or worn, not what sets it apart ("simple but
# well-made armor" read as "Made-Armored")
_HOW_WORN = frozenset({"ill", "well", "made", "built", "worn", "torn", "cut", "kept", "sewn", "spun", "woven",
                       "too", "very", "over", "under"})
_PERSON_NOUNS = (
    "man", "woman", "boy", "girl", "youth", "kid", "child", "baby", "infant", "lad", "lass", "figure", "veteran",
    "knight", "crone", "hag", "elder", "giant", "dwarf", "elf", "gnome", "halfling", "orc",
    "goblin", "automaton", "priest", "priestess", "monk", "nun", "soldier", "warrior", "hawker",
    "merchant", "sailor", "fisherman", "farmer", "beggar", "noble", "nobleman", "noblewoman",
    "gentleman", "matron", "widow", "hunter", "scholar", "traveller", "traveler", "wanderer",
)
_BARE_NOUNS = frozenset({"man", "woman", "figure"})   # "Man" alone tells the player nothing
_STOP = frozenset({"a", "an", "the", "his", "her", "their", "its", "of", "with", "in", "and", "to", "but", "yet", "or"})
_WORD = r"[a-z][a-z'’-]*"
_NOT_STOP = r"(?!(?:an?|the|of|with|in|on|at|to)\b)"
_PERSON_RE = re.compile(
    rf"(?:^|\b(?:an?|the)\s+)((?:{_NOT_STOP}{_WORD},?\s+(?:and\s+)?){{0,4}}?)({'|'.join(_PERSON_NOUNS)})\b"
)
_GARMENT_RE = re.compile(
    rf"\b(?:in|wearing|wears|with)\s+(?:an?\s+|the\s+|his\s+|her\s+|their\s+)?"
    rf"((?:{_NOT_STOP}{_WORD},?\s+(?:and\s+)?){{0,3}}?)({'|'.join(_GARMENTS)})\b"
)


def _title(words: list[str]) -> str:
    """Title Case that leaves "herbalist's" as "Herbalist's", not "Herbalist'S",
    and an acronym ("CEO") as it was written."""
    return " ".join(w if len(w) > 1 and w.isupper() else
                    "-".join(p[:1].upper() + p[1:].lower() for p in w.split("-")) for w in words)


def _faction_name(npc: NPC) -> str:
    try:
        faction = npc.faction
    except Exception:  # a detached row: no faction to strip
        return ""
    name = str(getattr(faction, "name", "") or "")
    return re.sub(r"^the\s+", "", name.strip(), flags=re.I)


def _role_words(part: str, faction: str) -> list[str]:
    """One profession cut to a public role: "Captain of the Lantern Watch" ->
    [watch, captain]; "Ashen Fangs scout" -> [scout] (allegiance is learned,
    not seen); "Retired adventurer turned innkeeper" -> [innkeeper]."""
    text = _TRADE_CLAUSE.sub("", part.strip())
    text = re.sub(r"^.*?\bturned\s+", "", text, flags=re.I)
    if faction and text.lower().startswith(faction.lower()):
        text = text[len(faction):]
    m = re.match(r"^(.*?)\s+(of|at|for|from|to|in|on|with)\s+(.*)$", text, re.I)
    head, prep, tail = (m.group(1), m.group(2).lower(), m.group(3)) if m else (text, "", "")
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'’-]*", head) if w.lower() not in ("the", "a", "an")]
    if prep == "of" and len(words) == 1:
        place = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'’-]*", re.split(r"\s+of\s+", tail)[0])]
        if place and place[-1] in _INSTITUTIONS:
            words = [place[-1]] + words
    # The head noun is last; past three words the far end is a split proper
    # name ("Santa Fe Office Manager" read as "Fe Office Manager")
    return words if len(words) <= MAX_ROLE_WORDS else words[-2:]


def _trade_options(npc: NPC) -> list[list[str]]:
    """Each trade the profession lists, as role words, in the order written
    ("Bartender & Information Broker" -> [bartender], [information, broker])."""
    text = re.sub(r"\([^)]*\)", " ", str(npc.profession or "")).strip()
    if text.lower().strip(" .") in _NO_TRADE:
        return []
    faction = _faction_name(npc)
    return [words for part in _TRADE_PARTS.split(text) if (words := _role_words(part, faction))]


def _is_covert(words: list[str]) -> bool:
    return any(p in _COVERT_ROLES for w in words for p in w.lower().split("-"))


def _modifier(words: list[str]) -> str:
    """The word that sets a garment apart: a colour if there is one, else the
    nearest one that says something; of a compound, its first part that is
    not a participle ("ash-gray" -> ash, "boiled-leather" -> leather)."""
    words = [w.strip(",") for w in words]
    words = [w for w in words if w and w not in _STOP and "'" not in w and "’" not in w]
    coloured = [w for w in words if set(w.split("-")) & _COLOURS]
    for word in coloured[:1] + words[::-1]:
        # Of a coloured compound, the colour ("blood-red" -> red; a colour
        # is never a participle, though "red" ends in -ed). Otherwise "ill-
        # fitting" says how it is worn, not what sets it apart, and two
        # letters is a lowercased acronym ("MP uniform" read as "Mp-Uniformed")
        parts = ([p for p in word.split("-") if p in _COLOURS]
                 or [p for p in word.split("-") if len(p) > 2 and not p.endswith(("ed", "ing"))
                     and p not in _HOW_WORN])
        if parts:
            return parts[0]
    return ""


def _adjectives(words: list[str]) -> list[str]:
    """The describing words before a person noun, an adverb taken with the
    word it bends: "solidly built" and "hauntingly beautiful" say nothing
    alone ("Solidly Man" was a label), so both go."""
    out: list[str] = []
    skip = False
    for i, raw in enumerate(words):
        word = raw.strip(",")
        if skip:
            skip = False
            continue
        if not word or word in _STOP:
            continue
        if (word.endswith("ly") and word not in _LY_ADJECTIVES and word not in _AGE_WORDS
                and not raw.endswith(",") and i + 1 < len(words)):
            skip = True
            continue
        out.append(word)
    return out


def _look_words(npc: NPC) -> list[list[str]]:
    """A stranger as the eye reads them, most telling first: "A lean, menacing
    man in an ash-gray cloak" -> [ash-cloaked, man], then [lean, man]; "An
    old, weathered veteran" -> [old, veteran]."""
    text = " ".join(str(npc.description_physical or "").lower().split())
    if not text:
        return []
    out = []
    person = _PERSON_RE.search(text)
    noun = person.group(2) if person else "figure"
    garment = _GARMENT_RE.search(text)
    if garment:
        mod = _modifier(garment.group(1).split())
        dressed = _GARMENTS[garment.group(2)]
        out.append([f"{mod}-{dressed}" if mod else dressed, noun])
    if person:
        adjectives = _adjectives(person.group(1).split())
        aged = [w for w in adjectives if w in _AGE_WORDS]
        pick = aged[0] if aged else (adjectives[0] if adjectives else "")
        if pick:
            out.append([pick, noun])
        elif noun not in _BARE_NOUNS:
            out.append([noun])
    return out


_POSSESSIVE_END = re.compile(r"['’]s?$")


def _leaks_name(words: list[str], npc: NPC) -> bool:
    """A label must never carry the name: a word of it that is not also part
    of their trade ("Kelp Farmer" may stay for Kelp Farmer Finn). An epithet
    is part of the name ("Grey-Cloaked Figure" would hint at Gareth the
    Grey; 'Jax "The Shadow" Vane' keeps "Vane" too). A trade word in the
    possessive is someone's name, not a trade: "Edward Teller's Wife" told
    the player Mici Teller's surname."""
    trade = {t for t in _tokens(npc.profession or "") if not _POSSESSIVE_END.search(t)}
    said = {_POSSESSIVE_END.sub("", t) for w in words for t in _tokens(w)}
    name = {_POSSESSIVE_END.sub("", t) for t in _distinctive(npc.name)}
    return bool((said & name) - trade)


# A name that opens with one of these belongs to a person, whatever animal
# it mentions ("Commander Fox", 'Ser Androw "The Bull" Bulwer')
_PERSON_TITLES = frozenset({
    "ser", "sir", "lord", "lady", "captain", "commander", "sergeant", "general", "colonel", "major",
    "lieutenant", "corporal", "private", "admiral", "prince", "princess", "king", "queen", "duke",
    "duchess", "count", "countess", "baron", "baroness", "emperor", "empress", "dame", "master",
    "mistress", "mister", "mr", "mrs", "ms", "miss", "madam", "madame", "doctor", "dr", "professor",
    "inspector", "constable", "sheriff", "magistrate", "judge", "chancellor", "senator", "father",
    "mother", "brother", "sister", "uncle", "aunt", "granny", "maester", "archmaester", "septon",
    "septa", "abbot", "abbess", "bishop", "cardinal", "signor", "signora", "don", "goodwife",
})
_PERSON_WORDS = frozenset({"man", "woman", "boy", "girl", "lad", "lass", "gentleman", "knight", "soldier"})
_EPITHET = re.compile(r'"[^"]*"|“[^”]*”|\([^)]*\)|\[[^\]]*\]')


def is_beast(npc: NPC) -> bool:
    """An animal — it keeps its own name as its label and gets no talk chip.

    The sprite pipeline's test (`ImageGenerator._is_beast`) trusts an animal
    word anywhere in the name, which is right for "Hollow Wolf" but made
    animals of 'Ser Androw "The Bull" Bulwer', "Commander Fox" and "Robin
    (Tim Drake)" — and an animal's label is its name, so a villain called
    "Garrick the Wolf" would have been named to the player. Here an
    authored humanoid species ("Human (Andal)"; plain "human" is only the
    column default), a title, or a person word in the name says person;
    otherwise the pipeline's test runs on the name without its quoted or
    bracketed epithets, and an animal it finds by name is still a person
    when the description names a person before any animal ("Elias Fox: a
    wiry man..."). The description only ever rescues a person — "a bull
    of a man" must not make an animal of a commissioner.
    """
    try:
        from src.services.image_generator import ImageGenerator
        beasts, humanoid = ImageGenerator._BEAST_WORDS, ImageGenerator._HUMANOID_SPECIES
    except Exception:  # an asset import never decides who is a person
        return False
    species = " ".join(str(getattr(npc, "species", "") or "").lower().split())
    kinds = set(re.split(r"[^a-z]+", species)) - {""}
    if kinds & beasts:
        return True
    if kinds & humanoid and species != "human":
        return False
    bare = _EPITHET.sub(" ", str(npc.name or ""))
    words = _tokens(bare)
    if words and (words[0] in _PERSON_TITLES or set(words) & _PERSON_WORDS):
        return False
    if not ImageGenerator._is_beast(SimpleNamespace(
            name=bare, species=npc.species, description_physical=npc.description_physical)):
        return False
    looks = " ".join(str(npc.description_physical or "").lower().split())
    first = re.search(rf"(?<![\w'’-])({'|'.join(sorted(beasts | _PERSON_WORDS))})(?![\w'’-])", looks)
    return not (first and first.group(1) in _PERSON_WORDS)


def role_label(npc: NPC) -> str:
    """What the player calls someone whose name they have not learned.

    Short (1-3 words), Title Case, stable (it depends only on the NPC row,
    so the DM, the speaker resolver and the chips all agree on it turn after
    turn), and never the name. A beast keeps its own name — a wolf is a wolf.
    Covert people (a boss, or a first-named trade that is the secret) are
    read by their looks, everyone else by their trade first — the next one
    listed when the first would give a name away; "Stranger" when nothing
    says anything.
    """
    if is_beast(npc):
        return npc.name
    trades = _trade_options(npc)
    covert = (npc.danger_tier or "").strip().lower() == "boss" or bool(trades and _is_covert(trades[0]))
    public = [] if covert else [t for t in trades if not _is_covert(t)]
    for words in [*public, *_look_words(npc)]:
        if words and not _leaks_name(words, npc):
            return _title(words[-MAX_ROLE_WORDS:])
    return STRANGER


# fleet8 (all three runs): the stable hand was "she" in one reply and "he" in
# the next — Piper guessed before his dossier was in front of the DM, and a
# role label ("Stable Hand") carries no sex. Every person line the DM reads
# carries a short look with the pronoun, read off the authored description
_HE_NOUNS = frozenset({"man", "boy", "lad", "gentleman", "nobleman", "fisherman", "monk", "priest", "king",
                       "prince", "lord", "father", "brother", "son", "husband", "widower", "grandfather"})
_SHE_NOUNS = frozenset({"woman", "girl", "lass", "noblewoman", "matron", "widow", "priestess", "nun", "crone",
                        "hag", "lady", "queen", "princess", "mother", "sister", "daughter", "wife", "grandmother"})
_HE_WORDS = re.compile(r"\b(?:he|him|his|himself|he's|he'd|he'll)\b", re.I)
_SHE_WORDS = re.compile(r"\b(?:she|her|hers|herself|she's|she'd|she'll)\b", re.I)
_QUOTED_ANY = re.compile(r'"[^"]*"|“[^”]*”|\'[^\']{6,}\'')


def pronoun(npc: NPC) -> str:
    """'he', 'she', or '' when the row does not say: the head noun of their
    looks ("a friendly young man"), else the pronouns their own texts use
    (looks, manner, wants, secrets — never a quoted line, which speaks of
    others), and only when they agree."""
    looks = " ".join(str(npc.description_physical or "").lower().split())
    person = _PERSON_RE.search(looks)
    noun = person.group(2) if person else ""
    if noun in _HE_NOUNS:
        return "he"
    if noun in _SHE_NOUNS:
        return "she"
    texts = [npc.description_physical, npc.description_personality]
    for column in (npc.goals, npc.secrets):
        texts.extend(column if isinstance(column, list) else [column])
    bare = _QUOTED_ANY.sub(" ", " ".join(str(t) for t in texts if t))
    he, she = len(_HE_WORDS.findall(bare)), len(_SHE_WORDS.findall(bare))
    if he and not she:
        return "he"
    if she and not he:
        return "she"
    return ""


def look_tag(npc: NPC) -> str:
    """'young man, he' — what the eye reads first, and the pronoun to keep:
    a few words for every line the DM reads about a person. '' for a beast
    or a row that says nothing; never a word of the name."""
    try:
        if is_beast(npc):
            return ""
        looks = " ".join(str(npc.description_physical or "").lower().split())
        person = _PERSON_RE.search(looks)
        look = ""
        if person:
            noun = person.group(2)
            adjectives = _adjectives(person.group(1).split())
            aged = [w for w in adjectives if w in _AGE_WORDS]
            words = ([aged[0]] if aged else adjectives[:1]) + ([] if noun == "figure" else [noun])
            if words and not _leaks_name(words, npc):
                look = " ".join(words)
        return ", ".join(p for p in (look, pronoun(npc)) if p)
    except Exception as e:      # a look is never worth the brief
        logger.debug(f"no look for {getattr(npc, 'id', '?')}: {e}")
        return ""


# ----------------------------------------------------------------------
# Who knows whom
# ----------------------------------------------------------------------
# fleet6 (all three runs): one NPCRelationship row meant three things — the
# player knows this NPC's name (the gate unmasked whoever had a row), the two
# have a relationship (trust, the giver's thanks, @trust, the secret gates),
# and the NPC knows the PLAYER's name (the dossier said so). Each failed:
# Coldiron hired, passed and paid Bram as "Watch Captain" and never got a
# row, so the giver's thanks did nothing and `@trust Hesk Bramble -5` was
# refused as "still a stranger"; Piper said "Bram" (T19) and "Tamsin" (T15)
# because the row told the DM she knew it. Now the row is the relationship,
# written on the first real exchange, and two nullable columns hold the
# names: `name_known` (the player knows theirs) and `knows_player_name`
# (they know the player's). NULL is a row from before the split, and means
# both, as the row did then.

def name_known(rel: Any) -> bool:
    """By the row alone: does the player know this NPC's name?"""
    return rel is not None and getattr(rel, "name_known", None) is not False


def knows_player_name(rel: Any) -> bool:
    """By the row alone: does this NPC know the player's name?"""
    return rel is not None and getattr(rel, "knows_player_name", None) is not False


def name_known_clause():
    """`name_known` as a filter on NPCRelationship rows (NULL counts as known)."""
    from sqlalchemy import or_

    return or_(NPCRelationship.name_known.is_(None), NPCRelationship.name_known.is_(True))


def knows_player_name_clause():
    """`knows_player_name` as a filter on NPCRelationship rows (NULL counts as known)."""
    from sqlalchemy import or_

    return or_(NPCRelationship.knows_player_name.is_(None), NPCRelationship.knows_player_name.is_(True))


def relationship_row(session, player_id: str | None, npc_id: str | None) -> NPCRelationship | None:
    if not player_id or not npc_id:
        return None
    return session.query(NPCRelationship).filter(
        NPCRelationship.npc_id == npc_id, NPCRelationship.player_id == player_id
    ).first()


def ensure_relationship(session, player_id: str, npc_id: str, *, name_known: bool = False,
                        knows_player_name: bool = False, day: int | None = None) -> NPCRelationship:
    """The pair's row, written now if this is their first exchange.

    A new row starts neutral (trust 50 — every gate reads it) and records
    the names given here and what is already on file (`_names_on_file`):
    @trust and a giver's thanks write rows too, not only a talk, and a row
    that forgot what was known before it would say "no" where the heard
    names, the memory file and the audit all say yes. An existing row only
    ever learns (a True turns False into True; a pre-split NULL stays).
    Never commits.
    """
    rel = relationship_row(session, player_id, npc_id)
    if rel is None:
        on_file = _names_on_file(session, player_id, npc_id)
        name_known = name_known or on_file[0]
        knows_player_name = knows_player_name or on_file[1]
        rel = NPCRelationship(npc_id=npc_id, player_id=player_id, trust_level=50,
                              current_disposition="neutral", name_known=bool(name_known),
                              knows_player_name=bool(knows_player_name))
        session.add(rel)
    else:
        if name_known and rel.name_known is False:
            rel.name_known = True
        if knows_player_name and rel.knows_player_name is False:
            rel.knows_player_name = True
    if day is not None:
        rel.last_interaction_day = day
    return rel


def _names_on_file(session, player_id: str, npc_id: str) -> tuple[bool, bool]:
    """(the player has heard a word of their name, they know the player's)
    from before the pair had a row: the heard-names file; the player naming
    themself in an earlier exchange (`memory_files.player_named_themself`)
    or the name overheard (`overheard_player_name`). Never raises."""
    try:
        from src.services import memory_files

        npc = session.get(NPC, npc_id)
        name = npc is not None and _heard_name_of(session, player_id, npc)
        player = session.get(Player, player_id)
        theirs = player is not None and (memory_files.player_named_themself(player_id, npc_id, player.name)
                                         or overheard_player_name(player_id, npc_id))
        return name, bool(theirs)
    except Exception as e:
        logger.debug(f"names on file unreadable for {npc_id}: {e}")
        return False, False


def _names_known(session, player_id: str | None) -> set[str]:
    """Ids of the NPCs whose name this player knows by their row (the name
    gate's "met": a row with `name_known` false is still masked)."""
    if not player_id:
        return set()
    return {row[0] for row in session.query(NPCRelationship.npc_id).filter(
        NPCRelationship.player_id == player_id, name_known_clause()).all()}


def player_has_met(session, player_id: str | None, npc_id: str | None) -> bool:
    """Does the player know this NPC's NAME? (Not: have they spoken — that is
    `relationship_row`.) Their row says so (or predates the split), or a word
    of their name is among the names the player has heard said (`heard_names`:
    fleet6-wenna heard "Maera" from Piper at T12 and the brief still had the
    DM call her "the Keeper" through T18)."""
    if not player_id or not npc_id:
        return False
    if name_known(relationship_row(session, player_id, npc_id)):
        return True
    npc = session.get(NPC, npc_id)
    if npc is None:
        return False
    return _heard_name_of(session, player_id, npc)


def _heard_name_of(session, player_id: str, npc: NPC) -> bool:
    """Is a word of their name among the names the player has heard said —
    one that is not also a word of a place or faction? The heard-names file
    of every round-5 world holds "Wolf" and "Hollow", heard in "Wolf's
    Hollow" (the beast is called "Hollow Wolf"): a "Garrick Wolf" is not
    met by a walk to the Hollow."""
    heard = heard_names(player_id)
    hits = [w for w in name_words(npc.name) if w in heard] if heard else []
    if not hits:
        return False
    from src.models import Faction, Location

    places: set[str] = set()
    for (name,) in session.query(Location.name).all() + session.query(Faction.name).all():
        places |= {_POSSESSIVE_END.sub("", w) for w in re.findall(r"[A-Z][\w'’-]*", name or "")}
    return any(w not in places for w in hits)


def stranger_label(npc: NPC, *, met: bool) -> str | None:
    """The role label the player knows this person by — None once they have
    met, or when the label IS the name (a beast's)."""
    if met:
        return None
    label = role_label(npc)
    return None if _label_key(label) == _label_key(npc.name) else label


def display_name(session, player_id: str | None, npc: NPC) -> str:
    """The name the player may see for this NPC right now: their role label
    until introduced, then their name."""
    return stranger_label(npc, met=player_has_met(session, player_id, npc.id)) or npc.name


def _label_key(label: str) -> str:
    """Tokens without a leading article: "the Ash-Cloaked Man" == "Ash Cloaked Man"."""
    tokens = _tokens(label)
    while tokens and tokens[0] in ("the", "a", "an"):
        tokens = tokens[1:]
    return " ".join(tokens)


def is_role_label(label: str, npc: NPC) -> bool:
    """Is this speaker label the stranger's role label, not their name?

    A beast's role label IS its name, so a wolf is never a stranger.
    """
    key = _label_key(label)
    return bool(key) and key == _label_key(role_label(npc)) and key != _label_key(npc.name)


def _by_role(label: str, candidates: list[NPC]) -> NPC | None:
    key = _label_key(label)
    if not key:
        return None
    hits = {npc.id: npc for npc in candidates if _label_key(role_label(npc)) == key}
    # Two strangers who look alike: the line stays unattributed rather than
    # filed in the wrong one's memory
    return next(iter(hits.values())) if len(hits) == 1 else None


_TRADE_FILLER = frozenset({"the", "a", "an", "of", "old", "young"})
# A bare "Man:" or "Voice:" is anyone at all, not the one stranger whose
# look-label ends in it
_NOBODY_IN_PARTICULAR = _BARE_NOUNS | {"boy", "girl", "lad", "lass", "kid", "child", "youth", "person",
                                       "stranger", "someone", "voice", "people", "folk"}


# One trade, many words. fleet7-bram T20: "Barkeep" lines went out with no
# npc_id (no portrait, filed nowhere) while Maera, "Keeper of The Drowned
# Lantern", stood behind the bar; fleet7-tamsin T10: a nameless "Market
# Vendor" told the news with Hesk the hawker right there. The label's head
# word and the head of their trade name the same trade when they share a
# row here. Two-word trades are read as one ("stable hand", "tavern keeper").
_TRADE_SYNONYMS: dict[str, frozenset[str]] = {
    "keeper": frozenset({"keeper", "innkeeper", "innkeep", "tavernkeeper", "barkeep", "barkeeper", "bartender",
                         "barman", "barmaid", "publican", "landlord", "landlady", "alewife"}),
    "vendor": frozenset({"vendor", "hawker", "trader", "merchant", "peddler", "pedlar", "stallholder",
                         "stallkeeper", "seller", "shopkeeper", "shopkeep", "monger"}),
    "guard": frozenset({"guard", "guardsman", "guardswoman", "watchman", "watchwoman", "watch", "sentry",
                        "sentinel", "gatekeeper", "gateman"}),
    "stable": frozenset({"stablehand", "stableboy", "stablelad", "stablegirl", "stableman", "stablemaster",
                         "groom", "ostler", "hostler"}),
}
_TRADE_CLASS = {word: cls for cls, words in _TRADE_SYNONYMS.items() for word in words}


def _trade_compounds(words: list[str]) -> list[str]:
    """Two words that are one trade read as one: [stable, hand] -> [stablehand]."""
    out: list[str] = []
    for w in words:
        if out and out[-1] + w in _TRADE_CLASS and out[-1] not in _TRADE_CLASS:
            out[-1] += w
        else:
            out.append(w)
    return out


def _trade_heads(npc: NPC) -> set[str]:
    """The trade each of their trades names (`_TRADE_SYNONYMS`), read off the
    head word: a "Captain of the Lantern Watch" is a captain, not a guard."""
    heads = [_trade_compounds([w.lower() for w in words])[-1] for words in _trade_options(npc) if words]
    label = _trade_compounds(_tokens(role_label(npc)))
    if label:
        heads.append(label[-1])
    return {_TRADE_CLASS[h] for h in heads if h in _TRADE_CLASS}


def _by_trade(label: str, candidates: list[NPC]) -> NPC | None:
    """The one person here whose trade the label names, by a bare word of it.

    fleet6-wenna T13: "Keeper" lines went out with no npc_id (no portrait,
    filed nowhere) — her trade is "Keeper of The Drowned Lantern". A word
    counts when it is a word of their role label or profession, or one ends
    the other ("Keeper" for an innkeeper, "Innkeeper" for a keeper). Or the
    label's last word is a synonym of their trade's (`_TRADE_SYNONYMS`:
    "Barkeep" for a keeper, "Market Vendor" for a hawker) and its other
    words only say where or how — none is a person or another trade. Two
    who share the trade: nobody.
    """
    words = [w for w in _tokens(label) if w not in _TRADE_FILLER]
    if not words or set(words) <= _NOBODY_IN_PARTICULAR:
        return None
    joined = _trade_compounds(words)
    wanted = _TRADE_CLASS.get(joined[-1])
    if wanted and any(w in _NOBODY_IN_PARTICULAR or w in _PERSON_NOUNS or
                      _TRADE_CLASS.get(w, wanted) != wanted for w in joined[:-1]):
        wanted = None
    hits = []
    for npc in candidates:
        if is_beast(npc):
            continue
        heads = _trade_heads(npc)
        trade = {_POSSESSIVE_END.sub("", t) for t in _tokens(role_label(npc)) + _tokens(npc.profession or "")}
        trade = {t for t in trade if len(t) >= 4} | (trade & set(words))
        if wanted and heads and wanted not in heads and not all(w in trade for w in words):
            # The table knows both trades and they differ: a "Gatekeeper" is
            # no innkeeper, though one word ends the other — and a
            # "Shopkeeper" beside the hawker is him, never also the keeper.
            # Their own word still is them ("Keeper" for the Gate Keeper).
            continue
        if all(w in trade or (len(w) >= 4 and any(t.endswith(w) or w.endswith(t) for t in trade))
               for w in words) or (wanted and wanted in heads):
            hits.append(npc)
    return hits[0] if len(hits) == 1 else None


def resolve_speaker(session, label: str, present_ids: list[str] | None = None,
                    party_ids: list[str] | None = None) -> NPC | None:
    """The NPC a speaker label names, or None for an ambient voice.

    In the room (and the party) a role word is enough — "Guard" is Hilda the
    Guard when she is standing there. Across the world it is not: an
    ambient guard at the gate must not speak in the voice of, be filed in
    the memory of, or tick a talk_to objective for a Hilda two towns away.

    A stranger in the room answers to their exact role label ("Ash-Cloaked
    Man" is Vex Marrow until he gives his name). That match comes after an
    exact name and before any partial-word match, or "Ash-Cloaked Man"
    would find a Mirelle Ash by the word "ash".
    """
    present_ids = list(present_ids or [])
    party_ids = list(party_ids or [])
    nearby = []
    if present_ids:
        nearby.append(session.query(NPC).filter(NPC.id.in_(present_ids)).all())
    if party_ids:
        nearby.append(session.query(NPC).filter(NPC.id.in_(party_ids)).all())
    low = " ".join(_tokens(label))
    everyone_near = [npc for tier in nearby for npc in tier]
    exact = next((npc for npc in everyone_near if low and " ".join(_tokens(npc.name)) == low), None)
    if exact is not None:
        return exact
    stranger = _by_role(label, everyone_near)
    if stranger is not None:
        return stranger
    for candidates in nearby:
        npc = _match(label, candidates)
        if npc is not None:
            return npc
    for candidates in nearby:
        npc = _by_trade(label, candidates)
        if npc is not None:
            return npc
    alive = session.query(NPC).filter(NPC.status == "alive").all()
    return _match(label, alive, epithets=False, overlap=False)


def is_known_speaker(label: str, present_ids: list[str] | None = None) -> bool:
    """For unquoted `Name: words` lines — only real people count."""
    try:
        with get_session() as session:
            return resolve_speaker(session, label, present_ids) is not None
    except Exception:
        return False


# ----------------------------------------------------------------------
# A name in use, borrowed
# ----------------------------------------------------------------------
# fleet10-wenna T13-T14: the Captain said "Say it plain, Hesk" to a
# mud-caked cart driver the DM had just made up, and the "Hesk:" lines
# resolved across the world to Hesk Bramble — the market's hawker, the lead
# of a beat still to come. The engine walked him to the Gatehouse to match
# the story (`server._bring_speakers_here`) and filed the driver's testimony
# in his memory. A label naming someone who is not there is that person
# only when the engine can believe they came: never someone the story still
# needs where they are (story cast), a merchant at their counter, or anyone
# the player saw somewhere else this game-day (`kept_in_place`) — unless the
# label is their whole name, the player saw them HERE earlier today (the
# database is behind), or they stood where the reply began (a goodbye, a
# walk along). Such a line goes out under the role the prose gave the
# speaker (`role_from_prose`), in nobody's memory, and the DM is told to
# name them (turn_audit.name_in_use_borrowed_warning).

_SEEN_LOCK = threading.RLock()
# player id -> {npc id: (game day, location id)}: where the player last saw them
_SEEN: dict[str, dict[str, tuple[int, str]]] = {}


def _today(session) -> int:
    clock = session.query(WorldClock).first()
    return int(clock.day) if clock is not None and clock.day is not None else 1


def note_seen(player_id: str | None, location_id: str | None, npc_ids, day: int | None = None) -> None:
    """The player saw these people at this place (the brief's people as a
    reply begins; each speaker where they spoke). Never raises."""
    if not player_id or not location_id:
        return
    try:
        if day is None:
            with get_session() as session:
                day = _today(session)
        with _SEEN_LOCK:
            seen = _SEEN.setdefault(player_id, {})
            for npc_id in npc_ids or ():
                if npc_id:
                    seen[npc_id] = (int(day), location_id)
    except Exception as e:
        logger.debug(f"seen bookkeeping failed: {e}")


def note_seen_reply(player_id: str | None, spoken: list[dict[str, Any]] | None) -> None:
    """After a reply (and the engine's moves after it): each speaker where
    they spoke, then everyone where the player now stands. Never raises."""
    if not player_id:
        return
    try:
        with get_session() as session:
            player = session.get(Player, player_id)
            if player is None or not player.current_location_id:
                return
            day = _today(session)
            here = player.current_location_id
            ids = [row[0] for row in session.query(NPC.id).filter(
                NPC.current_location_id == here, NPC.status == "alive").all()]
        for note in spoken or []:
            if isinstance(note, dict) and note.get("npc_id") and note.get("_where"):
                note_seen(player_id, note["_where"], [note["npc_id"]], day)
        note_seen(player_id, here, ids, day)
    except Exception as e:
        logger.debug(f"seen bookkeeping after the reply failed: {e}")


def seen_at(player_id: str | None, npc_id: str | None) -> tuple[int, str] | None:
    """(game day, location id) where the player last saw this person, or None."""
    with _SEEN_LOCK:
        return (_SEEN.get(player_id or "") or {}).get(npc_id or "")


def forget_seen(player_id: str | None = None) -> None:
    """Tests and world switches."""
    with _SEEN_LOCK:
        if player_id is None:
            _SEEN.clear()
            _BORROWED.clear()
        else:
            _SEEN.pop(player_id, None)
            _BORROWED.pop(player_id, None)


# player id -> {(label words, location id): (game day, role)}: a label that
# went out as a borrowed name here today, and the role it was shown under —
# the driver's next "Hesk:" lines (fleet10-wenna T15-T16) are his again, under
# "Driver", though the prose before them names no trade
_BORROWED: dict[str, dict[tuple[str, str], tuple[int, str]]] = {}


def note_borrowed(player_id: str | None, label: str, location_id: str | None, role: str,
                  day: int | None = None) -> None:
    """This label was a name in use, borrowed here (shown as `role`)."""
    if not player_id or not location_id or not role:
        return
    try:
        if day is None:
            with get_session() as session:
                day = _today(session)
        with _SEEN_LOCK:
            _BORROWED.setdefault(player_id, {})[(" ".join(_tokens(label)), location_id)] = (int(day), role)
    except Exception as e:
        logger.debug(f"borrowed-label bookkeeping failed: {e}")


def borrowed_role(session, player_id: str | None, label: str, location_id: str | None) -> str | None:
    """The role this label went out under when it was borrowed here today, or None."""
    with _SEEN_LOCK:
        found = (_BORROWED.get(player_id or "") or {}).get((" ".join(_tokens(label)), location_id or ""))
    return found[1] if found and found[0] == _today(session) else None


def is_merchant(npc: NPC) -> bool:
    """Someone who keeps a counter: a selling or innkeeping trade, or a purse
    and wares (the shop's own test: stock that is no quest item)."""
    try:
        if is_beast(npc):
            return False
        if _trade_heads(npc) & {"vendor", "keeper"}:
            return True
    except Exception:  # a malformed row keeps no counter
        return False
    wares = [e for e in npc.inventory_notable or [] if isinstance(e, dict) and e.get("type") != "quest_item"]
    return npc.currency is not None and bool(wares)


def kept_in_place(session, player_id: str | None, npc: NPC, here_id: str | None) -> str | None:
    """Why the engine must not move this person to where a line of theirs
    was said, or None: the story needs them where they are, they keep a
    counter, or the player saw them somewhere else this game-day."""
    try:
        from src.services.campaign import story_cast_ids

        if npc.id in story_cast_ids(session, player_id):
            return "the story still needs them where they are"
    except Exception as e:
        logger.debug(f"story cast unreadable: {e}")
    if is_merchant(npc):
        return "they keep a counter there"
    seen = seen_at(player_id, npc.id)
    if seen and seen[0] == _today(session) and seen[1] != here_id:
        return "the player saw them there earlier today"
    return None


def _shown_as_them(npc: NPC, before: str, label: str) -> bool:
    """Did the prose before the line show THIS person — by their whole name,
    a word of their name the label did not use, or a word of their trade or
    role label ("the hawker" for Hesk Bramble)? Then the DM brought them."""
    text = (before or "").lower().replace("’", "'")
    if not text.strip():
        return False
    said = set(_tokens(label))
    words = {w for w in _distinctive(npc.name) if w not in said}
    try:
        words |= {w for w in _tokens(role_label(npc)) if w not in _NOBODY_IN_PARTICULAR}
        trades = _trade_options(npc)            # the first trade's head; the rest may be wares
        words |= set(_tokens(trades[0][-1])) if trades and trades[0] else set()
    except Exception:  # a row that cannot be labelled shows by name only
        pass
    words = {w for w in words if len(w) >= 4 and w not in _TITLE_WORDS and w not in _PLAIN_LOOKS}
    return (npc.name or "").lower() in text or any(
        re.search(rf"(?<![\w'-]){re.escape(w)}(?![\w-])", text) for w in words)


def borrowed_speaker(session, player_id: str | None, npc: NPC | None, label: str, present_ids,
                     here_id: str | None, left_from: str | None = None, before: str = "") -> str | None:
    """Why a speaker label that resolved to this ABSENT person is a name in
    use, borrowed for somebody else (see above) — else None. `before`: the
    reply's prose up to the line."""
    if npc is None or not player_id or not here_id or npc.id in set(present_ids or ()):
        return None
    if npc.current_location_id in (None, here_id) or (npc.status or "alive") != "alive":
        return None
    if " ".join(_tokens(label)) == " ".join(_tokens(npc.name)):
        return None                     # their whole name: the DM means them
    if left_from and left_from != here_id and npc.current_location_id == left_from:
        return None                     # where the reply began: a goodbye, or they came along
    if npc.home_location_id == here_id or _shown_as_them(npc, before, label):
        return None                     # back where they live, or the prose brought THEM
    try:
        from src.services import party as party_mod

        player = session.get(Player, player_id)
        if player is not None and npc.id in set(party_mod.member_ids(player)):
            return None
    except Exception as e:
        logger.debug(f"party unreadable: {e}")
    seen = seen_at(player_id, npc.id)
    if seen and seen[0] == _today(session) and seen[1] == here_id:
        return None                     # at the player's side here today: the database is behind
    why = kept_in_place(session, player_id, npc, here_id)
    # ...and only when someone new is speaking: the prose shows a trade or a
    # look of theirs ("The driver — wiry, mud to the knees"), or this label
    # was borrowed here already today. A known name with nobody new in the
    # prose ("Piper hurries up, breathless", no @npc-move) is that person:
    # shown as "Stranger" and told to @new-npc, the DM would make up a
    # second Piper (review probe) — speaker_not_here asks for the move
    if why and borrowed_role(session, player_id, label, here_id) is None:
        people = session.query(NPC).filter(NPC.id.in_(list(present_ids or []))).all() if present_ids else []
        if role_from_prose(before, people) == STRANGER:
            return None
    return why


# Who the prose said was speaking, for a line that is nobody's yet: a trade
# or a look the reply gave them ("The driver — wiry, mud to the knees")
_SCENE_ROLES = tuple(sorted(set(_PERSON_NOUNS) | {
    "driver", "carter", "drover", "teamster", "wagoner", "runner", "rider", "messenger", "courier", "guard",
    "watchman", "sentry", "farmer", "herder", "shepherd", "trapper", "woodcutter", "miller", "porter",
    "labourer", "laborer", "boatman", "ferryman", "pilgrim", "peddler", "tinker", "drunk", "lad", "lass",
    "stranger", "survivor", "witness", "refugee", "townsman", "townswoman", "villager", "local",
}, key=len, reverse=True))
_ROLE_IN_PROSE = re.compile(
    rf"\b(?:[Tt]he|[Aa]n?|[Tt]his|[Tt]hat)\s+((?:[a-z][a-z'’-]*,?\s+){{0,2}}?)({'|'.join(_SCENE_ROLES)})\b")


def role_from_prose(before: str, people: list | tuple = ()) -> str:
    """What the prose before a line called a speaker who is nobody yet: the
    last person it named by trade or look ("Driver"; "Hunched Man" for "a
    hunched man"), never a trade of the `people` there (the Captain is his
    own); "Stranger" when it named nobody."""
    taken: set[str] = set()
    for npc in people or ():
        try:
            taken |= set(_tokens(role_label(npc)))
        except Exception:  # a row that cannot be labelled takes no word
            continue
    best: list[str] | None = None
    for m in _ROLE_IN_PROSE.finditer(before or ""):
        noun = m.group(2).lower()
        if noun in taken:
            continue
        looks = [w.strip(",") for w in m.group(1).split() if w.strip(",").lower() not in _STOP]
        best = (looks[-1:] if noun in _BARE_NOUNS else []) + [noun]
    return _title(best) if best else STRANGER


def record_turn_dialogue(player_id: str, player_input: str, speeches: list[dict[str, Any]],
                         addressed_npc_id: str | None = None,
                         started_quest_ids: list[str] | tuple = (), *,
                         left_from: str | None = None) -> list[str]:
    """Persist this turn's dialogue to each speaking NPC's memory.

    `speeches` are resolved speech notifications ({npc_id, npc_name, text,
    label?}). Everyone in the exchange — who spoke, or was spoken to — gets
    their relationship row now (fleet6: the row is the relationship, not an
    introduction). Whether the player learned their NAME is the row's
    `name_known`: `label` is the speaker label the DM wrote (`npc_name`
    stands in when it is absent); a stranger who spoke only under their role
    label ("Ash-Cloaked Man") is remembered — the exchange goes in their
    file, the row is written — but their name stays unknown; the first line
    labelled with their name is the introduction — but only once that name
    has been SAID where the player could hear it (this line, an earlier line
    or the player's words this turn, or anything they heard before:
    `heard_corpus`). fleet4: labels flipped to "Aldric Vane", "Piper
    Quillow", "Maera Duskwell" with nobody saying a word of it. An
    `introduces` key is a caller that already judged (the server's
    `NameGate`, which also hears quotes in the prose). A speech with neither
    key is a caller vouching for the id (the legacy shape) and introduces as
    before. Being addressed while silent introduces nobody. A new row knows
    the player's name only if the player said it to them in an earlier
    exchange (`memory_files.player_named_themself`) or they overheard it
    (`note_player_name_heard`, which also handles this turn's saying).

    The player's words are filed with the NPC they were addressed to, or
    with the first named NPC who answered. Returns `objective_done` lines
    for any `talk_to` objective this conversation completed — only for
    someone the player talked TO (`_talked_to`), not someone who spoke up,
    and not on a quest taken in this reply (`started_quest_ids`). `left_from`:
    where the player stood when the reply began — when its @move took them
    elsewhere, what they said before setting out was said there.
    """
    from src.services import memory_files

    by_npc: dict[str, list[str]] = {}
    # (label, verdict, everything said up to and including that line);
    # verdict None = an explicit label whose name must have been heard
    labels: dict[str, list[tuple[str, bool | None, str]]] = {}
    order: list[str] = []
    said_so_far = [_player_words(player_input)]
    for s in speeches or []:
        if not isinstance(s, dict):
            continue
        npc_id = s.get("npc_id")
        text = str(s.get("text") or "").strip()
        if not npc_id or not isinstance(npc_id, str) or not text:
            continue
        if npc_id not in by_npc:
            by_npc[npc_id] = []
            labels[npc_id] = []
            order.append(npc_id)
        by_npc[npc_id].append(text)
        said_so_far.append(text)
        verdict = bool(s["introduces"]) if "introduces" in s else (None if "label" in s else True)
        labels[npc_id].append((str(s.get("label") or s.get("npc_name") or ""), verdict, "\n".join(said_so_far)))

    if addressed_npc_id and addressed_npc_id not in by_npc:
        # Spoken to, said nothing (a cold shoulder still happened)
        by_npc[addressed_npc_id] = []
        order.insert(0, addressed_npc_id)

    words_to = addressed_npc_id or (order[0] if order else None)
    done_lines: list[str] = []
    clean_input = _player_words(player_input)
    # Nothing said either way (a directive turn aimed at someone silent) is
    # not a conversation
    order = [n for n in order if by_npc[n] or (n == words_to and clean_input)]
    if not order or not player_id:
        return done_lines

    stamp, place = "", ""
    corpus: list[str] = []          # what they heard before this turn, read once if needed
    talked: set[str] = set()        # who the player talked TO: their talk_to steps tick

    def heard_before() -> str:
        if not corpus:
            corpus.append(heard_corpus(player_id))
        return corpus[0]

    try:
        with get_session() as session:
            clock = session.query(WorldClock).first()
            stamp = memory_files.format_stamp(clock)
            day = clock.day if clock is not None else 1
            player = session.get(Player, player_id)
            if player is not None and player.current_location is not None:
                place = player.current_location.name
            nearby = session.query(NPC).filter(
                NPC.current_location_id == player.current_location_id, NPC.status == "alive"
            ).all() if player is not None and player.current_location_id else []
            speakers = {n for n in order if by_npc[n]}
            here_ids = {o.id for o in nearby}
            # The reply's @move took the player elsewhere: what they said
            # before setting out was said where they were (fleet10-tamsin T6)
            moved = bool(left_from and player is not None and player.current_location_id
                         and left_from != player.current_location_id)
            said = str(player_input or "").lower()
            away = [o for o in session.query(NPC).filter(NPC.status == "alive").all()
                    if o.id not in here_ids and any(f.lower() in said for f in _address_forms(o))]
            real = []
            for npc_id in order:
                npc = session.get(NPC, npc_id)
                if npc is None:
                    continue
                real.append(npc_id)
                others = [o for o in nearby if o.id != npc_id]
                if _talked_to(npc, player_input, speakers, others, addressed_npc_id, here=npc_id in here_ids,
                              elsewhere=[o for o in away if o.id != npc_id],
                              moved=moved and npc.current_location_id != left_from):
                    talked.add(npc_id)
                rel = relationship_row(session, player_id, npc_id)
                introduced = name_known(rel) or _introduced(npc, labels.get(npc_id, []), heard_before)
                # The gates read trust from this row: a stranger starts neutral
                # (and a new row starts from the names on file)
                ensure_relationship(session, player_id, npc_id, name_known=introduced, day=day)
            session.commit()
            order = real
    except Exception as e:
        logger.warning(f"relationship rows not written for {order}: {e}")

    for npc_id in order:
        if not memory_files.has_history(player_id, npc_id):
            memory_files.add_npc_note(player_id, npc_id, f"First met{' at ' + place if place else ''}.")
        memory_files.append_exchange(
            player_id, npc_id,
            clean_input if npc_id == words_to else "",
            by_npc[npc_id],
            stamp,
        )
        if npc_id not in talked:
            continue
        try:
            from src.services import objectives as objectives_mod

            ticked = objectives_mod.note_conversation(player_id, npc_id, started_quest_ids)
            if ticked:
                done_lines.extend(objectives_mod.done_lines(ticked))
        except Exception as e:
            logger.debug(f"talk_to tick skipped for {npc_id}: {e}")
    return done_lines


def _player_words(player_input: str) -> str:
    """What the player actually said — never an engine directive."""
    text = str(player_input or "").strip()
    if text.startswith("["):
        return ""
    return text[:500]


def _introduced(npc: NPC, claims: list[tuple[str, bool | None, str]], heard_before) -> bool:
    """Did one of these lines introduce a stranger? A role-labelled line
    never does; a caller's verdict stands; an explicit label must carry a
    word of their name that has been said (`said` this turn, or before)."""
    for label, verdict, said in claims:
        if label and is_role_label(label, npc):
            continue
        if verdict is None:
            words = name_words(npc.name)
            in_label = {t for t in _tokens(label)}
            verdict = (not label or stranger_label(npc, met=False) is None
                       or (any(w.lower() in in_label for w in words)
                           and any(_said(w, said) or _said(w, heard_before()) for w in words)))
        if verdict:
            return True
    return False


# ----------------------------------------------------------------------
# Who the player talked to
# ----------------------------------------------------------------------
# fleet5-wenna T6: "Ask Sorrel Tack what the stable yard hears after dark"
# ticked on Sorrel's greeting ("you're new, aren't you?") while the player
# was reading the notice board and saying, to Piper, "I'll find Sorrel at the
# stables first" — the question came at T7. A talk_to step is earned by
# talking TO someone: the talk chip or a click on them (`addressed_npc_id`),
# their name or role said to them ("Sorrel? Piper mentioned you…", "…a
# message, Brann."), an act aimed at them ("I approach the herbalist's
# apprentice", "I hand Sorrel the coin", "meeting Sorrel's eyes"), or words
# to nobody in particular that only they answered — unless the player's own
# words were ABOUT them, to someone else. Replayed over every playtest: each
# tick this drops was an NPC speaking first, and came back within a turn.

_SPOKE = re.compile(
    r"[\"“]|\bI\s+(?:\w+ly\s+)?(?:ask|say|tell|call|shout|whisper|murmur|mutter|reply|answer|greet|hail|"
    r"question|press|add|explain|offer)\b", re.I,
)
# Done now, by the player: "I approach…", "…and hand Sorrel the coin", "head
# downstairs to ask the keeper" — not "what I'll tell Piper first" on the
# walk back (fleet5-tamsin T8) or "I'm going to ask Sorrel"
_ADDRESS_NOW = (r"(?:\bI\s+|\b(?:and|then)\s+|^|[.!?;,]\s+|(?<!going )(?<!want )(?<!need )(?<!have )\bto\s+)"
                r"(?:\w+ly\s+)?")
_ADDRESS_VERBS = (
    r"approach(?:es)?|ask|tell|greet|hail|question|thank|address|hand|give|show|offer|"
    r"(?:call|calls)(?:\s+out)?\s+to|turns?(?:\s+back)?\s+to|"
    r"(?:speak|talk|say|whisper|nod|wave|walk|go|head|step|move|lean|kneel|stride|cross|hurry|run|turn|"
    r"drift|edge|sidle|wander|saunter|amble|limp|stumble|slip|sit|crouch)s?"
    r"(?:\s+(?:over|up|back|straight|right|down|\w+ly))?\s+(?:to|toward|towards|at|beside)"
)
# A look is an address only when words follow it: "I glance at Sorrel. 'Come
# on then…'" (fleet6-bram T5), "I look at Sorrel hard. 'That fourth howl…'" —
# not "I glance at Sorrel, then back to the Captain" (fleet6-wenna T10)
_GLANCE = r"(?:glance|look)s?(?:\s+(?:over|up|back|straight|across|\w+ly))?\s+(?:at|to|toward|towards)"
_THEN_SPEAKS = r"(?:\s+[a-z]+)?\s*[.,:;!—–-]?\s*[\"“]"
_ADDRESS_FILLER = r"(?:(?:the|a|an|old|young|back|over|up|right|straight|directly|quietly|softly|gently)\s+){0,3}"
_LOOKS_AT = r"\b(?:meet|meets|meeting|catch|catches|catching|hold|holds|holding|study|studies|studying)\s+"
_VOCATIVE_BEFORE = (r"(?:^|[,;:!?.…—–-]\s*|\b(?:hey|oi|ho|hello|evening|morning|thanks|listen|look|please|but|"
                    r"and|so|oh|now|well|right|okay|ok|yes|no|sorry)[,!]?\s+)")
_VOCATIVE_TITLE = rf"(?:the\s+|(?:{'|'.join(sorted(_PERSON_TITLES, key=len, reverse=True))})\.?\s+)?"
_QUOTED = re.compile(r'"([^"]*)"|“([^”]*)”')


def _form_re(form: str) -> str:
    return r"\s+".join(re.sub(r"['’]", "['’]", re.escape(w)) for w in form.split())


def _address_forms(npc) -> list[str]:
    """What the player could call them: a word of their name, a title in it
    ("Captain"), their role label ("Herbalist's Apprentice") and its title."""
    forms = list(name_words(npc.name))
    forms += [w.strip(".,") for w in str(npc.name or "").split() if w.lower().strip(".,") in _PERSON_TITLES]
    label = stranger_label(npc, met=False)
    if label:
        forms.append(label)
        forms += [w for w in label.split() if w.lower() in _PERSON_TITLES]
    return [f for f in dict.fromkeys(forms) if f]


def _quotes(text: str) -> list[str]:
    return [m.group(1) if m.group(1) is not None else m.group(2) for m in _QUOTED.finditer(text)]


# fleet6-tamsin T5: the player asked "the straw-haired boy" when the yard goes
# quiet and Sorrel answered — no name, no role, so the step waited for a
# goodbye at T7. Someone standing there who answered may be picked out by
# their looks (a word of their description: "straw-colored hair") or the
# head word of their trade ("the apprentice").
_LOOK_NOUNS = tuple(sorted(set(_PERSON_NOUNS) | {"lad", "lass", "kid", "fellow", "one", "lady", "gent", "keeper"},
                           key=len, reverse=True))
_LOOK_PHRASE = re.compile(
    rf"\b(?:the|that|this)\s+((?:[a-z][a-z'’-]*,?\s+){{1,2}}?)({'|'.join(_LOOK_NOUNS)})\b", re.I)
# Words any description has, which pick nobody out
_PLAIN_LOOKS = frozenset({
    "young", "old", "older", "elderly", "tall", "short", "small", "large", "big", "little", "thin", "lean",
    "broad", "with", "from", "that", "this", "their", "them", "over", "under", "into", "like", "wearing",
    "worn", "eyes", "eyed", "hair", "haired", "face", "faced", "hand", "hands", "skin", "dark", "light",
    "pale", "woman", "girl", "both", "other", "some", "same", "each", "very", "much", "more", "most",
    "middle", "aged", "looking", "kind", "sort", "person", "people", "fellow",
})


def _describing_words(text: str) -> set[str]:
    return {p for p in re.split(r"[^a-z]+", (text or "").lower()) if len(p) >= 4} - _PLAIN_LOOKS


def _look_forms(npc, text: str, others: list) -> list[str]:
    """Phrases in the player's words that pick out `npc` (present, and
    answering) by looks or trade — "the straw-haired boy" for "a young man
    all elbows and straw-colored hair", "the apprentice" — and nobody else
    standing there."""
    forms: list[str] = []
    label = stranger_label(npc, met=False)
    head = label.split()[-1] if label else ""
    if len(head) >= 5 and head.lower() not in _NOBODY_IN_PARTICULAR:
        forms.append(head)
    seen = _describing_words(npc.description_physical)
    if not seen:
        return forms
    theirs = set().union(*(_describing_words(getattr(o, "description_physical", "")) for o in others)) if others else set()
    for m in _LOOK_PHRASE.finditer(_QUOTED.sub(" ", text)):
        words = _describing_words(m.group(1))
        if words & seen and not words & theirs:
            forms.append(" ".join(f"{m.group(1)} {m.group(2)}".split()))
    return forms


def _addresses(npc, text: str, extra_forms: list[str] | tuple = ()) -> bool:
    """`text` (the player's input) speaks to `npc` or acts on them by name or role."""
    return _address_span(npc, text, extra_forms) is not None


def _blank_quotes(text: str) -> str:
    """`text` with each quote blanked in place: positions still line up."""
    return _QUOTED.sub(lambda m: " " * len(m.group(0)), text)


def _address_span(npc, text: str, extra_forms: list[str] | tuple = ()) -> tuple[int, int] | None:
    """Where in `text` the player first speaks to or acts on `npc`, or None."""
    spans = [(m.start(), m.end()) for m in _QUOTED.finditer(text)]
    outside = _blank_quotes(text)
    found: list[tuple[int, int]] = []
    for form in [*_address_forms(npc), *extra_forms]:
        f = _form_re(form)
        for start, end in spans:
            if re.search(rf"{_VOCATIVE_BEFORE}{_VOCATIVE_TITLE}{f}\s*(?:[,;:!?.…—–-]|$)",
                         text[start + 1:end - 1].strip(), re.I):
                found.append((start, end))
                break
        for pattern, where in (
            (rf"{_ADDRESS_NOW}(?:{_ADDRESS_VERBS})\s+{_ADDRESS_FILLER}{f}(?![\w'’-])", outside),
            (rf"{_LOOKS_AT}{_ADDRESS_FILLER}{f}['’]s?\s+(?:eyes?|gaze|face)", outside),
            # the quote it waits for is in `text`, so the look is read there
            (rf"{_ADDRESS_NOW}{_GLANCE}\s+{_ADDRESS_FILLER}{f}(?={_THEN_SPEAKS})", text),
        ):
            m = re.search(pattern, where, re.I | re.M)
            if m:
                found.append((m.start(), m.end()))
    return min(found) if found else None


# fleet6-tamsin T7: "Ask Sorrel Tack what the stable yard hears after dark"
# ticked on "I turn toward Sorrel with a nod. 'Meet you at the stable yard in
# a few minutes…'" — a parting, not the asking. Words to them that open with
# a goodbye and ask nothing are no conversation for a talk_to step.
_FAREWELL = re.compile(
    r"^\s*(?:(?:well|right|okay|ok|alright|all\s+right|then|so|now)[,!.]?\s+)?"
    # "Later." is a goodbye; "Later that night it howled again" is the report,
    # as "Take care of the horse" and "See you're busy" are no partings
    r"(?:meet\s+you|see\s+you(?!['’])|i['’]ll\s+see\s+you|later(?:,?\s+then)?(?=\s*(?:[.!…—–-]|$))|"
    r"good\s*-?\s*bye|bye|farewell|take\s+care(?!\s+of)|"
    r"until\s+(?:tomorrow|tonight|then|later|morning|dawn)|good\s*-?\s*night|safe\s+(?:roads|travels)|"
    r"be\s+seeing\s+you|catch\s+you|i['’]ll\s+be\s+back|i['’]ll\s+come\s+(?:back|find\s+you)|"
    r"i['’]ll\s+find\s+you|don['’]t\s+wait\s+up|stay\s+safe|keep\s+safe|mind\s+yourself)\b",
    re.I,
)


def _farewell_to(npc, text: str, extra_forms: list[str] | tuple = ()) -> bool:
    """Are the player's words to `npc` only a parting — the first opening
    with a goodbye, none asking anything?

    Their words are the quotes from where the player turns to them until
    they turn to someone else; the quotes before count too unless the player
    was speaking to someone else first ("'You've got my word,' I tell the
    girl… I turn toward Sorrel. 'Meet you…'"). A goodbye after a real talk
    ("'Dyer,' I repeat… 'I'll be back with answers, Captain'", fleet3-bram
    T6) is still a talk.
    """
    quotes = list(_QUOTED.finditer(text))
    if not quotes:
        return False
    span = _address_span(npc, text, extra_forms)
    turns = [m.start() for m in re.finditer(rf"{_ADDRESS_NOW}(?:{_ADDRESS_VERBS}|{_GLANCE})\s+",
                                            _blank_quotes(text), re.I | re.M)]
    start, stop = 0, len(text)
    if span is not None:
        if any(t < span[0] for t in turns):
            start = span[0]
        later = [t for t in turns if t >= span[1]]
        stop = later[0] if later else len(text)
    said = [m.group(1) if m.group(1) is not None else m.group(2)
            for m in quotes if start <= m.start() < stop]
    return bool(said) and bool(_FAREWELL.search(said[0])) and not any("?" in s for s in said)


def _talks_about(npc, text: str) -> bool:
    """The player's own words name them — said to someone else ("I'll find
    Sorrel at the stables first", fleet5-wenna T6). The narration naming
    them ("Coldiron kept his word") is no sign either way, nor is a kind of
    person ("A herbalist's apprentice who pays attention…", to her face)."""
    return any(re.search(rf"(?<![\w'’-])(?<!\ba )(?<!\ban ){_form_re(f)}(?![\w-])", q, re.I)
               for q in _quotes(text) for f in _address_forms(npc))


# The player setting out, in their own narration: what they said before it
# was said where they stood (fleet10-tamsin T6: "And Piper — keep counting
# those gaps" in the square, then "I head toward the Stable Yard")
_SETS_OUT = re.compile(
    r"\bI\s+(?:\w+ly\s+)?(?:head|heads|walk|go|make\s+my\s+way|set\s+(?:off|out)|cross|hurry|move|step\s+out"
    r"|leave|return|ride|run|jog|climb|stride|wander|start\s+(?:off|back|toward|towards))\b", re.I)


def _after_setting_out(text: str) -> str:
    """The player's input from where they set out on: their words before
    it were said where they left. All of it when they never say so."""
    m = _SETS_OUT.search(_blank_quotes(text))
    return text[m.end():] if m else text


def _talked_to(npc, player_input: str, speakers: set[str], others: list, addressed_npc_id: str | None,
               *, here: bool = False, elsewhere: list | tuple = (), moved: bool = False) -> bool:
    """Did the player talk TO this NPC this turn (so a talk_to step on them ticks)?

    `here`: they stand where the player is, so if they answered, their looks
    or trade word name them too (`_look_forms`). fleet10-tamsin T6 ticked
    "Ask Sorrel what the stable yard hears after dark" on arrival: Sorrel
    only greeted her, and her one quote was to Piper, back in the square
    ("And Piper — keep counting those gaps"). Words addressed to anyone else
    — standing here, or `elsewhere` — are no talk to the one who answered,
    and when the reply's @move took the player to them (`moved`), only what
    the player said after setting out counts (`_after_setting_out`)."""
    if npc.id == addressed_npc_id:
        return True
    text = str(player_input or "").strip()
    if not text or text.startswith("["):
        return False                       # an engine directive talks to nobody
    try:
        if moved:
            text = _after_setting_out(text)
            if not text.strip():
                return False
        extra = _look_forms(npc, text, others) if here and npc.id in speakers else []
        if _addresses(npc, text, extra):
            return not _farewell_to(npc, text, extra)
        if speakers != {npc.id} or not _SPOKE.search(text) or _talks_about(npc, text):
            return False                   # someone else answered, nothing said, or said ABOUT them
        return not any(_addresses(other, text) for other in [*others, *elsewhere]) and not _farewell_to(npc, text)
    except Exception as e:
        logger.debug(f"talk_to address check failed for {npc.id}: {e}")
        return False


# ----------------------------------------------------------------------
# Names the player has heard
# ----------------------------------------------------------------------
# fleet4 (round 3): the first line the DM labelled with a stranger's real
# name counted as their introduction, said or not — "Wolf-Mantled Man"
# became "Aldric Vane" with nobody saying it (bram T4->T5), and so did
# Piper, Maera, Sorrel and the Captain; the prose named "Vex Marrow" while
# his label stayed "Ash-Cloaked Man" (wenna T11). A name is the player's
# once it has been SAID where they could hear it — a speaker line, a quote
# in the prose, their own words, their journal — and only the part that
# was said: "Vex", not "Vex Marrow".

# One exchange line of a memory file: "Day 1 17:40 · Brann Coldiron: "…"".
# Only the quoted words count — the label and the file's title carry the
# real name whether or not it was ever said.
_EXCHANGE_WORDS = re.compile(r'^\s*(?:[-*]\s+)?(?:[^·\n]*·\s*)?[^:"\n]{1,60}:\s*"(.*?)"?\s*$', re.M)
_NAME_TAIL = frozenset({"of", "the", "de", "da", "del", "van", "von", "du", "la", "le", "al", "bin", "and"})
_ALL_TITLES = frozenset((_PERSON_TITLES | _TITLE_WORDS) - {"the"})
# Words that are names AND ordinary words: at the start of a sentence they
# are the word ("Will you come?", "Tack hangs by the door"), so only a
# mid-sentence capital, or the whole name, is taken for the person there
_WORD_NAMES = frozenset({
    "will", "rose", "hope", "mark", "may", "grace", "faith", "joy", "june", "april", "august",
    "summer", "autumn", "winter", "frank", "jack", "bill", "pat", "sue", "dawn", "eve", "ruby",
    "amber", "iris", "lily", "ivy", "hazel", "holly", "heather", "olive", "pearl", "sky", "storm",
    "stone", "wolf", "fox", "crow", "raven", "rook", "wren", "finch", "hawk", "piper", "sorrel",
    "tack", "vane", "marrow", "cole", "reed", "ash", "flint", "steel", "gray", "grey", "black",
    "white", "brown", "green", "young", "hunter", "smith", "baker", "cook", "carter", "mason",
    "archer", "fisher", "porter", "page", "bishop", "king", "knight", "bell", "hall", "hill",
    "wood", "ford", "lane", "field", "brook", "banks", "burns", "mills", "wells", "little",
    "long", "short", "sharp", "swift", "strong", "wise", "bold", "still", "even", "rich", "gold",
    "silver", "penny", "crane", "drake", "hale", "rain", "snow", "frost", "march", "chase",
    "lance", "rod", "rob", "cliff", "dale", "glen", "heath", "moss", "rowan", "sage", "thorn",
})
# fleet5-tamsin T3: "Piper's whole face lights up" went out before anyone had
# said Piper — the exemption took the girl standing there for the word. Of
# the word-names, these open an ordinary sentence with no article in front
# ("Will you…", "Still, she waits", "Tack hangs by the door", "Rain falls"):
# a modal, an adverb, an adjective, a mass or weather noun. Any other one
# ("Piper", "Vane", "Rook") needs an article to be the thing, so opening a
# sentence while that person is near it is the person.
_BARE_WORDS = frozenset({
    "will", "may", "still", "even", "long", "little", "short", "sharp", "swift", "strong", "wise",
    "bold", "rich", "young", "gray", "grey", "black", "white", "brown", "green", "gold", "silver",
    "hope", "grace", "faith", "joy", "summer", "autumn", "winter", "june", "april", "august", "march",
    "dawn", "rain", "snow", "frost", "storm", "sky", "stone", "steel", "wood", "ash", "moss", "sage",
    "tack", "heather", "ivy", "amber",
})
# After a word-name: it is a person if it owns something ("Piper's face") or
# does what only people do ("Piper says", "Vane smiles")
_POSSESSIVE_AFTER = re.compile(r"['’]s(?![\w'’-])")
_PERSON_CUE = re.compile(
    r"\s+(?:says|said|asks|asked|answers|answered|replies|replied|whispers|whispered|mutters|muttered|"
    r"murmurs|murmured|nods|nodded|shrugs|shrugged|grins|grinned|smiles|smiled|laughs|laughed|chuckles|"
    r"chuckled|frowns|frowned|sighs|sighed|winces|winced|scowls|scowled|glances|glanced|beams|beamed|"
    r"snorts|snorted|gasps|gasped|hesitates|hesitated|blinks|blinked|grimaces|grimaced|stammers|"
    r"stammered|insists|insisted|admits|admitted|tells|told|warns|warned|leans|leaned|squints|squinted|"
    r"swallows|swallowed|flushes|flushed|herself|himself|themselves)(?![\w'’-])"
)


def name_words(name: str) -> list[str]:
    """The words of a real name a speaker can say, as written: "Captain Brann
    Coldiron" -> [Brann, Coldiron]. Titles are not the name, nor is a quoted
    epithet ('Jax "The Shadow" Vane' -> [Jax, Vane]) or a role after "the"
    ("Hilda the Guard" -> [Hilda])."""
    words = _EPITHET.sub(" ", str(name or "")).split()
    lowered = [w.lower() for w in words]
    if "the" in lowered[1:]:
        words = words[:lowered.index("the", 1)]
    out = []
    for raw in words:
        w = raw.strip(".,;:!?\"'’“”()")
        if len(w) > 2 and w[0].isupper() and w.lower() not in _ALL_TITLES:
            out.append(w)
    return list(dict.fromkeys(out))


# Not a name after an article: "the Garrison" is a barracks, not the
# Garrison Clerk (fleet-wenna), "the Hollow" a place
_NOT_AFTER_ARTICLE = r"(?<![\w'’-])(?<!the )(?<!The )(?<!\ba )(?<!\bA )(?<!\ban )(?<!\bAn )"


def _said(word: str, text: str) -> bool:
    """`word` written as a name in `text`: capitalised, whole, not after an
    article ("Vex's" counts; "Vexing", "the vex" and "the Vex" do not)."""
    return bool(word and text) and re.search(rf"{_NOT_AFTER_ARTICLE}{re.escape(word)}(?![\w-])", text) is not None


def heard_form(name: str, heard: set[str]) -> str:
    """The name as far as the player has heard it: "Vex Marrow" with only
    "Vex" said is "Vex"; "Captain Brann Coldiron" with "Coldiron" said is
    "Captain Coldiron". With all of it (or none of it) said, the whole name."""
    words = name_words(name)
    if not heard or set(words) <= set(heard) or not set(words) & set(heard):
        return name
    kept = [w for w in str(name).split() if w.strip(".,") not in words or w.strip(".,") in heard]
    while kept and kept[-1].lower() in _NAME_TAIL:
        kept.pop()
    return " ".join(kept) or name


def heard_corpus(player_id: str | None) -> str:
    """Everything this player has heard said aloud before this turn, or read
    in their journal: the quoted words of every exchange in their NPC memory
    files (their own words too — those are filed with whoever they spoke to)
    and the journal entries. Never raises."""
    if not player_id:
        return ""
    from src.services import memory_files

    parts: list[str] = []
    by_file: list[tuple[str, list[str]]] = []
    for npc_id, text in memory_files.npc_memory_texts(player_id).items():
        said = [m.group(1) for m in _EXCHANGE_WORDS.finditer(text)]
        by_file.append((npc_id, said))
        parts.extend(said)
    # A name said for someone else names nobody ("Old Marrow worked nights
    # before me", fleet9-wenna T12): it is blanked here, so it is never heard
    if any(_BORROWED_CUE.search(line) for _, said in by_file for line in said):
        parts = [line for npc_id, said in by_file
                 for line in (_without_borrowed(said, npc_id) if any(_BORROWED_CUE.search(x) for x in said)
                              else said)]
    try:
        parts.extend(memory_files.read_journal(player_id))
    except Exception as e:
        logger.debug(f"heard names: journal unreadable for {player_id}: {e}")
    parts.extend(sorted(memory_files.read_heard_names(player_id)))
    return "\n".join(parts)


# --- borrowed names (round 8) ----------------------------------------------
# fleet9-wenna T12: the stable hand said "Old Marrow worked nights before me"
# — a man the DM invented, given the surname of Vex Marrow, the story's
# successor villain. "Marrow" went into heard_names: the scout would have
# walked on stage already named, and the player would have tied him to an
# old stable hand. A word of a real person's name in such a frame is
# someone else's and names nobody: never credited as heard, this turn
# (`NameGate.heard_this_turn`) or from memory (`heard_corpus`), and the
# audit says so (turn_audit.name_collision). "Old" is them when their looks
# are old; a predecessor in the speaker's own trade may be them; "poor" and
# "the late" are how the dead are spoken of, never someone else.
_AGE_BEFORE = re.compile(r"\b(old|young|little)\s+$", re.IGNORECASE)
_AGE_LOOKS = {
    "old": ("old", "elderly", "aged", "ancient", "grey-haired", "gray-haired", "white-haired", "grizzled",
            "stooped", "wizened", "greybeard", "graybeard", "sixties", "seventies"),
    "young": ("young", "youth", "boy", "girl", "lad", "lass", "teen", "child"),
    "little": ("small", "little", "short", "slight", "child", "boy", "girl"),
}
_BEFORE_ME = re.compile(
    r"(?:['’]s)?[^.!?\"”\n]{0,40}?\bbefore\s+(?:me|my\s+time|I\s+(?:came|started|took\s+over|got\s+here))\b",
    re.IGNORECASE,
)
# Cheap pre-check: a corpus with neither frame needs no reading of the world.
# The name after "old" is capitalised — case-blind, "old man" and "little
# more" sent nearly every memory corpus to read the world once per file
_BORROWED_CUE = re.compile(r"\b(?i:old|young|little)\s+[A-Z]|\b(?i:before\s+(?:me|my\s+time))\b|\b(?i:before)\s+I\s+")


def _shared_trade(a: str, b: str) -> bool:
    words = lambda s: {w for w in re.findall(r"[a-z]{4,}", (s or "").lower())}  # noqa: E731
    return bool(words(a) & words(b))


def borrowed_name(text: str, start: int, end: int, *, looks: str = "", trade: str = "",
                  speaker_trade: str = "") -> str | None:
    """The words in which the name word at `text[start:end]` stands for
    someone else — "Old Marrow", "Marrow worked nights before me" — or None.
    `looks` and `trade` are the real person's; `speaker_trade` the
    speaker's (see above)."""
    m = _AGE_BEFORE.search(text[max(0, start - 12):start])
    if m:
        cues = _AGE_LOOKS.get(m.group(1).lower(), ())
        if not any(re.search(rf"\b{re.escape(c)}", (looks or "").lower()) for c in cues):
            return f"{m.group(1)} {text[start:end]}"
    after = _BEFORE_ME.match(text, end)
    if after and not _shared_trade(trade, speaker_trade):
        return text[start:after.end()]
    return None


def _word_spans(word: str, text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in
            re.finditer(rf"{_NOT_AFTER_ARTICLE}{re.escape(word)}(?![\w-])", text or "")]


def _without_borrowed(lines: list[str], speaker_id: str | None) -> list[str]:
    """`lines` (one NPC memory file's words) with every name word said for
    someone else blanked. Never raises: the lines as they are on trouble."""
    try:
        with get_session() as session:
            people = [(name_words(n.name), str(n.description_physical or ""), str(n.profession or ""))
                      for n in session.query(NPC).all() if n.name]
            speaker = session.get(NPC, speaker_id) if speaker_id else None
            speaker_trade = str(getattr(speaker, "profession", "") or "")
    except Exception as e:
        logger.debug(f"borrowed names unread: {e}")
        return lines
    out = []
    for line in lines:
        if _BORROWED_CUE.search(line):
            for words, looks, trade in people:
                for word in words:
                    for s, e in reversed(_word_spans(word, line)):
                        if borrowed_name(line, s, e, looks=looks, trade=trade, speaker_trade=speaker_trade):
                            line = line[:s] + "—" + line[e:]
        out.append(line)
    return out


def remember_heard(player_id: str | None, words: list[str]) -> None:
    """Keep the name words the player heard this turn where no NPC's memory
    would: an ambient voice ("Gate Guard: 'That's Vex.'"), a quote or an
    introduction in the prose, their own words when nobody answered. Without
    it, a name said once was masked again the next turn. Never raises."""
    if not player_id:
        return
    from src.services import memory_files

    memory_files.add_heard_names(player_id, words)


# fleet7-bram T18: the notice board read "… — Capt. B. Coldiron." and "Ask
# Piper." (`@interact Notice board`, "quote this; never add to it"); the
# DM quoted it word for word, the gate — which had heard neither name —
# showed "— Capt. B. The Watch Captain.", and the audit flagged the quote.
# A name read is a name learned, from the moment it is read.

def read_texts(result: Any) -> list[str]:
    """The words a Thing showed the player (`scene_objects.resolve_interaction`'s
    success body, the canvas's and @interact's): its panel — a notice, a
    carving, a letter found inside. Not a body's (that only lists the finds)."""
    if not isinstance(result, dict) or not result.get("success") or result.get("kind") == "body":
        return []
    inner = result.get("result") if isinstance(result.get("result"), dict) else {}
    document = inner.get("document") if isinstance(inner.get("document"), dict) else {}
    return [str(t) for t in (inner.get("panel_text"), document.get("text")) if t]


def remember_read(player_id: str | None, texts: list[str]) -> list[str]:
    """The names in words the player just read on the canvas are heard now —
    the heard-names file and the rows say so before the next reply (its
    brief, its gate, the chips). Returns the name words kept. Never raises."""
    text = "\n".join(t for t in texts or [] if t)
    if not player_id or not text.strip():
        return []
    try:
        gate = NameGate.for_player(player_id, text)
        if gate is None:
            return []
        words = [w for w in gate.heard_this_turn() if w not in gate._reserved]
        remember_heard(player_id, words)
        note_names_known(player_id, gate.known_ids())
        return words
    except Exception as e:
        logger.warning(f"names read not kept for {player_id}: {e}")
        return []


def heard_names(player_id: str | None) -> frozenset[str]:
    """The name words the player has heard said (`remember_heard`). Empty on
    any trouble."""
    if not player_id:
        return frozenset()
    from src.services import memory_files

    return memory_files.read_heard_names(player_id)


def note_names_known(player_id: str | None, npc_ids: list[str] | set[str]) -> list[str]:
    """The player has now heard these people's names: their rows say so.

    fleet6-wenna: the Keeper spoke at T13 under her role, Piper and the
    player said "Maera" at T12/T14, and her prose stayed "the Keeper" through
    T18. Only rows that exist move — hearing a name is no relationship (the
    heard-names file covers who has none). Returns the ids that flipped.
    Never raises."""
    ids = [i for i in dict.fromkeys(npc_ids or []) if isinstance(i, str) and i]
    if not player_id or not ids:
        return []
    flipped: list[str] = []
    try:
        with get_session() as session:
            for rel in session.query(NPCRelationship).filter(
                    NPCRelationship.player_id == player_id, NPCRelationship.npc_id.in_(ids),
                    NPCRelationship.name_known.is_(False)).all():
                rel.name_known = True
                flipped.append(rel.npc_id)
            if flipped:
                session.commit()
    except Exception as e:
        logger.warning(f"known names not recorded for {player_id}: {e}")
    return flipped


def note_player_name_heard(player_id: str | None, spoken: list[dict[str, Any]],
                           addressed_npc_id: str | None = None, *, left_from: str | None = None) -> list[str]:
    """The player's name was said aloud in the scene this turn (the caller
    judged it: `turn_audit.player_name_said`): whoever was there to hear it
    — the people standing where the player is, this turn's speakers, the one
    addressed — knows it now, on their row. A bystander with no row yet has
    no relationship to keep it on, so it goes in their memory file
    (OVERHEARD_PLAYER_NAME) and their row starts with it when they first
    speak (fleet4-wenna: "I'm Wenna," to Piper at T2, with Sorrel in the
    square; "Miss Wenna," from him at T20). Returns the ids that learned it.

    `left_from`: where the turn started. A reply that moved the player
    cannot say on which side of the door the name was given ("I tell the
    captain my name and head for the gate" / "I go in and tell the keeper my
    name"), so bystanders learn nothing then — a name wrongly "known" is a
    name the DM will say to someone who never heard it. Never raises."""
    if not player_id:
        return []
    learned: list[str] = []
    overheard: list[str] = []
    try:
        with get_session() as session:
            player = session.get(Player, player_id)
            here = player.current_location_id if player is not None else None
            ids = {s.get("npc_id") for s in spoken or [] if isinstance(s, dict) and s.get("npc_id")}
            if addressed_npc_id:
                ids.add(addressed_npc_id)
            if here and (not left_from or left_from == here):
                ids |= {npc.id for npc in session.query(NPC).filter(
                    NPC.current_location_id == here, NPC.status == "alive").all() if not is_beast(npc)}
            if not ids:
                return []
            rows = session.query(NPCRelationship).filter(
                NPCRelationship.player_id == player_id, NPCRelationship.npc_id.in_(list(ids))).all()
            for rel in rows:
                if rel.knows_player_name is False:
                    rel.knows_player_name = True
                    learned.append(rel.npc_id)
            overheard = sorted(ids - {rel.npc_id for rel in rows})
            if learned:
                session.commit()
    except Exception as e:
        logger.warning(f"player's name not recorded as heard for {player_id}: {e}")
    if overheard:
        from src.services import memory_files

        for npc_id in overheard:
            memory_files.add_npc_note(player_id, npc_id, OVERHEARD_PLAYER_NAME)
    return learned + overheard


OVERHEARD_PLAYER_NAME = "Heard the player's name said aloud."


def overheard_player_name(player_id: str, npc_id: str) -> bool:
    """Did this NPC hear the player's name before they ever spoke with them?"""
    from src.services import memory_files

    key = OVERHEARD_PLAYER_NAME.lower().rstrip(".")
    return any(key in str(note).lower() for note in memory_files.memory_parts(player_id, npc_id)["notes"])


def _spoken_with(player_id: str | None) -> set[str]:
    """Ids of the NPCs with a memory of this player."""
    if not player_id:
        return set()
    from src.services import memory_files

    return set(memory_files.npc_memory_texts(player_id))


@dataclass
class _Person:
    id: str
    name: str
    words: list[str]
    label: str | None           # their role label; None when it is their name (a beast)
    near: bool = False          # present, in the party or the campaign's cast this turn
    looks: str = ""             # what they look like and do: a borrowed name is someone
    trade: str = ""             # else's (`borrowed_name`)


# A quote in the prose was said aloud: whatever name it holds, the player heard
_QUOTE_SPAN = re.compile(r'"[^"\n]*"|“[^”\n]*”')
# The prose giving a name in the fiction: "introduces himself as Vex", "a man
# named Vex", "signed B. Coldiron" — not the narrator leaking it
_INTRO_CUE = re.compile(
    r"(?:\bname(?:'s|’s|\s+is|\s+was)|\b(?:the|a|his|her|their)\s+name|\bnamed|\bcalled"
    r"|\bcalls\s+(?:him|her|them)(?:self|selves)?"
    r"|\bintroduc\w*\s+(?:himself|herself|themselves|themself)\s+as|\bgoes\s+by|\banswers\s+to"
    r"|\bknown\s+as|\bsigned|\bsignature\s+reads?)\s*[,:—–-]?\s*(?:simply\s+|just\s+)?[\"“'‘*_]*"
    r"(?:[A-Z][\w.'’-]*\s+){0,2}$",
)
_DETERMINER_BEFORE = re.compile(r"\b(?:the|a|an|this|that)\s+$", re.I)
_SENTENCE_OPEN = re.compile(r'(?:^|\n|[.!?…]["”’)\]*_]*[ \t]+)[ \t*_>#-]*$')
_TITLE_ALT = "|".join(sorted((t.capitalize() for t in _ALL_TITLES), key=len, reverse=True))
_TITLE_HEAD = re.compile(rf"(?:{_TITLE_ALT})\.?\s+")
_WORD_START = re.compile(r"(?<![\w'’-])[A-Z]")
# A word-name at the end of what has streamed, and at most the next word
_WORD_NAME_TAIL = re.compile(r"([A-Z][a-z]+)(?:['’]s?)?(?:\s+[\w'’-]*)?")


def _reads_as_person(person: _Person, word: str, after: str) -> bool:
    """A word-name opening a sentence is the person, not the word, when it
    owns something or does what people do, or when that person is near
    and the word cannot open a sentence bare (fleet5-tamsin T3)."""
    if _POSSESSIVE_AFTER.match(after) or _PERSON_CUE.match(after):
        return True
    return person.near and word.lower() not in _BARE_WORDS


# ----------------------------------------------------------------------
# The player's name in the mouth of someone who never heard it
# ----------------------------------------------------------------------
# fleet7 (all three runs): Piper said "Straight lines, Bram." (bram T16, T18)
# and "Someone trained them, Tamsin — someone's out there" (tamsin T11), and
# the prose made a moment of it ("She says your name like she's only just
# letting herself use it"). Her dossier said "Knows the player's name: no";
# the audit caught it a turn late, after the player had read it. In the line
# of a speaker who does not know it, the name is cut where it is an address
# ("…, Bram." / "Tamsin — …") and becomes "you" anywhere else — before the
# bubble, the voice, the memory file and the transcript take the line.

# Words that open a clause before a name said to someone: "And Tamsin —"
_ADDRESS_OPENER = re.compile(
    r"(?:^|[.!?…;:—–(\"“*_-]\s*)(?:and|but|so|oh|hey|ah|well|now|listen|look|easy|careful|sorry|please|"
    r"okay|ok|right|yes|no|thanks|go|come|morning|evening|night)[,!]?\s+$", re.I)
# "now, Holloway, is what you…": the address sat between subject and verb
_AUXILIARIES = frozenset({"is", "was", "are", "were", "will", "would", "can", "could", "should", "must", "has",
                          "have", "had", "does", "did", "may", "might", "shall"})
_YOU_VERBS = {"is": "are", "was": "were", "has": "have", "does": "do", "isn't": "aren't", "wasn't": "weren't",
              "hasn't": "haven't", "doesn't": "don't", "says": "say"}
_NOT_VERBS = frozenset({"this", "thus", "plus", "towards", "afterwards", "perhaps", "always", "sometimes",
                        "unless", "across", "besides", "less", "his", "hers", "its", "theirs", "yours", "whereas"})
_SUBJECT_BEFORE = re.compile(r"(?:^|[.!?…;:—–\"“(]\s*|,\s+|\b(?:and|but|so|then|because|that|if|when|while|"
                             r"until|unless|though|since|where)\s+)$", re.I)
# "Bram's here" is "you're here"; "Bram's been" is "you've been"; "Bram's
# blade" is "your blade"
_YOURE_AFTER = frozenset({"here", "there", "gone", "right", "back", "not", "never", "always", "still", "just",
                          "already", "a", "an", "the", "out", "in", "done", "dead", "alive", "sure", "fine",
                          "good", "late", "early", "wrong", "safe", "too", "no", "off", "on", "up", "away",
                          "home", "new", "one", "like", "all"})
_YOUVE_AFTER = frozenset({"been", "got", "gotten"})
# The name as what "you" are, or who someone is ("You're Bram, aren't you?",
# "You must be Wenna.", "Who's Tamsin?"): "you" there reads "You're you" —
# a guess at someone is "the one", a question about them "that"
_YOU_ARE_BEFORE = re.compile(
    r"\b(?:you(?:['’]re|\s+are|\s+were)|(?:are|were)\s+you|you\s+(?:must|might|could|should|would|will|may|can)"
    r"\s+be)\s+$", re.I)
_WHO_IS_BEFORE = re.compile(r"\bwho(?:['’]s|\s+is|\s+was)\s+$", re.I)
_ADDRESS_CLOSE = ",.!?—–-…;:"
# The prose remarking on a name it just had someone use ("She says your name
# like…", "your name sounds strange in her mouth") — not "she never asks your
# name", which the cut line still bears out
_NAME_REMARK = re.compile(
    r"\b(?:says?|said|saying|speaks?|spoke|speaking|uses?|used|using|tries|tried|trying|tests?|tested|"
    r"lingers?|lingered|rolls?|rolled|shapes?|shaped|sounds?|sounded|hear|hears|heard|hearing|calls?|called|"
    r"lands?|landed)\b[^.!?\n]{0,40}\byour\s+(?:own\s+|first\s+|full\s+)?name\b"
    r"|\byour\s+(?:own\s+|first\s+|full\s+)?name\b[^.!?\n]{0,30}\b(?:sounds?|lands?|hangs?|lingers?|"
    r"in\s+(?:her|his|their)\s+mouth|on\s+(?:her|his|their)\s+(?:lips|tongue))\b", re.I)
_NAME_REMARK_DENIED = re.compile(r"\b(?:never|not|n['’]t|without)\b", re.I)
# ...and the prose telling the slip itself (fleet9-tamsin T11: "She catches
# herself using your name before remembering she was never told it" — the
# line had lost the name). Replayed over every transcript: that sentence, and
# one on a turn with no cut ("a town that's never heard your name"), kept
_NAME_SLIP = re.compile(
    r"\byour\s+(?:own\s+|first\s+|full\s+)?name\b[^.!?\n]{0,80}?\b(?:never|not|n['’]t)\s+(?:once\s+|even\s+"
    r"|been\s+|actually\s+)*(?:told|given|said|offered|shared|heard|learned|learnt|know|knew|known)\b"
    r"|\b(?:never|not|n['’]t)\s+(?:once\s+|even\s+|been\s+|actually\s+)*(?:told|given|offered|shared|said|heard"
    r"|learned|learnt|know|knew|known)\b[^.!?\n]{0,40}?\byour\s+(?:own\s+|first\s+|full\s+)?name\b"
    r"|\bcatch(?:es|ing)?\s+(?:herself|himself|themselves)\b[^.!?\n]{0,40}?\b(?:your\s+name|calling\s+you"
    r"|naming\s+you)\b"
    r"|\bcall(?:s|ed|ing)?\s+you\s+by\s+(?:your\s+)?name\b", re.I)
_NAME_TITLES = frozenset({"sir", "ser", "lady", "lord", "dame", "master", "mistress", "mister", "miss", "mr", "mrs",
                          "ms", "captain", "sergeant", "commander", "lieutenant", "brother", "sister", "father",
                          "mother", "doctor", "dr", "professor", "agent", "detective", "inspector", "officer"})


def _opens_here(before: str) -> bool:
    """Does a sentence start right after `before`?"""
    head = before.rstrip().rstrip("*_\"“‘(").rstrip()
    return not head or head[-1] in ".!?…"


def _cap(word: str, before: str) -> str:
    return word[:1].upper() + word[1:] if _opens_here(before) else word


def _as_you(before: str, after: str) -> str:
    """A name that is no address, as "you": its verb agreeing when it was the
    subject ("Tamsin counts them" -> "You count them")."""
    you = _cap("you", before)
    verb = re.match(r"(\s+)([a-z][\w'’]*)", after)
    if not verb or not _SUBJECT_BEFORE.search(before):
        return you + after
    word = verb.group(2).replace("’", "'")
    new = _YOU_VERBS.get(word)
    if new is None and len(word) > 3 and word.endswith("s") and not word.endswith("ss") \
            and word not in _NOT_VERBS and "'" not in word:
        new = (word[:-3] + "y" if word.endswith("ies") else
               word[:-2] if word.endswith(("ches", "shes", "sses", "xes", "zes", "oes")) else word[:-1])
    if new is None:
        return you + after
    return you + verb.group(1) + new + after[verb.end():]


def _cut_one(text: str, s: int, e: int) -> str:
    """`text` with the player's name at [s, e) cut (an address) or made "you"."""
    before, after = text[:s], text[e:]
    owned = re.match(r"['’]s\b", after)
    if owned:
        rest = after[owned.end():]
        nxt = re.match(r"\s+([\w'’-]+)", rest)
        word = nxt.group(1).lower() if nxt else ""
        you = ("you've" if word in _YOUVE_AFTER else
               "you're" if word in _YOURE_AFTER or word.endswith("ing") else "your")
        return before + _cap(you, before) + rest
    if _YOU_ARE_BEFORE.search(before):
        return before + "the one" + after                  # "You're Bram, aren't you?" -> "You're the one, …"
    if _WHO_IS_BEFORE.search(before):
        return before + "that" + after                     # "Who's Bram?" -> "Who's that?"
    if re.match(r",\s*(?:right|yeah|yes)\?", after) and before.rstrip(" ,;—–-"):
        # "You did it, Wenna, right?" -> "You did it, right?" ("Hey — Wenna,
        # right?" goes whole in `cut_player_name`: "Hey — you, right?" asked
        # nothing, fleet8-wenna)
        return before.rstrip(" ,;—–-") + after
    head = before.rstrip()
    lead = after.lstrip()
    # One character each, or "" at either end of the line (and "" is in every
    # string: the tests below are on a mark, so " " stands in for none)
    last, first = head[-1:] or " ", lead[:1] or " "
    if lead and first not in _ADDRESS_CLOSE:
        return before + _as_you(before, after)             # "Tamsin heard it too" -> "You heard it too"
    if last in ",;":
        base = head[:-1]
        if not lead or first in ".!?…":
            return base + lead                             # "an accident, Bram." -> "an accident."
        if first in ",;":
            nxt = re.match(r"[,;]\s*([\w'’]+)", lead)
            if nxt and nxt.group(1).lower() in _AUXILIARIES:
                return base + " " + lead[1:].lstrip()      # "now, Holloway, is what" -> "now is what"
            return base + lead                             # "wolves, Wenna, and" -> "wolves, and"
        return base + " " + lead                           # "them, Tamsin — someone's" -> "them — someone's"
    if last in "—–-…" and (not lead or first in ".!?…"):
        return head[:-1].rstrip() + lead                   # "Go home — Bram." -> "Go home."
    if last not in "—–-…" and not _opens_here(before) and not _ADDRESS_OPENER.search(before):
        return before + _as_you(before, after)             # "I saw Bram." -> "I saw you."
    # It opens a sentence, follows a dash, or "And": the name and its mark go
    sep = re.match(r"[.!?…]+[*_]*|[,;:]|[—–-]+", lead)
    rest = lead[sep.end():].lstrip() if sep else lead
    if _ADDRESS_OPENER.search(before) and first == ",":
        return head + ", " + rest                          # "Oh Bram, you didn't" -> "Oh, you didn't"
    if not rest and not re.search(r"[A-Za-z]", before):
        return before + _cap("you", before) + after         # the whole line was the name: "Cole—" -> "You—"
    return before + (_cap(rest, before) if rest else "")


def cut_player_name(text: str, player_name: str) -> tuple[str, list[str]]:
    """`text` (one speaker line) as it reads without the player's name.

    An address goes with its comma or dash ("That's not an accident, Bram."
    -> "That's not an accident."; "Wenna — what happened?" -> "What
    happened?"; "Miss Cole — Wenna — I know…" -> "I know…"); any other use
    is "you" ("Tamsin heard it too" -> "You heard it too"; "Bram's" ->
    "your"). A word of their name inside another name is no use of it
    ("Cole's Ferry", "Osric Grey": `turn_audit._name_uses`). Returns the
    line and the name words cut, in order ([] when there were none).
    """
    from src.web.turn_audit import _name_tokens, _name_uses

    tokens = _name_tokens(player_name or "")
    if not text or not tokens:
        return text, []
    spans = sorted([m.start(), m.end()] for t in tokens for m in _name_uses(text, t, tokens, possessive=True))
    if not spans:
        return text, []
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and re.fullmatch(r"\s+", text[merged[-1][1]:s]):
            merged[-1][1] = e                               # "Wenna Cole" is one address
        else:
            merged.append([s, e])
    cut = [text[s:e] for s, e in merged]
    out = text
    fallback = ""
    for s, e in reversed(merged):
        # A title goes with the name ("Miss Cole"), and so does a hyphenated
        # tail (fleet4-bram T16 "Holloway-who-didn't-give-his-name-till-just-now")
        title = re.search(r"([A-Z][a-z]+)\.?\s+$", out[:s])
        if title and title.group(1).lower() in _NAME_TITLES:
            s = title.start(1)
        tail = re.match(r"(?:-[\w'’]+)+", out[e:])
        if tail:
            e += tail.end()
        # The name asked after ("And — Wenna, was it?", "Hey — Bram,
        # right?"): the question IS the name, so it goes whole, with the
        # "And —" that led into it. fleet8-wenna T18 showed "And — was it?"
        start = _sentence_start(out, s)
        tag = _NAME_TAG.match(out[e:])
        lead_in = _FILLER_LEAD.fullmatch(out[start:s])
        if tag and lead_in:
            word = re.match(r"[\s*_\"“(]*(hey|oh|ah|well|listen|look)\b", out[start:s], re.I)
            fallback = fallback or (word.group(1).capitalize() + "." if word else "…")
            # The line may go on past the question: "Listen — Wenna, right? —
            # the wolves are close." reads "The wolves are close.", never "—
            # the wolves…"
            rest = re.sub(r"^[\s—–,;:-]+", "", out[e + tag.end():])
            out = out[:start] + rest[:1].upper() + rest[1:]
            continue
        # Set in emphasis on its own ("*Cole.*"), the marks go with it
        wrap = re.match(r"[.!?…]*([*_]+)", out[e:])
        if wrap and out[:s].endswith(wrap.group(1)):
            k, close = len(wrap.group(1)), e + wrap.start(1)
            out = out[:s - k] + out[s:close] + out[close + k:]
            s, e = s - k, e - k
        out = _cut_one(out, s, e)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+([,.!?;:…])", r"\1", out).strip()
    out = _without_new_filler(out, text)
    # Nothing of the line left but the name it asked after: a beat, not a
    # blank bubble
    return (out if re.search(r"\w", out) else fallback or out), cut


# A tag question on the name ("…, was it?", "…, right?"): the speaker is
# guessing it, and without it the question asks nothing
_NAME_TAG = re.compile(
    # "*Wenna*, was it?": the emphasis closing on the name stays out of the way
    r"[*_]*\s*[,—–-]?\s*(?:was it|is it|isn['’]t it|wasn['’]t it|right|yes|yeah|aye|no|eh|correct|innit)\s*\?+\s*",
    re.I)
# What may lead into a name asked after, from the sentence start: interjections,
# "and", "you're", and their marks — nothing that says anything on its own
_FILLER_WORDS = r"(?:and|but|so|oh|hey|ah|well|now|listen|look|okay|ok|um|er|uh|you['’]re|you are|and you['’]re)"
_FILLER_LEAD = re.compile(rf"[\s*_\"“(]*(?:{_FILLER_WORDS}\b[\s,—–…-]*)*", re.I)
# A sentence of nothing but such words, as a cut can leave it ("So — Wenna
# Cole." -> "So.")
_FILLER_ONLY = re.compile(
    r"[\s*_\"“(]*(?:(?:and|but|so|oh|ah|well|now|um|er|uh)\b[\s,—–…-]*)+[.!?…]*[\s*_\"”)]*", re.I)


_COPULA_BEFORE = re.compile(r"\b(?:is|isn['’]t|was|wasn['’]t|are|aren['’]t|were|weren['’]t|be|not|all)\W*$", re.I)


def without_player_name(text: str, player_name: str | None) -> str:
    """`text` from an NPC's memory file with the player's name made "you",
    for the dossier of someone who never heard it. fleet8-wenna: Sorrel's
    file read "— Wenna: …" and "Wenna came to the yard after dark", his
    dossier said "Knows the player's name: no", and he said "Wenna" in six
    replies — six cuts, the engine re-cutting what the file kept feeding.

    The same reading of a name as the cut (`turn_audit._name_uses`): a word
    of it inside another name ("Osric Cole", "the Grey Company") is no use
    of it, and "Wenna's here" is "you're here"."""
    from src.web.turn_audit import _name_tokens, _name_uses

    tokens = _name_tokens(str(player_name or ""))
    if not text or not tokens:
        return text
    spans = sorted([m.start(), m.end()] for t in tokens for m in _name_uses(text, t, tokens, possessive=True))
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and re.fullmatch(r"\s+", text[merged[-1][1]:s]):
            merged[-1][1] = e                               # "Wenna Cole" is one use
        else:
            merged.append([s, e])
    out = text
    for s, e in reversed(merged):
        before, after = out[:s], out[e:]
        word = "you"
        owned = re.match(r"['’]s\b", after)
        if owned:
            after = after[owned.end():]
            nxt = re.match(r"\s+([\w'’-]+)", after)
            following = nxt.group(1).lower() if nxt else ""
            word = ("you've" if following in _YOUVE_AFTER else
                    "you're" if following in _YOURE_AFTER or following.endswith("ing") else "your")
            # A possessive standing for the thing itself is "yours": "the
            # choice is Bram's.", "a bed that isn't Bram's to search" (a
            # round-8 drop rewritten as "isn't your to search")
            if not re.match(r"[\s*_\"“]*\w", after) or (following == "to" and _COPULA_BEFORE.search(before)):
                word = "yours"
        head = before.rstrip(" *_\"“(")
        opens = (not head or head.endswith(("\n", ".", "!", "?", "…", "—", "–", ":", "-", "·"))
                 or re.search(r"\(Day [^)]*\)$", head) is not None)   # a note's "(Day 1) …"
        out = before + (word.capitalize() if opens else word) + after
    return out


def _sentence_start(text: str, pos: int) -> int:
    """Where the sentence holding `pos` begins."""
    ends = list(re.finditer(r"[.!?…]+[\"”’)*_]*\s+", text[:pos]))
    return ends[-1].end() if ends else 0


def _without_new_filler(out: str, original: str) -> str:
    """`out` without a sentence the cut left as filler alone ("So.", "And —")
    — one the line already had stays."""
    pieces = re.split(r"(?<=[.!?…])\s+", out)
    had = {p.strip() for p in re.split(r"(?<=[.!?…])\s+", original)}
    kept = [p for p in pieces if not (_FILLER_ONLY.fullmatch(p) and p.strip() not in had)]
    if not kept or len(kept) == len(pieces):
        return out
    joined = " ".join(kept)
    return joined[:1].upper() + joined[1:]


class NameGate:
    """What the player may see a person called, for one turn.

    Built once per turn (`for_turn`) from what the player has heard before
    (`heard_corpus`), their own words this turn, and who they had met
    before it; it then `hear`s every speaker line and every quote in the
    prose as the reply streams. It answers two questions:

      * `speaker` — the name to show on a speaker line, and whether the line
        introduces a stranger. A real-name label nobody has said is shown
        as the role label (and recorded in `unintroduced` for the audit).
      * `feed`/`flush`/`mask` — prose with an unmet person's unheard name
        rewritten to what the player knows them by: the part of the name
        they heard, else "the Ash-Cloaked Man" (recorded in `masked`).

    And the other way round, `player_name_in`: a speaker line with the
    PLAYER's name in the mouth of someone who never heard it loses it
    (`cut_player_name`, recorded in `player_name_cuts`), and
    `comments_on_cut_name` finds the prose that remarks on it.

    Only the people this turn could name are masked (present, named in the
    DM's context, the campaign's cast, or spoken with before), compiled
    into one pattern, so a chunk costs one regex pass. Streaming holds back
    only a tail that could still grow into a name ("…glances at Vex Mar").
    A met person is shown by the part of their name that was said, too
    (fleet5-wenna T19: "Sorrel Tack" when only "Sorrel" had been).

    `for_player` builds the same gate over the whole cast for what the
    client shows outside the reply (`player_facing_state`); there nothing
    is speech (`fiction=False`), so a quote or "named" in a quest line
    gives no name away.
    """

    def __init__(self, player_id: str | None = None, player_input: str = "", *, corpus: str = "",
                 met_ids=(), people=(), reserved=(), fiction: bool = True) -> None:
        self.player_id = player_id
        self._corpus = corpus or ""
        self._said_now: list[str] = []
        self._said_raw: list[str] = []                    # as said, before the seal guard cut any (`_unhear`)
        self._cache: dict[str, tuple[bool, int]] = {}
        self._met = set(met_ids or ())
        self._persons: dict[str, _Person] = {}
        self._reserved = set(reserved or ())
        self._fiction = fiction
        self.masked: list[tuple[str, str]] = []           # (name the DM wrote, what the player saw)
        self.unintroduced: list[tuple[str, str]] = []     # (label the DM wrote, label shown)
        self._buf = ""
        self._seen = "\n"                                 # the tail of what was released
        # The player's own name (`player_name_in`): set by `for_turn`; empty
        # leaves every line as written
        self._player_name = ""
        self._player_input = player_input or ""
        self._knew_player: set[str] = set()               # ids whose row says they know it
        self._knows_cache: dict[str, bool] = {}
        self._lines: list[dict[str, Any]] = []            # this turn's speaker lines, as shown
        self._prose: list[str] = []                       # this turn's prose, as released
        self.player_name_cuts: list[tuple[str, str]] = []  # (speaker as shown, the name cut)
        self._spoken_by: dict[str, str] = {}              # a line as heard -> who said it
        self.sealed_cuts: list[dict[str, Any]] = []       # lines a sealed truth was cut from
        words = _player_words(player_input)
        if words:
            self.hear(words)
        self._compile([p for p in people or () if p.label])

    @classmethod
    def for_turn(cls, player_id: str | None, player_input: str = "", *, present_ids=(), context: str = "") -> "NameGate":
        """The gate for this turn; an inert one (shows real names, masks
        nothing) if the world cannot be read."""
        try:
            corpus = heard_corpus(player_id)
            spoken_with = _spoken_with(player_id)
            present = set(present_ids or ())
            # Who the DM could name this turn: anyone its context names
            in_context = {_POSSESSIVE_END.sub("", w) for w in re.findall(r"[A-Z][\w'’-]*", context or "")}
            from src.models import Faction, Location

            with get_session() as session:
                met = _names_known(session, player_id)
                reserved: set[str] = set()
                player = session.get(Player, player_id) if player_id else None
                if player is not None:
                    reserved |= set(name_words(player.name))
                    if player.current_location_id:
                        present |= {row[0] for row in session.query(NPC.id).filter(
                            NPC.current_location_id == player.current_location_id).all()}
                    # A companion walks beside them
                    raw_party = player.party_members if isinstance(player.party_members, list) else []
                    present |= {i for i in raw_party if isinstance(i, str)}
                # The story's own cast: the DM knows the villain's name from
                # every earlier beat, whether or not this context says it
                try:
                    from src.models import Campaign

                    for row in session.query(Campaign).filter(Campaign.player_id == player_id).all():
                        present |= {i for i in [row.villain_npc_id, row.successor_npc_id,
                                                *(row.ally_npc_ids or [])] if isinstance(i, str)}
                except Exception as e:
                    logger.debug(f"name gate: campaign cast unreadable: {e}")
                people: list[_Person] = []          # in scope, met or not
                for npc in session.query(NPC).all():
                    words = name_words(npc.name)
                    scoped = npc.id in present or npc.id in spoken_with or any(w in in_context for w in words)
                    if not scoped:
                        if npc.id in met:
                            reserved |= set(words)      # never masked as someone else's
                        continue
                    label = stranger_label(npc, met=False)
                    if label is None:
                        reserved |= set(words)
                    people.append(_Person(npc.id, npc.name, words, label, near=npc.id in present,
                                          looks=str(npc.description_physical or ""),
                                          trade=str(npc.profession or "")))
                # A word of a place or faction is not the person ("Wolf's Hollow")
                for (name,) in session.query(Location.name).all() + session.query(Faction.name).all():
                    reserved |= {_POSSESSIVE_END.sub("", w) for w in re.findall(r"[A-Z][\w'’-]*", name or "")}
                # Who knew the PLAYER's name before this turn, by their row
                # (the same rows the audit's `met_before` reads)
                knew_player = {row[0] for row in session.query(NPCRelationship.npc_id).filter(
                    NPCRelationship.player_id == player_id, knows_player_name_clause()).all()} if player else set()
                player_name = str(player.name or "") if player is not None else ""
            gate = cls(player_id, player_input, corpus=corpus, met_ids=met, people=people, reserved=reserved)
            for p in people:
                gate._persons[p.id] = p
            gate._player_name, gate._knew_player = player_name, knew_player
            return gate
        except Exception as e:
            logger.warning(f"name gate unavailable this turn: {e}")
            return cls(player_id, player_input)

    @classmethod
    def for_player(cls, player_id: str | None, heard_now: str = "") -> "NameGate | None":
        """The gate for what the client shows outside the reply: the lead,
        quest titles and steps, chips, toasts, the story panel.

        fleet5 (all three runs): the tracker read "Get Captain Coldiron's
        gate pass for the north road" while every speaker line still said
        "Watch Captain", and each player asked "Coldiron?" at T1. Authored
        text names people by their real names, so it goes through the same
        rule as the prose — recomputed per request from what has been heard
        (`heard_now`: what this turn said that no file holds yet). Everyone
        in the world is in scope and near; nothing here is speech. None if
        the world cannot be read (the caller shows the text as it is).
        """
        try:
            from src.models import Faction, Location

            corpus = heard_corpus(player_id)
            with get_session() as session:
                met = _names_known(session, player_id)
                reserved: set[str] = set()
                player = session.get(Player, player_id) if player_id else None
                if player is not None:
                    reserved |= set(name_words(player.name))
                people: list[_Person] = []
                for npc in session.query(NPC).all():
                    words = name_words(npc.name)
                    label = stranger_label(npc, met=False)
                    if label is None:
                        reserved |= set(words)
                    people.append(_Person(npc.id, npc.name, words, label, near=True,
                                          looks=str(npc.description_physical or ""),
                                          trade=str(npc.profession or "")))
                for (name,) in session.query(Location.name).all() + session.query(Faction.name).all():
                    reserved |= {_POSSESSIVE_END.sub("", w) for w in re.findall(r"[A-Z][\w'’-]*", name or "")}
            gate = cls(player_id, corpus=corpus, met_ids=met, people=people, reserved=reserved, fiction=False)
            gate._persons.update({p.id: p for p in people})
            gate.hear(heard_now)
            return gate
        except Exception as e:
            logger.warning(f"player-facing names unavailable: {e}")
            return None

    # --- hearing ------------------------------------------------------------

    def hear(self, text: str | None) -> None:
        """Something the player just heard said aloud (or read)."""
        if text and str(text).strip():
            self._said_now.append(str(text))
            self._said_raw.append(str(text))

    def _unhear(self, said: str, shown: str) -> None:
        """A line the seal guard cut was heard as written (the server hears a
        line before it reaches `player_name_in`): the player heard only what
        was left, so a name in the cut sentences names nobody — never kept
        in heard_names, never unmasking that person later."""
        for i, entry in enumerate(self._said_now):
            if said in entry:
                self._said_now[i] = entry.replace(said, shown)
                if entry in self._spoken_by:
                    self._spoken_by[self._said_now[i]] = self._spoken_by[entry]
        self._cache.clear()

    def heard_this_turn(self) -> list[str]:
        """The name words of everyone this gate knows of that were said this
        turn — for `remember_heard`. A word said only for someone else ("Old
        Marrow", `borrowed_name`) is not theirs."""
        return [w for p in self._persons.values() for w in p.words
                if any(_said(w, t) and not self._borrowed_in(p, w, t) for t in self._said_now)]

    def known_words(self) -> list[str]:
        """The name words of everyone in scope the player has heard, this
        turn or before (their journal, an exchange in another NPC's file) —
        so `heard_names` holds every name the player has, where a single
        file read finds it (`player_has_met`). A word that is also a place,
        a faction or someone else's (`_reserved`: "Wolf" of "Wolf's Hollow")
        names nobody by being heard, nor one said for an invented person."""
        return [w for p in self._persons.values() if p.label for w in p.words
                if w not in self._reserved and self._credited(p, w)]

    def known_ids(self) -> list[str]:
        """Who in scope the player now knows by name (a word of it heard,
        as `known_words`) — for `note_names_known`."""
        return [p.id for p in self._persons.values()
                if p.label and any(w not in self._reserved and self._credited(p, w) for w in p.words)]

    def said_this_turn(self) -> str:
        """Everything heard this turn, for a `for_player` gate built before
        `remember_heard` has kept it (the post-turn state event) — a name
        said for someone else blanked, as the corpus will keep it."""
        return "\n".join(self._unborrowed(t) for t in self._said_now)

    # --- borrowed names (`borrowed_name`, fleet9-wenna T12) -----------------

    def _speaker_trade(self, text: str) -> str:
        speaker = self._persons.get(self._spoken_by.get(text) or "")
        return speaker.trade if speaker is not None else ""

    def _borrowed_in(self, person: _Person, word: str, text: str) -> bool:
        """Is every use of `word` in `text` someone else's — a borrowed name?"""
        if not _BORROWED_CUE.search(text or ""):
            return False
        spans = _word_spans(word, text)
        trade = self._speaker_trade(text)
        return bool(spans) and all(
            borrowed_name(text, s, e, looks=person.looks, trade=person.trade, speaker_trade=trade)
            for s, e in spans)

    def _credited(self, person: _Person, word: str) -> bool:
        """`heard(word)` as kept for good: from before (the corpus, borrowed
        names already blanked) or said this turn as theirs."""
        if _said(word, self._corpus):
            return True
        return any(_said(word, t) and not self._borrowed_in(person, word, t) for t in self._said_now)

    def _unborrowed(self, text: str) -> str:
        if not _BORROWED_CUE.search(text or ""):
            return text
        trade = self._speaker_trade(text)
        for person in self._persons.values():
            for word in person.words:
                for s, e in reversed(_word_spans(word, text)):
                    if borrowed_name(text, s, e, looks=person.looks, trade=person.trade, speaker_trade=trade):
                        text = text[:s] + "—" + text[e:]
        return text

    def heard(self, word: str) -> bool:
        hit, upto = self._cache.get(word, (None, 0))
        if hit:
            return True
        if hit is None:
            hit = _said(word, self._corpus)
        if not hit:
            hit = any(_said(word, t) for t in self._said_now[upto:])
        self._cache[word] = (hit, len(self._said_now))
        return hit

    def _person(self, npc: NPC) -> _Person:
        person = self._persons.get(npc.id)
        if person is None:
            person = _Person(npc.id, npc.name, name_words(npc.name), stranger_label(npc, met=False))
            self._persons[npc.id] = person
        return person

    def known_as(self, npc: NPC) -> str:
        """What the player calls them now: the part of the name they have
        heard, else (met before the name rule) the name, else the role."""
        person = self._person(npc)
        if person.label is None:
            return person.name          # a wolf is a wolf
        heard = {w for w in person.words if self.heard(w)}
        if heard:
            return heard_form(person.name, heard)
        return person.name if npc.id in self._met else person.label

    def name_for(self, npc_id: str | None, fallback: str | None = None) -> str | None:
        """`known_as` by id, for a payload that carries one ("target": {"id",
        "name"}); a name this gate does not know is masked as text."""
        person = self._persons.get(npc_id) if npc_id else None
        if person is None:
            return self.shown(fallback)
        return self.known_as(SimpleNamespace(id=person.id, name=person.name))

    def shown(self, value: Any) -> Any:
        """Client text as the player may read it: a string masked, or every
        string inside a dict or list (ids are left alone)."""
        if isinstance(value, str):
            return self.mask(value)
        if isinstance(value, list):
            return [self.shown(v) for v in value]
        if isinstance(value, dict):
            return {k: v if k == "id" or str(k).endswith("_id") else self.shown(v) for k, v in value.items()}
        return value

    def speaker(self, npc: NPC, label: str, text: str | None = None) -> dict[str, Any]:
        """For a line the DM labelled `label`, resolved to `npc`: {"shown",
        "label", "introduces", "unintroduced"?}. The line's own words are
        heard first — a stranger saying "Name's Vex." introduces himself."""
        self.hear(text)
        person = self._person(npc)
        shown = self.known_as(npc)
        if person.label is None or npc.id in self._met:
            return {"shown": shown, "label": label, "introduces": True}
        in_label = set(_tokens(label))
        named = [w for w in person.words if w.lower() in in_label]
        if not named or is_role_label(label, npc):
            # The role ("Ash-Cloaked Man"), or a bare trade word that found
            # them ("Guard"): no name given, none withheld
            return {"shown": shown, "label": label, "introduces": False}
        if any(self.heard(w) for w in person.words):
            return {"shown": shown, "label": label, "introduces": True}
        self.unintroduced.append((label, person.label))
        return {"shown": person.label, "label": person.label, "introduces": False, "unintroduced": label}

    # --- the player's own name ----------------------------------------------

    def player_name_in(self, npc_id: str | None, text: str | None, speaker: str = "") -> str | None:
        """`text`, a speaker line by `npc_id` (None: an ambient voice), as the
        player may hear it: without the player's name when this speaker
        never heard it (`cut_player_name`). Every speaker line of the turn
        comes through here, in order, so a name given this turn — by the
        player, in the prose, or said aloud by someone who knew it — is
        everyone's from then on, as `turn_audit.player_name_said` judges it
        after the turn. Never raises: a line it cannot judge goes as written.

        First, a still-sealed story truth said in the line is cut from it
        (`campaign.seal_line`, fleet9-bram T1: the act-2 roster figure in
        the first exchange) — `sealed_cuts` keeps what went."""
        out = text
        if text and npc_id:
            self._spoken_by[text] = npc_id
        if text and self._fiction and self.player_id:
            try:
                from src.services.campaign import seal_line

                # The line was heard whole just before (the server hears a
                # long line once, then sends it as two bubbles): judged whole
                whole = next((s for s in reversed(self._said_raw) if text in s), "")
                sealed = seal_line(self.player_id, npc_id, text, speaker=speaker,
                                   player_input=self._player_input, whole=whole)
                if sealed != text:
                    self.sealed_cuts.append({"speaker": speaker or "Someone", "npc_id": npc_id, "line": text})
                    self._unhear(text, sealed)
                    out = text = sealed
            except Exception as e:
                logger.warning(f"sealed-truth guard unavailable: {e}")
        try:
            if text and self._player_name:
                shown, cut = cut_player_name(text, self._player_name)
                if cut and not self._knows_player(npc_id) and not self._player_name_given():
                    out = shown
                    self.player_name_cuts.append((speaker or "Someone", cut[0]))
        except Exception as e:
            logger.warning(f"player's name left in a line uncut: {e}")
        self._lines.append({"npc_id": npc_id, "text": out or ""})
        return out

    def _knows_player(self, npc_id: str | None) -> bool:
        """Did this speaker know the player's name before the turn? Their row
        says so, or (no row, or a row that never heard it) the player named
        themself to them before, or they overheard it — the audit's own
        `met_before`. An ambient voice knows nobody."""
        if not npc_id:
            return False
        if npc_id in self._knew_player:
            return True
        known = self._knows_cache.get(npc_id)
        if known is None:
            try:
                from src.services import memory_files

                known = bool(memory_files.player_named_themself(self.player_id, npc_id, self._player_name)
                             or overheard_player_name(self.player_id, npc_id))
            except Exception as e:
                logger.debug(f"who knows the player's name unreadable for {npc_id}: {e}")
                known = True                    # never cut on a guess
            self._knows_cache[npc_id] = known
        return known

    def _player_name_given(self) -> bool:
        """Has the player's name been said in the scene this turn, so far?"""
        from src.web.turn_audit import player_name_said

        knew = {line["npc_id"] for line in self._lines if line.get("npc_id") and self._knows_player(line["npc_id"])}
        return player_name_said(self._lines, self._player_name, knew, "".join(self._prose), self._player_input)

    def next_turn(self, player_input: str = "") -> None:
        """A replay reads turn after turn through one gate (the chat history):
        a name said aloud in one turn's scene was heard there, not in every
        later scene — the lines and prose `player_name_in` weighs start over."""
        self._lines, self._prose = [], []
        self._player_input = player_input or ""

    def comments_on_cut_name(self, sentence: str) -> bool:
        """Is this prose sentence about a name this turn's lines lost?
        fleet7-tamsin T11: "She says your name like she's only just letting
        herself use it" — after the line that said it was cut, a remark on
        nothing. Quotes are the fiction's own words and never count. So is
        the slip told outright (`_NAME_SLIP`, fleet9-tamsin T11: "She catches
        herself using your name before remembering she was never told it")."""
        if not self.player_name_cuts or not sentence:
            return False
        bare = _QUOTE_SPAN.sub(" ", sentence)
        return (bool(_NAME_REMARK.search(bare)) and not _NAME_REMARK_DENIED.search(bare)) \
            or bool(_NAME_SLIP.search(bare))

    # --- the prose ----------------------------------------------------------

    def _compile(self, people: list[_Person]) -> None:
        counts: dict[str, int] = {}
        for p in people:
            for w in p.words:
                counts[w] = counts.get(w, 0) + 1
        forms: dict[str, _Person] = {}
        for p in people:
            full = " ".join(_EPITHET.sub(" ", p.name).split())
            bare = full.split()
            while len(bare) > 1 and bare[0].lower().strip(".") in _ALL_TITLES:
                bare = bare[1:]
            for form in (full, " ".join(bare), " ".join(p.words)):
                if form and len(form) > 2:
                    forms.setdefault(form, p)
            # One word alone is them only when it is nobody (and nothing) else
            for w in p.words:
                if counts.get(w) == 1 and w not in self._reserved:
                    forms.setdefault(w, p)
        self._forms = forms
        self._longest = max((len(f) for f in forms), default=0)
        if not forms:
            self._pattern = None
            return
        alternation = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
        self._pattern = re.compile(rf"{_NOT_AFTER_ARTICLE}(?:(?:{_TITLE_ALT})\.?\s+)?(?P<name>{alternation})(?![\w-])")

    def mask(self, text: str) -> str:
        """`text` with each person's unheard name replaced by what the player
        knows them by. Quotes are speech: heard, never rewritten."""
        if not text or self._pattern is None:
            return text
        spans = [(m.start(), m.end()) for m in _QUOTE_SPAN.finditer(text)] if self._fiction else []
        for start, end in spans:
            self.hear(text[start:end])
        out: list[str] = []
        pos = 0
        for m in self._pattern.finditer(text):
            if any(s <= m.start() < e for s, e in spans):
                continue
            person = self._forms[m.group("name")]
            written = [w for w in person.words if _said(w, m.group("name"))]
            before = text[:m.start()]
            opens = bool(_SENTENCE_OPEN.search(self._seen + before))
            if (len(written) == 1 and written[0].lower() in _WORD_NAMES and opens and m.group(0) == written[0]
                    and not _reads_as_person(person, written[0], text[m.end():])):
                continue                       # "Tack hangs by the door": the word, not the man
            if self._fiction and _INTRO_CUE.search(before[-60:]):
                self.hear(m.group(0))          # the fiction gave the name
                continue
            if all(self.heard(w) for w in written):
                continue
            heard = {w for w in person.words if self.heard(w)}
            if not heard and person.id in self._met:
                continue                       # met before the name rule: the name they were given
            if m.start() < pos:
                continue
            # "Hesk the hawker": the DM's own epithet IS the role — drop the
            # name and keep it, not "The Hawker the hawker" (fleet-tamsin T2).
            # The whole label must follow: "tell Coldiron the toll stands" is
            # no epithet
            epithet = None if heard else re.match(
                rf"\s+the\s+(?={re.escape(person.label)}(?![\w'’-]))", text[m.end():], re.I)
            if heard:
                shown = heard_form(person.name, heard)
            elif epithet:
                shown = f"{'The' if opens else 'the'} "
            elif _DETERMINER_BEFORE.search(before):
                shown = person.label
            else:
                shown = f"{'The' if opens else 'the'} {person.label}"
            out.append(text[pos:m.start()])
            out.append(shown)
            pos = m.end() + (epithet.end() if epithet else 0)
            self.masked.append((m.group(0), shown + person.label if epithet else shown))
        if not out:
            return text
        out.append(text[pos:])
        return "".join(out)

    def _could_grow(self, tail: str) -> bool:
        """Could more text turn `tail` (which starts a word) into a name?"""
        word = _WORD_NAME_TAIL.fullmatch(tail)
        if word and word.group(1) in self._forms and word.group(1).lower() in _WORD_NAMES:
            return True         # "Piper" + "'s face" or " nods": the next word decides
        head = _TITLE_HEAD.match(tail)
        rest = tail[head.end():] if head else tail
        if head and not rest:
            return True
        if not head and re.fullmatch(r"[A-Z][a-z]*\.?", rest) and any(
                t.startswith(rest.rstrip(".").lower()) for t in _ALL_TITLES):
            return True
        if rest and (rest[-1].isalnum() or rest[-1] in "'’-"):
            return any(f.startswith(rest) for f in self._forms)       # the word may go on
        return any(f.startswith(rest) and len(f) > len(rest) for f in self._forms)

    def _hold_from(self, buf: str) -> int:
        if self._pattern is None or not buf or buf.endswith("\n"):
            return len(buf)
        lo = max(0, len(buf) - self._longest - 16)
        for m in _WORD_START.finditer(buf, lo):
            if self._could_grow(buf[m.start():]):
                return m.start()
        return len(buf)

    def feed(self, text: str) -> str:
        """Stream filter: release `text` masked, holding back only a tail
        that could still become a name."""
        self._buf += text or ""
        cut = self._hold_from(self._buf)
        ready, self._buf = self._buf[:cut], self._buf[cut:]
        out = self.mask(ready)
        if out:
            self._seen = (self._seen + out)[-8:]
            self._prose.append(out)
        return out

    def flush(self) -> str:
        rest, self._buf = self._buf, ""
        out = self.mask(rest)
        if out:
            self._seen = (self._seen + out)[-8:]
            self._prose.append(out)
        return out


# ----------------------------------------------------------------------
# What the client shows outside the reply
# ----------------------------------------------------------------------
# fleet5 (all three runs, flagged major): the lead read "Get Captain
# Coldiron's gate pass for the north road" and "Ask Sorrel Tack what the
# stable yard hears after dark", the quest panel and the chips ("Talk to
# Brann") the same, while every speaker line said "Watch Captain" — each
# player asked "Coldiron?" at T1. Quest and beat text is authored with real
# names; it is rewritten here, where it leaves for the client, so the DM's
# context keeps them.

def player_facing_lead(lead: dict[str, Any] | None, names: NameGate | None) -> dict[str, Any] | None:
    """A copy of `state.lead` as the player may read it: "Get the Watch
    Captain's gate pass…" until "Coldiron" is said, then "Get Captain
    Coldiron's gate pass…" (never "Brann" unless that was said too)."""
    if names is None or not isinstance(lead, dict):
        return lead
    out = names.shown({k: v for k, v in lead.items() if k != "target"})
    if "target" in lead:
        target = lead["target"]
        if isinstance(target, dict):
            shown = names.shown(target)
            if target.get("type") == "npc":
                shown["name"] = names.name_for(target.get("id"), target.get("name"))
            target = shown
        out["target"] = target
    return out


def player_facing_state(state: dict[str, Any], names: NameGate | None) -> dict[str, Any]:
    """The HUD snapshot (`build_player_state`) as the player may read it:
    the lead, the quest titles and steps, the chips and the story panel.
    Rewritten in place; `names` is `NameGate.for_player` (None leaves it).
    Never raises: a HUD with real names beats no HUD."""
    if names is None or not isinstance(state, dict):
        return state
    try:
        return _player_facing_state(state, names)
    except Exception as e:
        logger.warning(f"player-facing state names skipped: {e}")
        return state


def _player_facing_state(state: dict[str, Any], names: NameGate) -> dict[str, Any]:
    if state.get("lead"):
        state["lead"] = player_facing_lead(state["lead"], names)
    quests = []
    for quest in state.get("quests") or []:
        if isinstance(quest, dict):
            steps = quest.get("objectives")
            quest = names.shown({k: v for k, v in quest.items() if k != "objectives"})
            if isinstance(steps, list):
                quest["objectives"] = [_player_facing_step(o, names) for o in steps]
        quests.append(quest)
    if state.get("quests"):
        state["quests"] = quests
    for key in ("suggestions", "campaign"):
        if state.get(key):
            state[key] = names.shown(state[key])
    for member in state.get("party") or []:
        if isinstance(member, dict) and member.get("id") and member.get("name"):
            member["name"] = names.name_for(member["id"], member["name"])
    # The pack too (fleet8-tamsin: the Gate Pass read "Captain Coldiron's
    # seal…" though nobody had said "Coldiron"): item names and descriptions,
    # what is worn, what Q drinks. Ids stay, so equip/use still find them
    if state.get("inventory"):
        state["inventory"] = player_facing_items(state["inventory"], names)
    if isinstance(state.get("equipment"), dict):
        state["equipment"] = names.shown(state["equipment"])
    combat = state.get("combat")
    if isinstance(combat, dict) and isinstance(combat.get("quick_slot"), dict):
        combat["quick_slot"] = names.shown(combat["quick_slot"])
    return state


def player_facing_items(items: Any, names: NameGate | None) -> Any:
    """Inventory/stock rows ({item_id, name, description, …}) as the player
    may read them: every text masked, ids and numbers untouched."""
    if names is None or not isinstance(items, list):
        return items
    return [names.shown(item) if isinstance(item, (dict, str)) else item for item in items]


def _player_facing_step(step: Any, names: NameGate) -> Any:
    if not isinstance(step, dict):
        return names.shown(step)
    out = names.shown(step)
    if step.get("target_type") == "npc" and step.get("target_name"):
        out["target_name"] = names.name_for(step.get("target_id"), step.get("target_name"))
    return out


# --- proof nobody has found yet ------------------------------------------
# fleet10-bram T1 / fleet10-tamsin T2: the quest log read "Bring the cracked
# bone whistle back to Captain Coldiron as proof" from the moment the job was
# taken, the Captain said "Find the whistle" at T1, and the find at T15 only
# confirmed what the screen had said for fifteen turns. A hand-over or pickup
# step whose item the player has never held — one lying in an authored Thing,
# or nowhere at all — that comes after a step asking a question (the item is
# what the asking finds; "Get the gate pass" first is a means) and that the
# job's own pitch never named (its title and description: the letter of "A
# Letter for a Buried Man" is no secret) is shown generically until the item
# enters the pack. The DM's own context keeps the item (the brief tags it:
# nobody names it yet).

def _proof_words(step: dict[str, Any], quest, names_of: dict[str, str]) -> str:
    """The generic wording of an unfound proof step."""
    if step.get("kind") == "deliver":
        who = step.get("target_name") or names_of.get(step.get("target_id") or "")
        return f"Bring proof back to {who}" if who else "Bring the proof back"
    giver = names_of.get(getattr(quest, "assigned_by_npc_id", None) or "")
    return f"Find proof for {giver}" if giver else "Find the proof"


def unfound_steps(session, player, quests: Any = None, *,
                  named_only: bool = True) -> dict[tuple[str, str], dict[str, Any]]:
    """{(quest id, step id): {"text": the generic wording, "item": the item,
    "quest_title", "step_text"}} for every undone obtain/deliver step of the
    `quests` (default: every active one) whose item the player has never
    held (see above). `named_only`: only the steps whose own words name the
    item — the ones the screen must reword; the DM's tag wants them all.
    {} on any trouble — a spoiled step beats no quest log."""
    from src.models import QuestStatus
    from src.services import objectives as objectives_mod
    from src.services.lead import item_words

    out: dict[tuple[str, str], dict[str, Any]] = {}
    if player is None:
        return out
    try:
        rows = list(quests) if quests is not None else objectives_mod.active_quests_for(session, player)
        names_of: dict[str, str] = {}
        for quest in rows:
            if getattr(quest, "status", QuestStatus.ACTIVE) != QuestStatus.ACTIVE:
                continue
            never = [r for r in objectives_mod.undone_in_play(session, quest, player)
                     if r["kind"] in ("obtain", "deliver") and r["why"].startswith("they have never held")]
            if not never:
                continue
            pitch = item_words(f"{quest.title or ''} {quest.description or ''}")
            ordered = objectives_mod.normalize_objectives(quest.objectives)
            steps = {o["id"]: o for o in ordered}
            for row in never:
                step = steps.get(row["id"])
                item = (step or {}).get("item_name")
                if not step or not item:
                    continue
                # Only what a question before it is to find: "Get the gate
                # pass" first is the means, never the answer
                before = ordered[:ordered.index(step)]
                if not any(objectives_mod.investigates(o) for o in before):
                    continue
                head = objectives_mod._head_noun(item)
                if head and head in pitch:
                    continue                       # the job is about this very thing
                if named_only and not item_words(item) & item_words(step["text"]):
                    continue                       # its words already name no item
                for npc_id in (step.get("target_id"), quest.assigned_by_npc_id):
                    if npc_id and npc_id not in names_of:
                        npc = session.get(NPC, npc_id)
                        names_of[npc_id] = npc.name if npc is not None else ""
                out[(quest.id, step["id"])] = {"text": _proof_words(step, quest, names_of), "item": item,
                                               "quest_title": quest.title, "step_text": step["text"]}
    except Exception as e:
        logger.warning(f"unfound proof steps unread: {e}")
        return {}
    return out


def hide_unfound(value: Any, hidden: dict[tuple[str, str], dict[str, Any]]) -> Any:
    """A quest list (dicts with "id" and "objectives"), a lead (quest_id +
    objective_id) or a single quest, with each unfound proof step worded
    generically and its item name dropped. Copies; never raises."""
    if not hidden:
        return value
    try:
        if isinstance(value, list):
            return [hide_unfound(v, hidden) for v in value]
        if not isinstance(value, dict):
            return value
        if value.get("quest_id") and value.get("objective_id"):        # a lead
            hit = hidden.get((value["quest_id"], value["objective_id"]))
            # ...worded as the step: a lead pointing at a Thing here ("Take
            # the cracked bone whistle here") names what the canvas draws
            if hit is None or value.get("text") != hit["step_text"]:
                return value
            return {**value, "text": hit["text"], "item_name": None}
        if value.get("id") and isinstance(value.get("objectives"), list):
            steps = []
            for step in value["objectives"]:
                hit = hidden.get((value["id"], step.get("id"))) if isinstance(step, dict) else None
                steps.append({**step, "text": hit["text"], "item_name": None} if hit else step)
            return {**value, "objectives": steps}
    except Exception as e:
        logger.warning(f"unfound proof steps not hidden: {e}")
    return value


def shown_step_texts(session, player, quest) -> list[str]:
    """Every step of `quest` as the player's quest log shows it — what they
    have read there, so the sealed-word audit counts these words as met
    and no others (fleet9-tamsin T18's "whistle" was met through a step
    that should never have said it)."""
    from src.services.objectives import normalize_objectives

    hidden = unfound_steps(session, player, [quest]) if player is not None else {}
    out = []
    for step in normalize_objectives(quest.objectives):
        hit = hidden.get((quest.id, step["id"]))
        out.append(hit["text"] if hit else step["text"])
    return out


def player_facing_people(player_id: str | None, people: list[Any]) -> list[Any]:
    """`npcs_present` rows ({id, name, profession}) as the player may read
    them: the name as far as heard, and no trade for someone still a
    stranger ("Ashen Fangs scout" is who they are, not what they show)."""
    names = NameGate.for_player(player_id) if people else None
    if names is None:
        return people
    out = []
    try:
        for row in people:
            if isinstance(row, dict) and row.get("id") and row.get("name"):
                shown = names.name_for(row["id"], row["name"])
                person = names._persons.get(row["id"])
                row = {**row, "name": shown}
                if person is not None and person.label and shown == person.label:
                    row["profession"] = None
            out.append(row)
    except Exception as e:
        logger.warning(f"player-facing roster names skipped: {e}")
        return people
    return out


def player_facing(player_id: str | None, value: Any, names: NameGate | None = None, *,
                  heard_now: str = "") -> Any:
    """Any other client payload (quest/objective/story toasts, the quest
    list, the story panel) as the player may read it. Builds the gate only
    when there is something to rewrite. Never raises."""
    if not value:
        return value
    try:
        names = names or NameGate.for_player(player_id, heard_now)
        return names.shown(value) if names is not None else value
    except Exception as e:
        logger.warning(f"player-facing names skipped: {e}")
        return value


_WORLD_WORD = re.compile(r"[A-Za-zÀ-ÿ][\w'’-]*")


def world_words(session) -> tuple[set[str], set[str]]:
    """(names, words) the world already holds, for the unknown-person audit.

    `names`: every capitalised word of every authored row — NPCs, places,
    factions, quests, items, the world bible and its history, the campaign
    and its beats, events, the player — lowercased, possessive dropped. A
    person a dossier, a secret or the lore mentions is not the DM's
    invention. `words`: every word written in lowercase there (a word that
    is ever written small is an ordinary word). Every string and JSON column
    is read, so a new column needs no change here.
    """
    import json

    from src.models import Campaign, Event, Faction, HistoricalEvent, Item, Location, Quest, StoryBeat, WorldBible

    names: set[str] = set()
    words: set[str] = set()
    for model in (NPC, Location, Faction, Quest, Item, WorldBible, HistoricalEvent, Campaign, StoryBeat, Event, Player):
        try:
            columns = [c.key for c in model.__table__.columns if c.key != "id" and not c.key.endswith("_id")]
            for row in session.query(model).all():
                for key in columns:
                    value = getattr(row, key, None)
                    if isinstance(value, (list, dict)):
                        value = json.dumps(value, ensure_ascii=False)
                    if not isinstance(value, str) or not value:
                        continue
                    for w in _WORLD_WORD.findall(value):
                        bare = _POSSESSIVE_END.sub("", w)
                        (names if w[0].isupper() else words).add(bare.lower())
        except Exception as e:
            logger.debug(f"world words: {getattr(model, '__name__', model)} unreadable: {e}")
    return names, words


# --- confessions -----------------------------------------------------------
# Round-2 autoplay: two NPCs told their secrets in plain dialogue without the
# DM writing @reveal, so nothing was recorded — the secret stayed "hidden" in
# their dossier and Coldiron confessed the same thing three turns running.
# A secret an NPC was ready to share (trust gate passed) that their own words
# now cover is marked told, and the player's journal gets it.

_CONFESS_STOP = frozenset({
    "that", "this", "with", "from", "they", "them", "their", "there", "have", "been", "were",
    "what", "when", "where", "which", "would", "could", "should", "about", "into", "only",
    "never", "ever", "than", "then", "your", "yours", "will", "just", "know", "knows",
})
MIN_CONFESS_WORDS = 3
CONFESS_SHARE = 0.5


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in _CONFESS_STOP}


def note_confessions(player_id: str, speeches: list[dict[str, Any]]) -> list[str]:
    """Mark secrets the speakers just told (their words cover most of one).

    Only secrets the trust gate already allows (`standing.secret_refusal` is
    None) — a DM below the gate never saw the words. Returns the confessed
    secrets' text. Never raises.
    """
    told: list[str] = []
    try:
        from src.models import NPCRelationship
        from src.services import standing

        by_npc: dict[str, str] = {}
        for s in speeches or []:
            if isinstance(s, dict) and s.get("npc_id") and isinstance(s.get("text"), str):
                by_npc[s["npc_id"]] = by_npc.get(s["npc_id"], "") + " " + s["text"]
        if not by_npc:
            return told
        with get_session() as session:
            for npc_id, words in by_npc.items():
                npc = session.get(NPC, npc_id)
                if npc is None or not npc.secrets:
                    continue
                rel = session.query(NPCRelationship).filter(
                    NPCRelationship.npc_id == npc_id, NPCRelationship.player_id == player_id
                ).first()
                if rel is None or standing.secret_refusal(npc, rel) is not None:
                    continue
                said = _content_words(words)
                revealed = list(rel.revealed_secrets or [])
                for index, secret in enumerate(npc.secrets or []):
                    if index in revealed or not isinstance(secret, str):
                        continue
                    keys = _content_words(secret)
                    hits = keys & said
                    if len(keys) >= MIN_CONFESS_WORDS and len(hits) >= max(MIN_CONFESS_WORDS,
                                                                       CONFESS_SHARE * len(keys)):
                        revealed.append(index)
                        told.append(secret)
                rel.revealed_secrets = revealed            # reassign: JSON column
            session.commit()
        if told:
            from src.services import memory_files

            for secret in told:
                memory_files.add_journal_entry(player_id, secret)
            logger.info(f"Confessed without @reveal, recorded: {told}")
    except Exception as e:
        logger.warning(f"confession check failed: {e}")
    return told

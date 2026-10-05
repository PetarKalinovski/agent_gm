"""Suggested actions — two to four chips under the chat input, derived in code.

A blank text box is a blank page. The chips name what is actually possible
right now, in this order: the lead (the one thing the tracker shows), the
people standing here, the ways out, the things on the floor, and "look
around" to pad to two. Nothing is authored at runtime; nothing costs a
model call. Chips that resolve on the canvas (a door, an object) are
routed by the client through the same REST paths the canvas uses — no DM
turn — and the rest just fill the input.

Chips never name someone the player has not met (playtest 2026-10-04: "Talk
to Vex" and "Talk to Aldric" handed a player the villain's name on turn 9).
A stranger is approached by their role label ("Approach the ash-cloaked
man"), and the story's villain, their successor and any boss get no chip
at all until they have introduced themselves. One slot is kept for the
place's things: four people-and-doors chips once crowded out every authored
clue at the Hollow.

Shape: {"label", "text", "kind": lead|fight|party|talk|trade|travel|interact|look,
        "npc_id"?, "location_id"?, "object_id"?, "locked"?}
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from src.models import Location, NPC
from src.services import scene_objects
from src.services.travel import check_route
from src.tools.world_read.locations import get_available_destinations

logger = logging.getLogger(__name__)

MAX_CHIPS = 4
MIN_CHIPS = 2

_VERB_LABELS = {"take": "Take", "read": "Read", "open": "Open", "search": "Search",
                "body": "Search", "rest": "Rest at"}
# A `read` object is anything with a reveal panel — a well, a statue, a mural
# too. "Read the Town Well" (playtest 2026-10-04) reads as a bug, so the chip
# says Read only for things that carry writing and Examine for the rest.
_WRITTEN = re.compile(
    r"\b(?:notice|letter|sign|signpost|book|notebook|logbook|note|board|noticeboard|map|chart|"
    r"scroll|poster|placard|handbill|pamphlet|inscription|plaque|ledger|register|journal|diary|"
    r"tome|manuscript|parchment|page|list|warrant|decree|bulletin|missive|epitaph|headstone|"
    r"gravestone|tombstone|rune|runestone|graffiti|writing|menu|recipe|contract|deed|receipt)s?\b",
    re.I,
)
_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.I)
_LEADING_VERB = re.compile(
    r"^(?:find(?: out)?|ask|talk to|speak (?:to|with)|go to|reach|get to|bring|deliver|"
    r"kill|obtain|get|take|search|seek|visit|meet)\s+", re.I,
)


_HONORIFICS = frozenset({"captain", "sir", "lady", "lord", "master", "old", "the", "mister", "miss",
                         "mother", "father"})


def _first_name(name: str) -> str:
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    if not parts:
        return "them"
    # "Captain Brann Coldiron" -> "Brann"; "Maera" -> "Maera"
    for part in parts:
        if part.lower().strip(".") not in _HONORIFICS:
            return part
    return parts[-1]


def _names_person(topic: str, name: str) -> bool:
    """True when a lead's topic is just the person: "Talk to Captain Brann
    Coldiron" strips to "Captain Brann Coldiron", and the chip would read
    "Ask Brann about Captain Brann Coldiron"."""
    words = set(re.findall(r"[a-z']+", (topic or "").lower()))
    distinctive = {w for w in re.findall(r"[a-z']+", (name or "").lower())
                   if len(w) > 2 and w not in _HONORIFICS}
    return bool(words & distinctive)


def _topic(lead: dict[str, Any]) -> str:
    """'Find out what is organizing the wolves' -> 'what is organizing the wolves'."""
    text = str(lead.get("text") or "").strip().rstrip(".")
    stripped = _LEADING_VERB.sub("", text)
    if stripped and stripped != text:
        return stripped
    return str(lead.get("quest_title") or text)


def _lead_chip(lead: dict[str, Any] | None, exits_by_id: dict[str, dict[str, Any]],
               alias_for: Callable[[str | None], str | None] = lambda _id: None,
               known_for: Callable[[str | None], str | None] = lambda _id: None) -> dict[str, Any] | None:
    # A lead still ahead on the clock ("Tomorrow morning: ...") is nothing to
    # do now (lead.beat_due_later)
    if not lead or not lead.get("text") or lead.get("due"):
        return None
    target = lead.get("target") or {}
    hop = lead.get("route_next_hop_location_id")
    if target.get("type") == "npc" and target.get("here"):
        return _talk_lead_chip(lead, target, alias_for(target.get("id")), known_for(target.get("id")))
    if target.get("type") == "object" and target.get("here") and target.get("id"):
        # The quest's proof lying here (lead._proof_lead): the canvas's own
        # [E], no DM turn — a typed "I take the cracked bone whistle here"
        # cost one; the things-here chip for it folds into this one
        return {"kind": "interact", "object_id": target["id"], "label": str(lead["text"])[:60],
                "text": f"I {_lower_first(lead['text'])}"}
    if hop and hop in exits_by_id:
        ex = exits_by_id[hop]
        return {
            "kind": "travel", "location_id": hop, "locked": bool(ex.get("locked")),
            "label": f"Head to {ex.get('name')}"[:60],
            "text": f"I head to {ex.get('name')}.",
        }
    return {"kind": "lead", "label": str(lead["text"])[:60], "text": f"I {_lower_first(lead['text'])}"}


def _talk_lead_chip(lead: dict[str, Any], target: dict[str, Any], alias: str | None = None,
                    known: str | None = None) -> dict[str, Any]:
    """The lead is someone standing right here: word the chip for them.

    `alias` ("the ash-cloaked man") stands in for a person the player has
    not met — unless the lead itself already told them the name (only a
    caller without `known`: the lead the client shows is name-gated now).
    `known` is the name as far as the player has heard it ("Captain
    Coldiron" when only "Coldiron" was said).
    """
    name = str(target.get("name") or "")
    text = str(lead.get("text") or "").strip().rstrip(".")
    if alias and known is None and (_names_person(text, name)
                                    or _names_person(str(lead.get("quest_title") or ""), name)):
        alias = None
    shown = known or name
    first, full = (alias, alias) if alias else (_first_name(shown), shown)
    chip = {"kind": "talk", "npc_id": target.get("id")}
    if re.match(r"ask\s", text, re.I):
        # Already a question to someone ("Ask the Captain what the howling means")
        return {**chip, "label": text[:60], "text": f"I {_lower_first(text)}"}
    if lead.get("kind") == "kill":
        # A target to bring down is not a conversation ("Talk to Grey")
        who = alias or _npc_phrase(shown)
        return {"kind": "lead", "npc_id": target.get("id"),
                "label": f"Attack {who}"[:60], "text": f"I attack {who}."}
    item = str(lead.get("item_name") or "").strip()
    if lead.get("kind") == "deliver" and item:
        thing = _object_phrase(item)
        return {**chip, "label": f"Give {first} {thing}"[:60], "text": f"I give {full} {thing}."}
    topic = _topic(lead)
    if _names_person(topic, name):
        quest = str(lead.get("quest_title") or "").strip()
        if not quest or _names_person(quest, name):
            return {**chip, **_talk_words(first, full, stranger=bool(alias))}
        topic = quest
    return {**chip, "label": f"Ask {first} about {topic}"[:60], "text": f"I go to {full} and ask about {topic}."}


def _talk_words(first: str, full: str, *, stranger: bool) -> dict[str, str]:
    """Chip words: "Talk to Brann" for someone known; a stranger is approached, not named."""
    if stranger:
        return {"label": f"Approach {full}"[:60], "text": f"I approach {full}."}
    return {"label": f"Talk to {first}"[:60], "text": f"I approach {full} and start talking."}


def _stranger_phrase(label: str) -> str:
    """'Ash-Cloaked Man' -> 'the ash-cloaked man' mid-sentence."""
    return f"the {label.lower()}"


def _lower_first(text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    body = text[0].lower() + text[1:]
    return body if body.endswith((".", "!", "?")) else body + "."


def object_verb(kind: str | None, label: str | None) -> str:
    """The chip verb for a scene object: its interaction, worded for the thing."""
    if kind == "read":
        return "Read" if _WRITTEN.search(label or "") else "Examine"
    return _VERB_LABELS.get(kind or "", "Use")


_POSSESSIVE = re.compile(r"\S(?:['’]s|s['’])(?:\s|$)", re.I)


def _object_phrase(label: str | None) -> str:
    """'Notice board' -> 'the Notice board'; 'A torn letter' -> 'the torn letter'.

    A possessive takes no article: loot.body_label writes "<NPC name>'s body",
    and "Search the Captain Brann Coldiron's body" reads as a bug — unless the
    label itself opened with one ("The Grey Alpha's body" -> "the Grey Alpha's
    body")."""
    raw = " ".join(str(label or "").split())
    text = _ARTICLE.sub("", raw) or "thing"
    if _POSSESSIVE.search(text) and text == raw:
        return text
    return f"the {text}"


def _npc_phrase(name: str) -> str:
    """'The Grey Alpha' -> 'the Grey Alpha' mid-sentence; names stay names."""
    name = " ".join(str(name or "").split()) or "them"
    return re.sub(r"^The\s", "the ", name)


def _story_villains(session, player) -> set[str]:
    """The campaign's villain and successor — never chipped before they are met."""
    try:
        from src.services.campaign import get_campaign_row

        campaign = get_campaign_row(session, getattr(player, "id", None))
        if campaign is not None:
            return {i for i in (campaign.villain_npc_id, campaign.successor_npc_id) if i}
    except Exception as e:
        logger.debug(f"campaign villains unavailable: {e}")
    return set()


def _plural(word: str) -> str:
    low = word.lower()
    if low.endswith("f"):
        return low[:-1] + "ves"          # wolf -> wolves
    return low + ("es" if low.endswith(("s", "x", "ch", "sh")) else "s")


def _fight_chip(session, player, stranger: Callable[[Any], str | None], chips: list[dict[str, Any]],
                known: Callable[[Any], str] = lambda npc: npc.name,
                hidden: set[str] | frozenset = frozenset()) -> dict[str, Any] | None:
    """"Fight the wolves" / "Fight the ash-cloaked man": the encounter
    director's own roster of who here would fight (beasts, the hostile —
    never a party member, the story's villain or a boss, nor anyone `hidden`
    offstage), unless a parley is under way or the lead already offers the
    attack."""
    from src.services import encounters
    from src.services.dialogue import is_beast

    if encounters.in_parley(getattr(player, "id", None)):
        return None
    enemies = [e for e in encounters.enemies_here(session, player, beasts_only=False) if e.id not in hidden]
    if not enemies:
        return None
    npc = enemies[0]
    # A lead on any of them already offers this fight ("Attack the Hollow
    # Wolf" beside "Fight the wolves" is one choice in two chips)
    if any(c.get("kind") == "lead" and c.get("npc_id") in {e.id for e in enemies} for c in chips):
        return None
    tails = {e.name.split()[-1].lower() for e in enemies if is_beast(e)}
    if len(enemies) > 1 and len(tails) == 1 and all(is_beast(e) for e in enemies):
        phrase = f"the {_plural(next(iter(tails)))}"             # "the wolves"
    elif is_beast(npc):
        phrase = _npc_phrase(npc.name if npc.name.lower().startswith("the ") else f"the {npc.name}")
    else:
        label = stranger(npc)
        phrase = _stranger_phrase(label) if label else _npc_phrase(known(npc))
    return {"kind": "fight", "npc_id": npc.id, "label": f"Fight {phrase}"[:60], "text": f"I attack {phrase}."}


def suggest_actions(session, player, lead: dict[str, Any] | None,
                    location_id: str | None, names=None) -> list[dict[str, Any]]:
    """The chips for this moment. Never raises.

    `names` (`dialogue.NameGate.for_player`) words them with the names the
    player has heard: fleet5-wenna's square offered "Talk to Brann" when only
    "Coldiron" had ever been said, and the lead chip carried the lead's real
    names ("Ask Sorrel Tack what…") before anyone spoke them.
    """
    try:
        return _suggest(session, player, lead, location_id, names)
    except Exception as e:
        logger.warning(f"suggestions failed: {e}")
        return []


def _suggest(session, player, lead, location_id, names=None) -> list[dict[str, Any]]:
    chips: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(chip: dict[str, Any] | None) -> None:
        if not chip or len(chips) >= MAX_CHIPS:
            return
        key = (chip["kind"], str(chip.get("npc_id") or chip.get("location_id") or chip.get("object_id") or chip["label"]))
        if key in seen:
            return
        seen.add(key)
        chips.append(chip)

    from src.services.dialogue import is_beast, player_has_met, stranger_label

    player_id = getattr(player, "id", None)
    villains = _story_villains(session, player)
    strangers: dict[str, str | None] = {}

    def stranger(npc) -> str | None:
        """Their role label while the player has no name for them, else None."""
        if npc.id not in strangers:
            strangers[npc.id] = stranger_label(npc, met=player_has_met(session, player_id, npc.id))
        return strangers[npc.id]

    def unchipped(npc) -> bool:
        """The villain (or any boss) the player has not met stays off the chips
        — even one whose label is their name ("The Whistler"), which no
        role label would hide."""
        return ((npc.id in villains or (npc.danger_tier or "").lower() == "boss")
                and not player_has_met(session, player_id, npc.id))

    def alias_for(npc_id: str | None) -> str | None:
        npc = session.get(NPC, npc_id) if npc_id else None
        label = stranger(npc) if npc is not None else None
        return _stranger_phrase(label) if label else None

    def known(npc) -> str:
        """Their name as far as the player has heard it ("Captain Coldiron")."""
        return names.known_as(npc) if names is not None else npc.name

    def known_for(npc_id: str | None) -> str | None:
        npc = session.get(NPC, npc_id) if npc_id and names is not None else None
        return known(npc) if npc is not None else None

    if names is not None and lead:
        # Worded from the lead the client shows: masked before it is cut to
        # a chip's length, so no name is ever half-shown
        from src.services.dialogue import player_facing_lead
        lead = player_facing_lead(lead, names)

    exits_by_id: dict[str, dict[str, Any]] = {}
    location = session.get(Location, location_id) if location_id else None
    if location is not None:
        try:
            for d in get_available_destinations(location.id) or []:
                if not isinstance(d, dict) or not d.get("id"):
                    continue
                try:
                    route = check_route(session, location.id, d["id"], player)
                    locked = bool(route.get("blocked"))
                except Exception:
                    locked = bool(d.get("requirements"))
                if not str(d.get("name") or "").strip():
                    continue  # "Head to ?" is not a choice
                exits_by_id[d["id"]] = {"id": d["id"], "name": d["name"], "locked": locked}
        except Exception as e:
            logger.debug(f"exits for suggestions unavailable: {e}")

    # 1. The lead
    add(_lead_chip(lead, exits_by_id, alias_for, known_for))

    npcs: list = []
    hidden: set[str] = set()
    if location is not None:
        npcs = session.query(NPC).filter(
            NPC.current_location_id == location.id, NPC.status == "alive"
        ).all()
        # Nobody the story holds offstage gets a chip: the canvas leaves them
        # out (`campaign.offstage_ids`), and "Approach the ash-cloaked man"
        # would put them back on screen (round-9 review: the villain's people
        # held for their entrance had talk and fight chips)
        try:
            from src.services.campaign import offstage_ids
            hidden = offstage_ids(session, player_id) if player_id else set()
        except Exception as e:
            logger.debug(f"offstage people unavailable for chips: {e}")
        npcs = [npc for npc in npcs if npc.id not in hidden]

    # 1a. A fight on offer: a beast or someone hostile stands here and no talk
    # is under way. fleet5-bram, a brawler who "picks fights he might not
    # win", was raked by a wolf in the prose and never offered the fight
    if location is not None and player is not None:
        add(_fight_chip(session, player, stranger, chips, known, hidden))

    # 1b. Someone here who would come along (the engine's own gate says yes)
    if location is not None and player is not None:
        from src.services import party as party_mod, standing
        member_ids = set(party_mod.member_ids(player))
        if len(member_ids) < party_mod.MAX_PARTY:
            for npc in npcs:
                if npc.id in member_ids:
                    continue
                rel = standing.relationship_for(session, getattr(player, "id", None), npc.id)
                if rel is None or standing.join_refusal(npc, rel):
                    continue
                add({
                    "kind": "party", "npc_id": npc.id,
                    "label": f"Ask {_first_name(known(npc))} to come along"[:60],
                    "text": f"I ask {known(npc)} to travel with me.",
                })
                break

    # 2. People here (a wolf is not someone to chat with — "Talk to Hollow"
    # was offered for the Hollow Wolf on the 2026-10-04 playtest; the same
    # test as the labels', so "Commander Fox" is still someone to talk to)
    if location is not None:
        approached: set[str] = set()
        for npc in npcs:
            try:
                if is_beast(npc):
                    continue
            except Exception:
                pass
            if unchipped(npc):
                continue
            label = stranger(npc)
            if label:
                if label in approached:
                    continue    # two "Approach the hooded figure" chips are one choice
                approached.add(label)
                phrase = _stranger_phrase(label)
                add({"kind": "talk", "npc_id": npc.id, **_talk_words(phrase, phrase, stranger=True)})
                continue
            add({"kind": "talk", "npc_id": npc.id, **_talk_words(_first_name(known(npc)), known(npc), stranger=False)})

    # 2b. A counter to trade at
    if location is not None:
        for npc in npcs:
            if npc.inventory_notable and not unchipped(npc):
                label = stranger(npc)
                short, full = ((_stranger_phrase(label), _stranger_phrase(label)) if label
                               else (_first_name(known(npc)), known(npc)))
                add({
                    "kind": "trade", "npc_id": npc.id,
                    "label": f"Trade with {short}"[:60],
                    "text": f"I look over what {full} has for sale.",
                })

    # 3. Ways out (unlocked first)
    for ex in sorted(exits_by_id.values(), key=lambda e: (e["locked"], e["name"])):
        add({
            "kind": "travel", "location_id": ex["id"], "locked": ex["locked"],
            "label": f"Head to {ex['name']}"[:60],
            "text": f"I head to {ex['name']}." if not ex["locked"] else f"I try to get through to {ex['name']}.",
        })

    # 4. Things here — with a slot kept for them: when people and doors
    # filled all four, the last door (the canvas shows doors anyway) or
    # else the last chip after the lead gives way to the first thing
    things: list[dict[str, Any]] = []
    if location is not None:
        for obj in scene_objects.public_objects(location.objects):
            if obj.get("consumed"):
                continue
            verb = object_verb(obj.get("kind"), obj.get("label"))
            thing = _object_phrase(obj.get("label"))
            things.append({
                "kind": "interact", "object_id": obj["id"],
                "label": f"{verb} {thing}"[:60],
                "text": f"I {verb.lower()} {thing}.",
            })
    for chip in things:
        add(chip)
    if things and len(chips) > 1 and not any(c["kind"] == "interact" for c in chips):
        doors = [i for i, c in enumerate(chips) if i > 0 and c["kind"] == "travel"]
        chips[doors[-1] if doors else len(chips) - 1] = things[0]

    # 5. Never fewer than two
    if len(chips) < MIN_CHIPS:
        add({"kind": "look", "label": "Look around", "text": "I take a careful look around."})
    return chips[:MAX_CHIPS]

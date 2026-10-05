"""The world strikes first: ambushes decided by the engine, not the DM.

Three AI-played sessions on 2026-10-04 (63 turns, one of them a brawler who
"picks fights he might not win") produced ZERO fights. The DM kept every
confrontation conversational, the player model backed down, and the
real-time combat the game is built around never happened. A world of wolf
packs and armed scouts has to be able to bite on its own.

The director rolls once per DM turn and once per canvas arrival:

  * Who: enemies in the room — alive, able to hurt someone, not in the
    party, not the campaign's villain or successor (their fight is the
    story's), and either beasts or people who are hostile to the player
    (disposition, or a faction that hates them).
  * When: arriving somewhere only BEASTS jump you (people talk first); once
    the player lingers, anyone hostile may. Night and high tension raise the
    odds; a fight the engine just ran sets a cooldown.
  * What: the engine starts the fight (the same roster `@fight` builds) and
    tells the DM to write one freeze-frame sentence of it breaking out.
  * Not mid-parley: a hostile voice talking with the player — or the player
    reaching for words with someone of the danger's side present — holds
    the lingering roll for a turn, at most PARLEY_MAX_SKIPS turns in a row.
  * A place that already ambushed the player sends someone (or some tier)
    it has not sent yet, when the room allows; a night-edge scout the
    complication director made sits out its first roll (a choice first).
  * Never anyone a story beat still to play needs as written (round 6:
    the ambush killed the wolves an act-3 setpiece stages), and not while
    the player answers a choice the last reply put to them about the very
    danger in front of them — once, unless the answer is a blow.
  * What the player won stays won: a roll that went their way against a
    danger here (calming it, facing it down, slipping past it), or a
    standoff the reply settled peacefully, holds that danger out of the
    next CALM_HOLD_ROLLS rolls (fleet8-wenna T11 -> T12: the scout wolf she
    calmed on a real roll was the next reply's ambush).
  * A beast the player beat on a roll, or one the reply's prose sent off
    into the dark, leaves the scene: the engine moves it home after the
    reply (`send_off`) — fleet9-wenna T14 fed the wolf and the prose had it
    "back… into the black… and is gone", the database kept it at the fence,
    and T16 forced the fight. A roll LOST against a beast here provokes it:
    the next roll is its lunge, whatever the odds (fleet9-tamsin T13's fight
    came without a die cast; now a die decides).
  * A thief in sight is quarry for a reply or two (`mark_quarry`): the fight
    chip offers them, the director never sends them (fleet9-bram T11).

Every probability lives in this file.
"""

from __future__ import annotations

import logging
import random
import re
from typing import Any

from src.models import NPC, Location, Player, get_session

logger = logging.getLogger(__name__)

ARRIVAL_CHANCE = {"day": 0.30, "night": 0.55}
LINGER_STEP = 0.15            # per extra turn spent in the same dangerous place
LINGER_CAP = 0.70
TENSION_BONUS = {"high": 0.15, "climax": 0.25}
COOLDOWN_TURNS = 4            # turns after an engine ambush before another
MAX_AMBUSHERS = 3
NIGHT_HOURS = set(range(20, 24)) | set(range(0, 6))
SAFE_TIERS = {"harmless"}
# A parley holds the lingering roll this many turns in a row, then danger
# gets its say again — talking cannot stall a wolf pack forever
PARLEY_MAX_SKIPS = 2
# A danger the player won against sits out this many rolls (`note_check`)
CALM_HOLD_ROLLS = 2
WON_OUTCOMES = ("critical_success", "success", "partial")
# Skills that face a danger down without naming it: a won sneak, word or
# trick in a room with a danger in it was against that danger
_FACING_SKILLS = ("persuasion", "cunning", "stealth")

# Per player: (location_id, turns spent there), and turns since the last ambush
_linger: dict[str, tuple[str | None, int]] = {}
_since_ambush: dict[str, int] = {}
# Per player: did the last DM reply have a hostile voice talking with them,
# and how many lingering rolls in a row that talk has already held off
_parley: dict[str, bool] = {}
_parley_skips: dict[str, int] = {}
# The last reply's verdict as asked by others (in_parley). Kept apart from
# `_parley`, which a held roll spends: the roll runs before the context is
# built, so the complication director read "no talk" mid-parley
_talking: dict[str, bool] = {}
# NPCs the director leaves out of the next N rolls: a night-edge scout the
# complication director just made is a choice first, not an ambush
_held: dict[str, int] = {}
# Per player: who the director already sent at them, by place — a second
# visit draws someone (or some tier) else when the room allows
_drawn: dict[str, dict[str | None, set[str]]] = {}
# Per player: the hostiles the last reply left them facing with a choice put
# to them (`note_parley`), and whether the last roll was held for one
_standoff: dict[str, set[str]] = {}
_standoff_held: dict[str, bool] = {}
# ...and who the held roll left them answering: a reply that settles that
# standoff peacefully holds them on (`note_parley`)
_answering: dict[str, set[str]] = {}
# Per player: the beasts a roll won this reply (`note_check`) — after the
# reply they slink off out of the scene (`send_off`) — and those a lost roll
# provoked: the next roll is their lunge, whatever the odds
_calmed_now: dict[str, set[str]] = {}
_provoked: dict[str, set[str]] = {}
# Per player: someone to chase or fight for a reply or two (a thief in
# sight), with the replies left — offered by the fight chip, never sent
_quarry: dict[str, dict[str, int]] = {}
QUARRY_REPLIES = 2
# Skills a lost roll against a beast provokes it with (a missed look does not)
_PROVOKING_SKILLS = ("persuasion", "cunning", "stealth", "athletics")
LOST_OUTCOMES = ("failure", "critical_failure")
# The player facing a beast down holds the lingering roll — the die decides
# it (roll_intent orders the roll first) — this many replies in a row
# without a roll at most: a DM that never rolls cannot stall the pack
STANDOFF_MAX_HOLDS = 2
_stood: dict[str, int] = {}
# A danger shown first, not sprung: the player turning to go — leaving,
# heading back, retreating — when the roll would strike. fleet10-tamsin T18
# "I turn back toward the gate, moving as fast as my knee allows" and the
# Torn-Ear Wolf was "already airborne": no roll, no choice, five turns after
# the last fight. Eyes in the dark, a growl on the path; the stand-off or the
# fight is the player's answer, the reply after. Shown once: a danger they
# were shown and walked on from within WARNED_REPLIES comes for them
# (`_warned`). Per player: (reply, ids) shown this reply, and the reply each
# danger was shown at since the last fight
WARNED_REPLIES = 3
_shown_now: dict[str, tuple[int, set[str]]] = {}
_warned: dict[str, dict[str, int]] = {}
# The context line for a danger shown first opens with this: the travel line
# stays out of that reply (dm_orchestrator), so the engine walks nobody on
SHOWN_FIRST_MARK = "[DANGER ON THE WAY OUT"
# Complications whose person the player already knows (complications._entity:
# the one leaving is here and met; a summons names a met person elsewhere)
_KNOWN_ALREADY = ("leaving", "summons")
# ...and a place that just fought the player strikes at reduced odds for a
# few turns after the cooldown (the same T18: p=0.85 at the den, lingering
# at night, the turn the cooldown ran out). Per player: where the last fight began
AFTER_FIGHT_TURNS = 4
AFTER_FIGHT_DAMP = 0.5
_fought_at: dict[str, str | None] = {}
# Turning to face someone ("I turn toward Sorrel", "turn back toward the
# Captain at the board") is no leaving: a turn counts with the going in it
_LEAVING = re.compile(
    r"\b(?:turn|turns|turning)\s+(?:back\s+|around\s+|away\s+)?(?:toward|towards|to)\s+[^.!?\n]{0,50}?"
    r"(?:,\s*|\s+and\s+)(?:moving|walking|heading|hurrying|limping|striding|breaking\s+into|picking\s+up)\b"
    r"|\b(?:turn|turns|turning)\s+(?:to\s+(?:go|leave)|and\s+(?:go|head|leave|walk|make|start|limp|hurry|run)"
    r"|on\s+my\s+heel)\b"
    r"|\b(?:head|heads|heading|start|starts|starting|set|sets|setting|make|makes|making|walk|walks|walking|limp"
    r"|limps|limping|hurry|hurries|hurrying|move|moves|moving|hobbl\w*|stagger\w*)\s+"
    r"(?:(?:my|our)\s+way\s+)?(?:back|home|out\s+of\s+(?:here|the)|off\s+(?:toward|towards|for|down|up))\b"
    r"|\b(?:leave|leaves|leaving)\s+(?:the\s+(?:hollow|yard|square|clearing|hut|ruin|ruins|place|den|woods)"
    r"|this\s+place|here|now|at\s+once)\b"
    r"|\b(?:retreat\w*|withdraw\w*|fall(?:s|ing)?\s+back|get(?:ting)?\s+(?:out|away)\s+(?:of\s+here|from)"
    r"|get(?:ting)?\s+back\s+to|back\s+the\s+way\s+I\s+came)\b",
    re.IGNORECASE,
)
# ...done by the player now: never a plan ("before I walk back up there",
# "if I'm going back"), nor someone else's walk ("I watch Coldiron walk back
# to his notice board", "the walk back feels shorter")
_NOT_NOW = re.compile(r"(?:\b(?:not|never|won['’]t|don['’]t|can['’]t|without|if|before|once|when|after|until"
                      r"|whether)\b|n['’]t\b)(?:\s+[\w'’-]+){0,3}\s*$", re.IGNORECASE)
_CLAUSE = re.compile(r"[,;:—–]|\b(?:and|then|but|so)\b", re.IGNORECASE)
_MY_CLAUSE = re.compile(r"^\s*(?:I(?:['’]m|\s+am)?(?:\s+(?:\w+ly|already|just|now|still|finally|then))*)?\s*$",
                        re.IGNORECASE)


def leaving(player_input: str) -> bool:
    """Does the player turn to go — in what they DO (outside their quotes),
    themselves, now? Never on an engine input."""
    text = (player_input or "").strip()
    if not text or text.startswith("["):
        return False
    acts = _QUOTED.sub(" ", text)
    for m in _LEAVING.finditer(acts):
        lead = re.split(r"(?<=[.!?…])\s+|\n+", acts[:m.start()])[-1]
        if _NOT_NOW.search(lead[-40:]) or not _MY_CLAUSE.match(_CLAUSE.split(lead)[-1]):
            continue
        return True
    return False


def _walked_on_from(player_id: str, enemies: list[NPC], player_input: str) -> list[NPC]:
    """The dangers here shown first the reply before (`_warned`) that the
    player now walks on from — leaving again with no risky act of their own
    (a flight or a stand-off is rolled, and the roll decides it), and not a
    word for it: "I retreat slowly, eyes on the wolf" answers it (the reply
    rolls it); "I ignore it and head back" or a walk that never mentions it
    does not."""
    if not leaving(player_input):
        return []
    from src.services import stakes
    from src.services.roll_intent import _BEASTLY, risky_act

    if risky_act(player_input):
        return []
    reply = stakes.current_turn(player_id)
    shown = {i for i, at in (_warned.get(player_id) or {}).items() if reply and at == reply - 1}
    hits = [n for n in enemies if n.id in shown][:MAX_AMBUSHERS]
    acts = _QUOTED.sub(" ", player_input or "")
    if hits and not _IGNORES.search(acts) and (_named_enemies(hits, acts) or _BEASTLY.search(acts)):
        return []
    return hits


_IGNORES = re.compile(r"\b(?:ignor\w*|pay(?:ing)?\s+(?:it|them|him|her)\s+no\s+mind|without\s+(?:a\s+)?(?:glance"
                      r"|look)|turn(?:s|ing)?\s+my\s+back\s+on)\b", re.IGNORECASE)


def introduced_now(player_id: str) -> set[str]:
    """Who THIS reply brings into the scene as a danger the player has not
    seen: a beast the director shows first (`roll`), or the beast or person
    of a complication raised for this reply (its first reply is the choice).
    A roll on one of them decides a choice that is the player's
    (tools/world_write/progression.roll_check; fleet10-wenna T20: the
    night-edge scout shown, "You plant your feet and go still", rolled and
    sent off in one reply). Never raises."""
    if not player_id:
        return set()
    ids: set[str] = set()
    try:
        from src.services import complications, stakes

        reply = stakes.current_turn(player_id)
        shown = _shown_now.get(player_id)
        if shown and reply and shown[0] == reply:
            ids |= shown[1]
        op = complications.open_complication(player_id)
        # ...never someone the player already knows: the one packing up for
        # the night is who they were just talking with, and a summons names
        # someone they met elsewhere — "I lean on the hawker" is theirs to roll
        if op and not op.get("replies") and op.get("kind") not in _KNOWN_ALREADY:
            if op.get("entity_id"):
                ids.add(op["entity_id"])
            elif op.get("entity_name"):
                # (a person stub the reply's own @new-npc makes real)
                with get_session() as session:
                    row = next((n for n in session.query(NPC).filter(NPC.status == "alive").all()
                                if (n.name or "").strip().lower() == str(op["entity_name"]).strip().lower()), None)
                    if row is not None:
                        ids.add(row.id)
    except Exception as e:
        logger.debug(f"introduced-now check failed: {e}")
    return ids


def _is_beast(npc: NPC) -> bool:
    try:
        from src.services.image_generator import ImageGenerator

        return bool(ImageGenerator._is_beast(npc))
    except Exception:
        return False


def beast_here(player_id: str) -> bool:
    """Does a living beast stand where the player is? Never raises. Not one
    this reply brings in (`introduced_now`): the stand-off is rolled from the
    reply after it is shown, when the player has seen it (fleet10-wenna T20)."""
    try:
        new = introduced_now(player_id)
        with get_session() as session:
            player = session.get(Player, player_id)
            if player is None or not player.current_location_id:
                return False
            party = set(player.party_members or [])      # a hound at heel is no stand-off
            return any(_is_beast(n) and n.id not in party and n.id not in new for n in session.query(NPC).filter(
                NPC.current_location_id == player.current_location_id, NPC.status == "alive").all())
    except Exception:
        return False


def _story_bosses(session, player: Player) -> set[str]:
    try:
        from src.services.campaign import get_campaign_row

        campaign = get_campaign_row(session, player.id)
        if campaign is not None:
            return {i for i in (campaign.villain_npc_id, campaign.successor_npc_id) if i}
    except Exception:
        pass
    return set()


def _story_cast(session, player: Player) -> set[str]:
    """Everyone a beat still to play needs as written (campaign.story_cast_ids).
    The director never sends them: round 6, fleet7-wenna T14 sent Hollow Wolf
    and Gray-Flank Wolf, both died, and act-3 "The Cached Cargo" still has
    them "prowl the den's edge". A fight the player starts with them is
    still theirs to start (the fight chip uses `enemies_here`)."""
    try:
        from src.services.campaign import story_cast_ids

        return story_cast_ids(session, player.id)
    except Exception as e:
        logger.debug(f"story cast check skipped: {e}")
        return set()


def _hostile_person(session, player: Player, npc: NPC) -> bool:
    from src.services import standing

    rel = standing.relationship_for(session, player.id, npc.id)
    if standing.disposition_band(rel) == "hostile":
        return True
    if (npc.current_mood or "").strip().lower() in standing.HOSTILE_DISPOSITIONS:
        return True
    return bool(npc.faction_id and standing.standing_for(player, npc.faction_id) == "hostile")


def enemies_here(session, player: Player, *, beasts_only: bool,
                 limit: int | None = MAX_AMBUSHERS, quarry: bool = True) -> list[NPC]:
    """Who in the room would attack the player, weakest first (a pack's
    fodder springs the ambush; the lieutenant is a story). `limit=None`:
    everyone (the roll orders its own draw). `quarry`: with a thief in sight
    (`mark_quarry`) — someone to chase or fight, never to send."""
    from src.services import party as party_mod

    if player is None or not player.current_location_id:
        return []
    party = set(party_mod.member_ids(player))
    bosses = _story_bosses(session, player)
    marked = set(_quarry.get(player.id) or ()) if quarry and not beasts_only else set()
    out = []
    for npc in session.query(NPC).filter(
        NPC.current_location_id == player.current_location_id, NPC.status == "alive"
    ).all():
        if npc.id in party or npc.id in bosses:
            continue
        if npc.id in marked:
            out.append(npc)
            continue
        if (npc.danger_tier or "").lower() in SAFE_TIERS or (npc.danger_tier or "").lower() == "boss":
            continue
        if npc.condition == "wounded":
            continue
        beast = _is_beast(npc)
        if beasts_only and not beast:
            continue
        if not beast and not _hostile_person(session, player, npc):
            continue
        out.append(npc)
    out.sort(key=lambda n: (_TIER_ORDER.get((n.danger_tier or "standard").lower(), 1), n.name))
    return out if limit is None else out[:limit]


_TIER_ORDER = {"weak": 0, "standard": 1, "dangerous": 2, "deadly": 3}


def _draw(player_id: str, location_id: str | None, enemies: list[NPC]) -> list[NPC]:
    """Who springs this ambush, weakest first — but a place that already
    ambushed this player sends someone it has not sent, and a tier it has not
    sent, first, when the room allows (fleet6-bram T10 was round 4's fight
    again: the same two weak wolves)."""
    sent = _drawn.get(player_id, {}).get(location_id, set())
    if not sent:
        return enemies[:MAX_AMBUSHERS]
    by_id = {n.id: n for n in enemies}
    sent_tiers = {(by_id[i].danger_tier or "standard").lower() for i in sent if i in by_id}
    ordered = sorted(enemies, key=lambda n: (
        n.id in sent,
        (n.danger_tier or "standard").lower() in sent_tiers,
        _TIER_ORDER.get((n.danger_tier or "standard").lower(), 1),
        n.name,
    ))
    return ordered[:MAX_AMBUSHERS]


def _time_of_day(session) -> str:
    from src.models import WorldClock

    clock = session.query(WorldClock).first()
    return "night" if clock is not None and int(clock.hour or 0) in NIGHT_HOURS else "day"


def _tension(session) -> str:
    from src.models import DMState

    state = session.query(DMState).first()
    return (state.tension or "low") if state is not None else "low"


def note_turn(player_id: str, location_id: str | None) -> int:
    """Count the turn; returns how many turns in a row they have been here."""
    where, count = _linger.get(player_id, (None, 0))
    count = count + 1 if where == location_id else 1
    _linger[player_id] = (location_id, count)
    _since_ambush[player_id] = _since_ambush.get(player_id, COOLDOWN_TURNS) + 1
    # A thief in sight stays quarry for QUARRY_REPLIES replies, then is let go
    marked = _quarry.get(player_id)
    if marked:
        for npc_id in list(marked):
            marked[npc_id] -= 1
            if marked[npc_id] <= 0:
                marked.pop(npc_id)
        if not marked:
            _quarry.pop(player_id, None)
    return count


def reset(player_id: str) -> None:
    """A fight just happened (any fight): the cooldown starts over."""
    _since_ambush[player_id] = 0
    # (where it began: the turn's own place, `note_turn`)
    _fought_at[player_id] = _linger.get(player_id, (None, 0))[0]
    _warned.pop(player_id, None)
    _parley.pop(player_id, None)
    _parley_skips.pop(player_id, None)
    _talking.pop(player_id, None)
    _standoff.pop(player_id, None)
    _standoff_held.pop(player_id, None)
    _answering.pop(player_id, None)
    _provoked.pop(player_id, None)
    _calmed_now.pop(player_id, None)
    _quarry.pop(player_id, None)
    _stood.pop(player_id, None)


def mark_quarry(player_id: str, npc_id: str, replies: int = QUARRY_REPLIES) -> None:
    """Someone the player may chase or fight for the next `replies` replies
    — a thief still in sight (complications: the pickpocket). The fight chip
    offers them; the director never sends them."""
    if player_id and npc_id:
        _quarry.setdefault(player_id, {})[npc_id] = max(replies, (_quarry.get(player_id) or {}).get(npc_id, 0))


def quarry_ids(player_id: str) -> set[str]:
    """Who is the player's to chase or fight right now (`mark_quarry`)."""
    return set(_quarry.get(player_id) or ())


def hold(npc_id: str, rolls: int = 1) -> None:
    """Leave this NPC out of the next `rolls` encounter rolls."""
    if npc_id:
        _held[npc_id] = max(_held.get(npc_id, 0), rolls)


def note_check(player_id: str, outcome: str, skill: str, reason: str) -> list[str]:
    """As a roll lands (the roll_check tool): a roll that went the player's
    way against a danger here holds that danger out of the next
    CALM_HOLD_ROLLS rolls — the one its reason names ("approaching the
    hungry scout wolf calmly"), or, a word, a trick or a sneak naming none,
    every danger here. Returns the ids held. Never raises.

    A won roll against a beast also sends it off after the reply
    (`send_off`); a roll LOST against a beast here — gentling it, backing
    away, slipping past — provokes it: the next roll is its lunge (round 8:
    a failed roll or a blow starts the fight, nothing else does)."""
    skill = (skill or "").strip().lower()
    lost = outcome in LOST_OUTCOMES and skill in _PROVOKING_SKILLS
    if not player_id or (outcome not in WON_OUTCOMES and not lost):
        return []
    try:
        with get_session() as session:
            player = session.get(Player, player_id)
            if player is None:
                return []
            here = enemies_here(session, player, beasts_only=False, limit=None, quarry=False)
            named = _named_enemies(here, reason)
            if not named and skill in _FACING_SKILLS + (("athletics",) if lost else ()):
                # ...unless it names someone else here: pressing the stable
                # hand while a wolf noses at the fence faced no wolf down
                dangers = {n.id for n in here}
                others = [n for n in session.query(NPC).filter(
                    NPC.current_location_id == player.current_location_id, NPC.status == "alive").all()
                    if n.id not in dangers]
                if not _named_enemies(others, reason):
                    named = dangers
            names = [n.name for n in here if n.id in named]
            beasts = {n.id for n in here if n.id in named and _is_beast(n)}
            beast_names = [n.name for n in here if n.id in beasts]
    except Exception as e:
        logger.warning(f"won-roll hold failed: {e}")
        return []
    if beasts:
        _stood.pop(player_id, None)         # the die was cast on the stand-off
    if lost:
        if beasts:
            _provoked.setdefault(player_id, set()).update(beasts)
            logger.info(f"Encounter provoked: a lost roll against {', '.join(beast_names)} ({reason!r})")
        return []
    for npc_id in named:
        hold(npc_id, CALM_HOLD_ROLLS)
    if beasts:
        _calmed_now.setdefault(player_id, set()).update(beasts)
    if names:
        logger.info(f"Encounter held: a won roll faced down {', '.join(names)} ({CALM_HOLD_ROLLS} rolls)")
    return sorted(named)


# The player opening a parley: talk words in what they do or say, and no
# blow struck in what they do. fleet6-bram T10: "I stay still, hand on my
# blade but not drawing yet... I call out: 'I'm here to talk, not fight'" —
# the last reply had no hostile voice in it (the ash-gray figure had only
# signed to his wolves), so note_parley held nothing and the same two weak
# wolves cut him off in one sentence
_TALK_INTENT = re.compile(
    r"\b(?:talk|talking|parley|speak|speaking|negotiat\w*|hear\s+(?:me|him|her|them)\s+out)\b"
    r"|\bnot\s+(?:here\s+)?(?:to\s+)?fight\b|\bI\s+call(?:s)?\s+out\b|\bI\s+(?:say|ask|tell)\b"
    r"|\bhands?\s+(?:up|open|visible|raised|empty)\b|\blower(?:s|ing)?\s+my\s+(?:blade|knife|sword|weapon)\b"
    r"|\b(?:who\s+are\s+you|what\s+do\s+you\s+want)\b",
    re.IGNORECASE,
)
_ATTACK_INTENT = re.compile(
    r"\bI\s+(?:[\w'’-]+\s+){0,2}?(?:attack|charge|lunge|strike|swing|stab|slash|rush|throw\s+myself)\b"
    r"|\bI\s+draw\s+(?:my|the)\s+[\w-]+\s+and\b",
    re.IGNORECASE,
)
_QUOTED = re.compile(r"[\"“][^\"”\n]*[\"”]")


def opens_parley(player_input: str) -> bool:
    """Does the player's input reach for words, not a weapon?"""
    text = (player_input or "").strip()
    if not text or text.startswith("["):
        return False
    return bool(_TALK_INTENT.search(text)) and not _ATTACK_INTENT.search(_QUOTED.sub(" ", text))


def danger_among(player_id: str, npc_ids: list[str] | None = None) -> bool:
    """Is anyone here (or among `npc_ids`) a danger to the player — an enemy
    the director could send, or someone who speaks for one? A roll in their
    company is a confrontation, not a question (turn_audit.roll_without_attempt)."""
    try:
        with get_session() as session:
            player = session.get(Player, player_id)
            if player is None:
                return False
            if enemies_here(session, player, beasts_only=False):
                return True
            ids = set(npc_ids or ())
            if player.current_location_id:
                ids |= {n.id for n in session.query(NPC).filter(
                    NPC.current_location_id == player.current_location_id, NPC.status == "alive").all()}
            return any(_parley_partner(session, player, session.get(NPC, i)) for i in ids)
    except Exception as e:
        logger.debug(f"danger check failed: {e}")
        return False


def _parley_partner(session, player: Player, npc: NPC | None) -> bool:
    """A present person who speaks for the danger in the room: hostile to the
    player, a story villain or boss, or one of the pack's own side (the
    wolf-caller whose wolves are the ambush). Aldric Vane is 'patient', not
    hostile — but his wolves are the ones that cut the talk short."""
    if npc is None or npc.status != "alive" or npc.current_location_id != player.current_location_id:
        return False
    if _is_beast(npc):
        return False
    if (npc.danger_tier or "").lower() == "boss" or npc.id in _story_bosses(session, player):
        return True
    if _hostile_person(session, player, npc):
        return True
    if not npc.faction_id:
        return False
    return any(e.faction_id == npc.faction_id and e.id != npc.id
               for e in enemies_here(session, player, beasts_only=False))


def note_parley(player_id: str, speaker_ids: list[str], reply_text: str = "") -> bool:
    """Post-turn: was the reply a parley — someone on the danger's side
    talking with the player? Then the next LINGERING roll is held.

    fleet4-bram T6 and fleet4-wenna T9: the lingering roll fired in the
    middle of a negotiation the villain himself had invited, and the
    player's key question ("What's your name, and what did you carve that
    word for?") was cut off by a one-sentence ambush. Returns the verdict.

    `reply_text` (the reply's prose): did it end on a choice put to the
    player with a danger in front of them (`_standoff_ids`)? And did it
    settle the one the player was answering peacefully (`_settled`)?
    """
    talking = False
    standoff: set[str] = set()
    answering = _answering.pop(player_id, set())
    try:
        if speaker_ids or reply_text:
            with get_session() as session:
                player = session.get(Player, player_id)
                if player is not None:
                    talking = any(_parley_partner(session, player, session.get(NPC, npc_id))
                                  for npc_id in dict.fromkeys(speaker_ids or ()))
                    standoff = _standoff_ids(session, player, reply_text)
                    calm = [n for n in enemies_here(session, player, beasts_only=False, limit=None)
                            if n.id in answering]
                    if calm and _settled(calm, reply_text):
                        for npc in calm:
                            hold(npc.id, CALM_HOLD_ROLLS)
                        logger.info(f"Encounter held: the reply settled the standoff with "
                                    f"{', '.join(n.name for n in calm)} peacefully")
    except Exception as e:
        logger.warning(f"parley check failed: {e}")
    _parley[player_id] = talking
    _talking[player_id] = talking
    if not talking:
        _parley_skips.pop(player_id, None)
    _standoff[player_id] = standoff
    return talking


# A reply that ends by putting a choice to the player while a danger stands
# in front of them: the choice is answered before the world moves again.
# fleet7-bram T8 ended "You can hold still and see if it loses interest, make
# noise to drive it off, or back toward the market square yourself"; the T9
# lingering roll (p=0.7) sent the Torn-Ear Wolf before the player's "Get! Go
# on, get out of here!" was read, and the reply was 48 tokens of lunge
_CHOICE_OFFERED = re.compile(
    r"\bYou\s+(?:can|could|might)\b[^.!?]*\bor\b"
    r"|\bwhat\s+(?:do|will)\s+you\s+do\b|\byour\s+(?:move|call|choice)\b"
    r"|\bor\b[^.!?\n]*\?\s*$",
    re.IGNORECASE,
)
_STANDOFF_PARAGRAPHS = 2


def _standoff_ids(session, player: Player, reply_text: str) -> set[str]:
    """The hostiles here named in the reply's last paragraphs, when those
    paragraphs put a choice to the player; else nothing."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n", reply_text or "") if p.strip()]
    tail = "\n".join(paragraphs[-_STANDOFF_PARAGRAPHS:])
    if not tail or not any(_CHOICE_OFFERED.search(p) for p in paragraphs[-_STANDOFF_PARAGRAPHS:]):
        return set()
    return _named_enemies(enemies_here(session, player, beasts_only=False, limit=None), tail)


# A danger the reply settled without a blow: it sits, backs off, takes the
# food, steps aside — said of it by name or as "it"/"he"/"she" — and nothing
# in the reply comes at the player (fleet8-wenna T11: "and then it sits,
# haunches lowering into the mud")
_SETTLED = re.compile(
    r"\b(?:sits?|sat|settles?|settled|lies\s+down|lay\s+down|backs?\s+(?:off|away|down)|stands?\s+down"
    r"|lowers?\s+(?:its|his|her|their)\s+(?:head|hackles|blade|knife|sword|weapon|hands?)"
    r"|slinks?\s+(?:off|away|back)|retreats?|retreated|withdraws?|withdrew|melts?\s+(?:back|away)"
    r"|calms?|calmed|quiets?|quieted|steps?\s+aside|lets?\s+you\s+(?:pass|go|by)|wanders?\s+off"
    r"|(?:trots?|lopes?|pads?)\s+(?:off|away)|takes?\s+the\s+(?:food|scraps?|meat|bread|offering)|eats)\b",
    re.IGNORECASE,
)
_COMES_AT_YOU = re.compile(
    r"\b(?:lunge[sd]?|springs?|sprang|leaps?|leapt|attacks?|attacked|bites?|charges?|charged|strikes?|struck"
    r"|pounces?|pounced|rakes?|raked|lashes\s+out|goes\s+for)\b[^.!?\n]{0,30}\b(?:you|your)\b",
    re.IGNORECASE,
)
_SUBJECT_PRONOUN = re.compile(r"(?:^|[,;—–]\s*|\band\s+(?:then\s+)?|\bthen\s+)(?:it|he|she|they)\b",
                              re.IGNORECASE)


# A beast the prose sends out of the scene: "It backs instead, unhurried, into
# the black beyond the lantern's reach, and is gone" (fleet9-wenna T14 — no
# @npc-move, so the database kept it at the fence and T16 forced the fight)
_GONE = re.compile(
    r"\b(?:(?:is|was)\s+gone\b|vanish(?:es|ed)\b|disappear(?:s|ed)\b(?!\s+(?:from\s+)?(?:your|his|her)\s+(?:hand|palm))"
    r"|slinks?\s+(?:off|away|back|into)\b|slunk\s+(?:off|away|back|into)\b"
    r"|(?:lope[sd]?|lopes|loping|trot(?:s|ted|ting)?|pad(?:s|ded|ding)?|slip(?:s|ped|ping)?|melt(?:s|ed|ing)?"
    r"|fade[sd]?|fading|back(?:s|ed|ing)?|retreat(?:s|ed|ing)?|withdr[ae]ws?|withdrawing|bolt(?:s|ed|ing)?"
    r"|turn(?:s|ed)?\s+and\s+(?:goes|go|lopes?|trots?|slinks?|runs?|bolts?))"
    r"\b[^.!?\n]{0,40}?\b(?:into|toward|towards|beyond|out\s+of)\s+(?:the\s+)?(?:[\w'’-]+\s+){0,2}?"
    r"(?:dark|darkness|black|night|shadows?|trees|treeline|tree\s+line|birches|brush|scrub|hills?|ridge|woods?"
    r"|forest|fields?|gloom|mist|fog|sight)\b"
    r"|(?:lope[sd]?|lopes|trot(?:s|ted)?|pad(?:s|ded)?|melt(?:s|ed)?|slip(?:s|ped)?)\s+(?:off|away)\b)",
    re.IGNORECASE,
)
# ...and one the prose keeps in the scene, calm: it sits, lies down, stays
# put (fleet8-wenna T11 "and then it sits, haunches lowering into the mud")
_STAYS = re.compile(
    r"\b(?:sits?|sat|lies\s+down|lay\s+down|stays?\s+(?:put|where|by|at|close|near|beside)|curls?\s+up"
    r"|settles?\s+(?:down|on\s+its|onto\s+its)|follows?\s+(?:you|at\s+your))\b",
    re.IGNORECASE,
)
_IT_SUBJECT = re.compile(r"(?:^|[,;—–]\s*|\band\s+(?:then\s+)?|\bthen\s+)it\b", re.IGNORECASE)
_SENTENCES = re.compile(r"(?<=[.!?…])\s+|\n+")


def _settled(npcs: list[NPC], reply_text: str) -> bool:
    """Did the reply settle the standoff with `npcs` peacefully?"""
    text = reply_text or ""
    if not text.strip() or _COMES_AT_YOU.search(text):
        return False
    for sentence in re.split(r"(?<=[.!?…])\s+|\n+", text):
        if (_SETTLED.search(sentence) or _GONE.search(sentence)) \
                and (_named_enemies(npcs, sentence) or _SUBJECT_PRONOUN.search(sentence)):
            return True
    return False


_POSSESSIVE = re.compile(r"[\w-]+['’]s\b")
# The clause the verb is in: "The wolf watches as you back into the shadows"
# is the player backing off, "the wolf snarls while Piper slips into the
# dark" is Piper — the beast named earlier in the sentence is not the one
# going (a stand-off's prose is full of both)
_CLAUSE_SEP = re.compile(r"[,;:—–]|\b(?:as|while|whilst|when|until|and|but|then|before|after|so|because|though)\b",
                         re.IGNORECASE)
_PRONOUN_SUBJECT = re.compile(r"\b(?:you|I|we|he|she|they)\b", re.IGNORECASE)
_PROPER_SUBJECT = re.compile(r"\b(?!(?:The|A|An|It|Its|Then|Now|Still|Only|Even|Just|One|Something|Nothing)\b)"
                             r"[A-Z][\w'’-]+")


def _someone_else_goes(npc: NPC, before: str) -> bool:
    """Is the verb's own clause about someone else — a pronoun or a name
    between the last clause break and the verb, not this beast?"""
    clause = _CLAUSE_SEP.split(before)[-1]
    # ..."you watch it slink off" is still the beast going
    if _named_enemies([npc], _POSSESSIVE.sub(" ", clause)) or re.search(r"\bit\s*$", clause, re.IGNORECASE):
        return False
    return bool(_PRONOUN_SUBJECT.search(clause) or _PROPER_SUBJECT.search(clause))


def _said_of(npc: NPC, pattern: re.Pattern, sentences: list[str], *, alone: bool, bare_it: bool = False) -> bool:
    """Does a sentence say `pattern` of this beast — its subject, named before
    the verb ("the wolf's howl fades into the night" is the howl), or "it"
    when the sentences just before named it and it is the only beast here
    (`alone`)? `bare_it`: "it" alone will do (the beast a roll was just about)."""
    for i, sentence in enumerate(sentences):
        for m in pattern.finditer(sentence):
            before = sentence[:m.start()]
            if _someone_else_goes(npc, before):
                continue
            if _named_enemies([npc], _POSSESSIVE.sub(" ", before)):
                return True
            if alone and _IT_SUBJECT.search(before) and (
                    bare_it or _named_enemies([npc], " ".join(sentences[max(0, i - 3):i] + [before]))):
                return True
    return False


def send_off(player_id: str, reply_text: str, *, fought: bool = False) -> list[str]:
    """Post-reply: the beasts here that leave the scene, moved home by the
    engine — one a roll won against this reply (`note_check`; unless the
    prose keeps it, calm, in the scene), or one the prose sent off into the
    dark with no @npc-move. Never after a fight, nor when the reply has
    something come at the player. A beast whose home is here is held
    instead. Returns a ledger line per move (the DM reads them next turn).
    Never raises."""
    calmed = _calmed_now.pop(player_id, set())
    if not player_id or fought:
        return []
    text = reply_text or ""
    if _COMES_AT_YOU.search(text):
        return []
    # What someone SAYS ("the road stays shut") moves no beast
    sentences = [s for s in _SENTENCES.split(_QUOTED.sub(" ", text)) if s.strip()]
    lines: list[str] = []
    try:
        with get_session() as session:
            player = session.get(Player, player_id)
            if player is None or not player.current_location_id:
                return []
            here = player.current_location_id
            beasts = enemies_here(session, player, beasts_only=True, limit=None, quarry=False)
            alone = len(beasts) == 1
            for npc in beasts:
                gone = _said_of(npc, _GONE, sentences, alone=alone)
                won = npc.id in calmed and not (_said_of(npc, _STAYS, sentences, alone=alone, bare_it=True)
                                                and not gone)
                if not (gone or won):
                    continue
                home = session.get(Location, npc.home_location_id) if npc.home_location_id else None
                if home is None or home.id == here:
                    hold(npc.id, CALM_HOLD_ROLLS)       # nowhere else to go: calm, not gone
                    logger.info(f"Encounter held: {npc.name} settles where it is (no home elsewhere)")
                    continue
                npc.current_location_id = home.id
                why = ("the player's won roll held it, so it slinks off" if won and not gone
                       else "your story sent it off")
                # Moved on a won roll alone, the player never saw it go: the
                # next reply shows it, or the wolf they read "circling" is
                # simply missing
                show = ("" if gone else " Unless your reply already showed it go, open the next one with it "
                                        "slinking off into the dark, in one sentence.")
                lines.append(f"(engine) @npc-move {npc.name} | {home.name}: {why} — the engine took it out of the "
                             f"scene, back toward {home.name}. It is gone from here; never bring it back without "
                             f"showing it return, and never start a fight with it here.{show}")
                logger.info(f"Encounter: {npc.name} leaves the scene for {home.name} ({why})")
            if lines:
                session.commit()
    except Exception as e:
        logger.warning(f"send-off failed: {e}")
        return []
    return lines


def _named_enemies(enemies: list[NPC], text: str) -> set[str]:
    """The ids of `enemies` that `text` names — by name, role label, or a
    long-enough word of either ("the hungry scout wolf" is Torn-Ear Wolf)."""
    lowered = (text or "").lower().replace("’", "'")
    named: set[str] = set()
    if not lowered.strip():
        return named
    for npc in enemies:
        # The reply arrives masked: a hostile stranger is "the Ash-Cloaked
        # Man" in it, never "Vex Marrow" (dialogue.NameGate)
        labels = [npc.name or ""]
        try:
            from src.services.dialogue import role_label

            labels.append(role_label(npc))
        except Exception:  # pragma: no cover - the name alone still counts
            pass
        for label in labels:
            words = [w for w in re.findall(r"[a-z][a-z'-]+", label.lower().replace("’", "'")) if len(w) >= 4]
            # ...one or many: "slipping past the wolves" names the wolf
            forms = [rf"{re.escape(w)}(?:s|es)?" + (rf"|{re.escape(w[:-1])}ves" if w.endswith("f") else "")
                     for w in words]
            if (label and label.lower() in lowered) or any(
                    re.search(rf"(?<![\w'-])(?:{form})(?![\w-])", lowered) for form in forms):
                named.add(npc.id)
                break
    return named


def in_parley(player_id: str) -> bool:
    """Was the last reply a parley? (note_parley's verdict, untouched by the
    roll that spends it.) A complication or a "Fight the wolf" chip waits
    for the talk to end."""
    return bool(_talking.get(player_id))


def roll(player_id: str, *, arrived: bool, rng: random.Random | None = None,
         player_input: str = "") -> dict[str, Any] | None:
    """Decide whether the world strikes now. Returns
    {"npc_ids", "names", "chance", "why"} or None. Never raises.
    `player_input`: what the player just typed — reaching for words with
    someone of the danger's side present holds a lingering roll, as a
    hostile voice in the last reply does."""
    rng = rng or random
    _answering.pop(player_id, None)
    # A beast a lost roll provoked comes now, whatever the odds or the
    # cooldown — the die decided it (round 8)
    provoked = _provoked.pop(player_id, set())
    try:
        if _since_ambush.get(player_id, COOLDOWN_TURNS) < COOLDOWN_TURNS and not provoked:
            return None
        with get_session() as session:
            player = session.get(Player, player_id)
            if player is None or player.health_status in ("critical", "dead"):
                return None
            # (never one a story beat still needs — that fight is the DM's @fight)
            angry: list[NPC] = []
            if provoked:
                needed = _story_cast(session, player)
                angry = [n for n in enemies_here(session, player, beasts_only=True, limit=None, quarry=False)
                         if n.id in provoked and n.id not in needed]
            if angry:
                location = session.get(Location, player.current_location_id)
                result = {
                    "npc_ids": [n.id for n in angry[:MAX_AMBUSHERS]],
                    "names": [n.name for n in angry[:MAX_AMBUSHERS]],
                    "chance": 1.0, "why": "provoked: the roll against it was lost",
                    "where": location.name if location is not None else "",
                }
                logger.info(f"Encounter: {', '.join(result['names'])} come on at {result['where']} "
                            "(a lost roll provoked them)")
                reset(player_id)
                return result
            if _since_ambush.get(player_id, COOLDOWN_TURNS) < COOLDOWN_TURNS:
                return None
            _, lingered = _linger.get(player_id, (None, 1))
            beasts_only = arrived or lingered <= 1
            # Nobody the story still needs as written (`_story_cast`): a beast
            # held back for it leaves its pack's rank and file (`_stand_in`)
            cast = _story_cast(session, player)
            # (a thief in sight is the player's to chase, never the ambush)
            here_now = enemies_here(session, player, beasts_only=beasts_only, limit=None, quarry=False)
            everyone = [n for n in here_now if n.id not in cast]
            withheld = [n for n in here_now if n.id in cast and _is_beast(n)]
            # A held NPC (a scout that is a choice first) sits this roll out
            held_now = [n for n in everyone if _held.get(n.id, 0) > 0]
            for npc in held_now:
                _held[npc.id] -= 1
                if _held[npc.id] <= 0:
                    _held.pop(npc.id, None)
            held_ids = {n.id for n in held_now}
            # A held danger is a choice the player is answering this reply
            _answering[player_id] = set(held_ids)
            enemies = _draw(player_id, player.current_location_id,
                            [n for n in everyone if n.id not in held_ids])
            # The player faces a beast here down without a blow: the roll the
            # context orders decides it, not this one (fleet9-tamsin T13:
            # backing toward the barn, the ambush fired with no die cast).
            # With someone of the pack's side here it is a parley, and the
            # parley's own rules hold the roll (below)
            from src.services.roll_intent import faces_a_beast

            beasts_here = {n.id for n in here_now if _is_beast(n)}
            facing_down = (not arrived and bool(beasts_here) and faces_a_beast(player_input, beast_here=True)
                           and not any(_parley_partner(session, player, n) for n in session.query(NPC).filter(
                               NPC.current_location_id == player.current_location_id, NPC.status == "alive").all()))
            if not facing_down:
                _stood.pop(player_id, None)
            elif _stood.get(player_id, 0) < STANDOFF_MAX_HOLDS:
                _stood[player_id] = _stood.get(player_id, 0) + 1
                _answering[player_id] |= beasts_here
                _standoff.pop(player_id, None)
                logger.info("Encounter held: the player faces the beast down — the roll decides it")
                return None
            # A danger shown first that the player walks on from, the reply
            # after, comes for them — the shown line's own "if they ignore it,
            # it comes for them". Without this the hold that gave them the
            # choice let them walk off free (the move follows the roll)
            ignored = [] if arrived else _walked_on_from(player_id, everyone, player_input)
            if ignored:
                location = session.get(Location, player.current_location_id)
                result = {
                    "npc_ids": [n.id for n in ignored], "names": [n.name for n in ignored],
                    "chance": 1.0, "why": "ignored: shown first, then walked on from",
                    "where": location.name if location is not None else "",
                }
                _drawn.setdefault(player_id, {}).setdefault(player.current_location_id, set()).update(
                    result["npc_ids"])
                logger.info(f"Encounter: {', '.join(result['names'])} come on at {result['where']} "
                            "(shown first, and the player walked on)")
                reset(player_id)
                return result
            # The last reply put a choice to the player facing one of them:
            # the answer comes first, once — unless the answer is a blow
            # (then the fight is theirs) or they walked away (`_CHOICE_OFFERED`)
            facing = _standoff.pop(player_id, set())
            if facing and not arrived and not _standoff_held.get(player_id) \
                    and facing & {n.id for n in here_now} \
                    and not _ATTACK_INTENT.search(_QUOTED.sub(" ", player_input or "")):
                _standoff_held[player_id] = True
                _answering[player_id] |= facing & {n.id for n in here_now}
                logger.info("Encounter held: the player is answering the standoff the last reply offered")
                return None
            _standoff_held.pop(player_id, None)
            if not enemies and (held_now or not withheld):
                return None
            # The talk gets its answer first (arrivals are unchanged: walking
            # into a den is not a conversation) — the last reply's hostile
            # voice, or the player opening one now with the danger's side here
            opening = (not arrived and not _parley.get(player_id) and opens_parley(player_input)
                       and any(_parley_partner(session, player, n) for n in session.query(NPC).filter(
                           NPC.current_location_id == player.current_location_id, NPC.status == "alive").all()))
            if not arrived and (_parley.get(player_id) or opening):
                skips = _parley_skips.get(player_id, 0)
                if skips < PARLEY_MAX_SKIPS:
                    _parley[player_id] = False      # the next reply must still be talking
                    _parley_skips[player_id] = skips + 1
                    if opening:
                        _talking[player_id] = True  # ...and no complication cuts into it either
                    logger.info(f"Encounter held: a parley is under way ({skips + 1}/{PARLEY_MAX_SKIPS})"
                                + (" — the player opened it" if opening else ""))
                    return None
            when = _time_of_day(session)
            chance = ARRIVAL_CHANCE[when]
            if not arrived and lingered > 1:
                chance = min(LINGER_CAP, chance + LINGER_STEP * (lingered - 1))
            chance += TENSION_BONUS.get(_tension(session), 0.0)
            chance = min(chance, 0.9)
            # A place that just fought them strikes at reduced odds a while
            if not arrived and _fought_at.get(player_id) and _fought_at[player_id] == player.current_location_id \
                    and _since_ambush.get(player_id, COOLDOWN_TURNS) < COOLDOWN_TURNS + AFTER_FIGHT_TURNS:
                chance *= AFTER_FIGHT_DAMP
            if rng.random() >= chance:
                return None
            if not enemies:
                enemies = [_stand_in(session, player, withheld[0])]
            location = session.get(Location, player.current_location_id)
            why = (("arrival" if arrived else "lingering") + f", {when}")
            result = {
                "npc_ids": [n.id for n in enemies],
                "names": [n.name for n in enemies],
                "chance": round(chance, 2),
                "why": why,
                "where": location.name if location is not None else "",
            }
            _drawn.setdefault(player_id, {}).setdefault(player.current_location_id, set()).update(
                result["npc_ids"])
            # Turning to go: shown first, once (see `_shown_now`)
            from src.services import stakes

            reply = stakes.current_turn(player_id)
            shown = {i for i, at in (_warned.get(player_id) or {}).items() if reply - at <= WARNED_REPLIES}
            if not arrived and leaving(player_input) and not set(result["npc_ids"]) & shown:
                result["shown_first"] = True
        if result.get("shown_first"):
            ids = set(result["npc_ids"])
            for npc_id in ids:
                hold(npc_id, 1)             # the reply after is the player's answer
            _warned.setdefault(player_id, {}).update({npc_id: reply for npc_id in ids})
            _shown_now[player_id] = (reply, ids)
            logger.info(f"Encounter shown first: {', '.join(result['names'])} at {result['where']} as the player "
                        f"turns to go ({why}, p={result['chance']})")
            return result
        logger.info(f"Encounter: {', '.join(result['names'])} ambush at {result['where']} "
                    f"({why}, p={result['chance']})")
        reset(player_id)
        return result
    except Exception as e:
        logger.warning(f"encounter roll failed: {e}")
        return None


def _stand_in(session, player: Player, like: NPC) -> NPC:
    """One weak beast of `like`'s pack, made real here to spring the ambush
    the story's own beasts sit out — a den the story needs its wolves for is
    still no safe place (round 5 found no fights at all without the
    director). Only when nobody else here could strike; a stand-in that
    lives is drawn like anyone next time."""
    from src.services import spawn

    here = session.get(Location, player.current_location_id)
    words = set(re.findall(r"[a-z]+", f"{like.species or ''} {like.name or ''}".lower()))
    species = (like.species or "").strip().lower() or next(
        (w for w in ("wolf", "hound", "bear", "boar", "spider", "serpent") if w in words), "wolf")
    npc = spawn.spawn_beast_scout(session, here, species=species, faction_id=like.faction_id)
    npc.profession = f"{species} of the pack"
    npc.home_location_id = here.id
    npc.description_physical = f"A {species} of the same pack, lean and quiet, one of many."
    npc.description_personality = "Bold in numbers; it answers the pack, not hunger."
    npc.current_mood = "hostile"
    session.commit()
    logger.info(f"Encounter stand-in: {npc.name} (the story keeps {like.name})")
    return npc


def directive(encounter: dict[str, Any]) -> str:
    """What the DM is told when the engine has already started a fight — or,
    for a danger shown first (`shown_first`), that no fight has started."""
    names = ", ".join(encounter["names"])
    if encounter.get("shown_first"):
        first = encounter["names"][0]
        return (
            f"{SHOWN_FIRST_MARK} — the engine decided this; NO fight has started. As the player turns to go, "
            f"{names} {'is' if len(encounter['names']) == 1 else 'are'} there: eyes in the dark, a growl on the path, "
            "a shape between them and the way out. Answer what the player did, then show it in two or three "
            "sentences and STOP on it, before they have gone far — no @move this reply, whatever a travel line "
            "says: it stops them on the way out. What they do about it is "
            f"theirs, next reply: fight it (@fight {first}), or face it down, calm it or back away from it "
            "(roll_check first, then). Never write their answer, never roll for it now, never start the fight "
            "for them. If they ignore it, it comes for them.]"
        )
    return (
        "[AMBUSH — ALREADY HAPPENING. The engine decided this; the fight is on the player's "
        f"screen right now.] {names} attack the player. Whatever the player just said, it is "
        "cut short. Write ONE vivid freeze-frame sentence of the attack breaking out — no "
        "dialogue, no commands, nothing after it. Never @fight again; it is already running."
    )

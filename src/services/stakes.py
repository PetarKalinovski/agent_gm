"""Stakes: a failed roll costs something, and so does waiting.

Round 5 played three sessions in which failure was free and time cost
nothing:

  * fleet6-bram T7 failed the howl count; the only cost was the turn's own
    `@time 15m`, the audit said so (partial_without_cost), and T8 charged
    nothing either. At T18 the DM ticked the very step that failed
    (`@step-done Counting the Howls | Stand out the next howl and mark where
    it rises`) and T19 paid it in full. fleet6-wenna T8 -> T9 the same, one
    reply apart.
  * fleet6-tamsin carried the gate pass from T2 and sat in two rooms until
    T20 with the Hollow open to her; waiting for first light cost nothing.

Three answers, all in code:

  1. An unpaid price is charged. When partial_without_cost was raised and
     the NEXT reply still writes no cost, the engine applies one itself —
     trust down with the person the roll was about, time lost, or the
     danger a step closer, picked by the roll's skill — and the DM is told
     exactly what it cost so the prose can show it.
  2. A failed roll binds its step. A failure whose reason shares two or more
     content words with an undone custom step refuses `@step-done` on that
     step for the rest of that reply and the next: it needs a new attempt
     (roll_check) or another route first.
  3. Waiting has a clock. While the main quest's next step is a place the
     player can already reach, every reply spent elsewhere counts; after
     WAIT_TURNS of them, or six open hours, a consequence drawn from the
     act's own missed-consequence vocabulary (another cart taken, a patrol
     overdue) lands as a world event, and a [WHILE YOU WAITED] line tells the
     DM to show it as the world moving on. A way in the player holds nothing
     for, or a place shut for the night, stops the clock (fleet7-bram).

Round 7 (fleet8) sharpened the first two: a coin the player chose to toss
is no price of a roll (`chosen_payment`), a critical failure on a climb or a
drop is charged as a fall, and a story beat's words are never the dice's to
withhold — a social roll about the willing person a firing beat has speak
is not rolled at all (`beat_tells_it`, read by the roll_check tool).

Round 9 (fleet10) made the waiting clock fair and concrete: it pauses while
the player works the story's own side job and resets on any step nearer the
place (`_side_job_work`, `_hops_to`; fleet10-wenna T10-T16); a trail is
never "cold" within COLD_FRESH_HOURS of anything fresh there; and someone
missing is someone — an NPC on record and a trace to find
(`_register_missing`; fleet10-bram T10-T20's invented "old Dennet's boy").

In memory, like the encounter and complication directors: a restart only
forgets the counts.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable

from src.models import NPC, Location, Player, get_session

logger = logging.getLogger(__name__)

FAILED = ("failure", "critical_failure")
COSTLY = ("partial", "failure", "critical_failure")
STEP_OVERLAP = 2              # content words a failed roll's reason shares with the step it attempted
FAILED_STEP_REPLIES = 2       # the reply that failed, and the next
TRUST_COST = 5
TIME_COST_MINUTES = 30
WAIT_TURNS = 6                # replies spent elsewhere while the main quest's road is open
WAIT_NIGHT_MINUTES = 6 * 60   # ...or a night of game time
MAX_WAIT_CONSEQUENCES = 2     # per step: the world pushes, it does not pile on
_SOCIAL = ("persuasion", "cunning")
_TENSION_NEXT = {"low": "rising", "falling": "rising", "rising": "high", "high": "climax"}

# Per player
_turn: dict[str, int] = {}
_blocked: dict[str, list[dict[str, Any]]] = {}
_owed: dict[str, dict[str, Any]] = {}
_waiting: dict[str, dict[str, Any]] = {}
_wait_kinds: dict[str, list[str]] = {}


def reset_state() -> None:
    """Forget every count (tests, world switches)."""
    for state in (_turn, _blocked, _owed, _waiting, _wait_kinds):
        state.clear()


def begin_turn(player_id: str) -> int:
    """A reply is about to be written: count it, and let the step bindings
    of replies before last expire."""
    if not player_id:
        return 0
    _turn[player_id] = _turn.get(player_id, 0) + 1
    now = _turn[player_id]
    if player_id in _blocked:
        _blocked[player_id] = [b for b in _blocked[player_id] if b["until"] >= now]
    return now


def current_turn(player_id: str) -> int:
    """Which reply is being written (0: none counted — the CLI never begins
    one, so nothing keyed on replies may assume one)."""
    return _turn.get(player_id, 0) if player_id else 0


# ---------------------------------------------------------------------------
# What the roll was for: roll_check's own arguments, read off the stream
# ---------------------------------------------------------------------------

def _parsed_input(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None       # still streaming: the next event carries more
        return parsed if isinstance(parsed, dict) else None
    return None


def capture_roll_input(response: Any, calls: dict[str, dict[str, Any]]) -> None:
    """Keep each roll_check call's arguments ({skill, reason, about_npc_id})
    by tool-use id, from the streamed tool use or the finished assistant
    message. The tool's result carries only the outcome; the reason says
    WHAT was attempted. Never raises."""
    if not isinstance(response, dict):
        return
    try:
        uses: list[Any] = []
        if isinstance(response.get("current_tool_use"), dict):
            uses.append(response["current_tool_use"])
        message = response.get("message")
        if isinstance(message, dict):
            for block in message.get("content") or []:
                if isinstance(block, dict) and isinstance(block.get("toolUse"), dict):
                    uses.append(block["toolUse"])
                # A roll the tool refused (a skill or difficulty it does not
                # know) has no outcome: left in, its reason would pair with
                # the NEXT roll's result and bind the wrong step
                result = block.get("toolResult") if isinstance(block, dict) else None
                if isinstance(result, dict) and str(result.get("toolUseId") or "") in calls \
                        and "check_result" not in json.dumps(result.get("content"), default=str):
                    calls.pop(str(result["toolUseId"]), None)
        for use in uses:
            if use.get("name") != "roll_check" or not use.get("toolUseId"):
                continue
            parsed = _parsed_input(use.get("input"))
            if parsed is not None:
                calls[str(use["toolUseId"])] = {
                    "skill": str(parsed.get("skill") or ""),
                    "reason": str(parsed.get("reason") or ""),
                    "about_npc_id": str(parsed.get("about_npc_id") or "") or None,
                }
            else:
                calls.setdefault(str(use["toolUseId"]), {})
    except Exception as e:      # the stream never pays for bookkeeping
        logger.debug(f"roll input not captured: {e}")


def take_roll_call(calls: dict[str, dict[str, Any]], taken: set[str]) -> dict[str, Any]:
    """The oldest roll_check call not yet paired with its result."""
    for call_id, args in calls.items():
        if call_id not in taken:
            taken.add(call_id)
            return dict(args)
    return {}


# ---------------------------------------------------------------------------
# A failed roll binds the step it attempted
# ---------------------------------------------------------------------------

_STOP = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for", "from", "with", "into", "onto",
    "by", "it", "its", "his", "her", "their", "them", "him", "she", "he", "they", "you", "your", "what",
    "where", "when", "who", "how", "why", "which", "that", "this", "these", "those", "is", "are", "was",
    "be", "been", "out", "up", "down", "over", "about", "back", "next", "before", "after", "while",
    "without", "being", "get", "gets", "getting", "find", "make", "take", "some", "any", "all", "more",
    "own", "there", "here", "now", "then", "just", "still", "really", "exactly",
})


def _stem(word: str) -> str:
    """Crude, consistent: "marking"/"marks"/"mark", "rises"/"rise"."""
    word = word.lower().strip("'’")
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            word = word[: -len(suffix)]
            break
    return word[:-1] if len(word) > 3 and word.endswith("e") else word


def content_stems(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[A-Za-z][A-Za-z'’]+", text or "")
            if len(w) >= 3 and w.lower() not in _STOP}


def _custom_steps(session) -> list[tuple[str, str, dict[str, Any]]]:
    """(quest id, quest title, objective) for every undone custom step of an
    active quest."""
    from src.models.quests import Quest, QuestStatus
    from src.services import objectives as objectives_mod

    out = []
    for quest in session.query(Quest).filter(Quest.status == QuestStatus.ACTIVE).all():
        for obj in objectives_mod.normalize_objectives(quest.objectives):
            if obj.get("kind") == "custom" and not obj.get("done"):
                out.append((quest.id, quest.title, obj))
    return out


def note_roll(player_id: str, roll: dict[str, Any]) -> list[str]:
    """Mid-reply, as each roll lands: a failure binds the custom steps its
    reason attempted; a later success on the same step frees it (the fresh
    attempt). Returns the step texts bound. Never raises."""
    if not player_id or not isinstance(roll, dict):
        return []
    try:
        outcome = str(roll.get("outcome") or "")
        reason = content_stems(str(roll.get("reason") or ""))
        if not reason:
            return []
        now = _turn.get(player_id, 0)
        with get_session() as session:
            steps = _custom_steps(session)
        hits = [(qid, title, obj) for qid, title, obj in steps
                if len(content_stems(obj["text"]) & reason) >= STEP_OVERLAP]
        if not hits:
            return []
        keys = {(qid, obj["id"]) for qid, _, obj in hits}
        kept = [b for b in _blocked.get(player_id, []) if (b["quest_id"], b["objective_id"]) not in keys]
        if outcome in FAILED:
            kept += [{"quest_id": qid, "objective_id": obj["id"], "text": obj["text"], "quest_title": title,
                      "reason": str(roll.get("reason") or ""), "until": now + FAILED_STEP_REPLIES - 1}
                     for qid, title, obj in hits]
            logger.info(f"Failed roll binds step(s): {[obj['text'] for _, _, obj in hits]}")
        _blocked[player_id] = kept
        return [obj["text"] for _, _, obj in hits] if outcome in FAILED else []
    except Exception as e:
        logger.warning(f"roll/step binding failed: {e}")
        return []


def step_refusal(player_id: str, quest_id: str, objective: dict[str, Any]) -> str | None:
    """Why `@step-done` may not tick this step now, or None."""
    if not player_id or not isinstance(objective, dict):
        return None
    now = _turn.get(player_id, 0)
    for block in _blocked.get(player_id, []):
        if block["quest_id"] == quest_id and block["objective_id"] == objective.get("id") and block["until"] >= now:
            return ("that attempt failed — it needs a new attempt (roll_check) or another route first; "
                    f"'{objective.get('text')}' stays open, and the failure still costs something")
    return None


# ---------------------------------------------------------------------------
# An unpaid price is charged by the engine
# ---------------------------------------------------------------------------

def _npc_in_reason(session, reason: str, npc_ids: Iterable[str]) -> NPC | None:
    """The present person a roll's reason names: their name's own words or
    their role label ("pressing a frightened Hesk", "the stable hand")."""
    from src.services.dialogue import role_label

    words = {w.lower() for w in re.findall(r"[A-Za-z][\w'’-]+", reason or "")}
    for npc_id in dict.fromkeys(i for i in npc_ids or () if i):
        npc = session.get(NPC, npc_id)
        if npc is None or npc.status != "alive":
            continue
        own = {w.lower() for w in re.findall(r"[A-Za-z][\w'’-]+", npc.name or "") if len(w) >= 3}
        own -= {"the", "captain", "old", "young", "master", "mistress"}
        label = {w.lower() for w in re.findall(r"[A-Za-z][\w'’-]+", role_label(npc) or "") if len(w) >= 4}
        label -= {"man", "woman", "stranger"}
        if own & words or (label and label <= words):
            return npc
    return None


def owe(player_id: str, rolls: Iterable[dict[str, Any]], *, present_ids: Iterable[str] = (),
        addressed_npc_id: str | None = None) -> dict[str, Any] | None:
    """Post-turn, when partial_without_cost was raised: remember the price
    the next reply must write. Returns the debt."""
    from src.services.perception import RECOGNITION_SKILL

    # A recognition roll's failure is paid by the recognition itself
    costly = [r for r in rolls or () if isinstance(r, dict) and r.get("outcome") in COSTLY
              and not (r.get("about_npc_id") and r.get("skill") == RECOGNITION_SKILL)]
    if not player_id or not costly:
        return None
    worst = max(costly, key=lambda r: COSTLY.index(r["outcome"]))
    npc_id = worst.get("about_npc_id")
    try:
        if not npc_id:
            with get_session() as session:
                npc = _npc_in_reason(session, str(worst.get("reason") or ""), list(present_ids or ()))
                npc_id = npc.id if npc is not None else None
    except Exception as e:
        logger.debug(f"debt subject not resolved: {e}")
    debt = {"outcome": worst["outcome"], "skill": str(worst.get("skill") or ""),
            "reason": str(worst.get("reason") or ""), "npc_id": npc_id or addressed_npc_id,
            "turn": _turn.get(player_id, 0)}
    _owed[player_id] = debt
    return debt


def owed(player_id: str) -> dict[str, Any] | None:
    return _owed.get(player_id)


def _tension_now() -> str:
    from src.models import DMState

    with get_session() as session:
        state = session.query(DMState).first()
        return (state.tension or "low") if state is not None else "low"


def _cost_lines(debt: dict[str, Any], player_id: str) -> list[tuple[str, str]]:
    """(command, what it cost in words) in order of fit: a social roll costs
    the person's trust, a watch or a climb costs time, a sneak raises the
    danger; time is always the last resort."""
    skill = debt.get("skill") or ""
    trust: list[tuple[str, str]] = []
    if skill in _SOCIAL and debt.get("npc_id"):
        with get_session() as session:
            npc = session.get(NPC, debt["npc_id"])
            # A stranger too: @trust lands on their row at once now (commands.
            # _open_relationship) — fleet6-tamsin T4's failed ask of Sorrel,
            # still unnamed, is exactly the price that was lost
            if npc is not None and npc.status == "alive":
                trust.append((f"@trust {npc.name} -{TRUST_COST} | wary",
                              f"{npc.name} trusts them less now (trust -{TRUST_COST}) — show it: a cooler "
                              "look, a shorter answer"))
    nxt = _TENSION_NEXT.get(_tension_now())
    tension = [(f"@tension {nxt}", f"the danger rose a step (tension {nxt}) — show it: a noise answered, "
                                   "eyes turning, something closer")] if nxt else []
    time = [(f"@time {TIME_COST_MINUTES}m | the failed attempt",
             f"{TIME_COST_MINUTES} minutes lost to it (the clock has moved on) — show the time gone")]
    if skill in ("perception", "athletics"):
        # A climb or a drop that went badly wrong is a fall, and a fall hurts
        # (fleet8-tamsin T9: a critical failure dropping from the loft, then
        # "boots finding the mud without a stumble" and nothing charged)
        worse = _one_worse(player_id) if skill == "athletics" and debt.get("outcome") == "critical_failure" \
            else None
        if worse:
            what = debt.get("reason") or "a bad fall"
            return [(f"@health {worse} | cause: {what}",
                     f"it hurt them — they are {worse.replace('_', ' ')} now; show it: a jarred knee, a torn "
                     "palm, a limp")] + time
        return time                     # a watch or a climb missed costs the hour it ate
    return trust + tension + time       # time is always the last resort


def _one_worse(player_id: str) -> str | None:
    """One step down the health ladder from where the player stands, never
    past hurt: a fall is a wound, not a death sentence."""
    from src.services.progression import HEALTH_ORDER

    with get_session() as session:
        player = session.get(Player, player_id)
        health = (player.health_status or "healthy") if player is not None else None
    if health not in ("healthy", "winded"):
        return None
    return HEALTH_ORDER[HEALTH_ORDER.index(health) + 1]


# A price the player CHOSE to pay is no price the dice charged. fleet8-tamsin
# T9: "I toss him a coin as I follow the runner" — `@coin -1 | tossed to
# Sorrel for his watch tonight` stood as the cost of a critical failure on the
# drop from the loft, and nothing was owed. What the player's own words hand
# over (coin, or the thing itself) is theirs to give; what the world takes (a
# toll, a fine, a theft — complications._IMPOSED) still bites
_PLAYER_PAYS = re.compile(
    r"\b(?:toss(?:es|ed|ing)?|flip(?:s|ped|ping)?|pay(?:s|ing)?|paid|slid(?:e|es|ing)?|hand(?:s|ed|ing)?"
    r"|giv(?:e|es|ing)|gave|press(?:es|ed|ing)?|offer(?:s|ed|ing)?|tip(?:s|ped|ping)?|push(?:es|ed|ing)?"
    r"|count(?:s|ed|ing)?\s+out|hold(?:s|ing)?\s+(?:it|them)\s+out|held\s+(?:it|them)\s+out)\b",
    re.IGNORECASE,
)
_MONEY = re.compile(r"\b(?:coins?|silver|copper|gold|money|purse|crowns?|pennies|penny)\b", re.IGNORECASE)


def chosen_payment(command: Any, player_input: str) -> bool:
    """Is this command (a line, or one parse_command made) — a coin debit, a
    thing handed or removed — something the player's own input gave away: a
    tip, a payment, a gift they typed? Then it is not the price of a roll.
    Never raises."""
    try:
        from src.services.commands import parse_command, parse_signed
        from src.services.complications import _IMPOSED

        text = (player_input or "").strip()
        parsed = command if hasattr(command, "verb") else parse_command(str(command or ""))
        if parsed is None or not text or text.startswith("["):
            return False
        if _IMPOSED.search(" ".join(str(e) for e in parsed.extras or [])):
            return False
        sentences = [s for s in _SENTENCE.split(text) if _PLAYER_PAYS.search(s)]
        if parsed.verb in ("coin", "coins", "gold", "money"):
            value = parsed.main or (parsed.extras[0] if parsed.extras else "")
            return (parse_signed(value) or 0) < 0 and any(_MONEY.search(s) for s in sentences)
        if parsed.verb in ("remove", "hand", "lose", "drop"):
            words = re.findall(r"[a-z]{4,}", str(parsed.main or "").lower())
            return any(re.search(rf"\b{re.escape(w)}", s.lower()) for s in sentences for w in words)
    except Exception as e:      # a check it cannot make leaves the price standing
        logger.debug(f"chosen payment check failed: {e}")
    return False


# ---------------------------------------------------------------------------
# A story beat's words are not the dice's to withhold
# ---------------------------------------------------------------------------
_BEAST_WORDS = re.compile(
    r"\b(?:wol(?:f|ves)|beasts?|hounds?|dogs?|curs?|animals?|creatures?|bears?|boars?)\b",
    re.IGNORECASE,
)
# A roll whose act is done TO a beast — "calming the wolf while the stable hand
# watches" is the wolf's roll even though it names the beat's teller; "pressing
# Sorrel about the wolf" is still Sorrel's
_ON_A_BEAST = re.compile(
    r"\b(?:calm(?:s|ed|ing)?|sooth(?:e|es|ed|ing)|gentl(?:e|es|ed|ing)|feed(?:s|ing)?|fed|tam(?:e|es|ed|ing)"
    r"|fac(?:e|es|ed|ing)(?:\s+down)?|driv(?:e|es|ing)(?:\s+(?:off|away))?|scar(?:e|es|ed|ing)(?:\s+(?:off|away))?"
    r"|chas(?:e|es|ed|ing)(?:\s+(?:off|away))?|star(?:e|es|ed|ing)\s+down|befriend\w*|approach\w*|back(?:s|ed|ing)?"
    r"\s+(?:away|off)\s+from|hold(?:s|ing)?\s+(?:\w+\s+)?ground\s+(?:against|before))\s+"
    r"(?:(?:the|a|an|that|this|its)\s+)?(?:(?!(?:about|of|on|over|for|with|to|regarding)\b)[\w'’-]+\s+){0,3}?"
    r"(?:wol(?:f|ves)|beasts?|hounds?|dogs?|curs?|animals?|creatures?|bears?|boars?)\b",
    re.IGNORECASE,
)


# fleet8-bram T19: the beat "What Sorrel Saw" said "on a second press he adds
# the man moved like … an old Watchman would"; the player pressed, the DM
# rolled persuasion, it failed, and the session's only pointer at the
# villain's past was deleted by the dice (the trust it cost came back a reply
# later). A willing person whom a beat firing now has say something says it:
# a social roll about them is no roll at all.

def beat_tells_it(session, player: Player | None, skill: str, reason: str) -> NPC | None:
    """The willing person a fired story beat has speak, present here, whom
    this persuasion/cunning roll is about — or None. About: the reason names
    them, or names nobody here and they are the only person here — never a
    roll about a beast in the scene (fleet9-wenna T14).

    Fired only: that beat's directive is in the DM's context this reply. A
    stalled beat shows the DM nothing but its lead — "play it exactly as the
    beat writes it" would be an order to invent, and a failed roll there
    withholds nothing the DM was given (it is delivered by play later)."""
    if player is None or not player.current_location_id or (skill or "").strip().lower() not in _SOCIAL:
        return None
    from src.models.campaign import StoryBeat
    from src.services import standing
    from src.services.campaign import _beat_people, get_campaign_row
    from src.services.dialogue import is_beast

    campaign = get_campaign_row(session, player.id)
    if campaign is None or campaign.status != "active":
        return None
    beats = session.query(StoryBeat).filter(StoryBeat.campaign_id == campaign.id,
                                            StoryBeat.status == "fired").all()
    people = {i for beat in beats for i in _beat_people(beat)}
    villains = {campaign.villain_npc_id, campaign.successor_npc_id}
    willing = []
    for npc in (session.get(NPC, i) for i in sorted(people)):
        if npc is None or npc.status != "alive" or npc.current_location_id != player.current_location_id \
                or npc.id in villains or is_beast(npc) or (npc.danger_tier or "").lower() == "boss":
            continue
        rel = standing.relationship_for(session, player.id, npc.id)
        if standing.disposition_band(rel) == "hostile" \
                or (npc.current_mood or "").strip().lower() in standing.HOSTILE_DISPOSITIONS:
            continue
        willing.append(npc)
    if not willing:
        return None
    here = session.query(NPC).filter(NPC.current_location_id == player.current_location_id,
                                     NPC.status == "alive").all()
    # ...even when the reason also names the teller ("calming the wolf while
    # the stable hand watches")
    if any(is_beast(n) for n in here) and _ON_A_BEAST.search(reason or ""):
        return None
    named = _npc_in_reason(session, reason, [n.id for n in willing])
    if named is not None:
        return named
    # A roll about a beast here is the beast's, never a beat-teller's words:
    # fleet9-wenna T14 "calming a hungry wolf at the fence" fell through to
    # the one person there (Sorrel), no die was cast, and the wolf she fed
    # was the fight two replies later
    beasts = [n for n in here if is_beast(n)]
    if beasts:
        from src.services.encounters import _named_enemies

        if _BEAST_WORDS.search(reason or "") or _named_enemies(beasts, reason or ""):
            return None
    # A reason naming nobody ("talking my way past") is about the one person
    # here, or about no one we can tell: a sentry beside the beat's teller
    # may be the one being bluffed
    people = [n for n in here if not is_beast(n)]
    return willing[0] if len(willing) == 1 and [n.id for n in people] == [willing[0].id] else None


def settle_owed_cost(player_id: str, *, paid: bool, fought: bool = False,
                     present_ids: Iterable[str] = ()) -> dict[str, Any] | None:
    """Post-turn: the reply after a partial_without_cost. Paid (a cost
    command landed, or a fight was the price) clears the debt; otherwise the
    engine writes the cost itself. Returns {"line", "message", "note"} for
    an engine charge, else None. Never raises."""
    debt = _owed.pop(player_id, None) if player_id else None
    if debt is None or paid or fought:
        return None
    try:
        from src.services.commands import CommandRunner

        runner = CommandRunner(player_id, present_ids=list(present_ids or ()))
        for line, words in _cost_lines(debt, player_id):
            result = runner.run(line)
            if not result.ok or (isinstance(result.result, dict) and result.result.get("held_for_intro")):
                continue
            what = debt.get("reason") or "the attempt"
            label = "failed" if debt["outcome"] in FAILED else "half-made"
            note = (f"Two replies ago a roll came back {debt['outcome'].replace('_', ' ')} ({what}) and no reply "
                    f"charged its price, so the engine did: {words}. It is ALREADY applied — show it in this "
                    f"reply in a beat of prose; never refund it, never charge it again. (A {label} roll always "
                    "costs something: write the command yourself next time.)")
            logger.info(f"Engine charged an unpaid roll for {player_id}: {line}")
            return {"line": line, "message": result.message, "note": note}
        logger.warning(f"no default cost could be applied for {player_id}: {debt}")
    except Exception as e:
        logger.warning(f"default cost failed: {e}")
    return None


# ---------------------------------------------------------------------------
# Waiting has a clock
# ---------------------------------------------------------------------------

# (kind, vocabulary, name, what happened) — picked by the act's own
# missed-consequence words, so the world's pressure sounds like this story.
# Told as the world moving on, never as the player's dawdling: fleet7-bram
# T13 heard "that errand cost you the window" and answered "that's on me"
_WAIT_CONSEQUENCES = (
    ("cart", ("cart", "carts", "carter", "carters", "wagon", "caravan", "supply", "cargo", "convoy"),
     "Another Cart Taken",
     "Another cart was taken today on the way past {target} — the cargo gone, the news running through town."),
    ("patrol", ("watch", "watchman", "watchmen", "patrol", "sentry", "guard", "guards", "roster", "picket"),
     "A Patrol Overdue",
     "A patrol sent out toward {target} has not come back on time, and people have started to count "
     "the hours."),
    ("flock", ("sheep", "flock", "cattle", "herd", "livestock", "goats", "paddock", "horses", "stable"),
     "A Paddock Raided",
     "Something got into a paddock on the side toward {target}: two animals gone, the rail pushed in."),
    ("missing", ("missing", "vanished", "taken", "disappeared", "abducted", "lost"),
     "Someone Else Missing",
     "{who} went missing on the way toward {target}."),
)
# fleet10-wenna T16: "the signs there are older now" came out as "nothing's
# touched that ground in longer than three weeks" — two hours after a cart
# was stripped there (T10). Concrete, and about the trail, never the deeds
_WAIT_FALLBACK = ("cold", (), "The Trail Grows Cold",
                  "The freshest tracks at {target} are being trampled and blown over while nobody goes: "
                  "the proof there gets harder to read the longer it waits.")
# ...and never within this many game hours of anything fresh there: a trail a
# fresh attack just laid is not cold
COLD_FRESH_HOURS = 12
# The clock stops while the world holds the road shut. fleet7-bram T13-T19:
# the gate barred till dawn, and six replies spent in town were charged as
# "Another Cart Taken" — for idling the player could not avoid. A way in
# they hold nothing for is the world's wall (`lead.way_in_blocked`); a place
# shut for the night (its road's requirement, or a fired event, says it is
# barred after dark) is waited out, and only its open hours count.
from src.services.travel import CLOSURE as _CLOSURE, NIGHT_HOURS, NIGHTLY as _NIGHTLY  # noqa: E402
_GOING = re.compile(r"\b(?:set(?:ting)?\s+out|head(?:ing)?\s+out|go(?:ing)?\s+now|leav(?:e|ing)\s+now|"
                    r"(?:walk|go|head|make)\w*\s+(?:north|south|east|west|up|out)\b"
                    r"|(?:walk|go|head|step|push)\w*\s+through\s+(?:the|that)\s+gate\b)", re.IGNORECASE)
# A verb of going, for a sentence that names the place itself
_GOING_VERB = re.compile(
    r"\b(?:go|goes|going|head|heads|heading|set(?:s|ting)?\s+(?:off|out)|leave|leaves|leaving|walk|walks|walking|"
    r"mak(?:e|es|ing)\s+(?:for|my\s+way|our\s+way)|ride|rides|riding|start|starts|starting|move|moves|moving|"
    r"march|marches|marching)\b",
    re.IGNORECASE,
)
# ...and a plan is the waiting itself: fleet6-wenna said "I'm going up to
# Wolf's Hollow at first light" at T10, T12, T14, T16 and T18 of one evening;
# fleet6-tamsin T20 "I need sleep before we walk into whatever's waiting at
# Wolf's Hollow"; T2 "whether I'm heading north tonight or waiting"
_LATER = re.compile(
    r"\b(?:first\s+light|dawn|daybreak|sunrise|sunup|daylight|tomorrow|morning|later|soon|someday|whether"
    r"|(?:before|after|once|until|till)\W+(?:I|we)|plan(?:s|ned|ning)?|need\s+(?:sleep|rest))\b",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?…])\s+|\n+")


def _walk_quest_ids(value: Any, found: set[str]) -> None:
    if isinstance(value, dict):
        if isinstance(value.get("quest_id"), str):
            found.add(value["quest_id"])
        for child in value.values():
            _walk_quest_ids(child, found)
    elif isinstance(value, list):
        for child in value:
            _walk_quest_ids(child, found)


def _campaign(session, player_id: str):
    try:
        from src.services.campaign import get_campaign_row

        campaign = get_campaign_row(session, player_id)
        return campaign if campaign is not None and campaign.status == "active" else None
    except Exception:
        return None


def _beats(session, campaign) -> list:
    from src.services.campaign import _campaign_beats

    return _campaign_beats(session, campaign)


def main_go_to(session, player: Player) -> tuple[Any, dict[str, Any]] | None:
    """(quest, step) when the campaign's own quest is next asking the player
    to GO somewhere, else None."""
    from src.services import objectives as objectives_mod

    campaign = _campaign(session, player.id)
    if campaign is None:
        return None
    spine: set[str] = set()
    for beat in _beats(session, campaign):
        for blob in (beat.trigger_data, beat.met_condition, beat.consequence_met, beat.consequence_missed):
            _walk_quest_ids(blob, spine)
    for quest in objectives_mod.active_quests_for(session, player):
        if quest.id not in spine:
            continue
        step = objectives_mod.first_undone(objectives_mod.normalize_objectives(quest.objectives))
        if step and step.get("kind") == "go_to" and step.get("target_type") == "location" and step.get("target_id"):
            return quest, step
        return None
    return None


def _act_vocabulary(session, campaign, quest) -> tuple[str, str]:
    """(what this act says goes wrong if the player is late, the job and the
    premise) — the first decides, the second breaks a tie."""
    missed: list[str] = []
    for beat in _beats(session, campaign):
        if beat.act != campaign.current_act or beat.status in ("delivered", "skipped"):
            continue
        missed += [str(beat.directive_missed or ""), str(beat.clock_label or "")]
        for op in beat.consequence_missed or []:
            if isinstance(op, dict) and op.get("op") == "world_event":
                missed += [str(op.get("name") or ""), str(op.get("description") or "")]
                missed += [str(c) for c in op.get("consequences") or []]
    context = [str(getattr(quest, "description", "") or ""), str(getattr(campaign, "premise", "") or "")]
    return " ".join(missed).lower(), " ".join(context).lower()


def _pick_consequence(player_id: str, vocabulary: tuple[str, str]) -> tuple[str, str, str]:
    missed = set(re.findall(r"[a-z]+", vocabulary[0]))
    context = set(re.findall(r"[a-z]+", vocabulary[1]))
    used = set(_wait_kinds.get(player_id, []))
    scored = [(2 * sum(1 for w in vocab if w in missed) + sum(1 for w in vocab if w in context), i,
               (kind, name, what))
              for i, (kind, vocab, name, what) in enumerate(_WAIT_CONSEQUENCES) if kind not in used]
    scored = [s for s in scored if s[0] > 0]
    if scored:
        scored.sort(key=lambda s: (-s[0], s[1]))
        return scored[0][2]
    kind, _, name, what = _WAIT_FALLBACK
    return kind, name, what


def _clock_minutes(session) -> tuple[int, int, int]:
    from src.models import WorldClock

    clock = session.query(WorldClock).first()
    if clock is None:
        return 1, 0, 0
    return clock.day, clock.hour, ((clock.day or 1) * 24 + (clock.hour or 0)) * 60 + (clock.minute or 0)


def _shut_at_night(session, target: Location) -> bool:
    """Is the way to `target` barred after dark? A road into it whose
    requirement says so ("gates are barred after the evening howl"), or a
    fired event at it that did ("North Gate barred for the night")."""
    from src.models import Connection, Event

    roads = session.query(Connection).filter(
        (Connection.to_location_id == target.id)
        | ((Connection.from_location_id == target.id) & (Connection.bidirectional == True))  # noqa: E712
    ).all()
    for text in (str(r) for road in roads for r in (road.requirements or []) if r):
        if _CLOSURE.search(text) and _NIGHTLY.search(text):
            return True
    for event in session.query(Event).filter(Event.occurred_day.isnot(None)).all():
        if target.id not in (event.locations_involved or []):
            continue
        for text in [str(c) for c in event.consequences or []]:
            if _CLOSURE.search(text) and _NIGHTLY.search(text):
                return True
    return False


def _open_minutes(start: int, end: int) -> int:
    """The minutes between two clock readings that fall outside NIGHT_HOURS."""
    total, t = 0, start
    while t < end:
        step = min((t // 60 + 1) * 60, end) - t
        if (t // 60) % 24 not in NIGHT_HOURS:
            total += step
        t += step
    return total


def _heading_there(player_input: str, target_name: str) -> bool:
    """Is the player leaving NOW — one sentence with a road out ("I head
    north", "setting out"), or the place itself with a verb of going ("I make
    for the Hollow") — and no "at first light" / "before I" / "tomorrow" in
    it? The place counts by the last word of its name, whole: in a wolf story
    "wolves" is the beasts, not Wolf's Hollow (fleet6-bram T18 "wrapped in
    wolf-pelts", T20 "This isn't a wolf problem" held the clock off)."""
    words = [w for w in re.findall(r"[A-Za-z]{4,}", target_name or "") if w.lower() not in _STOP]
    place = re.compile(rf"\b{re.escape(words[-1])}s?\b", re.IGNORECASE) if words else None
    for sentence in _SENTENCE.split(player_input or ""):
        going = _GOING.search(sentence) or (
            place is not None and place.search(sentence) and _GOING_VERB.search(sentence))
        if going and not _LATER.search(sentence):
            return True
    return False


def waited_line(player_id: str, *, engine_turn: bool = False, player_input: str = "",
                owned: bool = False) -> str | None:
    """Pre-turn: count one more reply the main quest's open road went
    untaken; past WAIT_TURNS of them (or a night), land a consequence and
    return the [WHILE YOU WAITED] line. Never raises."""
    if not player_id or engine_turn:
        return None
    try:
        return _waited_line(player_id, player_input=player_input, owned=owned)
    except Exception as e:
        logger.warning(f"waiting clock failed: {e}")
        return None


def _waited_line(player_id: str, *, player_input: str, owned: bool) -> str | None:
    from src.models import Event
    from src.services.lead import next_hop, way_in_blocked

    with get_session() as session:
        player = session.get(Player, player_id)
        found = main_go_to(session, player) if player is not None else None
        if found is None:
            _waiting.pop(player_id, None)
            return None
        quest, step = found
        target = session.get(Location, step["target_id"])
        if target is None or player.current_location_id == target.id:
            _waiting.pop(player_id, None)
            return None
        hop = next_hop(session, player, target.id)
        day, hour, now = _clock_minutes(session)
        # Shut for the night: the night's replies and hours do not count
        shut = _shut_at_night(session, target)
        closed_now = shut and hour in NIGHT_HOURS
        # The road is not theirs (yet): not waiting, blocked — the lead still
        # lights the gate, but a way in they hold nothing for is no road left
        # untaken; the count starts when it opens to them. A road barred only
        # for the night (travel.barred_tonight) pauses the count instead.
        if hop is None or (not closed_now and way_in_blocked(session, player, target.id)):
            _waiting.pop(player_id, None)
            return None
        key = (quest.id, step["id"])
        campaign = _campaign(session, player_id)
        dist = _hops_to(session, player.current_location_id, target.id)
        wait = _waiting.get(player_id)
        working = None
        if wait is None or wait["key"] != key:
            wait = {"key": key, "turns": 0, "since": now, "hop": hop, "fired": 0, "seen": now, "paused": 0,
                    "best": dist}
            _side_job_work(session, player, campaign, quest, "", wait)       # what is done already
        else:
            working = _side_job_work(session, player, campaign, quest, player_input, wait)
            # Nearer the place than they have stood since the count began —
            # past the hop too (fleet10-wenna T12 walked the Stable Yard to
            # the North Gatehouse in one move, and the clock kept counting)
            nearer = dist is not None and (wait.get("best") is None or dist < wait["best"])
            if player.current_location_id == wait["hop"] or nearer:
                wait.update(turns=0, since=now, paused=0)       # they took a step along the road
            elif working:
                # The story's own side job is no idling (fleet10-wenna T10-T16:
                # two consequences while she worked the job the campaign itself
                # pitched): neither this reply nor its hours count
                seen = wait.get("seen", now)
                wait["paused"] = wait.get("paused", 0) + (_open_minutes(seen, now) if shut else now - seen)
                logger.info(f"Waiting clock paused for {player_id}: they work '{working}'")
            elif not closed_now:
                wait["turns"] += 1
        if dist is not None and (wait.get("best") is None or dist < wait["best"]):
            wait["best"] = dist
        wait["hop"], wait["seen"] = hop, now
        _waiting[player_id] = wait
        if owned or closed_now or working or wait["fired"] >= MAX_WAIT_CONSEQUENCES \
                or _heading_there(player_input, target.name):
            return None
        open_for = max(0, (_open_minutes(wait["since"], now) if shut else now - wait["since"])
                       - wait.get("paused", 0))
        overnight = open_for >= WAIT_NIGHT_MINUTES
        if wait["turns"] < WAIT_TURNS and not overnight:
            return None

        kind, name, what = _pick_consequence(player_id, _act_vocabulary(session, campaign, quest))
        place = target.name
        # A trail is not cold the same evening something fresh happened there
        # (fleet10-wenna T10 a cart stripped at 17:00, T16 "nothing's touched
        # that ground in longer than three weeks" at 19:00): it waits
        if kind == "cold" and _fresh_at(session, target.id, now):
            logger.debug(f"waiting clock holds 'cold' for {player_id}: something fresh happened at {place}")
            return None
        missing = _register_missing(session, player, target, wait) if kind == "missing" else None
        description = what.format(target=place, who=missing["who"] if missing else "Someone else")
        session.add(Event(
            name=name, description=description, event_type="meso",
            occurred_day=day, occurred_hour=hour,
            locations_involved=[target.id], consequences=[f"The world moved on around '{quest.title}'"],
            # The line below IS its narration: the world tick must not
            # re-surface it as news on top
            player_visible=True, narrated_to_player=True,
        ))
        session.commit()
        how_long = f"{open_for // 60} open hours" if overnight else f"{wait['turns']} replies"
        wait.update(turns=0, since=now, fired=wait["fired"] + 1, paused=0)
        _wait_kinds.setdefault(player_id, []).append(kind)
    nxt = _TENSION_NEXT.get(_tension_now())
    if nxt:
        from src.tools.world_write.dm_state import update_dm_state

        update_dm_state(tension=nxt)
    logger.info(f"While they waited ({how_long}): {name} for {player_id}")
    # Who it is, by name, and what was left to find (fleet10-bram T10: the
    # DM made up "old Dennet's boy", and nothing of him could be found)
    who = (f" The one missing is {missing['name']} ({missing['role']}) — ALREADY on record as missing: name "
           f"them exactly so, in the news (a speaker line says the name), and invent no other missing person."
           + (f" Where they were last seen, at {missing['where']}, the engine left a trace to find: "
              f"{missing['trace']} — searchable there; never describe what it shows before the player looks."
              if missing.get("trace") else "")
           if missing else "")
    return (
        f"[WHILE YOU WAITED — the world kept its own pace: the road to {place} has stood open to the "
        f"player for {how_long}, and things moved on out there. ALREADY RECORDED as a world event: "
        f"\"{description}\"{who} Show it THIS reply — through someone who would know (a runner, a Watch voice, "
        "a carter at the gate, a bell) — in two or three sentences, after answering what the player did, "
        "and let it raise the stakes of the road. It is news of the world, not a verdict on the player: "
        "nobody says they were slow, late or to blame. It adds to what is already known and contradicts "
        "none of it: everything told before about that place stays as fresh as it was told. Never undo it, "
        "never let it be a rumour someone denies.]"
    )


# A watch kept for the step an event will answer counts as work only when
# the event is this close (a howl at eight, kept from five)
VIGIL_EVENT_HOURS = 4


def _hops_to(session, start: str | None, target_id: str) -> int | None:
    """Roads and parent/child steps from `start` to the target over the
    known map (lead's own adjacency), or None."""
    from collections import deque

    from src.services.lead import _adjacency

    if not start:
        return None
    adjacency = _adjacency(session)
    seen = {start: 0}
    queue = deque([start])
    while queue:
        here = queue.popleft()
        if here == target_id:
            return seen[here]
        for nxt in sorted(adjacency.get(here, ())):
            if nxt not in seen:
                seen[nxt] = seen[here] + 1
                queue.append(nxt)
    return None


def _side_jobs(session, player: Player, campaign, main_quest) -> list:
    """The player's other active quests the story itself stands behind: one
    a beat of the current act names, or one given by someone the current
    act's beats cast — fleet10-wenna's "Counting the Howls", pitched by
    Piper in the act-1 beat "A Pattern Written in Ink"."""
    from src.services import objectives as objectives_mod
    from src.services.campaign import _beat_cast

    if campaign is None:
        return []
    beats = [b for b in _beats(session, campaign) if b.act == campaign.current_act]
    named: set[str] = set()
    cast: set[str] = set()
    npcs = session.query(NPC).filter(NPC.status == "alive").all()
    for beat in beats:
        for blob in (beat.trigger_data, beat.met_condition, beat.consequence_met, beat.consequence_missed):
            _walk_quest_ids(blob, named)
        cast |= {npc.id for npc in _beat_cast(session, beat, npcs=npcs)}
    return [q for q in objectives_mod.active_quests_for(session, player)
            if q.id != main_quest.id and (q.id in named or (q.assigned_by_npc_id and q.assigned_by_npc_id in cast))]


def _side_job_work(session, player: Player, campaign, main_quest, player_input: str,
                   wait: dict[str, Any]) -> str | None:
    """The title of the story's side job (`_side_jobs`) the player is
    working right now, or None: a step of it done since the last count, or a
    watch they keep (complications.keeping_vigil) for a step an event due
    within VIGIL_EVENT_HOURS will answer — "I'll wait here, I want to hear it
    myself" (fleet10-wenna T10) for "Stand out the next howl". Remembers
    what is done on `wait`."""
    from src.services import objectives as objectives_mod

    jobs = _side_jobs(session, player, campaign, main_quest)
    done = {f"{q.id}:{s.get('id')}" for q in jobs for s in objectives_mod.normalize_objectives(q.objectives)
            if s.get("done")}
    before = wait.get("side_done")
    wait["side_done"] = sorted(done)
    fresh = done - set(before) if before is not None else set()
    if fresh:
        return next((q.title for q in jobs if any(k.startswith(f"{q.id}:") for k in fresh)), None)
    if not jobs or not (player_input or "").strip():
        return None
    from src.services.complications import keeping_vigil

    if not keeping_vigil(player_input):
        return None
    from src.models import Event
    from src.services.job_offers import vigil_matches

    _, _, now = _clock_minutes(session)
    due = [e for e in session.query(Event).filter(Event.scheduled_day.isnot(None), Event.occurred_day.is_(None),
                                                  Event.player_visible == True).all()  # noqa: E712
           if 0 <= ((e.scheduled_day or 1) * 24 + (e.scheduled_hour or 0)) * 60 - now <= VIGIL_EVENT_HOURS * 60]
    for quest in jobs:
        for s in objectives_mod.normalize_objectives(quest.objectives):
            if s.get("kind") == "custom" and not s.get("done") and any(vigil_matches(s.get("text") or "", e)
                                                                       for e in due):
                return quest.title
    return None


def _fresh_at(session, location_id: str, now: int) -> bool:
    """Did anything happen at this place within COLD_FRESH_HOURS?"""
    from src.models import Event

    for event in session.query(Event).filter(Event.occurred_day.isnot(None)).all():
        if location_id not in (event.locations_involved or []):
            continue
        at = ((event.occurred_day or 1) * 24 + (event.occurred_hour or 0)) * 60
        if 0 <= now - at <= COLD_FRESH_HOURS * 60:
            return True
    return False


# ---------------------------------------------------------------------------
# Someone missing is someone (fleet10-bram T10-T20)
# ---------------------------------------------------------------------------

# (who they were, how they look, the trace's label, what it shows, the thing
# the player can carry back). The trace's words never name them: the player
# has not heard the name until someone says it
_MISSING = (
    ("goatherd's boy", "a thin boy of about twelve in a patched smock, always with a goat on a rope",
     "Frayed goat tether",
     "A goat's tether snagged on a thornbush, chewed through at one end and knotted the way a child knots — "
     "the ground beside it churned, small boot prints running one way and not coming back.",
     "Chewed goat tether"),
    ("woodcutter", "a broad, quiet woman in a split leather apron, a hatchet always at her belt",
     "Dropped hatchet",
     "A woodcutter's hatchet lying in the bracken, haft dark with handling — and from it, two long drag "
     "marks scored through the leaf litter, heading away from the road.",
     "Woodcutter's hatchet"),
    ("drover's daughter", "a sturdy girl of sixteen in a drover's oilcloth, mud to the knees",
     "Small boot in the mud",
     "One small boot, still laced, stuck fast in the mud where its owner was pulled out of it; the prints "
     "beyond it are bare-footed, and running.",
     "Small muddy boot"),
    ("charcoal-burner", "a soot-grey old man with a sack over one shoulder and a cough you hear first",
     "Spilled charcoal sack",
     "A charcoal sack split open across the track, the lumps scattered as if it was dropped at a run; a "
     "smear of soot leads off the track toward the trees.",
     "Torn charcoal sack"),
)
# Places a trace may lie: never a map container
_NO_TRACE_TYPES = {"root", "world", "region_1", "region_2", "region_3", "continent", "kingdom", "province",
                   "galaxy", "sector", "system", "planet", "settlement", "city", "town", "village"}


def _register_missing(session, player: Player, target: Location, wait: dict[str, Any]) -> dict[str, Any]:
    """Make the missing person real — an NPC on record as missing, named in
    the world's own style, never a name in use — and leave a trace to find
    where they were last seen (`_trace_place`: the place itself, or the road's
    near end when it has no room). Runs in the caller's session (flushes; the
    caller commits). Returns {"name", "role", "who", "trace", "where"}."""
    import zlib

    from src.models import NPCTier
    from src.services.complications import world_name

    seed = f"{player.id}:{wait.get('key')}:{wait.get('fired', 0)}"
    role, looks, label, text, carried = _MISSING[zlib.crc32(seed.encode()) % len(_MISSING)]
    name = world_name(session, seed)
    home = player.current_location_id
    npc = NPC(name=name, tier=NPCTier.MINOR, profession=role, status="missing",
              current_location_id=None, home_location_id=home,
              description_physical=looks[:1].upper() + looks[1:] + ".",
              description_personality="Missing — last seen on the way toward " + (target.name or "the road") + ".",
              goals=[], secrets=[], current_mood="unknown")
    session.add(npc)
    session.flush()
    out = {"name": name, "role": role, "who": f"{name}, a {role},", "trace": "", "where": ""}
    try:
        place, evict = _trace_place(session, player, target)
        if place is not None:
            from src.services.scene_objects import normalize_objects

            objects = [o for o in normalize_objects(place.objects) if o["id"] != evict]
            objects.append({
                "id": f"trace_{npc.id[:8]}", "kind": "search", "label": label,
                "x": 30.0 if not any(abs(o["x"] - 30.0) < 12 for o in objects) else 70.0, "y": 72.0,
                "payload": {"text": text, "item": {"name": carried, "type": "quest_item",
                                                   "description": text.split(" — ")[0].split(";")[0] + "."}},
            })
            place.objects = normalize_objects(objects)
            out.update(trace=label, where=place.name)
            logger.info(f"Missing person on record: {name} ({role}); trace '{label}' at {place.name}")
    except Exception as e:      # the person still counts without the trace
        logger.warning(f"missing person's trace not placed: {e}")
    return out


def _trace_place(session, player: Player, target: Location) -> tuple[Location | None, str | None]:
    """(where a trace can lie, the id of a spent Thing it replaces): the
    target, if its authored pool has room or a spent Thing to give way; else
    a place beside it on the known map that does (the road's near end),
    never a map container or the room the player stands in."""
    from src.services.lead import _adjacency
    from src.services.scene_objects import AUTHORED_KINDS, MAX_OBJECTS_PER_LOCATION, normalize_objects

    def room_in(place: Location | None) -> tuple[bool, str | None]:
        if place is None or str(getattr(place.type, "value", place.type) or "").lower() in _NO_TRACE_TYPES:
            return False, None
        authored = [o for o in normalize_objects(place.objects) if o["kind"] in AUTHORED_KINDS and o["kind"] != "rest"]
        if len(authored) < MAX_OBJECTS_PER_LOCATION:
            return True, None
        spent = next((o for o in authored if o.get("consumed")), None)
        return (True, spent["id"]) if spent is not None else (False, None)

    near = [session.get(Location, i) for i in sorted(_adjacency(session).get(target.id, ()))]
    near = [p for p in near if p is not None and p.id != player.current_location_id]
    near.sort(key=lambda p: (str(getattr(p.type, "value", p.type) or "").lower() in ("building", "interior", "room"),
                             p.name or ""))
    for place in [target] + near:
        ok, evict = room_in(place)
        if ok:
            return place, evict
    return None, None

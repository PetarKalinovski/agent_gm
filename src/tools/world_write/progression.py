"""DM tools for the progression engine: the roll and the merchant's shelf.

Two tools, both label-in / label-out. The DM picks WORDS — a skill, a
difficulty, a gear tier — and `src/services/progression.py` owns every number
behind them. `roll_check` never tells the DM what it rolled (the dice live in
the log, stripped from the return value), and `stock_merchant` prices from
`TIER_PRICE`, so a legendary blade can never turn up on a village shelf for
five coins.
"""

import math
import re
from typing import Any

from loguru import logger
from strands import tool

from src.models import Item, NPC, Player, WorldClock, get_session
from src.services import perception
from src.services import progression as prog
from src.services import stakes

# A roll is the dice deciding an act; its reason names the act. fleet8-tamsin
# T9 rolled athletics a second time with "placeholder - not needed": it came
# back critical_failure, the player saw a "Failed" floater, the prose landed
# the drop clean and nothing was charged. Only words ABOUT the roll: "getting
# the guard to ignore it", "spotting the spurious seal" and "persuading him
# his help is not needed" are acts, and roll
_JUNK_REASON = re.compile(
    r"^\W*(?:test(?:ing)?|n/?a|none|null|tbd|dummy|x+|unneeded|(?:(?:roll|check)\s+)?(?:is\s+)?"
    r"not\s+(?:really\s+)?needed)\W*$"
    r"|\bplaceholder\b|(?:^|\s[-–—:(]|[-–—:(])\s*not\s+(?:really\s+)?needed\b"
    r"|\b(?:(?:roll|check)\s+(?:is\s+)?not\s+(?:really\s+)?needed|(?:no|unneeded|dummy|spurious|throwaway)\s+"
    r"(?:roll|check|result)|(?:roll|check)\s+(?:needed|required)\s*:\s*no"
    r"|(?:ignore|discard)\s+(?:this|that|the)\s+(?:roll|check|result)"
    r"|(?:roll|check)\s+to\s+(?:discard|ignore|throw\s+away))\b",
    re.IGNORECASE,
)
# One act, one roll: within a reply (stakes.current_turn) a second roll of
# the same skill for the same act returns the first result. Per player:
# (reply, [{skill, reason, about_npc_id, payload}])
_REPLY_ROLLS: dict[str, tuple[int, list[dict[str, Any]]]] = {}
SAME_ACT_SHARE = 0.6          # of the shorter reason's content words, shared
# The body pays (fleet8-bram: "winded" from T4 to T20, and nothing was ever
# harder for it): hurt, a climb, a run or a sneak goes a step harder (two,
# badly hurt); past "desperate" it is rolled at a disadvantage. The DM reads
# a phrase, never a number
_WOUND_SKILLS = ("athletics", "stealth")
_WOUND = {
    "winded": (1, "short of breath, their ribs slow them"),
    "hurt": (1, "the wound pulls at every move"),
    "badly_hurt": (2, "badly hurt, every move costs them"),
    "critical": (2, "barely on their feet"),
}
_LADDER = ("easy", "tricky", "hard", "desperate")


def _same_act(first: dict[str, Any], skill: str, reason: str, about_npc_id: str) -> bool:
    if first["skill"] != skill or (first.get("about_npc_id") or "") != (about_npc_id or ""):
        return False
    a, b = stakes.content_stems(first.get("reason") or ""), stakes.content_stems(reason)
    if not a or not b:
        return True                     # a roll with no reason is the same act again
    return len(a & b) >= max(1, math.ceil(SAME_ACT_SHARE * min(len(a), len(b))))


def _wound(player: Player, skill: str, about_npc_id: str) -> dict[str, Any] | None:
    """{"health", "steps", "because"} when the player's hurt tells on this
    skill — never on a recognition roll (their face, not their legs)."""
    skill = (skill or "").strip().lower()
    if skill not in _WOUND_SKILLS or (about_npc_id and skill == perception.RECOGNITION_SKILL):
        return None
    hit = _WOUND.get(str(player.health_status or "healthy"))
    return {"health": player.health_status, "steps": hit[0], "because": hit[1]} if hit else None


def _harder(difficulty: str, steps: int) -> tuple[str, bool]:
    """(the difficulty `steps` up the ladder, whether it ran past the top)."""
    word = (difficulty or "").strip().lower()
    if word not in _LADDER:
        return difficulty, False          # the roll refuses the unknown word itself
    index = _LADDER.index(word) + steps
    return _LADDER[min(index, len(_LADDER) - 1)], index >= len(_LADDER)


# No offered choice is a ~1% roll (fleet10-bram T18-T19: the DM's "hard" with
# its own disadvantage for "moving hurt, in the dark", plus the wound's step,
# made DC 20 at disadvantage — a natural 19-20 on both dice). The DM's
# disadvantage for the wound the engine already counts is dropped (one cause,
# one penalty), and no roll goes below ODDS_FLOOR: a disadvantage that would
# take it there is dropped, then the difficulty eases. The DM reads a word
# for how it stood going in, never a number
ODDS_FLOOR = 0.10
# (the body, never the ground: "over the broken palisade", "the weak plank in
# the fence" are the DM's own cause for a disadvantage, and it stands)
_BODY_CAUSE = re.compile(
    r"\b(?:hurt|wound\w*|injur\w*|limp\w*|bleed\w*|blood(?:y|ied)|bitten|bite|knee|ankle|leg|ribs?|shoulder"
    r"|winded|breathless|exhaust\w*|battered|bruised|lame|pain\w*|aching|weakened|weak\s+(?:from|with|as))\b",
    re.IGNORECASE,
)
# (only the poor end is said: a fair chance needs no word, a long shot does)
_ODDS_WORDS = ((0.20, "a long shot"), (0.40, "against the odds"))


def _chance(difficulty: str, bonus: int, effective: str) -> float:
    """The engine's own odds that the roll comes up success or better
    (`progression.roll_skill_check`: a natural 20 always does, a 1 never)."""
    dc = prog.DIFFICULTY_DC[difficulty]
    one = sum(1 for n in range(1, 21) if n == 20 or (n != 1 and n + bonus >= dc)) / 20
    if effective == "advantage":
        return 1 - (1 - one) ** 2
    if effective == "disadvantage":
        return one ** 2
    return one


def _odds_word(chance: float) -> str | None:
    return next((word for top, word in _ODDS_WORDS if chance < top), None)


# fleet9-bram T1: the player asked the Captain two questions ("What's really
# happening on that north road? And why should I trust a uniform…?"), the DM
# rolled persuasion on "pressing the Captain to admit how bad it really is",
# the dice came up critical, and the Captain spent the act-2 roster secret
# in the first exchange. A social roll answers a social ACT the player chose
# — a lie, pressure, a bribe, a threat, a haggle (`roll_intent.risky_act`) —
# never a plain question: the person answers or refuses in their own voice.
# Not a plea to be let through (that is pressing someone against orders),
# not with a danger in the room (`encounters.danger_among`: that talk is a
# confrontation), never a recognition roll the context ordered. The tool
# reads what the player typed this reply (`note_turn_input`, kept where the
# reply's context is built); unknown, it rolls as it always did.
_SOCIAL_SKILLS = ("persuasion", "cunning")
_TURN_INPUT: dict[str, tuple[int, str]] = {}     # player -> (reply, what they typed)
_ASKS = re.compile(r"\?|\b(?:ask|asks|asking|wonder|wondering|tell\s+me|explain)\b", re.IGNORECASE)
_PLEA = re.compile(r"\blet\s+(?:me|us)\s+(?:through|past|in|out|go|by|pass)\b"
                   r"|\bopen\s+(?:the|this|that|your)\s+(?:gate|door|bar)\b", re.IGNORECASE)
# ...and the roll is about getting words out of someone ("pressing the
# Captain to admit how bad it really is"), not an act of the player's own
# a question rode along with (fleet3-tamsin T4: ditching a stolen purse,
# then "You there — what's your name?")
_INQUIRY = re.compile(
    r"\b(?:admit\w*|tell\w*|talk\w*|reveal\w*|confess\w*|answer\w*|shar(?:e|es|ing)|explain\w*|spill\w*"
    r"|open(?:s|ing)?\s+up|learn\w*|find(?:ing)?\s+out|ask\w*|question\w*|draw(?:s|ing)?\s+(?:it\s+|him\s+|her\s+"
    r"|them\s+)?out|pry(?:ing)?\s+(?:it\s+)?(?:out|loose)|what\s+(?:he|she|they)\s+(?:know|knows|saw|heard|means?)"
    r"|information|info|secrets?|truth|the\s+real\s+story|how\s+bad)\b",
    re.IGNORECASE,
)
# A social act `roll_intent.risky_act` does not know — it orders a roll
# FIRST, so it is built to be sure, and misses "I lie and tell him",
# "I grab his collar", "I slide two coins across", "I keep pressing him",
# "or I start breaking fingers". Refusing a roll on one of those is worse
# than rolling on a question, so any of them keeps the roll. In what they
# do (outside their quotes):
_PRESSING = re.compile(
    r"\bI\s+(?:[\w'’-]+\s+){0,2}?(?:lie|lies|lied)\b(?!\s+(?:down|back|still|awake|low|flat|in|on|there)\b)"
    r"|\b(?:grab\w*|seiz\w*|slam\w*|shov\w*|pin(?:s|ned|ning)?|twist\w*|collar\w*|corner(?:s|ed|ing)?\s+(?:him|her"
    r"|them)|block\w*\s+(?:his|her|their)\s+(?:way|path|exit)|loom\w*|glar\w*|stare\w*\s+(?:him|her|them)\s+down"
    r"|in\s+(?:his|her|their)\s+face|press(?:es|ed|ing)?\b|push(?:es|ed|ing)?\s+(?:harder|him|her|them|on|it)"
    r"|keep\s+at\s+(?:him|her|them)|lean(?:s|ed|ing)?\s+(?:in|closer|over|hard)|charm\w*|smil\w*\s+sweetly"
    r"|coins?|silver|purse|bribe\w*|pay(?:s|ing)?\s+(?:him|her|them)|across\s+the\s+(?:bar|table|counter)"
    r"|knife|blade|dagger|sword|steel)\b",
    re.IGNORECASE,
)
# ...and in what they say: a threat ("…or I start breaking fingers")
_THREAT_SAID = re.compile(r"\bor\s+(?:else|I\b|we\b|you['’]ll)|\bunless\s+you\b|\byou['’]ll\s+regret\b", re.IGNORECASE)
_SAID = re.compile(r"[\"“][^\"”\n]*[\"”]")


def note_turn_input(player_id: str | None, player_input: str | None) -> None:
    """What the player typed for the reply being written (an engine input —
    "[OPENING]", "[COMBAT OUTCOME]" — is kept as such and never asks)."""
    if player_id:
        _TURN_INPUT[player_id] = (stakes.current_turn(player_id), str(player_input or ""))


def _pure_question(player_id: str, skill: str, about_npc_id: str, reason: str = "") -> bool:
    """Did the player only ask, this reply, and is this roll about the
    answer (see above)? Never raises (False)."""
    if skill not in _SOCIAL_SKILLS or about_npc_id or not _INQUIRY.search(reason or ""):
        return False
    reply, typed = _TURN_INPUT.get(player_id, (None, ""))
    if reply != stakes.current_turn(player_id) or not typed.strip() or typed.lstrip().startswith("["):
        return False
    try:
        from src.services import encounters, roll_intent

        if roll_intent.risky_act(typed) or not _ASKS.search(typed) or _PLEA.search(typed):
            return False
        if _PRESSING.search(_SAID.sub(" ", typed)) or any(_THREAT_SAID.search(q) for q in _SAID.findall(typed)):
            return False
        return not encounters.danger_among(player_id)
    except Exception as e:
        logger.debug(f"pure-question check skipped: {e}")
        return False


# fleet10-wenna T20: the night-edge scout came in as she turned for town, and
# the same reply wrote "You plant your feet and go still", rolled cunning on
# "facing down the scout wolf" and sent it off — the session's one danger
# decided for her, though the scout's own line said the choice was hers. A
# roll that faces someone this reply brought in (`encounters.introduced_now`)
# waits for the player's answer, unless what they DID (outside their quotes)
# already met it — a watch kept for the wolf that came. Its reason names
# them, or names nobody here on a reply whose player attempted nothing
_FACING_UNNAMED = ("persuasion", "cunning", "stealth", "athletics")


def _unchosen_danger(session, player: Player, skill: str, reason: str) -> NPC | None:
    """The just-introduced danger this roll would decide for the player, or
    None. Words unknown (the CLI) or an engine input: never. Never raises."""
    try:
        from src.services import encounters, roll_intent

        new_ids = encounters.introduced_now(player.id)
        if not new_ids:
            return None
        reply, typed = _TURN_INPUT.get(player.id, (None, ""))
        if reply != stakes.current_turn(player.id) or not typed.strip() or typed.lstrip().startswith("["):
            return None
        new = [n for n in (session.get(NPC, i) for i in new_ids) if n is not None and n.status == "alive"]
        faced = [n for n in new if encounters._named_enemies([n], reason)]
        if not faced and skill in _FACING_UNNAMED and not roll_intent.attempted(typed):
            others = [n for n in session.query(NPC).filter(
                NPC.current_location_id == player.current_location_id, NPC.status == "alive").all()
                if n.id not in new_ids]
            if not encounters._named_enemies(others, reason):
                faced = new
        if not faced:
            return None
        acts = roll_intent._narration(typed)
        for npc in faced:
            beastly = encounters._is_beast(npc) and roll_intent._BEASTLY.search(acts)
            if beastly or encounters._named_enemies([npc], acts):
                return None
        return faced[0]
    except Exception as e:
        logger.debug(f"just-introduced danger check skipped: {e}")
        return None

# Gear kinds a merchant may stock, plus the `Item.type` each maps to. A
# trinket is not one of the legacy item types, so it lands as "misc".
GEAR_KINDS = prog.SLOTS
_ITEM_TYPE_BY_SLOT = {"weapon": "weapon", "armor": "armor", "trinket": "misc"}
_STOCK_KINDS = tuple(GEAR_KINDS) + ("consumable",)
MAX_STOCK_COUNT = 10


@tool
def roll_check(
    player_id: str,
    skill: str,
    difficulty: str,
    modifier: str = "none",
    reason: str = "",
    about_npc_id: str = "",
) -> dict[str, Any]:
    """Resolve a risky, uncertain action with an engine roll. Use this BEFORE
    narrating whether it worked.

    Call it for anything the player could plausibly fail: sneaking, lying,
    persuading, climbing, picking a lock, spotting an ambush, forcing a door,
    holding a poker face. Do NOT call it for safe actions (walking, asking a
    question, buying openly) or for real-time fights (that is start_combat).

    You choose the WORDS. The engine owns the numbers and will not tell you
    what they were — narrate the outcome it returns and nothing about how it
    was decided. One act, one roll: the same act rolled again in one reply
    returns the first result, and a placeholder roll is refused.

    Args:
        player_id: The player's ID.
        skill: One of "stealth", "persuasion", "athletics", "perception", "cunning".
        difficulty: One of "easy", "tricky", "hard", "desperate".
        modifier: "advantage" if the fiction clearly favours them (a distraction
            they set up, a lie backed by evidence, a rope they brought),
            "disadvantage" if it clearly works against them (watched, no
            tools), otherwise "none". Their wounds the engine adds itself.
        reason: One short phrase for the log, e.g. "slipping past the dock guard".
        about_npc_id: Only for a recognition check your context asked for —
            the NPC who might be placing the player. Passing it makes the
            answer stick for the rest of the day, so you never have to ask
            twice in one scene.

    Returns:
        Dictionary with the outcome label ("critical_success", "success",
        "partial", "failure" or "critical_failure") and how to narrate it.
    """
    situation: dict[str, Any] | None = None
    wound: dict[str, Any] | None = None
    skill_word = (skill or "").strip().lower()
    if _JUNK_REASON.search(reason or ""):
        logger.info(f"roll_check refused a junk reason: {reason!r}")
        return {"error": (
            "Refused — nothing was rolled. A roll is the dice deciding an act the player is attempting "
            "NOW, and its reason names that act (\"dropping from the loft\"). Never roll a placeholder or a "
            "roll you mean to discard: if nothing is uncertain, roll nothing and narrate."
        )}
    reply = stakes.current_turn(player_id)
    made: list[dict[str, Any]] = []
    if reply:
        seen, made = _REPLY_ROLLS.get(player_id, (0, []))
        if seen != reply:
            made = []
            _REPLY_ROLLS[player_id] = (reply, made)
        first = next((m for m in made if _same_act(m, skill_word, reason, about_npc_id)), None)
        if first is not None:
            # No "event": the client shows one roll, and stakes pairs none
            again = first["payload"]
            logger.info(f"roll_check repeated in one reply ({skill_word}: {reason!r}) -> {again['outcome']} stands")
            return {
                "success": True, "outcome": again["outcome"], "skill": again["skill"],
                "difficulty": again["difficulty"], "repeat": True,
                "instruction": (f"Already rolled this reply — one act, one roll: it came back "
                                f"{again['outcome'].replace('_', ' ').upper()}, and that stands. "
                                + again["instruction"]),
            }
    with get_session() as session:
        player = session.get(Player, player_id)
        if not player:
            return {"error": "Player not found"}

        # A willing person whom a story beat firing now has speak says it:
        # the dice never withhold a beat's words (fleet8-bram T19)
        try:
            teller = None if about_npc_id else stakes.beat_tells_it(session, player, skill_word, reason)
        except Exception as e:
            logger.debug(f"beat check skipped: {e}")
            teller = None
        if teller is not None:
            logger.info(f"roll_check not rolled: a story beat has {teller.name} tell it ({reason!r})")
            return {
                "success": True, "no_roll": True, "skill": skill_word,
                "instruction": (
                    f"No roll — none was needed: {teller.name} is willing, and the story beat in your "
                    "context already decides what they say. Play it exactly as the beat writes it, in their "
                    "own voice (a pause or a lowered voice if the fiction wants one), but say it — and "
                    "nothing beyond it this reply. Nothing was rolled: there is no outcome to narrate and "
                    "nothing is owed."
                ),
            }

        # A plain question is no act to roll on (fleet9-bram T1)
        if _pure_question(player.id, skill_word, about_npc_id, reason):
            asked = None
            if player.current_location_id:
                here = [n.id for n in session.query(NPC).filter(
                    NPC.current_location_id == player.current_location_id, NPC.status == "alive").all()]
                asked = stakes._npc_in_reason(session, reason, here)
            who = asked.name if asked is not None else "Whoever they asked"
            logger.info(f"roll_check not rolled: the player only asked ({skill_word}: {reason!r})")
            return {
                "success": True, "no_roll": True, "skill": skill_word,
                "instruction": (
                    "No roll — the player asked; they attempted nothing that can fail (no lie, no pressure, "
                    f"no bribe, no threat). {who} answers or refuses in their own voice: what they are willing "
                    "to say, they say; what they keep — a secret, anything SEALED — they keep, with a "
                    "deflection or their cover story. Nothing was rolled: there is no outcome to narrate "
                    "and nothing is owed."
                ),
            }

        # A danger this very reply brought in is the player's to answer
        # (fleet10-wenna T20): shown now, rolled the reply after they choose
        unchosen = _unchosen_danger(session, player, skill_word, reason)
        if unchosen is not None:
            logger.info(f"roll_check refused: {unchosen.name} only came in this reply ({skill_word}: {reason!r})")
            return {"error": (
                f"Not yet — nothing was rolled. {unchosen.name} only came into the scene in THIS reply: the "
                "player has not seen it, so what they do about it is theirs to choose. Show it and STOP — two or "
                "three sentences, where it is and what it seems to want — and never write their answer for them "
                "(no going still, no backing away, no blow), never roll it, never start the fight. The roll comes "
                "in the reply after they choose (roll_check first, then)."
            )}

        # The player's own body: hurt, the physical roll is harder — and the
        # DM's own disadvantage for that same hurt is not charged twice
        wound = _wound(player, skill_word, about_npc_id)
        same_cause = bool(wound) and (modifier or "").strip().lower() == "disadvantage" \
            and bool(_BODY_CAUSE.search(reason or ""))
        dm_word = "none" if same_cause else modifier
        # ONE code-computed factor at most, layered over the DM's own word.
        # The tool combines (perception must never be imported by progression).
        factor = perception.situational_factor(session, player, skill)
        if factor:
            situation = {
                **factor,
                "effective": perception.combine_modifier(dm_word, factor["effect"]),
            }
        rolled_difficulty, roll_situation = difficulty, situation
        if same_cause and not factor:
            roll_situation = {"effective": "none"}
        if wound:
            rolled_difficulty, past_top = _harder(difficulty, wound["steps"])
            if past_top:
                base = (roll_situation or {}).get("effective") or (dm_word or "none").strip().lower()
                roll_situation = {**(roll_situation or {}),
                                  "effective": perception.combine_modifier(base, "disadvantage")}
        # ...and never below the floor: a disadvantage that would take it
        # there goes first, then the difficulty eases
        odds, eased = None, None
        effective = ((roll_situation or {}).get("effective") or modifier or "none").strip().lower()
        if (rolled_difficulty or "").strip().lower() in prog.DIFFICULTY_DC and skill_word in prog.SKILLS \
                and effective in prog.MODIFIERS:
            rolled_difficulty = rolled_difficulty.strip().lower()
            bonus = prog.player_level(player) + prog.trait_bonus(player, skill_word)
            start = (rolled_difficulty, effective)
            while _chance(rolled_difficulty, bonus, effective) < ODDS_FLOOR - 1e-9:
                if effective == "disadvantage":
                    effective = "none"
                elif _LADDER.index(rolled_difficulty) > 0:
                    rolled_difficulty = _LADDER[_LADDER.index(rolled_difficulty) - 1]
                else:
                    break
            if (rolled_difficulty, effective) != start:
                eased = start
                roll_situation = {**(roll_situation or {}), "effective": effective}
            odds = _odds_word(_chance(rolled_difficulty, bonus, effective))

        result = prog.roll_skill_check(player, skill, rolled_difficulty, modifier,
                                       situation=roll_situation)

        if "error" not in result and about_npc_id:
            if result["skill"] == perception.RECOGNITION_SKILL:
                clock = session.query(WorldClock).first()
                perception.remember_recognition(
                    player_id, about_npc_id,
                    recognized=result["outcome"] in perception.RECOGNISED_OUTCOMES,
                    day=clock.day if clock else 1,
                )
            else:
                logger.debug(
                    f"roll_check ignored about_npc_id on a {result['skill']} check"
                )

    if "error" in result:
        # Never silently coerce — a coerced word is a number the DM did not choose
        return result

    # The dice, the DC and the bonuses are for the log ONLY. Everything the DM
    # can read, it can leak into prose.
    debug = result.pop("_debug", {})
    outcome = result["outcome"]
    logger.info(
        f"roll_check {result['skill']}/{result['difficulty']} "
        f"({result['modifier']}) -> {outcome} "
        f"[roll {debug.get('natural')} + lvl {debug.get('level_bonus')} "
        f"+ trait {debug.get('trait_bonus')} = {debug.get('total')} vs DC {debug.get('dc')}"
        f"{', effective ' + debug['effective_modifier'] if debug.get('effective_modifier') else ''}]"
        + (f" [{wound['health']}: asked {difficulty}]" if wound else "")
        + (" [the DM's disadvantage was the wound's: not charged twice]" if same_cause else "")
        + (f" [eased from {eased[0]}/{eased[1]}: odds floor]" if eased else "")
        + (f" [{odds}]" if odds else "")
        + (f" :: {reason}" if reason else "")
    )

    payload = {
        "success": True,
        "outcome": outcome,
        "skill": result["skill"],
        "difficulty": result["difficulty"],
        "event": "check_result",
        "instruction": prog.OUTCOME_INSTRUCTIONS[outcome],
    }
    if situation:
        # A label and a phrase — never the word "advantage", never a number
        payload["situation"] = {"factor": situation["factor"],
                                "because": situation["because"]}
        payload["instruction"] += (
            f" The world was with them: {situation['because']} — let that show in how it goes."
            if situation["effect"] == "advantage" else
            f" The world was against them: {situation['because']} — let that show in how it goes."
        )
    if wound:
        payload["wound"] = {"health": wound["health"], "because": wound["because"]}
        payload["instruction"] += (f" Their body was against them: {wound['because']} — it was harder for "
                                   "that; let the wound show in how it goes.")
    if odds:
        # How it stood going in, as a word the prose may echo ("it was always
        # a long shot") — fleet10-bram T19's 1% climb read like any other
        payload["odds"] = odds
        payload["instruction"] += f" Going in, it was {odds} — let that weigh in how it reads, in words."
    if reply:
        made.append({"skill": result["skill"], "reason": reason, "about_npc_id": about_npc_id,
                     "payload": {k: v for k, v in payload.items() if k != "event"}})
    # A won roll against a danger here holds the ambush off it (fleet8-wenna
    # T11 -> T12: the calmed scout wolf was the next reply's ambush), and
    # answers the complication that brought it
    try:
        from src.services import complications, encounters

        calmed = encounters.note_check(player_id, outcome, result["skill"], reason)
        if calmed:
            complications.note_calmed(player_id, calmed)
    except Exception as e:  # bookkeeping never costs the roll
        logger.debug(f"encounter hold after a roll skipped: {e}")
    return payload


def _stock_error(message: str, valid: list[str]) -> dict[str, Any]:
    """A refusal that teaches the rule instead of quietly accepting it."""
    return {"error": message, "valid": valid}


def _prepare_entry(entry: Any) -> dict[str, Any]:
    """Validate one stock line into an Item, or return an {"error": ...}."""
    if not isinstance(entry, dict):
        return _stock_error(
            "Each entry must be a dictionary like "
            '{"kind": "weapon", "tier": "common", "name": "Ashwood Blade"}.',
            list(_STOCK_KINDS),
        )

    kind = str(entry.get("kind") or "").strip().lower()
    name = str(entry.get("name") or "").strip()
    if kind not in _STOCK_KINDS:
        return _stock_error(
            f"Unknown kind {kind!r}. A merchant stocks gear or consumables.",
            list(_STOCK_KINDS),
        )
    if not name:
        return _stock_error("Every entry needs a 'name' — the flavour is yours.", [])

    try:
        count = int(entry.get("count") or 1)
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(MAX_STOCK_COUNT, count))

    description = str(entry.get("description") or "")
    visual_desc = str(entry.get("visual_desc") or "").strip() or None
    # Same slug rule as progression.item_key, so an equipment pointer written
    # against this item resolves back to it.
    item_id = prog._slug(name) or "stock_item"

    if kind in GEAR_KINDS:
        tier = str(entry.get("tier") or "").strip().lower()
        tier_idx = prog.tier_index(tier)
        if tier_idx is None:
            return _stock_error(
                f"Unknown tier {tier!r} for {name}. Gear tiers are fixed words.",
                list(prog.TIER_LABELS),
            )
        if tier_idx >= prog.LEGENDARY_INDEX:
            return _stock_error(
                f"Legendary gear is never for sale — {name} cannot sit on a shelf. "
                "Legendary is won: make it a quest reward or the prize of a boss. "
                "Stock master or below.",
                list(prog.TIER_LABELS[:prog.LEGENDARY_INDEX]),
            )
        item = Item(
            id=item_id,
            name=name,
            type=_ITEM_TYPE_BY_SLOT[kind],
            value=prog.TIER_PRICE[kind][tier_idx],
            description=description,
            stackable=False,
            quantity=count,
            slot=kind,
            tier=prog.TIER_LABELS[tier_idx],
            visual_desc=visual_desc,
            # Armor-only, and a garbled style word clamps rather than refusing
            # the whole stock line
            attire=perception.normalize_attire(entry.get("attire"), slot=kind),
        )
        return {"item": item}

    effect_tag = str(entry.get("effect_tag") or "").strip().lower()
    if effect_tag not in prog.CONSUMABLES:
        return _stock_error(
            f"Unknown effect_tag {effect_tag!r} for {name}. A consumable is one of "
            "these three; the engine decides what each one does.",
            list(prog.CONSUMABLES),
        )
    item = Item(
        id=item_id,
        name=name,
        type="consumable",
        value=prog.CONSUMABLES[effect_tag]["price"],
        description=description,
        stackable=True,
        quantity=count,
        effect_tag=effect_tag,
        visual_desc=visual_desc,
    )
    return {"item": item}


@tool
def stock_merchant(npc_id: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Fill a merchant's stock with real, priced gear.

    Each entry is {"kind": ..., "tier"/"effect_tag": ..., "name": ..., "count": 1}:
      - kind "weapon", "armor" or "trinket" -> requires "tier": one of
        "crude", "common", "fine", "superior", "master", "legendary"
      - kind "consumable" -> requires "effect_tag": "heal_light", "heal_strong"
        or "surge"
    "name" is yours (flavour: "Ashwood Blade", "Harbor Mail", "Dockhand's Draught").
    Prices, damage, and protection are set by the engine from the tier — do not
    invent numbers, and do not tell the player a price you have not read back
    from get_inventory.

    Two optional keys per entry:
      "visual_desc": one short concrete phrase for what the piece looks like
        ("a boiled-leather coat with a brass lantern badge").
      "attire": ARMOR ONLY. How the garment makes its wearer READ to strangers:
        {"style": "noble"|"fine"|"common"|"rough"|"filthy",
         "uniform_faction_id": "<faction id>" or null,
         "concealing": true|false}
        Set uniform_faction_id when the piece is a faction's recognisable
        colours — strangers will then treat the wearer as that faction, with
        everything that implies. Set concealing for hoods, masks and veils:
        strangers cannot identify the wearer at all, and people who know them
        have to work for it. The engine owns what each word does.

    Stock what the merchant would plausibly have. A village smith carries crude
    and common; only a capital armourer carries master. Legendary is never for
    sale — it is a reward.

    Args:
        npc_id: The merchant NPC's ID.
        entries: The stock lines, as described above.

    Returns:
        Dictionary with what was stocked and the price of each piece.
    """
    if not isinstance(entries, list) or not entries:
        return _stock_error(
            "Pass at least one stock entry, e.g. "
            '[{"kind": "weapon", "tier": "common", "name": "Ashwood Blade"}].',
            list(_STOCK_KINDS),
        )

    # Validate everything BEFORE writing anything: a half-stocked shelf with an
    # error message is a shop the DM cannot reason about.
    prepared: list[Item] = []
    for entry in entries:
        outcome = _prepare_entry(entry)
        if "error" in outcome:
            return outcome
        prepared.append(outcome["item"])

    with get_session() as session:
        npc = session.get(NPC, npc_id)
        if not npc:
            return {"error": "NPC not found"}

        # Imported here (not at module scope) so the item helpers stay in one file
        from src.tools.world_write.items import _validate_inventory

        inventory = _validate_inventory(npc.inventory_notable)
        stocked = []
        for item in prepared:
            for existing in inventory:
                if existing.get("id") == item.id and existing.get("stackable", True):
                    existing["quantity"] = existing.get("quantity", 1) + item.quantity
                    break
            else:
                inventory.append(item.to_dict())
            stocked.append({"name": item.name, "price": item.value, "quantity": item.quantity})

        npc.inventory_notable = inventory
        npc_name = npc.name
        session.commit()

    return {
        "success": True,
        "npc": npc_name,
        "stocked": stocked,
        "instruction": (
            "These prices are the engine's. Quote them as written and sell with "
            "transfer_item(is_purchase=True) — never invent a discount or a markup."
        ),
    }

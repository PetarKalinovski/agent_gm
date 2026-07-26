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
"""

import re
from typing import Any


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
    r"|you (?:pocket|stow|tuck away|now carry))\b",
    re.IGNORECASE,
)
_ITEM_TOOLS = {"add_to_inventory", "remove_from_inventory", "transfer_item"}

_ARRIVAL_CLAIM = re.compile(
    r"\byou (?:arrive (?:at|in)|reach the|step into|pass through the gates? of"
    r"|are waved (?:through|in|inside)|are let (?:in|inside|through))\b",
    re.IGNORECASE,
)

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
# instead of delivering them through `speak`/`prompt_npc_agent`. Those lines
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
_PRE_ATTRIBUTED_QUOTE = re.compile(
    rf"\b(?P<speaker>{_SPEAKER_PATTERN})"
    rf"(?:,[^,\n]{{0,30}},)?\s+(?:{_SPEECH_VERBS})\b[^\"“'‘.!?\n]{{0,25}}"
    rf"(?:[\"“](?P<dq>[^\"”“\n]{{8,400}})[\"”]|['‘](?P<sq>[^'’\n]{{8,400}})['’])"
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

    seen: set[str] = set()
    unique = []
    for s in speakers:
        if s.lower() not in seen:
            seen.add(s.lower())
            unique.append(s)
    return unique


def audit_turn(
    turn_text: str,
    tools: list[dict[str, Any]],
    state_before: dict[str, Any] | None,
    state_after: dict[str, Any] | None,
    *,
    code_actions: set[str] | None = None,
    campaign: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Compare what the narration claims against what actually happened.

    code_actions: tool names (or the category labels in CODE_ACTION_TOOLS)
        the ENGINE executed on the player's behalf between turns — canvas
        travel and pickups, potions, equips, combat results, campaign beat
        ops. They are unioned into the succeeded set: the DM may legitimately
        narrate their consequences without having called them.
    campaign: {"beat": {...} | None, "sealed_truths": [...]} — when present,
        the two campaign checks run.

    Returns a list of {check, detail} warnings (empty = clean turn).
    """
    warnings: list[dict[str, str]] = []
    if not turn_text:
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
    if _ITEM_CLAIM.search(turn_text) and not (succeeded & _ITEM_TOOLS) and not inventory_changed:
        warnings.append({
            "check": "item_without_tool",
            "detail": "Narration says an item entered the inventory but the inventory never changed.",
        })

    location_changed = before.get("location_id") != after.get("location_id")
    if _ARRIVAL_CLAIM.search(turn_text) and not location_changed and "move_player" not in succeeded:
        warnings.append({
            "check": "arrival_without_move",
            "detail": "Narration describes arriving somewhere but the player's location never changed.",
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
                f"Narration contains quoted dialogue attributed to {names} that was "
                "written inline instead of delivered through a speech tool — the player "
                "gets NO voice, portrait, or speech bubble for it. Deliver every spoken "
                "line via prompt_npc_agent (conversations) or speak (one-liners and "
                "ambient NPCs); never write dialogue inside narrate or plain text."
            ),
        })

    # First-person narration ("I lunge...") — the DM is the world, not the
    # character. Quoted spans are removed first: NPC dialogue legitimately
    # says "I". History momentum makes this sticky; the feedback loop nudges.
    unquoted = re.sub(r'["“][^"“”]*["”]', " ", turn_text)
    if len(re.findall(r"\b(?:I|I'm|I've|my)\b", unquoted)) >= 3:
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

    # Campaign checks — high precision by construction: they only run when a
    # campaign exists, and the truth check needs EVERY spoiler keyword present
    if campaign:
        lowered = turn_text.lower()
        beat = campaign.get("beat") or {}
        keywords = [str(k) for k in (beat.get("delivery_keywords") or []) if k]
        if keywords and not any(k.lower() in lowered for k in keywords):
            title = beat.get("title") or "untitled beat"
            warnings.append({
                "check": "campaign_beat_not_delivered",
                "detail": (
                    f"The engine gave you a MUST-DELIVER story beat this turn ('{title}') "
                    "and the world has already changed for it, but your narration never "
                    "mentioned it. Deliver it in your NEXT response before anything else."
                ),
            })
        for truth in campaign.get("sealed_truths") or []:
            spoilers = [str(k) for k in (truth.get("spoiler_keywords") or []) if k]
            if len(spoilers) >= 2 and all(k.lower() in lowered for k in spoilers):
                warnings.append({
                    "check": "campaign_truth_frontrun",
                    "detail": (
                        "Your narration is leaking a SEALED story truth the player has "
                        "not earned. Stop hinting at it — it is revealed by a beat, on "
                        "schedule, not by you."
                    ),
                })
                break

    return warnings


def format_audit_feedback(warnings: list[dict[str, str]]) -> str:
    """Context block injected into the DM's NEXT turn so it self-corrects."""
    if not warnings:
        return ""
    lines = "\n".join(f"- {w['detail']}" for w in warnings)
    return (
        "\n\n[REALITY AUDIT of your previous turn — the engine found narration "
        "claiming mechanics that never happened:\n"
        f"{lines}\n"
        "The database did NOT change for these claims. THIS turn, either make "
        "them real with the proper tool calls or stop asserting them. Never "
        "print mechanics or consequence lists in narration.]"
    )

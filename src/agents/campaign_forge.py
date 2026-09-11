"""Campaign Forge — the one LLM call that authors a character's story.

No tools, one shot, strict JSON out. Tools would add round-trips and let the
model half-write state; a single validated payload keeps code the sole writer
(`src/services/campaign.py::persist_campaign` does the validating, the
casting checks, and every downgrade rule).

The call runs on a background thread at character creation — the player is
naming their character or reading the opening scene while it happens, so the
10-30s is invisible and a failure degrades to the game exactly as it plays
today.
"""

import json
from pathlib import Path
import time
import re
from typing import Any

from loguru import logger

from src.models import (
    NPC,
    Faction,
    Location,
    NPCTier,
    Player,
    Quest,
    QuestStatus,
    WorldBible,
    WorldClock,
    get_session,
)

# Roster caps — the brief must stay around 4k tokens or the model starts
# skimming the very ids it is required to cast from.
MINOR_NPC_CAP = 25
LOCATION_CAP = 30
QUEST_CAP = 15
FACTION_CAP = 12

RETRY_MESSAGE = "Your previous reply was not valid JSON. Return ONLY the JSON object."


CAMPAIGN_FORGE_PROMPT = """You are the Campaign Forge. You author the SPINE of one character's story in an
existing, already-populated world: three acts, one villain, a set of truths that
are decided now and never change, and 9-15 concrete beats that will fire on a
schedule whether or not the player cooperates.

This is not a quest. It is the answer to "what is this character's story, and what
happens if they do nothing?"

## HARD RULES

1. CAST, DON'T INVENT. The villain, allies, and every entity you reference MUST be
   an existing id from the roster below. Do not invent NPCs, factions, locations, or
   quests. If the perfect villain isn't in the roster, pick the closest one and make
   them the villain — a familiar face turning out to be the knife is better writing
   than a stranger anyway.
2. PERSONAL STAKES. The premise must hook this specific character's name, background,
   and traits. Not "the kingdom is in danger" — "the man who trained you is the one
   selling the children, and he still thinks you're loyal."
3. TRUTHS ARE DECIDED NOW. Write 4-5 fixed truths: who the villain really is, the
   secret, the betrayal, where the thing is, and what it costs. These are facts of
   the world from this moment. Write them as flat statements of fact, not questions
   or teases.
4. THINGS HAPPEN WITHOUT THE PLAYER. At least 4 beats must fire on time or on a
   deadline, not on player action. The world moves.
5. NO NUMBERS. You never write damage, gold, power values, hours, or days. You pick
   LABELS from the menus given. The engine owns every number.
6. CONCRETE, NOT ATMOSPHERIC. "Tension rises in the district" is not a beat. "Kestrel
   hangs the kennel master from the market gate and leaves your name carved beside
   him" is a beat.

## STRUCTURE

- Act 1 (3-5 beats): the hook lands, the player is personally implicated, one
  deadline starts ticking.
- Act 2 (3-6 beats): the betrayal or the twist; the villain acts against the player
  directly; a second deadline.
- Act 3 (3-4 beats): confrontation and finale. The LAST beat of act 3 is the finale:
  beat_type "confrontation", cinematic "finale".
- 2-3 beats total must be beat_type "deadline_event" (each with clock_label,
  met_condition, and BOTH consequence_met and consequence_missed).

## MENUS (use these exact strings)

beat_type:     revelation | confrontation | deadline_event | setpiece | twist
truth kind:    villain_identity | the_secret | the_betrayal | the_location | the_cost
trigger_type:  time | location | quest_status | milestone | after_beat
  time:        trigger_data {"delay": "soon"|"days"|"week"|"fortnight"}
  location:    trigger_data {"location_id": "<id from roster>"}
  quest_status:trigger_data {"quest_id": "<id>", "status": "active"|"completed"|"failed"}
  milestone:   trigger_data {"milestone": "early"|"midway"|"late"}
  after_beat:  trigger_data {"beat_id": "<the `key` of an earlier beat>",
                             "delay": "immediate"|"soon"|"days"}
  deadline:    trigger_data {"deadline": "short"|"medium"|"long"}
cinematic:     none | act_open | revelation | betrayal | arrival | countdown_expired | finale

effect ops (consequence_met / consequence_missed — 1-4 per beat):
  {"op":"move_npc","npc_id":"<id>","to":"player"|"<location_id>"}
  {"op":"npc_status","npc_id":"<id>","status":"alive|dead|missing|imprisoned"}
  {"op":"npc_mood","npc_id":"<id>","mood":"<one or two words>"}
  {"op":"activate_quest","quest_id":"<id>"}
  {"op":"fail_quest","quest_id":"<id>"}
  {"op":"complete_quest","quest_id":"<id>"}
  {"op":"give_item","item_name":"<short name>","description":"<one line>"}
  {"op":"faction_power","faction_id":"<id>","shift":"weakened|strengthened|shattered|ascendant"}
  {"op":"faction_relationship","faction_a_id":"<id>","faction_b_id":"<id>","type":"allied|neutral|rival|war|vassal"}
  {"op":"world_event","name":"...","description":"...","scope":"macro|meso|player","consequences":["..."]}
  {"op":"tension_floor","level":"rising|high|climax"}

met_condition (deadline beats only, exactly one):
  {"check":"quest_status","quest_id":"<id>","status":"completed"}
  {"check":"truth_revealed","truth_id":"<truth key>"}
  {"check":"npc_status","npc_id":"<id>","status":"dead"}
  {"check":"player_visited","location_id":"<id>"}
  {"check":"item_held","item_name":"<short name>"}

## OUTPUT

Return ONLY a JSON object, no prose, no code fence:

{
  "title": "The Long Howl",
  "premise": "2-3 sentences. What the story is.",
  "stakes_personal": "1-2 sentences naming the PC and why this is THEIRS.",
  "villain_npc_id": "<roster id>",
  "villain_name": "<their name>",
  "villain_faction_id": "<roster id or null>",
  "successor_npc_id": "<roster id or null: who picks up the knife if the villain dies early>",
  "successor_name": "<name or empty>",
  "ally_npc_ids": ["<roster id>", "..."],
  "acts": [
    {"act": 1, "title": "...", "premise": "one sentence"},
    {"act": 2, "title": "...", "premise": "one sentence"},
    {"act": 3, "title": "...", "premise": "one sentence"}
  ],
  "fixed_truths": [
    {"key": "truth_villain", "kind": "villain_identity",
     "statement": "Flat statement of fact.",
     "spoiler_keywords": ["Kestrel", "breeds"]}
  ],
  "beats": [
    {
      "key": "b1",
      "act": 1,
      "sequence": 1,
      "beat_type": "revelation",
      "title": "Short scene title",
      "directive": "2-4 sentences addressed to the Dungeon Master, second person: what MUST happen in the scene. Name who acts, what they do, what the player sees.",
      "fixed_truths": ["Statements this beat makes true."],
      "reveals_truth_ids": ["truth_villain"],
      "delivery_keywords": ["Kestrel", "kennel", "brand"],
      "trigger_type": "time",
      "trigger_data": {"delay": "soon"},
      "cinematic": "revelation",
      "consequence_met": [{"op": "...", "...": "..."}],
      "clock_label": null,
      "directive_missed": null,
      "met_condition": null,
      "consequence_missed": []
    }
  ]
}

delivery_keywords: 3-6 distinctive words the DM will almost certainly write when it
delivers the beat (names, objects, places). The engine greps the turn text for them
to confirm the beat actually reached the player. Avoid generic words.
spoiler_keywords: 2-4 words that, appearing together, mean the truth has been let
out early. Use the specific names and nouns, not common verbs."""


def build_campaign_brief(player_id: str, predecessor_id: str | None = None) -> str:
    """Assemble the roster the generator must cast from. Pure DB reads.

    Everything is capped and truncated: the model has to be able to hold the
    whole roster in its head, because "cast, don't invent" is the one rule
    that makes the campaign land in a world that already exists.
    """
    parts: list[str] = []

    with get_session() as session:
        bible = session.query(WorldBible).first()
        if bible:
            parts.append("## WORLD")
            parts.append(f"Name: {bible.name} · Genre: {bible.genre}")
            if bible.tone:
                parts.append(f"Tone: {bible.tone}")
            if bible.themes:
                parts.append(f"Themes: {', '.join(str(t) for t in bible.themes[:5])}")
            if bible.current_situation:
                parts.append(f"Right now: {bible.current_situation}")
            if bible.major_conflicts:
                parts.append(f"Conflicts: {'; '.join(str(c) for c in bible.major_conflicts[:3])}")
            if bible.rules:
                parts.append(f"Rules: {'; '.join(str(r) for r in bible.rules[:5])}")

        factions = session.query(Faction).limit(FACTION_CAP).all()
        if factions:
            parts.append("\n## FACTIONS (id · name · ideology · power · goal)")
            for faction in factions:
                goal = (faction.goals_short or [""])[0] if faction.goals_short else ""
                parts.append(
                    f"- {faction.id} · {faction.name} · {_clip(faction.ideology, 120)} "
                    f"· power {faction.power_level} · {_clip(goal, 90)}"
                )

        npcs = session.query(NPC).filter(NPC.status == "alive").all()
        major = [n for n in npcs if n.tier == NPCTier.MAJOR]
        minor = [n for n in npcs if n.tier != NPCTier.MAJOR][:MINOR_NPC_CAP]
        if major or minor:
            parts.append("\n## NPCs (id · name · tier · profession · faction · location · goal · secret)")
            for npc in major + minor:
                parts.append("- " + _npc_line(session, npc))

        quests = (
            session.query(Quest)
            .filter(Quest.status == QuestStatus.NOT_STARTED)
            .limit(QUEST_CAP)
            .all()
        )
        if quests:
            parts.append("\n## UNSTARTED QUESTS (id · title · description · giver)")
            for quest in quests:
                giver = _get_name(session, NPC, quest.assigned_by_npc_id)
                parts.append(
                    f"- {quest.id} · {quest.title} · {_clip(quest.description, 140)} · {giver or 'unassigned'}"
                )

        locations = session.query(Location).limit(LOCATION_CAP).all()
        if locations:
            parts.append("\n## LOCATIONS (id · name · type · parent)")
            for location in locations:
                parent = _get_name(session, Location, location.parent_id)
                type_name = getattr(location.type, "value", location.type)
                parts.append(f"- {location.id} · {location.name} · {type_name} · {parent or '-'}")

        player = session.get(Player, player_id)
        if player:
            parts.append("\n## THE PLAYER CHARACTER")
            parts.append(f"Name: {player.name}")
            if player.description:
                parts.append(f"Appearance: {_clip(player.description, 200)}")
            if player.background:
                parts.append(f"Background: {_clip(player.background, 300)}")
            if player.traits:
                parts.append(f"Traits: {', '.join(str(t) for t in player.traits)}")
            here = _get_name(session, Location, player.current_location_id)
            if here:
                parts.append(f"Currently at: {here}")
            parts.append(f"Coin: {player.currency}")

        if predecessor_id:
            line = _predecessor_line(session, predecessor_id)
            if line:
                parts.append(line)

        clock = session.query(WorldClock).first()
        parts.append(f"\nCurrent world day: {clock.day if clock else 1}")

    return "\n".join(parts)


def generate_campaign_payload(brief: str) -> dict[str, Any]:
    """Run the one generator call and return the parsed payload.

    Raises on unparseable output after ONE retry — the caller marks the
    campaign `failed` and the world plays exactly as it does today.
    """
    from src.agents.base import create_agent

    agent = create_agent(
        agent_name="campaign_forge",
        system_prompt=CAMPAIGN_FORGE_PROMPT,
        tools=[],
        callback_handler=None,
    )

    prompt = f"{brief}\n\nAuthor this character's campaign. Return ONLY the JSON object."
    raw = str(agent(prompt))
    try:
        return _parse_payload(raw)
    except ValueError as e:
        logger.warning(f"campaign_forge: bad JSON ({e}) — retrying once")
        raw = str(agent(RETRY_MESSAGE))
        return _parse_payload(raw)


def _parse_payload(raw: str) -> dict[str, Any]:
    """Strip fences, parse, and insist on an object.

    Model JSON is almost right: trailing commas, a comment, a reply cut off
    by max_tokens mid-string. Each is repaired before giving up (seen live:
    two consecutive generations failed on a value at char 7025 and a new
    character played without a story). The raw reply is kept under
    data/debug/ when even the repair fails.
    """
    text = _strip_fences(raw)
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    if start != -1:
        candidates.append(text[start:])          # truncated: no closing brace yet
    last_error: Exception | None = None
    for candidate in candidates:
        for attempt in (candidate, _repair_json(candidate)):
            if not attempt:
                continue
            try:
                data = json.loads(attempt)
            except Exception as e:  # noqa: BLE001 - any decode failure
                last_error = e
                continue
            if isinstance(data, dict):
                return data
            last_error = ValueError("payload was not a JSON object")
    if start == -1:
        raise ValueError("no JSON object in reply")
    _dump_bad_reply(raw)
    raise ValueError(f"unparseable JSON: {last_error}")


_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_LINE_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)


def _repair_json(text: str) -> str:
    """Best-effort repairs: comments and trailing commas out, then close
    whatever a truncated reply left open (an unterminated string, arrays,
    objects) after cutting back to the last complete value."""
    if not text:
        return ""
    fixed = _LINE_COMMENT.sub("", text)
    fixed = _TRAILING_COMMA.sub(r"\1", fixed)
    try:
        json.loads(fixed)
        return fixed
    except Exception:
        pass
    # Truncation: walk the text tracking string/bracket state
    stack: list[str] = []
    in_string = False
    escape = False
    last_safe = 0        # index just after the last complete value/separator
    for i, ch in enumerate(fixed):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
                last_safe = i + 1
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
            last_safe = i + 1
        elif ch in "}]":
            if stack:
                stack.pop()
            last_safe = i + 1
        elif ch in ",:":
            last_safe = i + 1
        elif ch.isspace():
            continue
        else:
            last_safe = i + 1   # inside a bare value (number, true, null)
    if in_string:
        # drop the unterminated string and whatever key/comma preceded it
        cut = fixed.rfind('"', 0, len(fixed))
        cut = fixed.rfind(",", 0, cut)
        fixed = fixed[:cut if cut != -1 else last_safe]
    else:
        fixed = fixed[:last_safe]
    fixed = fixed.rstrip().rstrip(",")
    # re-balance after the cut
    stack = []
    in_string = False
    escape = False
    for ch in fixed:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    # a dangling key ("title":) must go too
    fixed = re.sub(r',?\s*"[^"\n]*"\s*:\s*$', "", fixed)
    fixed = fixed.rstrip().rstrip(",")
    fixed += "".join(reversed(stack))
    fixed = _TRAILING_COMMA.sub(r"\1", fixed)
    return fixed


def _dump_bad_reply(raw: str) -> None:
    try:
        debug_dir = Path("data/debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (debug_dir / f"campaign_reply_{stamp}.txt").write_text(raw or "", encoding="utf-8")
        logger.warning(f"campaign_forge: raw reply saved to data/debug/campaign_reply_{stamp}.txt")
    except OSError:
        pass


def _strip_fences(raw: str) -> str:
    """Drop ```json fences (and any prose before them) from a model reply."""
    text = (raw or "").strip()
    if "```" in text:
        parts = text.split("```")
        for part in parts[1:]:
            body = part.strip()
            if body.lower().startswith("json"):
                body = body[4:].strip()
            if body:
                return body
    return text


def _clip(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _get_name(session, model, ident) -> str | None:
    if not ident:
        return None
    row = session.get(model, ident)
    return getattr(row, "name", None) if row else None


def _npc_line(session, npc: NPC) -> str:
    faction = _get_name(session, Faction, npc.faction_id) or "-"
    location = _get_name(session, Location, npc.current_location_id) or "-"
    goal = (npc.goals or [""])[0] if npc.goals else ""
    secret = (npc.secrets or [""])[0] if npc.secrets else ""
    return (
        f"{npc.id} · {npc.name} · {npc.tier.value} · {npc.profession or '-'} · {faction} "
        f"· {location} · {_clip(goal, 80)} · {_clip(secret, 80)}"
    )


def _predecessor_line(session, predecessor_id: str) -> str | None:
    """Rebirth: the dead character is part of this character's stakes."""
    dead = session.get(Player, predecessor_id)
    if not dead:
        return None
    quest_title = ""
    for quest_id in (dead.active_quests or []):
        quest = session.get(Quest, quest_id)
        if quest:
            quest_title = quest.title
            break
    tail = f" pursuing {quest_title}" if quest_title else ""
    return f"Predecessor: {dead.name} died{tail}."

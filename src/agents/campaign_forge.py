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
    Event,
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
EVENT_CAP = 8

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
7. EVERY BEAT THE PLAYER MUST REACH CARRIES A lead. A beat with a location or
   quest_status trigger is a beat the player has to walk toward, and the ONLY way
   they can see the road is the lead on their screen. Every act-1 beat and at
   least half of all beats carry a lead and a lead_target (an npc or a location
   from the roster). Leads are written for the player: no directives, no truths,
   no names the player has not met unless the lead is how they meet them.
8. THE WORLD OFFERS, THE PLAYER ACTS. A directive never says the player character
   already did, took, stole, found, knew or decided anything ("the purse she lifted
   off a body" invents a theft the player never chose). Stage the opportunity in the
   world instead: "a dead carter lies by the north gate, his purse still on his
   belt" — what the player does with it is theirs. Nor does it script what the
   player WILL do: never "have <the character> read the notice", "if <the character>
   presses him", "before she pockets the token". Write what the WORLD and its
   people do — "the notice is nailed up for anyone to read", "the token lies in the
   crate, there to take", "the stable hand keeps glancing at the wall; when asked,
   he admits…". When a scene needs the player to ask, press or pay someone, say so
   plainly ("if pressed", "when asked") and write what that person shows unasked
   (a tell, an approach): the engine waits for the player to take it up.
9. QUESTS ALREADY STEER THE PLAYER. A quest marked [IN PLAY] is the player's work:
   already active (or DONE / FAILED), or offered in their opening scene. No beat
   offers it again, and no beat's whole content is going to a place or meeting a
   person its steps already send them to — they will get there through the quest,
   early (a DONE quest: they already have). Build on it instead: what they find
   there, what it changes, who reacts.
10. ONE TELLING PER EVENT. Whatever is listed under ALREADY SCHEDULED happens on
   its own clock, exactly as written. No beat restates it ("the overdue cart
   limps in half-empty" when the cart's arrival is already scheduled): build on
   it instead — who reacts, what it reveals, what someone does the morning after
   — and name it as something that has happened or will happen.
11. ACT TITLES AND PREMISES ARE SHOWN TO THE PLAYER. Each act opens on a title
   card with its title and premise. Neither may name, hint or rhyme with a fixed
   truth ("The Man Who Trained the Watch" when the villain's Watch past is a
   truth gives it away). Name the pressure the player feels, never the answer.
12. A SCENE AT A PLACE IS LED THERE. A beat that happens somewhere ("when the
   player reaches <place>", a find or a meeting at a place) carries that place as
   its lead_target ({"type": "location"}) or a location trigger — never a
   quest_status trigger alone: the engine holds it until the player stands there,
   and a beat with no place fires wherever they happen to be.
13. A SECRET COSTS. When someone in a beat hides something ("if pressed, he
   deflects"), the directive says what the player learns past the denial and how
   — what they see for themselves, a second press they must win, a risk, a trade.
   Never a denial and a full confession in one scene: the price is the scene.
14. TRUTHS FIT THE WORLD'S THINGS. A truth never contradicts what the world already
   holds: if a quest's proof or an object lies at a place, no truth puts it in
   someone's hands or anywhere else. Build the truths around what is already there.

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
  {"op":"give_item","item_name":"<short name>","description":"<one line>"}   (only for a thing NOTHING in the world holds yet — an item a scene object or a person already holds is found or handed over in play, and this op is dropped)
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
      "lead": "ONE short second-person line for the PLAYER'S screen, not the DM: what to DO next to reach this beat. Spoiler-safe — it may name a place or a person and a question, never the answer. 'Ask around the Drowned Lantern about the howling.' null for beats that fire without the player.",
      "lead_target": {"type": "npc", "id": "<roster id of the person the lead points at>"},
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
to confirm the beat actually reached the player. Avoid generic words. Take them from
what the scene shows FIRST, never from what only a second press or a later scene adds.
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

        player = session.get(Player, player_id)
        in_play = _quests_in_play(session, player)
        if in_play:
            # Round 3: the opening started "Teeth on the North Road" (to
            # Wolf's Hollow) while the spine was being authored, and the
            # spine still had Coldiron offer it later and an act-2 beat send
            # the player to the Hollow — both spent by turn 6
            parts.append("\n## [IN PLAY] — THE PLAYER'S WORK SO FAR (id · title · description · giver · steps)")
            for quest in in_play:
                giver = _get_name(session, NPC, quest.assigned_by_npc_id)
                done = {QuestStatus.COMPLETED: " (DONE)", QuestStatus.FAILED: " (FAILED)"}.get(quest.status, "")
                parts.append(
                    f"- {quest.id} · {quest.title}{done} · {_clip(quest.description, 140)} · "
                    f"{giver or 'unassigned'} · steps: {_quest_steps(session, quest) or '-'}"
                )
        in_play_ids = {quest.id for quest in in_play}

        quests = [
            quest for quest in (
                session.query(Quest)
                .filter(Quest.status == QuestStatus.NOT_STARTED)
                .limit(QUEST_CAP + len(in_play_ids))
                .all()
            )
            if quest.id not in in_play_ids
        ][:QUEST_CAP]
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

        # Round 4: the seed schedules "The Greywater Cart Arrives Short" (its
        # driver talks in the square on day 2) and the spine, never shown it,
        # authored a truth that the driver "has not been seen since" — the
        # world then told the player both.
        upcoming = (
            session.query(Event)
            .filter(Event.scheduled_day.isnot(None), Event.occurred_day.is_(None))
            .order_by(Event.scheduled_day, Event.scheduled_hour)
            .limit(EVENT_CAP)
            .all()
        )
        if upcoming:
            parts.append("\n## ALREADY SCHEDULED — these WILL happen as written; no truth or beat may "
                         "contradict or restate them (day · hour · name · what happens)")
            for event in upcoming:
                hour = f"{event.scheduled_hour:02d}:00" if event.scheduled_hour is not None else "?"
                parts.append(
                    f"- day {event.scheduled_day} · {hour} · {event.name} · {_clip(event.description, 160)}"
                )

        # Round 5: the square's notice reads "cart, driver unharmed" and the
        # spine, never shown it, authored a carter who came back dead — the DM
        # told the player both (fleet6-tamsin T1/T7/T16/T19)
        written = _written_in_world(locations)
        if written:
            parts.append("\n## ALREADY WRITTEN — what the world's notices, ledgers, letters and finds say, word "
                         "for word; the player reads these, so every truth and beat must agree with them "
                         "(place · thing: text)")
            parts.extend(written)

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
_DANGLING_KEY = re.compile(r',?\s*"[^"\n]*"\s*:\s*(?=[}\]])')
_LINE_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)


def _repair_json(text: str) -> str:
    """Best-effort repairs: comments and trailing commas out, then close
    whatever a truncated reply left open (an unterminated string, arrays,
    objects) after cutting back to the last complete value."""
    if not text:
        return ""
    fixed = _LINE_COMMENT.sub("", text)
    # a key the model abandoned before its value ("sequence":}}) — seen live
    fixed = _DANGLING_KEY.sub("", fixed)
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
    mismatch = False
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
            if stack and stack[-1] != ch:
                # the model closed the wrong bracket ("[{...}}"): cut here
                # and let the re-balance below close things properly
                fixed = fixed[:i]
                mismatch = True
                break
            if stack:
                stack.pop()
            last_safe = i + 1
        elif ch in ",:":
            last_safe = i + 1
        elif ch.isspace():
            continue
        else:
            last_safe = i + 1   # inside a bare value (number, true, null)
    if mismatch:
        pass
    elif in_string:
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


QUESTS_ON_OFFER_CAP = 1     # what the opening offers (`opening.OPENING_OFFERS`)


def _quests_in_play(session, player) -> list[Quest]:
    """The player's work so far: the quests active, done or failed — or,
    before any, the authored ones the opening scene will offer (an unstarted
    quest whose giver stands where the player starts; `opening._hand_over`
    tells the DM to offer exactly those). A failed generation is now retried
    minutes into play, by when the first job may be finished — and the
    givers around the player then are no opening offer."""
    quests = (
        session.query(Quest)
        .filter(Quest.status.in_([QuestStatus.ACTIVE, QuestStatus.COMPLETED, QuestStatus.FAILED]))
        .all()
    )
    quests.sort(key=lambda q: q.status != QuestStatus.ACTIVE)     # the live ones first
    if quests or player is None or not player.current_location_id:
        return quests
    givers = [
        npc.id for npc in session.query(NPC).filter(
            NPC.current_location_id == player.current_location_id, NPC.status == "alive"
        ).all()
    ]
    if not givers:
        return []
    from src.services.progression import QUEST_XP, quest_scale

    rows = (
        session.query(Quest)
        .filter(Quest.status == QuestStatus.NOT_STARTED, Quest.assigned_by_npc_id.in_(givers))
        .all()
    )
    rows = sorted((q for q in rows if (q.title or "").strip()), key=lambda q: -QUEST_XP[quest_scale(q)])
    return rows[:QUESTS_ON_OFFER_CAP]


def _quest_steps(session, quest: Quest) -> str:
    """The places and people a quest's steps send the player to, as text."""
    steps = []
    for step in (quest.objectives or []):
        text = step.get("text") if isinstance(step, dict) else step
        if text:
            steps.append(_clip(text, 70))
    return "; ".join(steps)


WRITTEN_CAP = 12        # object texts in the brief (three things a place, most places)


def _written_in_world(locations: list[Location]) -> list[str]:
    """One line per authored thing that states something in writing: what it
    reads, what the find in it is (its description) and a document's own
    words. Beds and the dead's pockets say nothing."""
    from src.services.scene_objects import normalize_objects

    lines: list[str] = []
    for location in locations:
        try:
            objects = normalize_objects(location.objects)
        except Exception:
            continue
        for obj in objects:
            if obj["kind"] in ("body", "rest"):
                continue
            payload = obj.get("payload") or {}
            item = payload.get("item") or {}
            if payload.get("text"):
                lines.append(f"- {location.name} · {obj['label']}: \"{_clip(payload['text'], 220)}\"")
            if item.get("name") and (item.get("description") or item.get("reads")):
                about = f" ({_clip(item['description'], 140)})" if item.get("description") else ""
                reads = f", which reads: \"{_clip(item['reads'], 320)}\"" if item.get("reads") else ""
                lines.append(f"- {location.name} · {obj['label']} holds {item['name']}{about}{reads}")
    return lines[:WRITTEN_CAP]


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

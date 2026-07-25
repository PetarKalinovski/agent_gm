# Campaign Spine — a story set in stone

**Status:** design spec, implementation-ready
**Owner system:** campaign (one of five parallel specs: progression, tiers, campaign, canvas, cinematics)
**Diagnosis this fixes:** with lazy generation, nothing feels *discovered* (the world
materializes as you look at it) and nothing *pulls* (no countdowns, no arc progress,
no sense that a story is already underway and will proceed without you).

---

## 0. The one-paragraph design

At player creation, one fast LLM call authors a **Campaign**: a premise, three acts,
a villain cast from the existing NPC roster, 4-5 **fixed truths** (villain identity,
the secret, the betrayal, where the thing is), and 9-15 **story beats** with
deterministic triggers and pre-authored consequence variants. Everything lands in
SQLite. From then on the LLM never writes campaign facts again — it only *reads* them.
Code fires beats on the world tick (time / location / quest status / milestone /
after-beat), executes their consequences with a fixed op vocabulary (LLM picked
labels; code owns every number), and hands the DM a `THIS TURN — MUST DELIVER`
directive plus an `ALREADY APPLIED` ledger. Unfired beats are never shown to the DM,
so it cannot front-run them. Deadline beats carry both a *met* and a *missed*
consequence set; both are concrete and both are applied in code. The HUD shows
`Act II · 3/5` and live countdown chips ("The Howl returns — 2d") straight off the
existing `state` SSE event. Act transitions and the finale emit cinematic directive
labels for the cinematics system to render.

No new LLM round-trip enters the turn path. Generation is a background thread at
character creation. Firing is a handful of indexed SQLite queries per turn.

---

## 1. Schema

Two new tables. `Base.metadata.create_all` creates them on any existing DB;
`_migrate_missing_columns` adds later columns. **No changes to existing tables** —
`data/emberfall.db` opens, plays, and behaves exactly as today with zero campaign
rows (every read path returns `None`/`{}` and every context block returns `""`).

### 1.1 `src/models/campaign.py` (new)

```python
"""Campaign spine: the authored story a character is living inside.

Code enforces, the LLM narrates: the campaign is generated once (at character
creation), stored as fact, and from then on the DM may only READ it. There is
no tool that writes a fixed truth — the DM cannot rewrite the story it was
handed, only deliver it.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


# --- Fixed menus. The generator picks LABELS from these; code owns behavior. ---

BEAT_TYPES = ("revelation", "confrontation", "deadline_event", "setpiece", "twist")

TRUTH_KINDS = ("villain_identity", "the_secret", "the_betrayal", "the_location", "the_cost")

TRIGGER_TYPES = ("time", "location", "quest_status", "milestone", "after_beat")

CINEMATIC_LABELS = (
    "none", "act_open", "revelation", "betrayal", "arrival",
    "countdown_expired", "finale",
)

BEAT_STATUSES = ("pending", "armed", "fired", "delivered", "skipped")

CAMPAIGN_STATUSES = ("pending", "active", "won", "lost", "abandoned", "failed")


class Campaign(Base):
    """The authored story for ONE player character in ONE world."""
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    player_id: Mapped[str] = mapped_column(String(36), ForeignKey("players.id"), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    premise: Mapped[str] = mapped_column(Text, default="")          # 2-3 sentences, the pitch
    stakes_personal: Mapped[str] = mapped_column(Text, default="")  # why THIS character

    # Casting (existing world entities preferred; name kept denormalized so a
    # deleted/renamed NPC never breaks the briefing)
    villain_npc_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    villain_name: Mapped[str] = mapped_column(String(255), default="")
    villain_faction_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    successor_npc_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    successor_name: Mapped[str] = mapped_column(String(255), default="")
    ally_npc_ids: Mapped[list] = mapped_column(JSON, default=list)

    # [{"act": 1, "title": "...", "premise": "..."}, x3]
    acts: Mapped[list] = mapped_column(JSON, default=list)
    current_act: Mapped[int] = mapped_column(Integer, default=1)

    # [{"id","kind","statement","spoiler_keywords":[...],"revealed":false,
    #   "revealed_day":null}]
    fixed_truths: Mapped[list] = mapped_column(JSON, default=list)

    status: Mapped[str] = mapped_column(String(50), default="pending")
    # "pending" (generation in flight) | "active" | "won" | "lost"
    # | "abandoned" (character died) | "failed" (generation failed)
    generation_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Pacing guard: no two beats fire within CAMPAIGN_BEAT_SPACING_HOURS
    last_beat_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_beat_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)

    generated_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<Campaign(title={self.title!r}, act={self.current_act}, status={self.status})>"


class StoryBeat(Base):
    """One authored moment. Fires in code; the DM narrates it."""
    __tablename__ = "story_beats"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id: Mapped[str] = mapped_column(String(36), ForeignKey("campaigns.id"), nullable=False, index=True)

    act: Mapped[int] = mapped_column(Integer, default=1)
    sequence: Mapped[int] = mapped_column(Integer, default=0)      # order within the act
    beat_type: Mapped[str] = mapped_column(String(50), default="revelation")
    title: Mapped[str] = mapped_column(String(255), nullable=False)

    # What the DM MUST deliver, in second person, 2-4 sentences.
    directive: Mapped[str] = mapped_column(Text, default="")
    # Deadline beats only: what the DM must deliver if the deadline was missed.
    directive_missed: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Immutable facts this beat establishes (plain statements, shown to the DM
    # when the beat fires; they may never be contradicted afterwards)
    fixed_truths: Mapped[list] = mapped_column(JSON, default=list)
    # Campaign-level truth ids this beat un-seals on delivery
    reveals_truth_ids: Mapped[list] = mapped_column(JSON, default=list)
    # 3-6 distinctive tokens; code checks the turn text for them to decide
    # whether the beat was actually delivered (no LLM in the loop)
    delivery_keywords: Mapped[list] = mapped_column(JSON, default=list)

    trigger_type: Mapped[str] = mapped_column(String(50), default="time")
    trigger_data: Mapped[dict] = mapped_column(JSON, default=dict)

    # Resolved absolute fire time for `time` triggers and deadline beats
    scheduled_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scheduled_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Anti-stall: every armed beat gets a hard backstop. If the world never
    # satisfies the trigger, the beat fires anyway. The story always moves.
    fallback_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fallback_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Deadline beats: code-checkable "did the player make it?" + both outcomes
    met_condition: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    consequence_met: Mapped[list] = mapped_column(JSON, default=list)     # op list
    consequence_missed: Mapped[list] = mapped_column(JSON, default=list)  # op list

    # Short HUD countdown label, deadline beats only ("The Howl returns")
    clock_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    cinematic: Mapped[str | None] = mapped_column(String(50), nullable=True)

    status: Mapped[str] = mapped_column(String(50), default="pending")
    variant: Mapped[str | None] = mapped_column(String(20), nullable=True)  # "met" | "missed"
    fired_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fired_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivered_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    armed_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    armed_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Plain-language record of what the engine already changed (shown to the DM
    # as ALREADY APPLIED, so it narrates instead of re-applying)
    applied_effects: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<StoryBeat(act={self.act}.{self.sequence} {self.title!r} {self.status})>"
```

`src/models/__init__.py`: add
`from src.models.campaign import Campaign, StoryBeat, BEAT_TYPES, TRUTH_KINDS, TRIGGER_TYPES, CINEMATIC_LABELS`
and extend `__all__`.

### 1.2 Fixed vocabularies (all code-owned constants, `src/services/campaign.py`)

**Trigger data shapes** (validated at persist; unknown → beat dropped):

| `trigger_type` | `trigger_data` | fires when |
|---|---|---|
| `time` | `{"delay": "soon"\|"days"\|"week"\|"fortnight"}` | world clock ≥ `scheduled_*` (resolved at arm time) |
| `location` | `{"location_id": "<uuid>"}` | `player.current_location_id == location_id` |
| `quest_status` | `{"quest_id": "<uuid>", "status": "active"\|"completed"\|"failed"}` | that quest reaches that status |
| `milestone` | `{"milestone": "early"\|"midway"\|"late"}` | see below |
| `after_beat` | `{"beat_id": "<beat title or index>", "delay": "immediate"\|"soon"\|"days"}` | prerequisite beat `delivered`, then delay elapses |

Deadline beats (`beat_type == "deadline_event"`) use
`trigger_data = {"deadline": "short"|"medium"|"long"}` and ignore `delay`.

**Code-owned numbers** (`campaign.py` module constants):

```python
DELAY_HOURS      = {"immediate": 0, "soon": 6, "days": 48, "week": 168, "fortnight": 336}
DEADLINE_DAYS    = {"short": 2, "medium": 5, "long": 9}
FALLBACK_DAYS    = 7            # backstop for non-time triggers
TIME_FALLBACK_DAYS = 3          # extra slack past a `time` beat's schedule
CAMPAIGN_BEAT_SPACING_HOURS = 2 # min in-game gap between non-deadline beats
BEAT_DELIVERY_ATTEMPTS = 3      # mirrors Event delivery re-surfacing
FACTION_POWER_SHIFT = {"weakened": -15, "strengthened": +15, "shattered": -40, "ascendant": +30}
MILESTONE_THRESHOLDS = {"early": (2, 1), "midway": (4, 3), "late": (6, 5)}
# (player_level_at_least, completed_quests_at_least) — level read defensively via
# getattr(player, "level", None); the progression system may add it later, and
# until it does the quest-count arm of the OR carries the trigger.
BEAT_PRIORITY = {"deadline_event": 0, "twist": 1, "confrontation": 1, "revelation": 2, "setpiece": 3}
```

**Effect op vocabulary** — the *only* things a beat may do to the world. The
generator emits labels + entity ids; code owns magnitudes.

```jsonc
{"op": "move_npc",     "npc_id": "...", "to": "player" | "<location_id>"}
{"op": "npc_status",   "npc_id": "...", "status": "alive|dead|missing|imprisoned"}
{"op": "npc_mood",     "npc_id": "...", "mood": "<free label, stored verbatim>"}
{"op": "activate_quest", "quest_id": "..."}
{"op": "fail_quest",     "quest_id": "..."}
{"op": "complete_quest", "quest_id": "..."}
{"op": "give_item",    "item_name": "...", "description": "..."}      // qty always 1
{"op": "faction_power","faction_id": "...", "shift": "weakened|strengthened|shattered|ascendant"}
{"op": "faction_relationship", "faction_a_id": "...", "faction_b_id": "...",
                       "type": "allied|neutral|rival|war|vassal"}
{"op": "world_event",  "name": "...", "description": "...",
                       "scope": "macro|meso|player", "consequences": ["..."]}
{"op": "tension_floor","level": "rising|high|climax"}
```

Deliberately **absent**: anything that damages the player (health is combat's and
the DM's job), anything that moves the player, anything numeric. An op whose entity
id doesn't resolve is dropped with a `logger.warning` — the beat still fires.

**`met_condition` vocabulary** (deadline beats):

```jsonc
{"check": "quest_status", "quest_id": "...", "status": "completed"}
{"check": "truth_revealed", "truth_id": "..."}
{"check": "npc_status", "npc_id": "...", "status": "dead"}
{"check": "player_visited", "location_id": "..."}     // Location.visited
{"check": "item_held", "item_name": "..."}            // case-insensitive substring
```

### 1.3 `config/settings.yaml` + `src/config.py`

```yaml
# Campaign spine — the authored story generated per player character
campaign:
  enabled: true
  # Generate a campaign for an existing character that has none, on their
  # first turn (runs in a background thread; the turn is never blocked)
  auto_generate_on_first_turn: true
  acts: 3
  beats_min: 9
  beats_max: 15
  deadline_clocks_min: 2
  deadline_clocks_max: 3
```

```python
class CampaignConfig(BaseModel):
    enabled: bool = True
    auto_generate_on_first_turn: bool = True
    acts: int = 3
    beats_min: int = 9
    beats_max: int = 15
    deadline_clocks_min: int = 2
    deadline_clocks_max: int = 3

class Settings(BaseModel):
    ...
    campaign: CampaignConfig = Field(default_factory=CampaignConfig)
```

### 1.4 `config/agents.yaml`

```yaml
  # Campaign spine — ONE call at character creation, in the background
  campaign_forge:
    model: "openrouter/deepseek/deepseek-v3.2"
    temperature: 0.85
    max_tokens: 8192
    description: "Authors the three-act campaign, fixed truths, and story beats for a new character"
```

---

## 2. Generation

### 2.1 When

| Moment | Call | Blocking? |
|---|---|---|
| `POST /api/world/players` (new/rebirth PC) | `ensure_campaign(player_id, predecessor_id=...)` | no — background thread |
| `PUT /api/world/players/{id}` from the name modal (the real "character created" moment in the web flow) | `ensure_campaign(player_id)` if none exists | no |
| First turn of a campaign-less character | `run_campaign_tick` → `ensure_campaign` when `settings.campaign.auto_generate_on_first_turn` | no |
| `POST /api/campaign/{player_id}/generate` (dev/debug, `{"force": true}` regenerates) | `ensure_campaign(..., force=True)` | no |

`ensure_campaign` writes a `Campaign` row with `status="pending"` **immediately**
(so concurrent callers no-op, same pattern as `maybe_advance_world` claiming the
day), then runs the LLM call on a thread. On success it fills the row and arms Act I.
On failure it sets `status="failed"` + `generation_error` and logs; the game plays
exactly as it does today. The player is naming their character / reading the opening
scene while this happens — the 10-30s generation is invisible.

### 2.2 The generator agent — `src/agents/campaign_forge.py` (new)

No tools. One shot. Strict JSON out. Tools would add round-trips and let the model
half-write state; a single validated JSON payload keeps code the sole writer.

```python
def build_campaign_brief(player_id: str) -> str: ...   # assembles the roster below
def generate_campaign_payload(brief: str) -> dict: ... # create_agent("campaign_forge", ...), parse JSON
```

`generate_campaign_payload` strips ```` ```json ```` fences, `json.loads`, and raises
on failure (caller marks the campaign `failed`). One retry with the message
`"Your previous reply was not valid JSON. Return ONLY the JSON object."`

**Brief contents** (assembled in code, capped so the prompt stays ~4k tokens):

- World Bible: name, genre, tone, themes, current_situation, major_conflicts, rules (5).
- Factions: `id · name · ideology (120 chars) · power_level · top short-term goal`.
- Major NPCs (all) + minor NPCs (cap 25): `id · name · tier · profession · faction · location_name · first goal · first secret`.
- Authored quests with `status == "not_started"`: `id · title · description (140 chars) · giver name`.
- Locations (cap 30): `id · name · type · parent name`.
- The PC: name, description, background, traits, current location, currency, and
  (rebirth only) `Predecessor: <name> died on Day N pursuing <last active quest>.`
- Current world day.

### 2.3 The generator prompt (draft — use verbatim)

```
You are the Campaign Forge. You author the SPINE of one character's story in an
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
out early. Use the specific names and nouns, not common verbs.
```

### 2.4 Validation + persistence (`src/services/campaign.py::persist_campaign`)

Executed in one transaction. Rules, in order:

1. `acts` clamped to exactly 3 (pad with `{"act": n, "title": f"Act {n}", "premise": ""}`).
2. `fixed_truths`: keep 3-6, keep only known `kind`s, `id = key or uuid4()`,
   `revealed=False`, `spoiler_keywords` lowercased, entries with <2 keywords keep
   the statement but get `spoiler_keywords: []` (front-run check skips them).
3. `villain_npc_id` must resolve to an NPC row; else try exact then `ilike` match on
   `villain_name`; else set `NULL` and keep the name (campaign still valid).
   Same for `successor_npc_id`, `ally_npc_ids` (unresolvable allies dropped).
4. Beats: drop any with an unknown `beat_type` / `trigger_type` / `cinematic`.
   Clamp `act` to 1..3. If beats survive < 6 or > 20 → generation is a failure
   (`status="failed"`), campaign inert.
5. Ops: drop unknown `op`s, drop ops whose `npc_id`/`quest_id`/`faction_id`/
   `location_id` doesn't resolve, drop unknown `shift`/`type`/`level`/`status` labels.
   Each drop → `logger.warning`.
6. `after_beat.beat_id` is remapped from the payload `key` to the persisted uuid;
   unresolvable → trigger downgraded to `time` with `delay="days"`.
7. Deadline beats missing `met_condition` or `consequence_missed` are downgraded to
   `beat_type="setpiece"` (a deadline you can't miss is not a deadline).
   `met_condition` with an unknown `check` or unresolvable id → same downgrade.
8. `campaign.status = "active"`, `generated_day = clock.day`, then `arm_act(1)`.

### 2.5 Arming (`arm_act(session, campaign, act)`)

For every beat of that act with `status == "pending"`:

- `status = "armed"`, `armed_day/hour = now`.
- `time` trigger → `scheduled_* = now + DELAY_HOURS[delay]`;
  `fallback_* = scheduled_* + TIME_FALLBACK_DAYS`.
- `deadline_event` → `scheduled_* = now + DEADLINE_DAYS[deadline]` days at the
  current hour; `fallback_* = scheduled_*` (a deadline never slips).
- `after_beat` → stays `pending` (armed when its prerequisite is delivered; then it
  gets `scheduled_* = deliver_time + DELAY_HOURS[delay]` and the normal fallback).
- everything else → `fallback_* = now + FALLBACK_DAYS`.
- Write `DMState.current_arc = f"[CAMPAIGN] {title} — Act {n}: {act_title}. {act_premise}"`
  and append the act title to `DMState.world_pressures` if a deadline clock exists.

---

## 3. Firing — "actual things happening"

### 3.1 Entry point

`src/services/campaign.py`

```python
def run_campaign_tick(player_id: str) -> dict[str, Any]:
    """Advance the campaign one turn. Pure code — no LLM, no network.

    Called from DMOrchestrator._build_context immediately after run_world_tick()
    so CLI and web both get it. Returns the briefing payload AND caches it for
    the web layer (SSE emission + post-turn delivery marking)."""
```

Returned dict (also cached in `_LAST_TICK[player_id]`, read by the server via
`get_last_tick(player_id)` / `pop_pending_events(player_id)`):

```python
{
  "campaign": {...},                 # summary: title, act, act_title, premise, status
  "truths": [{"kind","statement","revealed"}],
  "clocks": [{"beat_id","label","days_left","hours_left","urgency","day","hour"}],
  "delivered_titles": ["...", "..."],
  "beats_delivered": 3, "beats_total": 5,   # current act
  "fired_beat": {                    # None most turns
      "beat_id","title","beat_type","directive","fixed_truths",
      "variant","applied_effects":[...],"cinematic","audit_exempt":["quest"],
      "delivery_keywords":[...], "attempt": 1
  },
  "events": [ ...SSE payloads... ],  # campaign_beat / campaign_act / campaign_resolved
  "audit": {"beat": {...}|None, "sealed_truths": [...]},
}
```

### 3.2 Tick algorithm (exact order)

1. Load campaign for `player_id`. None → if `settings.campaign.enabled` and
   `auto_generate_on_first_turn`, spawn `ensure_campaign` on a thread; `return {}`.
   `status` in `("pending","failed","abandoned","won","lost")` → `return {}` (a
   resolved campaign still exposes `clocks: []` and its summary for the HUD, but
   fires nothing).
2. **Villain integrity check.** If `villain_npc_id` resolves to an NPC with
   `status == "dead"`:
   - successor alive → swap `villain_npc_id/villain_name` to the successor, clear
     successor fields, `create_event` "The mantle passes: <successor> takes up
     <villain>'s work", and set a one-turn directive prefix
     `"<villain> is dead. <successor> steps into the gap — the story does not stop."`
   - no successor → skip all `armed`/`pending` beats in acts < 3, jump
     `current_act = 3`, arm act 3, and force the finale beat to fire this tick with
     `variant="met"` (the player decapitated the story early; honor it).
3. **Prerequisite arming.** Any `after_beat` beat whose prerequisite is `delivered`
   and that is still `pending` → arm it now.
4. **Undelivered re-surfacing.** Any beat with `status == "fired"`:
   `delivery_attempts >= BEAT_DELIVERY_ATTEMPTS` → mark `delivered` (log
   `campaign beat gave up: <title>`, reveal its truths, run act-transition check);
   else re-surface it as `fired_beat` with `attempt = delivery_attempts + 1`,
   increment, and **stop** (never fire a new beat while one is undelivered).
5. **Eligibility.** For each `armed` beat, evaluate:
   - `time` / `deadline_event`: `(day, hour) >= (scheduled_day, scheduled_hour)`
   - `location`: `player.current_location_id == trigger_data["location_id"]`
   - `quest_status`: quest exists and `quest.status == trigger_data["status"]`
   - `milestone`: `getattr(player, "level", 0) or 0 >= lvl` **or**
     `len(player.completed_quests or []) >= quests` for
     `MILESTONE_THRESHOLDS[milestone]`
   - any trigger: `(day, hour) >= (fallback_day, fallback_hour)` → eligible regardless.
6. **Spacing.** Drop non-deadline candidates if fewer than
   `CAMPAIGN_BEAT_SPACING_HOURS` in-game hours have passed since
   `campaign.last_beat_*`. Deadlines ignore spacing.
7. **Pick one.** Sort by `(BEAT_PRIORITY[beat_type], act, sequence)`; take the first.
   Never more than one beat per turn.
8. **Fire it.**
   - `deadline_event` → `variant = "met" if evaluate_condition(met_condition) else "missed"`;
     ops = `consequence_met` / `consequence_missed`; directive =
     `directive` / `directive_missed or directive`; cinematic for a missed deadline
     is forced to `countdown_expired` if the authored value is `none`.
   - other types → `variant = "met"`, ops = `consequence_met`.
   - `applied, audit_exempt = execute_ops(session, ops, campaign, player)`
   - `status="fired"`, `fired_*`, `delivery_attempts=1`, `applied_effects=applied`,
     `campaign.last_beat_* = now`.
   - Queue SSE `campaign_beat`.
9. Build clocks, truths, act progress; return.

### 3.3 `execute_ops` — the only code that mutates the world for a beat

Runs inside the same `get_session()` transaction as the fire. Returns
`(applied: list[str], audit_exempt: set[str])`.

| op | effect | `applied` line | audit exempt |
|---|---|---|---|
| `move_npc` | `npc.current_location_id = target` (`"player"` → player's location) | `Emory Vale arrived at The Drowned Lantern` | — |
| `npc_status` | `npc.status = status`; if `dead`, also `create_event` death record | `Emory Vale is dead` | `consequence` |
| `npc_mood` | `npc.current_mood = mood` | `Emory Vale is now furious` | `consequence` |
| `activate_quest` | quest `not_started` → `active`, sync `Player.active_quests` | `Quest activated: "Follow the Feed"` | `quest` |
| `complete_quest` | → `completed`, sync lists, apply `Quest.rewards` via `_apply_rewards` | `Quest completed: "..." (rewards applied: ...)` | `quest`, `item` |
| `fail_quest` | → `failed`, sync lists | `Quest failed: "..."` | `quest` |
| `give_item` | append `{id,name,type:"misc",description,quantity:1}` to `player.inventory` | `The player received a Sealed Writ` | `item` |
| `faction_power` | `power_level = clamp(1,100, power + FACTION_POWER_SHIFT[shift])` | `The Warden's Men are weakened` | `consequence` |
| `faction_relationship` | upsert `FactionRelationship.relationship_type` | `The Warden's Men and the Kennels are at war` | `consequence` |
| `world_event` | `Event(..., occurred_*=now, narrated_to_player=True, player_visible=True)` — narrated is True because the beat directive *is* its narration | `World event recorded: "Piper names the Warden"` | `consequence` |
| `tension_floor` | `apply_tension_floor(level, "campaign beat")` | *(no line — invisible plumbing)* | — |

Quest ops reuse `_sync_player_quest_lists` / `_apply_rewards` from
`src/tools/world_write/quests.py` (import them; do not duplicate). Quest ops also
push the matching `quest_activated` / `quest_completed` / `quest_failed` SSE payload
into `events` so the existing frontend banner + triumph stinger fire exactly as they
do for tool-driven quests.

### 3.4 Delivery detection + act transitions

Post-turn, the web layer calls:

```python
def mark_beat_delivery(player_id: str, turn_text: str) -> dict[str, Any]:
    """Did the DM actually deliver the fired beat? Keyword check, no LLM.

    Returns {"delivered": bool, "beat_id": ..., "act_advanced": int|None,
             "events": [...]} — events queued for the NEXT turn's SSE flush."""
```

- Any beat with `status == "fired"` whose `delivery_keywords` intersect the
  lowercased turn text (substring match, ≥1 hit; if `delivery_keywords` is empty,
  treat as delivered) → `status="delivered"`, `delivered_day`, reveal
  `reveals_truth_ids` (set `revealed=True`, `revealed_day`), append the title to
  `DMState.completed_beats`.
- Then **act transition check**: all beats of `current_act` in
  `("delivered","skipped")` →
  - `current_act < 3`: `current_act += 1`, `arm_act(next)`, rewrite
    `DMState.current_arc`, queue `campaign_act` SSE (cinematic `act_open`).
  - `current_act == 3`: `campaign.status = "won" if last_delivered.variant == "met" else "lost"`,
    queue `campaign_resolved` (cinematic `finale`).
- Not delivered → leave `fired`; step 4 of the next tick re-surfaces it.

CLI path: `mark_beat_delivery` is called from `GameSession` after the DM response
with the same text (one line); events are simply discarded there.

---

## 4. Visible pull — state payload and HUD

### 4.1 `build_player_state` (server.py) gains one key

```python
"campaign": campaign_state(player_id)   # None when the world has no campaign
```

```jsonc
"campaign": {
  "title": "The Long Howl",
  "act": 2,
  "acts_total": 3,
  "act_title": "The Hunt Turns",
  "act_premise": "Kestrel's hounds run the city openly.",
  "beats_delivered": 3,
  "beats_total": 5,
  "status": "active",
  "clocks": [
    {"label": "The Howl returns", "days_left": 2, "hours_left": 3,
     "urgency": "soon", "day": 9, "hour": 4}
  ],
  "delivered_titles": ["The kennel fire", "The Warden's warning"],
  "revealed_truths": ["Kestrel Vane breeds the howlers she is paid to hunt."]
}
```

`campaign_state()` is a **≤3-query, spoiler-free** read: it never emits sealed truth
statements, unfired beat titles, or directives. `urgency` menu:
`distant` (>72h) · `soon` (24-72h) · `imminent` (<24h) · `overdue` (past due, beat
not yet fired). `beats_total/delivered` count the *current act only* — "Act II · 3/5"
is legible; "beat 7 of 13" is not.

### 4.2 SSE events

Emitted by the server in `_run_turn`, immediately after `dm._build_context(...)`
returns and before `stream_async` starts (so the cinematic can play over the DM's
first tokens):

```json
{"type": "campaign_beat", "beat_id": "…", "act": 2, "title": "Piper's Confession",
 "beat_type": "revelation", "variant": "met", "cinematic": "revelation",
 "clock_label": null, "campaign_title": "The Long Howl"}
```
```json
{"type": "campaign_act", "act": 2, "acts_total": 3, "act_title": "The Hunt Turns",
 "act_premise": "Kestrel's hounds run the city openly.",
 "cinematic": "act_open", "campaign_title": "The Long Howl"}
```
```json
{"type": "campaign_resolved", "status": "won", "cinematic": "finale",
 "campaign_title": "The Long Howl",
 "epitaph": "Act 3 — The Kennels Burn"}
```

`campaign_act` / `campaign_resolved` are produced by `mark_beat_delivery` at the END
of turn N, so they are flushed at the start of turn N+1's stream (and immediately
after the post-turn `state` event of turn N via the same
`pop_pending_events` drain — spec: drain once after `_build_context`, once after
`mark_beat_delivery`).

**Cinematics contract:** this system only ever emits a `cinematic` *label* from
`CINEMATIC_LABELS`. Rendering, timing, and letterboxing belong to the cinematics
spec. If nothing handles the label, the HUD treatment below is the whole feature and
nothing breaks.

### 4.3 HUD (index.html only — no game.js change in v1)

**Chips** (`.game-overlay`, after `#hud-quests-chip`):

```html
<span class="hud-chip clickable" id="hud-campaign-chip" title="Story" style="display:none">
    &#9650; <span id="hud-campaign">Act I &middot; 0/4</span>
</span>
<span class="hud-chip" id="hud-clock-chip" style="display:none">
    &#8987; <span id="hud-clock"></span>
</span>
```

CSS additions next to the existing `#hud-health.*` rules:

```css
#hud-clock-chip.distant  { color: var(--text-secondary); }
#hud-clock-chip.soon     { color: #fbbf24; }
#hud-clock-chip.imminent { color: #f87171; animation: clockPulse 1.6s ease-in-out infinite; }
#hud-clock-chip.overdue  { color: #ef4444; font-weight: 700; }
@keyframes clockPulse { 0%,100% { opacity: 1; } 50% { opacity: 0.45; } }
```

**`applyPlayerState(state)`** — append before `renderHudPanel()`:

```js
const c = state.campaign;
const campChip = document.getElementById('hud-campaign-chip');
const clockChip = document.getElementById('hud-clock-chip');
if (c) {
    campChip.style.display = '';
    document.getElementById('hud-campaign').textContent =
        `${ROMAN[c.act] || c.act} · ${c.beats_delivered}/${c.beats_total}`;
    campChip.title = `${c.title} — Act ${c.act}: ${c.act_title}`;
    const clock = (c.clocks || [])[0];
    if (clock) {
        clockChip.style.display = '';
        clockChip.className = `hud-chip ${clock.urgency}`;
        document.getElementById('hud-clock').textContent =
            `${clock.label} — ${formatCountdown(clock)}`;
    } else { clockChip.style.display = 'none'; }
} else { campChip.style.display = 'none'; clockChip.style.display = 'none'; }
```

with `const ROMAN = {1:'Act I', 2:'Act II', 3:'Act III'};` and

```js
function formatCountdown(c) {
    if (c.urgency === 'overdue') return 'now';
    if (c.days_left >= 1) return `${c.days_left}d`;
    return `${Math.max(1, c.hours_left)}h`;
}
```

**`renderHudPanel()`** — new `'campaign'` mode showing: campaign title, `Act N —
title`, act premise, the ticking clocks (all of them, with urgency colors), a
"Story so far" list built from `campaign.delivered_titles`, and
`campaign.revealed_truths` under a `What you know` heading. Wire
`document.getElementById('hud-campaign-chip').addEventListener('click', () => toggleHudPanel('campaign'));`

**SSE handlers** in `sendMessage`'s payload switch:

```js
} else if (payload.type === 'campaign_beat') {
    showToast(`&#9650; ${escapeHtml(payload.title)}`, 'campaign');
    document.dispatchEvent(new CustomEvent('forge-cinematic',
        { detail: { kind: payload.cinematic, source: 'campaign_beat', payload } }));
} else if (payload.type === 'campaign_act') {
    showToast(`&#9650; Act ${payload.act} — ${escapeHtml(payload.act_title)}`, 'campaign-act', 6000);
    document.dispatchEvent(new CustomEvent('forge-cinematic',
        { detail: { kind: payload.cinematic, source: 'campaign_act', payload } }));
} else if (payload.type === 'campaign_resolved') {
    showToast(`&#9650; ${escapeHtml(payload.campaign_title)} — ${payload.status === 'won' ? 'Resolved' : 'Lost'}`,
              'campaign-act', 8000);
    document.dispatchEvent(new CustomEvent('forge-cinematic',
        { detail: { kind: 'finale', source: 'campaign_resolved', payload } }));
}
```

`forge-cinematic` is the hand-off to the cinematics system. In v1 nothing listens and
the toasts carry the moment. Two new toast styles go beside the existing
`.toast.quest-done` / `.toast.quest-failed` rules (index.html ~line 514):

```css
.toast.campaign     { border-color: #6366f1; color: #c7d2fe; }
.toast.campaign-act { border-color: #fbbf24; color: #fde68a; font-size: 1.05rem; letter-spacing: 0.04em; }
```

**Cache-buster:** v1 touches no file under `static/js/`, so the `?v=` on
`game.js`/`audio.js` (line 1700-1701 of index.html) stays. The moment the cinematics
hook lands in `game.js`, bump it (`?v=YYYYMMDDx`).

### 4.4 REST

```
GET /api/campaign/{player_id}            → 200
{
  "campaign": { ...campaign_state() payload... , "premise": "...",
                "villain_name": "Kestrel Vane", "stakes_personal": "..."},
  "delivered_beats": [{"act":1,"title":"The kennel fire","day":3}],
  "revealed_truths": [{"kind":"villain_identity","statement":"..."}],
  "clocks": [ ... ],
  "sealed_truth_count": 2
}
```
No campaign → `{"campaign": null}` (200, never 404 — the frontend polls it freely).
**Sealed truth statements are never serialized here.** A curious player with devtools
must not be able to read the ending. `?spoilers=1` returns them and is documented as
a debug/author flag only.

```
POST /api/campaign/{player_id}/generate  body {"force": false}
→ {"status": "pending"|"ready"|"exists"|"disabled", "campaign_id": "..."}
```

---

## 5. DM integration

### 5.1 `_build_context` (dm_orchestrator.py)

```python
tick_result = run_world_tick()
world_briefing = format_world_tick_context(tick_result)

campaign_tick = run_campaign_tick(self.context.player_id)   # NEW
campaign_briefing = format_campaign_context(campaign_tick)  # NEW ("" when absent)
```

and in the f-string, `{campaign_briefing}` goes **after** `{world_briefing}` — the
campaign is the loudest thing in the context, so it sits closest to the player input.

### 5.2 Context block (exact format produced by `format_campaign_context`)

```
### CAMPAIGN: The Long Howl
Act 2 of 3 — "The Hunt Turns": Kestrel's hounds run the western district openly and
the Warden calls it weather.
Story so far (already delivered): The kennel fire · The Warden's warning
This act: 2 of 5 beats delivered. The rest are sealed from you — do NOT foreshadow,
hint at, or gesture toward anything you have not been told.

FIXED TRUTHS — facts of this world. You may not contradict, soften, or replace them.
  [KNOWN]  villain_identity: Kestrel Vane breeds the howlers she is paid to hunt.
  [SEALED] the_betrayal: Warden Halloway sold the western district to Kestrel for
           silence. The player does NOT know this. Write the scene as if you did not
           know it either. No knowing looks, no NPC almost-saying it.

CLOCKS — visible countdowns. NPCs know about these and may speak of them.
  - The Howl returns: 2 days, 3 hours (Day 9, 04:00)

THIS TURN — MUST DELIVER  [revelation · "Piper's Confession"]
  Piper breaks. She admits she has been feeding the kennels since the fire, and she
  names the Warden's seal stamped on the feed crates. She is terrified, not
  apologetic. She asks you — by name — not to make her say it twice.
  This beat establishes as fact:
    - The feed crates carry the Warden's seal.
  ALREADY APPLIED BY THE ENGINE (narrate as fact; do NOT call tools to redo these):
    - Piper Vance arrived at The Drowned Lantern
    - Quest activated: "Follow the Feed"
    - World event recorded: "Piper names the Warden"
  Deliver this IN THIS RESPONSE. If the player is doing something else, let it
  interrupt them — that is what a story is.
```

Re-surfaced beats (attempt 2-3) replace the last line with:

```
  You were given this beat 2 turns ago and have not delivered it. The world has
  ALREADY changed. Deliver it NOW, in this response, before anything else.
```

Note what is **absent**: unfired beat titles, unfired directives, act-3 content,
sealed truths belonging to future acts (sealed truths *are* shown — the DM must know
what not to say — but no beat text is). This is the anti-front-running posture:
starve the model of what it must not leak.

### 5.3 New DM prompt section (insert after `### NARRATIVE DIRECTOR`)

```
### THE CAMPAIGN (your spine — read the CAMPAIGN block in your context every turn)

This character is living inside a story that was decided before your first sentence:
a premise, three acts, a villain, and a set of FIXED TRUTHS. You did not write it and
you cannot rewrite it. Your job is to deliver it well.

- **Fixed truths are facts.** Who the villain is, what the secret is, who betrayed
  whom, where the thing is. You may not contradict, soften, retcon, or "improve"
  them. If the player theorises something that contradicts a truth, NPCs can be
  wrong, evasive, or lying — the truth does not bend.
- **SEALED truths are secrets you are keeping from the player.** Do not reveal them,
  hint at them, or let an NPC almost-say them. Write every scene as if you did not
  know. A truth becomes sayable only when a beat directive tells you to say it.
- **Never foreshadow a beat you have not been given.** You are shown delivered beats
  and the one firing right now. Everything else is sealed on purpose. Do not invent
  "the next step" of the campaign; the engine decides when the story moves.
- **MUST-DELIVER beats are not suggestions.** When your context contains a
  `THIS TURN — MUST DELIVER` block, that beat happens in THIS response. Not next
  turn, not "soon". The changes listed under `ALREADY APPLIED BY THE ENGINE` are
  already true in the database — narrate them as fact and do NOT call tools to redo
  them.
- **The campaign owns your arc.** `update_dm_state` still owns tension, threats, and
  world pressures — keep using it exactly as before. But `current_arc` is written by
  the engine from the campaign act: do not overwrite it, and do not put campaign
  beats into `planned_beats` (that list is for scene-level texture — a rumour you
  want to plant, a mood you want to build).
- **Clocks are real and public.** A countdown in your context ("The Howl returns —
  2 days") is visible to the player and known to NPCs. Let people talk about it. It
  will fire whether or not the player is ready, and the world changes either way.
- Call `get_campaign` if you need the premise, the current act, or the truths you
  are allowed to know.
```

Also amend the existing `### NARRATIVE DIRECTOR` bullet
*"When the current arc resolves, start a new one…"* with:
*"— unless a CAMPAIGN block is present, in which case the campaign act IS your arc
and the engine advances it."*

`tests/test_dm_prompt_tools.py` passes because `get_campaign` is registered (below).

### 5.4 The one new tool — `src/tools/world_read/campaign.py`

```python
@tool
def get_campaign(player_id: str) -> dict[str, Any]:
    """Read the authored campaign for this character: premise, current act,
    villain, live deadline clocks, delivered beats, and the fixed truths you
    are allowed to know.

    SEALED truths are included with their statements so you know what NOT to
    say — never reveal a truth marked sealed. There is no tool to change a
    truth: the story is set in stone. Read it, deliver it, do not rewrite it.
    """
```

Returns `{"campaign": {...}|None, "act": n, "acts_total": 3, "truths":
[{"kind","statement","revealed"}], "clocks": [...], "delivered_beats": [...],
"villain_name": "..."}`. Registered in `DM_TOOLS` (read block) and exported from
`src/tools/world_read/__init__.py`. **No write tool exists for campaigns.** That is
the enforcement posture: the DM physically cannot author or alter a truth, a beat, or
an act.

### 5.5 Subordinating `DMState`

| Field | Owner after this change |
|---|---|
| `current_arc` | **code** (rewritten on campaign creation + every act transition). The DM is told not to touch it; if it does, the next act transition overwrites it. Non-campaign worlds: unchanged, DM-owned. |
| `planned_beats` | DM (scene texture only) |
| `completed_beats` | both — code appends delivered campaign beat titles |
| `tension` | unchanged (`src/services/tension.py` + DM), plus the `tension_floor` op |
| `active_threats` / `world_pressures` | DM; code adds the villain name to `active_threats` once at campaign creation and each live clock label to `world_pressures` at arm time (de-duplicated) |

---

## 6. The audit

### 6.1 No false positives on code-path actions

`audit_turn` gains a keyword-only parameter:

```python
def audit_turn(turn_text, tools, state_before, state_after, *,
               code_actions: set[str] | None = None,
               campaign: dict[str, Any] | None = None) -> list[dict[str, str]]:
```

`code_actions` suppresses checks whose mutation was performed by the engine:

| code action | suppresses |
|---|---|
| `quest` | `quest_banner_without_tool` |
| `item` | `item_without_tool` |
| `consequence` | `consequences_without_tools` |
| `health` | `damage_without_tool` (reserved for combat; campaign never emits it) |
| `arrival` | `arrival_without_move` (reserved; campaign never emits it) |

The server passes `code_actions=set(campaign_tick.get("fired_beat", {}).get("audit_exempt", []))`.
Existing callers and tests are unaffected (default `None`). **Follow-up worth doing
in the same PR:** `/api/combat/result` already mutates health in code, so the
auto-sent `[COMBAT OUTCOME]` turn should pass `code_actions={"health"}` — same
mechanism, one line, kills a known false positive class.

### 6.2 Two new campaign checks (`campaign` param, evaluated only when present)

```python
{"beat": {"beat_id","title","delivery_keywords":[...]} | None,
 "sealed_truths": [{"id","spoiler_keywords":[...]}]}
```

1. **`campaign_beat_not_delivered`** — a MUST-DELIVER beat was in context and none of
   its `delivery_keywords` appear (case-insensitive substring) in the turn text.
   Detail: *"The engine gave you a MUST-DELIVER story beat this turn ('<title>') and
   the world has already changed for it, but your narration never mentioned it.
   Deliver it in your NEXT response before anything else."*
2. **`campaign_truth_frontrun`** — for a truth still `sealed` with ≥2
   `spoiler_keywords`, **all** of its keywords appear in the turn text.
   Detail: *"Your narration is leaking a SEALED story truth the player has not
   earned. Stop hinting at it — it is revealed by a beat, on schedule, not by you."*

Both are high-precision by construction (all-keywords conjunction; only when a
campaign exists) and flow through the existing `_pending_audit_feedback` →
`[REALITY AUDIT]` channel, so the DM self-corrects on the next turn.

---

## 7. Missed deadlines change the world

A deadline beat is the only beat with two authored futures, both concrete, both
applied in code:

```jsonc
{
  "beat_type": "deadline_event",
  "title": "The Howl Returns",
  "clock_label": "The Howl returns",
  "trigger_data": {"deadline": "short"},
  "met_condition": {"check": "quest_status", "quest_id": "q-feed", "status": "completed"},
  "directive": "The howlers come and find the feed gates barred...",
  "consequence_met": [
    {"op": "faction_power", "faction_id": "f-kennels", "shift": "weakened"},
    {"op": "npc_mood", "npc_id": "n-kestrel", "mood": "cornered"},
    {"op": "world_event", "name": "The gates held", "scope": "macro",
     "description": "...", "consequences": ["The district trusts the player"]}
  ],
  "directive_missed": "The howlers come through the western gate at moonrise...",
  "consequence_missed": [
    {"op": "npc_status", "npc_id": "n-piper", "status": "dead"},
    {"op": "faction_power", "faction_id": "f-warden", "shift": "weakened"},
    {"op": "faction_relationship", "faction_a_id": "f-warden", "faction_b_id": "f-kennels", "type": "war"},
    {"op": "fail_quest", "quest_id": "q-feed"},
    {"op": "world_event", "name": "The western district falls", "scope": "macro", ...},
    {"op": "tension_floor", "level": "high"}
  ]
}
```

The clock fires on time regardless of where the player is or what they're doing. The
missed branch kills a named NPC, breaks a faction, fails a quest, and records a world
event — permanent, code-applied, visible in the HUD before the DM writes a word. That
is the difference between a deadline and a warning.

---

## 8. Build checklist

1. `src/models/campaign.py` — `Campaign`, `StoryBeat`, the fixed-menu tuples.
   Export from `src/models/__init__.py`. Verify an existing DB
   (`uv run main.py play --db data/emberfall.db`) still opens and plays: the two
   tables are created empty and nothing else changes.
2. `src/config.py` + `config/settings.yaml` — `CampaignConfig`.
   `config/agents.yaml` — `campaign_forge`.
3. `src/services/campaign.py` scaffolding — constants (§1.2), `get_campaign_row`,
   `campaign_state`, `campaign_public_view` (spoiler-redacting), `_now(session)`.
4. `execute_ops` + `evaluate_condition` (§3.3, §1.2). Reuse
   `_sync_player_quest_lists` / `_apply_rewards` from `world_write/quests.py` and
   `apply_tension_floor` from `services/tension.py`.
5. `arm_act`, `run_campaign_tick`, `mark_beat_delivery`, `_LAST_TICK` /
   `get_last_tick` / `pop_pending_events`, `format_campaign_context`.
6. `src/agents/campaign_forge.py` — `build_campaign_brief`,
   `generate_campaign_payload` (prompt from §2.3, one JSON retry).
7. `src/services/campaign.py::persist_campaign` + `ensure_campaign`
   (claim-row-then-thread, §2.1/§2.4).
8. `src/tools/world_read/campaign.py::get_campaign`; export it; register in
   `DM_TOOLS`.
9. `src/agents/dm_orchestrator.py` — prompt section (§5.3), the
   `### NARRATIVE DIRECTOR` amendment, `_build_context` wiring (§5.1).
10. `src/web/turn_audit.py` — `code_actions` + `campaign` params, two new checks
    (§6). Pass `code_actions={"health"}` for `[COMBAT OUTCOME]` turns while you're
    in there.
11. `src/web/server.py` — `build_player_state["campaign"]`; drain
    `pop_pending_events` after `_build_context` and after `mark_beat_delivery`;
    call `mark_beat_delivery(player_id, "".join(turn_text_parts))` post-turn
    (before the audit, so a delivered beat isn't flagged); pass audit params;
    `ensure_campaign` in `create_player` and in `update_player` (name modal);
    `GET /api/campaign/{player_id}`, `POST /api/campaign/{player_id}/generate`.
12. `src/game/session.py` — one call to `mark_beat_delivery` after the DM response
    so the CLI path also advances acts.
13. `src/web/static/index.html` — chips, CSS, `applyPlayerState`, `renderHudPanel`
    campaign mode, chip listener, three SSE handlers, two toast styles (§4.3).
14. Tests (§9).
15. `CLAUDE.md` — add a **Campaign spine** bullet under *Mechanical consequences*
    and a line in the *Audio / cinematic event flow* section about the three new SSE
    events.

**Scope split**

*v1 (build now):* everything above. That is the smallest thing that delivers the
player-facing point — a story that exists before you look at it, visibly moves
without you, and shows you how far along you are and how long you have.

*Later:*
- Branching beats (player choice selects among 2+ authored variants).
- Campaign editor in the World Viewer tab (author/repair beats by hand).
- Beat-scoped music directives and per-beat cinematic camera notes.
- Act 4+ / sequel campaigns chained off a `won`/`lost` campaign.
- Cross-character campaign echoes (the new PC inherits the dead PC's unfinished
  clock instead of a fresh campaign).
- Re-casting when an ally dies (v1 handles only the villain).
- Semantic (embedding) delivery detection instead of keyword matching.

---

## 9. Tests (`tests/test_campaign.py`, `tests/test_campaign_audit.py`)

All use the existing `db` / `player` fixtures. A module-level `_payload()` helper
returns a canned generator payload; `persist_campaign` is called directly so no test
touches the network.

| Test | Asserts |
|---|---|
| `test_world_without_campaign_ticks_clean` | `run_campaign_tick(player)` → `{}`, `format_campaign_context({})` → `""`, `campaign_state(player)` → `None`. Existing worlds unaffected. |
| `test_persist_campaign_creates_beats_and_arms_act_one` | 3 acts, N beats; act-1 beats `armed` with `fallback_*` set, act-2/3 `pending`; `DMState.current_arc` starts with `"[CAMPAIGN]"`. |
| `test_persist_drops_unknown_entity_ids` | ops referencing a bogus `npc_id` are dropped, the beat survives, villain with an unresolvable id falls back to name-only (`villain_npc_id is None`, `villain_name` kept). |
| `test_persist_downgrades_deadline_without_variants` | deadline beat missing `consequence_missed` becomes `beat_type == "setpiece"`. |
| `test_time_beat_fires_once_on_schedule` | advance clock past `scheduled_*` → one `fired_beat`; second tick returns the same beat as a re-surface (attempt 2), not a second fire. |
| `test_location_beat_fires_on_entry` | beat fires only after `player.current_location_id` is set to the trigger location. |
| `test_quest_status_beat_fires_on_completion` | fires when the quest flips to `completed`. |
| `test_milestone_beat_fires_on_completed_quest_count` | with no `Player.level` attribute, `milestone: "early"` fires at 1 completed quest. |
| `test_fallback_deadline_fires_stalled_beat` | a `location` beat whose location is never entered fires once the clock passes `fallback_*`. |
| `test_only_one_beat_fires_per_tick_and_priority_order` | three eligible beats → exactly one fires, and it is the `deadline_event`. |
| `test_beat_spacing_blocks_pileup` | two eligible non-deadline beats an hour apart → second does not fire; a deadline ignores spacing. |
| `test_deadline_met_applies_met_ops` | `met_condition` satisfied → `variant == "met"`, faction weakened by exactly 15, NPC alive. |
| `test_deadline_missed_applies_missed_ops` | condition unsatisfied → `variant == "missed"`, NPC `status == "dead"`, quest `failed`, `FactionRelationship.relationship_type == "war"`, an `Event` row exists, `cinematic == "countdown_expired"`. |
| `test_faction_power_shift_is_code_owned_and_clamped` | `"weakened"` on a power-10 faction → 1, never below. |
| `test_give_item_adds_single_quantity` | inventory gains exactly one entry, `quantity == 1`. |
| `test_beat_delivery_by_keyword_reveals_truths` | turn text containing a keyword → beat `delivered`, its truth `revealed is True`, title appended to `DMState.completed_beats`. |
| `test_beat_redelivery_then_giveup` | no keyword for 3 turns → re-surfaced with rising `attempt`, then marked `delivered` and no longer surfaced. |
| `test_act_transition_arms_next_act` | all act-1 beats delivered → `current_act == 2`, act-2 beats `armed`, `campaign_act` event queued with `cinematic == "act_open"`. |
| `test_finale_resolves_campaign` | last act-3 beat delivered with `variant == "met"` → `status == "won"`, `campaign_resolved` queued. |
| `test_villain_death_swaps_successor` | villain NPC set `dead` → tick swaps in the successor, campaign still `active`, an `Event` records the handover. |
| `test_villain_death_without_successor_forces_finale` | no successor → `current_act == 3`, pre-act-3 beats `skipped`, finale fires. |
| `test_context_block_hides_unfired_beats` | `format_campaign_context` contains delivered titles and the fired directive, and does **not** contain any pending beat's title or directive. |
| `test_context_block_marks_sealed_truths` | sealed truth appears under `[SEALED]` with the do-not-hint instruction; revealed appears under `[KNOWN]`. |
| `test_campaign_state_clock_urgency` | deadline 2 days out → `days_left == 2`, `urgency == "soon"`; 6h out → `imminent`; past due → `overdue`. |
| `test_public_view_redacts_sealed_truths` | `campaign_public_view(player)` contains no sealed statement text; `spoilers=True` does. |
| `test_audit_flags_undelivered_beat` | `audit_turn(text_without_keywords, [], None, None, campaign={...})` → `campaign_beat_not_delivered`. |
| `test_audit_ignores_delivered_beat` | text containing a keyword → no warning. |
| `test_audit_flags_truth_frontrun` | all spoiler keywords of a sealed truth present → `campaign_truth_frontrun`; only one present → no warning. |
| `test_audit_code_actions_suppress_quest_check` | `"QUEST ACCEPTED"` in text, no quest tool, `code_actions={"quest"}` → no `quest_banner_without_tool`. |
| `test_every_prompt_tool_reference_is_registered` (existing) | still passes with `get_campaign` referenced in the DM prompt. |

---

## 10. Considered and rejected (one line each)

- **Beats as tool calls the DM makes** — rejected: the DM skips, reorders, and
  rewrites what it is asked to call; code firing is the only way a story is "set in
  stone".
- **Generating the campaign at world creation (WorldForge)** — rejected: stakes must
  be personal to the PC, and the PC doesn't exist yet; also every rebirth would
  inherit a stranger's story.
- **Blocking the first turn on generation** — rejected: 10-30s of dead air on the
  most important screen in the game. Background thread + `status="pending"`.
- **A `update_campaign_truth` tool ("so the DM can adapt")** — rejected: that is
  exactly the failure being fixed. No write path exists.
- **Showing the DM the next 2 pending beats "for foreshadowing"** — rejected:
  front-running is the observed failure mode; starve the model instead of prompting
  it harder.
- **An LLM judge deciding whether a beat was delivered** — rejected: an extra
  round-trip per turn. Keyword matching with a 3-turn re-surface is good enough and
  free.
- **A separate `campaign_clocks` table** — rejected: a clock is just a deadline beat
  with a label; two sources of truth would drift.
- **Numeric fields in the generator payload (`power_delta`, `days`)** — rejected:
  code owns every number; the model picks labels (`"weakened"`, `"short"`).

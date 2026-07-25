# Cinematic Language — title cards, boss intros, setpieces

**Status:** implementation-ready spec (v1 scope defined at the bottom)
**Owner system:** presentation. Touches: combat, campaign beats, audio, canvas.
**Design law:** the LLM picks a LABEL from a fixed menu. Code owns every pixel,
every millisecond, every cooldown. Nothing here adds an LLM round-trip.

---

## 0. The point

Forge already has the raw materials for drama — a PixiJS world container that can
tint/zoom/shake, a particle layer, a screen-space UI layer, DOM overlays, an
adaptive music palette with crossfade + stingers, TTS voices, and a real-time
combat loop. What it lacks is a **vocabulary**: a small set of named moments that
always look the same, always land, and are rare enough to mean something.

This spec defines five moments, end to end. An LLM (or a campaign beat, or the
combat loop) says a word; the engine performs it.

The anti-spam machinery is not a footnote — it is the feature. A title card that
fires every third turn is a bug that makes the game feel *worse* than having no
title cards at all. Cooldowns are enforced in code, in the database, and survive a
page reload.

---

## 1. The v1 vocabulary

| kind | what it is | duration | freezes | skippable | trigger paths |
|---|---|---|---|---|---|
| `title_card` | Letterbox bars + act/chapter text over a dimmed world, slow zoom | 4200 ms | input only | after 400 ms | DM tool, campaign beat |
| `boss_intro` | World freeze + camera push-in on the enemy + name splash + hit | 3600 ms | world | after 1200 ms | DM tool, **auto** on boss `combat_start` |
| `kill_cam` | Slow-motion + zoom on the killing blow | ~1500 ms real | no (time-scaled) | no | **auto only** (boss death, client-side) |
| `vision` | Dream/memory treatment: cold tint, vignette, motes, whisper-duck | until end of turn (+2.5 s), cap 45 s | no | after 600 ms | DM tool, campaign beat |
| `weather` | Persistent scene modifier: rain/snow/fog/embers/ash/clear | persistent (world state) | no | n/a | DM tool, campaign beat, DB state |

**Considered and rejected for v1: `setpiece`.** As proposed (slow zoom + vignette +
weather burst) it is a recombination of `title_card`'s framing, `vision`'s vignette
and `weather`'s particles, and its trigger condition ("a major beat") is exactly the
vague language that turns into spam. It goes in *later* as a composite once the
primitives are proven in play. Everything in v1 has a trigger condition you can
write a one-line rule for.

---

## 2. Schema deltas

Three changes. All are new-table or new-nullable-column, so `data/emberfall.db`
loads and plays unchanged with every cinematic feature absent (no bosses, no
weather, empty log).

### 2.1 New table `cinematics` — the ledger *and* the pending queue

`src/models/world_state.py` (new class, exported from `src/models/__init__.py`).
`Base.metadata.create_all` in `init_db` creates it on existing DBs automatically.

```python
class Cinematic(Base):
    """Ledger of played/rejected cinematics + queue for deferred ones.

    Cooldown enforcement reads this table; it is the only reason cinematics
    stay rare across page reloads and server restarts.
    """
    __tablename__ = "cinematics"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    player_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)          # title_card | boss_intro | vision | weather
    dedupe_key: Mapped[str | None] = mapped_column(String(120), nullable=True)  # npc_id | slug(title) | weather label
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    subtitle: Mapped[str | None] = mapped_column(String(200), nullable=True)
    npc_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mood: Mapped[str | None] = mapped_column(String(30), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="dm")          # dm | auto | campaign
    status: Mapped[str] = mapped_column(String(20), default="played")      # played | pending | rejected
    reject_reason: Mapped[str | None] = mapped_column(String(60), nullable=True)
    game_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    game_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    game_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
```

### 2.2 `NPC.boss_title` — the only definition of "boss" in v1

`src/models/npc.py`, next to `combat_archetype`:

```python
    # Cinematic boss flag: a non-NULL epithet ("Warden of the Ashen Gate")
    # marks this NPC as a boss — code auto-plays boss_intro when a fight with
    # them starts and kill_cam when they fall. NULL = ordinary NPC.
    boss_title: Mapped[str | None] = mapped_column(String(120), nullable=True)
```

`_migrate_missing_columns` in `src/models/base.py` adds it to existing DBs as
`ADD COLUMN "boss_title" VARCHAR(120) DEFAULT NULL` — no action needed.

`is_boss(npc) -> bool` is `bool(npc.boss_title)`. **Integration note:** if the NPC
tier/elite system lands a `combat_tier` column, `is_boss` should prefer
`npc.combat_tier == "boss"` and fall back to `boss_title`; keep the helper in one
place (`src/services/cinematics.py`) so that is a one-line change.

### 2.3 `DMState.weather` — weather is world state, not an effect

`src/models/world_state.py`, in `DMState`:

```python
    # Persistent weather label (rain|snow|fog|embers|ash) — NULL/"clear" = none.
    # The frontend restores it on reload and on scene change from the state event.
    weather: Mapped[str | None] = mapped_column(String(30), nullable=True)
```

---

## 3. The service — `src/services/cinematics.py` (new)

Single source of truth for the vocabulary, durations, cooldowns and the ledger.
No LLM, no I/O beyond SQLite. Every caller goes through `request_cinematic`.

```python
"""Cinematic vocabulary + cooldown enforcement. Code decides; the LLM labels.

A cinematic takes over the screen. Rarity is the whole product: the limits
below are enforced against a DB ledger so a page reload can't reset them and
a chatty DM turn can't stack two of them.
"""

VOCABULARY = ("title_card", "boss_intro", "kill_cam", "vision", "weather")
DM_CALLABLE = ("title_card", "boss_intro", "vision", "weather")   # kill_cam is combat-internal
WEATHER_LABELS = ("rain", "snow", "fog", "embers", "ash", "clear")

# Informational for the client + used for audio hold windows. The JS table in
# game.js is the authority on actual tween timing; test_cinematics keeps them equal.
DURATIONS_MS = {"title_card": 4200, "boss_intro": 3600, "kill_cam": 1500,
                "vision": 0, "weather": 0}   # 0 = open-ended / stateful

# kind -> (min game-MINUTES since the same kind,
#          max plays per game DAY,
#          min REAL seconds since ANY cinematic,
#          max plays per rolling 60 real minutes)
LIMITS = {
    "title_card": (720, 1, 45, 4),
    "boss_intro": (0,   2, 15, 3),
    "vision":     (120, 2, 45, 5),
    "weather":    (45,  4, 10, 12),
}
SESSION_WINDOW_MIN = 60      # the rolling real-time window = "a session"

REJECT_REASONS = (
    "unknown_kind", "not_dm_callable", "bad_weather_label", "missing_title",
    "cooldown_game_time", "daily_cap", "too_soon", "session_cap",
    "duplicate", "boss_already_introduced", "world_not_initialized",
)
```

### 3.1 API

```python
def request_cinematic(
    kind: str,
    *,
    player_id: str | None = None,
    title: str | None = None,
    subtitle: str | None = None,
    npc_id: str | None = None,
    mood: str | None = None,
    source: str = "dm",          # dm | auto | campaign
    defer: bool = False,         # True → queue it (status='pending') instead of returning it
    dm_callable_only: bool = True,
) -> dict[str, Any]:
    """Validate + rate-limit a cinematic request.

    Returns EITHER
      {"ok": True,  "payload": {kind, title, subtitle, npc_id, mood, duration_ms}}
    OR
      {"ok": False, "reason": "<one of REJECT_REASONS>", "detail": "<human text>"}

    Every call — accepted or rejected — writes one row to `cinematics`
    (status 'played' | 'pending' | 'rejected'). The ledger IS the cooldown state.
    """
```

```python
def drain_pending(player_id: str) -> list[dict[str, Any]]:
    """Pop queued cinematics (status='pending') for this player, oldest first,
    flip them to 'played', and return their payloads. Called once per turn by
    /api/play right after the world tick (which is when campaign beats fire)."""
```

```python
def is_boss(npc) -> bool: ...
def boss_intro_payload(npc, player_id: str) -> dict | None:
    """Auto path: build a boss_intro payload for this NPC if the cooldown
    allows and they have never been introduced. Returns None otherwise."""
```

```python
def recent(limit: int = 20) -> list[dict[str, Any]]:
    """Ledger rows for the debug endpoint / self-playtest scoreboard."""
```

### 3.2 Enforcement rules (exact)

Evaluated in this order; the first failure wins and is logged as `reject_reason`.

1. `kind not in VOCABULARY` → `unknown_kind`.
2. `dm_callable_only and kind not in DM_CALLABLE` → `not_dm_callable`
   (this is what blocks the DM from firing `kill_cam`).
3. `kind == "weather" and (mood or "").lower() not in WEATHER_LABELS` → `bad_weather_label`.
4. `kind == "title_card" and not (title or "").strip()` → `missing_title`.
5. **Duplicate identity.** `dedupe_key` = `npc_id` for `boss_intro`,
   `slug(title)` for `title_card`, `mood` for `weather`, `None` for `vision`.
   - `boss_intro` with a `dedupe_key` already present with status `played` →
     `boss_already_introduced` (a boss gets exactly one entrance, ever).
   - `title_card` with the same `dedupe_key` already played → `duplicate`.
   - `weather` whose `mood` equals `DMState.weather` → `duplicate` (no-op set).
6. **Real-time floor.** Seconds since the most recent `played` row of ANY kind
   `< LIMITS[kind][2]` → `too_soon`. (Stops two cinematics inside one DM turn.)
7. **Same-kind game-time cooldown.** Game minutes since the last `played` row of
   this kind `< LIMITS[kind][0]` → `cooldown_game_time`.
   Game minutes = `(day*24*60 + hour*60 + minute)` from `WorldClock`.
8. **Per game day cap.** `played` rows of this kind with `game_day == today`
   `>= LIMITS[kind][1]` → `daily_cap`.
9. **Session cap.** `played` rows of this kind with
   `created_at >= now - SESSION_WINDOW_MIN` `>= LIMITS[kind][3]` → `session_cap`.

Side effects on success:
- `kind == "weather"` writes `DMState.weather = mood` (or `NULL` when `mood == "clear"`).
  The SSE event is the *transition*; `DMState.weather` is the durable truth that
  `build_player_state` re-broadcasts on reload and scene change.
- All kinds insert the ledger row with the current clock stamp.

**Rejected alternative:** an in-process per-session counter on the FastAPI session
dict. Rejected because a page reload (or a server restart during a long session)
resets it and the DM immediately re-fires the same card.

---

## 4. Trigger paths

### 4.1 DM tool — `play_cinematic`

New file `src/tools/world_write/cinematics.py`, exported from
`src/tools/world_write/__init__.py`, registered in `DM_TOOLS`.

```python
@tool
def play_cinematic(
    player_id: str,
    kind: str,
    title: str | None = None,
    subtitle: str | None = None,
    npc_id: str | None = None,
    mood: str | None = None,
) -> dict[str, Any]:
    """Play a cinematic moment on the player's screen. Use sparingly.

    kind:
      "title_card" — a chapter break. title = 2-5 words, subtitle = one line.
      "boss_intro" — the arrival of a named dangerous enemy. Pass npc_id.
                     Call it in the same turn as start_combat, BEFORE it.
      "vision"     — dream/memory/prophecy treatment; call it, then narrate.
      "weather"    — set world weather. mood = rain|snow|fog|embers|ash|clear.

    The engine owns all presentation and enforces cooldowns: if this returns
    rejected, that is normal — say nothing about it and narrate the moment in
    prose instead.
    """
```

**Return — accepted:**

```python
{
  "success": True,
  "event": "cinematic",          # the tool-result event mechanism (speak/scene precedent)
  "kind": "title_card",
  "title": "THE ROAD NORTH",
  "subtitle": "Three days from Emberfall",
  "npc_id": None,
  "mood": None,
  "duration_ms": 4200,
}
```

**Return — rejected (NO `event` key, so nothing reaches the client):**

```python
{
  "success": False,
  "rejected": True,
  "reason": "cooldown_game_time",
  "instruction": (
    "Cinematic not played (too soon since the last one). This is normal and "
    "expected. Do NOT mention it, do NOT describe a title card or its text — "
    "just narrate the moment in prose."
  ),
}
```

### 4.2 Automatic — boss intro on `combat_start`

`src/services/combat.py: build_combat_start()` — after the roster loop, inside the
same `get_session()` block, collect boss NPCs; after the block:

```python
    cinematic = None
    boss = next((n for n in boss_npcs), None)     # first boss in the roster
    if boss is not None:
        cinematic = boss_intro_payload(boss, player_id)   # None if on cooldown / already introduced

    result = {"success": True, "event": "combat_start", "enemies": enemies, "instruction": ...}
    if cinematic:
        result["cinematic"] = cinematic
    return result
```

Each entry in `enemies` gains two fields so the client can drive `kill_cam`:

```python
    enemies.append({
        "npc_id": npc.id,
        "name": npc.name,
        "archetype": infer_archetype(npc),
        "boss": is_boss(npc),                 # NEW
        "boss_title": npc.boss_title,         # NEW (may be None)
    })
```

The `"cinematic"` key on **any** tool result is converted to a `cinematic` SSE event
by the tracker (§5), emitted **before** the tool's own event — so the client sees
`boss_intro` first and defers the enemy spawn until it ends.

### 4.3 Automatic — kill cam on boss death

Purely client-side, inside `GameRenderer._updateCombat`'s attack hit-test. No REST
call, no server round-trip: the fight is already client-authoritative and the
outcome POST happens afterwards as it does today.

Rule (deliberately narrow so slow-mo never gets you killed):

> Fire `kill_cam` when the enemy that just dropped to 0 HP `isBoss` **and** every
> other enemy in `combat.enemies` is already dead.

Not logged to the ledger, not rate-limited: the trigger condition can fire at most
once per boss per fight.

### 4.4 Campaign beats (interface only — the campaign spec owns its schema)

A beat carries an optional cinematic label. When the campaign layer activates a
beat (during the world tick inside `dm._build_context`, i.e. *before* the DM
streams), it calls:

```python
from src.services.cinematics import request_cinematic

request_cinematic(
    beat.cinematic_kind,             # "title_card" | "vision" | "weather"
    player_id=player_id,
    title=beat.cinematic_title,      # campaign-authored strings
    subtitle=beat.cinematic_subtitle,
    mood=beat.cinematic_mood,
    source="campaign",
    defer=True,                      # queue it — there is no tool result to ride on
)
```

`/api/play` drains the queue immediately after context building (§6.2). Campaign
beats obey exactly the same cooldowns as the DM; `source` only affects the ledger.

**Contract for the campaign spec:** it may pass any `kind` in `DM_CALLABLE` plus the
optional strings above. It must not pass durations, colors, or timings.

---

## 5. Event plumbing

### 5.1 `src/web/streaming.py: ToolUsageTracker._parse_tool_output_for_events`

Add a normalizer and two entry points. The nested-key check runs **first** so a
tool that both starts combat and carries an intro emits the cinematic first.

```python
    _CINE_FIELDS = ("kind", "title", "subtitle", "npc_id", "mood", "duration_ms")

    def _push_cinematic(self, data: dict) -> None:
        if not isinstance(data, dict) or not data.get("kind"):
            return
        note = {"type": "cinematic"}
        note.update({k: data.get(k) for k in self._CINE_FIELDS})
        self._notifications.append(note)
```

In `_parse_tool_output_for_events`, immediately after `output_data` is parsed and
before `event = output_data.get("event")`:

```python
            # A cinematic can ride along on ANY tool result (start_combat carries
            # a boss intro, campaign tools carry title cards). Emitted first so
            # the client can defer whatever the tool's own event triggers.
            self._push_cinematic(output_data.get("cinematic"))
```

and in the `elif` chain (for `play_cinematic`'s own result):

```python
            elif event == "cinematic":
                self._push_cinematic(output_data)
```

### 5.2 SSE payloads (exact)

The notification dict is serialized verbatim by the existing
`yield f"data: {json.dumps(notification)}\n\n"` loop in `/api/play`.

```jsonc
// title_card
{"type":"cinematic","kind":"title_card","title":"THE ROAD NORTH",
 "subtitle":"Three days from Emberfall","npc_id":null,"mood":null,"duration_ms":4200}

// boss_intro (auto, riding on start_combat's tool result)
{"type":"cinematic","kind":"boss_intro","title":"COLDIRON",
 "subtitle":"Warden of the Ashen Gate","npc_id":"3f2c…","mood":null,"duration_ms":3600}

// vision
{"type":"cinematic","kind":"vision","title":null,"subtitle":null,
 "npc_id":null,"mood":null,"duration_ms":0}

// weather
{"type":"cinematic","kind":"weather","title":null,"subtitle":null,
 "npc_id":null,"mood":"rain","duration_ms":0}
```

`kill_cam` never crosses the wire.

### 5.3 `state` event gains `weather`

`src/web/server.py: build_player_state` — inside the existing DMState `try` block:

```python
            weather = (dm_state.weather if dm_state else None) or "clear"
```

then, before the return:

```python
    # Weather is world state, but it is invisible indoors — code decides, so the
    # client never has to know what a "room" is.
    if str(location.get("type", "")).lower() in {"interior", "room", "building"}:
        weather = "clear"
```

and add `"weather": weather` to the returned dict. This makes weather survive
reloads, world switches and scene changes with zero extra endpoints.

---

## 6. Server changes

### 6.1 Tool registration

- `src/tools/world_write/__init__.py`: import + `__all__` entry for `play_cinematic`.
- `src/agents/dm_orchestrator.py`: import it and add to `DM_TOOLS` (in the
  "World state writes" block, after `start_combat`).

### 6.2 `/api/play` — drain the campaign queue

`src/web/server.py`, in `_run_turn`, immediately after the `prior_warnings` block
and before `set_web_output_callback(narration_callback)`:

```python
            # Campaign beats fired during the world tick (inside _build_context)
            # may have queued a cinematic — deliver it before the DM's first token
            try:
                for cine in drain_pending(request.player_id):
                    yield f"data: {json.dumps({'type': 'cinematic', **cine})}\n\n"
            except Exception as e:
                logger.warning(f"Cinematic queue drain failed: {e}")
```

### 6.3 Debug endpoint (no fast path depends on it)

```
GET /api/cinematics/recent?limit=20
→ 200 {"cinematics":[{"kind":"title_card","title":"THE ROAD NORTH","source":"dm",
        "status":"played","reject_reason":null,"game_day":3,"game_hour":19,
        "created_at":"2026-07-25T18:02:11"}, …]}
```

Read-only, for playtest scoreboards ("did the DM try to spam title cards?").

---

## 7. DM prompt

New section in `DM_SYSTEM_PROMPT` (`src/agents/dm_orchestrator.py`), placed
immediately after **3. Combat & Danger** inside GUIDELINES FOR SPECIFIC SITUATIONS.
Draft text, verbatim:

```
**4. Cinematic Moments (RARE — they only work because they are rare):**
- `play_cinematic(player_id, kind, title, subtitle, npc_id, mood)` takes over the
  player's screen. Used two or three times in a session it is the strongest tool
  you have. Used every few turns it is noise, and the engine will REJECT it.
- Kinds — you choose the LABEL, the engine owns every pixel and every timing:
  - `title_card` — a chapter break. ONLY when the story genuinely turns a page: an
    act ends, a journey of days begins, the player crosses into a new chapter of
    their story. `title` is 2-5 words in the story's voice ("THE ROAD NORTH");
    `subtitle` is one short line ("Three days from Emberfall"). At most once per
    in-game day, and most days deserve none.
  - `boss_intro` — the arrival of a named, dangerous, story-important enemy. Pass
    their `npc_id`. Call it in the SAME turn as `start_combat` and BEFORE it. Each
    character gets one entrance, ever; the engine also plays it automatically for
    enemies the world marks as bosses.
  - `vision` — the treatment for a dream, memory, prophecy, hallucination or
    drug-vision. Call it FIRST, then narrate the vision normally. It ends by itself
    when your turn ends.
  - `weather` — set the weather for the world: `mood` = rain, snow, fog, embers,
    ash, or clear. This is world state, not a moment: set it when the sky actually
    changes and then leave it alone. Do not "set" the weather that is already there.
- NEVER describe the effect in your prose. Do not write "a title card reads", do
  not print "ACT II", "CHAPTER THREE", "cut to black", or the card's own text. The
  screen shows it; you write the fiction.
- If the tool returns `rejected`, that is normal and expected. Say nothing about it
  and narrate the moment in prose instead — the story never depends on the effect.
- Never call it twice in one turn, and never during a real-time fight (the only
  exception is `boss_intro` immediately before `start_combat`).
```

`tests/test_dm_prompt_tools.py` must gain `"play_"` in `TOOL_PREFIXES` so the
prompt/tool sync check actually covers this tool.

---

## 8. Reality-audit adaptation

`src/web/turn_audit.py`.

**No false positives from the new code paths.** `play_cinematic` claims no
mechanics — it touches no health, items, quests or location — so none of the
existing checks can fire because of it. `boss_intro`/`kill_cam` are code-side and
produce no narration. Weather changes `DMState.weather`, which no check reads.
Nothing to relax.

**One new high-precision check** — the failure mode this feature introduces is the
DM *typing the card* instead of playing it (especially after a rejection):

```python
_CINEMATIC_TEXT_CLAIM = re.compile(
    r"(?m)(?:^\s*(?:ACT|CHAPTER|PART|EPISODE)\s+(?:[IVXL]+|\d+|ONE|TWO|THREE|FOUR|FIVE)\b"
    r"|\btitle card\b|\bcut to black\b|\bfade to black\b|\bletterbox\b"
    r"|\bthe screen (?:goes|fades|shakes)\b)",
    re.IGNORECASE,
)
```

```python
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
```

This one fires regardless of tool calls — printing screen directions is always
wrong, whether or not the cinematic played. It is literal-string precise, so a
normal fantasy sentence cannot trip it. It flows into the `[REALITY AUDIT]`
next-turn block through the existing `format_audit_feedback` path with no changes.

---

## 9. Frontend

### 9.1 `index.html` — markup

Insert inside `#game-container`, immediately after the `#travel-overlay` div
(so cinematics are scoped to the canvas and the chat panel is never covered):

```html
        <!-- Cinematic overlay: bars/scrim/text are DOM (crisp type, CSS
             transitions); zoom, tint, shake and particles are PixiJS. -->
        <div id="cine-overlay" aria-hidden="true">
            <div class="cine-scrim"></div>
            <div class="cine-vignette"></div>
            <div class="cine-flash"></div>
            <div class="cine-bar top"></div>
            <div class="cine-bar bottom"></div>
            <div class="cine-text">
                <div class="cine-title" id="cine-title"></div>
                <div class="cine-rule"></div>
                <div class="cine-sub" id="cine-sub"></div>
            </div>
            <div class="cine-skip" id="cine-skip">any key &middot; Esc to skip</div>
        </div>
```

### 9.2 `index.html` — CSS

Added to the existing `<style>` block, after the `.toast` rules. z-index 970 sits
above `#toast-stack` (900) and the controls overlay (950), below `#travel-overlay`
(1000) and the death overlay (2000) — a death screen always wins.

```css
        /* ===== Cinematics ===== */
        #cine-overlay {
            position: absolute; inset: 0; z-index: 970;
            pointer-events: none; opacity: 0; visibility: hidden;
            transition: opacity 0.35s ease;
            font-family: Georgia, serif;
        }
        #cine-overlay.active { opacity: 1; visibility: visible; }
        #cine-overlay.skippable { pointer-events: auto; cursor: pointer; }

        .cine-scrim {
            position: absolute; inset: 0; background: #000;
            opacity: 0; transition: opacity 0.5s ease;
        }
        #cine-overlay.dim .cine-scrim { opacity: 0.55; }

        .cine-vignette {
            position: absolute; inset: 0; opacity: 0;
            transition: opacity 0.8s ease;
            background: radial-gradient(ellipse at 50% 50%,
                rgba(0,0,0,0) 38%, rgba(0,0,0,0.55) 82%, rgba(0,0,0,0.8) 100%);
        }
        #cine-overlay.vignette .cine-vignette { opacity: 1; }
        #cine-overlay.boss .cine-vignette {
            background: radial-gradient(ellipse at 50% 55%,
                rgba(0,0,0,0) 30%, rgba(60,0,0,0.5) 78%, rgba(0,0,0,0.85) 100%);
        }

        .cine-flash {
            position: absolute; inset: 0; background: #fff;
            opacity: 0; transition: opacity 0.12s ease-out;
        }
        #cine-overlay.flash .cine-flash { opacity: 0.35; transition: none; }

        .cine-bar {
            position: absolute; left: 0; right: 0; height: 12%;
            background: #000; transition: transform 0.55s cubic-bezier(.22,.9,.24,1);
        }
        .cine-bar.top    { top: 0;    transform: translateY(-100%); }
        .cine-bar.bottom { bottom: 0; transform: translateY(100%); }
        #cine-overlay.letterbox .cine-bar.top,
        #cine-overlay.letterbox .cine-bar.bottom { transform: translateY(0); }
        #cine-overlay.thin-bars .cine-bar { height: 7%; }

        .cine-text {
            position: absolute; left: 0; right: 0; top: 50%;
            transform: translateY(-50%); text-align: center; padding: 0 8%;
        }
        .cine-title {
            font-size: clamp(1.6rem, 4.2vw, 2.9rem); font-weight: 700;
            letter-spacing: 0.22em; text-transform: uppercase; color: #f4f0e6;
            text-shadow: 0 2px 24px rgba(0,0,0,0.9);
            opacity: 0; transform: translateY(14px) scale(1.0);
            transition: opacity 0.6s ease, transform 0.7s cubic-bezier(.22,.9,.24,1);
        }
        .cine-rule {
            width: 0; height: 1px; margin: 0.7rem auto;
            background: rgba(244,240,230,0.55);
            transition: width 0.7s cubic-bezier(.22,.9,.24,1) 0.15s;
        }
        .cine-sub {
            font-size: clamp(0.85rem, 1.5vw, 1.05rem); font-style: italic;
            letter-spacing: 0.12em; color: #cbd5e1;
            text-shadow: 0 2px 14px rgba(0,0,0,0.9);
            opacity: 0; transition: opacity 0.6s ease 0.35s;
        }
        #cine-overlay.show-text .cine-title { opacity: 1; transform: translateY(0) scale(1); }
        #cine-overlay.show-text .cine-rule  { width: min(320px, 42%); }
        #cine-overlay.show-text .cine-sub   { opacity: 1; }

        /* Boss splash: heavier, red-lit, snaps in instead of drifting */
        #cine-overlay.boss .cine-title {
            font-weight: 800; letter-spacing: 0.16em; color: #f8f4ec;
            text-shadow: 0 0 28px rgba(220,38,38,0.55), 0 2px 18px rgba(0,0,0,0.95);
            transform: translateY(0) scale(1.16);
            transition: opacity 0.18s ease, transform 0.22s cubic-bezier(.2,1.3,.3,1);
        }
        #cine-overlay.boss.show-text .cine-title { transform: translateY(0) scale(1); }
        #cine-overlay.boss .cine-sub { color: #fca5a5; font-style: normal; letter-spacing: 0.2em; }

        .cine-skip {
            position: absolute; right: 18px; bottom: 14%;
            font-family: system-ui, sans-serif; font-size: 0.7rem;
            letter-spacing: 0.08em; color: rgba(229,225,216,0.55);
            opacity: 0; transition: opacity 0.4s ease;
        }
        #cine-overlay.skippable .cine-skip { opacity: 1; }

        @media (prefers-reduced-motion: reduce) {
            .cine-bar, .cine-title, .cine-rule, .cine-sub { transition-duration: 0.15s; }
        }
```

### 9.3 `index.html` — script changes

1. **Cache-buster (mandatory).** Line ~1700:
   `game.js?v=20260724a` → `?v=20260725a`, same for `audio.js`.
2. **SSE handler** (`sendMessage`, the `payload.type` chain ~line 2867) — add
   before the `npc_death` branch:

```js
                                    } else if (payload.type === 'cinematic') {
                                        if (game && game.playCinematic) game.playCinematic(payload);
```

   `window.forgeAudio.handlePlayEvent(payload)` already runs for every payload at
   the top of the loop, so the audio side needs no call here.
3. **Vision auto-exit**: in the `finally` block of `sendMessage` (next to
   `flushChatRender()`), add `if (game && game.endVision) game.endVision(2500);`
   — the vision treatment holds 2.5 s past the end of the turn that opened it.
4. **Weather restore**: in `applyPlayerState`, next to the `setTimeOfDay` line:

```js
            if (game && game.setWeather) game.setWeather(state.weather || 'clear');
```

5. **Scene-change safety**: in `handleLocationChange`, after
   `await game.loadLocation(...)`, nothing extra is needed — `loadLocation` ends any
   running cinematic itself (§9.4.10).

### 9.4 `game.js` — changes by function

**1. `constructor`** — new state:

```js
        // Cinematics (see docs/design/cinematics.md)
        this.cine = null;                  // active moment: {kind, t, dur, …}
        this.cineTint = { r: 1, g: 1, b: 1 };   // multiplies the day/night tint
        this.cineTintTarget = { r: 1, g: 1, b: 1 };
        this.cineShake = 0;
        this.cineZoom = 1;
        this.cineFocus = null;             // {x, y} in world pixels
        this.timeScale = 1;                // kill_cam slow-mo
        this._pendingCombat = null;        // enemies queued behind a boss_intro
        this.weather = 'clear';
        this._weatherParts = [];
```

**2. `init()`** — screen-space weather layer, inserted directly **before** the
existing `this.uiLayer = new PIXI.Container();` lines so z-order is
world → weather → HUD:

```js
        // Weather is screen-space: it must not scroll with the camera
        this.weatherLayer = new PIXI.Container();
        this.weatherLayer.eventMode = 'none';
        this.app.stage.addChild(this.weatherLayer);
```

and cache the DOM refs:

```js
        this.cineEl = document.getElementById('cine-overlay');
        this.cineTitleEl = document.getElementById('cine-title');
        this.cineSubEl = document.getElementById('cine-sub');
        if (this.cineEl) {
            this._onCineClick = () => {
                if (this.cine && this.cine.skippable && performance.now() >= this.cine.skipAt) {
                    this._endCinematic(false);
                }
            };
            this.cineEl.addEventListener('pointerdown', this._onCineClick);
        }
```

**3. `setupInput()._onKeyDown`** — two insertions.

At the very top, *before* the INPUT/TEXTAREA guard (so Esc works while the chat box
has focus, which it does after every turn):

```js
            if (e.key === 'Escape' && this.cine && this.cine.skippable &&
                performance.now() >= this.cine.skipAt) {
                this._endCinematic(false);
                e.preventDefault();
                return;
            }
```

Directly after the INPUT/TEXTAREA guard (any key skips when not typing):

```js
            if (this.cine && this.cine.skippable && performance.now() >= this.cine.skipAt) {
                this._endCinematic(false);
                e.preventDefault();
                return;
            }
```

**4. `update()`** — the top of the function becomes:

```js
    update() {
        if (!this.player || this.isLoading) return;

        const realDt = this.app.ticker.deltaMS;
        this._updateCinematic(realDt);      // always real time — the show must finish
        this._updateWeather(realDt);
        const deltaTime = realDt * this.timeScale;   // kill_cam slows the world

        this._updateSceneTint(realDt);
        this._updateFloaters(realDt);
        this._updateBubbles();

        // A world-freezing cinematic (boss_intro) suspends simulation and input
        if (this.cine && this.cine.freeze === 'world') {
            this.updateCamera();
            return;
        }
        if (this.playerDead) { this.updateCamera(); return; }
        …
```

The existing `const deltaTime = this.app.ticker.deltaMS;` line is deleted (it is now
computed above), and the movement block is guarded so input-freezing cinematics
still animate the world:

```js
        const inputLocked = !!(this.cine && this.cine.freeze);
        …
        if (this.keys['w'] … ) { … }        // wrap the WASD reads in `if (!inputLocked)`
```

**5. `updateCamera()`** — append after `this.worldContainer.y = -this.camera.y;`
and *before* the existing combat-shake block:

```js
        // Cinematic zoom: keep the focus point pinned while the world scales
        const z = this.cineZoom || 1;
        if (z !== 1) {
            const f = this.cineFocus || { x: this.player.sprite.x, y: this.player.sprite.y };
            this.worldContainer.scale.set(z);
            this.worldContainer.x = this.app.screen.width * 0.5 - f.x * z;
            this.worldContainer.y = this.app.screen.height * 0.55 - f.y * z;
        } else if (this.worldContainer.scale.x !== 1) {
            this.worldContainer.scale.set(1);
        }
```

and extend the shake block to include `this.cineShake`:

```js
        const shake = Math.max(this.cineShake || 0, (this.combat && this.combat.shake) || 0);
        if (shake > 0) {
            this.worldContainer.x += (Math.random() - 0.5) * shake * 2;
            this.worldContainer.y += (Math.random() - 0.5) * shake * 2;
        }
```

**6. `_updateSceneTint(dt)`** — multiply in the cinematic tint (eased over 700 ms)
right before writing `worldContainer.tint`:

```js
        const k2 = Math.min(1, dt / 700);
        const ct = this.cineTint, tt = this.cineTintTarget;
        ct.r += (tt.r - ct.r) * k2; ct.g += (tt.g - ct.g) * k2; ct.b += (tt.b - ct.b) * k2;
        this.worldContainer.tint =
            (Math.round(c.r * ct.r * 255) << 16) |
            (Math.round(c.g * ct.g * 255) << 8) |
             Math.round(c.b * ct.b * 255);
```

**7. New: `playCinematic(p)`** — the single entry point from SSE.

```js
    static CINE = {
        title_card: { dur: 4200, skipAfter: 400,  freeze: 'input', zoom: 1.06, blocksCombat: false },
        boss_intro: { dur: 3600, skipAfter: 1200, freeze: 'world', zoom: 1.35, blocksCombat: true  },
        kill_cam:   { dur: 1500, skipAfter: null, freeze: null,    zoom: 1.45, blocksCombat: false },
        vision:     { dur: 45000, skipAfter: 600, freeze: null,    zoom: 1.04, blocksCombat: false },
    };

    playCinematic(p) {
        if (!p || !p.kind) return;
        if (p.kind === 'weather') { this.setWeather(p.mood || 'clear'); return; }
        // Client-side safety: a fight owns the screen. boss_intro precedes the
        // spawn, so it is the only one allowed while combat is pending.
        if (this.combat && p.kind !== 'kill_cam') return;
        if (this.cine) this._endCinematic(true);   // never stack
        …builds this.cine, sets DOM classes, kicks audio…
    }
```

Per-kind realization (`playCinematic` switch):

- **title_card** — DOM: `active dim letterbox` immediately; `show-text` on a 520 ms
  timer; `skippable` at +400 ms. Canvas: `cineFocus` = player sprite, `cineZoom`
  tweened 1.00→1.06 across the full 4200 ms (ease-out). Audio:
  `forgeAudio.holdVoiceQueue(true)`, `duckFor(0.35)`, `playSfx('whoosh',{volume:0.5})`
  at bar-in, `playStinger('epic', 9000)` at text-in.
  Out at t=3600: drop `show-text`; at t=4000 drop `letterbox dim`; end at 4200.
- **boss_intro** — DOM: `active thin-bars letterbox boss vignette dim`; title =
  `p.title` (NPC name), sub = `p.subtitle` (`boss_title`). `show-text` at 900 ms
  (after the push-in has travelled), `skippable` at 1200 ms. Canvas: `cineFocus` =
  the boss NPC sprite position if `p.npc_id` resolves in `this.npcs`, else the
  player; `cineZoom` 1.00→1.35 ease-out over 1400 ms, hold, 1.35→1.00 over the last
  500 ms. Spotlight: every other NPC sprite `alpha = 0.35` for the duration (restored
  on end); the boss sprite gets `scale *= 1.05` and back. `cineTintTarget =
  {r:1.0,g:0.86,b:0.86}`. `cineShake = 9` at the splash frame, decayed in
  `_updateCinematic`. Audio: `holdVoiceQueue(true)`, `duckFor(0.30)`,
  `playSfx('boom')` on the splash, `playStinger('epic', 10000)`.
- **vision** — DOM: `active vignette` only (no bars, no dim, no text). Canvas:
  `cineTintTarget = {r:0.72,g:0.76,b:1.00}`; `cineZoom` drifts 1.00→1.04 over 20 s;
  motes spawn into `particleLayer` every 220 ms (r 1-2, `0xdfe6ff`, alpha 0.22,
  vy −0.006, maxLife 3200, cap 40 — same particle struct `_updateJuice` already
  drains). `skippable` at 600 ms. Audio: `duckFor(0.18)`, **voice queue NOT held**
  (visions have voices), `playSfx('whoosh',{volume:0.35})` on enter and on exit.
  Ends via `endVision(delayMs)` from the turn's `finally`, or the 45 s cap.
- **kill_cam** — see `_startKillCam` below.

**8. New: `_updateCinematic(realDt)`** — advances `this.cine.t`, drives the zoom/
tint/shake tweens from the per-kind table, decays `cineShake` (`-= realDt * 0.02`),
flips the `show-text` / `skippable` DOM classes on their timers, and calls
`_endCinematic(true)` when `t >= dur`. For `kill_cam` it also ramps `timeScale`
back: `t < 900` → 0.18; `900 ≤ t < 1300` → lerp 0.18→1.0; else 1.0.

**9. New: `_endCinematic(silent)`** — clears every DOM class except `active`, drops
`active` on a 350 ms fade, restores `cineZoom = 1`, `cineTintTarget = {1,1,1}`,
`timeScale = 1`, `cineShake = 0`, restores NPC alphas/scale, calls
`forgeAudio.holdVoiceQueue(false)` + `forgeAudio.duckFor(1)`, then:

```js
        if (this._pendingCombat) {
            const enemies = this._pendingCombat;
            this._pendingCombat = null;
            this.startCombat(enemies);
        }
```

**10. `loadLocation()`** — after `if (this.combat) this._endCombat(true);` add
`if (this.cine) this._endCinematic(true);`. The weather layer is **not** cleared
(weather is world state); `setWeather` is re-driven by the next `state` event.

**11. `startCombat(enemies)`** — first lines:

```js
        if (this.cine && GameRenderer.CINE[this.cine.kind]?.blocksCombat) {
            this._pendingCombat = enemies;     // boss intro is playing — spawn after
            window.dispatchEvent(new CustomEvent('combat-active', { detail: { active: true } }));
            return;
        }
```

**12. `_spawnCombatant(npcId, archetype, name)`** — signature gains `isBoss`; store
`isBoss` on the enemy object. `startCombat` passes `en.boss === true`.

**13. `_updateCombat(dt)`** — in the melee hit-test, replace the death branch:

```js
                if (e.hp <= 0) {
                    e.dead = true; e.deathT = 0; e.telegraph.clear(); e.hpBar.clear();
                    window.forgeAudio?.playSfx('death', { volume: 0.9 });
                    // The last boss standing goes down in slow motion
                    if (e.isBoss && c.enemies.every(o => o === e || o.dead)) this._startKillCam(e);
                }
```

**14. New: `_startKillCam(enemy)`** —

```js
    _startKillCam(enemy) {
        if (this.cine) return;
        this.combat.hitstop = 120;
        this.cineFocus = {
            x: (this.player.sprite.x + enemy.g.x) / 2,
            y: (this.player.sprite.y + enemy.g.y) / 2,
        };
        this.cine = { kind: 'kill_cam', t: 0, dur: 1500, skippable: false, skipAt: Infinity, freeze: null };
        this.cineShake = 14;
        this.timeScale = 0.18;
        this._cineDom('active flash', true);
        setTimeout(() => this._cineDom('flash', false), 120);
        window.forgeAudio?.playSfx('boom', { volume: 1.0 });
        window.forgeAudio?.duckFor(0.15, 1500);
    }
```

The victory check (`e.deathT > 700`) is unchanged — under `timeScale 0.18` it simply
takes ~4× longer in real seconds, which is exactly the shot we want.

**15. `destroy()`** — remove the overlay pointer listener, `_endCinematic(true)`,
`setWeather('clear')`.

**16. `onResize()`** — `this._buildWeather(this.weather)` (particle field is sized
to the screen).

### 9.5 Weather system (`setWeather` / `_updateWeather` / `_buildWeather`)

Screen-space, pooled, never recreated per frame. Exact parameters — the LLM never
sees any of these numbers:

| label | count | shape | color / alpha | velocity px/s | tint multiplier | ambience |
|---|---|---|---|---|---|---|
| `rain` | 220 | 1×14 rect | `0x9fb6d4` / 0.35 | (−60, +900) ±10% | 0.82, 0.86, 0.95 | `amb_rain` |
| `snow` | 140 | circle r 1.5-3 | `0xffffff` / 0.55 | (sin drift ±25, +70) | 0.92, 0.95, 1.00 | `amb_wind` |
| `fog` | 6 | ellipse w=screen·0.9, h=screen·0.35 | `0xffffff` / 0.06 | (±8-16, 0) | 0.88, 0.90, 0.92 | `amb_wind` |
| `embers` | 70 | circle r 1-2.5, additive | `0xff8844` / 0.7 flicker | (±20, −60) | 1.00, 0.90, 0.82 | — |
| `ash` | 90 | 2×2 rect | `0xd7d3cc` / 0.40 | (±15, +45) | 0.86, 0.85, 0.84 | — |
| `clear` | 0 | — | — | — | 1, 1, 1 | — |

`setWeather(label)` is idempotent (`if (label === this.weather) return;`), cross-fades
the layer alpha 0→1 over 1200 ms, and pushes the tint multiplier into a second
multiplier slot (`this.weatherTint`, applied in `_updateSceneTint` alongside
`cineTint`). Particles wrap: anything leaving the screen respawns on the opposite
edge, so the field is stable and allocation-free. Total live particles are capped
at 240; the existing dust/floater systems are untouched.

---

## 10. Audio

`static/js/audio.js` (`ForgeAudio`).

### 10.1 New palette mood: `epic`

`src/services/music_generator.py`:

```python
    "epic": "monumental, heroic scale, choir and low brass, slow inevitable build",
```

Consequences: `/api/assets/music/manifest` now reports 7 moods; existing worlds
report `epic: {"status": "missing"|"generating"}` and generate it on the next poll
(one extra Suno job per world, backgrounded, never blocking). Frontend fallback
chain in `_trackUrl`:

```js
            epic: ['epic', 'triumph', 'danger', 'explore'],
```

`playStinger` already returns silently when no track resolves, so a world without
music simply gets a silent title card — no error path.

### 10.2 Per-kind behavior

| kind | music | SFX | voice queue |
|---|---|---|---|
| `title_card` | duck to 0.35 for 600 ms → `playStinger('epic', 9000)`; reverts to `lastStateMood` | `whoosh` (0.5) at bar-in | **held** for the duration + 300 ms |
| `boss_intro` | duck to 0.30 → `playStinger('epic', 10000)`; the following `combat_start` sets `danger`, which interrupts stingers by existing rule | `boom` (1.0) on splash | **held** |
| `kill_cam` | duck to 0.15 for 1500 ms, then restore | `boom` (1.0) + the existing `death` | not held (nothing speaks mid-fight) |
| `vision` | duck to 0.18 and hold until exit; no mood change (visions are a *treatment*, not a track) | `whoosh` (0.35) on enter + exit | **not** held — visions have voices |
| `weather` | unchanged | `setAmbience('amb_rain'\|'amb_wind'\|null)` | unchanged |

### 10.3 New `ForgeAudio` members

```js
        this.cineDuck = 1;        // extra music multiplier owned by cinematics
        this.voiceHold = false;   // title cards silence the voice queue
        this.ambSource = null;    // looping weather ambience
```

```js
    /** Cinematic ducking, independent of the voice duck (they multiply). */
    duckFor(level, ms = 0) {
        this.cineDuck = Math.max(0, Math.min(1, level));
        this._applyVolumes();
        if (this._duckTimer) clearTimeout(this._duckTimer);
        if (ms > 0) this._duckTimer = setTimeout(() => { this.cineDuck = 1; this._applyVolumes(); }, ms);
    }

    /** Pause the voice queue without dropping it (title cards / boss intros). */
    holdVoiceQueue(on) {
        this.voiceHold = !!on;
        if (!on) this._processVoiceQueue();
    }

    /** Looping weather bed; silent if the file is absent. */
    setAmbience(name) { … BufferSource with loop=true + gain 0.35·sfxVolume … }
```

- `_effectiveMusicVolume()` becomes
  `this.muted ? 0 : this.musicVolume * duck * (this.cineDuck ?? 1)`.
- `_processVoiceQueue()` gains `if (this.voiceHold) return;` as its first line
  (after the existing `voicePlaying` guard).
- `SFX_NAMES` becomes
  `['swing','hit','hurt','roll','death','windup','clash','boom','whoosh']`.
  Two new one-shots under `src/web/static/sfx/` (ElevenLabs sound-generation, same
  pipeline as the existing seven): `boom.mp3` — deep impact with a long tail;
  `whoosh.mp3` — short cloth/air sweep. Missing files decode to nothing and
  `playSfx` no-ops, so this is not a blocking dependency.
- `handlePlayEvent` gains:

```js
            } else if (payload.type === 'cinematic') {
                if (payload.kind === 'title_card')  { this.duckFor(0.35, 4500); this.holdVoiceQueue(true);
                                                      this.playSfx('whoosh', { volume: 0.5 });
                                                      setTimeout(() => this.holdVoiceQueue(false), 4500);
                                                      this.playStinger('epic', 9000); }
                else if (payload.kind === 'boss_intro') { this.duckFor(0.30, 3900); this.holdVoiceQueue(true);
                                                      setTimeout(() => this.holdVoiceQueue(false), 3900);
                                                      this.playStinger('epic', 10000); }
                else if (payload.kind === 'vision')  { this.duckFor(0.18); this.playSfx('whoosh', { volume: 0.35 }); }
                else if (payload.kind === 'weather') { this.setAmbience(
                                                        payload.mood === 'rain' ? 'amb_rain' :
                                                        (payload.mood === 'snow' || payload.mood === 'fog') ? 'amb_wind' : null); }
            }
```

  `boom` fires from `game.js` (it must land on the exact splash frame, not on event
  arrival). Vision's duck is released by `game.endVision()` calling `duckFor(1)`.

---

## 11. Screen-safety notes

- **The chat never stops.** The overlay is a child of `#game-container`, so the chat
  panel, its rAF-batched token rendering and its scroll position are untouched — a
  title card plays *over the canvas* while the DM's prose keeps streaming below it.
  This is deliberate: the narration is the content of the moment.
- **No JS animation loops for DOM.** Bars, text, scrim and vignette animate via CSS
  transitions driven by class flips. The only per-frame JS work is inside the Pixi
  ticker, which already runs. A cinematic adds one tween evaluation per frame.
- **`prefers-reduced-motion`** shortens the DOM transitions; the canvas zoom targets
  are small (≤1.45) and time-limited.
- **Never two at once.** `playCinematic` ends any running cinematic before starting
  the next; the server-side `too_soon` limit (10-45 s) means this should never
  actually happen.
- **Death always wins.** The death overlay is z-index 2000 and `position: fixed`;
  `applyPlayerState` → `showDeathScreen()` is unaffected by an active cinematic, and
  `loadLocation`/`destroy` both tear cinematics down.
- **Cache-bust**: bump `?v=` on **both** `game.js` and `audio.js` in `index.html`.
  Stale JS with a new SSE event type is a silent no-op — the worst kind of bug.

---

## 12. Build checklist (implementation order)

1. `src/models/world_state.py`: add `Cinematic` model + `DMState.weather`; export
   `Cinematic` from `src/models/__init__.py`. Verify an existing world DB
   (`data/emberfall.db`) still opens: `uv run main.py play --db data/emberfall.db`.
2. `src/models/npc.py`: add `boss_title`.
3. `src/services/cinematics.py`: vocabulary tables, `request_cinematic`,
   `drain_pending`, `is_boss`, `boss_intro_payload`, `recent`. Unit-testable with no
   web layer.
4. `src/tools/world_write/cinematics.py`: `play_cinematic`; export from
   `world_write/__init__.py`; register in `DM_TOOLS`.
5. `src/agents/dm_orchestrator.py`: add the CINEMATIC MOMENTS prompt section.
   `tests/test_dm_prompt_tools.py`: add `"play_"` to `TOOL_PREFIXES`. Run pytest —
   the sync test must pass.
6. `src/web/streaming.py`: `_push_cinematic` + the two call sites.
7. `src/services/combat.py`: `boss`/`boss_title` on enemy dicts; attach
   `result["cinematic"]` for boss rosters.
8. `src/web/server.py`: `drain_pending` in `_run_turn`; `weather` in
   `build_player_state` (with the indoor suppression); `GET /api/cinematics/recent`.
9. `src/web/turn_audit.py`: `cinematic_text_in_narration` check.
10. `src/services/music_generator.py`: `epic` mood.
11. `static/js/audio.js`: `cineDuck`, `voiceHold`, `duckFor`, `holdVoiceQueue`,
    `setAmbience`, `epic` fallback chain, new SFX names, `handlePlayEvent` branch.
12. `static/index.html`: overlay markup + CSS + SSE branch + `endVision` in
    `finally` + weather in `applyPlayerState`.
13. `static/js/game.js`: constructor state → `init` (weatherLayer + DOM refs) →
    input skip hooks → `update`/`updateCamera`/`_updateSceneTint` rework →
    `playCinematic` / `_updateCinematic` / `_endCinematic` / `endVision` →
    weather system → `startCombat` deferral → `_startKillCam`.
14. **Bump `?v=` on both script tags.**
15. Generate `boom.mp3` / `whoosh.mp3` (optional `amb_rain.mp3`, `amb_wind.mp3`)
    into `src/web/static/sfx/`.
16. Manual pass: `uv run main.py web` → set an NPC's `boss_title` via the World
    Viewer → provoke a fight → intro, fight, kill cam. Then ask the DM for a
    chapter break twice in a row and confirm the second is rejected and the
    narration says nothing about it.

---

## 13. Tests (`tests/test_cinematics.py` unless noted)

| test | asserts |
|---|---|
| `test_vocabulary_and_duration_tables_agree` | every `VOCABULARY` kind has a `DURATIONS_MS` entry and every `LIMITS` key is DM-callable |
| `test_play_cinematic_returns_event_payload` | `play_cinematic(player, "title_card", title="THE ROAD NORTH")` returns `event == "cinematic"`, echoes title/subtitle, carries `duration_ms == 4200` |
| `test_rejected_result_has_no_event_key` | a second `title_card` the same game day returns `success False`, `reason == "daily_cap"`, and **no** `"event"` key (nothing can reach the client) |
| `test_tracker_converts_event_result_to_sse` | feeding `play_cinematic`'s dict through `ToolUsageTracker` yields one `{"type":"cinematic","kind":"title_card",…}` notification |
| `test_tracker_converts_nested_cinematic_key` | a tool result `{"event":"combat_start","enemies":[…],"cinematic":{…}}` yields **two** notifications, `cinematic` **before** `combat_start` |
| `test_title_card_game_time_cooldown` | two cards 4 in-game hours apart → second rejected `cooldown_game_time`; advancing the clock 13 h lets a *differently titled* card through |
| `test_duplicate_title_is_rejected` | the same `title` twice, days apart → `duplicate` |
| `test_boss_intro_fires_once_per_boss` | first `boss_intro` for an npc ok; second (any later day) → `boss_already_introduced` |
| `test_kill_cam_is_not_dm_callable` | `play_cinematic(..., "kill_cam")` → `not_dm_callable`; `request_cinematic("kill_cam", dm_callable_only=False)` is allowed |
| `test_unknown_kind_rejected` | `"explosion"` → `unknown_kind` |
| `test_weather_label_menu_enforced` | `mood="apocalypse"` → `bad_weather_label`; `"rain"` accepted |
| `test_weather_sets_dm_state_and_clear_nulls_it` | accepted `rain` writes `DMState.weather == "rain"`; `clear` writes `None` |
| `test_weather_same_label_twice_is_duplicate` | setting `rain` while it is raining → `duplicate`, DMState unchanged |
| `test_real_time_floor_blocks_two_in_one_turn` | two accepted-eligible kinds back to back → second `too_soon` |
| `test_session_cap_over_rolling_window` | N+1 plays of a kind inside the 60-min window → `session_cap` |
| `test_every_call_writes_a_ledger_row` | accepted and rejected calls both insert exactly one `cinematics` row with the right `status`/`reject_reason` |
| `test_pending_queue_drains_once` | `request_cinematic(defer=True)` → `drain_pending` returns it once, second call returns `[]`, row is `played` |
| `test_combat_start_attaches_boss_intro` | `build_combat_start` with a `boss_title` NPC returns a `cinematic` key with `kind == "boss_intro"`, `title == npc.name`, `subtitle == npc.boss_title` |
| `test_combat_start_without_boss_has_no_cinematic` | ordinary NPC roster → no `cinematic` key; enemies carry `boss: False` |
| `test_combat_start_boss_second_time_has_no_cinematic` | after one intro, a rematch roster carries no `cinematic` (fight still starts normally) |
| `test_existing_world_defaults` (`tests/test_playtest_seed.py`) | a seeded world has zero bosses, `DMState.weather is None`, and `build_player_state` reports `weather == "clear"` |
| `test_state_event_carries_weather_and_suppresses_indoors` | with `DMState.weather = "rain"`, `build_player_state` returns `"rain"` outdoors and `"clear"` when the player's location type is `interior`/`room`/`building` |
| `test_audit_flags_printed_title_card` (`tests/test_turn_filters.py`) | `audit_turn("ACT II — THE LONG DARK\n\nYou walk on.", [], …)` yields `cinematic_text_in_narration` |
| `test_audit_ignores_ordinary_prose` (`tests/test_turn_filters.py`) | "The act of climbing takes an hour; the fog cuts the light to a chapter of grey." → no cinematic warning |
| `test_every_prompt_tool_reference_is_registered` (existing, extended) | with `"play_"` added to `TOOL_PREFIXES`, the prompt's `play_cinematic` resolves to a registered tool |

---

## 14. Scope

### v1 — build now
The five kinds above, the `cinematics` ledger/queue, `play_cinematic`, the boss
auto-intro + kill cam, weather as world state, the `epic` mood, the audio ducking /
voice-hold plumbing, the one audit check, and the DM prompt section.

That is the smallest thing that delivers the player-facing point: **a handful of
moments per session where the game stops being a chat window and becomes a film.**

### later — explicitly out of v1
- `setpiece` as a composite (letterbox + slow push-in + weather burst + vignette),
  once the primitives have been played with.
- Arrival title cards on first entry to a major location (`move_player` attaching a
  `cinematic` key when `Location.first_visit` and a location importance flag exists).
- Per-location authored weather defaults (`Location.default_weather`) so a swamp is
  always foggy without the DM saying so.
- Boss health bar / name banner *during* the fight (belongs with the tier system).
- A `cinematic` mood-track per world beyond `epic` (e.g. `dread` for visions).
- Screen-space post-FX (color grading, chromatic aberration) — needs Pixi filters and
  a perf budget study.
- Replay of the last cinematic from a codex/journal screen.

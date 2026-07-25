# Canvas-first play: doors, objects, and a living world between turns

**Status:** design spec, implementation-ready
**Owner system:** canvas / real-time exploration
**Precedent this extends:** real-time combat (`src/services/combat.py`, `POST /api/combat/result`) — canvas resolves, code enforces, DM narrates the aftermath.

---

## 0. The one sentence

Walking through an unlocked door, picking up a crate, reading a notice, and hearing the market mutter must all happen **in under 100 ms with zero LLM calls**; the DM finds out on its next turn from a context block and never re-litigates it.

Today the loop is `type → wait 10–35 s → read`. After this spec the loop is `play → occasionally type → wait → read`. The wait stops being dead air because there is something to do inside it.

---

## 1. The line: what code resolves vs. what the DM resolves

This is the governing rule of the whole spec. **Draw it once, never blur it.**

### Code path (REST, no LLM, no DM turn)

| Action | Why code can own it |
|---|---|
| Walking inside a scene | Already code-owned today |
| Stepping through a **discovered** connection with **no unmet requirements** | Pure graph traversal + a fixed time cost. Nothing to judge. |
| `take` an authored object | The world author already decided it was free to take |
| `read` an authored object | Text was authored at generation time |
| `open` an authored object **with no `lock`** | Same |
| `search` an authored object | Same |
| Ambient barks | Pre-generated one-liners, no state, no memory |
| NPC wandering | Already client-only today |

### Chat path (DM turn, unchanged)

| Action | Why the DM must own it |
|---|---|
| **Every spoken line, always** | `speak` / `prompt_npc_agent` own dialogue, voice, portrait, memory |
| A door whose `Connection.requirements` are unmet | Whether the player talks/bribes/forces their way through is fiction |
| A destination that is not discovered / does not exist ("is there a back door?") | Lazy generation via `prompt_creator_agent` |
| An `open` object carrying a `lock` hint | Same as a locked door |
| Anything the player *types* | If they typed it, they want a story answer |
| Combat initiation | `start_combat` is a DM decision |
| Risky / contested acts — stealing in view of an NPC, prying a lock, climbing, lying | Needs the SUCCESS & FAILURE rubric |
| Using an item on the world | Effects are fiction |
| Quest state | Already DM-owned |

**The authoring rule that keeps this line clean:** *an object placed on the canvas is a thing nobody minds you touching.* Anything owned, guarded, sacred, or dangerous is either not an object at all, or is an `open` with a `lock` (which routes to chat). This is written into the creator agent's prompt (§8).

**Corollary the DM must be told:** the DM never narrates the player opening a door or pocketing a crate — the player already watched themselves do it. See the prompt draft in §7.

---

## 2. Invariant compliance

| Invariant | How this spec obeys it |
|---|---|
| Code enforces, LLM narrates | Every number here (travel minutes, door radius, bark cadence, item stacking) is in Python/JS constants. The creator agent picks **one word** from `take / read / open / search` and writes flavour text. Never a number, never an effect. |
| Latency is the enemy | Three new REST endpoints, all pure DB work. Zero LLM calls on any fast path. The only LLM call added anywhere is the bark pool, which is generated **in a background task** and served from a DB column. |
| SQLite, no migrations | Three new **nullable** JSON columns on `locations`. `_migrate_missing_columns` in `src/models/base.py` adds them with `DEFAULT NULL` on first `init_db`. Worlds with all three NULL behave exactly as today: no doors highlighted... no, doors still work (they're derived, not stored), no objects, no barks. |
| Single-file SPA + game.js + audio.js | All frontend work is in `game.js` and `index.html`. `audio.js` is untouched (barks are silent in v1). Cache-buster bumped on both script tags. |
| Structured event flow | No new SSE event types. The REST responses embed the **exact same `state` dict** that the `state` SSE event carries, and the frontend feeds them to the existing `applyPlayerState()`. Reuse, don't invent. |
| Reality audit must not false-positive | §9 — verified failure case identified and fixed with a `code_actions` union. |
| `test_dm_prompt_tools.py` | No new DM tools. The prompt additions only backtick tools already in `DM_TOOLS`. |

---

## 3. Schema deltas

All three on `locations`, all `nullable=True`, all default `NULL`.

`src/models/location.py`, inside `class Location`, immediately after `obstacles`:

```python
    # Canvas interactables: list of dicts, see src/services/scene_objects.py
    # {"id","kind","label","x","y","payload","consumed"} — normalized 0-100 coords
    objects: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Pre-generated ambient one-liners for this scene (no TTS, no DM)
    # [{"npc_id": str|None, "text": str}, ...]
    ambient_barks: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # {"signature": str, "day": int, "hour": int} — staleness bookkeeping
    ambient_barks_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
```

**Nothing else changes in the schema.** Door positions are *derived* (§5.3), not stored — so every existing world gets doors for free on the next page load.

### Config deltas

`src/config.py`, `class GameConfig`:

```python
    barks_enabled: bool = True
    bark_pool_size: int = 10
    bark_refresh_hours: int = 12       # in-game hours before a pool goes stale
```

`config/settings.yaml`, under `game:`:

```yaml
  barks_enabled: true
  bark_pool_size: 10
  bark_refresh_hours: 12
```

`config/agents.yaml`, new entry (required — `get_agent_config` raises `KeyError` on unknown names):

```yaml
  # Ambient scene chatter — one background call per location, cached in the DB
  bark:
    model: "openrouter/deepseek/deepseek-v3.2"
    temperature: 0.9
    max_tokens: 600
    description: "Generates a pool of ambient one-liners for a location"
```

---

## 4. New server modules

### 4.1 `src/services/travel.py` (new)

Single source of truth for "can the player walk from A to B", extracted out of `move_player` so the canvas and the DM tool cannot drift.

```python
def check_route(session, from_location_id: str | None, destination_id: str) -> dict:
    """Return {"travel_time_hours": float, "blocked": bool, "requirements": list[str]}.

    Implements exactly the two rules move_player already enforces:
      1. A direct Connection (either direction, honouring `bidirectional`)
         with non-empty `requirements` blocks.
      2. With no direct Connection, if EVERY authored edge whose
         to_location_id == destination_id carries requirements, the
         destination is gate-locked on any approach path.
    Travel time: connection.travel_time_hours; 0.1 for a parent/child
    hierarchy hop; 0.5 default otherwise.
    """
```

`move_player` in `src/tools/world_write/player.py` is refactored to call `check_route` and keep its current return shape byte-for-byte (`blocked` / `success` dicts, same `message` strings). `tests/test_travel_gate.py` must pass unchanged — it is the regression net for this refactor.

```python
def travel_now(player_id: str, destination_id: str) -> dict:
    """Canvas travel: validate, move, advance the clock. No LLM.

    Returns one of:
      {"success": True, "from_id","from_name","destination_id","destination_name",
       "minutes": int, "first_visit": bool}
      {"blocked": True, "requirements": [...], "destination_name": str}
      {"error": "unreachable"|"not_found"|"no_player"}
    Steps on success:
      1. reachable check: destination_id must appear in
         get_available_destinations(player.current_location_id)
      2. check_route -> blocked? return blocked
      3. first_visit = not destination.visited
      4. move_player(player_id, destination_id)   # reuses the tool, one txn
      5. advance_time(travel_time_hours, reason="canvas travel")
    """
```

`minutes = round(travel_time_hours * 60)`.

**Why `travel_now` calls the `@tool` functions directly:** Strands `@tool`-decorated functions stay plainly callable (see `tests/test_travel_gate.py`, which calls `move_player(...)` and reads the dict). One code path, one behaviour.

### 4.2 `src/services/scene_objects.py` (new)

```python
OBJECT_KINDS = ("take", "read", "open", "search")   # FIXED MENU. Never extend at runtime.

# Objects are consumed by take/open/search; `read` is re-readable forever.
CONSUMING_KINDS = ("take", "open", "search")

MAX_OBJECTS_PER_LOCATION = 3
LABEL_MAX = 48
TEXT_MAX = 400

def normalize_objects(raw) -> list[dict]:
    """Coerce whatever an LLM produced into valid object rows. Never raises.

    - Drops entries that are not dicts, or whose `kind` is not in OBJECT_KINDS.
    - Assigns `id` when missing: slug(label) + "_" + i, deduped.
    - Clamps x,y into [10.0, 90.0]; defaults to (50, 65) when missing/NaN.
    - Truncates label to LABEL_MAX, payload["text"] to TEXT_MAX.
    - Strips payload["item"]["effects"] and any key not in
      {name,type,description,value,quantity}; forces value>=0, quantity 1..5;
      forces type into {consumable,weapon,armor,quest_item,misc} else "misc".
    - Drops payload["lock"] unless kind == "open"; coerces it to a str.
    - Forces consumed=False on any object that lacks the key.
    - Caps the list at MAX_OBJECTS_PER_LOCATION.
    """

def public_objects(raw) -> list[dict]:
    """What the client is allowed to see: id, kind, label, x, y, consumed,
    plus `locked: bool` for kind=="open". Payload text/items are NEVER sent
    to the client before interaction — the reveal happens in the response."""

def resolve_interaction(player_id: str, location_id: str, object_id: str) -> dict:
    """Apply an interaction in code. Returns the /api/interact body (§6.2)."""
```

`resolve_interaction` behaviour, per kind:

| kind | Precondition | Effect | Response `result` |
|---|---|---|---|
| `take` | not consumed | `add_to_inventory(player_id, **payload.item)` | `floater: "+ {item.name}"`, `items_gained:[…]`, `consumed: true` |
| `read` | — (never consumes) | none | `panel_title: label`, `panel_text: payload.text`, `consumed: false` |
| `open` | not consumed; `payload.lock` falsy | `add_to_inventory` if `payload.item` | `floater`, `panel_text` if authored, `consumed: true` |
| `open` locked | — | **none** | HTTP 409 `{"locked": true, "lock_hint": …, "chat_prompt": "I try to open the {label}."}` |
| `search` | not consumed | `add_to_inventory` if `payload.item` | `panel_text: payload.text`, `floater` if item, `consumed: true` |

Consumption is written back with `session.commit()` on a **copied** list (the JSON column must be reassigned, not mutated in place — same trap `_normalize_inventory` documents). Already-consumed → HTTP 409 `{"spent": true}`. Unknown `object_id` → HTTP 404.

### 4.3 `src/services/scene_exits.py` (new)

Derives door positions. Deterministic, stateless, no DB writes.

```python
DOOR_SLOTS = [                       # normalized scene coords, on the rim
    ("north",      50.0,  8.0), ("northeast", 84.0, 14.0),
    ("east",       92.0, 50.0), ("southeast", 84.0, 86.0),
    ("south",      50.0, 92.0), ("southwest", 16.0, 86.0),
    ("west",        8.0, 50.0), ("northwest", 16.0, 14.0),
]
MAX_DOORS = 8

def compute_exit_doors(location, destinations: list[dict]) -> dict[str, dict]:
    """exit_id -> {"x": float, "y": float, "slot": str}.

    Preference, in order:
      travel_type == "enter"  (a child location)  -> "north"
      travel_type == "exit"   (the parent)        -> "south"
      otherwise: compass bearing from
          (dest.position_x - location.position_x, dest.position_y - location.position_y)
          bucketed into the 8 slots (y grows southward on the world map).
          Both deltas |d| < 1.0 -> no preference, start at "north".
    Assignment: sort destinations by (preferred_slot_index, exit_id) and walk
    DOOR_SLOTS forward from each preference, taking the first free slot.
    Coordinates are then clamped inside walkable_bounds with a 2.0 margin.
    Destinations past MAX_DOORS get no door (still reachable via the
    edge-push chooser).
    """
```

Sorting by id keeps the layout **stable across reloads** — a door does not move when the player comes back.

### 4.4 `src/services/barks.py` (new)

```python
BARK_PROMPT = """You write one-line ambient chatter for a living RPG scene.

You are given a place and the people standing in it. Write EXACTLY {n} lines
these people could say out loud, unprompted, to nobody in particular —
overheard fragments, not conversation. Each line is at most 14 words.

Rules:
- Never address the player. No greetings, no questions aimed at them.
- No plot, no quest hooks, no prophecy, no numbers, no stage directions.
- No names of people who are not in the list below.
- Ground every line in THIS place: its trade, its weather, its smells, its
  grievances, its gossip, its complaints about the neighbours.
- Match the world's tone exactly.
- Vary the speakers. If one of the listed NPCs would say it, use their exact
  name. Otherwise use "ambient".

Return ONLY a JSON array. No prose, no code fences, no commentary:
[{"speaker": "<exact NPC name or 'ambient'>", "text": "<the line>"}]
"""

def bark_signature(location_id: str) -> str:
    """sha1 of location_id + current_state + sorted alive NPC ids here."""

def barks_stale(location, clock) -> bool:
    """True when: ambient_barks is falsy, OR meta["signature"] != current
    signature, OR the in-game hours elapsed since meta["day"/"hour"]
    exceed settings.game.bark_refresh_hours."""

async def generate_barks(location_id: str) -> list[dict]:
    """ONE fast-model call. Builds the request from the WorldBible tone +
    the location's name/description/atmosphere_tags + the alive NPCs here
    (name, profession, mood). Parses defensively:
      1. strip ``` fences
      2. json.loads; on failure, slice from the first '[' to the last ']'
      3. drop entries without a non-empty `text`
      4. truncate text to 120 chars, cap the list at bark_pool_size + 2
      5. resolve `speaker` case-insensitively against present NPC names ->
         npc_id; anything unmatched (including "ambient") -> npc_id None
    Writes ambient_barks + ambient_barks_meta and commits. On ANY exception
    logs a warning and returns [] — barks are decoration, never an error."""
```

Generation is always dispatched through the existing dedupe plumbing:
`AssetManager.spawn_generation(f"barks:{location_id}", lambda: generate_barks(location_id))`, which already handles in-flight dedupe and a 300-second failure cooldown.

### 4.5 `src/web/pending_actions.py` (new)

The ledger that tells the DM what happened while it wasn't looking. Mirrors the existing `_pending_audit_feedback` pattern in `server.py` (module-level dict, in-memory, popped at context-build time).

```python
MAX_PENDING = 12

# player_id -> list of {"kind","summary","tools","first_visit"?}
_pending: dict[str, list[dict]] = {}

def record_action(player_id: str, kind: str, summary: str,
                  tools: Iterable[str] = (), **extra) -> None:
    """kind in {"move","take","read","open","search"}. Appends; when the list
    exceeds MAX_PENDING the OLDEST entry is dropped and a sticky
    {"kind":"omitted"} marker is kept at the head."""

def drain(player_id: str) -> list[dict]:
    """Pop and return everything queued for this player."""

def tool_equivalents(actions: list[dict]) -> set[str]:
    """Tool names the ENGINE already ran on the player's behalf. Fed to the
    turn audit so it does not flag the DM for narrating them.
      move                      -> {"move_player", "advance_time"}
      take/open/search w/ items -> {"add_to_inventory"}
      read                      -> set()
    """

def format_pending_actions(actions: list[dict]) -> str:
    """The context block. Empty string when there is nothing to report."""
```

`format_pending_actions` output — **exact template**:

```
[SINCE YOUR LAST TURN the player acted on the canvas. The engine ALREADY
applied every line below to the database — the moves, the time, the items.
Do NOT call move_player, advance_time, or add_to_inventory for any of it,
and do NOT narrate them doing it; they watched themselves do it.
- Travelled: Market Square -> Dock Row -> The Salt Tavern (18 min passed)
- Picked up: bundle of oilcloth
- Read: notice nailed by the door ("Bounty stands at the pier. Ask for Vell.")
- Opened: fish crate (found salted eels)
- Searched: straw bedding (found nothing but a broken comb)
They are now in The Salt Tavern, which they have never been described.
Call describe_location for it before anything else.]
```

Rules baked into the formatter:
- Consecutive `move` entries collapse into one arrow chain with the summed minutes.
- The trailing "which they have never been described / call describe_location" sentence appears **only** when the last move carried `first_visit=True`.
- When the head marker `{"kind":"omitted"}` is present, the first bullet is `- (earlier canvas actions omitted)`.
- No backticked tool names (keeps `test_dm_prompt_tools.py`'s regex out of a runtime string; the names appear as bare words).

---

## 5. Changes to existing server code

### 5.1 `src/web/server.py` — `/api/play` context build

Inside `_run_turn()`, immediately after `context = dm._build_context(request.player_input)`:

```python
# Canvas actions the engine resolved since the last turn — the DM learns
# about doors, pickups, and reads here, never through an LLM round-trip
from src.web import pending_actions
canvas_actions = pending_actions.drain(request.player_id)
canvas_block = pending_actions.format_pending_actions(canvas_actions)
if canvas_block:
    context += "\n\n" + canvas_block
code_actions = pending_actions.tool_equivalents(canvas_actions)
```

Order is deliberate: **canvas block first** (it is factual world state), the existing NPC-addressing line next, the `[REALITY AUDIT]` block **last** (it is a correction and must be the most salient thing in the window).

The audit call at the end of the turn becomes:

```python
audit_warnings = audit_turn(
    "".join(turn_text_parts), tool_tracker.snapshot(),
    state_before, state_after, code_actions=code_actions,
)
```

**Draining semantics:** popped at context-build time, under `_play_lock`. If the turn then crashes, the entries are lost — identical to `_pending_audit_feedback` today. Accepted; canvas actions are already in the database, so the only cost is the DM being slightly less informed on one turn.

### 5.2 `src/web/server.py` — enrich `/api/assets/location/{id}`

The `exits` block (currently ~line 869) becomes:

```python
from src.services.travel import check_route
from src.services.scene_exits import compute_exit_doors
from src.services.scene_objects import public_objects

destinations = get_available_destinations(location_id) or []
with get_session() as db:
    loc = db.get(Location, location_id)
    doors = compute_exit_doors(loc, destinations)
    exits = []
    seen = set()
    for d in destinations:
        if not isinstance(d, dict) or not d.get("id") or d["id"] in seen:
            continue
        seen.add(d["id"])
        route = check_route(db, location_id, d["id"])
        exits.append({
            "id": d["id"],
            "name": d.get("name"),
            "type": d.get("type", ""),
            "travel_type": d.get("travel_type", ""),
            "travel_time_hours": route["travel_time_hours"],
            "locked": route["blocked"],
            "requirements": route["requirements"],
            "door": doors.get(d["id"]),          # None when past MAX_DOORS
        })
```

`AssetManager._load_scene_data` gains one line next to `"obstacles"`:

```python
"objects": public_objects(location.objects),
```

and `get_location_assets_fast` passes `"objects": scene["objects"]` through. The endpoint returns it as a top-level `objects` key.

Full enriched response (added keys marked **NEW**):

```json
{
  "location_id": "...", "location_name": "Market Square",
  "background_url": "/assets/...", "walkable_bounds": {...},
  "obstacles": [[[x,y],...]],
  "exits": [
    {"id": "loc-tavern", "name": "The Salt Tavern", "type": "building",
     "travel_type": "enter", "travel_time_hours": 0.1,
     "locked": false, "requirements": [],
     "door": {"x": 50.0, "y": 8.0, "slot": "north"}}
  ],
  "objects": [                                                  // NEW
    {"id": "oilcloth_1", "kind": "take", "label": "Bundle of oilcloth",
     "x": 31.0, "y": 68.0, "consumed": false, "locked": false}
  ],
  "pending": false,
  "player": {...}, "npcs": [...]
}
```

Note `objects[].locked` is present only as a rendering hint for `open`; the payload (item, text, lock reason) is **never** sent before interaction — that is the reveal.

---

## 6. REST endpoints (exact)

### 6.1 `POST /api/travel`

```python
class TravelRequest(BaseModel):
    player_id: str
    destination_id: str
    source: str = "door"        # "door" | "chooser" — logging only
```

**200 success**

```json
{"success": true,
 "from_id": "loc-square", "from_name": "Market Square",
 "destination_id": "loc-tavern", "destination_name": "The Salt Tavern",
 "minutes": 6, "first_visit": true,
 "state": { /* exactly build_player_state(player_id) */ }}
```

**409 blocked** (route requirements unmet — hand off to the DM)

```json
{"success": false, "blocked": true,
 "destination_name": "Ash Hollow",
 "requirements": ["gate pass"],
 "chat_prompt": "I try to get through to Ash Hollow."}
```

**409 busy** (a DM turn is streaming — `_play_lock.locked()`)

```json
{"success": false, "busy": true}
```

**403** `{"success": false, "error": "unreachable"}` — destination is not in the current location's `get_available_destinations`. This is the anti-teleport guard; the client is not trusted.
**404** `{"success": false, "error": "not_found"}`.

Handler body:

```python
@app.post("/api/travel")
async def canvas_travel(request: TravelRequest):
    init_db(get_active_db_path())
    if _play_lock.locked():
        return JSONResponse({"success": False, "busy": True}, status_code=409)
    result = await asyncio.to_thread(travel_now, request.player_id, request.destination_id)
    if result.get("blocked"):
        result["chat_prompt"] = f"I try to get through to {result['destination_name']}."
        return JSONResponse({"success": False, **result}, status_code=409)
    if "error" in result:
        return JSONResponse({"success": False, **result},
                            status_code=404 if result["error"] == "not_found" else 403)
    pending_actions.record_action(
        request.player_id, "move",
        f"{result['from_name']} -> {result['destination_name']}",
        tools=("move_player", "advance_time"),
        minutes=result["minutes"], first_visit=result["first_visit"],
    )
    return {"success": True, **result, "state": build_player_state(request.player_id)}
```

**Why travel is blocked during a streaming turn but interaction is not:** a mid-turn location change desyncs the DM's own `get_current_location` reads and races the frontend's scene load. A mid-turn pickup is transactional and harmless — and *making the 10–35 second wait playable is the entire point of this spec*, so we take the small narrative fuzz.

### 6.2 `POST /api/interact`

```python
class InteractRequest(BaseModel):
    player_id: str
    location_id: str
    object_id: str
```

**200 success**

```json
{"success": true,
 "object_id": "oilcloth_1", "kind": "take", "label": "Bundle of oilcloth",
 "consumed": true,
 "result": {
   "floater": "+ Bundle of oilcloth",
   "panel_title": null,
   "panel_text": null,
   "items_gained": [{"name": "Bundle of oilcloth", "quantity": 1}]
 },
 "state": { /* build_player_state(player_id) */ }}
```

A `read` returns `consumed: false`, `floater: null`, `panel_title: "<label>"`, `panel_text: "<authored text>"`, `items_gained: []`.

**409 locked** → chat handoff

```json
{"success": false, "locked": true, "label": "Iron strongbox",
 "lock_hint": "a small brass key",
 "chat_prompt": "I try to open the iron strongbox."}
```

**409 spent** `{"success": false, "spent": true, "label": "Fish crate"}`
**404** `{"success": false, "error": "no such object"}`
**409 wrong-room** `{"success": false, "error": "not here"}` when `location_id != player.current_location_id`.

On success: `pending_actions.record_action(player_id, kind, summary, tools=("add_to_inventory",) if items else ())` where `summary` is e.g. `Bundle of oilcloth`, or for `read`, `notice nailed by the door ("Bounty stands at the pier…")` (text truncated to 90 chars).

### 6.3 `GET /api/barks/{location_id}`

```json
{"location_id": "loc-square",
 "barks": [{"npc_id": "npc-piper", "text": "Third gull this week. They know when the boats are late."},
           {"npc_id": null, "text": "Salt in the wind means salt in the bread."}],
 "pending": false,
 "unavailable": false,
 "refresh_after_ms": 180000}
```

Handler: `init_db`; load the location; if `not settings.game.barks_enabled` → `{"barks": [], "unavailable": true, "pending": false}`. If `barks_stale(loc, clock)` → `pending = get_asset_manager().spawn_generation(f"barks:{location_id}", ...)`. Always return whatever is currently cached (a stale pool is far better than silence while the new one generates). `refresh_after_ms` is `180000` when not pending, `20000` when pending — the client polls once more and then stops.

---

## 7. SSE events

**No new SSE event types in v1.** This is deliberate: every canvas action is a request the client itself initiated, so a push channel adds nothing. The endpoints return the payload the client needs synchronously.

What is reused, verbatim: the `state` dict from `build_player_state(player_id)` — the same shape the `state` SSE event carries — embedded under the `state` key in `/api/travel` and `/api/interact` responses. The client feeds it to the existing `applyPlayerState()`, which already:
- diffs inventory/currency/health and spawns canvas floaters (`spawnStateFloaters`),
- updates the HUD chips,
- sets the day/night tint from `time_of_day` (so travel time visibly moves the light),
- publishes `window.lastPlayerState` for combat HP.

That means **the "+ Bundle of oilcloth" floater is free** — the state diff produces it with no new code.

### DM prompt addition

New section in `DM_SYSTEM_PROMPT` (`src/agents/dm_orchestrator.py`), inserted immediately after **THE REALITY CONTRACT**, before **NARRATIVE DIRECTOR**:

```
### THE CANVAS PLAYS ITSELF (never narrate what the engine already did)

Between your turns the player is not waiting. They walk the scene, step
through unlocked doors, and pick up, read, open, and search the small things
placed in the world. All of that resolves in code: the database is already
updated, the clock has already moved, items are already in their pack. You
find out from a "SINCE YOUR LAST TURN" block at the top of your context.

When that block is present:
- Treat every line as TRUE and ALREADY APPLIED. Do NOT call `move_player`,
  `advance_time`, or `add_to_inventory` for anything it lists. Calling them
  again double-charges the world.
- Do NOT re-narrate the journey ("you make your way to the tavern") or the
  act ("you crouch and pocket the oilcloth"). The player watched themselves
  do it; describing it again reads as a stutter.
- Open on what is NEW: who looks up when they walk in, what the room is
  doing, what the thing they just read implies, who wants something.
- If it says they arrived somewhere never described, call
  `describe_location` for it first.
- If nothing in the block deserves a reaction, ignore it and answer what the
  player actually typed.

What remains yours, always: every spoken line, every locked door, every
risky or contested act, every fight. When a requirement stops the player —
a gate pass, a key, a guard's say-so — the engine hands them to you. Decide
whether they get through, and make it cost something.
```

`move_player`, `advance_time`, `add_to_inventory`, and `describe_location` are all already in `DM_TOOLS`, so `tests/test_dm_prompt_tools.py` stays green.

---

## 8. Creator agent: authoring objects

### Tool signature changes

`src/tools/world_write/locations.py` — `add_location` gains a trailing parameter:

```python
def add_location(
    ...,
    discovered: bool = False,
    objects: list[dict] | None = None,   # NEW
) -> dict[str, Any]:
    """...
    Args:
        ...
        objects: 0-3 canvas interactables. Each is
            {"kind": "take"|"read"|"open"|"search", "label": str,
             "x": 0-100, "y": 0-100, "payload": {...}}.
            See the SCENE OBJECTS section of your instructions for the exact
            payload per kind. Invalid entries are dropped by the engine.
    """
```

Implementation: `location.objects = normalize_objects(objects) or None`.

`update_location` gains the same `objects: list[dict] | None = None` parameter with identical handling (so existing locations can be retrofitted). The `LocationUpdate` pydantic model in `server.py` gains `objects: list | None = None`, and `PUT /api/world/locations/{id}` gains:

```python
if update.objects is not None:
    loc.objects = normalize_objects(update.objects) or None
```

`GET /api/world/locations/{id}` returns `"objects": loc.objects or []` so the world editor can see them.

### Creator prompt addition

New section in `CREATOR_SYSTEM_PROMPT` (`src/agents/creation_agent.py`), after **LOCATION TYPES**:

```
## SCENE OBJECTS (things the player can touch without asking)

Every location you create SHOULD carry 0-3 objects. Pass them as the
`objects` argument to `add_location` (or `update_location`). They are the
difference between a painted backdrop and a place.

Each object is exactly:
{"kind": "<take|read|open|search>",
 "label": "<3-5 words, shown on screen>",
 "x": <0-100>, "y": <0-100>,
 "payload": {...}}

The kind menu — pick ONE of these four words. Never invent a fifth:
- "take"   a loose thing lying there for the taking.
           payload: {"item": {"name": "...", "type": "consumable|weapon|armor|quest_item|misc",
                              "description": "...", "value": <int>}}
- "read"   words in the world: a notice, a grave marker, a torn page.
           payload: {"text": "<the words themselves, 1-3 sentences>"}
- "open"   a container. Add "lock" ONLY if it should resist the player.
           payload: {"item": {...}, "lock": "<what it needs, e.g. 'a small brass key'>"}
- "search" a place to rummage: a woodpile, a bunk, a rubbish heap.
           payload: {"text": "<what they turn up>", "item": {...}}   (item optional)

Rules:
1. x/y are positions in the SCENE IMAGE, 0-100, where y=0 is the far
   background and y=100 is the front edge. Keep both between 15 and 85, and
   keep objects apart from each other.
2. Only author things NOBODY MINDS the player touching. Anything owned,
   guarded, sacred, or dangerous is not an object — leave it to the DM, or
   make it an "open" with a "lock" so the DM handles the attempt.
3. Never hand out quest-critical items, money, or weapons of consequence
   this way. Flavour, supplies, and lore only.
4. Write no numbers except the item's `value`. No effects, no stats, no
   damage, no healing amounts — the engine owns all of that.
5. "read" objects are the cheapest worldbuilding you will ever write. Use
   them: a price list tells the player more about a town than a paragraph.
```

Rule 4 keeps the code-enforces invariant intact; `normalize_objects` strips `effects` regardless of what the model writes.

---

## 9. Turn audit adaptation

### The verified false positive

`state_before` is built at the **start** of a DM turn (`server.py`, `_run_turn`). Canvas actions commit **between** turns. Therefore, after a canvas move:

- `state_before["location_id"] == state_after["location_id"] == <new location>` → `location_changed` is **False**
- `"move_player" not in succeeded` (the DM did not call it)
- The DM writes "You step into the tavern's heat" in the first line of the next turn

→ `arrival_without_move` fires. Same mechanic for `item_without_tool` after a canvas `take` (inventory changed before the snapshot).

Both would fire **every turn after a canvas action**, which is exactly the "false alarm every turn trains everyone to ignore the audit" failure the module header warns about.

### The fix

`src/web/turn_audit.py`:

```python
def audit_turn(
    turn_text: str,
    tools: list[dict[str, Any]],
    state_before: dict[str, Any] | None,
    state_after: dict[str, Any] | None,
    code_actions: set[str] | None = None,   # NEW, keyword, defaults to None
) -> list[dict[str, str]]:
    """...
    code_actions: tool names the ENGINE executed on the player's behalf
        between turns (canvas travel, canvas object pickups, combat
        results). They are unioned into the succeeded set: the DM may
        legitimately narrate their consequences without having called them.
    """
    ...
    succeeded = {
        t.get("name") for t in (tools or [])
        if t.get("status") == "success" and t.get("name")
    } | set(code_actions or ())
```

That is the entire change — every existing check reads `succeeded`, so all of them are corrected at once. The parameter is keyword-with-default, so `tests/test_turn_filters.py` and any existing caller compile unchanged.

**Retrofit for combat, same bug, same fix:** `/api/combat/result` should also record its mutations. Add, in the combat-result handler:

```python
tools = ["update_player_health"] if changes.get("health") else []
if changes.get("npc_deaths"):
    tools.append("kill_npc")
pending_actions.record_action(request.player_id, "combat",
                              f"fight resolved: {changes['outcome']}", tools=tools)
```

`format_pending_actions` skips `kind == "combat"` in its bullet list (the `[COMBAT OUTCOME]` message already covers it) but `tool_equivalents` still returns its tools. This kills a pre-existing `damage_without_tool` false positive for free.

**No new audit checks in v1.** Canvas actions produce no narration, so there is nothing new to catch.

---

## 10. Frontend

Both `<script>` tags in `index.html` (~line 1700) bump their cache-buster together:

```html
<script src="/static/js/game.js?v=20260801a"></script>
<script src="/static/js/audio.js?v=20260801a"></script>
```

### 10.1 `game.js` — constructor additions

```js
// Canvas-first exploration
this.doors = [];              // [{exitId,name,locked,x,y,slot,g,label}]
this.doorDwell = 0;           // ms inside an unlocked door zone
this.doorArmed = null;        // exitId currently being dwelled on
this.doorCooldown = 0;        // ms before another transition may fire
this.objects = [];            // [{id,kind,label,x,y,consumed,locked,g}]
this.nearObject = null;       // the object E would act on
this.nearDoor = null;         // the locked door E would try
this.barks = [];              // [{npc_id,text}]
this.barkTimer = 0;
this.barkRecent = [];         // last 4 texts, to avoid repeats
```

Class constants next to `ARCHETYPES`:

```js
static DOOR_RADIUS = 5.5;     // normalized units
static DOOR_DWELL_MS = 220;   // walk into it this long -> travel
static DOOR_COOLDOWN_MS = 2500;
static OBJECT_RADIUS = 7.0;
static BARK_MIN_MS = 12000;
static BARK_MAX_MS = 26000;
static BARK_NEAR = 34.0;      // only bark when this close to the player
```

### 10.2 `game.js` — `init()`

Add one layer between `backgroundLayer` and `shadowLayer` (doors are scenery, they must render behind everyone):

```js
this.doorLayer = new PIXI.Container();
this.worldContainer.addChildAt(this.doorLayer, 1);
```

Object markers go in `entityLayer` with `zIndex = worldY` so the player walks in front of a low crate and behind a tall one — consistent with NPC depth sorting.

In the existing stage `pointerdown` handler, first line: `if (document.activeElement && document.activeElement.blur) document.activeElement.blur();` — clicking the scene releases the chat input so WASD and E work. (Today the player must click away manually; this is a strict improvement and costs three lines.)

### 10.3 `game.js` — `setupInput()`

After the `Ctrl+K` branch and **before** the WASD branch:

```js
// E: interact with the nearest object (or try a locked door)
if (!e.ctrlKey && !this.editorMode && key === 'e') {
    this._tryInteract();
    e.preventDefault();
    return;
}
// 'q' is RESERVED for quick-use consumables (progression spec) — do not bind.
```

The existing `Ctrl+E` branch runs first, so editor mode is unaffected. The existing `INPUT`/`TEXTAREA` guard at the top of `_onKeyDown` means E never steals a keystroke from the chat box.

### 10.4 `game.js` — `loadLocation()`

In the clear block (~line 265) add `this.doorLayer.removeChildren();` and reset `this.doors = []; this.objects = []; this.barks = []; this.nearObject = null; this.nearDoor = null; this.doorArmed = null; this.doorDwell = 0;`.

After `this.exits = data.exits || [];` add:

```js
this._buildDoors(data.exits || []);
this._buildObjects(data.objects || []);
this._loadBarks(locationId);   // fire and forget
```

### 10.5 `game.js` — new methods

```js
_buildDoors(exits)          // one PIXI.Graphics arch + PIXI.Text label per exit
                            // that has a `door`; locked doors draw in amber with
                            // a small keyhole glyph, unlocked in soft indigo.
_buildObjects(objects)      // a pulsing 4-point sparkle (Graphics) per object;
                            // consumed -> alpha 0.22, no prompt, no hit test.
_updateDoors(dt)            // dwell + fire; sets this.nearDoor for locked ones
_updateObjects()            // sets this.nearObject; drives the prompt
_updateInteractPrompt()     // uiLayer text: "[E]  Take the bundle of oilcloth"
_tryInteract()              // dispatches canvas-interact / canvas-travel(locked)
markObjectConsumed(id)      // grey it out after a successful interact
setBarks(barks)             // store + reset barkTimer
_loadBarks(locationId)      // fetch /api/barks/{id}; if pending, one retry in 20s
_updateBarks(dt)            // timed proximity bubbles
```

`_updateDoors(dt)` logic:

```
if (this.combat || this.playerDead || this.editorMode) return;
this.doorCooldown = Math.max(0, this.doorCooldown - dt);
find the nearest door within DOOR_RADIUS of the player
  none -> doorArmed = null, doorDwell = 0, nearDoor = null; return
  locked -> nearDoor = door (E will try it); doorDwell = 0; return
  unlocked:
     if (window.forgeTurnBusy) { doorDwell = 0; return; }   // DM turn streaming
     if (door.exitId !== doorArmed) { doorArmed = door.exitId; doorDwell = 0; }
     doorDwell += dt;
     if (doorDwell >= DOOR_DWELL_MS && doorCooldown <= 0) {
         doorCooldown = DOOR_COOLDOWN_MS; doorDwell = 0;
         window.dispatchEvent(new CustomEvent('canvas-travel', {detail:
             {exitId: door.exitId, name: door.name, locked: false}}));
     }
```

`_updateBarks(dt)`:

```
if (this.combat || this.editorMode || this.playerDead) return;
if (window.forgeTurnBusy) return;            // never talk over the DM
this.barkTimer -= dt; if (this.barkTimer > 0) return;
this.barkTimer = BARK_MIN_MS + Math.random() * (BARK_MAX_MS - BARK_MIN_MS);
candidates = alive NPCs within BARK_NEAR of the player that have no bubble up
if none -> return
pick a bark whose text is not in barkRecent (retry 5x, else return)
speaker = bark.npc_id && this.npcs[bark.npc_id] alive-and-near
          ? bark.npc_id : random candidate        // "ambient" lines get a body
this.showSpeechBubble(speaker, bark.text, 3800, null);
barkRecent.push(bark.text); if (barkRecent.length > 4) barkRecent.shift();
```

Barks reuse `showSpeechBubble` untouched: `audioId = null` means no `forge-voice-*` sync, no TTS, no chat line, no music ducking. Zero audio cost, exactly as scoped.

`update()` gains three calls next to `_updateNpcWander(deltaTime)`:

```js
this._updateDoors(deltaTime);
this._updateObjects();
this._updateBarks(deltaTime);
```

`onResize()` repositions doors and object markers (same pattern as the NPC loop) and calls `this._redrawCollisionOverlay()` as it already does.

### 10.6 `game.js` — edge-push reconciliation

The existing `location-exit` dispatch (in `update()`, ~line 684) gains one guard so the chooser does not fight the doors:

```js
// A door within reach handles this edge — don't also pop the chooser
const doorNear = this.doors.some(d =>
    Math.hypot(d.x - this.player.normalizedX, d.y - this.player.normalizedY) < 12);
if (!this.combat && !doorNear && this.edgePushTimer >= this.edgePushThreshold && ...) {
    ... existing dispatch, unchanged ...
}
```

So: **doors are the primary verb; the edge-push chooser survives as the "I don't see a way out here" fallback** — which is exactly what it is good at (exits with no door slot, and the "No known ways out / ask the DM" hint).

### 10.7 `index.html` — listeners

`window.forgeTurnBusy`: set `true` at the top of `sendMessage`, `false` in its `finally`. Also set from the `combat-active` listener.

```js
window.addEventListener('canvas-travel', async (e) => {
    const { exitId, name, locked } = e.detail || {};
    if (!currentPlayerId) return;
    if (locked) { sendMessage(`I try to get through to ${name}.`); return; }
    try {
        const res = await fetch('/api/travel', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ player_id: currentPlayerId,
                                   destination_id: exitId, source: 'door' })
        });
        const data = await res.json();
        if (data.busy) return;                       // DM mid-turn; try again later
        if (data.blocked) { sendMessage(data.chat_prompt); return; }
        if (!data.success) return;
        applyPlayerState(data.state);
        await handleLocationChange(data.destination_id, data.destination_name);
        showToast(`${escapeHtml(data.destination_name)} &middot; ${data.minutes} min`, '', 2200);
    } catch (err) { console.error('canvas travel failed:', err); }
});

window.addEventListener('canvas-interact', async (e) => {
    const { objectId, label } = e.detail || {};
    if (!currentPlayerId || !currentLocationId) return;
    try {
        const res = await fetch('/api/interact', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ player_id: currentPlayerId,
                                   location_id: currentLocationId, object_id: objectId })
        });
        const data = await res.json();
        if (data.locked) { sendMessage(data.chat_prompt); return; }
        if (data.spent || !data.success) return;
        applyPlayerState(data.state);                 // floaters + HUD, free
        if (data.consumed && game) game.markObjectConsumed(objectId);
        if (data.result && data.result.panel_text) {
            showLorePanel(data.result.panel_title || label, data.result.panel_text);
        }
    } catch (err) { console.error('canvas interact failed:', err); }
});
```

New `showLorePanel(title, text)` — a centred card over the canvas styled like the existing exit chooser (`--bg-secondary`, 12 px radius, indigo border), serif body, dismissed by click anywhere or Escape, auto-dismissed after 14 s. ~30 lines, no dependencies.

The `location-exit` chooser's buttons change one line each:

```js
btn.addEventListener('click', () => {
    dismissExitChooser();
    if (exit.locked) sendMessage(`I head to ${exit.name}.`);
    else window.dispatchEvent(new CustomEvent('canvas-travel',
        { detail: { exitId: exit.id, name: exit.name, locked: false } }));
});
```

Locked exits also get a small amber lock glyph in their button label.

### 10.8 `index.html` — controls overlay

`showControlsHint()` grid gains two rows and one edit:

```html
<b>E</b><span>take, read, open, search what you're standing by</span>
<b>Walk into a door</b><span>travel there instantly — no waiting on the DM</span>
<b>Q</b><span>quick-use a consumable</span>
```

and the existing `<b>Push an edge</b>` row's text becomes `find a way out when no door is visible`.

---

## 11. Hotkey map (complete, after these additions)

| Key | Context | Action | Owner |
|---|---|---|---|
| `W A S D` / arrows | scene | walk | existing |
| `E` | scene, near an object or locked door | interact / try the door | **this spec** |
| walk into a door | scene | instant travel (220 ms dwell) | **this spec** |
| push a scene edge (350 ms) | scene, no door nearby | exit chooser | existing, narrowed |
| `Space` | combat | dodge-roll (i-frames) | existing |
| `J` | combat | melee strike | existing |
| `Q` | scene | quick-use consumable — **reserved, do not bind here** | progression spec |
| `Enter` | chat input focused | send turn | existing |
| click NPC | scene | address them (then type) | existing |
| click scene | scene | deselect NPC + **blur chat input** | existing + this spec |
| `Ctrl+E` | anywhere | toggle world-editor mode | existing |
| `C` | editor mode | toggle collision editor | existing |
| click / `Enter` / `Esc` / `G` / right-click | collision editor | vertex / close / cancel / auto-detect / delete | existing |
| `Ctrl+K` | scene | spawn gray-box test enemy | existing |
| `Esc` | lore panel open | dismiss | **this spec** |

Conflict audit: `E` is checked **after** `Ctrl+E` and is skipped entirely in `editorMode`, so the editor keeps its meaning. `Q` is documented but deliberately unbound — the progression spec owns it; binding it here would create a merge conflict in `setupInput()`.

---

## 12. Failure modes and how each degrades

| Failure | Behaviour |
|---|---|
| No `OPENROUTER_API_KEY` / bark call fails | `/api/barks` returns `[]`, `unavailable: true`; the scene is quiet. Nothing else changes. Failure cooldown (300 s) prevents re-billing. |
| Existing world, all three columns NULL | Doors work (derived). No objects, no barks. Identical to today plus instant travel. |
| Creator agent emits garbage `objects` | `normalize_objects` drops the bad rows and keeps the good ones. Worst case: no objects. |
| Player spams a door | `DOOR_COOLDOWN_MS` (2500) plus the server's `_play_lock` busy check. |
| Player spams `E` on a consumed object | Marker is inert client-side; server returns 409 `spent` regardless. Two layers. |
| `/api/travel` while the DM is mid-turn | 409 `busy`, silently ignored client-side; the player keeps walking and can try again in a second. |
| DM turn crashes after the ledger was drained | Canvas actions are already in the DB; only the narrative hint is lost. Same failure profile as `_pending_audit_feedback` today. |
| Door slot lands on an obstacle | Door is a trigger zone, not a passage — the player may not be able to reach it; the edge-push chooser still lists the exit. |

---

## 13. Scope

### v1 — build now

1. Three nullable columns + config + `agents.yaml` bark entry.
2. `src/services/travel.py` (`check_route` extraction + `travel_now`), `move_player` refactored onto it.
3. `src/services/scene_exits.py` — derived doors.
4. `src/services/scene_objects.py` — normalize / public / resolve.
5. `src/services/barks.py` — signature, staleness, one background call.
6. `src/web/pending_actions.py` — ledger + context block.
7. `POST /api/travel`, `POST /api/interact`, `GET /api/barks/{id}`.
8. `/api/assets/location/{id}` enriched with `door`/`locked`/`requirements` per exit and a top-level `objects` list.
9. `audit_turn(..., code_actions=...)` union; combat-result retrofit.
10. DM prompt section; creator prompt section; `objects` param on `add_location` / `update_location` / `LocationUpdate`.
11. `game.js`: doors, object markers, `E`, interact prompt, barks, canvas-click blur, edge-push narrowing.
12. `index.html`: two listeners, `showLorePanel`, chooser rewire, `window.forgeTurnBusy`, controls overlay, cache-buster.

### Later — explicitly out of v1

- **Background-image hotspots.** Vision-model detection of "what in this picture is takeable", reusing `ImageGenerator.detect_obstacles` machinery, to place objects on the *actual* painted crate instead of authored coordinates.
- **Authored door coordinates.** A `Location.exit_doors` JSON column plus a Ctrl+E editor mode (`D`) to drag doors onto the painted archway. Derived slots are the fallback.
- **Object sprites.** Generated per-object art instead of the sparkle marker.
- **Bark TTS.** Deliberately excluded on cost; when the local Qwen3-TTS path is cheap enough, barks become the best use of it.
- **World tick on canvas travel.** Today scheduled events fire on DM turns only, so a player who explores five rooms without typing sees them fire late. Fix by running a read-only tick on travel and surfacing fired events as a `pending_actions` entry.
- **Object-aware DM tools.** A `place_object` tool so the DM can drop a thing into the scene mid-narration ("the smuggler kicks a satchel toward you").
- **Bark reactivity.** Regenerate the pool when tension crosses into `high` so the market mutters about the thing that just happened.
- **Multi-item containers, weight, ownership.**

---

## 14. Build checklist (implementation order)

1. **Schema.** Add the three columns to `src/models/location.py`. Run `uv run pytest` — `_migrate_missing_columns` must add them silently to `data/emberfall.db` on next boot. Verify by opening an existing world in the web UI.
2. **Config.** `GameConfig` fields, `settings.yaml`, `agents.yaml` `bark` entry.
3. **`src/services/travel.py`.** Write `check_route`, refactor `move_player` onto it, run `tests/test_travel_gate.py` — must pass with zero edits to the test file. Then write `travel_now`.
4. **`src/services/scene_exits.py`.** `compute_exit_doors` + unit test for determinism.
5. **`src/services/scene_objects.py`.** `normalize_objects`, `public_objects`, `resolve_interaction`.
6. **`src/web/pending_actions.py`.** Ledger + formatter.
7. **`turn_audit.audit_turn(..., code_actions=None)`.** One-line union.
8. **Endpoints.** `/api/travel`, `/api/interact`, `/api/barks/{id}`; enrich `/api/assets/location/{id}`; `_load_scene_data` objects line; combat-result `record_action` retrofit; `/api/play` drain + audit wiring.
9. **`src/services/barks.py`.** Prompt, defensive parse, staleness, DB write.
10. **Tools & prompts.** `objects` param on `add_location` / `update_location`; `LocationUpdate.objects`; creator prompt section; DM prompt section. Run `tests/test_dm_prompt_tools.py`.
11. **`game.js`.** Constants, layer, constructor state, `setupInput` `E`, `loadLocation` wiring, `_buildDoors` / `_buildObjects` / `_updateDoors` / `_updateObjects` / `_updateInteractPrompt` / `_tryInteract` / `markObjectConsumed`, barks, edge-push guard, `onResize`, canvas-click blur.
12. **`index.html`.** `window.forgeTurnBusy`, the two listeners, `showLorePanel`, chooser rewire, controls overlay, **bump both `?v=` cache-busters**.
13. **Seed data.** Add 2–3 objects to two locations in `main.py seed` / `tests/test_playtest_seed.py` fixtures so the feature is testable without a world-gen run.
14. **Manual pass.** Walk into a door (instant), walk into a gated door (chat), take a crate (floater + HUD), read a notice (panel), re-press `E` on the taken crate (nothing), then type one turn and confirm the DM's context block appears in the server log and the audit is clean.

---

## 15. Tests (`tests/`, pytest)

### `tests/test_canvas_travel.py`
- `test_unlocked_travel_moves_and_advances_time` — `travel_now` on an open connection moves the player, returns `minutes == round(hours*60)`, and the `WorldClock` advanced by exactly that.
- `test_locked_route_blocks_and_does_not_move` — gated connection returns `blocked` with the requirement list; `player.current_location_id` is unchanged.
- `test_hierarchy_gate_bypass_still_blocked` — mirrors `test_travel_gate.py`'s scenario through `travel_now`, proving the extracted `check_route` kept rule 2.
- `test_unreachable_destination_rejected` — a location with no connection and no parent/child relation returns `{"error": "unreachable"}` and does not move.
- `test_first_visit_flag` — first arrival returns `first_visit True`, second returns `False`.
- `test_move_player_tool_behaviour_unchanged` — the refactored `move_player` returns the same keys (`success`, `destination`, `travel_time_hours`, `reminder`) and the same `blocked` shape.

### `tests/test_scene_objects.py`
- `test_normalize_drops_unknown_kinds` — `{"kind": "smash"}` is dropped; valid siblings survive.
- `test_normalize_clamps_and_caps` — coords outside 10–90 are clamped; a 7-object list is cut to 3; `effects` is stripped from item payloads.
- `test_normalize_assigns_stable_ids` — missing ids are generated and unique.
- `test_take_adds_item_and_consumes` — `resolve_interaction` puts the item in the inventory and flips `consumed`.
- `test_take_twice_is_spent` — the second call returns `spent` and the inventory count does not change.
- `test_read_never_consumes` — two reads both return the text, `consumed` stays `False`.
- `test_locked_open_does_nothing` — returns `locked` with the hint, no inventory change, `consumed` stays `False`.
- `test_search_yields_text_and_optional_item`.
- `test_public_objects_hides_payload` — the client dict has no `payload` key and no lock string, only `locked: true`.

### `tests/test_scene_exits.py`
- `test_doors_are_deterministic` — two calls on the same inputs return identical coordinates.
- `test_child_gets_north_parent_gets_south`.
- `test_slots_never_collide` — 8 destinations get 8 distinct slots.
- `test_doors_clamped_into_walkable_bounds`.

### `tests/test_pending_actions.py`
- `test_record_and_drain_clears` — a second `drain` returns `[]`.
- `test_cap_drops_oldest_and_marks_omitted` — 15 records leave 12 plus the omitted marker; the formatted block starts with the omitted bullet.
- `test_consecutive_moves_collapse` — three moves render as one `A -> B -> C -> D` line with summed minutes.
- `test_first_visit_adds_describe_hint` — the describe_location sentence appears only with `first_visit=True`.
- `test_tool_equivalents` — a move yields `{"move_player","advance_time"}`; a read yields an empty set.

### `tests/test_turn_audit_code_actions.py`
- `test_arrival_claim_suppressed_by_code_move` — arrival text + `code_actions={"move_player"}` → no warning.
- `test_arrival_claim_still_fires_without_code_action` — the same text with `code_actions=None` → `arrival_without_move` (guards against over-suppression).
- `test_item_claim_suppressed_by_code_take` — "added to your pack" + `code_actions={"add_to_inventory"}` → no warning.
- `test_default_signature_unchanged` — calling `audit_turn` with four positional args still works.

### `tests/test_barks.py`
- `test_signature_changes_with_npc_set` — killing an NPC at the location changes the signature.
- `test_stale_when_signature_changes` / `test_stale_after_refresh_hours` / `test_fresh_pool_is_not_stale`.
- `test_parse_handles_code_fences_and_prose` — a response wrapped in ```json fences with a leading sentence still parses.
- `test_parse_returns_empty_on_garbage` — non-JSON returns `[]` and does not raise.
- `test_speaker_resolution` — an exact NPC name resolves to `npc_id`; `"ambient"` and unknown names resolve to `None`.
- `test_text_truncated_and_capped`.

### Existing tests that must stay green, unmodified
`tests/test_travel_gate.py`, `tests/test_dm_prompt_tools.py`, `tests/test_turn_filters.py`, `tests/test_combat.py`, `tests/test_inventory.py`.

---

## 16. Integration points with the other four systems

- **Progression / tiers.** `Q` is reserved here and left unbound. Object payloads flow through `add_to_inventory` untouched, so the moment progression defines a fixed consumable menu, `take`/`open` objects can name those items by label and inherit their effects — `normalize_objects` strips authored `effects` precisely so the item table stays the single source. If progression adds an XP or skill hook on item acquisition, it belongs in `add_to_inventory`, not in `resolve_interaction`, so both the canvas and DM paths get it.
- **Campaign / arcs.** The pending-actions block is the campaign system's cheapest read on player behaviour between turns: it already knows the player wandered three rooms and read the bounty notice. `read` objects are the natural place to seed an arc without spending a DM turn. Bark regeneration on tension change (deferred to "later") is the campaign system's lever on scene mood.
- **Cinematics.** Barks reuse `showSpeechBubble` with `audioId = null`, which means no `forge-voice-start/end` dispatch, no music ducking, and no chat line — cinematic speech remains visually distinct from ambient chatter by virtue of having a portrait and a voice. If cinematics later wants a "scene establishing" beat, doors and objects should be suppressed for its duration by the same `window.forgeTurnBusy` flag.
- **Tiers / NPC importance.** Bark speakers are chosen from whoever is present regardless of tier; if the tier system wants MAJOR NPCs to never mutter filler, the filter goes in `barks.generate_barks` (exclude MAJOR from the speaker list) — one line, one place.
- **Canvas (this system) ↔ combat.** Doors, objects, `E`, and barks are all hard-disabled while `this.combat` is live, matching the existing exit lock. Combat's result path is retrofitted onto the same `pending_actions` ledger, which fixes a pre-existing audit false positive as a side effect.

---

## 17. Open questions

1. **Should a canvas move fire the world tick?** Today scheduled events only fire on DM turns, so a player who explores for ten in-game minutes without typing gets their events late. Deferred to "later", but if playtests show events bunching up, `travel_now` should run a read-only tick and push fired events into the ledger.
2. **Door dwell vs. door press.** 220 ms of walking into a door is chosen so travel feels like walking, not like a menu. If playtesters report accidental transitions while circling a room, the fallback is requiring `E` on doors too — one constant and one branch.
3. **Bark pool size and refresh window.** 10 lines / 12 in-game hours is a guess. If lines start repeating audibly, the cheap fix is raising `bark_pool_size` to 16; the expensive one is regenerating on tension change.
4. **Objects on locations generated before this feature.** Existing worlds have `objects = NULL` forever unless someone retrofits them. Options: a `POST /api/world/locations/{id}/author-objects` admin endpoint calling the creator agent, or authoring them by hand in the world editor. Not scoped in v1.
5. **Should `read` objects feed NPC memory or the world bible?** A notice the player read is a fact they know; nothing currently records that. The ledger tells the DM once and then forgets. Persisting "lore the player has read" is worth a follow-up.

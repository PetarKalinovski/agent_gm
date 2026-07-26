# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Forge is a multi-agent text-based RPG dungeon master powered by LLMs. It uses the Strands Agents SDK for multi-agent orchestration with LiteLLM for provider-agnostic LLM access.

## Commands

```bash
# Install dependencies (uses UV package manager)
uv sync

# Start the game (CLI)
uv run main.py play

# Start web frontend (localhost:12000)
uv run main.py web

# Run tests
uv run pytest

# Create a test world
uv run main.py seed

# Reset/clear the world
uv run main.py clear

# Custom database path
uv run main.py play --db path/to/game.db
```

**Environment:** Requires `OPENROUTER_API_KEY` environment variable.
Optional: `SUNO_API_KEY` (music palette generation), `ELEVENLABS_API_KEY`
(NPC dialogue TTS) — audio degrades gracefully without them.

## Architecture

### Agent Hierarchy

The DM Orchestrator is the main reasoning engine. It handles combat, economy,
inventory, and quests **directly with tools** (no delegation — a sub-agent hop
is player-visible latency) and delegates only where a second persona/memory
genuinely helps:

```
DM Orchestrator (main loop, strongest model)
├── NPC Agent - Dialogue with persistent memory (fast model)
└── Creator Agent - On-demand location/NPC generation (fast model)
```

The DM prompt and `DM_TOOLS` in `src/agents/dm_orchestrator.py` must stay in
sync — `tests/test_dm_prompt_tools.py` enforces that every tool the prompt
references is registered.

World generation uses separate agents:
- WorldForge - Master world generator
- Faction/Location/NPC/History generators - Domain-specific generation

### Mechanical consequences (code enforces, LLM narrates)

- **Time**: every turn applies a minimum time cost if the DM forgot
  `advance_time` (`enforce_minimum_time_cost` in `src/services/world_tick.py`);
  the clock tracks minutes so fractional costs accumulate.
- **Events**: scheduled events fire deterministically on the world tick;
  fired-but-unnarrated events are re-surfaced to the DM for up to 3 turns.
- **Tension** (`src/services/tension.py`): health loss floors it
  (winded/hurt→rising, badly_hurt→high, critical→climax), combat floors it
  to high, sustained calm decays it one level at a time (climax 2 / high 3 /
  rising 4 calm turns), and low→rising auto-escalates after 8 quiet turns.
  DM-authored `update_dm_state` changes are respected and reset decay.
- **Quests**: `activate_quest`/`update_quest_status` sync `Player.active_quests`
  and apply `Quest.rewards` (currency/items/reputation) transactionally; both
  emit structured quest events (frontend banners + triumph music stinger).
- **Travel**: `move_player` blocks on `Connection.requirements` until the DM
  confirms them with `requirements_met=True`; hierarchy hops cannot bypass a
  destination whose every authored entry route is requirement-locked.
- **Attire semantics** (`src/services/perception.py`): what the player WEARS
  decides how strangers read them, separately from who they are. The equipped
  armor's `attire` label set (`style` from a five-word menu, optional
  `uniform_faction_id`, `concealing`) produces a code-owned perception profile
  (`state.appearance.read_as`, a `Player reads as:` context line, a
  character-panel row). Strangers react to the clothes — a faction uniform
  makes them treat the wearer as that faction regardless of identity — while
  NPCs with an `NPCRelationship` row know the player on sight; a concealed
  face makes an acquaintance's recognition *contested*, resolved by a
  DM-called `roll_check` (never rolled automatically) whose answer sticks for
  the game-day. `Location.dress_code` (`min_style` / `banned_styles` incl.
  `concealing` / `reason`) is enforced inside `travel.check_route`, so it
  blocks canvas doors, `/api/travel` (blocked 409 carries `reason: "dress"`),
  and `move_player` through one path, overridable only with
  `requirements_met=True`. `roll_check` also layers ONE code-computed
  situational factor (night/weather stealth, filthy-in-fine-company,
  hooded-and-watched, wearing-their-colours) over the DM's modifier and
  reports it back as a phrase, never a number.
- **Progression** (`src/services/progression.py`): every number lives here —
  XP thresholds (levels 1-10), quest XP by `Quest.scale` label
  (errand/job/mission/saga), gear tier tables (crude→legendary: weapon damage,
  armor hearts, trinket i-frames, catalog prices), consumables, level scalars.
  XP is granted ONLY by code (quest completion via `_complete_quest`, combat
  kills via DB tier re-read); `rewards["xp"]` authored by the DM is stripped.
  `combat_stats(player)` produces the `state.combat` dict game.js runs on;
  level-1-unarmed is test-pinned byte-identical to the pre-overhaul game.
  Equipment = pointers into inventory (`Player.equipment`); equip/unequip/
  quick-slot/use-consumable are REST endpoints, no DM turn.
- **Skill checks** (`roll_check` tool): risky non-combat actions are decided
  by a code-rolled d20 vs difficulty label (easy/tricky/hard/desperate, ±
  advantage/disadvantage) — the DM picks words, narrates the outcome label,
  never sees or prints numbers. The audit flags risky-action resolutions
  narrated without a check (`risky_action_without_check`).
- **Enemy tiers** (`src/services/combat.py`): ABSOLUTE danger ladder
  (harmless/weak/standard/dangerous/deadly/boss × brute/skirmisher/stalker/
  ranged) — `combat_params(tier, archetype)` takes no player argument, so a
  beggar stays trivial forever and growth is legible. `NPC.danger_tier` is
  assigned by the creator agent (never `boss` — that's authored-only via the
  world editor); NULL infers from keywords. `standard` row == legacy values
  (test-pinned). Tier XP 0/5/15/40/100/300 awarded by `/api/combat/result`.
- **Real-time combat** (`src/services/combat.py` + `start_combat` tool): the
  DM names present NPCs; the frontend fights them Hades-style with
  server-resolved params riding the `combat_start` event (client table is
  fallback only). Player: light (J), heavy (hold J), parry (K), dodge
  (Space), potion (Q). Bosses: named HP bar, phase 2 at 50%, ring slam,
  lunge combo, once-ever `boss_intro` cinematic, client-side kill cam.
  `POST /api/combat/result` applies outcomes in code (hearts_lost/healed
  severity, defeat→critical, NPC deaths + world event, tier XP + level-ups)
  BEFORE the DM narrates the aftermath via an auto-sent `[COMBAT OUTCOME]`
  turn.
- **Campaign spine** (`src/services/campaign.py` + `campaign_forge` agent):
  generated at player creation from the existing roster — fixed truths and
  9-15 story beats across 3 acts live in DB tables the DM can read
  (`get_campaign`) but never write. Beats fire deterministically in
  `run_campaign_tick` (called inside `_build_context`, pure SQLite) on
  time/location/quest/level triggers with hard fallback deadlines; ops
  (move/kill NPC, quest flips, faction shifts) execute in code, then the DM
  gets a MUST-DELIVER directive + already-applied ledger. Deadline beats
  carry met AND missed consequences — blown clocks change the world. HUD
  shows act progress + countdown chips from `state.campaign`.
- **Canvas-first play** (`src/services/travel.py`, `scene_exits.py`,
  `scene_objects.py`, `src/services/barks.py`): unlocked doors travel via
  `POST /api/travel` (same code path as `move_player`, applies time) and
  objects resolve via `POST /api/interact` (take/read/open/search, fixed
  menu, numbers stripped at authoring) — no DM turn. Ambient bark pools per
  location (`GET /api/barks/{id}`, fast model, cached on Location) drive
  proximity speech bubbles between turns. Everything the engine did lands in
  the DM's next-turn context as a `[SINCE YOUR LAST TURN]` block
  (`src/web/pending_actions.py`) whose `tool_equivalents` feed the audit's
  `code_actions` so engine mutations never false-positive.
- **Cinematics** (`src/services/cinematics.py`): fixed vocabulary
  (title_card/boss_intro/kill_cam/vision/weather) triggered by label from
  campaign beats (`CAMPAIGN_CINEMATIC_MAP`), auto (boss combat), or the
  cooldown-guarded `play_cinematic` DM tool; a DB-backed rarity ledger
  (per-kind cooldowns, daily/session caps, once-ever boss intros) keeps them
  special. Rejections carry no `event` key + a say-nothing instruction.
  `DMState.weather` rides the state event as a persistent scene modifier.
- **Turn integrity** (`src/web/turn_filters.py` + `turn_audit.py`): the
  DialogueDeduper strips DM re-quotes of lines already delivered as `speech`
  events; the post-turn audit diffs narration claims (quests, damage, items,
  arrivals, consequence ledgers, inline-attributed dialogue quotes) against
  actual tool calls, attaches warnings to the `complete` SSE event, and
  injects them into the DM's next-turn context as a `[REALITY AUDIT]` block
  so it self-corrects. ALL dialogue must flow through a speech tool: the DM
  has `speak` for ambient NPCs and named-NPC one-liners (voiced + bubbled;
  unknown names degrade to a chat dialogue line), `prompt_npc_agent` for
  conversations; quoted lines inside `narrate` are silent and get flagged
  (`dialogue_without_speak`).

### Tool-Based World Mutation

Agents interact with world state exclusively through tools:
- `src/tools/world_read/` - Query world state (locations, NPCs, quests, etc.)
- `src/tools/world_write/` - Mutate world state (move player, advance time, etc.)
- `src/tools/narration.py` - Output to player (narrate, describe_location)
- `src/tools/agents_as_tools.py` - Sub-agents exposed as tools

### Data Models

SQLAlchemy models in `src/models/`:
- **WorldBible** - World lore, genre, tone, visual style, PC guidelines
- **Player/NPC** - Character state with NPC tiers (MAJOR/MINOR/AMBIENT)
- **Location** - Hierarchical locations with connection graph
- **Connection** - Travel routes between locations
- **Faction** - Factions with relationship tracking
- **WorldClock** - Game time simulation
- **NPCRelationship** - Player-NPC memory (trust, secrets, mood, messages)
- **Quest** - Quest definitions and tracking (+ `scale` XP label)
- **Campaign/StoryBeat** - Per-PC authored story spine (fixed truths, acts,
  deterministic beat triggers) — no write tool exists for these
- **Cinematic** - Cinematic rarity ledger + deferred queue
- Player carries `level`/`xp`/`equipment`/`quick_slot_item_id`; NPC carries
  `danger_tier`/`boss_title`; Location carries `objects`/`ambient_barks`;
  DMState carries `weather` — all nullable, NULL plays as the legacy game

No migration tool — schema evolution uses nullable columns with `DEFAULT NULL` so SQLite auto-adds them to existing DBs.

### Services Layer

`src/services/` handles asset generation and caching:
- **AssetManager** (`asset_manager.py`) - Orchestrates sprite/portrait/background generation with DB-backed caching. Entry point for all asset requests.
- **ImageGenerator** (`image_generator.py`) - Generates sprites, portraits, and location backgrounds via LLM image APIs. Walk animations are 6-frame cycles generated as a single filmstrip per direction (front/back/right; left is mirrored from right — image models have a left-to-right walking bias and flip requested left-facing strips, so we generate the direction they're biased toward; the left idle is likewise a mirror of the right idle so it matches the mirrored walk frames) — one call per direction keeps frames style-consistent, then the strip is sliced (artifact-immune: full-span rows/columns like grid lines and letterbox cards are cleared before the content bbox), background-removed, validated (ghost/sliver cells are replaced by their phase partner; a mostly-broken strip regenerates once), and normalized to the idle sprite's character-height ratio and baseline with feet-centroid horizontal anchoring so frames render at identical size with feet planted. Combat action cycles (`generate_action_cycle`: attack/dodge, 6 keys, right-facing, mirrored client-side) reuse the same machinery but skip RIFE (attacks read through snap, not smoothness) and repair broken frames from the nearest neighbor instead of the phase partner (actions are arcs, not loops). Action frames generate lazily: a 404 on `{id}_attack1.png` triggers `ensure_action_sprites` in the background.
- **ReferenceImageSearch** (`reference_search.py`) - Searches Bing Images for reference photos of known-IP characters/locations. Uses entity-ID-based caching (`references/{entity_id}.png`). Controlled by `reference_search_query` field on NPC/Location models — `NULL` means no web search.
- **MusicGenerator** (`music_generator.py`) - Per-world mood palette (explore/tension/danger/somber/night/triumph) via the Suno API; style prompts built from the WorldBible. Cached as `music/{mood}.mp3`; `GET /api/assets/music/manifest` triggers background generation and reports status.
- **VoiceGenerator** (`voice_generator.py`) - NPC dialogue TTS: local Qwen3-TTS voice cloning when a reference clip exists at `voices/refs/{npc_id}.wav`, else ElevenLabs using `NPC.voice_id` (auto-assigned from the `audio.voice_pool` by tag-matching NPC descriptions). Lines cached as `voices/{sha1(npc|text|tone)}.mp3`.

Asset pipeline: `reference_search` finds web images → `image_generator` creates assets using reference + text description → `asset_manager` caches results to disk and DB.

Appearance variants (`src/services/appearance.py`): the player's sprite set is keyed by deterministic signatures over equipped gear — `body_sig` (armor → idles/walks/dodge, `sprites/variants/{sig}/`) and `action_sig` (armor+weapon pair → attack strip); `base`/`base-unarmed` resolve to the flat legacy dir with zero migration. `state.appearance` carries the WANTED signatures (+ `read_as`); readiness lives on `GET /api/assets/appearance/{player_id}` and the scene fetch (`serving_*` = complete on disk). The sprite endpoint takes `?a=<sig>` (strict when present, lenient serving-signature resolution when absent); equip/unequip pre-warm generation and the client swaps textures atomically per axis only when a whole set exists — never a half-dressed frame.

### Scene collision & canvas liveliness

- **Collision**: `Location.obstacles` holds polygons (normalized 0-100).
  Auto-detected from the background image at generation time via the vision
  model (`ImageGenerator.detect_obstacles`; boxes are reduced to bottom-slice
  footprints since characters walk behind the upper part of objects in the
  3/4 view). Re-runnable via `POST /api/world/locations/{id}/detect-obstacles`.
  In-game editor: Ctrl+E then C — click vertices, Enter closes, right-click
  deletes, G auto-detects. Frontend blocks movement by point-in-polygon.
  NOTE: `gemini-2.5-flash` is sunset (404); `vision_model` is `gemini-3.5-flash`.
- **Ambient NPC wandering** (client-side only, not persisted): alive NPCs
  stroll between walkable points near their DB position, pause near the
  player; drag-editing an NPC rebases its wander home.
- **Day/night tint**: the frontend tints the world container from the state
  event's `time_of_day` (evening amber, night blue, etc.).
- **Real-time combat**: DM-initiated via `start_combat` → `combat_start` SSE
  → `GameRenderer.startCombat` spawns present NPCs as combatants using their
  real sprites (ambient bodies hidden meanwhile), driven by the fixed
  archetype table `GameRenderer.ARCHETYPES` (brute/skirmisher/stalker;
  stalkers circle before committing). Player combat HP derives from health
  status — wounded characters fight weaker. Space = dodge-roll with i-frames,
  J = melee arc; chase → telegraph → lunge → recover AI; hitstop, knockback,
  screenshake, exits lock during combat. Outcome → `combat-ended` event →
  `POST /api/combat/result` (code applies health/deaths) → auto-sent
  `[COMBAT OUTCOME]` turn for the aftermath. Ctrl+K still spawns a gray-box
  test enemy (client-only, resets on defeat).

### Audio / cinematic event flow

`speak()` and `describe_location()` return an `"event"` key in their tool-result
dicts; `ToolUsageTracker._parse_tool_output_for_events` (same mechanism as
`npc_death`) converts these into structured SSE events: `speech`
(npc_name/text/tone/action, enriched server-side with npc_id + a TTS
`audio_id`), `scene` (location/time_of_day/atmosphere), quest lifecycle
events (`quest_activated`/`quest_completed`/`quest_failed`),
`combat_start`, `check_result` (outcome label, never numbers), `xp_gain` +
`level_up` (fan-out of any tool result carrying a `progression` block —
order: quest_completed → xp_gain → level_up), `cinematic` (a nested
`cinematic` key on ANY tool result emits BEFORE the tool's own event, so a
boss intro precedes `combat_start`), and `campaign_beat`/`campaign_act`/
`campaign_resolved` (drained server-side around the stream). `state` events
stream mid-turn after every successful world-write tool AND post-turn,
carrying `tension` plus level/xp/equipment/`combat` stats/`campaign`
summary/`weather`. Combat XP rides the `/api/combat/result` REST response,
never SSE. The frontend `ForgeAudio`
class (`static/js/audio.js`) maps tension/time/death to a music mood,
crossfades palette tracks, plays a one-shot triumph stinger on quest
completion (reverts to the world mood; danger interrupts), and plays voice
clips sequentially (polling `/api/assets/voice/{audio_id}`), ducking music
during speech and dispatching `forge-voice-start/end` for speech-bubble sync.
Combat SFX: seven pre-generated one-shots (ElevenLabs sound-generation,
committed under `static/sfx/`) play via `ForgeAudio.playSfx` (WebAudio —
overlapping plays, pitch jitter, per-name throttle, "Effects" volume slider);
game.js triggers swing/hit/hurt/roll/death/windup/clash from the combat loop.
Canvas feedback: state diffs spawn floating text over the player
(gold/health/items), speech events show bubbles above NPC sprites, and
`prompt_npc_agent` tool pills carry `npc_id` so the chat shows "<NPC>
considers…" with their portrait during the slow sub-agent hop. Resuming a
world with history shows a "Previously…" recap (`GET /api/recap/{player_id}`,
fast model).

### Data Storage

- **Databases**: `data/{world_name}.db` (SQLite)
- **Assets**: `data/assets/{world_name}/` with subdirectories:
  - `sprites/` - Character sprites (`{id}_{direction}.png`, `{id}_{direction}_walk{frame}.png`)
  - `portraits/` - NPC dialogue portraits
  - `locations/` - Location background images
  - `references/` - Reference images keyed by entity ID (`{entity_id}.png`, `.miss` markers for failed searches)

### Web API

`src/web/server.py` is a FastAPI server with:
- **Game endpoints** - SSE-streamed play (`/api/play`), session info, chat history
- **Canvas play** - `POST /api/travel` + `POST /api/interact` (code-resolved,
  no DM turn; responses embed the full state), `GET /api/barks/{location_id}`
- **Progression** - `POST /api/player/{id}/equip|unequip|quick-slot|use-consumable`
- **Campaign/cinematics** - `GET /api/campaign/{player_id}` (spoiler-safe;
  `?spoilers=1` debug), `POST .../generate`, `GET /api/cinematics/recent`
- **Asset endpoints** - On-demand sprite/portrait/background generation, reference image upload/get/delete
- **World CRUD** - Full REST API for all entity types (NPCs, locations, factions, quests, connections, players, world bible)
- **World management** - List/select/create worlds, WorldForge streaming generation
- **Frontend** - Single-file SPA at `src/web/static/index.html`

### Key Patterns

1. **Lazy Generation**: NPCs and locations created on-demand when players explore
2. **Persistent NPC Memory**: Uses `SemanticSummarizingConversationManager` for compressed conversation history
3. **Time Costs**: Actions consume game time (configured in `config/settings.yaml`)
4. **Database Sessions**: Always use `get_session()` context manager

## Configuration

- `config/agents.yaml` - LLM models per agent (format: `provider/model-name`)
- `config/settings.yaml` - Game settings, time costs, database path

## Entry Points

| File | Purpose |
|------|---------|
| `main.py` | CLI entry point with subcommands |
| `src/game/session.py` | Game loop, player creation |
| `src/agents/dm_orchestrator.py` | Main DM agent |
| `src/web/server.py` | FastAPI web server |

## Key Dependencies

- `strands-agents[litellm]` - Multi-agent orchestration
- `sqlalchemy` - Database ORM
- `rich` - CLI formatting
- `fastapi` + `uvicorn` - Web frontend
- `sentence-transformers` - Semantic embeddings for NPC memory

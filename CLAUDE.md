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
- **Real-time combat** (`src/services/combat.py` + `start_combat` tool): the
  DM names present NPCs; the frontend fights them Hades-style with a fixed
  archetype library (brute/skirmisher/stalker — LLM picks a label, never
  numbers). `POST /api/combat/result` applies outcomes in code (monotonic
  health worsening, defeat→critical, NPC deaths + world event) BEFORE the DM
  narrates the aftermath via an auto-sent `[COMBAT OUTCOME]` turn.
- **Turn integrity** (`src/web/turn_filters.py` + `turn_audit.py`): the
  DialogueDeduper strips DM re-quotes of lines already delivered as `speech`
  events; the post-turn audit diffs narration claims (quests, damage, items,
  arrivals, consequence ledgers) against actual tool calls, attaches warnings
  to the `complete` SSE event, and injects them into the DM's next-turn
  context as a `[REALITY AUDIT]` block so it self-corrects.

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
- **Quest** - Quest definitions and tracking

No migration tool — schema evolution uses nullable columns with `DEFAULT NULL` so SQLite auto-adds them to existing DBs.

### Services Layer

`src/services/` handles asset generation and caching:
- **AssetManager** (`asset_manager.py`) - Orchestrates sprite/portrait/background generation with DB-backed caching. Entry point for all asset requests.
- **ImageGenerator** (`image_generator.py`) - Generates sprites, portraits, and location backgrounds via LLM image APIs. Walk animations are 6-frame cycles generated as a single filmstrip per direction (front/back/right; left is mirrored from right — image models have a left-to-right walking bias and flip requested left-facing strips, so we generate the direction they're biased toward; the left idle is likewise a mirror of the right idle so it matches the mirrored walk frames) — one call per direction keeps frames style-consistent, then the strip is sliced (artifact-immune: full-span rows/columns like grid lines and letterbox cards are cleared before the content bbox), background-removed, validated (ghost/sliver cells are replaced by their phase partner; a mostly-broken strip regenerates once), and normalized to the idle sprite's character-height ratio and baseline with feet-centroid horizontal anchoring so frames render at identical size with feet planted. Combat action cycles (`generate_action_cycle`: attack/dodge, 6 keys, right-facing, mirrored client-side) reuse the same machinery but skip RIFE (attacks read through snap, not smoothness) and repair broken frames from the nearest neighbor instead of the phase partner (actions are arcs, not loops). Action frames generate lazily: a 404 on `{id}_attack1.png` triggers `ensure_action_sprites` in the background.
- **ReferenceImageSearch** (`reference_search.py`) - Searches Bing Images for reference photos of known-IP characters/locations. Uses entity-ID-based caching (`references/{entity_id}.png`). Controlled by `reference_search_query` field on NPC/Location models — `NULL` means no web search.
- **MusicGenerator** (`music_generator.py`) - Per-world mood palette (explore/tension/danger/somber/night/triumph) via the Suno API; style prompts built from the WorldBible. Cached as `music/{mood}.mp3`; `GET /api/assets/music/manifest` triggers background generation and reports status.
- **VoiceGenerator** (`voice_generator.py`) - NPC dialogue TTS: local Qwen3-TTS voice cloning when a reference clip exists at `voices/refs/{npc_id}.wav`, else ElevenLabs using `NPC.voice_id` (auto-assigned from the `audio.voice_pool` by tag-matching NPC descriptions). Lines cached as `voices/{sha1(npc|text|tone)}.mp3`.

Asset pipeline: `reference_search` finds web images → `image_generator` creates assets using reference + text description → `asset_manager` caches results to disk and DB.

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
events (`quest_activated`/`quest_completed`/`quest_failed`), and
`combat_start`. `state` events stream mid-turn after every successful
world-write tool AND post-turn, carrying `tension`. The frontend `ForgeAudio`
class (`static/js/audio.js`) maps tension/time/death to a music mood,
crossfades palette tracks, plays a one-shot triumph stinger on quest
completion (reverts to the world mood; danger interrupts), and plays voice
clips sequentially (polling `/api/assets/voice/{audio_id}`), ducking music
during speech and dispatching `forge-voice-start/end` for speech-bubble sync.
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

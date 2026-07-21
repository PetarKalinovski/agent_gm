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
- **Tension**: auto-escalates low→rising after 8 quiet turns.
- **Quests**: `activate_quest`/`update_quest_status` sync `Player.active_quests`
  and apply `Quest.rewards` (currency/items/reputation) transactionally.
- **Travel**: `move_player` blocks on `Connection.requirements` until the DM
  confirms them with `requirements_met=True`.

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
- **ImageGenerator** (`image_generator.py`) - Generates sprites, portraits, and location backgrounds via LLM image APIs. Walk animations are 6-frame cycles generated as a single filmstrip per direction (front/back/left; right is mirrored from left) — one call per direction keeps frames style-consistent, then the strip is sliced (artifact-immune: full-span rows/columns like grid lines and letterbox cards are cleared before the content bbox), background-removed, and normalized to the idle sprite's character-height ratio and baseline so frames render at identical size with feet planted.
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
- **Combat gray-box prototype**: Ctrl+K spawns a test enemy (no art, no DM
  integration). Space = dodge-roll with i-frames, J = melee arc. Enemy runs a
  chase → telegraph → lunge → recover state machine; hitstop, knockback,
  screenshake, room exits lock during combat. This is the feel prototype for
  in-world combat — archetype/skin system and DM result reporting come later.

### Audio / cinematic event flow

`speak()` and `describe_location()` return an `"event"` key in their tool-result
dicts; `ToolUsageTracker._parse_tool_output_for_events` (same mechanism as
`npc_death`) converts these into structured SSE events: `speech`
(npc_name/text/tone/action, enriched server-side with npc_id + a TTS
`audio_id`) and `scene` (location/time_of_day/atmosphere). The post-turn
`state` event carries `tension`. The frontend `ForgeAudio` class
(`static/js/audio.js`) maps tension/time/death to a music mood, crossfades
palette tracks, and plays voice clips sequentially (polling
`/api/assets/voice/{audio_id}`), ducking music during speech.

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

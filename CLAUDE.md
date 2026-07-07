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
- **ImageGenerator** (`image_generator.py`) - Generates sprites, portraits, and location backgrounds via LLM image APIs.
- **ReferenceImageSearch** (`reference_search.py`) - Searches Bing Images for reference photos of known-IP characters/locations. Uses entity-ID-based caching (`references/{entity_id}.png`). Controlled by `reference_search_query` field on NPC/Location models — `NULL` means no web search.

Asset pipeline: `reference_search` finds web images → `image_generator` creates assets using reference + text description → `asset_manager` caches results to disk and DB.

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

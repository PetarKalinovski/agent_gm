# Agent GM - Forge

A multi-agent text-based RPG dungeon master powered by LLMs. Create entire game worlds from a premise, then play through them with an AI dungeon master that maintains persistent state, NPC relationships, and dynamic world expansion.

## Features

### World Generation
- **World Forge**: Generate complete game worlds from a text premise including:
  - World Bible (tone, rules, themes, visual style)
  - Factions with complex relationships
  - Historical events that shaped the current situation
  - Hierarchical locations (galaxy → sector → planet → city → building)
  - NPCs distributed across locations with goals, secrets, and personalities
  - Pre-seeded quests ready to offer
- **Research Agent**: WorldForge can delegate to a research agent that browses the web for reference material (Wikipedia, fan wikis) to inspire authentic world-building

### Gameplay
- **Intelligent DM**: The DM Orchestrator understands player intent and responds appropriately:
  - Movement and exploration
  - NPC conversations with persistent memory
  - Combat encounters
  - Economic transactions (buying, selling, inventory)
  - Quest management
- **Dynamic World Expansion**: If you try to go somewhere that makes sense but doesn't exist (e.g., "I go into the kitchen" while in a tavern), the DM creates it on the fly
- **NPC Memory**: NPCs remember past conversations, build relationships with trust levels, and can reveal secrets over time
- **Time Simulation**: Actions consume in-game time (travel, conversations, rest)

### Visual Game Interface
- **2D Game View**: PIXI.js-powered game canvas with:
  - AI-generated location backgrounds
  - Character sprites with 4-directional movement (WASD controls)
  - Clickable NPCs for interaction
  - Real-time position persistence
- **NPC Portraits**: AI-generated portraits displayed during conversations
- **Death Animations**: NPCs can be killed with Minecraft-style death animations

### World Management
- **World Viewer**: Full CRUD interface for all game entities:
  - Players, NPCs, Locations, Factions, Quests
  - Connections between locations
  - Historical events and runtime events
  - JSON export for backup
- **Multiple Worlds**: Create and switch between different game worlds
- **World Forge Chat**: Interactive chat interface to query/modify worlds via natural language

## Installation

### Prerequisites
- Python 3.11+
- [UV package manager](https://github.com/astral-sh/uv)

### Setup

```bash
# Clone the repository
git clone <repository-url>
cd agent_gm

# Install dependencies
uv sync

# Set up environment variables. 
# It doesn't have to be Openrouter. Any compatible provider with strands-agents will work
export OPENROUTER_API_KEY=your_openrouter_api_key
```

## Quick Start

### Start the Web Interface

```bash
uv run main.py web
```

Open http://localhost:8000 in your browser.

### Create Your First World

1. Click **"Create New World"** in the world selector
2. Enter a **World Name** (e.g., "Cogsworth City")
3. Enter a **World Premise**:
   ```
   A noir detective story set in a 1920s-inspired steampunk metropolis.
   Brass gears turn the city's machinery, airships dock at sky-harbor towers,
   and the wealthy elite live in clock-tower penthouses while the poor toil
   in smoke-choked factory districts. Corruption runs deep, from the
   clockwork police force to the mysterious Artificers' Guild that controls
   all steam-tech patents. Something sinister stirs in the fog-shrouded streets.
   ```
4. Enter **Genre** (e.g., "steampunk noir")
5. Enter **Player Character Concept**:
   ```
   A disgraced former detective turned private investigator, kicked off the
   Clockwork Constabulary for asking too many questions about the wrong people.
   Now scraping by in a cramped office above a gear-grinding shop, taking cases
   the police won't touch. Has a mechanical arm - a reminder of the case that
   cost them everything.
   ```
6. Click **"Create World"** and wait for WorldForge to generate everything

### Character Setup (Recommended Workflow)

After world generation, the player is created with minimal info. To flesh them out:

1. Go to the **World Forge** tab
2. Ask: *"Generate a detailed description and background for the player character based on the world bible and PC concept"*
3. Copy the generated description and background
4. Go to the **World Viewer** tab → **Players** section
5. Click **Edit** on your player
6. Paste the description and background into the appropriate fields
7. **Save**

This gives your character a proper backstory that the DM will reference during gameplay.

### Play the Game

1. Select your world from the world selector
2. Select your player from the dropdown
3. Use **WASD** to move around the 2D game view
4. **Click on NPCs** to interact with them
5. Type in the chat to take actions, ask questions, or roleplay

## Architecture

### Agent Hierarchy

```
DM Orchestrator (main game loop)
├── NPC Agent        - Handles dialogue with persistent memory
├── Creator Agent    - On-demand world expansion
├── Economy Agent    - Inventory, shops, transactions
└── Combat Agent     - Combat encounters

World Forge (world generation)
└── Research Agent   - Web research for reference material
```

### Data Models

| Model | Purpose |
|-------|---------|
| `WorldBible` | Static world configuration (tone, rules, themes) |
| `Faction` | Organizations with goals, resources, relationships |
| `FactionRelationship` | Alliances, rivalries, wars between factions |
| `Location` | Hierarchical places with connections |
| `Connection` | Travel routes between locations |
| `NPC` | Characters with tiers (Major/Minor/Ambient) |
| `NPCRelationship` | Player-NPC memory, trust, revealed secrets |
| `Player` | Player character state and inventory |
| `Quest` | Quest definitions with objectives and rewards |
| `WorldClock` | In-game time tracking |
| `Event` | Runtime events during gameplay |
| `HistoricalEvent` | Lore events from before game start |

### NPC Tiers

- **Major**: Fully fleshed out with multiple goals, secrets, and relationships. Key story characters.
- **Minor**: Role, faction, one goal, one secret. Shopkeepers, guards, contacts.
- **Ambient**: Name, profession, one quirk. Generated on-demand as background characters.

### Conversation Memory

Agent GM uses [strands-agents-semantic-summarizing-conversation-manager](https://github.com/danilop/strands-agents-semantic-summarizing-conversation-manager) for intelligent session management. This combines:

- **Summarization**: Long conversations are automatically compressed into summaries to stay within context limits
- **Semantic Search**: Relevant past conversation snippets are retrieved based on semantic similarity to the current context

This allows NPCs to "remember" important details from past conversations even across long play sessions, while keeping token usage efficient.

## Web Interface

### Tabs

#### Game Tab
The main gameplay interface:
- 2D game canvas with AI-generated location backgrounds and character sprites
- WASD movement controls
- Click NPCs to start conversations
- Chat panel for commands and dialogue
- CRPG-style NPC portrait sidebar during conversations

#### World Viewer Tab
Full database browser and editor with sub-sections:
- **Overview**: World Bible view and edit
- **Players**: Player character management
- **NPCs**: Filterable NPC list with full editing
- **Locations**: Location hierarchy tree
- **Connections**: Travel routes between locations
- **Factions**: Faction cards with relationship matrix
- **Quests**: Quest management with status tracking
- **Items**: Inventory viewing
- **History**: Historical events timeline
- **Events**: Runtime events log
- **Export**: JSON export for all entities

#### World Forge Tab
Chat interface to interact with the WorldForge agent:
- Ask questions about the world
- Request new content generation (NPCs, locations, quests)
- Modify existing content via natural language
- Generate player descriptions and backgrounds

**Pro tip**: Use this tab frequently! Initial world generation is just a starting point. The best worlds are built iteratively through multiple World Forge conversations.

### Game Controls

| Key/Action | Effect |
|------------|--------|
| W | Move up |
| A | Move left |
| S | Move down |
| D | Move right |
| Click NPC | Start interaction |
| Click empty space | Deselect NPC / hide portrait |
| Right-click NPC | Open context menu |
| Scroll on NPC | Resize NPC sprite |

## Configuration

### `config/settings.yaml`

```yaml
database:
  path: "data/game.db"      # Default database path
  echo: false               # SQL query logging

game:
  time_costs:
    conversation: 0.5       # 30 minutes
    short_travel: 1.0       # 1 hour (within city)
    long_travel: 8.0        # 8 hours (between cities)
    rest: 8.0               # Full rest
    combat: 0.25            # 15 minutes per encounter

  recent_messages_limit: 10          # NPC conversation memory
  summary_trigger_threshold: 20      # Compress after N messages
```

### `config/agents.yaml`

Configure which LLM models power each agent:

```yaml
agents:
  dm_orchestrator:
    model: "openrouter/z-ai/glm-4.7"
    temperature: 0.7
    max_tokens: 2048

  npc_agent:
    model: "openrouter/z-ai/glm-4.7"
    temperature: 0.8
    max_tokens: 1024

  world_forge:
    model: "openrouter/deepseek/deepseek-v3.2"
    temperature: 0.8
    max_tokens: 4096

  research_agent:
    model: "openrouter/z-ai/glm-4.7"
    temperature: 0.5
    max_tokens: 4096
```

Models use LiteLLM format: `provider/model-name`

Examples:
- `openrouter/anthropic/claude-3.5-sonnet`
- `openrouter/z-ai/glm-4.7`
- `openrouter/deepseek/deepseek-v3.2`
- `anthropic/claude-3-sonnet-20240229`
- `openai/gpt-4o`

## CLI Commands

```bash
# Start web interface (recommended)
uv run main.py web

# Play in terminal (CLI mode)
uv run main.py play

# Create a test world with sample data
uv run main.py seed

# Clear/reset the current world
uv run main.py clear

# Use a specific database file
uv run main.py web --db data/my_world.db
```

## API Reference

### Game Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/session/{player_id}` | GET | Get current game session state |
| `/api/play` | POST | Send player input, get streamed response |
| `/api/look/{player_id}` | GET | Get current location description |
| `/api/player/move` | POST | Move player to coordinates |
| `/api/chat-history/{player_id}` | GET | Get conversation history |

### Asset Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/assets/location/{location_id}` | GET | Get all assets for a location |
| `/api/assets/sprite/{type}/{id}/{direction}` | GET | Get character sprite |
| `/api/assets/portrait/{npc_id}` | GET | Get NPC portrait |
| `/api/npc/transform` | POST | Update NPC position/scale |

### World Management Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/worlds` | GET | List all worlds |
| `/api/worlds/select` | POST | Switch to a different world |
| `/api/worlds/create` | POST | Create new world with WorldForge |
| `/api/world/bible` | GET/PUT | World Bible CRUD |
| `/api/world/npcs` | GET | List all NPCs |
| `/api/world/npcs/{id}` | GET/PUT/DELETE | NPC CRUD |
| `/api/world/locations/{id}` | GET/PUT/DELETE | Location CRUD |
| `/api/world/factions/{id}` | GET/PUT/DELETE | Faction CRUD |
| `/api/world/quests` | GET/POST | Quest list and creation |
| `/api/world/quests/{id}` | GET/PUT/DELETE | Quest CRUD |
| `/api/world/connections` | GET/POST | Connection management |
| `/api/world/export/{type}` | GET | Export entities as JSON |
| `/api/world/forge/query` | POST | Chat with WorldForge |

## Project Structure

```
agent_gm/
├── main.py                 # CLI entry point
├── config/
│   ├── settings.yaml       # Game settings
│   └── agents.yaml         # LLM model configuration
├── data/
│   ├── *.db                # SQLite world databases
│   └── assets/             # Generated images
│       ├── locations/      # Background images
│       ├── sprites/        # Character sprites
│       └── portraits/      # NPC portraits
└── src/
    ├── agents/
    │   ├── dm_orchestrator.py   # Main DM agent
    │   ├── npc_agent.py         # NPC dialogue agent
    │   ├── creation_agent.py    # World expansion agent
    │   ├── economy_agent.py     # Inventory/shop agent
    │   ├── world_forge.py       # World generation agent
    │   └── research_agent.py    # Web research agent
    ├── models/                  # SQLAlchemy models
    ├── tools/
    │   ├── world_read/          # Query tools
    │   ├── world_write/         # Mutation tools
    │   ├── narration.py         # Output tools
    │   └── agents_as_tools.py   # Sub-agent delegation
    ├── services/
    │   ├── asset_manager.py     # Asset loading/caching
    │   └── image_generator.py   # AI image generation
    └── web/
        ├── server.py            # FastAPI server
        ├── streaming.py         # SSE event handling
        └── static/
            ├── index.html       # Main UI
            └── js/game.js       # PIXI.js game engine
```

## Tips for Best Experience

### World Creation
1. **Write detailed world premises**: The more context you give WorldForge, the more coherent and interesting the world will be
2. **Use the Research Agent**: For worlds based on real history or existing IPs, let WorldForge research via the research agent for authentic details
3. **Review the World Bible**: After generation, check the World Viewer to see what was created and tweak as needed

### Character Setup
1. **Always set up your player character**: After world generation, use WorldForge to generate a detailed description and background
2. **Paste into World Viewer**: Copy the generated content and save it to your player in the World Viewer
3. **This matters**: The DM references player description/background during gameplay for immersive roleplay

### Gameplay
1. **Explore dynamically**: Don't be afraid to try going places that weren't explicitly created - the DM will expand the world as needed
2. **Build NPC relationships**: Talk to NPCs multiple times. They remember conversations and can reveal secrets as trust builds
3. **Check the World Viewer**: Use it to understand the world state, see NPC secrets (for DM reference), and track quest progress
4. **Use natural language**: Just describe what you want to do. "I search the room", "I ask about the artifact", "I try to sneak past the guards"

### World Forge Chat
1. **Use it often**: The initial world generation is just a starting point. Use the World Forge tab frequently to:
   - Add more NPCs to sparse locations
   - Create additional quests
   - Flesh out faction relationships
   - Add historical events for depth
   - Generate location descriptions for unexplored areas
2. **Iterative is better than one-shot**: Worlds improve dramatically with multiple passes. After initial generation, ask things like:
   - *"Add 3 more minor NPCs to the tavern district"*
   - *"Create a side quest involving the merchant guild"*
   - *"What secrets might the blacksmith be hiding?"*
   - *"Add some tension between the two main factions"*

### Images & Assets
1. **Images are saved locally**: All generated images are saved in `data/assets/`:
   - `data/assets/locations/` - Location backgrounds
   - `data/assets/sprites/` - Character sprites (4 directions each)
   - `data/assets/portraits/` - NPC conversation portraits
2. **Don't like an image? Delete it**: Simply delete the image file and it will regenerate on next load. Useful when:
   - The AI generated something that doesn't fit
   - You want a different style or angle
   - The image has artifacts or issues
3. **Image generation takes time**: First visit to a location generates the background. Subsequent visits use the cached image.

### Performance
1. **Consider model costs**: Cheaper models (DeepSeek, GLM) work well for WorldForge. Use better models for DM/NPC agents for more immersive dialogue.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENROUTER_API_KEY` | Yes | API key for OpenRouter (LLM access) |
| `ANTHROPIC_API_KEY` | No | Direct Anthropic API access |
| `OPENAI_API_KEY` | No | Direct OpenAI API access |

## Troubleshooting

### "World clock not initialized"
This happens with older worlds created before the WorldClock feature. Re-select your world from the world selector - it will automatically create the clock.

### Chat history not showing all messages
Restart the server. The chat history extraction was recently updated to properly capture narration from tool calls.

### Session corruption errors
Clear the session cache:
```bash
# Windows
rmdir /s /q %TEMP%\strands\sessions

# Linux/Mac
rm -rf /tmp/strands/sessions
```

### Images not generating
Check that your `OPENROUTER_API_KEY` is set and has access to image generation models (Gemini 2.5 Flash).

## License

MIT License

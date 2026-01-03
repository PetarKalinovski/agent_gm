# Agent GM

A multi-agent text game dungeon master powered by LLMs.

## Setup

1. Install dependencies:
```bash
pip install -e .
```

2. Set your OpenRouter API key:
```bash
export OPENROUTER_API_KEY=your_key_here
```

3. Run the game:
```bash
python main.py
```

## Commands

- `python main.py` or `python main.py play` - Start the game
- `python main.py seed` - Create a test world
- `python main.py clear` - Reset the world

## In-Game Commands

- `look` or `l` - Look around
- `status` - Show your status
- `help` - Show help
- `quit` or `exit` - Leave the game

During exploration, type what you want to do in natural language.
During conversation, type what you want to say. Use `bye` to end conversations.

## Configuration

- `config/agents.yaml` - Configure LLM models per agent
- `config/settings.yaml` - General game settings

## Architecture

The game uses a multi-agent architecture:

- **DM Orchestrator** - Main dungeon master that narrates the world
- **NPC Agent** - Handles NPC dialogue with persistent memory
- **World State** - SQLite database tracking locations, NPCs, and player

See the implementation plan at `.claude/plans/` for more details.

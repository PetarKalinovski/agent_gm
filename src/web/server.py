"""FastAPI server for Forge web frontend."""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.agents.base import setup_api_keys
from src.agents.dm_orchestrator import DMOrchestrator
from src.config import load_settings, get_active_db_path, set_runtime_db_path
from src.models import Location, Player, get_session, init_db, reset_engine
from src.models.location import Connection
from src.tools.world_read import (
    get_current_location,
    get_player,
    get_world_clock,
    get_active_quests,
)
from src.tools.narration import set_web_output_callback
from src.web.streaming import ToolUsageTracker
from src.agents.callback_context import set_callback_handler, clear_callback_handler
from src.services.asset_manager import AssetManager

load_dotenv()

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)
# Also make sure strands logging is visible
logging.getLogger("strands").setLevel(logging.DEBUG)

# Initialize
app = FastAPI(title="Forge", description="AI-powered world building and text adventure game")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Store active sessions - DMOrchestrator + ToolUsageTracker
sessions: dict[str, dict[str, Any]] = {}


class GameRequest(BaseModel):
    """Request model for game input."""
    player_input: str
    player_id: str


class SessionInfo(BaseModel):
    """Response model for session info."""
    player_id: str
    player_name: str
    location: str
    time: str

class TransformRequest(BaseModel):
    npc_id: str
    x: float
    y: float
    scale: float


class WorldCreateRequest(BaseModel):
    """Request model for creating a new world."""
    name: str  # Used as the database filename
    premise: str
    genre: str = "fantasy"
    pc_concept: str = ""
    num_factions: int = 6
    num_major_npcs: int = 12
    num_minor_npcs: int = 40


class WorldSelectRequest(BaseModel):
    """Request model for selecting a world."""
    db_path: str


def get_or_create_session(player_id: str) -> dict[str, Any]:
    """Get or create a session with DM and tool tracker."""
    if player_id in sessions:
        return sessions[player_id]

    settings = load_settings()
    setup_api_keys()
    init_db(get_active_db_path())

    # Create tool tracker first (must be passed during agent creation)
    tool_tracker = ToolUsageTracker()

    # Create DMOrchestrator with callback_handler
    dm = DMOrchestrator(player_id, callback_handler=tool_tracker)

    sessions[player_id] = {
        "dm": dm,
        "tool_tracker": tool_tracker,
    }

    return sessions[player_id]


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    """Serve the main frontend HTML."""
    frontend_path = Path(__file__).parent / "static" / "index.html"
    if frontend_path.exists():
        return FileResponse(frontend_path)
    return HTMLResponse("<h1>Forge</h1><p>Frontend not found. Run from correct directory.</p>")


@app.get("/api/players")
async def list_players():
    """List all available players."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db_session:
        players = db_session.query(Player).all()
        return [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "location_id": p.current_location_id,
            }
            for p in players
        ]


@app.get("/api/session/{player_id}")
async def get_session_info(player_id: str):
    """Get current session info for a player."""
    get_or_create_session(player_id)

    # Get current state
    location = get_current_location(player_id) # This helper usually returns a dict
    clock = get_world_clock()
    player = get_player(player_id)

    return {
        "player_id": player_id,
        "player_name": player.get("name", "Unknown"),
        "location": location.get("name", "Unknown"),
        "location_id": location.get("id"),  # <--- ADD THIS LINE
        "location_description": location.get("description", ""),
        "time": f"Day {clock.get('day', 1)}, {clock.get('hour', 8)}:00",
        "time_of_day": clock.get("time_of_day", "day"),
        "npcs_present": location.get("npcs_present", []),
    }


@app.get("/api/chat-history/{player_id}")
async def get_chat_history(player_id: str, limit: int = 50):
    """Get chat history for a player from the DM's conversation.

    Args:
        player_id: The player's ID.
        limit: Maximum number of messages to return (default 50).

    Returns:
        List of chat messages with role and content.
    """
    import json

    session = get_or_create_session(player_id)
    dm: DMOrchestrator = session["dm"]

    try:
        # Access agent.messages which contains the conversation history
        messages = dm.agent.messages or []

        # Convert to simple format for frontend
        chat_history = []
        narration_tools = {"narrate", "describe_location"}

        for msg in messages[-limit:]:  # Get last N messages
            role = msg.get("role", "unknown")
            content_blocks = msg.get("content", [])

            # Extract text content from message blocks
            text_content = ""
            for block in content_blocks:
                if isinstance(block, dict):
                    # Direct text content
                    if "text" in block and "toolUse" not in block and "reasoningContent" not in block:
                        text_content += block.get("text", "")

                    # Strands format: toolUse inside content blocks
                    elif "toolUse" in block:
                        tool_use = block["toolUse"]
                        tool_name = tool_use.get("name", "")
                        if tool_name in narration_tools:
                            # Get input (Strands uses 'input', not 'arguments')
                            tool_input = tool_use.get("input", {})
                            narration_text = tool_input.get("text") or tool_input.get("description", "")
                            if narration_text:
                                text_content += narration_text + "\n"

                elif isinstance(block, str):
                    text_content += block

            # Also check old-style tool_calls field (for compatibility)
            tool_calls = msg.get("tool_calls", [])
            for tool_call in tool_calls:
                if isinstance(tool_call, dict):
                    func = tool_call.get("function", {})
                    tool_name = func.get("name", "")
                    if tool_name in narration_tools:
                        try:
                            args_str = func.get("arguments", "{}")
                            if isinstance(args_str, str):
                                args = json.loads(args_str)
                            else:
                                args = args_str
                            narration_text = args.get("text") or args.get("description", "")
                            if narration_text:
                                text_content += narration_text + "\n"
                        except (json.JSONDecodeError, TypeError):
                            pass

            # Skip empty messages
            if text_content.strip():
                final_content = text_content.strip()

                # For user messages, extract just the player's actual message
                # The DM prepends context like "Current context:\n...\n\nPlayer says: <actual message>"
                if role == "user" and "Player says:" in final_content:
                    parts = final_content.split("Player says:", 1)
                    if len(parts) > 1:
                        final_content = parts[1].strip()

                chat_history.append({
                    "role": role,
                    "content": final_content
                })

        return {"messages": chat_history}

    except Exception as e:
        logger.error(f"Error getting chat history: {e}", exc_info=True)
        return {"messages": [], "error": str(e)}


@app.get("/api/debug/messages/{player_id}")
async def debug_messages(player_id: str, limit: int = 10):
    """Debug endpoint to see raw message structure."""
    session = get_or_create_session(player_id)
    dm: DMOrchestrator = session["dm"]

    try:
        messages = dm.agent.messages or []
        # Return last N messages with their full structure
        debug_data = []
        for msg in messages[-limit:]:
            content_blocks = msg.get("content", [])

            # Extract block types and tool names from content
            block_types = []
            content_tool_names = []
            for block in content_blocks:
                if isinstance(block, dict):
                    if "text" in block:
                        block_types.append("text")
                    if "toolUse" in block:
                        block_types.append("toolUse")
                        content_tool_names.append(block["toolUse"].get("name", "unknown"))
                    if "reasoningContent" in block:
                        block_types.append("reasoning")
                    if "toolResult" in block:
                        block_types.append("toolResult")

            debug_data.append({
                "role": msg.get("role"),
                "block_types": block_types,
                "content_tool_names": content_tool_names,
                "content_preview": str(content_blocks)[:300] if content_blocks else "[]",
            })
        return {"messages": debug_data, "total_count": len(messages)}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/play")
async def play_sse(request: GameRequest):
    """Stream game response via SSE using stream_async pattern."""
    session = get_or_create_session(request.player_id)
    dm: DMOrchestrator = session["dm"]
    tool_tracker: ToolUsageTracker = session["tool_tracker"]

    # Reset tool tracker for new request
    tool_tracker.reset()

    # Build context like DMOrchestrator.process_input does
    location = get_current_location(request.player_id)
    clock = get_world_clock()

    context = f"""Current context:
- Location: {location.get('name', 'Unknown')} ({location.get('type', 'unknown')})
- Time: Day {clock.get('day', 1)}, {clock.get('hour', 8)}:00 ({clock.get('time_of_day', 'day')})
- NPCs here: {', '.join(n['name'] for n in location.get('npcs_present', [])) or 'None'}

Player says: {request.player_input}"""

    # Set up narration callback
    narration_buffer: list[str] = []

    def narration_callback(text: str):
        narration_buffer.append(text)

    set_web_output_callback(narration_callback)

    # Set callback handler in context so sub-agents can use it
    set_callback_handler(tool_tracker)

    async def event_stream():
        """Generate SSE events from agent stream."""
        try:
            logger.info(f"Starting stream for player {request.player_id}")
            logger.info(f"Context: {context[:200]}...")  # Log first 200 chars

            event_count = 0

            # Use stream_async to get streaming responses
            async for response in dm.agent.stream_async(context):
                event_count += 1
                logger.debug(f"Stream event {event_count}: {type(response)} - keys: {response.keys() if isinstance(response, dict) else 'N/A'}")

                # Process through tool tracker
                tool_tracker.process_stream_payload(response)

                # Yield tool notifications
                for notification in tool_tracker.drain_notifications():
                    logger.debug(f"Tool notification: {notification.get('type', 'unknown')}")
                    encoded = json.dumps(notification, ensure_ascii=False, default=str)
                    yield f"data: {encoded}\n\n"

                # Yield token data if present
                if isinstance(response, dict) and "data" in response:
                    token_data = response["data"]
                    if token_data:
                        token_event = {"type": "token", "data": token_data}
                        yield f"data: {json.dumps(token_event)}\n\n"

                # Yield any narration output
                while narration_buffer:
                    text = narration_buffer.pop(0)
                    logger.debug(f"Narration: {text[:50]}...")
                    narration_event = {"type": "narration", "content": text}
                    yield f"data: {json.dumps(narration_event)}\n\n"

            logger.info(f"Stream finished with {event_count} events")

            # Final payload with tool summary
            final_payload = {
                "type": "complete",
                "tools": tool_tracker.snapshot(),
            }
            yield f"data: {json.dumps(final_payload)}\n\n"
            logger.info("Stream completed successfully")

        except Exception as e:
            logger.error(f"Stream error: {e}", exc_info=True)
            error_payload = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(error_payload)}\n\n"
        finally:
            set_web_output_callback(None)
            clear_callback_handler()
            yield "event: end\ndata: {}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


@app.get("/api/look/{player_id}")
async def look_around(player_id: str):
    """Get current location description."""
    location = get_current_location(player_id)
    clock = get_world_clock()

    if "error" in location:
        return {"error": location["error"]}

    return {
        "location": location,
        "time": clock,
        "npcs": location.get("npcs_present", []),
    }


@app.get("/api/locations")
async def get_locations():
    """Get all locations for the map, including NPCs at each location."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db_session:
        from src.models.location import Connection
        from src.models.npc import NPC

        locations = db_session.query(Location).all()
        result = []
        for loc in locations:
            # Get connections from this location
            connections = db_session.query(Connection).filter(
                Connection.from_location_id == loc.id
            ).all()

            # Get NPCs at this location
            npcs = db_session.query(NPC).filter(
                NPC.current_location_id == loc.id
            ).all()

            result.append({
                "id": loc.id,
                "name": loc.name,
                "type": loc.type.value if hasattr(loc.type, "value") else str(loc.type),
                "description": loc.description,
                "x": loc.position_x,
                "y": loc.position_y,
                "parent_id": loc.parent_id,
                "discovered": loc.discovered,
                "visited": loc.visited,
                "connections": [
                    {"target_id": c.to_location_id, "travel_time": c.travel_time_hours}
                    for c in connections
                ],
                "npcs": [
                    {"id": npc.id, "name": npc.name, "tier": npc.tier}
                    for npc in npcs
                ],
            })
        return result


@app.get("/api/quests/{player_id}")
async def get_quests(player_id: str):
    """Get quests for a player."""
    return get_active_quests()


# =====================
# Asset Endpoints (Visual RPG)
# =====================

# Global asset manager instance
_asset_manager: AssetManager | None = None


def get_asset_manager() -> AssetManager:
    """Get or create asset manager singleton."""
    global _asset_manager
    if _asset_manager is None:
        _asset_manager = AssetManager()
    return _asset_manager


@app.get("/api/assets/location/{location_id}")
async def get_location_assets(location_id: str, player_id: str):
    """Get all assets needed to render a location.

    Returns background, walkable bounds, player sprite, and NPC sprites.
    """
    get_or_create_session(player_id)  # Ensure DB is initialized
    asset_manager = get_asset_manager()

    try:
        assets = await asset_manager.get_location_assets(location_id, player_id)

        # Convert paths to URLs
        return {
            "location_id": assets["location_id"],
            "location_name": assets["location_name"],
            "background_url": asset_manager.get_asset_url(assets["background_path"]),
            "walkable_bounds": assets["walkable_bounds"],
            "player": {
                "id": assets["player"]["id"],
                "name": assets["player"]["name"],
                "x": assets["player"]["x"],
                "y": assets["player"]["y"],
                "scale": assets["player"].get("scale", 1.0),
                "status": assets["player"].get("status", "healthy"),
                "direction": assets["player"]["direction"],
                "sprite_url": asset_manager.get_asset_url(assets["player"]["sprite_path"])
            },
            "npcs": [
                {
                    "id": npc["id"],
                    "name": npc["name"],
                    "x": npc["x"],
                    "y": npc["y"],
                    "scale": npc.get("scale", 1.0),
                    "status": npc.get("status", "alive"),
                    "sprite_url": asset_manager.get_asset_url(npc["sprite_path"]),
                    "tier": npc["tier"]
                }
                for npc in assets["npcs"]
            ]
        }
    except Exception as e:
        logger.error(f"Error getting location assets: {e}", exc_info=True)
        return {"error": str(e)}


@app.get("/api/assets/sprite/{character_type}/{character_id}/{direction}")
async def get_sprite(character_type: str, character_id: str, direction: str):
    """Get or generate character sprite."""
    # 1. STRIP EXTENSION FIRST
    if "." in direction:
        direction = direction.split(".")[0]

    asset_manager = get_asset_manager()
    try:
        # 2. NOW parse the walk frame
        if "_walk" in direction:
            parts = direction.rsplit("_walk", 1)
            base_direction = parts[0]
            try:
                frame = int(parts[1])  # This will now be "1" or "2"
                path = await asset_manager.get_walk_frame(character_id, base_direction, frame, character_type)
            except ValueError:
                # Fallback if parsing fails
                path = await asset_manager.get_player_sprite(character_id, base_direction)
        elif character_type == "player":
            path = await asset_manager.get_player_sprite(character_id, direction)
        else:
            path = await asset_manager.get_npc_sprite(character_id, direction)

        return FileResponse(path, media_type="image/png")
    except Exception as e:
        logger.error(f"Error getting sprite: {e}", exc_info=True)
        return {"error": str(e)}

@app.get("/api/assets/portrait/{npc_id}")
async def get_portrait(npc_id: str):
    """Get or generate NPC portrait for dialogue."""
    asset_manager = get_asset_manager()

    try:
        path = await asset_manager.get_npc_portrait(npc_id)
        return FileResponse(path, media_type="image/png")
    except Exception as e:
        logger.error(f"Error getting portrait: {e}", exc_info=True)
        return {"error": str(e)}


class MoveRequest(BaseModel):
    """Request model for player movement."""
    player_id: str
    x: float
    y: float
    direction: str


@app.post("/api/player/move")
async def move_player(request: MoveRequest):
    """Update player position within current location."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        player = db.query(Player).filter(Player.id == request.player_id).first()
        if not player:
            return {"error": "Player not found"}

        player.position_x = request.x
        player.position_y = request.y
        player.facing_direction = request.direction
        db.commit()

    return {"success": True, "x": request.x, "y": request.y, "direction": request.direction}


@app.post("/api/assets/pregenerate/{location_id}")
async def pregenerate_assets(location_id: str):
    """Pre-generate all assets for a location (background + NPC sprites/portraits).

    Useful for warming up the cache before gameplay.
    """
    asset_manager = get_asset_manager()

    try:
        await asset_manager.pregenerate_location_assets(location_id)
        return {"success": True, "message": f"Pre-generated assets for location {location_id}"}
    except Exception as e:
        logger.error(f"Error pre-generating assets: {e}", exc_info=True)
        return {"error": str(e)}

@app.post("/api/npc/transform")
async def update_npc_transform(request: TransformRequest):
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        # Import NPC here to avoid circular imports
        from src.models.npc import NPC

        npc = db.query(NPC).filter(NPC.id == request.npc_id).first()
        if not npc:
            print(f"Error: NPC {request.npc_id} not found in DB")
            return {"error": "NPC not found"}

        # Update the database columns
        npc.position_x = request.x
        npc.position_y = request.y
        npc.scale = request.scale

        # Save changes
        db.commit()

        print(f"--- DB SUCCESS: {npc.name} moved to ({request.x}, {request.y}) scale={request.scale} ---")
        return {"success": True}


# =====================
# World API Endpoints
# =====================

class WorldBibleUpdate(BaseModel):
    """Request model for updating World Bible."""
    name: str | None = None
    genre: str | None = None
    tone: str | None = None
    themes: list | None = None
    visual_style: str | None = None
    current_situation: str | None = None
    setting_description: str | None = None


class WorldForgeRequest(BaseModel):
    """Request model for World Forge queries."""
    query: str


class NPCUpdate(BaseModel):
    """Request model for updating an NPC."""
    name: str | None = None
    species: str | None = None
    age: int | None = None
    profession: str | None = None
    status: str | None = None
    current_mood: str | None = None
    current_location_id: str | None = None
    home_location_id: str | None = None
    faction_id: str | None = None
    description_physical: str | None = None
    description_personality: str | None = None
    voice_pattern: str | None = None
    goals: list | None = None
    secrets: list | None = None
    skills: list | None = None
    inventory_notable: list | None = None
    position_x: float | None = None
    position_y: float | None = None
    scale: float | None = None


class LocationUpdate(BaseModel):
    """Request model for updating a location."""
    name: str | None = None
    description: str | None = None
    parent_id: str | None = None
    atmosphere_tags: list | None = None
    economic_function: str | None = None
    population_level: str | None = None
    secrets: list | None = None
    current_state: str | None = None
    controlling_faction_id: str | None = None
    visited: bool | None = None
    discovered: bool | None = None


class FactionUpdate(BaseModel):
    """Request model for updating a faction."""
    name: str | None = None
    ideology: str | None = None
    aesthetic: str | None = None
    power_level: int | None = None
    goals_short: list | None = None
    goals_long: list | None = None
    methods: list | None = None
    resources: dict | None = None
    leadership: dict | None = None
    secrets: list | None = None
    history_notes: list | None = None


class PlayerUpdate(BaseModel):
    """Request model for updating a player."""
    name: str | None = None
    description: str | None = None
    background: str | None = None
    traits: list | None = None
    current_location_id: str | None = None
    health_status: str | None = None
    currency: int | None = None
    reputation: dict | None = None
    party_members: list | None = None
    status_effects: list | None = None
    position_x: float | None = None
    position_y: float | None = None
    scale: float | None = None


class QuestUpdate(BaseModel):
    """Request model for updating a quest."""
    title: str | None = None
    description: str | None = None
    status: str | None = None
    objectives: list | None = None
    rewards: dict | None = None
    assigned_by_npc_id: str | None = None


@app.get("/api/world/bible")
async def get_world_bible():
    """Get the World Bible."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        from src.models.world_bible import WorldBible
        bible = db.query(WorldBible).first()
        if not bible:
            return {"error": "No world bible found"}
        return {
            "id": bible.id,
            "name": bible.name,
            "tagline": bible.tagline,
            "genre": bible.genre,
            "sub_genres": bible.sub_genres,
            "tone": bible.tone,
            "themes": bible.themes,
            "time_period": bible.time_period,
            "setting_description": bible.setting_description,
            "technology_level": bible.technology_level,
            "magic_system": bible.magic_system,
            "rules": bible.rules,
            "current_situation": bible.current_situation,
            "major_conflicts": bible.major_conflicts,
            "faction_overview": bible.faction_overview,
            "narration_style": bible.narration_style,
            "dialogue_style": bible.dialogue_style,
            "visual_style": bible.visual_style,
            "color_palette": bible.color_palette,
        }


@app.put("/api/world/bible")
async def update_world_bible(update: WorldBibleUpdate):
    """Update the World Bible."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        from src.models.world_bible import WorldBible
        bible = db.query(WorldBible).first()
        if not bible:
            return {"error": "No world bible found"}

        if update.name is not None:
            bible.name = update.name
        if update.genre is not None:
            bible.genre = update.genre
        if update.tone is not None:
            bible.tone = update.tone
        if update.themes is not None:
            bible.themes = update.themes
        if update.visual_style is not None:
            bible.visual_style = update.visual_style
        if update.current_situation is not None:
            bible.current_situation = update.current_situation
        if update.setting_description is not None:
            bible.setting_description = update.setting_description

        db.commit()
        return {"success": True}


@app.get("/api/world/factions")
async def get_factions():
    """Get all factions."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        from src.models.faction import Faction
        factions = db.query(Faction).all()
        return [
            {
                "id": f.id,
                "name": f.name,
                "ideology": f.ideology,
                "methods": f.methods,
                "aesthetic": f.aesthetic,
                "power_level": f.power_level,
                "resources": f.resources,
                "goals_short": f.goals_short,
                "goals_long": f.goals_long,
                "leadership": f.leadership,
            }
            for f in factions
        ]


@app.get("/api/world/npcs")
async def get_all_npcs():
    """Get all NPCs with their locations."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        from src.models.npc import NPC
        npcs = db.query(NPC).all()

        # Get location names
        location_map = {}
        locations = db.query(Location).all()
        for loc in locations:
            location_map[loc.id] = loc.name

        return [
            {
                "id": npc.id,
                "name": npc.name,
                "species": npc.species,
                "profession": npc.profession,
                "tier": npc.tier.value if hasattr(npc.tier, 'value') else str(npc.tier),
                "status": npc.status,
                "current_location_id": npc.current_location_id,
                "location_name": location_map.get(npc.current_location_id, "Unknown"),
                "faction_id": npc.faction_id,
                "description_physical": npc.description_physical,
                "description_personality": npc.description_personality,
            }
            for npc in npcs
        ]


@app.get("/api/world/npcs/{npc_id}")
async def get_npc_detail(npc_id: str):
    """Get detailed NPC info."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        from src.models.npc import NPC
        npc = db.query(NPC).filter(NPC.id == npc_id).first()
        if not npc:
            return {"error": "NPC not found"}

        location_name = "Unknown"
        if npc.current_location_id:
            loc = db.query(Location).filter(Location.id == npc.current_location_id).first()
            if loc:
                location_name = loc.name

        return {
            "id": npc.id,
            "name": npc.name,
            "species": npc.species,
            "age": npc.age,
            "profession": npc.profession,
            "tier": npc.tier.value if hasattr(npc.tier, 'value') else str(npc.tier),
            "status": npc.status,
            "current_mood": npc.current_mood,
            "current_location_id": npc.current_location_id,
            "home_location_id": npc.home_location_id,
            "location_name": location_name,
            "faction_id": npc.faction_id,
            "description_physical": npc.description_physical,
            "description_personality": npc.description_personality,
            "voice_pattern": npc.voice_pattern,
            "goals": npc.goals,
            "secrets": npc.secrets,
            "skills": npc.skills,
            "inventory_notable": npc.inventory_notable,
            "npc_relationships": npc.npc_relationships,
            "position_x": npc.position_x,
            "position_y": npc.position_y,
            "scale": npc.scale,
        }


@app.put("/api/world/npcs/{npc_id}")
async def update_npc(npc_id: str, update: NPCUpdate):
    """Update an NPC."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        from src.models.npc import NPC
        npc = db.query(NPC).filter(NPC.id == npc_id).first()
        if not npc:
            return {"error": "NPC not found"}

        if update.name is not None:
            npc.name = update.name
        if update.species is not None:
            npc.species = update.species
        if update.age is not None:
            npc.age = update.age
        if update.profession is not None:
            npc.profession = update.profession
        if update.status is not None:
            npc.status = update.status
        if update.current_mood is not None:
            npc.current_mood = update.current_mood
        if update.current_location_id is not None:
            npc.current_location_id = update.current_location_id
        if update.home_location_id is not None:
            npc.home_location_id = update.home_location_id if update.home_location_id != "" else None
        if update.faction_id is not None:
            npc.faction_id = update.faction_id if update.faction_id != "" else None
        if update.description_physical is not None:
            npc.description_physical = update.description_physical
        if update.description_personality is not None:
            npc.description_personality = update.description_personality
        if update.voice_pattern is not None:
            npc.voice_pattern = update.voice_pattern
        if update.goals is not None:
            npc.goals = update.goals
        if update.secrets is not None:
            npc.secrets = update.secrets
        if update.skills is not None:
            npc.skills = update.skills
        if update.inventory_notable is not None:
            npc.inventory_notable = update.inventory_notable
        if update.position_x is not None:
            npc.position_x = update.position_x
        if update.position_y is not None:
            npc.position_y = update.position_y
        if update.scale is not None:
            npc.scale = update.scale

        db.commit()
        return {"success": True}


@app.delete("/api/world/npcs/{npc_id}")
async def delete_npc(npc_id: str):
    """Delete an NPC."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        from src.models.npc import NPC
        npc = db.query(NPC).filter(NPC.id == npc_id).first()
        if not npc:
            return {"error": "NPC not found"}
        db.delete(npc)
        db.commit()
        return {"success": True}


@app.get("/api/world/players/{player_id}")
async def get_player_detail(player_id: str):
    """Get detailed player info."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        player = db.query(Player).filter(Player.id == player_id).first()
        if not player:
            return {"error": "Player not found"}

        location_name = "Unknown"
        if player.current_location_id:
            loc = db.query(Location).filter(Location.id == player.current_location_id).first()
            if loc:
                location_name = loc.name

        return {
            "id": player.id,
            "name": player.name,
            "description": player.description,
            "background": player.background,
            "traits": player.traits,
            "current_location_id": player.current_location_id,
            "location_name": location_name,
            "health_status": player.health_status,
            "currency": player.currency,
            "inventory": player.inventory,
            "reputation": player.reputation,
            "party_members": player.party_members,
            "active_quests": player.active_quests,
            "completed_quests": player.completed_quests,
            "status_effects": player.status_effects,
            "position_x": player.position_x,
            "position_y": player.position_y,
            "scale": player.scale,
            "facing_direction": player.facing_direction,
        }


@app.put("/api/world/players/{player_id}")
async def update_player(player_id: str, update: PlayerUpdate):
    """Update a player."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        player = db.query(Player).filter(Player.id == player_id).first()
        if not player:
            return {"error": "Player not found"}

        if update.name is not None:
            player.name = update.name
        if update.description is not None:
            player.description = update.description
        if update.background is not None:
            player.background = update.background
        if update.traits is not None:
            player.traits = update.traits
        if update.current_location_id is not None:
            player.current_location_id = update.current_location_id
        if update.health_status is not None:
            player.health_status = update.health_status
        if update.currency is not None:
            player.currency = update.currency
        if update.reputation is not None:
            player.reputation = update.reputation
        if update.party_members is not None:
            player.party_members = update.party_members
        if update.status_effects is not None:
            player.status_effects = update.status_effects
        if update.position_x is not None:
            player.position_x = update.position_x
        if update.position_y is not None:
            player.position_y = update.position_y
        if update.scale is not None:
            player.scale = update.scale

        db.commit()
        return {"success": True}


@app.get("/api/world/locations/{location_id}")
async def get_location_detail(location_id: str):
    """Get detailed location info."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        loc = db.query(Location).filter(Location.id == location_id).first()
        if not loc:
            return {"error": "Location not found"}

        return {
            "id": loc.id,
            "name": loc.name,
            "description": loc.description,
            "type": loc.type.value if hasattr(loc.type, 'value') else str(loc.type),
            "parent_id": loc.parent_id,
            "position_x": loc.position_x,
            "position_y": loc.position_y,
            "atmosphere_tags": loc.atmosphere_tags or [],
            "economic_function": loc.economic_function,
            "population_level": loc.population_level,
            "secrets": loc.secrets or [],
            "current_state": loc.current_state,
            "controlling_faction_id": loc.controlling_faction_id,
            "visited": loc.visited,
            "discovered": loc.discovered,
        }


@app.put("/api/world/locations/{location_id}")
async def update_location(location_id: str, update: LocationUpdate):
    """Update a location."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        loc = db.query(Location).filter(Location.id == location_id).first()
        if not loc:
            return {"error": "Location not found"}

        if update.name is not None:
            loc.name = update.name
        if update.description is not None:
            loc.description = update.description
        if update.parent_id is not None:
            loc.parent_id = update.parent_id if update.parent_id != "" else None
        if update.atmosphere_tags is not None:
            loc.atmosphere_tags = update.atmosphere_tags
        if update.economic_function is not None:
            loc.economic_function = update.economic_function if update.economic_function != "" else None
        if update.population_level is not None:
            loc.population_level = update.population_level if update.population_level != "" else None
        if update.secrets is not None:
            loc.secrets = update.secrets
        if update.current_state is not None:
            loc.current_state = update.current_state
        if update.controlling_faction_id is not None:
            loc.controlling_faction_id = update.controlling_faction_id if update.controlling_faction_id != "" else None
        if update.visited is not None:
            loc.visited = update.visited
        if update.discovered is not None:
            loc.discovered = update.discovered

        db.commit()
        return {"success": True}


@app.get("/api/world/factions/{faction_id}")
async def get_faction_detail(faction_id: str):
    """Get detailed faction info."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        from src.models.faction import Faction
        faction = db.query(Faction).filter(Faction.id == faction_id).first()
        if not faction:
            return {"error": "Faction not found"}

        return {
            "id": faction.id,
            "name": faction.name,
            "ideology": faction.ideology,
            "methods": faction.methods,
            "aesthetic": faction.aesthetic,
            "power_level": faction.power_level,
            "resources": faction.resources,
            "goals_short": faction.goals_short,
            "goals_long": faction.goals_long,
            "leadership": faction.leadership,
            "secrets": faction.secrets,
            "history_notes": faction.history_notes or [],
        }


@app.put("/api/world/factions/{faction_id}")
async def update_faction(faction_id: str, update: FactionUpdate):
    """Update a faction."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        from src.models.faction import Faction
        faction = db.query(Faction).filter(Faction.id == faction_id).first()
        if not faction:
            return {"error": "Faction not found"}

        if update.name is not None:
            faction.name = update.name
        if update.ideology is not None:
            faction.ideology = update.ideology
        if update.aesthetic is not None:
            faction.aesthetic = update.aesthetic
        if update.power_level is not None:
            faction.power_level = update.power_level
        if update.goals_short is not None:
            faction.goals_short = update.goals_short
        if update.goals_long is not None:
            faction.goals_long = update.goals_long
        if update.methods is not None:
            faction.methods = update.methods
        if update.resources is not None:
            faction.resources = update.resources
        if update.leadership is not None:
            faction.leadership = update.leadership
        if update.secrets is not None:
            faction.secrets = update.secrets
        if update.history_notes is not None:
            faction.history_notes = update.history_notes

        db.commit()
        return {"success": True}


@app.get("/api/world/quests/{quest_id}")
async def get_quest_detail(quest_id: str):
    """Get detailed quest info."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        from src.models.quests import Quest
        quest = db.query(Quest).filter(Quest.id == quest_id).first()
        if not quest:
            return {"error": "Quest not found"}

        return {
            "id": quest.id,
            "title": quest.title,
            "description": quest.description,
            "status": quest.status,
            "objectives": quest.objectives,
            "rewards": quest.rewards,
            "assigned_by_npc_id": quest.assigned_by_npc_id,
        }


@app.put("/api/world/quests/{quest_id}")
async def update_quest(quest_id: str, update: QuestUpdate):
    """Update a quest."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        from src.models.quests import Quest
        quest = db.query(Quest).filter(Quest.id == quest_id).first()
        if not quest:
            return {"error": "Quest not found"}

        if update.title is not None:
            quest.title = update.title
        if update.description is not None:
            quest.description = update.description
        if update.status is not None:
            quest.status = update.status
        if update.objectives is not None:
            quest.objectives = update.objectives
        if update.rewards is not None:
            quest.rewards = update.rewards
        if update.assigned_by_npc_id is not None:
            quest.assigned_by_npc_id = update.assigned_by_npc_id if update.assigned_by_npc_id != "" else None

        db.commit()
        return {"success": True}


@app.get("/api/world/quests")
async def get_all_quests():
    """Get all quests."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        from src.models.quests import Quest
        quests = db.query(Quest).all()
        return [
            {
                "id": q.id,
                "title": q.title,
                "description": q.description,
                "status": q.status,
                "assigned_by_npc_id": q.assigned_by_npc_id,
                "objectives": q.objectives,
                "rewards": q.rewards,
            }
            for q in quests
        ]


class QuestCreate(BaseModel):
    title: str
    description: str = ""
    objectives: list = []
    assigned_by_npc_id: str | None = None
    status: str = "not_started"
    rewards: dict = {}


@app.post("/api/world/quests")
async def create_quest(data: QuestCreate):
    """Create a new quest."""
    import uuid
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        from src.models.quests import Quest, QuestStatus

        # Map status string to enum
        status_map = {
            "not_started": QuestStatus.NOT_STARTED,
            "active": QuestStatus.ACTIVE,
            "completed": QuestStatus.COMPLETED,
            "failed": QuestStatus.FAILED,
        }
        quest_status = status_map.get(data.status, QuestStatus.NOT_STARTED)

        new_quest = Quest(
            id=str(uuid.uuid4()),
            title=data.title,
            description=data.description,
            objectives=data.objectives,
            assigned_by_npc_id=data.assigned_by_npc_id,
            status=quest_status,
            rewards=data.rewards,
        )
        db.add(new_quest)
        db.commit()
        return {"success": True, "id": new_quest.id}


@app.delete("/api/world/quests/{quest_id}")
async def delete_quest(quest_id: str):
    """Delete a quest."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        from src.models.quests import Quest
        quest = db.query(Quest).filter(Quest.id == quest_id).first()
        if not quest:
            return {"error": "Quest not found"}

        db.delete(quest)
        db.commit()
        return {"success": True}


@app.delete("/api/world/locations/{location_id}")
async def delete_location(location_id: str):
    """Delete a location."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        location = db.query(Location).filter(Location.id == location_id).first()
        if not location:
            return {"error": "Location not found"}

        db.delete(location)
        db.commit()
        return {"success": True}


@app.delete("/api/world/factions/{faction_id}")
async def delete_faction(faction_id: str):
    """Delete a faction."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        from src.models.faction import Faction
        faction = db.query(Faction).filter(Faction.id == faction_id).first()
        if not faction:
            return {"error": "Faction not found"}

        db.delete(faction)
        db.commit()
        return {"success": True}


@app.delete("/api/world/players/{player_id}")
async def delete_player(player_id: str):
    """Delete a player."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        player = db.query(Player).filter(Player.id == player_id).first()
        if not player:
            return {"error": "Player not found"}

        db.delete(player)
        db.commit()
        return {"success": True}


@app.get("/api/world/clock")
async def api_world_clock():
    """Get current world clock."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        from src.models.world_state import WorldClock
        clock = db.query(WorldClock).first()
        if clock:
            return {
                "day": clock.day,
                "hour": clock.hour,
                "time_of_day": clock.get_time_of_day() if hasattr(clock, 'get_time_of_day') else "unknown"
            }
        return {"day": 1, "hour": 8, "time_of_day": "morning"}


@app.get("/api/world/faction-relationships")
async def get_faction_relationships():
    """Get all faction relationships."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        from src.models.faction import FactionRelationship, Faction
        relationships = db.query(FactionRelationship).all()
        factions = db.query(Faction).all()
        faction_names = {f.id: f.name for f in factions}

        return [
            {
                "faction_a_id": r.faction_a_id,
                "faction_a_name": faction_names.get(r.faction_a_id, "Unknown"),
                "faction_b_id": r.faction_b_id,
                "faction_b_name": faction_names.get(r.faction_b_id, "Unknown"),
                "relationship_type": r.relationship_type,
                "stability": r.stability,
            }
            for r in relationships
        ]


@app.get("/api/world/historical-events")
async def get_historical_events():
    """Get all historical events."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        from src.models.world_bible import HistoricalEvent
        events = db.query(HistoricalEvent).all()
        return [
            {
                "id": e.id,
                "name": e.name,
                "description": e.description,
                "time_ago": e.time_ago,
                "event_type": e.event_type,
                "involved_parties": e.involved_parties,
                "key_figures": e.key_figures,
                "locations_affected": e.locations_affected,
                "consequences": e.consequences,
                "common_knowledge": e.common_knowledge,
                "artifacts_left": e.artifacts_left,
            }
            for e in events
        ]


@app.get("/api/world/runtime-events")
async def get_runtime_events():
    """Get runtime events from the game log."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        # Check if RuntimeEvent model exists
        try:
            from src.models.runtime_event import RuntimeEvent
            events = db.query(RuntimeEvent).order_by(RuntimeEvent.id.desc()).limit(100).all()
            return [
                {
                    "id": e.id,
                    "event_type": e.event_type,
                    "description": e.description,
                    "day": e.day,
                    "hour": e.hour,
                    "actor_id": e.actor_id,
                }
                for e in events
            ]
        except Exception:
            # Model may not exist - return empty list
            return []


@app.get("/api/world/export/{entity_type}")
async def export_world_data(entity_type: str):
    """Export world data as JSON."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        if entity_type == "bible":
            from src.models.world_bible import WorldBible
            bible = db.query(WorldBible).first()
            if bible:
                return {
                    "name": bible.name,
                    "genre": bible.genre,
                    "tone": bible.tone,
                    "themes": bible.themes,
                    "visual_style": bible.visual_style,
                    "setting_description": bible.setting_description,
                    "current_situation": bible.current_situation,
                }
            return {}

        elif entity_type == "players":
            players = db.query(Player).all()
            return [
                {
                    "id": p.id,
                    "name": p.name,
                    "current_location_id": p.current_location_id,
                }
                for p in players
            ]

        elif entity_type == "factions":
            from src.models.faction import Faction
            factions = db.query(Faction).all()
            return [
                {
                    "id": f.id,
                    "name": f.name,
                    "ideology": f.ideology,
                    "power_level": f.power_level,
                }
                for f in factions
            ]

        elif entity_type == "locations":
            locations = db.query(Location).all()
            return [
                {
                    "id": loc.id,
                    "name": loc.name,
                    "description": loc.description,
                    "type": loc.type.value if hasattr(loc.type, 'value') else str(loc.type),
                    "parent_id": loc.parent_id,
                }
                for loc in locations
            ]

        elif entity_type == "npcs":
            from src.models.npc import NPC
            npcs = db.query(NPC).all()
            return [
                {
                    "id": npc.id,
                    "name": npc.name,
                    "species": npc.species,
                    "profession": npc.profession,
                    "tier": npc.tier.value if hasattr(npc.tier, 'value') else str(npc.tier),
                    "status": npc.status,
                    "current_location_id": npc.current_location_id,
                }
                for npc in npcs
            ]

        elif entity_type == "quests":
            from src.models.quests import Quest
            quests = db.query(Quest).all()
            return [
                {
                    "id": q.id,
                    "title": q.title,
                    "description": q.description,
                    "status": q.status,
                }
                for q in quests
            ]

        return {"error": f"Unknown entity type: {entity_type}"}


@app.get("/api/world/connections")
async def get_connections():
    """Get all location connections."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        connections = db.query(Connection).all()
        return [
            {
                "id": c.id,
                "from_location_id": c.from_location_id,
                "from_location_name": c.from_location.name if c.from_location else "Unknown",
                "to_location_id": c.to_location_id,
                "to_location_name": c.to_location.name if c.to_location else "Unknown",
                "travel_type": c.travel_type,
                "travel_time_hours": c.travel_time_hours,
                "difficulty": c.difficulty,
                "description": c.description,
                "bidirectional": c.bidirectional,
                "hidden": c.hidden,
                "discovered": c.discovered,
            }
            for c in connections
        ]


class ConnectionCreate(BaseModel):
    from_location_id: str
    to_location_id: str
    travel_type: str = "walk"
    travel_time_hours: float = 1.0
    bidirectional: bool = True
    discovered: bool = True
    description: str = ""


@app.post("/api/world/connections")
async def create_connection(data: ConnectionCreate):
    """Create a new connection between locations."""
    import uuid
    settings = load_settings()
    init_db(get_active_db_path())

    if data.from_location_id == data.to_location_id:
        return {"error": "Cannot create connection from a location to itself"}

    with get_session() as db:
        # Check if connection already exists
        existing = db.query(Connection).filter(
            ((Connection.from_location_id == data.from_location_id) & (Connection.to_location_id == data.to_location_id)) |
            ((Connection.from_location_id == data.to_location_id) & (Connection.to_location_id == data.from_location_id) & (Connection.bidirectional == True))
        ).first()

        if existing:
            return {"error": "A connection between these locations already exists"}

        new_conn = Connection(
            id=str(uuid.uuid4()),
            from_location_id=data.from_location_id,
            to_location_id=data.to_location_id,
            travel_type=data.travel_type,
            travel_time_hours=data.travel_time_hours,
            bidirectional=data.bidirectional,
            discovered=data.discovered,
            description=data.description,
        )
        db.add(new_conn)
        db.commit()
        return {"success": True, "id": new_conn.id}


@app.delete("/api/world/connections/{connection_id}")
async def delete_connection(connection_id: str):
    """Delete a connection by ID."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        connection = db.query(Connection).filter(Connection.id == connection_id).first()
        if not connection:
            return {"error": "Connection not found"}

        db.delete(connection)
        db.commit()
        return {"success": True, "message": "Connection deleted"}


@app.get("/api/world/items")
async def get_items():
    """Get all items from player inventories and NPC notable items."""
    settings = load_settings()
    init_db(get_active_db_path())

    with get_session() as db:
        from src.models.npc import NPC

        all_items = []

        # Get player inventory items
        players = db.query(Player).all()
        for player in players:
            for item in (player.inventory or []):
                if isinstance(item, dict):
                    all_items.append({
                        **item,
                        "owner_type": "player",
                        "owner_id": player.id,
                        "owner_name": player.name,
                    })

        # Get NPC notable items
        npcs = db.query(NPC).all()
        for npc in npcs:
            for item in (npc.inventory_notable or []):
                if isinstance(item, dict):
                    all_items.append({
                        **item,
                        "owner_type": "npc",
                        "owner_id": npc.id,
                        "owner_name": npc.name,
                    })
                elif isinstance(item, str):
                    all_items.append({
                        "name": item,
                        "owner_type": "npc",
                        "owner_id": npc.id,
                        "owner_name": npc.name,
                    })

        return all_items


@app.post("/api/world/forge/query")
async def world_forge_query(request: WorldForgeRequest):
    """Stream World Forge response via SSE."""
    settings = load_settings()
    setup_api_keys()
    db_path = get_active_db_path()
    init_db(db_path)

    # Create unique session ID based on database path
    session_id = f"forge_{hashlib.md5(db_path.encode()).hexdigest()[:12]}"

    async def event_stream():
        try:
            from src.agents.world_forge import WorldForge

            world_forge = WorldForge(session_id)

            # Stream the response
            async for response in world_forge.agent.stream_async(request.query):
                if isinstance(response, dict) and "data" in response:
                    token_data = response["data"]
                    if token_data:
                        token_event = {"type": "token", "data": token_data}
                        yield f"data: {json.dumps(token_event)}\n\n"

            # Final event
            yield f"data: {json.dumps({'type': 'complete'})}\n\n"

        except Exception as e:
            logger.error(f"World Forge error: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            yield "event: end\ndata: {}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


# ===============================
# WORLD MANAGEMENT ENDPOINTS
# ===============================

@app.get("/api/worlds")
async def list_worlds():
    """List all available worlds (databases) in the data directory."""
    data_dir = Path("data")
    data_dir.mkdir(parents=True, exist_ok=True)

    worlds = []
    for db_file in data_dir.glob("*.db"):
        # Use forward slashes for cross-platform compatibility
        db_path = str(db_file).replace("\\", "/")
        world_info = {
            "db_path": db_path,
            "name": db_file.stem,
            "size_mb": round(db_file.stat().st_size / (1024 * 1024), 2),
            "description": None,
            "genre": None,
            "has_world_bible": False,
        }

        # Try to get world bible info
        try:
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker
            engine = create_engine(f"sqlite:///{db_file}", echo=False)
            SessionLocal = sessionmaker(bind=engine)
            session = SessionLocal()
            try:
                from src.models.world_bible import WorldBible
                bible = session.query(WorldBible).first()
                if bible:
                    world_info["has_world_bible"] = True
                    world_info["description"] = bible.tagline or bible.setting_description[:200] if bible.setting_description else None
                    world_info["genre"] = bible.genre
                    world_info["world_name"] = bible.name
            except Exception:
                pass
            finally:
                session.close()
                engine.dispose()
        except Exception:
            pass

        worlds.append(world_info)

    # Sort by name
    worlds.sort(key=lambda w: w["name"])

    return {
        "worlds": worlds,
        "current_db": get_active_db_path(),
    }


@app.get("/api/worlds/current")
async def get_current_world():
    """Get info about the currently selected world."""
    db_path = get_active_db_path()

    if not Path(db_path).exists():
        return {
            "db_path": db_path,
            "exists": False,
            "has_world_bible": False,
        }

    result = {
        "db_path": db_path,
        "exists": True,
        "has_world_bible": False,
    }

    try:
        init_db(db_path)
        with get_session() as db:
            from src.models.world_bible import WorldBible
            bible = db.query(WorldBible).first()
            if bible:
                result["has_world_bible"] = True
                result["world_name"] = bible.name
                result["genre"] = bible.genre
                result["description"] = bible.tagline or (bible.setting_description[:200] if bible.setting_description else None)

            # Count entities
            from src.models.npc import NPC
            from src.models.faction import Faction
            result["player_count"] = db.query(Player).count()
            result["npc_count"] = db.query(NPC).count()
            result["location_count"] = db.query(Location).count()
            result["faction_count"] = db.query(Faction).count()
    except Exception as e:
        result["error"] = str(e)

    return result


@app.post("/api/worlds/select")
async def select_world(request: WorldSelectRequest):
    """Select a world (database) to use."""
    global sessions

    db_path = request.db_path

    if not Path(db_path).exists():
        return {"success": False, "error": f"Database not found: {db_path}"}

    # Clear all active sessions (they're tied to the old database)
    sessions.clear()

    # Reset the database engine and set new path
    reset_engine()
    set_runtime_db_path(db_path)

    # Initialize the new database
    init_db(db_path)

    # Ensure WorldClock and at least one player exists
    with get_session() as db:
        # Create WorldClock if missing (for older worlds)
        from src.models.world_state import WorldClock
        clock = db.query(WorldClock).first()
        if not clock:
            clock = WorldClock(day=1, hour=8)
            db.add(clock)
            logger.info("Created WorldClock for existing world")

        players = db.query(Player).all()
        if not players:
            # Find a starting location (prefer settlements, cities, etc.)
            from src.models.location import LocationType
            starting_types = [
                LocationType.SETTLEMENT, LocationType.CITY, LocationType.TOWN,
                LocationType.STATION, LocationType.POI, LocationType.DISTRICT
            ]
            location = db.query(Location).filter(
                Location.type.in_(starting_types)
            ).first()
            if not location:
                location = db.query(Location).first()

            # Create placeholder player
            player = Player(
                name="Unnamed",
                description="A mysterious figure awaiting their story.",
                current_location_id=location.id if location else None,
            )
            db.add(player)
            logger.info(f"Created placeholder player: {player.id}")

        db.commit()

    return {"success": True, "db_path": db_path}


@app.post("/api/worlds/create")
async def create_world(request: WorldCreateRequest):
    """Create a new world with WorldForge."""
    global sessions

    # Sanitize name for filename
    safe_name = "".join(c for c in request.name if c.isalnum() or c in "._- ").strip()
    if not safe_name:
        return {"success": False, "error": "Invalid world name"}

    db_path = f"data/{safe_name}.db"

    if Path(db_path).exists():
        return {"success": False, "error": f"World '{safe_name}' already exists"}

    # Clear sessions
    sessions.clear()

    # Reset engine and set new path
    reset_engine()
    set_runtime_db_path(db_path)

    # Initialize fresh database
    init_db(db_path)

    # Setup API keys for generation
    setup_api_keys()

    try:
        from src.agents.world_forge import WorldForge

        # Unique session per world
        session_id = f"forge_{hashlib.md5(db_path.encode()).hexdigest()[:12]}"
        forge = WorldForge(session_id)
        result = forge.generate_world(
            premise=request.premise,
            genre=request.genre,
            pc_concept=request.pc_concept or f"A wanderer exploring this {request.genre} world",
            num_factions=request.num_factions,
            num_major_npcs=request.num_major_npcs,
            num_minor_npcs=request.num_minor_npcs,
        )

        return {
            "success": True,
            "db_path": db_path,
            "message": f"World '{safe_name}' created successfully",
        }
    except Exception as e:
        logger.error(f"World creation failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@app.post("/api/worlds/create/stream")
async def create_world_stream(request: WorldCreateRequest):
    """Create a new world with streaming progress updates."""
    global sessions

    # Sanitize name for filename
    safe_name = "".join(c for c in request.name if c.isalnum() or c in "._- ").strip()
    if not safe_name:
        async def error_stream():
            yield f"data: {json.dumps({'type': 'error', 'message': 'Invalid world name'})}\n\n"
        return StreamingResponse(error_stream(), media_type="text/event-stream")

    db_path = f"data/{safe_name}.db"

    if Path(db_path).exists():
        async def error_stream():
            yield f"data: {json.dumps({'type': 'error', 'message': f'World already exists: {safe_name}'})}\n\n"
        return StreamingResponse(error_stream(), media_type="text/event-stream")

    async def generation_stream():
        global sessions

        # Clear sessions
        sessions.clear()

        # Reset engine and set new path
        reset_engine()
        set_runtime_db_path(db_path)

        # Initialize fresh database
        init_db(db_path)

        yield f"data: {json.dumps({'type': 'status', 'message': 'Database initialized...'})}\n\n"

        # Setup API keys
        setup_api_keys()

        yield f"data: {json.dumps({'type': 'status', 'message': 'Starting world generation...'})}\n\n"

        try:
            from src.agents.world_forge import WorldForge

            # Unique session per world
            session_id = f"forge_{hashlib.md5(db_path.encode()).hexdigest()[:12]}"
            forge = WorldForge(session_id)

            # Stream the generation
            prompt = f"""Generate a complete game world with the following specifications:

## PREMISE
{request.premise}

## GENRE
{request.genre}

## PLAYER CHARACTER
{request.pc_concept or f"A wanderer exploring this {request.genre} world"}

## REQUIREMENTS
- Create {request.num_factions} factions
- Create {request.num_major_npcs} major NPCs
- Create {request.num_minor_npcs} minor NPCs

Start now. Work through each step.
"""

            async for response in forge.agent.stream_async(prompt):
                if isinstance(response, dict) and "data" in response:
                    token_data = response["data"]
                    if token_data:
                        yield f"data: {json.dumps({'type': 'token', 'data': token_data})}\n\n"

            yield f"data: {json.dumps({'type': 'complete', 'db_path': db_path, 'message': f'World {safe_name} created!'})}\n\n"

        except Exception as e:
            logger.error(f"World creation failed: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            yield "event: end\ndata: {}\n\n"

    return StreamingResponse(
        generation_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


# Mount static files
static_path = Path(__file__).parent / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

# Mount assets directory for generated images
assets_path = Path("data/assets")
assets_path.mkdir(parents=True, exist_ok=True)
app.mount("/assets", StaticFiles(directory=str(assets_path)), name="assets")


def run_server(host: str = "0.0.0.0", port: int = 8000):
    """Run the FastAPI server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port)



if __name__ == "__main__":
    run_server()

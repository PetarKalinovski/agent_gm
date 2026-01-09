"""FastAPI server for Agent GM web frontend."""

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
from src.config import load_settings
from src.models import Location, Player, get_session, init_db
from src.tools.world_read import (
    get_current_location,
    get_player,
    get_world_clock,
    get_active_quests,
)
from src.tools.narration import set_web_output_callback
from src.web.streaming import ToolUsageTracker
from src.agents.callback_context import set_callback_handler, clear_callback_handler

load_dotenv()

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)
# Also make sure strands logging is visible
logging.getLogger("strands").setLevel(logging.DEBUG)

# Initialize
app = FastAPI(title="Agent GM", description="Multi-agent text adventure game")

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


def get_or_create_session(player_id: str) -> dict[str, Any]:
    """Get or create a session with DM and tool tracker."""
    if player_id in sessions:
        return sessions[player_id]

    settings = load_settings()
    setup_api_keys()
    init_db(settings.database.path)

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
    return HTMLResponse("<h1>Agent GM</h1><p>Frontend not found. Run from correct directory.</p>")


@app.get("/api/players")
async def list_players():
    """List all available players."""
    settings = load_settings()
    init_db(settings.database.path)

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
    location = get_current_location(player_id)
    clock = get_world_clock()
    player = get_player(player_id)

    return {
        "player_id": player_id,
        "player_name": player.get("name", "Unknown"),
        "location": location.get("name", "Unknown"),
        "location_description": location.get("description", ""),
        "time": f"Day {clock.get('day', 1)}, {clock.get('hour', 8)}:00",
        "time_of_day": clock.get("time_of_day", "day"),
        "npcs_present": location.get("npcs_present", []),
    }


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
    init_db(settings.database.path)

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


# Mount static files
static_path = Path(__file__).parent / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")


def run_server(host: str = "0.0.0.0", port: int = 8000):
    """Run the FastAPI server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_server()

"""FastAPI server for Forge web frontend."""

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.agents.base import setup_api_keys
from src.agents.dm_orchestrator import DMOrchestrator
from src.config import load_settings, get_active_db_path, set_runtime_db_path
from src.models import Location, NPC, Player, get_session, init_db, reset_engine
from src.models.location import Connection
from src.tools.world_read import (
    get_current_location,
    get_player,
    get_world_clock,
    get_active_quests,
    get_available_destinations,
)
from src.tools.narration import set_web_output_callback
from src.web.streaming import ToolUsageTracker
from src.agents.callback_context import set_callback_handler, clear_callback_handler
from src.services.asset_manager import AssetManager
from src.services.music_generator import MOODS, MusicGenerator
from src.services.voice_generator import VoiceGenerator, compute_audio_id
from src.services.world_tick import snapshot_clock, enforce_minimum_time_cost
from src.services.world_simulation import maybe_advance_world
from src.services.tension import snapshot_turn_tension, enforce_tension
from src.services import appearance as appearance_mod
from src.services import barks as barks_service
from src.services import perception
from src.services import progression
from src.services.campaign import (
    campaign_public_view,
    campaign_state,
    ensure_campaign,
    get_last_tick,
    mark_beat_delivery,
    pop_pending_events,
    reset_state as reset_campaign_state,
)
from src.services.combat import ARCHETYPES as COMBAT_ARCHETYPES, DANGER_TIERS
from src.services.cinematics import drain_pending as drain_pending_cinematics
from src.services.cinematics import recent as recent_cinematics
from src.services.scene_exits import compute_exit_doors
from src.services.scene_objects import normalize_objects, resolve_interaction
from src.services.travel import check_route, travel_now
from src.web import pending_actions
from src.services import objectives as objectives_mod
from src.services import lead as lead_mod
from src.services import opening as opening_mod
from src.services import suggestions as suggestions_mod
from src.services.rest import normalize_rest, rest_now
from src.services import standing
from src.tools.world_write.items import _validate_inventory, transfer_item
from src.web.turn_filters import CodeFenceFilter, DialogueDeduper, JunkLineFilter, SentenceDeduper, speech_events_for_unspoken
from src.web.tool_rescue import rescue_tool_calls, resolve_npc
from src.web.turn_audit import audit_turn, format_audit_feedback

# Last turn's audit warnings per player — injected into the next turn's
# context so the DM repairs (or stops making) unbacked claims
_pending_audit_feedback: dict[str, list] = {}
# What the ENGINE did on the DM's behalf last turn (tool calls it wrote as
# text and the rescue path executed) — the DM must not redo or re-narrate it
_pending_engine_notes: dict[str, list[str]] = {}
# The last combat roster sent to each player that no /api/combat/result has
# answered yet. Seen live: start_combat streamed while the client was still
# loading the new scene, the roster was dropped, and the DM waited for an
# outcome that could never come. The next turn re-issues it instead.
_pending_combat: dict[str, dict] = {}

load_dotenv()

# Set up logging (set FORGE_DEBUG=1 for verbose stream/token logging)
import os as _os
_log_level = logging.DEBUG if _os.environ.get("FORGE_DEBUG") else logging.INFO
logging.basicConfig(level=_log_level)
logger = logging.getLogger(__name__)
logging.getLogger("strands").setLevel(_log_level)

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

# Cap on cached DM sessions — each holds a full conversation history in memory
MAX_SESSIONS = 8

# Serializes /api/play turns (narration/tool callbacks are process-wide)
_play_lock = asyncio.Lock()


class GameRequest(BaseModel):
    """Request model for game input."""
    player_input: str
    player_id: str
    npc_id: str | None = None
    npc_name: str | None = None


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
        sessions[player_id]["last_used"] = time.monotonic()
        return sessions[player_id]

    settings = load_settings()
    setup_api_keys()
    init_db(get_active_db_path())

    # Evict the least-recently-used session if at capacity (DM history is
    # persisted via FileSessionManager, so eviction only costs a reload)
    if len(sessions) >= MAX_SESSIONS:
        oldest = min(sessions, key=lambda k: sessions[k].get("last_used", 0))
        del sessions[oldest]
        logger.info(f"Evicted idle DM session for player {oldest}")

    # Create tool tracker first (must be passed during agent creation)
    tool_tracker = ToolUsageTracker()

    # Create DMOrchestrator with callback_handler
    dm = DMOrchestrator(player_id, callback_handler=tool_tracker)

    sessions[player_id] = {
        "dm": dm,
        "tool_tracker": tool_tracker,
        "last_used": time.monotonic(),
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
                "health_status": p.health_status,
            }
            for p in players
        ]


# Weather is world state, but it is invisible indoors — code decides which
# location types are "inside" so the client never has to know what a room is
INDOOR_LOCATION_TYPES = frozenset({"interior", "room", "building"})

# What a state payload carries for a world with no player row at all
_NO_PLAYER_PROGRESS = {"level": 1, "xp": 0, "xp_into_level": 0, "xp_for_level": 0}
_NO_PLAYER_EQUIPMENT = {slot: None for slot in progression.SLOTS}
_NO_PLAYER_APPEARANCE = {
    "body_sig": appearance_mod.BASE_BODY_SIG,
    "weapon_sig": appearance_mod.BASE_WEAPON_SIG,
    "action_sig": appearance_mod.BASE_ACTION_SIG,
    "version": appearance_mod.APPEARANCE_VERSION,
    "read_as": perception.STYLE_READ_AS[perception.DEFAULT_STYLE],
}

# The mechanical keys a legacy bare-string inventory entry reads as
_PLAIN_ITEM_MECHANICS = {
    "slot": None, "tier": None, "effect_tag": None,
    "equipped": False, "equippable": False, "price": 0,
}


def build_player_state(player_id: str) -> dict[str, Any]:
    """Snapshot of everything the HUD needs — sent as an SSE `state` event
    after each turn and included in /api/session.

    Runs after every successful world-write tool, so everything derived here
    is pure dict math over the already-loaded rows (`progression.combat_stats`
    issues no query) plus the campaign summary's handful of indexed reads.
    """
    location = get_current_location(player_id)
    clock = get_world_clock()

    inventory: list[dict[str, Any]] = []
    with get_session() as db:
        p = db.query(Player).filter(Player.id == player_id).first()
        health = p.health_status if p else "healthy"
        currency = p.currency if p else 0
        for entry in (list(p.inventory or []) if p else []):
            if isinstance(entry, dict):
                item = {
                    "item_id": progression.item_key(entry),
                    "name": entry.get("name", "?"),
                    "quantity": entry.get("quantity", 1),
                    "type": entry.get("type", "misc"),
                    "description": entry.get("description", ""),
                }
                # slot / tier / effect_tag / equipped / equippable / price
                item.update(progression.annotate_item(p, entry))
                inventory.append(item)
            elif isinstance(entry, str):
                inventory.append({
                    "item_id": entry, "name": entry, "quantity": 1,
                    "type": "misc", "description": "", **_PLAIN_ITEM_MECHANICS,
                })
        # Level, gear and combat numbers are the engine's — the client renders
        # them and never derives them from raw items
        progress = progression.xp_progress(p) if p else dict(_NO_PLAYER_PROGRESS)
        equipment = progression.equipment_summary(p) if p else dict(_NO_PLAYER_EQUIPMENT)
        combat = progression.combat_stats(p) if p else None
        # Appearance = the WANTED sprite signatures (pure dict math — appearance
        # ruling A6: readiness never rides the state event) merged with the
        # social reading strangers get (perception profile; one Faction read
        # only when a uniform is actually worn).
        appearance = dict(_NO_PLAYER_APPEARANCE)
        if p:
            appearance = appearance_mod.appearance_payload(p)
            try:
                appearance["read_as"] = perception.profile_for(db, p)["read_as"]
            except Exception as e:
                logger.debug(f"Appearance reading unavailable: {e}")
                appearance["read_as"] = _NO_PLAYER_APPEARANCE["read_as"]

    try:
        quests = [
            {"id": q.get("id"), "title": q.get("title", "?"), "objectives": q.get("objectives") or []}
            for q in (get_active_quests() or []) if isinstance(q, dict)
        ]
    except Exception:
        quests = []

    with get_session() as db:
        from src.models import WorldClock
        wc = db.query(WorldClock).first()
        minute = (wc.minute or 0) if wc else 0

    # Tension drives the adaptive music layer client-side; weather is durable
    # world state, so it survives reloads and scene changes through here
    tension = "low"
    weather = "clear"
    try:
        with get_session() as db:
            from src.models.world_state import DMState
            dm_state = db.query(DMState).first()
            if dm_state:
                tension = dm_state.tension or "low"
                weather = dm_state.weather or "clear"
    except Exception:
        pass

    if str(location.get("type", "")).lower() in INDOOR_LOCATION_TYPES:
        weather = "clear"

    # The compass: one code-chosen next step (quest objective, else beat
    # lead) and the chips that make it, the people here, and the ways out
    # one click each
    lead = None
    suggestions: list[dict[str, Any]] = []
    try:
        with get_session() as db:
            row = db.get(Player, player_id)
            lead = lead_mod.compute_lead(db, row) if row else None
            if row is not None:
                suggestions = suggestions_mod.suggest_actions(db, row, lead, row.current_location_id)
    except Exception as e:
        logger.debug(f"Lead unavailable: {e}")

    # Spoiler-free by construction — never a sealed truth or an unfired beat
    try:
        campaign = campaign_state(player_id)
    except Exception as e:
        logger.debug(f"Campaign state unavailable: {e}")
        campaign = None

    return {
        "location_id": location.get("id"),
        "location": location.get("name", "Unknown"),
        "day": clock.get("day", 1),
        "hour": clock.get("hour", 8),
        "minute": minute,
        "time_of_day": clock.get("time_of_day", "day"),
        "health": health,
        "currency": currency,
        "inventory": inventory,
        "quests": quests,
        "tension": tension,
        "level": progress["level"],
        "xp": progress["xp"],
        "xp_into_level": progress["xp_into_level"],
        "xp_for_level": progress["xp_for_level"],
        "equipment": equipment,
        "combat": combat,
        "appearance": appearance,
        "campaign": campaign,
        "lead": lead,
        "suggestions": suggestions,
        "weather": weather,
    }


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
        "location_id": location.get("id"),
        "location_description": location.get("description", ""),
        "time": f"Day {clock.get('day', 1)}, {clock.get('hour', 8)}:00",
        "time_of_day": clock.get("time_of_day", "day"),
        "npcs_present": location.get("npcs_present", []),
        "state": build_player_state(player_id),
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


RECAP_PROMPT = """You write "Previously..." recaps for a text RPG the player is
resuming after time away. From the transcript, write 2-4 sentences in second
person ("you") capturing: where the player is, what they were pursuing, and
any unresolved thread or looming danger. Write ONLY the recap text — no
preamble, no headers, no meta commentary. Match the world's tone."""


@app.post("/api/opening/{player_id}")
async def run_opening(player_id: str):
    """A brand-new character's first moment: title card + scene + the
    `[OPENING]` directive the client auto-sends. `{"opening": False}` once
    the DM has any history, so a refresh mid-opening cannot double-fire."""
    init_db(get_active_db_path())
    session = await asyncio.to_thread(get_or_create_session, player_id)
    if not opening_mod.is_first_turn(session["dm"]):
        return {"opening": False}
    await asyncio.to_thread(opening_mod.wait_for_campaign, player_id)
    payload = await asyncio.to_thread(opening_mod.opening_payload, player_id)
    if not payload:
        return {"opening": False}
    logger.info(f"Opening sequence for {player_id}: card={'yes' if payload.get('cinematic') else 'no'}")
    return payload


@app.get("/api/recap/{player_id}")
async def get_session_recap(player_id: str):
    """A short 'Previously...' recap of recent play, for resuming a session."""
    history = await get_chat_history(player_id, limit=40)
    messages = history.get("messages", [])
    if len(messages) < 6:
        return {"recap": None}

    lines = []
    for m in messages[-30:]:
        who = "Player" if m.get("role") == "user" else "DM"
        lines.append(f"{who}: {m.get('content', '')}")
    transcript = "\n".join(lines)[-6000:]

    def _run() -> str:
        from src.agents.base import create_agent
        agent = create_agent("recap", RECAP_PROMPT, tools=[], callback_handler=None)
        return str(agent(f"Transcript of recent play:\n\n{transcript}"))

    try:
        recap = (await asyncio.to_thread(_run)).strip()
        return {"recap": recap or None}
    except Exception as e:
        logger.warning(f"Recap generation failed: {e}")
        return {"recap": None}


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


# Tools whose success means the HUD is stale — a mid-turn state snapshot
# follows each one so damage/gold/items show when they land, not at turn end
STATE_MUTATING_TOOLS = frozenset({
    "update_player_health",
    "add_to_inventory",
    "remove_from_inventory",
    "transfer_item",
    "adjust_currency",
    "activate_quest",
    "update_quest_status",
    "update_quest_objectives",
    "create_quest",
    "move_player",
    "advance_time",
    "update_player_reputation",
    "use_item",
    # A merchant's stock does not move the HUD, but its catalog prices echo
    # into the next get_inventory — keeping the set honest costs nothing
    "stock_merchant",
})


def _drain_story_events(player_id: str) -> list[str]:
    """SSE frames for everything the story engine queued out-of-band.

    Campaign beats/acts/resolutions and campaign-deferred cinematics fire in
    pure code (inside the world tick, or in `mark_beat_delivery` after the
    turn), so they have no tool result to ride on. Drained twice per turn:
    once after the context build, once after delivery marking.
    """
    frames: list[str] = []
    try:
        for event in pop_pending_events(player_id):
            frames.append(f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n")
    except Exception as e:
        logger.warning(f"Campaign event drain failed: {e}")
    try:
        for event in objectives_mod.pop_pending_events(player_id):
            frames.append(f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n")
    except Exception as e:
        logger.warning(f"Objective event drain failed: {e}")
    try:
        for cine in drain_pending_cinematics(player_id):
            payload = {"type": "cinematic", **cine}
            frames.append(f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n")
    except Exception as e:
        logger.warning(f"Cinematic queue drain failed: {e}")
    return frames


@app.post("/api/play")
async def play_sse(request: GameRequest):
    """Stream game response via SSE using stream_async pattern.

    Turns are serialized under a lock: the narration/tool-tracking callbacks
    are process-wide (they must survive thread boundaries inside the agent
    runtime), so two concurrent turns would cross-wire each other's output
    and corrupt the shared DM conversation history. A second request queues
    until the current turn finishes.
    """
    session = get_or_create_session(request.player_id)
    dm: DMOrchestrator = session["dm"]
    tool_tracker: ToolUsageTracker = session["tool_tracker"]

    async def event_stream():
        """Generate SSE events from agent stream."""
        async with _play_lock:
            async for chunk in _run_turn():
                yield chunk

    async def _run_turn():
        try:
            # All setup happens under the lock — a queued second turn must
            # see the world state the first turn left behind
            tool_tracker.reset()

            # Snapshot the clock so we can enforce a minimum time cost after the turn
            turn_start_clock = snapshot_clock()
            # Snapshot health+tension so danger can mechanically raise tension
            turn_start_tension = snapshot_turn_tension(request.player_id)

            # Pre-turn state snapshot: the audit diffs it against post-turn
            try:
                state_before = build_player_state(request.player_id)
            except Exception:
                state_before = None

            # Strips DM re-quotes of lines already delivered as speech events
            deduper = DialogueDeduper()
            # Holds code fences back from the player (a tool call typed as
            # text); their contents are executed by the rescue path below
            fence = CodeFenceFilter()
            # Drops tool-result echoes ("[]", content-block JSON) line by line
            junk = JunkLineFilter()
            # Drops a paragraph the player already read this turn (narrate()
            # output repeated as the reply, or the reverse)
            sentences = SentenceDeduper()
            turn_text_parts: list[str] = []
            last_state_json: str | None = None

            # The campaign tick caches its briefing per player; hold the old
            # object so a turn where it did NOT run can be told apart from one
            # where it did (a resolved campaign stops ticking, and its final
            # beat must not keep exempting the audit forever)
            previous_tick = get_last_tick(request.player_id)

            # A roster the client never fought (dropped mid scene-load, a
            # refresh, a crash) is fought NOW — the DM already narrated the
            # fight breaking out and is waiting for its outcome.
            stale_roster = _pending_combat.get(request.player_id)
            if stale_roster and "[COMBAT OUTCOME]" not in (request.player_input or "")[:40]:
                logger.warning("Re-issuing a combat roster the client never fought")
                yield f"data: {json.dumps({'type': 'narration', 'content': 'The fight is not over.'})}\n\n"
                yield f"data: {json.dumps(stale_roster, ensure_ascii=False, default=str)}\n\n"
                yield f"data: {json.dumps({'type': 'complete', 'tools': [], 'audit': []})}\n\n"
                return

            # What the ENGINE did between turns — canvas travel, pickups,
            # potions, gear, growth. Drained under the play lock; the DM reads
            # it as already-true facts and must not re-apply any of it.
            canvas_actions = pending_actions.drain(request.player_id)
            canvas_notes = pending_actions.format_pending_actions(canvas_actions)
            code_actions = pending_actions.tool_equivalents(canvas_actions)

            # Build the full DM context — runs the world tick (fires scheduled
            # events) and the campaign tick, and includes player
            # health/inventory/quests, same as the CLI path. Block order (R11)
            # is the orchestrator's: briefing -> canvas ledger -> campaign.
            context = dm._build_context(request.player_input, canvas_notes=canvas_notes)

            # If the player clicked an NPC on the canvas, tell the DM who they're addressing
            if request.npc_id:
                npc_label = f"{request.npc_name} (npc_id={request.npc_id})" if request.npc_name else f"npc_id={request.npc_id}"
                context += f"\n\n[The player is currently addressing {npc_label} — route dialogue to this NPC via prompt_npc_agent.]"

            # Last turn's audit violations feed back so the DM self-corrects
            prior_warnings = _pending_audit_feedback.pop(request.player_id, None)
            if prior_warnings:
                context += format_audit_feedback(prior_warnings)
            engine_notes = _pending_engine_notes.pop(request.player_id, None)
            if engine_notes:
                context += (
                    "\n\n[ENGINE NOTE — already happened, do not redo or repeat]\n- "
                    + "\n- ".join(engine_notes)
                )

            # A campaign beat that fired inside _build_context already moved
            # NPCs, flipped quests and wrote events in code. Its exemption
            # categories join the ledger's tool names as the ONE suppression
            # mechanism the reality audit reads (R4).
            campaign_tick = get_last_tick(request.player_id) or {}
            if campaign_tick is previous_tick:
                campaign_tick = {}          # the tick did not run this turn
            campaign_audit = campaign_tick.get("audit")
            code_actions |= set((campaign_tick.get("fired_beat") or {}).get("audit_exempt") or ())

            # Flush whatever the tick queued (the beat itself, an act it just
            # resolved) and any cinematic it deferred, BEFORE the first token.
            for chunk in _drain_story_events(request.player_id):
                yield chunk

            # Set up narration callback
            narration_buffer: list[str] = []

            def narration_callback(text: str):
                narration_buffer.append(text)

            set_web_output_callback(narration_callback)

            # Set callback handler in context so sub-agents can use it
            set_callback_handler(tool_tracker)

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
                    if notification.get("type") == "speech":
                        try:
                            _enrich_speech_event(notification)
                        except Exception as e:
                            logger.warning(f"Speech TTS enrichment failed: {e}")
                        deduper.register_speech(notification.get("text"))
                    elif notification.get("type") == "combat_start":
                        _pending_combat[request.player_id] = dict(notification)
                    encoded = json.dumps(notification, ensure_ascii=False, default=str)
                    yield f"data: {encoded}\n\n"

                    # A world-write tool just succeeded → push a fresh state
                    # snapshot so the HUD updates mid-turn (damage shows when
                    # it lands, not when the turn ends)
                    if (
                        notification.get("type") == "tool_update"
                        and notification.get("tool", {}).get("status") == "success"
                        and notification.get("tool", {}).get("name") in STATE_MUTATING_TOOLS
                    ):
                        try:
                            mid_state = build_player_state(request.player_id)
                            mid_json = json.dumps(mid_state, ensure_ascii=False, default=str)
                            if mid_json != last_state_json:
                                last_state_json = mid_json
                                yield f'data: {{"type": "state", "state": {mid_json}}}\n\n'
                        except Exception as e:
                            logger.debug(f"Mid-turn state snapshot failed: {e}")

                # Yield token data if present (deduper holds text back only
                # until a safe sentence boundary, and only after speech)
                if isinstance(response, dict) and "data" in response:
                    token_data = response["data"]
                    if token_data:
                        released = sentences.filter(deduper.feed(junk.feed(fence.feed(token_data))))
                        if released:
                            turn_text_parts.append(released)
                            token_event = {"type": "token", "data": released}
                            yield f"data: {json.dumps(token_event)}\n\n"

                # Yield any narration output
                while narration_buffer:
                    text = sentences.filter(deduper.filter_block(narration_buffer.pop(0)))
                    if not text.strip():
                        continue
                    logger.debug(f"Narration: {text[:50]}...")
                    turn_text_parts.append(text)
                    narration_event = {"type": "narration", "content": text}
                    yield f"data: {json.dumps(narration_event)}\n\n"

            logger.info(f"Stream finished with {event_count} events")

            # Release any text still held by the filters (fence first — a
            # held backtick or an unterminated fence — then the deduper)
            held = junk.feed(fence.flush()) + junk.flush()
            if held:
                released = sentences.filter(deduper.feed(held))
                if released:
                    turn_text_parts.append(released)
                    yield f"data: {json.dumps({'type': 'token', 'data': released})}\n\n"
            tail = sentences.filter(deduper.flush())
            if tail.strip():
                turn_text_parts.append(tail)
                yield f"data: {json.dumps({'type': 'token', 'data': tail})}\n\n"

            # Tool calls the DM typed instead of making: execute the dialogue
            # ones now, stream their speech, and tell the DM next turn
            rescued = []
            if fence.fences:
                try:
                    rescued = await asyncio.to_thread(
                        rescue_tool_calls, fence.fences, request.player_id, request.npc_id
                    )
                except Exception as e:
                    logger.warning(f"Tool rescue failed: {e}")
            for index, record in enumerate(rescued):
                pill = {
                    "id": f"rescue-{index}", "name": record["tool"], "status": "success",
                    "is_subagent": record["tool"] == "prompt_npc_agent",
                    "output": record.get("output") or "",
                }
                yield f"data: {json.dumps({'type': 'tool_update', 'tool': pill}, ensure_ascii=False)}\n\n"
                for event in record.get("events") or []:
                    notification = {
                        "type": "speech", "npc_name": event.get("npc"),
                        "text": event.get("text"), "tone": event.get("tone") or "normal",
                        "action": event.get("action"),
                    }
                    try:
                        _enrich_speech_event(notification)
                    except Exception as e:
                        logger.warning(f"Speech TTS enrichment failed: {e}")
                    deduper.register_speech(notification.get("text"))
                    yield f"data: {json.dumps(notification, ensure_ascii=False, default=str)}\n\n"
                _pending_engine_notes.setdefault(request.player_id, []).append(record["note"])
                logger.warning(f"Tool rescue: executed {record['tool']} the DM wrote as text")

            # If the DM forgot to advance time this turn, apply a minimum cost
            # so the world clock (and scheduled events) can't freeze.
            time_result = enforce_minimum_time_cost(turn_start_clock)
            if time_result.get("enforced"):
                logger.info("Minimum time cost enforced for this turn")

            # Tension floor/decay from what measurably happened this turn —
            # the state event below carries the enforced value to the music layer
            try:
                enforce_tension(turn_start_tension, request.player_id)
            except Exception as e:
                logger.warning(f"Tension enforcement failed: {e}")

            # A new in-game day? Advance the world in the background —
            # factions move, consequences ripple, all offscreen
            asyncio.create_task(asyncio.to_thread(maybe_advance_world))

            # Post-turn state snapshot so the HUD updates without polling
            state_after = None
            try:
                state_after = build_player_state(request.player_id)
                state_event = {"type": "state", "state": state_after}
                yield f"data: {json.dumps(state_event, ensure_ascii=False, default=str)}\n\n"
            except Exception as e:
                logger.warning(f"Could not build state event: {e}")

            # Did the DM actually deliver the beat the engine gave it? Runs
            # BEFORE the audit so a delivered beat is never flagged, and may
            # itself close an act (queueing more events + a title card).
            turn_text = "".join(turn_text_parts)
            try:
                mark_beat_delivery(request.player_id, turn_text)
            except Exception as e:
                logger.warning(f"Campaign delivery marking failed: {e}")
            for chunk in _drain_story_events(request.player_id):
                yield chunk

            # Lines the DM wrote as prose for the NPC in front of the player
            # are voiced anyway (voice + bubble; the prose already shows the
            # words, so the client adds no second chat line). The audit below
            # still flags the habit so the DM learns to use speak.
            try:
                for notification in _synthesized_speech(request, turn_text, deduper):
                    yield f"data: {json.dumps(notification, ensure_ascii=False, default=str)}\n\n"
            except Exception as e:
                logger.warning(f"Synthesized speech failed: {e}")

            # Fiction/database consistency audit — logged and attached to
            # the complete event (self-playtests read it as a scoreboard)
            audit_warnings = []
            try:
                # Objectives the engine ticked during THIS turn are code
                # actions too — the DM narrating them is not a fabrication
                code_actions = set(code_actions or ()) | objectives_mod.pop_audit_tools(request.player_id)
                audit_warnings = audit_turn(
                    turn_text, tool_tracker.snapshot(), state_before, state_after,
                    code_actions=code_actions, campaign=campaign_audit,
                )
                if junk.dropped:
                    audit_warnings.append({
                        "check": "tool_result_in_prose",
                        "detail": (
                            "The response pasted raw tool output into the narration "
                            f"({len(junk.dropped)} fragment(s) such as {junk.dropped[0][:60]!r}). "
                            "The engine removed them. Tool results are for YOU to read; the "
                            "player only ever sees prose."
                        ),
                    })
                if fence.fences:
                    audit_warnings.append({
                        "check": "tool_call_in_prose",
                        "detail": (
                            "The response contained a fenced code block — a tool call written "
                            "as text. The player never sees code; the engine hid it and executed "
                            "what it could. CALL tools; never type them into the narration."
                        ),
                    })
                for w in audit_warnings:
                    logger.warning(f"Turn audit [{w['check']}]: {w['detail']}")
                if audit_warnings:
                    _pending_audit_feedback[request.player_id] = audit_warnings
            except Exception as e:
                logger.warning(f"Turn audit failed: {e}")

            # Final payload with tool summary
            final_payload = {
                "type": "complete",
                "tools": tool_tracker.snapshot(),
                "audit": audit_warnings,
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

# Asset managers keyed by world name — a singleton would stay bound to the
# first world after a world switch and write assets to the wrong directory
_asset_managers: dict[str, AssetManager] = {}


def _get_world_name() -> str:
    """Derive world name from the active database path."""
    db_path = get_active_db_path()
    return Path(db_path).stem


def get_asset_manager() -> AssetManager:
    """Get or create the asset manager for the active world."""
    world_name = _get_world_name()
    if world_name not in _asset_managers:
        _asset_managers[world_name] = AssetManager(world_name)
    return _asset_managers[world_name]


# Audio generators, keyed by world name like the asset managers
_voice_generators: dict[str, VoiceGenerator] = {}
_music_generators: dict[str, MusicGenerator] = {}


def get_voice_generator() -> VoiceGenerator:
    world_name = _get_world_name()
    if world_name not in _voice_generators:
        _voice_generators[world_name] = VoiceGenerator(world_name)
    return _voice_generators[world_name]


def get_music_generator() -> MusicGenerator:
    world_name = _get_world_name()
    if world_name not in _music_generators:
        _music_generators[world_name] = MusicGenerator(world_name)
    return _music_generators[world_name]


def _synthesized_speech(request, turn_text: str, deduper: DialogueDeduper) -> list[dict[str, Any]]:
    """Speech events for quoted NPC lines the DM wrote inline this turn.

    The speaker is the NPC the player addressed (canvas click), else the
    only NPC present; with nobody to attribute to, nothing is voiced.
    """
    speaker = (getattr(request, "npc_name", None) or "").strip()
    with get_session() as db:
        if not speaker and getattr(request, "npc_id", None):
            npc = db.get(NPC, request.npc_id)
            speaker = npc.name if npc else ""
        if not speaker:
            player = db.get(Player, request.player_id)
            if player and player.current_location_id:
                present = [
                    n for n in db.query(NPC).filter(
                        NPC.current_location_id == player.current_location_id
                    ).all()
                    if str(getattr(n, "status", "alive") or "alive").lower().endswith("alive")
                ]
                if len(present) == 1:
                    speaker = present[0].name
    if not speaker:
        return []

    out: list[dict[str, Any]] = []
    for event in speech_events_for_unspoken(turn_text, deduper.spoken, speaker, synthesized=True):
        notification = {
            "type": "speech",
            "npc_name": event["npc"],
            "text": event["text"],
            "tone": "normal",
            "action": None,
            "synthesized": True,
        }
        try:
            _enrich_speech_event(notification)
        except Exception as e:
            logger.warning(f"Speech TTS enrichment failed: {e}")
        logger.info(f"Synthesized speech for {speaker}: {event['text'][:60]!r}")
        out.append(notification)
    return out


def _enrich_speech_event(notification: dict[str, Any]) -> None:
    """Resolve the speaking NPC and kick off background TTS for the line.

    Adds npc_id, audio_id, and audio_ready to the speech SSE event; the
    client polls /api/assets/voice/{audio_id} until the clip exists.
    """
    settings = load_settings()
    if not settings.audio.tts_enabled:
        return

    npc_name = (notification.get("npc_name") or "").strip()
    text = (notification.get("text") or "").strip()
    tone = notification.get("tone") or "normal"
    if not npc_name or not text:
        return

    with get_session() as db:
        npc = db.query(NPC).filter(NPC.name.ilike(npc_name)).first()
        npc_id = npc.id if npc else None
    if not npc_id:
        return
    notification["npc_id"] = npc_id

    voice_gen = get_voice_generator()
    if not voice_gen.available() or len(text) > settings.audio.max_tts_chars:
        return

    audio_id = compute_audio_id(npc_id, text, tone)
    notification["audio_id"] = audio_id
    if voice_gen.line_path(audio_id):
        notification["audio_ready"] = True
    else:
        notification["audio_ready"] = False
        get_asset_manager().spawn_generation(
            f"voice:{audio_id}",
            lambda: voice_gen.generate_line(npc_id, text, tone, audio_id),
        )


@app.get("/api/assets/voice/{audio_id}")
async def get_voice_line(audio_id: str):
    """Serve a generated TTS clip; 404 with pending status while generating."""
    if not re.fullmatch(r"[0-9a-f]{6,40}", audio_id):
        return JSONResponse({"error": "bad audio id"}, status_code=400)
    path = get_voice_generator().line_path(audio_id)
    if path:
        media = "audio/mpeg" if path.suffix == ".mp3" else "audio/wav"
        return FileResponse(str(path), media_type=media)
    return JSONResponse({"status": "pending"}, status_code=404)


@app.get("/api/assets/music/manifest")
async def get_music_manifest():
    """Mood -> track manifest for the active world.

    Calling this also kicks off (deduplicated) background generation of
    any missing palette tracks, so the frontend polls it while pending.
    """
    settings = load_settings()
    if not settings.audio.music_enabled:
        return {"enabled": False, "moods": {}, "generating": False}

    music_gen = get_music_generator()
    asset_manager = get_asset_manager()

    generating = False
    if music_gen.api_key and music_gen.missing_moods():
        generating = asset_manager.spawn_generation("music:palette", music_gen.generate_palette)

    moods = {}
    for mood in MOODS:
        path = music_gen.track_path(mood)
        if path:
            status = "ready"
        elif generating:
            status = "generating"
        elif music_gen.api_key:
            status = "missing"  # recent failure cooldown — retried on a later poll
        else:
            status = "unavailable"  # no API key configured
        moods[mood] = {
            "status": status,
            "url": asset_manager.get_asset_url(str(path)) if path else None,
        }

    return {"enabled": True, "moods": moods, "generating": generating}


@app.get("/api/assets/location/{location_id}")
async def get_location_assets(location_id: str, player_id: str):
    """Get all assets needed to render a location.

    Returns background, walkable bounds, player sprite, and NPC sprites.
    """
    get_or_create_session(player_id)  # Ensure DB is initialized
    asset_manager = get_asset_manager()

    def _url(path):
        return asset_manager.get_asset_url(path) if path else None

    try:
        # Never blocks: cached assets return immediately, misses generate in
        # background tasks and the client polls while "pending" is true
        assets = await asset_manager.get_location_assets_fast(location_id, player_id)

        # Pre-warm backgrounds for connected locations so travel feels instant
        asset_manager.prewarm_connected_locations(location_id)

        # Available exits so the canvas can draw doors at scene edges. Doors
        # are DERIVED (never stored) from the destination's bearing on the
        # world map, and each carries the same route verdict `move_player`
        # would give — the canvas can never walk somewhere the DM would block.
        try:
            destinations = get_available_destinations(location_id) or []
            exits = []
            seen_exit_ids = set()
            with get_session() as db:
                loc = db.query(Location).filter(Location.id == location_id).first()
                # A dress code is a door policy: the player row rides into
                # check_route so a dress-locked destination renders locked
                # with the house's own reason in `requirements`
                player_row = db.query(Player).filter(Player.id == player_id).first()
                doors = compute_exit_doors(loc, destinations)
                for d in destinations:
                    if not isinstance(d, dict) or not d.get("id") or d["id"] in seen_exit_ids:
                        continue
                    seen_exit_ids.add(d["id"])
                    route = check_route(db, location_id, d["id"], player_row)
                    exits.append({
                        "id": d["id"],
                        "name": d.get("name"),
                        "type": d.get("type", ""),
                        "travel_type": d.get("travel_type", ""),
                        "travel_time_hours": route["travel_time_hours"],
                        "locked": route["blocked"],
                        "requirements": route["requirements"],
                        "door": doors.get(d["id"]),      # None past MAX_DOORS
                        # Map bearing, so the lead's edge arrow can point at a
                        # destination that has no door in this scene
                        "position": d.get("position"),
                    })
                location_position = (
                    {"x": loc.position_x, "y": loc.position_y} if loc else None
                )
        except Exception as e:
            logger.warning(f"Exit enrichment failed for {location_id}: {e}")
            exits = []
            location_position = None

        # Convert paths to URLs
        return {
            "location_id": assets["location_id"],
            "location_name": assets["location_name"],
            "background_url": _url(assets["background_path"]),
            "walkable_bounds": assets["walkable_bounds"],
            "obstacles": assets.get("obstacles", []),
            "objects": assets.get("objects", []),
            "exits": exits,
            "location_position": location_position,
            # What to load right now (`serving_*`, guaranteed on disk) and
            # what to aim for (`body_sig`/`action_sig`) — same shape as
            # GET /api/assets/appearance/{player_id}, saves a round-trip
            "appearance": assets.get("appearance"),
            "pending": assets.get("pending", False),
            # The client's buffering gate: hold the loading overlay until the
            # scene is playable, not merely visible (background + player set +
            # a body for every present NPC)
            "scene_ready": assets.get("scene_ready", True),
            "readiness": assets.get("readiness"),
            "player": {
                "id": assets["player"]["id"],
                "name": assets["player"]["name"],
                "x": assets["player"]["x"],
                "y": assets["player"]["y"],
                "scale": assets["player"].get("scale", 1.0),
                "status": assets["player"].get("status", "healthy"),
                "direction": assets["player"]["direction"],
                "sprite_url": _url(assets["player"]["sprite_path"])
            },
            "npcs": [
                {
                    "id": npc["id"],
                    "name": npc["name"],
                    "x": npc["x"],
                    "y": npc["y"],
                    "scale": npc.get("scale", 1.0),
                    "status": npc.get("status", "alive"),
                    "sprite_url": _url(npc["sprite_path"]),
                    "tier": npc["tier"]
                }
                for npc in assets["npcs"]
            ]
        }
    except Exception as e:
        logger.error(f"Error getting location assets: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


# Which sprite set a frame belongs to: attack frames ride the ACTION axis
# (the armor+weapon pair); everything else — idles, walks, dodge — is BODY
# (appearance ruling A3: a roll shows the outfit, never the weapon).
_ACTION_FRAME_RE = re.compile(r"^attack\d+$")
_COMBAT_FRAME_RE = re.compile(r"^(attack|dodge)\d+$")


@app.get("/api/assets/sprite/{character_type}/{character_id}/{direction}")
async def get_sprite(character_type: str, character_id: str, direction: str,
                     a: str | None = None):
    """Serve a character sprite from cache.

    `?a=<signature>` is STRICT (appearance ruling A7): exactly that
    signature's file is served, or a 404 spawns its generation — which makes
    `?a=` a real cache-buster. Without it the endpoint is LENIENT: it
    resolves the player's SERVING signature (the wanted one if its whole set
    is on disk, else base). NPCs are always base in v1 — `a` is ignored.

    On a miss the right axis' set generates in a deduplicated background
    task and 404 is returned — the client shows a placeholder and retries.
    Never blocks the request on image generation, and never returns JSON
    with HTTP 200 where an <img> expects a PNG.
    """
    # Strip extension
    if "." in direction:
        direction = direction.split(".")[0]

    # A cold process (first request after start) has no engine yet; the
    # background generation this may spawn reads the character row
    init_db(get_active_db_path())
    asset_manager = get_asset_manager()
    is_player = character_type == "player"
    axis = "action" if _ACTION_FRAME_RE.match(direction) else "body"
    try:
        if not is_player:
            # NPCs have no equipment model yet (appearance spec §13)
            sig = appearance_mod.BASE_ACTION_SIG if axis == "action" \
                else appearance_mod.BASE_BODY_SIG
        elif a is not None:
            if not appearance_mod.is_valid_signature(a):
                return JSONResponse({"error": "bad signature"}, status_code=404)
            sig = a
        else:
            # LENIENT: one indexed Player read, scene-load only — steady-state
            # rendering always passes `?a=` and never touches the database
            try:
                wanted = asset_manager.wanted_appearance(character_id)
            except Exception as e:
                logger.debug(f"Lenient sprite resolution fell back to base: {e}")
                wanted = appearance_mod.base_payload()
            sig = (
                asset_manager.serving_action_sig(character_id, wanted["action_sig"])
                if axis == "action"
                else asset_manager.serving_body_sig(character_id, wanted["body_sig"])
            )

        sprite_path = asset_manager.sprite_dir(sig) / f"{character_id}_{direction}.png"
        if sprite_path.exists() and sprite_path.stat().st_size > 0:
            return FileResponse(str(sprite_path), media_type="image/png")

        # Miss: spawn the owning set. The dedup key carries the signature —
        # one in-flight task per (character, signature, axis).
        if is_player and axis == "action":
            asset_manager.spawn_generation(
                asset_manager.action_key(character_id, sig),
                lambda: asset_manager.ensure_attack_sprites(character_id, "player", sig=sig),
            )
        elif is_player:
            # Body axis owns idles, walks AND dodge (ruling A3) — a dodge 404
            # on a legacy world spawns body:{id}:base, which fills the gap
            asset_manager.spawn_generation(
                asset_manager.body_key(character_id, sig),
                lambda: asset_manager.ensure_body_set(character_id, sig),
            )
        elif _COMBAT_FRAME_RE.match(direction):
            # NPC combat frames stay their own lazy set (attack only in v1)
            asset_manager.spawn_generation(
                asset_manager.action_key(character_id, sig),
                lambda: asset_manager.ensure_attack_sprites(character_id, character_type),
            )
        else:
            # NPC full-set miss — same key get_location_assets_fast uses
            asset_manager.spawn_generation(
                f"sprites:{character_id}",
                lambda: asset_manager.ensure_all_sprites_generated(character_id, character_type),
            )
        return JSONResponse({"status": "generating", "sig": sig}, status_code=404)
    except Exception as e:
        logger.error(f"Error getting sprite: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/assets/appearance/{player_id}")
async def get_appearance_status(player_id: str):
    """Readiness of the player's wanted sprite sets — both axes, one answer.

    Polled by the client only between a gear change and the texture swap
    (5 s cadence, capped). Requesting it also re-spawns any missing set, so a
    client that reconnects mid-generation restarts a task a server restart
    dropped. Idempotent by construction.
    """
    init_db(get_active_db_path())
    try:
        return get_asset_manager().appearance_status(player_id)
    except Exception as e:
        logger.error(f"Appearance status failed: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/assets/portrait/{npc_id}")
async def get_portrait(npc_id: str):
    """Serve an NPC portrait from cache; generate in background on miss."""
    asset_manager = get_asset_manager()

    try:
        with get_session() as db:
            # The id often arrives as whatever the DM typed into a tool call
            # ("brann_coldiron"); the "considers…" portrait must still resolve
            npc = resolve_npc(db, npc_id, None)
            cached = npc.portrait_path if npc else None
            if npc:
                npc_id = npc.id

        if not npc:
            return JSONResponse({"error": "NPC not found"}, status_code=404)
        if cached and Path(cached).exists():
            return FileResponse(cached, media_type="image/png")

        asset_manager.spawn_generation(
            f"portrait:{npc_id}",
            lambda: asset_manager.get_npc_portrait(npc_id),
        )
        return JSONResponse({"status": "generating"}, status_code=404)
    except Exception as e:
        logger.error(f"Error getting portrait: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


class CombatResultRequest(BaseModel):
    """Outcome of a real-time canvas fight, reported by the frontend.

    `hearts_lost` and `hearts_healed` are the honest severity: a 3-heart boss
    slam is not "one hit", and a potion drunk mid-fight is a wound that did
    not stick. Both default to 0, so an un-updated client keeps working off
    `hits_taken` alone.
    """
    player_id: str
    outcome: str                        # "victory" | "defeat" | "fled"
    hits_taken: int = 0
    hearts_lost: int = 0
    hearts_healed: int = 0
    defeated_npc_ids: list[str] = []
    defeated_positions: dict[str, dict[str, float]] = {}   # where each enemy fell


@app.post("/api/combat/result")
async def combat_result(request: CombatResultRequest):
    """Apply a finished real-time fight to the world — code enforces.

    Health mapping, NPC deaths and the XP those deaths earned land here,
    BEFORE the DM sees anything; the frontend then auto-sends the returned
    `dm_message` through /api/play so the DM narrates an aftermath that is
    already true. Growth rides this response, never SSE.
    """
    from src.services.combat import apply_combat_result, combat_outcome_message

    _pending_combat.pop(request.player_id, None)

    init_db(get_active_db_path())
    changes = apply_combat_result(
        request.player_id,
        request.outcome,
        hits_taken=request.hits_taken,
        hearts_lost=request.hearts_lost,
        hearts_healed=request.hearts_healed,
        defeated_npc_ids=request.defeated_npc_ids,
        defeated_positions=request.defeated_positions,
    )
    if "error" in changes:
        return JSONResponse(changes, status_code=400)

    # A level earned in a canvas fight is news the DM must acknowledge once,
    # obliquely — the ledger carries the instruction into the next turn
    if changes.get("level_up"):
        pending_actions.record_action(request.player_id, "levelup", "")

    response: dict[str, Any] = {
        "success": True,
        "changes": changes,
        "dm_message": combat_outcome_message(changes),
        "state": build_player_state(request.player_id),
        "objectives_done": objectives_mod.pop_pending_events(request.player_id),
        # Bodies to sparkle on the canvas without a scene reload
        "bodies": changes.get("bodies") or [],
    }
    if changes.get("progression"):
        response["progression"] = changes["progression"]
    return response


# =====================
# Progression: gear, quick slot, consumables (HUD, zero DM latency)
# =====================

class EquipRequest(BaseModel):
    item_id: str


class UnequipRequest(BaseModel):
    slot: str


class QuickSlotRequest(BaseModel):
    item_id: str | None = None


class UseConsumableRequest(BaseModel):
    item_id: str
    in_combat: bool = False


def _prewarm_appearance(player_id: str) -> None:
    """Start background generation for the player's new look.

    Adds no latency: two deduplicated `spawn_generation` calls that return
    immediately. Every other path (DM auto-equip, quest rewards, campaign
    ops) self-heals through the sprite 404 trigger — this is a head start,
    not a requirement.
    """
    try:
        manager = get_asset_manager()
        wanted = manager.wanted_appearance(player_id)
        manager.spawn_missing_appearance(player_id, wanted["body_sig"], wanted["action_sig"])
    except Exception as e:
        logger.debug(f"Appearance pre-warm skipped for {player_id}: {e}")


@app.post("/api/player/{player_id}/equip")
async def equip_item(player_id: str, request: EquipRequest):
    """Wear/wield an inventory item. The item stays in the bag — equipment is
    a pointer, so every existing inventory path keeps working."""
    init_db(get_active_db_path())
    with get_session() as db:
        player = db.query(Player).filter(Player.id == player_id).first()
        if not player:
            return JSONResponse({"error": "Player not found", "reason": "missing"}, status_code=404)
        read_before = perception.profile_for(db, player)["read_as"]
        result = progression.equip(db, player, request.item_id)
        if "error" in result:
            return JSONResponse(result, status_code=400)
        read_after = perception.profile_for(db, player)["read_as"]
        db.commit()

    pending_actions.record_action(
        player_id, "equip",
        pending_actions.equip_note(
            result["item_name"], result["slot"],
            # The social consequence rides the note ONLY when the piece
            # actually changed how strangers read them
            reads_as=read_after if read_after != read_before else None,
        ),
    )
    _prewarm_appearance(player_id)
    return {**result, "state": build_player_state(player_id)}


@app.post("/api/player/{player_id}/unequip")
async def unequip_item(player_id: str, request: UnequipRequest):
    """Clear one equipment slot."""
    init_db(get_active_db_path())
    with get_session() as db:
        player = db.query(Player).filter(Player.id == player_id).first()
        if not player:
            return JSONResponse({"error": "Player not found", "reason": "missing"}, status_code=404)
        read_before = perception.profile_for(db, player)["read_as"]
        result = progression.unequip(db, player, request.slot)
        if "error" in result:
            return JSONResponse(result, status_code=400)
        read_after = perception.profile_for(db, player)["read_as"]
        db.commit()

    if result.get("item_name"):
        note = pending_actions.unequip_note(result["item_name"])
        if read_after != read_before:
            note += f" To strangers they now read as {read_after}."
        pending_actions.record_action(player_id, "equip", note)
    _prewarm_appearance(player_id)
    return {**result, "state": build_player_state(player_id)}


@app.post("/api/player/{player_id}/quick-slot")
async def set_quick_slot(player_id: str, request: QuickSlotRequest):
    """Bind (or clear) the Q hotkey. No note to the DM — a hotkey is not fiction."""
    init_db(get_active_db_path())
    with get_session() as db:
        player = db.query(Player).filter(Player.id == player_id).first()
        if not player:
            return JSONResponse({"error": "Player not found", "reason": "missing"}, status_code=404)
        if request.item_id:
            item = progression.find_item(player, request.item_id)
            if not item:
                return JSONResponse(
                    {"error": "Not in your inventory.", "reason": "missing"}, status_code=400
                )
            player.quick_slot_item_id = progression.item_key(item)
        else:
            player.quick_slot_item_id = None
        db.commit()
        quick_slot = progression.quick_slot_payload(player)

    return {"success": True, "quick_slot": quick_slot, "state": build_player_state(player_id)}


@app.post("/api/player/{player_id}/use-consumable")
async def use_consumable(player_id: str, request: UseConsumableRequest):
    """Drink/apply one consumable. Hearts apply only IN combat, health steps
    only out of it — one item, two honest readings of the fiction."""
    init_db(get_active_db_path())
    with get_session() as db:
        player = db.query(Player).filter(Player.id == player_id).first()
        if not player:
            return JSONResponse({"error": "Player not found", "reason": "missing"}, status_code=404)
        result = progression.apply_consumable(db, player, request.item_id, request.in_combat)
        if "error" in result:
            return JSONResponse(result, status_code=400)
        db.commit()

    pending_actions.record_action(
        player_id, "consume",
        pending_actions.consume_note(
            result["item_name"], result["remaining"],
            effect=result["effect_tag"], in_combat=request.in_combat,
        ),
    )
    return {**result, "state": build_player_state(player_id)}


# =====================
# Canvas play: travel, interaction, ambient chatter
# =====================

class TravelRequest(BaseModel):
    player_id: str
    destination_id: str
    source: str = "door"        # "door" | "chooser" — logging only


class InteractRequest(BaseModel):
    player_id: str
    location_id: str
    object_id: str


@app.post("/api/travel")
async def canvas_travel(request: TravelRequest):
    """Walk through an unlocked door — no LLM, no DM turn.

    Blocked while a turn streams: a mid-turn location change desyncs the DM's
    own location reads and races the frontend's scene load. (A mid-turn pickup
    is transactional and harmless, so /api/interact has no such guard.)
    """
    init_db(get_active_db_path())
    if _play_lock.locked():
        return JSONResponse({"success": False, "busy": True}, status_code=409)

    result = await asyncio.to_thread(travel_now, request.player_id, request.destination_id)
    if result.get("blocked"):
        result["chat_prompt"] = f"I try to get through to {result['destination_name']}."
        return JSONResponse({"success": False, **result}, status_code=409)
    if "error" in result:
        status = 404 if result["error"] in ("not_found", "no_player") else 403
        return JSONResponse({"success": False, **result}, status_code=status)

    logger.info(
        f"Canvas travel ({request.source}): {result['from_name']} -> {result['destination_name']}"
    )
    pending_actions.record_action(
        request.player_id, "move",
        f"{result['from_name']} -> {result['destination_name']}",
        tools=("move_player", "advance_time"),
        minutes=result["minutes"], first_visit=result["first_visit"],
    )
    return {
        "success": True, **result,
        "state": build_player_state(request.player_id),
        "objectives_done": objectives_mod.pop_pending_events(request.player_id),
    }


# HTTP status for each way an interaction can refuse
_INTERACT_ERROR_STATUS = {
    "no such object": 404, "no player": 404, "not here": 409,
    "cannot_afford": 409, "no_rest_here": 409, "dead": 409, "no_clock": 409,
}


class RestRequest(BaseModel):
    player_id: str


class ShopTradeRequest(BaseModel):
    player_id: str
    item_id: str
    quantity: int = 1


def _shop_view(db, player, npc) -> dict[str, Any]:
    """What the counter shows: their shelf priced for THIS player, and what
    the player could sell. Numbers are the engine's; the client renders."""
    mul, reason = standing.price_multiplier(db, player, npc)
    stock = []
    for entry in _validate_inventory(npc.inventory_notable or []):
        mech = progression.mechanize_item(entry) or {}
        price = standing.buy_price(standing.base_price(entry), mul)
        stock.append({
            "item_id": progression.item_key(entry),
            "name": entry.get("name", "?"),
            "quantity": int(entry.get("quantity", 1) or 1),
            "type": entry.get("type", "misc"),
            "description": entry.get("description", ""),
            "slot": mech.get("slot"),
            "tier": progression.tier_label(mech.get("tier")),
            "effect_tag": mech.get("effect_tag"),
            "price": price,
            "affordable": (player.currency or 0) >= price,
        })
    sellable = []
    for entry in (player.inventory or []):
        if not isinstance(entry, dict):
            continue
        notes = progression.annotate_item(player, entry)
        equipped = bool(notes.get("equipped"))
        is_quest = str(entry.get("type") or "") == "quest_item"
        sellable.append({
            "item_id": progression.item_key(entry),
            "name": entry.get("name", "?"),
            "quantity": int(entry.get("quantity", 1) or 1),
            "price": standing.sell_price(standing.base_price(entry)),
            "equipped": equipped,
            "sellable": not equipped and not is_quest,
            "reason": "worn" if equipped else ("not for sale" if is_quest else None),
        })
    faction_band = standing.standing_for(player, npc.faction_id) if npc.faction_id else "neutral"
    relationship = standing.relationship_for(db, player.id, npc.id)
    return {
        "npc_id": npc.id,
        "npc_name": npc.name,
        "standing": faction_band,
        "disposition": standing.disposition_band(relationship),
        "reason": reason,
        "multiplier": mul,
        "currency": player.currency or 0,
        "npc_currency": standing.npc_purse(npc),
        "stock": stock,
        "sellable": sellable,
    }


def _shop_guard(db, player_id: str, npc_id: str, *, need_stock: bool):
    """(player, npc, error_response). The refusals a counter can give."""
    player = db.query(Player).filter(Player.id == player_id).first()
    if not player:
        return None, None, JSONResponse({"error": "Player not found", "reason": "missing"}, status_code=404)
    npc = db.get(NPC, npc_id)
    if not npc or npc.status != "alive":
        return None, None, JSONResponse({"error": "No one is trading here", "reason": "missing"}, status_code=404)
    if npc.current_location_id != player.current_location_id:
        return None, None, JSONResponse({"error": f"{npc.name} is not here", "reason": "not_here"}, status_code=409)
    if need_stock and not (npc.inventory_notable or []) and standing.npc_purse(npc) is None:
        return None, None, JSONResponse({"error": f"{npc.name} has nothing to trade", "reason": "no_shop"}, status_code=404)
    refusal = standing.conversation_refusal(db, player, npc)
    if refusal:
        return None, None, JSONResponse(
            {"error": f"{npc.name} will not trade with you", "reason": "standing", "line": refusal["line"]},
            status_code=409,
        )
    return player, npc, None


@app.get("/api/shop/{npc_id}")
async def shop_view(npc_id: str, player_id: str):
    """A merchant's counter, priced for this player. No DM turn."""
    init_db(get_active_db_path())
    with get_session() as db:
        player, npc, err = _shop_guard(db, player_id, npc_id, need_stock=True)
        if err:
            return err
        return _shop_view(db, player, npc)


async def _shop_trade(npc_id: str, request: ShopTradeRequest, *, selling: bool):
    init_db(get_active_db_path())
    with get_session() as db:
        player, npc, err = _shop_guard(db, request.player_id, npc_id, need_stock=not selling)
        if err:
            return err
        if selling:
            entry = next((e for e in (player.inventory or [])
                          if isinstance(e, dict) and progression.item_key(e) == request.item_id), None)
            if entry is None:
                return JSONResponse({"error": "You do not carry that", "reason": "missing"}, status_code=404)
            if progression.annotate_item(player, entry).get("equipped"):
                return JSONResponse({"error": "Take it off first", "reason": "equipped"}, status_code=409)
            if str(entry.get("type") or "") == "quest_item":
                return JSONResponse({"error": "That is not for sale", "reason": "quest_item"}, status_code=409)
        npc_name = npc.name

    quantity = max(1, int(request.quantity or 1))
    if selling:
        result = transfer_item(request.player_id, npc_id, request.item_id, quantity,
                               from_type="player", to_type="npc", is_purchase=True)
    else:
        result = transfer_item(npc_id, request.player_id, request.item_id, quantity,
                               from_type="npc", to_type="player", is_purchase=True)
    if "error" in result:
        reason = "coin" if "afford" in result["error"] or "currency" in result["error"] else "missing"
        return JSONResponse({**result, "reason": reason}, status_code=409 if reason == "coin" else 404)

    with get_session() as db:
        player = db.query(Player).filter(Player.id == request.player_id).first()
        npc = db.get(NPC, npc_id)
        item_name = next((e.get("name") for e in ((npc.inventory_notable if selling else player.inventory) or [])
                          if isinstance(e, dict) and progression.item_key(e) == request.item_id), request.item_id)
        shop = _shop_view(db, player, npc)
    pending_actions.record_action(
        request.player_id, "shop",
        pending_actions.shop_note("sold" if selling else "bought", str(item_name), quantity, npc_name, result["cost"]),
        tools=("transfer_item", "adjust_currency"),
    )
    if not selling:
        _prewarm_appearance(request.player_id)
    return {**result, "shop": shop, "state": build_player_state(request.player_id)}


@app.post("/api/shop/{npc_id}/buy")
async def shop_buy(npc_id: str, request: ShopTradeRequest):
    """Buy from the counter: the engine's price, the player's coin, no DM turn."""
    return await _shop_trade(npc_id, request, selling=False)


@app.post("/api/shop/{npc_id}/sell")
async def shop_sell(npc_id: str, request: ShopTradeRequest):
    """Sell at the counter: the merchant actually pays (buyback fraction)."""
    return await _shop_trade(npc_id, request, selling=True)


@app.post("/api/rest")
async def canvas_rest(request: RestRequest):
    """Sleep the night where the player stands — code owns the whole thing.

    Blocked while a turn streams (rest moves the clock, same reason
    /api/travel guards). The day tick runs in the background afterwards;
    the campaign tick waits for the next turn, where it has a turn to ride on.
    """
    init_db(get_active_db_path())
    if _play_lock.locked():
        return JSONResponse({"success": False, "busy": True}, status_code=409)
    result = await asyncio.to_thread(rest_now, request.player_id)
    if "error" in result:
        status = 404 if result["error"] == "no_player" else 409
        return JSONResponse({"success": False, **result}, status_code=status)
    asyncio.create_task(asyncio.to_thread(maybe_advance_world))
    return {**result, "state": build_player_state(request.player_id)}

# A read's summary quotes the words; anything longer is a paragraph
_INTERACT_SUMMARY_MAX = 90


def _interact_summary(result: dict[str, Any]) -> str:
    """The one-line note the DM reads: what they picked up, or what they read."""
    label = result.get("label") or ""
    items = result.get("result", {}).get("items_gained") or []
    coin = result.get("result", {}).get("coin_gained") or 0
    if items or coin:
        names = [str(i.get("name") or "") for i in items if i.get("name")]
        if coin:
            names.append(f"{coin} coin")
        found = ", ".join(names) or label
        return f"{found} (from {label})" if result.get("kind") == "body" else found
    text = result.get("result", {}).get("panel_text")
    if text:
        return f'{label} ("{str(text)[:_INTERACT_SUMMARY_MAX]}")'
    return label


@app.post("/api/interact")
async def canvas_interact(request: InteractRequest):
    """Touch a scene object. Code owns the whole interaction: the payloads
    were stripped of numbers and effects when they were authored."""
    init_db(get_active_db_path())
    result = await asyncio.to_thread(
        resolve_interaction, request.player_id, request.location_id, request.object_id
    )

    if result.get("locked"):
        return JSONResponse(result, status_code=409)
    if result.get("spent"):
        return JSONResponse(result, status_code=409)
    if not result.get("success"):
        return JSONResponse(
            result, status_code=_INTERACT_ERROR_STATUS.get(result.get("error"), 400)
        )

    items = result.get("result", {}).get("items_gained") or []
    coin = result.get("result", {}).get("coin_gained") or 0
    tools: list[str] = []
    if items:
        tools.append("add_to_inventory")
    if coin:
        tools.append("adjust_currency")
    if result["kind"] == "rest":
        # rest_now wrote its own ledger line; the day tick is the same as /api/rest
        asyncio.create_task(asyncio.to_thread(maybe_advance_world))
    else:
        pending_actions.record_action(
            request.player_id, "loot" if result["kind"] == "body" else result["kind"],
            _interact_summary(result), tools=tuple(tools),
        )
    return {
        **result,
        "state": build_player_state(request.player_id),
        "objectives_done": objectives_mod.pop_pending_events(request.player_id),
    }


# Poll cadence for the client: a settled pool, and a shorter retry while one
# is generating (one more poll, then it gives up until the next scene load)
BARK_REFRESH_MS = 180000
BARK_PENDING_MS = 20000


@app.get("/api/barks/{location_id}")
async def get_location_barks(location_id: str):
    """Ambient one-liners for a scene. Always serves what is cached — a stale
    pool beats silence — and regenerates in the background when it goes off."""
    init_db(get_active_db_path())
    if not barks_service.barks_enabled():
        return {"location_id": location_id, "barks": [], "pending": False, "unavailable": True,
                "refresh_after_ms": BARK_REFRESH_MS}

    with get_session() as db:
        from src.models import WorldClock
        location = db.query(Location).filter(Location.id == location_id).first()
        if not location:
            return JSONResponse({"error": "Location not found"}, status_code=404)
        clock = db.query(WorldClock).first()
        barks = barks_service.cached_barks(location)
        stale = barks_service.barks_stale(location, clock)

    pending = False
    if stale:
        pending = get_asset_manager().spawn_generation(
            f"barks:{location_id}",
            lambda: barks_service.generate_barks(location_id),
        )

    return {
        "location_id": location_id,
        "barks": barks,
        "pending": pending,
        "unavailable": False,
        "refresh_after_ms": BARK_PENDING_MS if pending else BARK_REFRESH_MS,
    }


# =====================
# Campaign + cinematics (read models; the story writes itself in code)
# =====================

class CampaignGenerateRequest(BaseModel):
    force: bool = False


@app.get("/api/campaign/{player_id}")
async def get_campaign_view(player_id: str, spoilers: int = 0):
    """The player-facing campaign view. Sealed truth STATEMENTS are never
    serialized — a curious player with devtools must not read the ending.
    `?spoilers=1` returns them and is a documented author/debug flag."""
    init_db(get_active_db_path())
    return campaign_public_view(player_id, spoilers=bool(spoilers))


@app.post("/api/campaign/{player_id}/generate")
async def generate_campaign(player_id: str, request: CampaignGenerateRequest | None = None):
    """Author (or re-author) this character's campaign off-thread."""
    init_db(get_active_db_path())
    force = bool(request.force) if request else False
    return await asyncio.to_thread(ensure_campaign, player_id, None, force)


@app.get("/api/cinematics/recent")
async def get_recent_cinematics(limit: int = 20):
    """The cinematic ledger — played, pending and rejected. Read-only, for
    playtest scoreboards ('did the DM try to spam title cards?')."""
    init_db(get_active_db_path())
    return {"cinematics": recent_cinematics(limit)}


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


@app.post("/api/world/locations/{location_id}/detect-obstacles")
async def detect_location_obstacles(location_id: str):
    """Run obstacle detection on a location's existing background image.

    Used by the collision editor's auto-detect and for upgrading locations
    generated before obstacle detection existed. Overwrites stored polygons.
    """
    init_db(get_active_db_path())
    with get_session() as db:
        loc = db.query(Location).filter(Location.id == location_id).first()
        if not loc:
            return JSONResponse({"error": "Location not found"}, status_code=404)
        bg_path = loc.background_image_path
    if not bg_path or not Path(bg_path).exists():
        return JSONResponse({"error": "No background image yet"}, status_code=409)

    asset_manager = get_asset_manager()
    try:
        obstacles = await asset_manager.image_gen.detect_obstacles(Path(bg_path).read_bytes())
    except Exception as e:
        logger.error(f"Obstacle detection failed: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

    with get_session() as db:
        loc = db.query(Location).filter(Location.id == location_id).first()
        if loc:
            loc.obstacles = obstacles
            db.commit()
    return {"success": True, "obstacles": obstacles}


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

@app.post("/api/assets/reference/{entity_id}")
async def upload_reference(entity_id: str, file: UploadFile = File(...)):
    """Upload a reference image for an entity (NPC or location)."""
    asset_manager = get_asset_manager()
    ref_path = asset_manager.ref_search._get_reference_path(entity_id)
    miss_marker = ref_path.with_suffix(".miss")

    contents = await file.read()
    if len(contents) < 100:
        return {"error": "File too small to be a valid image"}

    ref_path.write_bytes(contents)

    # Clear miss marker if it exists
    if miss_marker.exists():
        miss_marker.unlink()

    return {"success": True, "path": str(ref_path)}


@app.get("/api/assets/reference/{entity_id}")
async def get_reference(entity_id: str):
    """Get reference image for an entity if it exists."""
    asset_manager = get_asset_manager()
    ref_path = asset_manager.ref_search._get_reference_path(entity_id)

    if ref_path.exists() and ref_path.stat().st_size > 0:
        return FileResponse(ref_path, media_type="image/png")

    return {"error": "No reference image found"}


@app.delete("/api/assets/reference/{entity_id}")
async def delete_reference(entity_id: str):
    """Delete reference image and miss marker for an entity."""
    asset_manager = get_asset_manager()
    ref_path = asset_manager.ref_search._get_reference_path(entity_id)
    miss_marker = ref_path.with_suffix(".miss")

    deleted = False
    if ref_path.exists():
        ref_path.unlink()
        deleted = True
    if miss_marker.exists():
        miss_marker.unlink()
        deleted = True

    if deleted:
        return {"success": True}
    return {"error": "No reference image or miss marker found"}


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
    reference_search_query: str | None = None
    # Combat authoring. `boss` is authored-only — it never arrives by
    # inference and the DM's update_npc tool refuses it; the world editor is
    # the one place it can be set. `boss_title` is a display epithet.
    danger_tier: str | None = None
    combat_archetype: str | None = None
    boss_title: str | None = None


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
    obstacles: list | None = None  # collision polygons, normalized 0-100
    objects: list | None = None    # canvas interactables (normalized on write)
    dress_code: dict | None = None  # door policy (normalized on write; {} clears)
    rest: dict | None = None        # where to sleep (normalized on write; {} clears)
    reference_search_query: str | None = None


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
            "pc_guidelines": bible.pc_guidelines,
            "pc_starting_situation": bible.pc_starting_situation,
            "pc_suggested_name": getattr(bible, "pc_suggested_name", ""),
            "pc_suggested_description": getattr(bible, "pc_suggested_description", ""),
            "pc_suggested_background": getattr(bible, "pc_suggested_background", ""),
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
            "reference_search_query": npc.reference_search_query,
            "danger_tier": npc.danger_tier,
            "combat_archetype": npc.combat_archetype,
            "boss_title": npc.boss_title,
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
        if update.danger_tier is not None:
            tier = update.danger_tier.strip().lower()
            if tier and tier not in DANGER_TIERS:
                return {"error": f"Unknown danger_tier {update.danger_tier!r}",
                        "valid": list(DANGER_TIERS)}
            npc.danger_tier = tier or None
        if update.combat_archetype is not None:
            archetype = update.combat_archetype.strip().lower()
            if archetype and archetype not in COMBAT_ARCHETYPES:
                return {"error": f"Unknown combat_archetype {update.combat_archetype!r}",
                        "valid": list(COMBAT_ARCHETYPES)}
            npc.combat_archetype = archetype or None
        if update.boss_title is not None:
            npc.boss_title = update.boss_title.strip() or None
        if update.reference_search_query is not None:
            old_query = npc.reference_search_query
            npc.reference_search_query = update.reference_search_query if update.reference_search_query != "" else None
            # Clear miss marker when query changes so a new search can happen
            if npc.reference_search_query != old_query:
                asset_manager = get_asset_manager()
                miss_marker = asset_manager.ref_search._get_reference_path(npc.id).with_suffix(".miss")
                if miss_marker.exists():
                    miss_marker.unlink()

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


class PlayerCreate(BaseModel):
    """Request model for creating a new player character."""
    name: str
    description: str = ""
    background: str = ""
    location_id: str | None = None       # Defaults to a sensible spawn point
    predecessor_id: str | None = None    # Dead character being succeeded


@app.post("/api/world/players")
async def create_player(req: PlayerCreate):
    """Create a new player character in the current world.

    Used by the death/rebirth flow: the world (and everything the previous
    character did to it) persists; a new character steps into it.
    """
    init_db(get_active_db_path())

    with get_session() as db:
        location_id = req.location_id

        # Spawn where the predecessor fell, else any visited location,
        # else the first location in the world
        if not location_id and req.predecessor_id:
            predecessor = db.query(Player).filter(Player.id == req.predecessor_id).first()
            if predecessor:
                location_id = predecessor.current_location_id
        if not location_id:
            loc = (db.query(Location).filter(Location.visited == True).first()  # noqa: E712
                   or db.query(Location).first())
            location_id = loc.id if loc else None

        player = Player(
            name=req.name,
            description=req.description,
            background=req.background,
            current_location_id=location_id,
            currency=25,
        )
        db.add(player)
        db.commit()
        player_id = player.id

    # A character deserves a story of their own. Authoring it is one fast-model
    # call that takes 10-30s — fire and forget, never on the response path. A
    # failure degrades to exactly today's game.
    asyncio.create_task(
        asyncio.to_thread(ensure_campaign, player_id, req.predecessor_id)
    )

    return {"success": True, "player_id": player_id, "location_id": location_id}


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

    # The name-modal save is the moment a character becomes someone; the spine
    # is authored here too (no-op when a live row already exists), so a world
    # whose player row predates POST /api/world/players still gets a story
    try:
        await asyncio.to_thread(ensure_campaign, player_id)
    except Exception as e:
        logger.debug(f"ensure_campaign on player update skipped: {e}")
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
            "objects": loc.objects or [],
            "dress_code": loc.dress_code,
            "rest": loc.rest,
            "reference_search_query": loc.reference_search_query,
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
        if update.obstacles is not None:
            loc.obstacles = update.obstacles
        if update.objects is not None:
            # Never trust authored objects: the normalizer strips effects,
            # numbers and unknown kinds before anything reaches a scene
            loc.objects = normalize_objects(update.objects, trusted=False) or None
        if update.rest is not None:
            loc.rest = normalize_rest(update.rest)
        if update.dress_code is not None:
            # Clamped to the fixed menu; an empty gate stores as NULL
            loc.dress_code = perception.normalize_dress_code(update.dress_code)
        if update.reference_search_query is not None:
            old_query = loc.reference_search_query
            loc.reference_search_query = update.reference_search_query if update.reference_search_query != "" else None
            # Clear miss marker when query changes so a new search can happen
            if loc.reference_search_query != old_query:
                asset_manager = get_asset_manager()
                miss_marker = asset_manager.ref_search._get_reference_path(loc.id).with_suffix(".miss")
                if miss_marker.exists():
                    miss_marker.unlink()

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
            "objectives": objectives_mod.objectives_public(quest),
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
            quest.objectives = objectives_mod.normalize_objectives(
                update.objectives, previous=quest.objectives, session=db
            )
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
                "objectives": objectives_mod.objectives_public(q),
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
            objectives=objectives_mod.normalize_objectives(data.objectives, session=db),
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

            tool_tracker = ToolUsageTracker()
            world_forge = WorldForge(session_id)

            # Stream the response
            async for response in world_forge.agent.stream_async(request.query):
                # Process through tool tracker
                tool_tracker.process_stream_payload(response)

                # Yield tool notifications
                for notification in tool_tracker.drain_notifications():
                    encoded = json.dumps(notification, ensure_ascii=False, default=str)
                    yield f"data: {encoded}\n\n"

                if isinstance(response, dict) and "data" in response:
                    token_data = response["data"]
                    if token_data:
                        token_event = {"type": "token", "data": token_data}
                        yield f"data: {json.dumps(token_event)}\n\n"

            # Drain any remaining notifications
            for notification in tool_tracker.drain_notifications():
                encoded = json.dumps(notification, ensure_ascii=False, default=str)
                yield f"data: {encoded}\n\n"

            # Final event with tool summary
            yield f"data: {json.dumps({'type': 'complete', 'tools': tool_tracker.snapshot()})}\n\n"

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

        # Try to get world bible info (raw SQL to avoid ORM column mismatch on old DBs)
        try:
            from sqlalchemy import create_engine, text as sa_text
            engine = create_engine(f"sqlite:///{db_file}", echo=False)
            with engine.connect() as conn:
                row = conn.execute(sa_text("SELECT name, tagline, setting_description, genre FROM world_bible LIMIT 1")).first()
                if row:
                    world_info["has_world_bible"] = True
                    world_info["world_name"] = row[0]
                    world_info["description"] = row[1] or (row[2][:200] if row[2] else None)
                    world_info["genre"] = row[3]
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
    global sessions, _asset_manager

    db_path = request.db_path

    if not Path(db_path).exists():
        return {"success": False, "error": f"Database not found: {db_path}"}

    # Clear all active sessions (they're tied to the old database), plus the
    # per-player caches keyed by ids that belong to the world we are leaving
    sessions.clear()
    _asset_manager = None
    reset_campaign_state()

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
    global sessions, _asset_manager

    # Sanitize name for filename
    safe_name = "".join(c for c in request.name if c.isalnum() or c in "._- ").strip()
    if not safe_name:
        return {"success": False, "error": "Invalid world name"}

    db_path = f"data/{safe_name}.db"

    if Path(db_path).exists():
        return {"success": False, "error": f"World '{safe_name}' already exists"}

    # Clear sessions
    sessions.clear()
    _asset_manager = None

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
        global sessions, _asset_manager

        # Clear sessions
        sessions.clear()
        _asset_manager = None

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
            tool_tracker = ToolUsageTracker()

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
                # Process through tool tracker
                tool_tracker.process_stream_payload(response)

                # Yield tool notifications
                for notification in tool_tracker.drain_notifications():
                    encoded = json.dumps(notification, ensure_ascii=False, default=str)
                    yield f"data: {encoded}\n\n"

                if isinstance(response, dict) and "data" in response:
                    token_data = response["data"]
                    if token_data:
                        yield f"data: {json.dumps({'type': 'token', 'data': token_data})}\n\n"

            # Drain any remaining notifications
            for notification in tool_tracker.drain_notifications():
                encoded = json.dumps(notification, ensure_ascii=False, default=str)
                yield f"data: {encoded}\n\n"

            yield f"data: {json.dumps({'type': 'complete', 'db_path': db_path, 'message': f'World {safe_name} created!', 'tools': tool_tracker.snapshot()})}\n\n"

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
